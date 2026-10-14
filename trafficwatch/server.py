"""Local FastAPI server: serves the cached base maps, the delayed live data and the dashboard history."""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import VERSION
from .ais import AISClient
from .basemap import ensure_maps
from .config import MAPS_DIR, Port, load_mode, load_ports, load_settings, save_mode
from .enrich import aircraft_info, refresh_reference, vessel_info
from .history import AIS_WARMUP_S, SAMPLE_EVERY, History
from .opensky import OpenSkyPoller
from .setup import collector_status, router as setup_router
from .store import TrackStore
from .weather import WeatherService

M_TO_FT = 3.28084
MPS_TO_KT = 1.943844

ports, groups = load_ports()
history = History()
HEADLESS = os.environ.get("TW_HEADLESS") == "1"   # set by `app.py --headless` (the background collector)


class Services:
    """Everything that depends on the API keys / mode. Rebuilt in place when setup is saved."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []
        self._build()

    def _build(self) -> None:
        s = self.settings = load_settings()
        if s.anonymous:
            # Hourly snapshots: keep each plane for the whole interval and don't project it forward.
            self.aircraft = TrackStore(keep_s=s.lite_interval_s + 900, max_age_s=s.lite_interval_s + 300,
                                       max_extrap_s=0)
        else:
            self.aircraft = TrackStore(keep_s=600, max_age_s=300, max_extrap_s=180)
        self.vessels = TrackStore(keep_s=1800, max_age_s=900, max_extrap_s=600)
        self.opensky = OpenSkyPoller(groups, self.aircraft, s)
        self.opensky.on_poll = self._sample_group
        # With no window open (collector), keep polling the port you last looked at.
        last = load_mode().get("last_port")
        if last in ports:
            self.opensky.active_group = ports[last].group
        self.ais = AISClient(groups, self.vessels, s)
        self.weather = WeatherService(ports, groups, s.weather_every)

    def start(self) -> None:
        coros = [self.opensky.run(), self.weather.run(), self._prune(), self._sampler()]
        if self.settings.ships:
            coros.append(self.ais.run())
        self.tasks = [asyncio.create_task(c) for c in coros]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def restart(self) -> None:
        active = self.opensky.active_group
        await self.stop()
        self._build()
        self.opensky.active_group = active or self.opensky.active_group
        self.start()

    async def _prune(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            self.aircraft.prune(now)
            self.vessels.prune(now)

    # ---- dashboard history ----
    async def _sampler(self) -> None:
        """Ship counts every 10 min, once the AIS feed has been connected for 15 min
        (before that the picture is still filling in and would show false dips)."""
        last = 0.0
        while True:
            await asyncio.sleep(30)
            now = time.time()
            since = self.ais.connected_since
            if not self.settings.ships or since is None or now - since < AIS_WARMUP_S or now - last < SAMPLE_EVERY:
                continue
            for p in ports.values():
                snap = self.vessels.snapshot(now, p.bbox)
                moving = sum(1 for *_, meta in snap if (meta.get("sog") or 0) >= 0.5)
                history.record_vessels(p.id, now, len(snap), moving)
            history.commit()
            last = now

    def _sample_group(self, group_id: str) -> None:
        """Right after an area is polled its aircraft count is exact, so record it then
        (at most once per port per 10 min)."""
        now = time.time()
        for p in ports.values():
            if p.group == group_id:
                history.record_aircraft(p.id, now, len(self.aircraft.snapshot(now, p.bbox)))
        history.commit()


services = Services()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(ensure_maps, ports)
    services.start()
    app.state.services = services
    # Aircraft/airline/flag/type lookups refresh in the background when over 30 days old; never blocks startup.
    refresh = asyncio.create_task(asyncio.to_thread(refresh_reference))
    yield
    refresh.cancel()
    await services.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(setup_router)


def _port(port_id: str) -> Port:
    p = ports.get(port_id)
    if p is None:
        raise HTTPException(404, f"unknown port {port_id}")
    return p


@app.get("/")
def index():
    if not services.settings.configured:
        return RedirectResponse("/setup")
    return RedirectResponse(f"/port/{next(iter(ports))}")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(Path(__file__).parent.parent / "assets" / "traffic-watch.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return {"ok": True, "app": "traffic-watch", "version": VERSION, "headless": HEADLESS}


@app.get("/api/config")
def config():
    s = services.settings
    return {"version": VERSION, "configured": s.configured, "anonymous": s.anonymous, "ships": s.ships,
            "interval_s": s.lite_interval_s if s.anonymous else None,
            "collector": {"serving": HEADLESS, **collector_status()}}


@app.get("/api/ports")
def list_ports():
    return [{"id": p.id, "name": p.name, "region": p.region} for p in ports.values()]


@app.get("/port/{port_id}")
def port_page(port_id: str):
    p = _port(port_id)
    services.opensky.active_group = p.group
    if load_mode().get("last_port") != p.id:
        save_mode({"last_port": p.id})
    return FileResponse(MAPS_DIR / f"{p.id}.html", headers={"Cache-Control": "no-store"})


def _r(x, nd=0):
    return None if x is None else round(x, nd) if nd else int(round(x))


def aircraft_items(p: Port, t_view: float) -> list[dict]:
    out = []
    for key, lat, lon, o, meta in services.aircraft.snapshot(t_view, p.bbox):
        alt = meta.get("alt_baro") if meta.get("alt_baro") is not None else meta.get("alt_geo")
        out.append({
            "id": key, "cs": meta.get("callsign") or key.upper(), "lat": round(lat, 5), "lon": round(lon, 5),
            "hdg": _r(o.course), "alt": _r(alt * M_TO_FT) if alt is not None else None,
            "gs": _r(meta["velocity"] * MPS_TO_KT) if meta.get("velocity") is not None else None,
            "vr": _r(meta["vrate"] * M_TO_FT * 60) if meta.get("vrate") is not None else None,
            "sq": meta.get("squawk"), "gnd": bool(meta.get("on_ground")), "cty": meta.get("country"),
            "cat": meta.get("category"), "age": _r(t_view - o.t),
            **aircraft_info(key, meta.get("callsign"), meta.get("category")),
        })
    return out


def vessel_items(p: Port, t_view: float) -> list[dict]:
    out = []
    for key, lat, lon, o, meta in services.vessels.snapshot(t_view, p.bbox):
        out.append({
            "id": key, "name": meta.get("name") or f"MMSI {key}", "lat": round(lat, 5), "lon": round(lon, 5),
            "cog": meta.get("cog"), "hdg": meta.get("heading"), "sog": meta.get("sog"),
            "type": meta.get("ship_type"), "dest": meta.get("destination"), "cs": meta.get("callsign"),
            "len": meta.get("length"), "draught": meta.get("draught"), "nav": meta.get("nav_status"),
            "imo": meta.get("imo"), "cls": meta.get("cls"), "age": _r(t_view - o.t),
            **vessel_info(key, meta.get("ship_type"), meta.get("name")),
        })
    return out


# Data endpoints are async so they run on the event loop, the same thread that updates the stores.
@app.get("/api/live/{port_id}")
async def live(port_id: str):
    p = _port(port_id)
    s = services.settings
    services.opensky.active_group = p.group
    now = time.time()
    t_view = now - s.delay_s
    last = services.opensky.last_poll.get(p.group) or 0
    # No-key snapshots are already old by nature; show the whole snapshot at once (the banner gives its time).
    ac_view = now if s.anonymous else t_view
    return {
        # No-key mode has no live feed to delay, so its clock shows plain local time.
        "server_now": now, "t_view": t_view, "delay": 0 if s.anonymous else s.delay_s,
        "mode": {"anonymous": s.anonymous, "ships": s.ships, "snapshot_ts": last or None,
                 "interval_s": s.lite_interval_s if s.anonymous else None},
        "aircraft": aircraft_items(p, ac_view),
        "vessels": vessel_items(p, t_view) if s.ships else [],
        "status": {
            "opensky": {**services.opensky.status(), "group_age": _r(now - last) if last else None},
            "ais": {**services.ais.status(), "enabled": s.ships},
            "wx": services.weather.status(),
        },
    }


@app.get("/api/history/{port_id}")
async def port_history(port_id: str, hours: float = 24):
    p = _port(port_id)
    return {"port": p.id, "hours": hours, **history.query(p.id, min(max(hours, 1), 168))}


@app.get("/api/wx/{port_id}")
async def wx(port_id: str):
    p = _port(port_id)
    return {"panel": services.weather.panel(p), **services.weather.layer(p)}
