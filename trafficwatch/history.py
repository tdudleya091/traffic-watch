"""Traffic history behind the dashboard's over-time charts (schema v2).

Two tables, each written only when its count is trustworthy:
  aircraft_samples  recorded right after an area is polled (counts are never stale),
                    at most once per port per 10 min
  vessel_samples    every 10 min, but only after the AIS feed has been connected for
                    15 min (before that the ship picture is still filling in)
WAL mode lets the app window and the background collector share the file safely.
Rows older than 7 days are deleted.
"""
from __future__ import annotations

import sqlite3
import statistics
import time

from .config import STATE_DIR

HISTORY_DB = STATE_DIR / "history.sqlite"
SCHEMA_VERSION = 2
SAMPLE_EVERY = 600
AIS_WARMUP_S = 900
KEEP_S = 7 * 86400
MIN_GAP_S = 1500   # never break a line for spacing shorter than 25 min


class History:
    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Only the server's event-loop thread touches this connection.
        self.db = sqlite3.connect(HISTORY_DB, check_same_thread=False, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS aircraft_samples (port TEXT NOT NULL, ts REAL NOT NULL, total INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS vessel_samples (port TEXT NOT NULL, ts REAL NOT NULL,
                                                       total INTEGER NOT NULL, moving INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS aircraft_port_ts ON aircraft_samples (port, ts);
            CREATE INDEX IF NOT EXISTS vessel_port_ts ON vessel_samples (port, ts);
        """)
        self._migrate_v1()
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.db.commit()
        # Last aircraft sample per port, so restarts (or window + collector) don't double-record.
        self.last_aircraft = dict(self.db.execute("SELECT port, MAX(ts) FROM aircraft_samples GROUP BY port"))
        self.last_prune = 0.0

    def _migrate_v1(self) -> None:
        """Move v1.0.2's single `samples` table across, dropping the rows its sampler got wrong:
        aircraft zeros (often stale data, not empty skies) and ship counts taken in the first
        15 min of a session (AIS still filling in)."""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='samples'").fetchone():
            return
        rows = self.db.execute("SELECT port, ts, ac_total, ves_total, ves_moving FROM samples ORDER BY port, ts")
        aircraft, vessels = [], []
        prev: dict[str, float] = {}
        session_start: dict[str, float] = {}
        for port, ts, ac, vt, vm in rows:
            if ac:
                aircraft.append((port, ts, ac))
            if vt is not None:
                if port not in prev or ts - prev[port] >= 1200:
                    session_start[port] = ts
                prev[port] = ts
                if ts - session_start[port] >= AIS_WARMUP_S:
                    vessels.append((port, ts, vt, vm or 0))
        # A quick restart leaves no visible gap, so also drop ship counts under half the port's median.
        by_port: dict[str, list[int]] = {}
        for port, _, vt, _ in vessels:
            by_port.setdefault(port, []).append(vt)
        medians = {p: statistics.median(v) for p, v in by_port.items()}
        vessels = [r for r in vessels if medians[r[0]] < 20 or r[2] >= medians[r[0]] / 2]
        self.db.executemany("INSERT INTO aircraft_samples VALUES (?,?,?)", aircraft)
        self.db.executemany("INSERT INTO vessel_samples VALUES (?,?,?,?)", vessels)
        self.db.execute("DROP TABLE samples")
        self.db.commit()

    def record_aircraft(self, port: str, ts: float, total: int) -> bool:
        if ts - self.last_aircraft.get(port, 0) < SAMPLE_EVERY - 30:
            return False
        self.db.execute("INSERT INTO aircraft_samples VALUES (?,?,?)", (port, ts, total))
        self.last_aircraft[port] = ts
        return True

    def record_vessels(self, port: str, ts: float, total: int, moving: int) -> None:
        self.db.execute("INSERT INTO vessel_samples VALUES (?,?,?,?)", (port, ts, total, moving))

    def commit(self) -> None:
        self.db.commit()
        if time.time() - self.last_prune > 3600:
            cutoff = time.time() - KEEP_S
            self.db.execute("DELETE FROM aircraft_samples WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM vessel_samples WHERE ts < ?", (cutoff,))
            self.db.commit()
            self.last_prune = time.time()

    @staticmethod
    def _gap(ts: list[float]) -> float:
        """Spacing beyond which two points are treated as a recording gap: 2.5x the usual spacing."""
        steps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
        return max(MIN_GAP_S, 2.5 * statistics.median(steps)) if steps else MIN_GAP_S

    def query(self, port: str, hours: float) -> dict:
        since = time.time() - hours * 3600
        ac = self.db.execute("SELECT ts, total FROM aircraft_samples WHERE port = ? AND ts >= ? ORDER BY ts",
                             (port, since)).fetchall()
        vs = self.db.execute("SELECT ts, total, moving FROM vessel_samples WHERE port = ? AND ts >= ? ORDER BY ts",
                             (port, since)).fetchall()
        return {
            "aircraft": [{"ts": ts, "total": t} for ts, t in ac],
            "vessels": [{"ts": ts, "total": t, "moving": m} for ts, t, m in vs],
            "gap_s": {"aircraft": self._gap([r[0] for r in ac]), "vessels": self._gap([r[0] for r in vs])},
        }
