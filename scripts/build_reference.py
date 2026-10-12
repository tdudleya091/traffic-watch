"""Download and trim the static reference data.

Map layers (airports, highways, cities, ports) are cut down to whatever falls
inside a configured port box (padded by 1 degree), so adding ports anywhere in
the world only needs a rerun of this script. Raw downloads are cached in
data/raw/; the trimmed files the app reads go to data/ref/.

    python scripts/build_reference.py            # build what is missing or stale
    python scripts/build_reference.py --refresh  # also re-download the aircraft/airline/flag lookups
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trafficwatch.config import DATA, REF_DIR, BBox, load_ports  # noqa: E402
from trafficwatch.enrich import refresh_reference  # noqa: E402

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
MIN_ROAD_POINTS = 50   # below this, a group box falls back to OpenStreetMap highways
MIN_OSM_WAYS = 50      # below this, the OSM query widens to the next road class
OSM_LEVELS = ["motorway", "trunk", "primary"]
SIMPLIFY_DEG = 0.002
COVER_PAD_DEG = 1.0
UA = {"User-Agent": "traffic-watch/1.0 (personal desktop app)"}

RAW = DATA / "raw"
NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
SOURCES = {
    "airports.csv": "https://davidmegginson.github.io/ourairports-data/airports.csv",
    "roads.geojson": NE + "ne_10m_roads.geojson",
    "places.geojson": NE + "ne_10m_populated_places_simple.geojson",
    "ports.geojson": NE + "ne_10m_ports.geojson",
}


class Coverage:
    """Union of every port box, padded, so reference data is kept only where a map needs it."""

    def __init__(self, boxes: list[BBox]) -> None:
        self.boxes = [b.padded(COVER_PAD_DEG) for b in boxes]

    def __contains__(self, latlon: tuple[float, float]) -> bool:
        return any(b.contains(*latlon) for b in self.boxes)


def fetch_raw() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for name, url in SOURCES.items():
            path = RAW / name
            if path.exists():
                continue
            print(f"downloading {name} ...")
            r = client.get(url)
            r.raise_for_status()
            path.write_bytes(r.content)


def build_airports(cover: Coverage) -> int:
    out = []
    with open(RAW / "airports.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            major = row["type"] == "large_airport"
            regional = row["type"] == "medium_airport" and row["scheduled_service"] == "yes"
            if not (major or regional):
                continue
            lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            if (lat, lon) not in cover:
                continue
            out.append({
                "name": row["name"],
                "iata": row["iata_code"],
                "icao": row["icao_code"] or row["gps_code"],
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "major": major,
            })
    (REF_DIR / "airports.json").write_text(json.dumps(out, ensure_ascii=False))
    return len(out)


def route_label(props: dict) -> str | None:
    name = (props.get("name") or "").strip()
    level = props.get("level")
    if not name or level in ("Ferry", "U/C"):
        return None
    if level == "Interstate":
        return f"I-{name}"
    if level == "Federal":
        return f"US {name}"
    if props.get("type") in ("Major Highway", "Beltway"):
        return f"SR {name}" if name.isdigit() else name
    return None


def build_roads(cover: Coverage, groups: dict) -> int:
    src = json.loads((RAW / "roads.geojson").read_text())
    feats = []
    for f in src["features"]:
        p = f["properties"]
        label = route_label(p)
        if label is None:
            continue
        geom = f["geometry"]
        lines = geom["coordinates"] if geom["type"] == "MultiLineString" else [geom["coordinates"]]
        if not any((lat, lon) in cover for line in lines for lon, lat in line):
            continue
        lines = [[[round(x, 4), round(y, 4)] for x, y in line] for line in lines]
        feats.append({"label": label, "interstate": p.get("level") == "Interstate", "lines": lines})
    feats += osm_fallback_roads(feats, groups)
    (REF_DIR / "roads.json").write_text(json.dumps(feats, ensure_ascii=False))
    return len(feats)


def osm_label(ref: str) -> tuple[str, bool]:
    """Label and 'shield as expressway' flag for an OSM ref (US 'I 5', Alaska 'AK 1', Japan 'E1' / '16')."""
    ref = ref.split(";")[0].strip()
    if ref.startswith("I "):
        return ref.replace("I ", "I-", 1), True
    if re.fullmatch(r"E\d+[A-Z]?", ref):      # Japanese expressway numbering
        return ref, True
    if ref.isdigit():                          # Japanese national routes
        return f"Rt {ref}", False
    return ref, False


def _simplify(line: list[list[float]], tol: float = SIMPLIFY_DEG) -> list[list[float]]:
    """Ramer-Douglas-Peucker in degrees; ~0.002 deg (~200 m) is invisible at the map's zoom levels."""
    if len(line) < 3:
        return line
    (x1, y1), (x2, y2) = line[0], line[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
    idx, dmax = 0, 0.0
    for i in range(1, len(line) - 1):
        d = abs(dy * line[i][0] - dx * line[i][1] + x2 * y1 - y2 * x1) / norm
        if d > dmax:
            idx, dmax = i, d
    if dmax <= tol:
        return [line[0], line[-1]]
    return _simplify(line[:idx + 1], tol)[:-1] + _simplify(line[idx:], tol)


def _overpass(b: BBox, levels: str) -> list[dict]:
    """Query Overpass, retrying across public mirrors (the main server often answers 429/504 when busy)."""
    query = (f'[out:json][timeout:170];way["highway"~"^({levels})$"]["ref"]'
             f"({b.lamin:.4f},{b.lomin:.4f},{b.lamax:.4f},{b.lomax:.4f});out geom;")
    last = ""
    for attempt in range(6):
        url = OVERPASS_MIRRORS[attempt % len(OVERPASS_MIRRORS)]
        host = url.split("/")[2]
        try:
            r = httpx.post(url, data={"data": query}, timeout=200, headers=UA)
            if r.status_code == 200:
                return r.json().get("elements", [])
            last = f"HTTP {r.status_code} from {host}"
            wait = 30 * (attempt + 1) if r.status_code == 429 else 5 * (attempt + 1)
        except (httpx.HTTPError, ValueError) as e:
            last, wait = f"{e.__class__.__name__} from {host}", 5 * (attempt + 1)
        print(f"    overpass attempt {attempt + 1}: {last}; waiting {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"Overpass unavailable ({last})")


def _overpass_tiled(b: BBox, levels: str) -> list[dict]:
    """Whole box first; if Overpass keeps failing (big, dense areas), retry as four quarter tiles."""
    try:
        return _overpass(b, levels)
    except RuntimeError:
        print("    retrying as 4 smaller tiles ...")
    mlat, mlon = (b.lamin + b.lamax) / 2, (b.lomin + b.lomax) / 2
    tiles = [BBox(b.lamin, b.lomin, mlat, mlon), BBox(b.lamin, mlon, mlat, b.lomax),
             BBox(mlat, b.lomin, b.lamax, mlon), BBox(mlat, mlon, b.lamax, b.lomax)]
    seen, out = set(), []
    for t in tiles:
        for w in _overpass(t, levels):
            if w["id"] not in seen:     # ways crossing a tile edge come back twice
                seen.add(w["id"])
                out.append(w)
    return out


def osm_fallback_roads(feats: list[dict], groups: dict) -> list[dict]:
    """Natural Earth has no highways for some areas (Alaska, Hawaii, Japan); fill those from OSM.

    Results are cached per group in data/raw/osm_<group>.json and reused while the group box is unchanged.
    """
    extra = []
    for g in groups.values():
        b = g.bbox
        points = sum(1 for f in feats for line in f["lines"] for lon, lat in line if b.contains(lat, lon))
        if points >= MIN_ROAD_POINTS:
            continue
        cache = RAW / f"osm_{g.id}.json"
        key = [round(b.lamin, 3), round(b.lomin, 3), round(b.lamax, 3), round(b.lomax, 3)]
        ways = None
        if cache.exists():
            cached = json.loads(cache.read_text())
            if cached.get("bbox") == key and cached.get("levels") == OSM_LEVELS:
                ways = cached["ways"]
        if ways is None:
            print(f"  {g.id}: only {points} Natural Earth road points, fetching OSM highways ...")
            ways = []
            try:
                # Widen one road class at a time: dense areas (Tokyo) stop at motorways,
                # sparse ones (Alaska, Wakkanai) go on to trunk and primary roads.
                for level in OSM_LEVELS:
                    ways += _overpass_tiled(b, level)
                    if len(ways) >= MIN_OSM_WAYS:
                        break
            except RuntimeError as e:
                print(f"  {g.id}: skipping highways this run ({e}); rerun the script later to fill them in")
                continue
            ways = [{"ref": w["tags"]["ref"],
                     "geometry": _simplify([[n["lon"], n["lat"]] for n in w.get("geometry", [])])}
                    for w in ways]
            cache.write_text(json.dumps({"bbox": key, "levels": OSM_LEVELS, "ways": ways}))
        for w in ways:
            line = [[round(x, 4), round(y, 4)] for x, y in w["geometry"]]
            if len(line) > 1:
                label, expressway = osm_label(w["ref"])
                extra.append({"label": label, "interstate": expressway, "lines": [line]})
    return extra


def build_places(cover: Coverage) -> int:
    src = json.loads((RAW / "places.geojson").read_text())
    out = []
    for f in src["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        if (p.get("pop_max") or 0) < 2_000 or (lat, lon) not in cover:
            continue
        out.append({"name": p["name"], "lat": round(lat, 4), "lon": round(lon, 4), "pop": p["pop_max"]})
    (REF_DIR / "places.json").write_text(json.dumps(out, ensure_ascii=False))
    return len(out)


def build_ports(cover: Coverage) -> int:
    src = json.loads((RAW / "ports.geojson").read_text())
    out = []
    for f in src["features"]:
        lon, lat = f["geometry"]["coordinates"]
        if (lat, lon) in cover:
            out.append({"name": f["properties"]["name"], "lat": round(lat, 4), "lon": round(lon, 4)})
    (REF_DIR / "ports.json").write_text(json.dumps(out, ensure_ascii=False))
    return len(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download aircraft/airline/flag lookups")
    args = ap.parse_args()

    REF_DIR.mkdir(parents=True, exist_ok=True)
    fetch_raw()
    ports, groups = load_ports()
    cover = Coverage([p.bbox for p in ports.values()])
    print("airports:", build_airports(cover))
    print("highways:", build_roads(cover, groups))
    print("cities:  ", build_places(cover))
    print("ports:   ", build_ports(cover))
    refresh_reference(force=args.refresh)


if __name__ == "__main__":
    main()
