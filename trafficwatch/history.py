"""Traffic history behind the dashboard's over-time charts.

One row per port per sample: every 10 minutes with API keys, or after each
hourly aircraft snapshot in no-key mode. Rows older than 7 days are deleted.
Aircraft columns are NULL when the port's area wasn't freshly polled, so
charts show a gap rather than an invented zero.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter

from .config import STATE_DIR

HISTORY_DB = STATE_DIR / "history.sqlite"
SAMPLE_EVERY = 600
KEEP_S = 7 * 86400
TOP_N = 10

ALT_BANDS = (("< 5,000 ft", 5000), ("5,000–15,000 ft", 15000), ("15,000–30,000 ft", 30000), ("> 30,000 ft", float("inf")))


def summarize_aircraft(items: list[dict]) -> dict:
    alt = Counter()
    airlines = Counter()
    mfr = Counter()
    for a in items:
        if a.get("gnd"):
            alt["On ground"] += 1
        else:
            ft = a.get("alt") or 0
            alt[next(label for label, top in ALT_BANDS if ft < top)] += 1
        airlines[a.get("airline") or "Private / unknown"] += 1
        if a.get("mfr"):
            mfr[a["mfr"]] += 1
    return {"total": len(items), "alt": dict(alt), "airlines": _top(airlines), "mfr": _top(mfr)}


def summarize_vessels(items: list[dict]) -> dict:
    flags = Counter(v["flag"] for v in items if v.get("flag"))
    return {"total": len(items), "moving": sum(1 for v in items if (v.get("sog") or 0) >= 0.5),
            "flags": _top(flags)}


def _top(c: Counter) -> dict:
    """Top N entries plus an 'Other' bucket, so totals stay correct."""
    keep = dict(c.most_common(TOP_N))
    rest = sum(c.values()) - sum(keep.values())
    if rest:
        keep["Other"] = keep.get("Other", 0) + rest
    return keep


class History:
    def __init__(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Only the server's event-loop thread touches this connection.
        self.db = sqlite3.connect(HISTORY_DB, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS samples (
            port TEXT NOT NULL, ts REAL NOT NULL,
            ac_total INTEGER, ac_alt TEXT, ac_airlines TEXT, ac_mfr TEXT,
            ves_total INTEGER, ves_moving INTEGER, ves_flags TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS samples_port_ts ON samples (port, ts)")
        self.db.commit()
        self.last_prune = 0.0

    def record(self, port: str, ts: float, ac: dict | None, vs: dict | None) -> None:
        if ac is None and vs is None:
            return
        self.db.execute(
            "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)",
            (port, ts,
             ac["total"] if ac else None, json.dumps(ac["alt"]) if ac else None,
             json.dumps(ac["airlines"]) if ac else None, json.dumps(ac["mfr"]) if ac else None,
             vs["total"] if vs else None, vs["moving"] if vs else None, json.dumps(vs["flags"]) if vs else None))

    def commit(self) -> None:
        self.db.commit()
        if time.time() - self.last_prune > 3600:
            self.db.execute("DELETE FROM samples WHERE ts < ?", (time.time() - KEEP_S,))
            self.db.commit()
            self.last_prune = time.time()

    def query(self, port: str, hours: float) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, ac_total, ac_alt, ac_airlines, ac_mfr, ves_total, ves_moving FROM samples "
            "WHERE port = ? AND ts >= ? ORDER BY ts", (port, time.time() - hours * 3600)).fetchall()
        return [{"ts": ts, "ac_total": act, "ac_alt": json.loads(aa) if aa else None,
                 "ac_airlines": json.loads(al) if al else None, "ac_mfr": json.loads(mf) if mf else None,
                 "ves_total": vt, "ves_moving": vm}
                for ts, act, aa, al, mf, vt, vm in rows]
