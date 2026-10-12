"""Detail lookups for the live layers: aircraft type/airline/owner and ship type/flag/operator.

Reference files (all in data/ref/, built by refresh_reference()):
  aircraft.sqlite  tar1090-db (wiedehopf/tar1090-db, refreshed weekly upstream): icao24 ->
                   registration, type code, model, year, registered owner, flags
  airlines.json    OpenFlights airlines.dat: ICAO airline designator -> name
  mid.json         powers9/MIDs (Apache-2.0): MMSI MID -> flag country
  types.json       ICAO Doc 8643 aircraft types (via OpenSky): type designator -> manufacturer
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sqlite3
import threading
import time
from functools import lru_cache

import httpx

from .config import REF_DIR

AIRCRAFT_DB = REF_DIR / "aircraft.sqlite"
AIRLINES_FILE = REF_DIR / "airlines.json"
MID_FILE = REF_DIR / "mid.json"
TYPES_FILE = REF_DIR / "types.json"
TYPES_URL = "https://s3.opensky-network.org/data-samples/metadata/doc8643AircraftTypes.csv"
TAR1090_URL = "https://github.com/wiedehopf/tar1090-db/raw/refs/heads/csv/aircraft.csv.gz"
AIRLINES_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"
MIDS_URL = "https://raw.githubusercontent.com/powers9/MIDs/master/mids.json"
MAX_AGE_S = 30 * 86400
HEADERS = {"User-Agent": "traffic-watch/1.0 (personal desktop app)"}

# OpenFlights stops around 2017 and misses many US regionals and Japanese carriers.
AIRLINE_OVERRIDES = {
    "AAL": "American Airlines", "DAL": "Delta Air Lines", "UAL": "United Airlines", "SWA": "Southwest Airlines",
    "ASA": "Alaska Airlines", "JBU": "JetBlue Airways", "NKS": "Spirit Airlines", "FFT": "Frontier Airlines",
    "AAY": "Allegiant Air", "HAL": "Hawaiian Airlines", "MXY": "Breeze Airways", "SCX": "Sun Country Airlines",
    "SKW": "SkyWest Airlines", "ENY": "Envoy Air", "RPA": "Republic Airways", "EDV": "Endeavor Air",
    "JIA": "PSA Airlines", "ASH": "Mesa Airlines", "GJS": "GoJet Airlines", "QXE": "Horizon Air",
    "PDT": "Piedmont Airlines", "CPZ": "Compass Airlines", "FDX": "FedEx Express", "UPS": "UPS Airlines",
    "GTI": "Atlas Air", "ABX": "ABX Air", "ATN": "Air Transport International", "CKS": "Kalitta Air",
    "JAL": "Japan Airlines", "ANA": "All Nippon Airways", "APJ": "Peach Aviation", "SJO": "Spring Japan",
    "JJP": "Jetstar Japan", "SFJ": "StarFlyer", "ADO": "Air Do", "SNJ": "Solaseed Air", "IBX": "Ibex Airlines",
    "JTA": "Japan Transocean Air", "TZP": "ZIPAIR Tokyo", "AKX": "ANA Wings", "JAC": "Japan Air Commuter",
    "JLJ": "J-Air", "FDA": "Fuji Dream Airlines", "RAC": "Ryukyu Air Commuter", "ORC": "Oriental Air Bridge",
}
MID_OVERRIDES = {"249": "Malta"}
# ICAO manufacturer codes -> the name people know the company by.
MANUFACTURER_ALIASES = {
    "CANADAIR": "Bombardier", "GULFSTREAM AEROSPACE": "Gulfstream", "DE HAVILLAND CANADA": "De Havilland Canada",
    "MCDONNELL DOUGLAS": "McDonnell Douglas", "BRITISH AEROSPACE": "BAE Systems", "AIRBUS HELICOPTERS": "Airbus Helicopters",
    "EUROCOPTER": "Airbus Helicopters", "BELL HELICOPTER TEXTRON": "Bell", "TEXTRON AVIATION": "Textron (Cessna/Beechcraft)",
    "RAYTHEON": "Beechcraft", "HAWKER BEECHCRAFT": "Beechcraft", "AGUSTAWESTLAND": "Leonardo", "AGUSTA": "Leonardo",
    "LEONARDO": "Leonardo", "SIKORSKY": "Sikorsky", "MITSUBISHI": "Mitsubishi", "ATR": "ATR", "DASSAULT": "Dassault",
}
COUNTRY_SHORT = {"United States of America": "United States", "Korea": "South Korea",
                 "United Kingdom of Great Britain and Northern Ireland": "United Kingdom"}

# OpenSky "extended" emitter categories.
ADSB_CATEGORY = {2: "Light aircraft", 3: "Small aircraft", 4: "Large aircraft", 5: "Large aircraft (high vortex)",
                 6: "Heavy aircraft", 7: "High-performance aircraft", 8: "Helicopter", 9: "Glider",
                 10: "Balloon / airship", 11: "Parachutist", 12: "Ultralight", 14: "Drone (UAV)",
                 15: "Space vehicle", 16: "Emergency vehicle", 17: "Service vehicle"}
HELI_TYPES = re.compile(r"^(H\d\d|EC\d\d|AS\d\d|B06|B06T|B407|B412|B429|B505|R22|R44|R66|S76|S92|A109|A119|A139|"
                        r"A169|A189|AS50|AS55|AS65|EC20|EC30|EC35|EC45|EC55|EC75|MD52|MD60|UH1|H60|H47|H53|V22|"
                        r"BK17|NH90|CH47|S61|S64|AW09)$")

AIS_TYPE = {
    20: "Wing in ground craft", 30: "Fishing vessel", 31: "Towing vessel", 32: "Towing vessel (large tow)",
    33: "Dredger", 34: "Diving support vessel", 35: "Military vessel", 36: "Sailing vessel", 37: "Pleasure craft",
    40: "High-speed craft", 50: "Pilot vessel", 51: "Search & rescue vessel", 52: "Tug", 53: "Port tender",
    54: "Anti-pollution vessel", 55: "Law enforcement vessel", 58: "Medical transport", 59: "Non-combatant vessel",
    60: "Passenger ship", 70: "Cargo ship", 80: "Tanker", 90: "Other vessel",
}
HAZARD = {1: "hazardous cat. A", 2: "hazardous cat. B", 3: "hazardous cat. C", 4: "hazardous cat. D"}

# Operator guessed from the ship's name: (pattern, operator, only for AIS cargo types 70-79).
# Order matters: first match wins.
OPERATOR_PATTERNS = [
    (r"^MSC ", "MSC (Mediterranean Shipping Co.)", False), (r"^MAERSK ", "Maersk", False),
    (r"^CMA CGM ", "CMA CGM", False), (r"^APL ", "APL (CMA CGM)", False), (r"^COSCO ", "COSCO Shipping", False),
    (r"^OOCL ", "OOCL (COSCO)", False), (r"^EVER ", "Evergreen Marine", False),
    (r"^ONE ", "Ocean Network Express", False), (r"^(HMM|HYUNDAI) ", "HMM", False), (r"^YM ", "Yang Ming", False),
    (r"^ZIM ", "ZIM", False), (r"^NYK ", "NYK Line", False), (r"^MOL ", "Mitsui O.S.K. Lines", False),
    (r"^WAN HAI ", "Wan Hai Lines", False), (r"^SEASPAN ", "Seaspan (charter owner)", False),
    (r"^(MATSON |MANUKAI$|MANULANI$|MAUNALEI$|MAUNAWILI$|KAIMANA HILA$|DANIEL K\.? INOUYE$)", "Matson", False),
    (r"^PASHA ", "Pasha Hawaii", False), (r"^CROWLEY", "Crowley", False), (r"^FOSS ", "Foss Maritime", False),
    (r"^KIRBY ", "Kirby Corp.", False), (r"^(TOTE |ISLA BELLA$|PERLA DEL CARIBE$)", "TOTE Maritime", False),
    (r" EXPRESS$", "Hapag-Lloyd", True),
    (r"^CARNIVAL ", "Carnival Cruise Line", False), (r" OF THE SEAS$", "Royal Caribbean", False),
    (r"^NORWEGIAN ", "Norwegian Cruise Line", False), (r"^CELEBRITY ", "Celebrity Cruises", False),
    (r" PRINCESS$", "Princess Cruises", False), (r"^DISNEY ", "Disney Cruise Line", False),
    (r"^(NIEUW STATENDAM|NIEUW AMSTERDAM|KONINGSDAM|ROTTERDAM|ZUIDERDAM|OOSTERDAM|WESTERDAM|EURODAM|"
     r"VOLENDAM|ZAANDAM|NOORDAM)$", "Holland America Line", False),
    (r"^VIKING ", "Viking", False),
]
OPERATOR_RE = [(re.compile(p), name, cargo) for p, name, cargo in OPERATOR_PATTERNS]
CALLSIGN_RE = re.compile(r"^([A-Z]{3})\d")

_db_lock = threading.Lock()
_db: sqlite3.Connection | None = None


# ---------------------------------------------------------------- building
def _stale(path, max_age=MAX_AGE_S) -> bool:
    return not path.exists() or time.time() - path.stat().st_mtime > max_age


def build_aircraft_db() -> int:
    """Download tar1090-db and write data/ref/aircraft.sqlite atomically."""
    r = httpx.get(TAR1090_URL, follow_redirects=True, timeout=180, headers=HEADERS)
    r.raise_for_status()
    tmp = AIRCRAFT_DB.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE aircraft (icao TEXT PRIMARY KEY, reg TEXT, type TEXT, flags TEXT, "
                "model TEXT, year TEXT, owner TEXT) WITHOUT ROWID")
    rows = []
    with gzip.open(io.BytesIO(r.content), "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split(";")
            if len(parts) < 7 or not parts[0]:
                continue
            rows.append((parts[0].lower(), *[(p.strip() or None) for p in parts[1:7]]))
    con.executemany("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    tmp.replace(AIRCRAFT_DB)
    _reset_db()
    return len(rows)


def build_airlines() -> int:
    r = httpx.get(AIRLINES_URL, timeout=60, headers=HEADERS)
    r.raise_for_status()
    out: dict[str, dict] = {}
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) < 8:
            continue
        icao, name, country, active = row[4], row[1], row[6], row[7]
        if not re.fullmatch(r"[A-Z]{3}", icao or ""):
            continue
        if icao in out and not (active == "Y" and out[icao]["active"] != "Y"):
            continue
        out[icao] = {"name": name, "country": country, "active": active}
    data = {k: v["name"] for k, v in out.items()}
    data.update(AIRLINE_OVERRIDES)
    AIRLINES_FILE.write_text(json.dumps(data, ensure_ascii=False))
    _airlines.cache_clear()
    return len(data)


def build_mids() -> int:
    r = httpx.get(MIDS_URL, timeout=60, headers=HEADERS)
    r.raise_for_status()
    data = {mid: COUNTRY_SHORT.get(v[3], v[3]) for mid, v in r.json().items()}
    data.update(MID_OVERRIDES)
    MID_FILE.write_text(json.dumps(data, ensure_ascii=False))
    _mids.cache_clear()
    return len(data)


def build_types() -> int:
    """Type designator -> manufacturer, using the most common manufacturer listed for each type."""
    r = httpx.get(TYPES_URL, timeout=60, headers=HEADERS)
    r.raise_for_status()
    counts: dict[str, dict[str, int]] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        d, m = row.get("Designator", "").strip(), row.get("ManufacturerCode", "").strip()
        if d and m:
            counts.setdefault(d, {}).setdefault(m, 0)
            counts[d][m] += 1
    data = {d: _manufacturer_name(max(ms, key=ms.get)) for d, ms in counts.items()}
    TYPES_FILE.write_text(json.dumps(data, ensure_ascii=False))
    _types.cache_clear()
    return len(data)


def _manufacturer_name(code: str) -> str:
    return MANUFACTURER_ALIASES.get(code) or " ".join(w.capitalize() for w in code.split())


def refresh_reference(force: bool = False, log=print) -> None:
    """Build whichever lookup files are missing or older than 30 days."""
    REF_DIR.mkdir(parents=True, exist_ok=True)
    for path, builder, label in ((AIRLINES_FILE, build_airlines, "airlines"), (MID_FILE, build_mids, "ship flags"),
                                 (TYPES_FILE, build_types, "aircraft types"),
                                 (AIRCRAFT_DB, build_aircraft_db, "aircraft")):
        if force or _stale(path):
            try:
                log(f"[enrich] {label}: {builder()} entries")
            except (httpx.HTTPError, OSError, sqlite3.Error) as e:
                log(f"[enrich] {label}: refresh failed ({e.__class__.__name__}); keeping existing data")


# ---------------------------------------------------------------- lookups
@lru_cache(maxsize=1)
def _airlines() -> dict[str, str]:
    try:
        return json.loads(AIRLINES_FILE.read_text())
    except (OSError, ValueError):
        return dict(AIRLINE_OVERRIDES)


@lru_cache(maxsize=1)
def _types() -> dict[str, str]:
    try:
        return json.loads(TYPES_FILE.read_text())
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _mids() -> dict[str, str]:
    try:
        return json.loads(MID_FILE.read_text())
    except (OSError, ValueError):
        return dict(MID_OVERRIDES)


def _reset_db() -> None:
    global _db
    with _db_lock:
        if _db is not None:
            _db.close()
        _db = None
    _aircraft_row.cache_clear()


@lru_cache(maxsize=50_000)
def _aircraft_row(icao: str) -> tuple | None:
    global _db
    with _db_lock:
        if _db is None:
            if not AIRCRAFT_DB.exists():
                return None
            _db = sqlite3.connect(f"file:{AIRCRAFT_DB}?mode=ro", uri=True, check_same_thread=False)
        return _db.execute("SELECT reg, type, flags, model, year, owner FROM aircraft WHERE icao = ?",
                           (icao,)).fetchone()


def _tidy(model: str | None) -> str | None:
    """'BOEING 787-9 Dreamliner' -> 'Boeing 787-9 Dreamliner'. Words with digits and short
    acronyms (ATR, MAX) keep their case; an all-caps name has every plain word title-cased."""
    if not model:
        return model
    all_caps = model.isupper()
    return " ".join(
        w.capitalize() if w.isalpha() and w.isupper() and (len(w) > 3 or all_caps) and w not in ("ATR",) else w
        for w in model.split())


def airline_for(callsign: str | None) -> str | None:
    m = CALLSIGN_RE.match((callsign or "").strip().upper())
    return _airlines().get(m.group(1)) if m else None


def aircraft_info(icao24: str, callsign: str | None, category: int | None) -> dict:
    out: dict = {"airline": airline_for(callsign)}
    row = _aircraft_row(icao24.lower())
    if row:
        reg, typ, flags, model, year, owner = row
        flags = flags or ""
        military = flags[:1] == "1"
        private = flags[2:3] == "1" or flags[3:4] == "1"   # PIA / LADD: owner asked for limited display
        out.update(reg=reg, type=typ, model=_tidy(model), year=year, mil=military,
                   owner=None if private or military else owner)
    out["cat_label"] = ADSB_CATEGORY.get(category or 0)
    out["heli"] = category == 8 or bool(out.get("type") and HELI_TYPES.match(out["type"]))
    mfr = _types().get(out.get("type") or "")
    if not mfr and out.get("model"):
        mfr = out["model"].split()[0]
    out["mfr"] = mfr
    return out


def ship_type_label(code: int | None) -> str | None:
    if code is None:
        return None
    if 60 <= code <= 89:
        base = AIS_TYPE[code // 10 * 10]
        hz = HAZARD.get(code % 10)
        return f"{base} — {hz}" if hz else base
    if 40 <= code <= 49:
        return AIS_TYPE[40]
    if 20 <= code <= 29:
        return AIS_TYPE[20]
    if 90 <= code <= 99:
        return AIS_TYPE[90]
    return AIS_TYPE.get(code)


def flag_for(mmsi: str) -> str | None:
    if len(mmsi) != 9 or not mmsi.isdigit():
        return None
    mid = mmsi[:3]
    if mmsi[0] in "0":            # group/coast-station MMSIs: 0MIDxxxxx / 00MIDxxxx
        mid = mmsi[2:5] if mmsi[1] == "0" else mmsi[1:4]
    elif mmsi.startswith(("98", "99")):  # craft associated with a parent ship, aids to navigation
        mid = mmsi[2:5]
    elif mmsi.startswith("111"):  # SAR aircraft
        mid = mmsi[3:6]
    return _mids().get(mid)


def operator_for(name: str | None, ship_type: int | None) -> str | None:
    n = (name or "").strip().upper()
    if not n or n.startswith("MMSI "):
        return None
    cargo = ship_type is not None and 70 <= ship_type <= 79
    for rx, op, cargo_only in OPERATOR_RE:
        if (cargo or not cargo_only) and rx.search(n):
            return op
    return None


def vessel_info(mmsi: str, ship_type: int | None, name: str | None) -> dict:
    return {"type_label": ship_type_label(ship_type), "flag": flag_for(mmsi),
            "operator": operator_for(name, ship_type)}
