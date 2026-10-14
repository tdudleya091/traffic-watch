"""AISStream.io websocket client: one subscription covering every group box."""
from __future__ import annotations

import asyncio
import json
import time

import websockets

from .config import Group, Settings
from .store import Obs, TrackStore

URL = "wss://stream.aisstream.io/v0/stream"
KNOT = 0.514444
MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                 "ShipStaticData", "StaticDataReport"]


class AISClient:
    def __init__(self, groups: dict[str, Group], store: TrackStore, settings: Settings) -> None:
        self.groups = groups
        self.store = store
        self.settings = settings
        self.connected = False
        self.connected_since: float | None = None   # start of the current connection (history warm-up)
        self.msg_count = 0
        self.last_msg = 0.0
        self.last_error: str | None = None
        self._rate_window: list[float] = []

    def _subscription(self) -> str:
        boxes = [[[g.bbox.lamin, g.bbox.lomin], [g.bbox.lamax, g.bbox.lomax]] for g in self.groups.values()]
        return json.dumps({"APIKey": self.settings.ais_key, "BoundingBoxes": boxes,
                           "FilterMessageTypes": MESSAGE_TYPES})

    async def run(self) -> None:
        if not self.settings.ais_key:
            self.last_error = "no AISSTREAM_API_KEY in .env"
            return
        backoff = 2
        while True:
            try:
                async with websockets.connect(URL, ping_interval=20, max_size=2**22) as ws:
                    await ws.send(self._subscription())
                    self.connected, self.last_error, backoff = True, None, 2
                    self.connected_since = time.time()
                    async for raw in ws:
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network drops, server closes, bad key
                self.last_error = f"{e.__class__.__name__}: {e}"[:160]
            self.connected, self.connected_since = False, None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _handle(self, raw: str | bytes) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if "error" in m:
            self.last_error = str(m["error"])[:160]
            return
        mtype = m.get("MessageType")
        meta = m.get("MetaData") or {}
        body = (m.get("Message") or {}).get(mtype) or {}
        mmsi = str(meta.get("MMSI") or body.get("UserID") or "")
        if not mmsi:
            return
        now = time.time()
        self.msg_count += 1
        self.last_msg = now
        self._rate_window.append(now)
        name = (meta.get("ShipName") or "").strip()

        if mtype in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
            lat, lon = body.get("Latitude"), body.get("Longitude")
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
                return
            sog = body.get("Sog")
            cog = body.get("Cog")
            hdg = body.get("TrueHeading")
            sog = None if sog is None or sog >= 102.2 else sog
            cog = None if cog is None or cog >= 360 else cog
            hdg = None if hdg is None or hdg >= 360 else hdg
            moving = sog is not None and sog >= 0.5
            self.store.add(mmsi, Obs(now, lat, lon, sog * KNOT if moving else None, cog),
                           sog=sog, cog=cog, heading=hdg, nav_status=body.get("NavigationalStatus"),
                           cls="A" if mtype == "PositionReport" else "B")
            if name:
                self.store.update_meta(mmsi, name=name)
        elif mtype == "ShipStaticData":
            dim = body.get("Dimension") or {}
            self.store.update_meta(
                mmsi, name=(body.get("Name") or name).strip(), ship_type=body.get("Type"),
                callsign=(body.get("CallSign") or "").strip(),
                destination=(body.get("Destination") or "").strip(),
                imo=body.get("ImoNumber") or None,
                length=(dim.get("A") or 0) + (dim.get("B") or 0) or None,
                draught=body.get("MaximumStaticDraught") or None,
            )
        elif mtype == "StaticDataReport":
            a = body.get("ReportA") or {}
            b = body.get("ReportB") or {}
            dim = b.get("Dimension") or {}
            self.store.update_meta(
                mmsi, name=(a.get("Name") or name).strip() if a.get("Valid") else name,
                ship_type=b.get("ShipType") if b.get("Valid") else None,
                callsign=(b.get("CallSign") or "").strip() if b.get("Valid") else None,
                length=((dim.get("A") or 0) + (dim.get("B") or 0) or None) if b.get("Valid") else None,
            )

    def status(self) -> dict:
        now = time.time()
        self._rate_window = [t for t in self._rate_window if now - t < 60]
        return {"connected": self.connected, "msgs_per_min": len(self._rate_window),
                "last_msg_age": round(now - self.last_msg, 1) if self.last_msg else None,
                "error": self.last_error}
