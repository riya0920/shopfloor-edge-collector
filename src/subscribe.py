"""A driver contract that can tell "no change" from "no answer".

The OPC-UA leg found the mismatch and the README stated it plainly:

    a `Driver` interface built around polling has to hide a subscription behind
    a poll, and a collector that reads "no data" as "device down" will declare a
    healthy machine dead the moment its temperature stops moving -- which on an
    idle machine is most of the weekend.

The diagnosis was right and the fix is not a bigger poll. It is a contract in
which a fetch returns two independent things:

    WHAT THE DEVICE SAID     zero or more readings, each with its own quality
    WHETHER THE DEVICE IS THERE   liveness, derived from CONTACT and never from
                                  whether any value moved

Those are different questions and the poll-shaped interface could only answer
them together. A polled device answers both at once because a poll that returns
data proves contact; a subscribed device does not, and that is the entire
difference. So the subscription contract carries a KEEP-ALIVE: the publisher
promises to send *something* at least every `keepalive_s`, an empty publish if it
has no news, and silence past that deadline is the only evidence of a dead
device that either transport can offer.

WHY LIVENESS IS THREE-VALUED AND NOT TWO. FRESH and SILENT are not enough,
because the useful state is the middle one: the device answered, on time, and
had nothing to say. Collapsing that into FRESH loses the ability to say how old
the newest value is; collapsing it into SILENT is the original bug. So:

    FRESH      new data arrived within the deadline
    KEEPALIVE  no new data, but the device checked in within the deadline
    SILENT     nothing within the deadline -- and only this one means down

The polled drivers keep working unchanged: `PolledAdapter` wraps any existing
`Driver` and reports FRESH on a successful poll and SILENT on a failed one,
because for a polled device those really are the only two outcomes.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

FRESH, KEEPALIVE, SILENT = "FRESH", "KEEPALIVE", "SILENT"

GOOD, NO_DATA, BAD = "GOOD", "NO_DATA", "BAD"


@dataclass
class Reading:
    """One value, with the quality that belongs to it rather than to the fetch."""
    tag: str
    value: float | None
    quality: str = GOOD
    source_ts: float = 0.0


@dataclass
class Fetch:
    """The result of one collection cycle.

    `readings` may be empty on a perfectly healthy subscribed device. That is the
    case the old contract could not express, and it is why `liveness` is a
    separate field rather than something the collector infers from `len()`.
    """
    readings: list = field(default_factory=list)
    liveness: str = FRESH
    last_contact_s: float = 0.0      # seconds since the device last said anything
    keepalives: int = 0
    notifications: int = 0

    @property
    def healthy(self) -> bool:
        return self.liveness != SILENT

    def values(self) -> dict:
        return {r.tag: r.value for r in self.readings}


class SubscriptionDriver:
    """Push-capable driver. Subclasses implement `_drain` and `_subscribe`.

    `keepalive_s` is the interval the PUBLISHER promises, and `deadline_s` is
    what the subscriber will wait before calling it dead. They are separate
    numbers on purpose: setting the deadline equal to the interval declares a
    device dead on the first late packet, and a deadline is a statement about
    tolerance for jitter, not a restatement of the interval. The default of three
    missed keep-alives is the usual one and it is a choice, so it is named.
    """

    def __init__(self, name: str, keepalive_s: float = 1.0,
                 missed_allowed: int = 3):
        self.name = name
        self.keepalive_s = float(keepalive_s)
        self.missed_allowed = int(missed_allowed)
        self.subscribed = False
        self.last_contact = 0.0
        self.notifications = 0
        self.keepalives = 0
        self.silences = 0

    @property
    def deadline_s(self) -> float:
        return self.keepalive_s * (self.missed_allowed + 1)

    async def subscribe(self) -> bool:
        ok = await self._subscribe()
        self.subscribed = bool(ok)
        if ok:
            self.last_contact = time.monotonic()
        return self.subscribed

    async def _subscribe(self) -> bool:      # pragma: no cover - interface
        raise NotImplementedError

    async def _drain(self) -> tuple:         # pragma: no cover - interface
        """Return (readings, saw_contact). `saw_contact` is True when the device
        said ANYTHING, including an empty keep-alive publish."""
        raise NotImplementedError

    async def fetch(self, now: float | None = None) -> Fetch:
        now = time.monotonic() if now is None else now
        readings, contact = await self._drain()
        if contact:
            self.last_contact = now
        age = now - self.last_contact
        if readings:
            self.notifications += 1
            live = FRESH
        elif contact:
            self.keepalives += 1
            live = KEEPALIVE
        elif age > self.deadline_s:
            self.silences += 1
            live = SILENT
        else:
            # Inside the deadline with nothing heard yet. Not a keep-alive --
            # nothing was received -- but not dead either. Reported as KEEPALIVE
            # because the contract's promise (heard from recently enough) still
            # holds, and the age is carried so a caller that cares can see it.
            live = KEEPALIVE
        return Fetch(readings=readings, liveness=live, last_contact_s=age,
                     keepalives=self.keepalives, notifications=self.notifications)

    async def close(self) -> None:
        self.subscribed = False


class PolledAdapter:
    """Wraps an existing poll-shaped `Driver` in the new contract.

    A polled device genuinely has only two outcomes -- it answered or it did not
    -- so this reports FRESH or SILENT and never KEEPALIVE. That asymmetry is the
    honest one: the adapter must not manufacture a liveness signal the transport
    does not provide.
    """

    def __init__(self, driver, name: str | None = None):
        self.driver = driver
        self.name = name or getattr(driver.profile, "name", "polled")
        self.last_contact = 0.0
        self.notifications = 0
        self.keepalives = 0
        self.silences = 0

    @property
    def deadline_s(self) -> float:
        return 0.0

    async def subscribe(self) -> bool:
        ok = await self.driver.connect()
        if ok:
            self.last_contact = time.monotonic()
        return bool(ok)

    async def fetch(self, now: float | None = None) -> Fetch:
        now = time.monotonic() if now is None else now
        try:
            raw = await self.driver.poll()
        except Exception:                                   # noqa: BLE001
            raw = None
        if raw is None:
            self.silences += 1
            return Fetch(readings=[], liveness=SILENT,
                         last_contact_s=now - self.last_contact)
        self.last_contact = now
        self.notifications += 1
        readings = [Reading(tag=k, value=v,
                            quality=GOOD if v is not None else NO_DATA,
                            source_ts=now)
                    for k, v in raw.items()]
        return Fetch(readings=readings, liveness=FRESH, last_contact_s=0.0,
                     notifications=self.notifications)

    async def close(self) -> None:
        await self.driver.close()


# ---------------------------------------------------------------------------
# a subscription that behaves like OPC-UA
# ---------------------------------------------------------------------------

class ChangeDrivenDevice:
    """An in-process stand-in for a device that publishes on CHANGE.

    Deliberately faithful about the one behaviour that matters: writing the SAME
    value produces no notification. That is what the real OPC-UA leg did, and it
    is what breaks a poll-shaped contract.
    """

    def __init__(self, tags: dict, keepalive_s: float = 1.0,
                 deadband: float = 0.0):
        self.values = dict(tags)
        self.queue: list = []
        self.keepalive_s = float(keepalive_s)
        self.deadband = float(deadband)
        self.last_publish = 0.0
        self.alive = True
        self.suppressed = 0
        # First publish carries the initial value of every tag, which is why a
        # subscription reports n+1 notifications for n writes.
        for k, v in self.values.items():
            self.queue.append(Reading(tag=k, value=v, source_ts=0.0))

    def write(self, tag: str, value: float, ts: float = 0.0) -> bool:
        old = self.values.get(tag)
        if old is not None and abs(value - old) <= self.deadband:
            self.suppressed += 1
            self.values[tag] = value
            return False
        self.values[tag] = value
        self.queue.append(Reading(tag=tag, value=value, source_ts=ts))
        return True

    def drain(self, now: float) -> tuple:
        """Everything queued, plus whether the publisher checked in."""
        if not self.alive:
            return [], False
        out, self.queue = self.queue, []
        if out:
            self.last_publish = now
            return out, True
        if now - self.last_publish >= self.keepalive_s:
            self.last_publish = now
            return [], True                      # an empty keep-alive publish
        return [], False


class ChangeDrivenDriver(SubscriptionDriver):
    def __init__(self, device: ChangeDrivenDevice, name: str = "opcua-sim",
                 keepalive_s: float = 1.0, missed_allowed: int = 3):
        super().__init__(name, keepalive_s=keepalive_s,
                         missed_allowed=missed_allowed)
        self.device = device

    async def _subscribe(self) -> bool:
        return True

    async def _drain(self) -> tuple:
        return self.device.drain(time.monotonic())

    async def fetch(self, now: float | None = None) -> Fetch:
        now = time.monotonic() if now is None else now
        readings, contact = self.device.drain(now)
        if contact:
            self.last_contact = now
        age = now - self.last_contact
        if readings:
            self.notifications += 1
            live = FRESH
        elif contact:
            self.keepalives += 1
            live = KEEPALIVE
        elif age > self.deadline_s:
            self.silences += 1
            live = SILENT
        else:
            live = KEEPALIVE
        return Fetch(readings=readings, liveness=live, last_contact_s=age,
                     keepalives=self.keepalives, notifications=self.notifications)


# ---------------------------------------------------------------------------
# the health decision, old and new
# ---------------------------------------------------------------------------

def health_old_contract(values: dict | None) -> str:
    """What the poll-shaped collector did: no data means down.

    Kept, and kept honest -- this is the behaviour being replaced, and having it
    runnable is the only way the comparison below is a measurement rather than
    an assertion.
    """
    if not values:
        return "DOWN"
    return "UP"


def health_new_contract(f: Fetch) -> str:
    return "DOWN" if f.liveness == SILENT else "UP"


async def compare_contracts(steady_s: float = 2.5, tick_s: float = 0.05,
                            keepalive_s: float = 0.5,
                            missed_allowed: int = 3) -> dict:
    """Run one change-driven device that goes quiet, and score both contracts.

    The device is healthy throughout the steady period -- it simply has nothing
    new to say, which is what an idle machine looks like. Then it is killed, and
    the new contract has to notice within its own deadline rather than instantly:
    a deadline that fires immediately is the old bug wearing a new name.
    """
    dev = ChangeDrivenDevice({"temp_C": 60.0}, keepalive_s=keepalive_s)
    drv = ChangeDrivenDriver(dev, keepalive_s=keepalive_s,
                             missed_allowed=missed_allowed)
    await drv.subscribe()

    old_down = new_down = ticks = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < steady_s:
        f = await drv.fetch()
        ticks += 1
        if health_old_contract(f.values()) == "DOWN":
            old_down += 1
        if health_new_contract(f) == "DOWN":
            new_down += 1
        await asyncio.sleep(tick_s)

    # Now the device really dies.
    dev.alive = False
    died = time.monotonic()
    detect_s = None
    for _ in range(400):
        f = await drv.fetch()
        if health_new_contract(f) == "DOWN":
            detect_s = time.monotonic() - died
            break
        await asyncio.sleep(tick_s)

    return {
        "ticks": ticks, "steady_s": steady_s, "tick_s": tick_s,
        "keepalive_s": keepalive_s, "missed_allowed": missed_allowed,
        "deadline_s": drv.deadline_s,
        "old_contract_false_down": old_down,
        "old_contract_false_down_frac": old_down / max(ticks, 1),
        "new_contract_false_down": new_down,
        "keepalives_seen": drv.keepalives,
        "notifications_seen": drv.notifications,
        "detection_s": detect_s,
        "detection_within_deadline": (detect_s is not None
                                      and detect_s <= drv.deadline_s * 1.5),
    }


async def write_repeat_experiment(n_writes: int = 4) -> dict:
    """The original OPC-UA finding, now measured under both contracts.

    n writes where one repeats the previous value produce n notifications, not
    n+1 -- one initial publish plus (n-1) changes. The repeat produces nothing,
    and under the old contract that tick is indistinguishable from a dead device.
    """
    dev = ChangeDrivenDevice({"temp_C": 60.0}, keepalive_s=0.2)
    drv = ChangeDrivenDriver(dev, keepalive_s=0.2)
    await drv.subscribe()
    first = await drv.fetch()                    # the initial publish

    seq = [61.0, 62.0, 62.0, 63.0][:n_writes]
    per_write = []
    for v in seq:
        changed = dev.write("temp_C", v)
        f = await drv.fetch()
        per_write.append({"wrote": v, "device_published": changed,
                          "n_readings": len(f.readings),
                          "liveness": f.liveness,
                          "old_contract": health_old_contract(f.values()),
                          "new_contract": health_new_contract(f)})
    return {"initial_publish_readings": len(first.readings),
            "writes": per_write,
            "n_writes": len(seq),
            "n_notifications": sum(1 for p in per_write if p["n_readings"]),
            "repeat_wrote_nothing": any(
                p["device_published"] is False for p in per_write),
            "old_contract_called_it_down": sum(
                1 for p in per_write if p["old_contract"] == "DOWN"),
            "new_contract_called_it_down": sum(
                1 for p in per_write if p["new_contract"] == "DOWN")}
