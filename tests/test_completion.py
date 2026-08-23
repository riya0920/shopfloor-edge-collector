"""Tests for the third-pass modules."""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ops as OPS  # noqa: E402


# ---------------------------------------------------------------------------
# deadband
# ---------------------------------------------------------------------------

def test_zero_deadband_sends_everything():
    d = OPS.Deadband(0.0, max_gap_s=1e9)
    for i in range(50):
        assert d.should_send("D", "t", float(i), float(i))
    assert d.stats()["suppressed"] == 0


def test_a_still_signal_is_suppressed_but_the_heartbeat_still_fires():
    """Without the heartbeat, a still sensor and a dead one are identical."""
    d = OPS.Deadband(1.0, max_gap_s=60.0)
    sent = sum(d.should_send("D", "t", 50.0, float(t)) for t in range(0, 601, 5))
    s = d.stats()
    assert s["suppressed"] > 100
    assert s["heartbeats"] >= 9, "a heartbeat must fire about every 60 s"
    assert sent == s["sent"]


def test_a_step_change_is_never_suppressed():
    """The one thing a deadband must not swallow."""
    d = OPS.Deadband(1.0, max_gap_s=1e9)
    d.should_send("D", "t", 50.0, 0.0)
    assert not d.should_send("D", "t", 50.2, 1.0)
    assert d.should_send("D", "t", 57.0, 2.0), "a 7-unit step must go"


def test_bad_quality_bypasses_the_deadband():
    """Suppressing a fault because its value matches the last good one would
    hide a sensor failure behind a bandwidth optimisation."""
    d = OPS.Deadband(5.0, max_gap_s=1e9)
    d.should_send("D", "t", 50.0, 0.0)
    assert d.should_send("D", "t", None, 1.0)


def test_deadband_is_per_tag_not_global():
    d = OPS.Deadband(1.0, max_gap_s=1e9)
    d.should_send("D", "temp", 50.0, 0.0)
    # A different tag has no history, so its first reading always goes.
    assert d.should_send("D", "pressure", 50.0, 0.0)
    assert d.stats()["n_tags"] == 2


def test_reconstruction_uses_zero_order_hold():
    """Interpolating would flatter the deadband by inventing smoothness no
    historian applies."""
    orig = [(float(t), float(t)) for t in range(10)]
    sent = [(0.0, 0.0), (5.0, 5.0)]
    err = OPS.reconstruction_error(orig, sent)
    # Held at 0 for t=1..4 then at 5 for t=6..9: max error is 4.
    assert err["max_abs_error"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# clock skew
# ---------------------------------------------------------------------------

def test_a_constant_offset_is_not_reported_as_drift():
    base = np.arange(200, dtype=float) * 5
    pairs = {"D": [(t, t + 90.0) for t in base]}
    r = OPS.skew_analysis(pairs)[0]
    assert r["mean_offset_s"] == pytest.approx(90.0, abs=0.1)
    assert not r["drifting"]
    assert "correctable" in r["verdict"]


def test_a_drifting_clock_is_separated_from_an_offset():
    """The dangerous case: a correction is stale as soon as it is applied."""
    base = np.arange(200, dtype=float) * 5
    pairs = {"D": [(t, t + 2.0 + 0.004 * t) for t in base]}
    r = OPS.skew_analysis(pairs)[0]
    assert r["drifting"]
    assert r["drift_s_per_hour"] == pytest.approx(0.004 * 3600, rel=0.05)


def test_skew_analysis_never_suggests_rewriting_timestamps():
    base = np.arange(50, dtype=float) * 5
    for r in OPS.skew_analysis({"D": [(t, t + 30.0) for t in base]}):
        assert "never rewrite" in r["action"]


def test_devices_with_too_little_history_are_skipped():
    assert OPS.skew_analysis({"D": [(0.0, 1.0), (1.0, 2.0)]}) == []


# ---------------------------------------------------------------------------
# resource envelope
# ---------------------------------------------------------------------------

def test_resource_watch_reports_peak_and_growth():
    w = OPS.ResourceWatch(mem_target_mb=1e6)
    for _ in range(5):
        w.sample()
    r = w.report(seconds=10.0, n_tags=1000)
    assert r["peak_rss_mb"] > 0
    assert r["tags_per_second"] == pytest.approx(100.0)
    assert r["within_memory_envelope"]
    assert "not enforced" in r["enforcement"]


def test_an_impossible_target_fails_the_envelope():
    w = OPS.ResourceWatch(mem_target_mb=0.001)
    w.sample()
    assert not w.report(1.0, 1)["within_memory_envelope"]


def test_pinning_sets_the_thread_env_vars():
    """A 'single-threaded' measurement that quietly uses eight cores is not a
    gateway measurement."""
    import os
    OPS.pin_single_thread()
    assert os.environ["OMP_NUM_THREADS"] == "1"
    assert os.environ["OPENBLAS_NUM_THREADS"] == "1"


# ---------------------------------------------------------------------------
# ops view
# ---------------------------------------------------------------------------

def test_health_rows_flag_degraded_and_skewed_separately():
    stats = {"A": {"total": 100, "bad": 20, "uncertain": 0},
             "B": {"total": 100, "bad": 0, "uncertain": 0},
             "C": {"total": 100, "bad": 0, "uncertain": 0}}
    skew = [{"device": "C", "mean_offset_s": 90.0, "drifting": False}]
    rows = {r["device"]: r for r in OPS.health_rows(stats, skew, 0, 0.0)}
    assert rows["A"]["state"] == "DEGRADED"
    assert rows["B"]["state"] == "OK"
    assert rows["C"]["state"] == "SKEWED"


def test_ops_page_is_self_contained(tmp_path):
    rows = OPS.health_rows({"A": {"total": 10, "bad": 0, "uncertain": 0}}, [], 0, 0)
    env = OPS.ResourceWatch(mem_target_mb=1e6)
    env.sample()
    out = OPS.render_ops_page(
        tmp_path / "ops.html", rows, env.report(1.0, 10),
        OPS.Deadband(1.0).stats(), [], 0,
        {"X": {"first check": "a", "do NOT": "b"}})
    html = (tmp_path / "ops.html").read_text(encoding="utf-8")
    assert out["self_contained"]
    assert "http://" not in html and "https://" not in html, "no CDN references"
    assert "do NOT" in html
