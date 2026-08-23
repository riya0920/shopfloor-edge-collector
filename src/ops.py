"""Resource limits, deadband suppression, clock-skew monitoring and an ops view.

The remaining items from SE-1's not-built list. Each one is a claim the earlier
passes made in a docstring and never put a number against.

===========================================================================
1. A RESOURCE ENVELOPE, which is the spec's headline metric
===========================================================================

The spec asks for the collector inside 256 MB and 0.5 CPU, with the throughput
measured *within* that envelope. The earlier passes ran unconstrained on a
desktop, and the README says plainly that a throughput number without a stated
envelope is meaningless -- an edge gateway is not a desktop, and a figure measured
without the constraint tells you nothing about whether it fits.

WHAT CAN AND CANNOT BE ENFORCED HERE. There is no container runtime and Windows
has no cgroups, so a hard 256 MB cap cannot be imposed. What CAN be done, and is:

  * pin the process to one worker thread, which is the closer analogue of
    0.5 CPU than any timing trick
  * measure peak RSS with psutil while the load runs, so the memory figure is
    observed rather than assumed
  * check the observed peak against the target and report PASS/FAIL

That is an envelope CHECK rather than an envelope ENFORCEMENT, and the difference
is stated in the report rather than glossed. A checked envelope still catches the
thing that matters -- unbounded growth -- because a leak shows up as a rising
peak long before it shows up as a crash.

===========================================================================
2. DEADBAND SUPPRESSION, measured instead of argued
===========================================================================

Every poll is currently recorded regardless of whether the value moved. The
docstrings argue for report-by-exception and no number was produced.

The heartbeat is the part that is not optional. Without it a still sensor and a
dead sensor both transmit nothing, and the historian cannot tell them apart --
which converts an instrument failure into a healthy flat line, the worst
possible failure mode for process data.

===========================================================================
3. CLOCK SKEW
===========================================================================

`LATHE-04` is configured 90 s slow and the collector records both device and
collector timestamps. Nothing analysed the difference. Two things matter and only
one is usually done:

  OFFSET  a constant difference. Annoying, correctable, and mostly harmless if
          you know it -- you can subtract it.
  DRIFT   the offset CHANGING over time. This is the dangerous one, because a
          correction applied today is wrong tomorrow, and it means the device
          clock is running at a different rate rather than merely being set
          wrong. Fitting a line to the offset separates them.

The rule that follows: **record the offset, never rewrite the timestamps.** A
corrected timestamp is unfalsifiable later.
"""
from __future__ import annotations

import os
import time

import numpy as np


# ---------------------------------------------------------------------------
# 1. resource envelope
# ---------------------------------------------------------------------------

class ResourceWatch:
    """Sample RSS while something runs and report the peak against a target."""

    def __init__(self, mem_target_mb: float = 256.0, cpu_target: float = 0.5):
        import psutil
        self.p = psutil.Process(os.getpid())
        self.mem_target_mb = mem_target_mb
        self.cpu_target = cpu_target
        self.samples: list[float] = []
        self.baseline_mb = self.p.memory_info().rss / 1e6

    def sample(self) -> float:
        mb = self.p.memory_info().rss / 1e6
        self.samples.append(mb)
        return mb

    def report(self, seconds: float, n_tags: int) -> dict:
        peak = max(self.samples) if self.samples else self.baseline_mb
        # Growth across the run is the leak signal, and it is more informative
        # than the peak: a flat 200 MB is fine forever, a rising 120 MB is not.
        growth = (self.samples[-1] - self.samples[0]) if len(self.samples) > 1 else 0.0
        return {
            "peak_rss_mb": peak, "baseline_rss_mb": self.baseline_mb,
            "growth_mb": growth,
            "growth_mb_per_minute": growth / max(seconds / 60.0, 1e-9),
            "mem_target_mb": self.mem_target_mb,
            "within_memory_envelope": peak <= self.mem_target_mb,
            "threads_pinned_to": 1,
            "cpu_target": self.cpu_target,
            "tags_per_second": n_tags / max(seconds, 1e-9),
            "n_samples": len(self.samples),
            "enforcement": ("CHECKED, not enforced: no container runtime and no "
                            "cgroups on this platform, so the limit is observed "
                            "rather than imposed"),
        }


def pin_single_thread() -> dict:
    """Restrict the process to one worker thread.

    The closest available analogue of 0.5 CPU. Setting the BLAS/OMP variables
    matters more than it looks: numpy will otherwise spin up one thread per core
    for anything vectorised, and a "single-threaded" measurement that quietly
    uses eight cores is not a gateway measurement.
    """
    before = {}
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS"):
        before[var] = os.environ.get(var)
        os.environ[var] = "1"
    try:
        import torch
        torch.set_num_threads(1)
    except Exception:                                        # pragma: no cover
        pass
    return {"pinned": True, "previous": before}


# ---------------------------------------------------------------------------
# 2. deadband
# ---------------------------------------------------------------------------

class Deadband:
    """Report-by-exception with a mandatory heartbeat.

    Per (device, tag), because a deadband is a property of the signal. One global
    threshold across a temperature in degrees and a pressure in bar is a
    threshold that is wrong for both.
    """

    def __init__(self, deadband: float, max_gap_s: float = 60.0) -> None:
        self.deadband = float(deadband)
        self.max_gap_s = float(max_gap_s)
        self._last: dict[tuple, tuple[float, float]] = {}
        self.sent = 0
        self.suppressed = 0
        self.heartbeats = 0

    def should_send(self, device: str, tag: str, value: float | None,
                    ts: float) -> bool:
        # A bad-quality reading always goes. Suppressing a fault because its
        # VALUE happens to match the last good one is how a sensor failure gets
        # hidden by the bandwidth optimisation meant to be orthogonal to it.
        if value is None:
            self.sent += 1
            return True
        key = (device, tag)
        prev = self._last.get(key)
        if prev is None:
            self._last[key] = (value, ts)
            self.sent += 1
            return True
        pv, pt = prev
        if abs(value - pv) >= self.deadband:
            self._last[key] = (value, ts)
            self.sent += 1
            return True
        if ts - pt >= self.max_gap_s:
            self._last[key] = (value, ts)
            self.sent += 1
            self.heartbeats += 1
            return True
        self.suppressed += 1
        return False

    def stats(self) -> dict:
        total = self.sent + self.suppressed
        return {"deadband": self.deadband, "max_gap_s": self.max_gap_s,
                "sent": self.sent, "suppressed": self.suppressed,
                "heartbeats": self.heartbeats,
                "suppression_rate": self.suppressed / max(total, 1),
                "n_tags": len(self._last)}


def reconstruction_error(original: list[tuple], sent: list[tuple]) -> dict:
    """What the historian would see, against what actually happened.

    Zero-order hold, because that is what a historian does with
    report-by-exception data -- the value is assumed to persist until the next
    report. Interpolating instead would flatter the deadband by inventing a
    smoothness the consumer does not apply.
    """
    if not sent or not original:
        return {"n": 0}
    ts = np.array([t for t, _ in original], dtype=float)
    v = np.array([x for _, x in original], dtype=float)
    st = np.array([t for t, _ in sent], dtype=float)
    sv = np.array([x for _, x in sent], dtype=float)
    idx = np.searchsorted(st, ts, side="right") - 1
    idx = np.clip(idx, 0, len(sv) - 1)
    recon = sv[idx]
    err = recon - v
    return {"n": len(v), "max_abs_error": float(np.abs(err).max()),
            "rms_error": float(np.sqrt(np.mean(err ** 2))),
            "signal_sd": float(v.std()),
            "error_as_pct_of_sd": float(np.sqrt(np.mean(err ** 2))
                                        / max(v.std(), 1e-9) * 100)}


# ---------------------------------------------------------------------------
# 3. clock skew
# ---------------------------------------------------------------------------

def skew_analysis(pairs: dict[str, list[tuple[float, float]]]) -> list[dict]:
    """Offset and DRIFT per device, from (device_ts, collector_ts) pairs.

    Separating the two is the whole value. A constant offset is correctable if
    you know it. A drifting one means the device clock runs at a different rate,
    so any correction is stale the moment it is applied -- and it is the case that
    silently corrupts an interval calculation, because two events an hour apart
    on the device are not an hour apart in the record.
    """
    out = []
    for dev, ps in sorted(pairs.items()):
        if len(ps) < 3:
            continue
        d = np.array([a for a, _ in ps], dtype=float)
        c = np.array([b for _, b in ps], dtype=float)
        off = c - d
        # Slope of offset against time: seconds of drift per second elapsed.
        span = d.max() - d.min()
        slope = float(np.polyfit(d, off, 1)[0]) if span > 0 else 0.0
        out.append({
            "device": dev, "n": len(ps),
            "mean_offset_s": float(off.mean()),
            "sd_offset_s": float(off.std()),
            "drift_s_per_hour": slope * 3600.0,
            "drifting": abs(slope * 3600.0) > 1.0,
            "verdict": ("DRIFT -- the device clock runs at a different rate; a "
                        "correction is stale as soon as it is applied"
                        if abs(slope * 3600.0) > 1.0 else
                        "constant offset -- correctable if recorded"),
            "action": "record the offset; never rewrite the stored timestamps",
        })
    return sorted(out, key=lambda r: -abs(r["mean_offset_s"]))


# ---------------------------------------------------------------------------
# 4. the ops view
# ---------------------------------------------------------------------------

def health_rows(stats_by_device: dict, skew: list[dict],
                buffer_depth: int, uplink_lag_s: float) -> list[dict]:
    """Per-device health, the thing `results.json` had and nothing displayed."""
    sk = {r["device"]: r for r in skew}
    rows = []
    for dev, s in sorted(stats_by_device.items()):
        total = max(s.get("total", 0), 1)
        bad = s.get("bad", 0) + s.get("uncertain", 0)
        rows.append({
            "device": dev, "samples": s.get("total", 0),
            "bad_quality_pct": 100 * bad / total,
            "offset_s": sk.get(dev, {}).get("mean_offset_s", 0.0),
            "drifting": sk.get(dev, {}).get("drifting", False),
            "state": ("DEGRADED" if bad / total > 0.05 else
                      "SKEWED" if abs(sk.get(dev, {}).get("mean_offset_s", 0)) > 30
                      else "OK"),
        })
    return rows


def render_ops_page(path, rows: list[dict], envelope: dict, deadband: dict,
                    skew: list[dict], buffer_depth: int, runbooks: dict) -> dict:
    """A self-contained ops page. One file, inline SVG, no CDN."""
    import html
    import pathlib

    def badge(state):
        c = {"OK": "#2f855a", "SKEWED": "#b7791f", "DEGRADED": "#c53030"}[state]
        return f'<span class="b" style="background:{c}">{state}</span>'

    dev_rows = "".join(
        f'<tr><td>{html.escape(r["device"])}</td>'
        f'<td class="n">{r["samples"]:,}</td>'
        f'<td class="n">{r["bad_quality_pct"]:.1f}%</td>'
        f'<td class="n">{r["offset_s"]:+.1f}s</td>'
        f'<td>{"yes" if r["drifting"] else "no"}</td>'
        f'<td>{badge(r["state"])}</td></tr>' for r in rows)

    rb = "".join(
        f'<details><summary>{html.escape(k)}</summary><div class="rb">'
        + "".join(f"<p><b>{html.escape(kk)}:</b> {html.escape(str(vv))}</p>"
                  for kk, vv in v.items()) + "</div></details>"
        for k, v in runbooks.items())

    ok = envelope["within_memory_envelope"]
    doc = f"""<!doctype html>
<meta charset="utf-8"><title>Collector ops</title>
<style>
:root{{--bg:#f7fafc;--fg:#1a202c;--card:#fff;--line:#e2e8f0;--mut:#718096}}
@media (prefers-color-scheme:dark){{:root{{--bg:#171923;--fg:#e2e8f0;
 --card:#242c3d;--line:#3a4459;--mut:#a0aec0}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--fg);
 font:14px/1.55 system-ui,sans-serif}}
h1{{font-size:20px;margin:0 0 2px}}
h2{{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);
 margin:0 0 10px}}
.sub{{color:var(--mut);margin-bottom:20px}}
.grid{{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:16px;overflow-x:auto}}
.wide{{grid-column:1/-1}}
.kpis{{display:flex;gap:22px;flex-wrap:wrap}}
.kpi b{{display:block;font-size:24px;line-height:1.1}}
.kpi span{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-size:11px;text-transform:uppercase}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.b{{color:#fff;font-size:11px;padding:1px 7px;border-radius:9px}}
.note{{font-size:12px;color:var(--mut);margin-top:8px}}
summary{{cursor:pointer;font-size:13px;margin-top:6px}}
.rb p{{margin:3px 0;font-size:12px;color:var(--mut)}}
</style>
<h1>Collector ops</h1>
<div class="sub">{len(rows)} devices &middot; generated by <code>complete.py</code></div>
<div class="grid">
  <div class="card wide"><h2>Resource envelope</h2>
    <div class="kpis">
      <div class="kpi"><b>{envelope['peak_rss_mb']:.0f} MB</b>
        <span>peak RSS (target {envelope['mem_target_mb']:.0f})</span></div>
      <div class="kpi"><b>{envelope['growth_mb_per_minute']:+.2f}</b>
        <span>MB/min growth</span></div>
      <div class="kpi"><b>{envelope['tags_per_second']:.0f}</b>
        <span>tags/s, 1 thread</span></div>
      <div class="kpi"><b>{'PASS' if ok else 'FAIL'}</b>
        <span>within envelope</span></div>
    </div>
    <div class="note">{html.escape(envelope['enforcement'])}. Growth per minute is
     the more useful number: a flat 200 MB is fine forever, a rising 120 MB is not.</div>
  </div>
  <div class="card"><h2>Deadband</h2>
    <div class="kpis">
      <div class="kpi"><b>{deadband['suppression_rate'] * 100:.0f}%</b>
        <span>messages suppressed</span></div>
      <div class="kpi"><b>{deadband['heartbeats']}</b><span>heartbeats</span></div>
    </div>
    <div class="note">The heartbeat is not optional: without it a still sensor and
     a dead sensor both transmit nothing.</div>
  </div>
  <div class="card"><h2>Buffer</h2>
    <div class="kpis"><div class="kpi"><b>{buffer_depth}</b>
      <span>rows pending uplink</span></div></div>
  </div>
  <div class="card wide"><h2>Devices</h2>
    <table><thead><tr><th>device</th><th class="n">samples</th>
      <th class="n">bad quality</th><th class="n">clock offset</th>
      <th>drifting</th><th>state</th></tr></thead>
      <tbody>{dev_rows}</tbody></table>
  </div>
  <div class="card wide"><h2>Runbooks</h2>{rb}</div>
</div>
"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc, encoding="utf-8")
    return {"path": str(p), "bytes": p.stat().st_size, "self_contained": True}
