"""The collector: config-driven acquisition, quality flags, store-and-forward.

Adding a device is CONFIG, not code -- see `devices.default_fleet()` and the
README's "adding a device" section. The collector knows about protocols, not about
machines.

THE DATA MODEL, and why each field is there:

    device, tag        who and what
    value              scaled engineering value, never a raw register
    device_ts          when the DEVICE says it happened (may be wrong; see LATHE-04)
    collector_ts       when WE received it (never wrong, never authoritative)
    quality            GOOD / STALE / BAD / UNCERTAIN
    seq                monotonic per-collector sequence number, the idempotency key

QUALITY FLAGS ARE NOT OPTIONAL. OPC-UA has had them since 1996 and every industrial
protocol that came after copied the idea, because "the value is 68.2" and "the
value was 68.2 three hours ago and the device has not spoken since" are different
facts and a float cannot carry the difference. Dropping quality at ingest is how
"the sensor was faulty for three weeks" becomes indistinguishable from "the process
changed" -- and the analytics team then explains the sensor fault as a process
improvement.

    GOOD       fresh reading, passed plausibility
    STALE      device answered but the value has not changed within its expected
               update window -- see `_stale_check` for why this needs BOTH a
               change test and a heartbeat
    BAD        protocol error, checksum failure, or connection down
    UNCERTAIN  value outside its configured plausible range: we received something,
               we do not believe it. This is the flag that catches a word-swapped
               float, which arrives as 1.7e19 rather than as an error.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import time
from dataclasses import dataclass, field

BUFFER_SCHEMA = """
CREATE TABLE IF NOT EXISTS buffer (
    seq          INTEGER PRIMARY KEY,
    device       TEXT NOT NULL,
    tag          TEXT NOT NULL,
    value        REAL,
    device_ts    REAL,
    collector_ts REAL NOT NULL,
    quality      TEXT NOT NULL,
    sent         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_unsent ON buffer(sent, seq);
"""

HISTORIAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    collector_id TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    device       TEXT NOT NULL,
    tag          TEXT NOT NULL,
    value        REAL,
    device_ts    REAL,
    collector_ts REAL NOT NULL,
    quality      TEXT NOT NULL,
    PRIMARY KEY (collector_id, seq)
);
"""


@dataclass
class Stats:
    read_ok: int = 0
    read_bad: int = 0
    frames_rejected_checksum: int = 0
    reconnects: int = 0
    captured: int = 0
    sent: int = 0
    dropped_overflow: int = 0
    duplicates_rejected: int = 0
    max_buffer_depth: int = 0
    by_quality: dict = field(default_factory=dict)
    per_device: dict = field(default_factory=dict)
    captured_by_tag: dict = field(default_factory=dict)
    dropped_by_tag: dict = field(default_factory=dict)


class Historian:
    """Upstream sink. Idempotent by (collector_id, seq)."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(HISTORIAN_SCHEMA)
        self.conn.commit()
        self.rejected = 0

    def ingest(self, collector_id: str, rows: list[tuple]) -> int:
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO samples (collector_id, seq, device, tag, value, "
            "device_ts, collector_ts, quality) VALUES (?,?,?,?,?,?,?,?)",
            [(collector_id,) + r for r in rows])
        self.conn.commit()
        inserted = self.conn.total_changes - before
        self.rejected += len(rows) - inserted
        return inserted

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0])

    def by_quality(self) -> dict:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT quality, COUNT(*) FROM samples GROUP BY quality")}


class Collector:
    def __init__(self, collector_id: str, buffer_path, historian: Historian,
                 max_buffer_rows: int = 20_000, overflow_policy: str = "priority",
                 batch_size: int = 400):
        self.collector_id = collector_id
        self.historian = historian
        self.max_buffer_rows = max_buffer_rows
        # OVERFLOW POLICY. An unbounded buffer is a lie about the hardware: a
        # gateway with 256 MB of RAM does not hold a three-day outage, and
        # pretending it does means the failure arrives as an OOM at 03:00 rather
        # than as a decision somebody made on purpose.
        #
        #   oldest   drop the oldest unsent rows      (keep recent trend)
        #   newest   drop incoming rows               (keep a contiguous history)
        #   priority drop the lowest-priority TAGS first, oldest within a tag
        #
        # "priority" is the default because it is the only one that survives the
        # question "which data would you rather lose". A 1 Hz temperature trend is
        # not worth the same as a part counter that feeds OEE, and a policy that
        # cannot express that is choosing by accident.
        assert overflow_policy in ("oldest", "newest", "priority")
        self.overflow_policy = overflow_policy
        self.batch_size = batch_size
        self.online = True
        self.stats = Stats()
        self.tag_priority: dict[str, int] = {}

        self.conn = sqlite3.connect(buffer_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(BUFFER_SCHEMA)
        self.conn.commit()
        row = self.conn.execute("SELECT MAX(seq) FROM buffer").fetchone()
        self._seq = int(row[0]) if row and row[0] is not None else 0
        self._last: dict[tuple[str, str], tuple[float, float]] = {}

    # -- capture ----------------------------------------------------------
    def record(self, device: str, tag: str, value: float | None, quality: str,
               device_ts: float | None) -> None:
        now = time.time()
        self._seq += 1
        self.conn.execute(
            "INSERT INTO buffer (seq, device, tag, value, device_ts, collector_ts, "
            "quality) VALUES (?,?,?,?,?,?,?)",
            (self._seq, device, tag, value, device_ts, now, quality))
        self.conn.commit()
        self.stats.captured += 1
        self.stats.by_quality[quality] = self.stats.by_quality.get(quality, 0) + 1
        self.stats.captured_by_tag[tag] = self.stats.captured_by_tag.get(tag, 0) + 1
        d = self.stats.per_device.setdefault(device, {})
        d[quality] = d.get(quality, 0) + 1
        self._enforce_bound()
        self.stats.max_buffer_depth = max(self.stats.max_buffer_depth, self.depth())

    def depth(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM buffer WHERE sent=0").fetchone()[0])

    def _enforce_bound(self) -> None:
        d = self.depth()
        if d <= self.max_buffer_rows:
            return
        excess = d - self.max_buffer_rows
        if self.overflow_policy == "oldest":
            sql = "SELECT seq FROM buffer WHERE sent=0 ORDER BY seq ASC LIMIT ?"
            args = (excess,)
        elif self.overflow_policy == "newest":
            sql = "SELECT seq FROM buffer WHERE sent=0 ORDER BY seq DESC LIMIT ?"
            args = (excess,)
        else:
            # Lowest priority first, oldest within a priority.
            rows = self.conn.execute(
                "SELECT seq, tag FROM buffer WHERE sent=0 ORDER BY seq ASC").fetchall()
            # LOWEST priority first, oldest within a priority. The sign here was
            # inverted in the first version (`-priority`), so the policy dropped
            # the MOST important tags first -- `part_count` lost 118 rows while
            # the lowest-priority `flow_lpm` lost none. Nothing failed, the buffer
            # stayed bounded, and the loss/duplication proof still passed: the
            # only thing that caught it was adding the per-tag drop counters and
            # looking at the table. A policy nobody measures is a policy nobody
            # has.
            rows.sort(key=lambda r: (self.tag_priority.get(r[1], 5), r[0]))
            victims = rows[:excess]
            for _, t in victims:
                self.stats.dropped_by_tag[t] = self.stats.dropped_by_tag.get(t, 0) + 1
            self.conn.executemany("DELETE FROM buffer WHERE seq=?",
                                  [(v[0],) for v in victims])
            self.conn.commit()
            self.stats.dropped_overflow += len(victims)
            return
        rows2 = self.conn.execute(sql.replace("SELECT seq", "SELECT seq, tag"),
                                  args).fetchall()
        for _, t in rows2:
            self.stats.dropped_by_tag[t] = self.stats.dropped_by_tag.get(t, 0) + 1
        victims = [r[0] for r in rows2]
        self.conn.executemany("DELETE FROM buffer WHERE seq=?", [(v,) for v in victims])
        self.conn.commit()
        self.stats.dropped_overflow += len(victims)

    # -- quality ----------------------------------------------------------
    def classify(self, device: str, tag, value: float | None,
                 stale_window_s: float) -> str:
        """Assign a quality flag. The stale test is the interesting one.

        STALE detection needs BOTH a change test and a heartbeat, and neither
        alone is sufficient:

          change-only   a genuinely steady process (a tank at setpoint, a machine
                        in a stable state) looks frozen, and the collector cries
                        wolf on the healthiest signal on the plant.
          heartbeat-only  the frozen device still answers every poll on time, so
                        the heartbeat is perfect and the freeze is invisible.

        So: a value is STALE if it has not changed by more than its deadband for
        longer than its stale window, where the window is a multiple of the scan
        rate. It is the combination -- "we are being answered, and the answer has
        not moved for N scans" -- that catches a frozen device without flagging a
        stable one. The window has to be tag-specific for the same reason: a
        setpoint that legitimately never moves needs a much longer one than a
        vibration reading.
        """
        if value is None:
            return "BAD"
        if tag.min_plausible is not None and value < tag.min_plausible:
            return "UNCERTAIN"
        if tag.max_plausible is not None and value > tag.max_plausible:
            return "UNCERTAIN"
        key = (device, tag.name)
        now = time.time()
        prev = self._last.get(key)
        if prev is None:
            self._last[key] = (value, now)
            return "GOOD"
        last_val, last_change = prev
        if abs(value - last_val) > max(tag.deadband, 1e-9):
            self._last[key] = (value, now)
            return "GOOD"
        if now - last_change > stale_window_s:
            return "STALE"
        return "GOOD"

    # -- uplink -----------------------------------------------------------
    def drain(self) -> int:
        if not self.online:
            return 0
        total = 0
        while True:
            rows = self.conn.execute(
                "SELECT seq, device, tag, value, device_ts, collector_ts, quality "
                "FROM buffer WHERE sent=0 ORDER BY seq LIMIT ?",
                (self.batch_size,)).fetchall()
            if not rows:
                break
            before = self.historian.rejected
            self.historian.ingest(self.collector_id, rows)
            self.stats.duplicates_rejected += self.historian.rejected - before
            # Mark sent only AFTER the historian commits. A crash between the two
            # re-sends the batch, which the primary key absorbs. That ordering is
            # the entire at-least-once + idempotent-sink design.
            self.conn.executemany("UPDATE buffer SET sent=1 WHERE seq=?",
                                  [(r[0],) for r in rows])
            self.conn.commit()
            self.stats.sent += len(rows)
            total += len(rows)
        return total

    def replay_all(self) -> int:
        rows = self.conn.execute(
            "SELECT seq, device, tag, value, device_ts, collector_ts, quality "
            "FROM buffer ORDER BY seq").fetchall()
        before = self.historian.rejected
        self.historian.ingest(self.collector_id, rows)
        self.stats.duplicates_rejected += self.historian.rejected - before
        return len(rows)

    def close(self) -> None:
        self.conn.close()
