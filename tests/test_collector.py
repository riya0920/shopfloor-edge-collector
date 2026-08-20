"""SE-1 tests: protocol encoding, framing, quality flags, and the ledger.

The protocol tests are the cheap ones and they are the ones that catch the bugs
that cost days on site: a word order, a checksum, a frame boundary.
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collector as C  # noqa: E402
import devices as D  # noqa: E402


# ------------------------------------------------------------ register encoding

@pytest.mark.parametrize("v", [0.0, 1.0, -273.15, 68.4321, 1e6, -1e-6])
def test_float_register_roundtrip_both_word_orders(v):
    for swap in (False, True):
        regs = D.float_to_registers(v, swap)
        assert len(regs) == 2
        assert all(0 <= r <= 0xFFFF for r in regs)
        assert D.registers_to_float(regs, swap) == pytest.approx(v, rel=1e-6, abs=1e-9)


def test_wrong_word_order_produces_nonsense_not_an_error():
    """The whole argument for plausibility limits: the bytes are valid either way,
    so a misconfiguration is silent and produces a number."""
    regs = D.float_to_registers(68.5, word_swap=True)
    correct = D.registers_to_float(regs, word_swap=True)
    wrong = D.registers_to_float(regs, word_swap=False)
    assert correct == pytest.approx(68.5, rel=1e-6)
    assert abs(wrong) > 1e10 or abs(wrong) < 1e-10   # wildly implausible, not an error


# ------------------------------------------------------------ ASCII framing

def test_ascii_frame_roundtrip():
    vals = {"temperature_c": 68.25, "vibration_mms": 2.5}
    frame = D.build_ascii_frame(4, vals)
    assert frame[0] == D.STX and D.ETX in frame
    out = D.parse_ascii_frame(frame)
    assert out["temperature_c"] == pytest.approx(68.25)
    assert out["vibration_mms"] == pytest.approx(2.5)


def test_corrupt_frame_is_rejected_not_guessed():
    frame = bytearray(D.build_ascii_frame(4, {"temperature_c": 68.25}))
    frame[5] = (frame[5] + 1) % 256          # flip a payload byte
    assert D.parse_ascii_frame(bytes(frame)) is None


def test_truncated_and_empty_frames_are_rejected():
    assert D.parse_ascii_frame(b"") is None
    assert D.parse_ascii_frame(b"\x02no-etx-here") is None
    assert D.parse_ascii_frame(b"garbage") is None


def test_checksum_is_xor_of_the_payload():
    payload = b"04,temperature_c=68.250"
    c = D.ascii_checksum(payload)
    expect = 0
    for b in payload:
        expect ^= b
    assert c == expect


# ------------------------------------------------------------ quality flags

@pytest.fixture
def coll(tmp_path):
    hist = C.Historian(tmp_path / "h.db")
    return C.Collector("T1", tmp_path / "b.db", hist, max_buffer_rows=50,
                       overflow_policy="priority")


def test_implausible_value_is_uncertain_not_good(coll):
    tag = D.TagSpec("temperature_c", 0, "float32", min_plausible=-20, max_plausible=200)
    assert coll.classify("DEV", tag, 68.0, 10.0) == "GOOD"
    assert coll.classify("DEV", tag, -9.9e33, 10.0) == "UNCERTAIN"
    assert coll.classify("DEV", tag, 5000.0, 10.0) == "UNCERTAIN"


def test_none_value_is_bad(coll):
    tag = D.TagSpec("t", 0, "float32")
    assert coll.classify("DEV", tag, None, 10.0) == "BAD"


def test_unchanging_value_becomes_stale_only_after_the_window(coll):
    tag = D.TagSpec("t", 0, "float32", deadband=0.01)
    assert coll.classify("DEV", tag, 50.0, stale_window_s=0.05) == "GOOD"
    assert coll.classify("DEV", tag, 50.0, stale_window_s=1e9) == "GOOD"   # window not elapsed
    import time
    time.sleep(0.08)
    assert coll.classify("DEV", tag, 50.0, stale_window_s=0.05) == "STALE"


def test_a_changing_value_never_goes_stale(coll):
    tag = D.TagSpec("t", 0, "float32", deadband=0.01)
    import time
    for i in range(5):
        assert coll.classify("DEV", tag, 50.0 + i, stale_window_s=0.01) == "GOOD"
        time.sleep(0.02)


# ------------------------------------------------------------ the ledger

def test_idempotent_historian_rejects_replays(coll):
    for i in range(20):
        coll.record("DEV", "temperature_c", float(i), "GOOD", None)
    coll.drain()
    before = coll.historian.count()
    coll.replay_all()
    assert coll.historian.count() == before
    assert coll.stats.duplicates_rejected >= 20


def test_no_loss_across_an_uplink_outage(coll):
    for i in range(10):
        coll.record("DEV", "temperature_c", float(i), "GOOD", None)
    coll.drain()
    coll.online = False
    for i in range(10, 30):
        coll.record("DEV", "temperature_c", float(i), "GOOD", None)
    assert coll.historian.count() == 10
    coll.online = True
    coll.drain()
    assert coll.historian.count() == 30 - coll.stats.dropped_overflow


def test_bounded_buffer_is_actually_bounded(coll):
    for i in range(500):
        coll.record("DEV", "flow_lpm", float(i), "GOOD", None)
    assert coll.depth() <= coll.max_buffer_rows
    assert coll.stats.dropped_overflow > 0


def test_priority_policy_drops_the_lowest_priority_tag_first(tmp_path):
    """The bug this test exists for: the sort key was inverted, so the policy
    dropped the MOST important tags and nothing failed."""
    hist = C.Historian(tmp_path / "h.db")
    c = C.Collector("T2", tmp_path / "b.db", hist, max_buffer_rows=40,
                    overflow_policy="priority")
    c.tag_priority = {"part_count": 10, "flow_lpm": 1}
    c.online = False
    for i in range(200):
        c.record("DEV", "part_count", float(i), "GOOD", None)
        c.record("DEV", "flow_lpm", float(i), "GOOD", None)
    dropped = c.stats.dropped_by_tag
    assert dropped.get("flow_lpm", 0) > dropped.get("part_count", 0)


def test_sequence_numbers_survive_a_restart(tmp_path):
    hist = C.Historian(tmp_path / "h.db")
    c = C.Collector("T3", tmp_path / "b.db", hist)
    for i in range(5):
        c.record("DEV", "t", float(i), "GOOD", None)
    c.close()
    c2 = C.Collector("T3", tmp_path / "b.db", hist)
    c2.record("DEV", "t", 99.0, "GOOD", None)
    seqs = [r[0] for r in c2.conn.execute("SELECT seq FROM buffer ORDER BY seq")]
    assert seqs == [1, 2, 3, 4, 5, 6], "a restart must not reuse sequence numbers"
