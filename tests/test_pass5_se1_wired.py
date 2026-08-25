"""Pass 5: the subscription contract wired into an acquisition loop.

Pass 4 built the contract and measured it in isolation; the README recorded that
it was not wired in, and that doing so "would change the health path and the
buffer's back-pressure logic". It is wired now, and these tests pin the two
places the change actually lands: the classifier's STALE rule and the device
health path.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collector as C            # noqa: E402
import subscribe as S            # noqa: E402


class _Tag:
    def __init__(self, name, deadband=0.0):
        self.name = name
        self.deadband = deadband
        self.min_plausible = None
        self.max_plausible = None


@pytest.fixture
def coll(tmp_path):
    hist = C.Historian(tmp_path / "h.db")
    c = C.Collector("c1", tmp_path / "b.db", hist)
    yield c
    c.close()


# --- the classifier ----------------------------------------------------------

def test_a_confirmed_keepalive_suppresses_stale(coll):
    """STALE means 'we are being answered and the answer has not moved'. For a
    device that publishes ON CHANGE, not moving is the normal state -- so a
    confirmed keep-alive means the value is current, not stale."""
    tag = _Tag("temp_C")
    assert coll.classify("D", tag, 60.0, 0.01) == "GOOD"     # first sight
    import time as _t
    _t.sleep(0.05)
    assert coll.classify("D", tag, 60.0, 0.01) == "STALE"
    assert coll.classify("D", tag, 60.0, 0.01,
                         liveness=S.KEEPALIVE) == "GOOD"


def test_a_silent_device_does_not_get_the_keepalive_reprieve(coll):
    tag = _Tag("temp_C")
    coll.classify("D", tag, 60.0, 0.01)
    import time as _t
    _t.sleep(0.05)
    assert coll.classify("D", tag, 60.0, 0.01, liveness=S.SILENT) == "STALE"
    assert coll.classify("D", tag, 60.0, 0.01, liveness=None) == "STALE"


def test_liveness_does_not_override_the_other_quality_rules(coll):
    """A keep-alive says the device is there. It says nothing about whether the
    number is believable."""
    tag = _Tag("t")
    tag.max_plausible = 100.0
    assert coll.classify("D", tag, 1.7e19, 10.0,
                         liveness=S.KEEPALIVE) == "UNCERTAIN"
    assert coll.classify("D", tag, None, 10.0, liveness=S.KEEPALIVE) == "BAD"


# --- the health path ---------------------------------------------------------

def test_only_silence_marks_a_device_down(coll):
    for live, expect in ((S.FRESH, "UP"), (S.KEEPALIVE, "UP"),
                         (S.SILENT, "DOWN")):
        coll.observe_liveness("D", live)
        assert coll.device_health("D") == expect, live


def test_a_device_nobody_asked_about_is_not_down(coll):
    assert coll.device_health("never-seen") == "UP"


def test_health_is_separate_from_tag_values(coll):
    """Whether a device is THERE and what it last SAID are different questions,
    which is the whole point of the contract."""
    coll.observe_liveness("D", S.KEEPALIVE, age_s=3.0)
    coll.record("D", "temp_C", None, "BAD", None)
    assert coll.device_health("D") == "UP"


# --- the loop ----------------------------------------------------------------

def _profile(name="MC-IDLE"):
    return types.SimpleNamespace(name=name, tags=[_Tag("temp_C")])


def test_an_idle_subscribed_device_writes_nothing_and_stays_up(coll):
    dev = S.ChangeDrivenDevice({"temp_C": 60.0}, keepalive_s=0.3)
    drv = S.ChangeDrivenDriver(dev, keepalive_s=0.3, missed_allowed=3)
    stop = asyncio.Event()
    out = asyncio.run(S.run_device(drv, coll, _profile(), stop,
                                   stale_window_s=0.6, scan_s=0.02,
                                   max_ticks=30))
    assert out["silent"] == 0
    assert out["keepalive"] > 20
    # one row: the initial publish. The old loop wrote one BAD row per tick.
    assert out["rows_written"] == 1
    assert coll.device_health("MC-IDLE") == "UP"


def test_a_dead_device_does_go_bad(coll):
    """The reprieve must not become a blanket amnesty."""
    dev = S.ChangeDrivenDevice({"temp_C": 60.0}, keepalive_s=0.05)
    drv = S.ChangeDrivenDriver(dev, keepalive_s=0.05, missed_allowed=1)
    stop = asyncio.Event()

    async def go():
        await drv.subscribe()
        await drv.fetch()
        dev.alive = False
        return await S.run_device(drv, coll, _profile("MC-DEAD"), stop,
                                  stale_window_s=0.2, scan_s=0.02,
                                  max_ticks=25)

    out = asyncio.run(go())
    assert out["silent"] > 0
    assert out["rows_written"] > 0
    assert coll.device_health("MC-DEAD") == "DOWN"


def test_a_driver_that_cannot_subscribe_marks_everything_bad(coll):
    class _NoSub(S.SubscriptionDriver):
        async def _subscribe(self):
            return False

        async def _drain(self):
            return [], False

    stop = asyncio.Event()
    out = asyncio.run(S.run_device(_NoSub("x"), coll, _profile("MC-GONE"), stop,
                                   stale_window_s=1.0, scan_s=0.01,
                                   max_ticks=3))
    assert out["subscribed"] is False
    assert coll.device_health("MC-GONE") == "DOWN"


def test_the_polled_drivers_still_work_through_the_same_loop(coll):
    """The migration must not break the transports that were already fine."""
    class _Fake:
        profile = types.SimpleNamespace(name="MC-POLL")

        def __init__(self):
            self.n = 0

        async def connect(self):
            return True

        async def poll(self):
            self.n += 1
            return {"temp_C": 60.0 + self.n}

        async def close(self):
            pass

    ad = S.PolledAdapter(_Fake())
    stop = asyncio.Event()
    out = asyncio.run(S.run_device(ad, coll, _profile("MC-POLL"), stop,
                                   stale_window_s=1.0, scan_s=0.01,
                                   max_ticks=5))
    assert out["silent"] == 0 and out["fresh"] == 5
    assert out["rows_written"] == 5
    assert coll.device_health("MC-POLL") == "UP"


def test_the_two_loops_compared_end_to_end():
    """The measurement the README asked for: what wiring it in actually fixes."""
    r = asyncio.run(S.compare_loops(steady_s=1.0, tick_s=0.03, keepalive_s=0.3))
    assert r["old_false_bad_frac"] > 0.8
    assert r["new_false_bad_frac"] == 0.0
    assert r["old"]["device_health"] == "DOWN"
    assert r["new"]["device_health"] == "UP"
    # and report-by-exception actually reports by exception
    assert r["new"]["rows"] < r["old"]["rows"] / 5
