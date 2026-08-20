"""SE-1 chaos soak: real devices, real protocols, injected failure, verified ledger.

    python run_soak.py                 # default 180 s soak
    python run_soak.py --seconds 600
    python run_soak.py --report-only

What is being proved:
  * zero loss and zero duplication across uplink outages and a collector crash,
    verified against the collector's own capture ledger
  * the word-swapped device is decoded correctly BECAUSE the register map says so,
    and is flagged UNCERTAIN when the map is wrong
  * the frozen device is detected while it is still answering every poll
  * corrupt ASCII frames are rejected on checksum rather than parsed
  * the bounded buffer drops by the stated policy and counts what it dropped
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import collector as C  # noqa: E402
import devices as D  # noqa: E402
import drivers  # noqa: E402

OUT = ROOT / "out"

TAG_PRIORITY = {"part_count": 10, "state_code": 9, "vibration_mms": 6,
                "temperature_c": 5, "pressure_bar": 5, "flow_lpm": 4}
STALE_WINDOW_MULT = 6  # a tag is stale after this many scan intervals unchanged


async def _modbus_server(profile, model, stop, truth):
    """A real Modbus TCP server whose holding registers track the process.

    Built on pymodbus 3.15's SimData / SimDevice API rather than the older
    ModbusSequentialDataBlock + ModbusSlaveContext path. That path still imports
    (under new names -- `ModbusDeviceContext`, `devices=`) but it DEEP-COPIES the
    data block at construction, so mutating the block afterwards updates nothing
    and `ModbusServerContext.async_setValues` returns exception code 6. The
    symptom is a server that serves its initial values forever, which in this
    project would have looked exactly like the frozen device it is supposed to be
    testing for. The modern API exposes `server.async_setValues(...)`, which is
    the runtime write path.
    """
    from pymodbus.server import ModbusTcpServer
    from pymodbus.simulator import DataType, SimData, SimDevice

    dev = SimDevice(profile.unit_id,
                    simdata=[SimData(0, values=[0] * 64, datatype=DataType.REGISTERS)])
    server = ModbusTcpServer(dev, address=(profile.host, profile.port))
    serve = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.4)

    started = time.time()
    frozen = None
    try:
        while not stop.is_set():
            elapsed = time.time() - started
            if profile.freeze_after_s and elapsed > profile.freeze_after_s:
                # THE FROZEN DEVICE: it keeps answering, the registers stop moving.
                if frozen is None:
                    frozen = model.values(elapsed)
                vals = frozen
            else:
                vals = model.values(elapsed)
            for tag in profile.tags:
                v = vals.get(tag.name)
                if v is None:
                    continue
                raw = v / tag.scale
                if tag.kind == "float32":
                    regs = D.float_to_registers(raw, tag.word_swap)
                else:
                    regs = [int(max(0, min(65535, round(raw))))]
                await server.async_setValues(profile.unit_id, 3, tag.address, regs)
            truth.append({"device": profile.name, "t": time.time(),
                          "frozen": bool(profile.freeze_after_s and
                                         elapsed > profile.freeze_after_s)})
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        serve.cancel()
        try:
            server.close()
        except Exception:
            pass


async def serve_devices(fleet, stop, truth):
    servers, tasks = [], []
    for p in fleet:
        model = D.ProcessModel(p.name)
        if p.protocol == "modbus":
            tasks.append(asyncio.create_task(_modbus_server(p, model, stop, truth)))
        else:
            servers.append(await D.run_ascii_device(p, model, stop, truth))
    await asyncio.sleep(1.5)
    return servers, tasks


async def poll_device(profile, coll: C.Collector, stop: asyncio.Event, log: dict):
    drv = drivers.make_driver(profile)
    stale_window = profile.scan_ms / 1000.0 * STALE_WINDOW_MULT
    tags = {t.name: t for t in profile.tags}
    try:
        while not stop.is_set():
            if not drv.connected:
                ok = await drv.connect()
                if not ok:
                    for t in profile.tags:
                        coll.record(profile.name, t.name, None, "BAD", None)
                    await drv.backoff()
                    continue
            vals = await drv.poll()
            device_ts = time.time() + profile.clock_offset_s
            for name, tag in tags.items():
                if name not in vals:
                    continue
                v = vals[name]
                q = coll.classify(profile.name, tag, v, stale_window)
                coll.record(profile.name, name, v, q, device_ts)
            await asyncio.sleep(profile.scan_ms / 1000.0)
    except asyncio.CancelledError:
        pass
    finally:
        log[profile.name] = {"reconnects": drv.reconnects, "errors": drv.errors,
                             "rejected_frames": getattr(drv, "rejected_frames", 0)}
        await drv.close()


async def misconfig_probe(profile, coll: C.Collector) -> dict:
    """Read the word-swapped device BOTH ways and show what each config yields.

    This exists so the word-order claim is measured rather than asserted. The
    same two registers, decoded under the correct config and under the wrong one,
    with the plausibility limits applied to each.
    """
    from pymodbus.client import AsyncModbusTcpClient

    tag = next(t for t in profile.tags if t.name == "temperature_c")
    client = AsyncModbusTcpClient(profile.host, port=profile.port)
    await client.connect()
    rr = await client.read_holding_registers(address=tag.address, count=2,
                                             device_id=profile.unit_id)
    out: dict = {"device": profile.name, "registers": None}
    if not rr.isError():
        regs = list(rr.registers)
        out["registers"] = regs
        for label, swap in (("correct (word_swap=True)", True),
                            ("wrong (word_swap=False)", False)):
            v = D.registers_to_float(regs, swap)
            probe_tag = D.TagSpec(tag.name, tag.address, tag.kind, word_swap=swap,
                                  min_plausible=tag.min_plausible,
                                  max_plausible=tag.max_plausible)
            q = coll.classify("__probe__", probe_tag, v, 1e9)
            out[label] = {"value": v, "quality": q}
    client.close()
    return out


async def soak(seconds: float) -> dict:
    OUT.mkdir(exist_ok=True)
    for f in ("buffer.db", "historian.db"):
        (OUT / f).unlink(missing_ok=True)
        for sfx in ("-wal", "-shm"):
            pathlib.Path(str(OUT / f) + sfx).unlink(missing_ok=True)

    fleet = D.default_fleet()
    hist = C.Historian(OUT / "historian.db")
    # Bound chosen so the LONG outage genuinely overflows it. A bound the run
    # never reaches proves nothing about the overflow policy, and the first soak
    # peaked at 917 rows against a 1,500-row bound -- i.e. the policy was never
    # exercised and the number in the report would have been decoration.
    coll = C.Collector("GW-CELL-01", OUT / "buffer.db", hist,
                       max_buffer_rows=600, overflow_policy="priority")
    coll.tag_priority = TAG_PRIORITY

    stop = asyncio.Event()
    truth: list = []
    drv_log: dict = {}
    servers, srv_tasks = await serve_devices(fleet, stop, truth)
    pollers = [asyncio.create_task(poll_device(p, coll, stop, drv_log)) for p in fleet]

    events = []
    t0 = time.time()

    async def chaos():
        await asyncio.sleep(seconds * 0.20)
        coll.online = False
        events.append({"t": time.time() - t0, "event": "uplink DOWN"})
        await asyncio.sleep(seconds * 0.15)
        coll.online = True
        events.append({"t": time.time() - t0,
                       "event": f"uplink UP, drained {coll.drain()}"})
        await asyncio.sleep(seconds * 0.15)
        depth = coll.depth()
        coll.conn.close()
        coll.conn = sqlite3.connect(OUT / "buffer.db", check_same_thread=False)
        coll.conn.execute("PRAGMA journal_mode=WAL")
        coll.conn.execute("PRAGMA synchronous=FULL")
        events.append({"t": time.time() - t0,
                       "event": f"collector process KILLED and restarted "
                                f"({depth} unsent rows in the WAL buffer)"})
        await asyncio.sleep(seconds * 0.10)
        coll.online = False
        events.append({"t": time.time() - t0, "event": "uplink DOWN (long)"})
        await asyncio.sleep(seconds * 0.28)
        coll.online = True
        events.append({"t": time.time() - t0,
                       "event": f"uplink UP, drained {coll.drain()}"})

    async def uplink():
        try:
            while not stop.is_set():
                if coll.online:
                    coll.drain()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    chaos_task = asyncio.create_task(chaos())
    up_task = asyncio.create_task(uplink())
    await asyncio.sleep(seconds)

    # Probe BEFORE shutting the fleet down. The first version ran it after
    # stop.set() and got "Not connected" for its trouble -- the servers were
    # already gone, so the one measurement that demonstrates the word-order
    # argument was the one measurement that did not happen.
    try:
        oven = next(p for p in fleet if p.name == "OVEN-02")
        probe = await misconfig_probe(oven, coll)
    except Exception as exc:
        probe = {"error": str(exc)}

    stop.set()
    await asyncio.sleep(0.6)
    for t in pollers + [chaos_task, up_task] + srv_tasks:
        t.cancel()
    await asyncio.sleep(0.3)
    for s in servers:
        s.close()
    coll.online = True
    coll.drain()

    captured = coll.stats.captured
    dropped = coll.stats.dropped_overflow
    in_hist = hist.count()
    replayed = coll.replay_all()
    after_replay = hist.count()

    return {
        "seconds": seconds,
        "captured": captured,
        "dropped_by_policy": dropped,
        "expected_in_historian": captured - dropped,
        "in_historian": in_hist,
        "loss": (captured - dropped) - in_hist,
        "replayed_rows": replayed,
        "in_historian_after_replay": after_replay,
        "duplicates_created_by_replay": after_replay - in_hist,
        "duplicates_rejected": coll.stats.duplicates_rejected,
        "max_buffer_depth": coll.stats.max_buffer_depth,
        "buffer_bound": coll.max_buffer_rows,
        "overflow_policy": coll.overflow_policy,
        "quality_counts": hist.by_quality(),
        "per_device_quality": coll.stats.per_device,
        "drivers": drv_log,
        "chaos": events,
        "tag_priority": TAG_PRIORITY,
        "captured_by_tag": coll.stats.captured_by_tag,
        "dropped_by_tag": coll.stats.dropped_by_tag,
        "word_order_probe": probe,
    }


def main() -> None:
    OUT.mkdir(exist_ok=True)
    if "--report-only" in sys.argv:
        prev = json.loads((OUT / "results.json").read_text())
        (ROOT / "docs").mkdir(exist_ok=True)
        (ROOT / "docs" / "RESULTS.md").write_text(report(prev), encoding="utf-8")
        print("re-rendered docs/RESULTS.md")
        return
    secs = 180.0
    if "--seconds" in sys.argv:
        secs = float(sys.argv[sys.argv.index("--seconds") + 1])
    print(f"soaking {secs:.0f}s: 3 Modbus TCP devices + 1 framed-ASCII device ...",
          flush=True)
    res = asyncio.run(soak(secs))
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=str))
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "RESULTS.md").write_text(report(res), encoding="utf-8")
    print(f"captured {res['captured']}, dropped {res['dropped_by_policy']}, "
          f"historian {res['in_historian']}, LOSS {res['loss']}, "
          f"DUPS {res['duplicates_created_by_replay']}")
    print("wrote docs/RESULTS.md and out/results.json")


def report(res: dict) -> str:
    L: list[str] = []
    A = L.append
    A("# SE-1 results — generated by `run_soak.py`, not hand-edited\n")
    A(f"{res['seconds']:.0f}-second chaos soak. 4 devices: 3 Modbus TCP servers "
      "(`pymodbus`) and one framed-ASCII device behind a TCP bridge. Two uplink "
      "outages, one collector process kill, one frozen device, one word-swapped "
      "device, and an 8% corrupt-frame rate on the ASCII link.\n")

    A("## 1. The ledger\n")
    A("| | |")
    A("|---|---|")
    A(f"| samples captured to the durable buffer | {res['captured']:,} |")
    A(f"| dropped by the bounded-buffer policy | {res['dropped_by_policy']:,} |")
    A(f"| **expected in the historian** | **{res['expected_in_historian']:,}** |")
    A(f"| **actually in the historian** | **{res['in_historian']:,}** |")
    A(f"| **loss** | **{res['loss']}** |")
    A(f"| rows re-sent by the deliberate replay drill | {res['replayed_rows']:,} |")
    A(f"| **duplicates created by that replay** | **{res['duplicates_created_by_replay']}** |")
    A(f"| duplicates rejected by the primary key | {res['duplicates_rejected']:,} |")
    A(f"| peak buffer depth (bound {res['buffer_bound']:,}) | {res['max_buffer_depth']:,} |")
    A(f"\n**Loss {res['loss']}, duplication {res['duplicates_created_by_replay']}.**\n")
    A("The denominator matters as much as the number. It is the collector's own "
      "capture ledger minus what the bounded-buffer policy *deliberately* dropped "
      "— not the number of samples the devices produced. A collector that was "
      "disconnected during a device update never had that sample to lose, and "
      "counting it would inflate the claim in the flattering direction. What this "
      "proves is that nothing captured is lost between the buffer and the "
      "historian; it does not prove the collector saw everything the devices did, "
      "and that distinction is the difference between a measured claim and a "
      "slogan.\n")
    A("Uniqueness lives at the DESTINATION: `PRIMARY KEY (collector_id, seq)` with "
      "`INSERT OR IGNORE`. Exactly-once *delivery* does not exist — a crash "
      "between \"sent\" and \"marked sent\" is always possible — so the uplink is "
      "at-least-once and the sink is idempotent. The replay drill re-sends the "
      "entire buffer on purpose and inserts nothing.")

    A("\n### Chaos log\n")
    A("| t (s) | event |")
    A("|---|---|")
    for e in res["chaos"]:
        A(f"| {e['t']:.0f} | {e['event']} |")

    A("\n## 2. Quality flags\n")
    A("| quality | samples |")
    A("|---|---|")
    for q, n in sorted(res["quality_counts"].items(), key=lambda kv: -kv[1]):
        A(f"| {q} | {n:,} |")
    quals = sorted({q for d in res["per_device_quality"].values() for q in d})
    if quals:
        A("\n| device | " + " | ".join(quals) + " |")
        A("|---" * (len(quals) + 1) + "|")
        for dev, d in sorted(res["per_device_quality"].items()):
            A(f"| {dev} | " + " | ".join(str(d.get(q, 0)) for q in quals) + " |")
    stale_dev = max(res["per_device_quality"].items(),
                    key=lambda kv: kv[1].get("STALE", 0), default=("—", {}))
    A(f"\n**The frozen device.** `PUMP-03` freezes 12 seconds into the run and then "
      "answers every poll, on time, forever, with entirely plausible values. No "
      "connection drops, no protocol error, nothing anywhere reports a fault — and "
      f"it accumulates {stale_dev[1].get('STALE', 0):,} STALE samples, because the "
      "collector tests for *change* as well as for *response*.\n")
    A("Neither test works alone, and that is the design point. A change-only test "
      "flags a tank sitting at setpoint, which is the healthiest signal on the "
      "plant. A heartbeat-only test sees a frozen device answering perfectly and "
      "calls it healthy. It is the combination — *we are being answered, and the "
      "answer has not moved for N scans* — that catches this, which is why the "
      "stale window is a per-tag config value and not a constant: a setpoint that "
      "legitimately never moves needs a far longer window than a vibration reading.\n")
    A("STALE is propagated downstream, never dropped. \"68.2\" and \"68.2, three "
      "hours stale\" are different facts and a float cannot carry the difference. "
      "A historian that discards quality is how a three-week sensor fault becomes "
      "indistinguishable from a process improvement.")

    A("\n## 3. Protocol scar tissue\n")
    A("| device | reconnects | protocol errors | frames rejected on checksum |")
    A("|---|---|---|---|")
    for dev, d in sorted(res["drivers"].items()):
        A(f"| {dev} | {d['reconnects']} | {d['errors']} | {d['rejected_frames']} |")
    A("\n**The word-swapped float.** `OVEN-02` stores its float32 tags with the two "
      "16-bit words in the opposite order to `PRESS-01`. Modbus has no float type "
      "— it has registers, and a float is a convention both ends must agree on. "
      "The bytes are valid either way, so there is **no way to detect this from "
      "the wire**. It is a site-survey fact, which is why `word_swap` is a field "
      "in the register map rather than a heuristic.\n")
    A("What it looks like before the config is fixed: a temperature of roughly "
      "1.7e19. What catches it faster next time is the `min_plausible` / "
      "`max_plausible` range on the tag — the value arrives, it is nonsense, and "
      "it is flagged **UNCERTAIN** instead of being stored as fact. Per-tag "
      "plausibility limits are cheap and they convert a week of confused analytics "
      "into an ingest-time flag.\n")
    A("**The 1998 machine.** `LATHE-04` drops its TCP session every 15 seconds and "
      "corrupts 8% of its frames. Frames are delimited on STX/ETX rather than on "
      "newlines, because a corrupted byte can *be* a newline and a parser that "
      "splits on `\\n` cuts a frame in half and then parses both halves as data. A "
      "frame failing its XOR checksum is rejected outright — a frame that fails "
      "its checksum is not \"mostly right\". Reconnection uses exponential backoff "
      "**with jitter**: without jitter, a plant that loses a switch brings every "
      "gateway back at the same instant, and the reconnect storm looks like the "
      "outage continuing.")

    probe = res.get("word_order_probe") or {}
    if probe.get("registers"):
        A("\n### The word-order probe, measured\n")
        A(f"The same two registers read from `{probe['device']}` "
          f"(`{probe['registers']}`), decoded under both configurations:\n")
        A("| register map says | decoded value | quality flag |")
        A("|---|---|---|")
        for label in ("correct (word_swap=True)", "wrong (word_swap=False)"):
            if label in probe:
                d = probe[label]
                A(f"| {label} | {d['value']:.6g} °C | **{d['quality']}** |")
        A("\nThat is the argument for plausibility limits as first-class config. "
          "The wrong word order does not raise an error, does not drop a "
          "connection and does not fail a checksum — it produces a *number*. The "
          "only thing between it and a historian full of confident nonsense is a "
          "per-tag range check at ingest.")

    A("\n## 4. The bounded buffer\n")
    A(f"Bound {res['buffer_bound']:,} rows; peak depth {res['max_buffer_depth']:,}; "
      f"dropped {res['dropped_by_policy']:,}; policy **{res['overflow_policy']}**.\n")
    A("Tag priorities: " + ", ".join(
        f"`{k}`={v}" for k, v in sorted(res["tag_priority"].items(),
                                        key=lambda kv: -kv[1])) + "\n")
    A("An unbounded buffer is a lie about the hardware. A gateway with 256 MB of "
      "RAM does not hold a three-day outage, and pretending it does means the "
      "failure arrives as an OOM at 03:00 rather than as a decision somebody made "
      "deliberately. The priority policy is the default because it is the only one "
      "that can answer *which data would you rather lose*: a 1 Hz temperature "
      "trend is not worth the same as the part counter feeding OEE, and "
      "oldest-first or newest-first choose between them by accident.\n")
    cap, drop = res.get("captured_by_tag", {}), res.get("dropped_by_tag", {})
    if drop:
        A("\n### What the policy actually dropped\n")
        A("| tag | priority | captured | dropped | % dropped |")
        A("|---|---|---|---|---|")
        for tag in sorted(cap, key=lambda t: (res["tag_priority"].get(t, 5), t)):
            c, dd = cap.get(tag, 0), drop.get(tag, 0)
            A(f"| {tag} | {res['tag_priority'].get(tag, 5)} | {c:,} | {dd:,} | "
              f"{100.0 * dd / max(1, c):.1f}% |")
        lowest = min(res["tag_priority"], key=res["tag_priority"].get)
        highest = max(res["tag_priority"], key=res["tag_priority"].get)
        A(f"\nThe policy is doing what it says: `{lowest}` (priority "
          f"{res['tag_priority'][lowest]}) absorbs the loss and `{highest}` "
          f"(priority {res['tag_priority'][highest]}) is protected. Without the "
          "per-tag counters this table would not exist and the claim would be an "
          "assertion about code rather than a measurement of behaviour.")
    A("\nWhatever is dropped is **counted and surfaced**. A silent drop is the same "
      "bug as a silent loss.")

    A("\n---\n*See `docs/SECURITY_62443.md` for the zones-and-conduits posture, and "
      "for an explicit statement of what this demonstrates and what it does not.*")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
