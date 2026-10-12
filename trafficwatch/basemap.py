"""Builds one folium base map per port and caches it as HTML in data/maps/.

The base map holds everything static: tiles, coverage box, highway lines and
labels for airports, ports, highways and cities. It also carries three empty
toggleable layers (aircraft, vessels, weather) that static/live.js fills.
"""
from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import folium
from branca.element import Element, MacroElement
from jinja2 import Template

from . import VERSION
from .config import CONFIG_FILE, MAPS_DIR, REF_DIR, BBox, Port

CONUS_RADAR = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/{z}/{x}/{y}.png"
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
ESRI_ATTR = "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors"
MAX_CITY_LABELS = 22
CHART_JS = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js"
MAP_LAYERS = ("airports.json", "roads.json", "places.json", "ports.json")
EXPRESSWAY_RE = re.compile(r"E\d+[A-Z]?")   # Japanese expressway numbering (E1, E1A, ...)


def _load(name: str):
    return json.loads((REF_DIR / name).read_text())


def _label(text: str, cls: str) -> folium.DivIcon:
    return folium.DivIcon(html=f'<div class="tw-label {cls}">{html.escape(text)}</div>',
                          icon_size=(0, 0), icon_anchor=(0, 0), class_name="tw-label-wrap")


def _major_route(label: str) -> bool:
    return label.startswith(("I-", "US ")) or bool(EXPRESSWAY_RE.fullmatch(label))


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    dlat = (a[0] - b[0]) * 111.0
    dlon = (a[1] - b[1]) * 111.0 * math.cos(math.radians(a[0]))
    return math.hypot(dlat, dlon)


def _clip_runs(line: list[list[float]], box: BBox) -> list[list[tuple[float, float]]]:
    """Split a lon/lat polyline into runs of consecutive points inside `box` (as lat/lon)."""
    runs, cur = [], []
    for lon, lat in line:
        if box.contains(lat, lon):
            cur.append((lat, lon))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) > 1]


class LiveHook(MacroElement):
    """Hands the JS names of the map and the three live layers to live.js."""
    _template = Template("""
        {% macro script(this, kwargs) %}
        TW.init({
            map: {{ this._parent.get_name() }},
            aircraft: {{ this.aircraft }},
            vessels: {{ this.vessels }},
            weather: {{ this.weather }},
            radar: {{ this.radar }},
            port: {{ this.port_json }}
        });
        {% endmacro %}
    """)

    def __init__(self, aircraft: str, vessels: str, weather: str, radar: str, port_json: str) -> None:
        super().__init__()
        self._name = "LiveHook"
        self.aircraft, self.vessels, self.weather, self.port_json = aircraft, vessels, weather, port_json
        self.radar = radar  # JS name of the RainViewer group, or "null" where NEXRAD tiles are used


def build_port_map(port: Port, conus: bool) -> folium.Map:
    box = port.bbox
    clip = box.padded(0.4)
    m = folium.Map(location=[port.lat, port.lon], zoom_start=8, tiles=None, control_scale=True,
                   prefer_canvas=False, max_bounds=False)
    # CartoDB basemaps now watermark keyless requests, so use Esri's gray canvases.
    folium.TileLayer(ESRI.format("World_Dark_Gray_Base"), name="Dark", attr=ESRI_ATTR, max_zoom=16,
                     show=True).add_to(m)
    folium.TileLayer(ESRI.format("World_Light_Gray_Base"), name="Light", attr=ESRI_ATTR, max_zoom=16,
                     show=False).add_to(m)
    folium.TileLayer("OpenStreetMap", name="Street (OSM)", show=False).add_to(m)
    m.fit_bounds(box.leaflet())

    radar = None
    if conus:
        folium.TileLayer(CONUS_RADAR, name="Weather radar (NEXRAD)", attr="NEXRAD via Iowa Environmental Mesonet",
                         overlay=True, show=False, opacity=0.6).add_to(m)
    else:
        # RainViewer tile paths change every 10 min, so live.js fills this group at runtime.
        radar = folium.FeatureGroup(name="Weather radar (RainViewer)", show=False).add_to(m)

    cover = folium.FeatureGroup(name="200-mile coverage box", show=True).add_to(m)
    folium.Rectangle(box.leaflet(), color="#7aa2ff", weight=1.5, dash_array="6 6", fill=False,
                     interactive=False).add_to(cover)

    # Highways: lines plus up to two shields per route, spaced apart.
    roads = folium.FeatureGroup(name="Highways", show=True).add_to(m)
    shields: dict[str, list[tuple[float, float]]] = {}
    best_runs: dict[str, list[list[tuple[float, float]]]] = {}
    interstate_runs, other_runs = [], []
    for f in _load("roads.json"):
        for line in f["lines"]:
            for run in _clip_runs(line, clip):
                (interstate_runs if f["interstate"] else other_runs).append(run)
                best_runs.setdefault(f["label"], []).append(run)
    if other_runs:
        folium.PolyLine(other_runs, color="#b08a55", weight=1.3, opacity=0.7, interactive=False).add_to(roads)
    if interstate_runs:
        folium.PolyLine(interstate_runs, color="#e0a33a", weight=2.2, opacity=0.8, interactive=False).add_to(roads)
    # Minor routes (state routes, Japanese national routes) only get shields where there
    # are no Interstate/US routes or Japanese expressways (e.g. Alaska, Hawaii).
    major = [k for k in best_runs if _major_route(k)]
    for label, runs in best_runs.items():
        if major and not _major_route(label):
            continue
        for run in sorted(runs, key=len, reverse=True):
            mid = run[len(run) // 2]
            if not box.contains(*mid):
                continue
            placed = shields.setdefault(label, [])
            if len(placed) >= 2 or any(_km(mid, p) < 80 for p in placed):
                continue
            placed.append(mid)
            cls = ("tw-shield-i" if label.startswith("I-")
                   else "tw-shield-e" if EXPRESSWAY_RE.fullmatch(label) else "tw-shield-us")
            folium.Marker(mid, icon=_label(label, f"tw-shield {cls}"), interactive=False).add_to(roads)

    cities = folium.FeatureGroup(name="Cities", show=True).add_to(m)
    places = sorted((c for c in _load("places.json") if box.contains(c["lat"], c["lon"])),
                    key=lambda c: -c["pop"])[:MAX_CITY_LABELS]
    for i, c in enumerate(places):
        cls = "tw-city-1" if i < 4 or c["pop"] > 1_000_000 else "tw-city-2"
        folium.CircleMarker([c["lat"], c["lon"]], radius=2.5, color="#ddd", fill=True, fill_opacity=1,
                            weight=0, interactive=False).add_to(cities)
        folium.Marker([c["lat"], c["lon"]], icon=_label(c["name"], f"tw-city {cls}"),
                      interactive=False, z_index_offset=1000).add_to(cities)

    airports = folium.FeatureGroup(name="Airports", show=True).add_to(m)
    for a in _load("airports.json"):
        if not box.contains(a["lat"], a["lon"]):
            continue
        code = a["iata"] or a["icao"]
        folium.Marker(
            [a["lat"], a["lon"]],
            icon=folium.DivIcon(html=f'<div class="tw-apt {"tw-apt-major" if a["major"] else ""}">'
                                     f'<span class="tw-apt-dot">✈</span>{html.escape(code)}</div>',
                                icon_size=(0, 0), icon_anchor=(7, 9), class_name="tw-label-wrap"),
            tooltip=f'{a["name"]} ({a["icao"]})',
        ).add_to(airports)

    harbors = folium.FeatureGroup(name="Ports", show=True).add_to(m)
    seen: list[tuple[float, float]] = []
    named = [(h["name"], h["lat"], h["lon"], True) for h in port.harbors]
    named += [(p["name"], p["lat"], p["lon"], False) for p in _load("ports.json")]
    for name, lat, lon, major in named:
        if not box.contains(lat, lon) or any(_km((lat, lon), s) < 8 for s in seen):
            continue
        seen.append((lat, lon))
        folium.Marker(
            [lat, lon],
            icon=folium.DivIcon(html=f'<div class="tw-port {"tw-port-major" if major else ""}">'
                                     f'<span class="tw-port-dot">⚓</span>{html.escape(name)}</div>',
                                icon_size=(0, 0), icon_anchor=(7, 9), class_name="tw-label-wrap"),
            tooltip=name,
        ).add_to(harbors)

    aircraft = folium.FeatureGroup(name="✈ Aviation traffic", show=True).add_to(m)
    vessels = folium.FeatureGroup(name="⚓ Maritime traffic", show=True).add_to(m)
    weather = folium.FeatureGroup(name="☁ Weather stations & PIREPs", show=True).add_to(m)

    folium.LayerControl(position="bottomright", collapsed=False).add_to(m)

    root = m.get_root()
    root.header.add_child(Element('<link rel="stylesheet" href="/static/live.css">'))
    root.html.add_child(Element(PANEL_HTML))
    root.html.add_child(Element(f'<script src="{CHART_JS}"></script>'))
    root.html.add_child(Element('<script src="/static/live.js"></script>'))
    root.html.add_child(Element('<script src="/static/dashboard.js"></script>'))
    # Browsers name some zones only as "GMT+9"; give the page the proper abbreviation (JST) as a fallback.
    tz_abbr = datetime.now(ZoneInfo(port.tz)).tzname() or ""
    port_json = json.dumps({"id": port.id, "name": port.name, "tz": port.tz, "station": port.station,
                            "tz_abbr": tz_abbr if tz_abbr.isalpha() else "",
                            "bbox": box.leaflet(), "radar": "nexrad" if conus else "rainviewer"})
    m.add_child(LiveHook(aircraft.get_name(), vessels.get_name(), weather.get_name(),
                         radar.get_name() if radar else "null", port_json))
    return m


PANEL_HTML = f"""
<div id="tw-portbar">
  <span class="tw-title" title="Traffic Watch v{VERSION}">Traffic Watch</span>
  <select id="tw-port" aria-label="Port"></select>
</div>
<div id="tw-panel">
  <div id="tw-clock"><span id="tw-local">--:--:--</span> <span id="tw-utc">(--:--:-- UTC)</span></div>
  <div id="tw-sub" class="tw-muted"></div>
  <div id="tw-mode" hidden></div>
  <div id="tw-wx"><div class="tw-muted">Loading weather…</div></div>
  <div id="tw-counts"></div>
</div>
<div id="tw-bl">
  <div id="tw-dash" class="tw-collapsed"></div>
  <div id="tw-legend"></div>
  <div id="tw-status" class="tw-muted"></div>
</div>
"""


def _stale(path: Path) -> bool:
    if not path.exists():
        return True
    deps = [CONFIG_FILE, Path(__file__), *(REF_DIR / n for n in MAP_LAYERS)]
    return any(d.stat().st_mtime > path.stat().st_mtime for d in deps)


def ensure_maps(ports: dict[str, Port], force: bool = False) -> None:
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    for p in ports.values():
        out = MAPS_DIR / f"{p.id}.html"
        if force or _stale(out):
            conus = 24 <= p.lat <= 50 and -125 <= p.lon <= -66
            print(f"[basemap] building {p.id} ...")
            build_port_map(p, conus).save(str(out))
