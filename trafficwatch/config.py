"""Port/group configuration, bounding-box math and settings (API keys, update mode)."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REF_DIR = DATA / "ref"
MAPS_DIR = DATA / "maps"
# Runtime state (history, OpenSky usage). Overridable so tests or a second install don't share it.
STATE_DIR = Path(os.environ["TRAFFIC_WATCH_STATE"]) if os.environ.get("TRAFFIC_WATCH_STATE") else DATA / "state"
# Port of the background collector; the app window looks for a running collector here.
COLLECTOR_PORT = int(os.environ.get("TRAFFIC_WATCH_PORT") or 8765)
CONFIG_FILE = ROOT / "config" / "ports.yaml"

# Per-user settings live outside the app folder, so keys never sit next to code that gets shared.
CONFIG_HOME = Path(os.environ.get("TRAFFIC_WATCH_CONFIG")
                   or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "traffic-watch")
KEYS_FILE = CONFIG_HOME / "keys.env"
MODE_FILE = CONFIG_HOME / "mode.json"
KEY_NAMES = ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "AISSTREAM_API_KEY")

MILES_PER_DEG_LAT = 69.0


@dataclass(frozen=True)
class BBox:
    lamin: float
    lomin: float
    lamax: float
    lomax: float

    @property
    def area(self) -> float:
        return (self.lamax - self.lamin) * (self.lomax - self.lomin)

    def contains(self, lat: float, lon: float) -> bool:
        return self.lamin <= lat <= self.lamax and self.lomin <= lon <= self.lomax

    def union(self, other: "BBox") -> "BBox":
        return BBox(min(self.lamin, other.lamin), min(self.lomin, other.lomin),
                    max(self.lamax, other.lamax), max(self.lomax, other.lomax))

    def padded(self, deg: float) -> "BBox":
        return BBox(self.lamin - deg, self.lomin - deg, self.lamax + deg, self.lomax + deg)

    def leaflet(self) -> list[list[float]]:
        return [[self.lamin, self.lomin], [self.lamax, self.lomax]]


def square_bbox(lat: float, lon: float, half_side_miles: float) -> BBox:
    dlat = half_side_miles / MILES_PER_DEG_LAT
    dlon = half_side_miles / (MILES_PER_DEG_LAT * math.cos(math.radians(lat)))
    return BBox(lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def opensky_credit_cost(area_sq_deg: float) -> int:
    """OpenSky /states/all credit cost by bounding-box area."""
    if area_sq_deg <= 25:
        return 1
    if area_sq_deg <= 100:
        return 2
    if area_sq_deg <= 400:
        return 3
    return 4


@dataclass
class Port:
    id: str
    name: str
    lat: float
    lon: float
    tz: str
    station: str
    group: str
    bbox: BBox
    harbors: list[dict] = field(default_factory=list)
    region: str = "Other"
    taf_station: str = ""   # nearby airport for the TAF when `station` issues none
    country: str = "US"     # outside the US the forecast comes from Open-Meteo instead of NWS


@dataclass
class Group:
    id: str
    ports: list[str]
    bbox: BBox

    @property
    def cost(self) -> int:
        return opensky_credit_cost(self.bbox.area)


@dataclass(frozen=True)
class Settings:
    ais_key: str
    opensky_id: str
    opensky_secret: str
    opensky_daily_credits: int
    delay_s: float
    configured: bool            # keys found or a setup choice saved; otherwise the app opens /setup
    lite_interval_s: float      # anonymous OpenSky: seconds between snapshots of the area on screen

    @property
    def anonymous(self) -> bool:
        """No OpenSky API client: keyless mode with hourly-ish aircraft snapshots."""
        return not (self.opensky_id and self.opensky_secret)

    @property
    def ships(self) -> bool:
        return bool(self.ais_key)

    @property
    def weather_every(self) -> tuple[float, float]:
        """(METAR/PIREP refresh, TAF/forecast refresh) in seconds."""
        return (3600, 10800) if self.anonymous else (300, 1800)


def _merged_env() -> dict[str, str]:
    """Repo .env (developer setup) < ~/.config/traffic-watch/keys.env (setup page) < environment.

    A key present but empty in keys.env deliberately switches that service off."""
    merged: dict[str, str] = {}
    for path in (ROOT / ".env", KEYS_FILE):
        if path.exists():
            merged.update({k: (v or "") for k, v in dotenv_values(path).items()})
    merged.update({k: v for k, v in os.environ.items() if k in KEY_NAMES or k.startswith(("OPENSKY_", "DATA_"))})
    return merged


def load_mode() -> dict:
    try:
        return json.loads(MODE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_mode(updates: dict) -> None:
    """Merge into ~/.config/traffic-watch/mode.json (private to the user)."""
    mode = load_mode()
    mode.update(updates)
    CONFIG_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(MODE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(mode))


def load_settings() -> Settings:
    env = _merged_env()
    delay = float(env.get("DATA_DELAY_SECONDS") or 90)
    mode = load_mode()
    keys_found = any(env.get(k) for k in KEY_NAMES)
    return Settings(
        ais_key=env.get("AISSTREAM_API_KEY", ""),
        opensky_id=env.get("OPENSKY_CLIENT_ID", ""),
        opensky_secret=env.get("OPENSKY_CLIENT_SECRET", ""),
        opensky_daily_credits=int(env.get("OPENSKY_DAILY_CREDITS") or 4000),
        delay_s=min(max(delay, 60.0), 120.0),
        configured=keys_found or bool(mode.get("configured")),
        lite_interval_s=60 * min(max(int(mode.get("lite_interval_min", 60)), 15), 120),
    )


def load_ports() -> tuple[dict[str, Port], dict[str, Group]]:
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    half = cfg.get("defaults", {}).get("half_side_miles", 100)
    default_country = cfg.get("defaults", {}).get("country", "US")
    region_of = {g: region for region, gs in (cfg.get("regions") or {}).items() for g in gs}
    ports: dict[str, Port] = {}
    groups: dict[str, Group] = {}
    for p in cfg["ports"]:
        bbox = square_bbox(p["lat"], p["lon"], p.get("half_side_miles", half))
        group = p.get("group", p["id"])
        port = Port(p["id"], p["name"], p["lat"], p["lon"], p["tz"], p["station"],
                    group, bbox, p.get("harbors", []), region_of.get(group, "Other"),
                    p.get("taf_station", p["station"]), p.get("country", default_country))
        ports[port.id] = port
        g = groups.get(port.group)
        if g is None:
            groups[port.group] = Group(port.group, [port.id], bbox)
        else:
            g.ports.append(port.id)
            g.bbox = g.bbox.union(bbox)
    # Dropdown follows the order of the `regions:` list; unlisted ports go last.
    order = {region: i for i, region in enumerate(cfg.get("regions") or {})}
    ports = dict(sorted(ports.items(), key=lambda kv: order.get(kv[1].region, len(order))))
    for g in groups.values():
        if g.cost > 1:
            print(f"[config] warning: group '{g.id}' covers {g.bbox.area:.1f} sq deg "
                  f"and costs {g.cost} OpenSky credits per poll; consider splitting it")
    return ports, groups
