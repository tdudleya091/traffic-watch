"""Weather from aviationweather.gov (METAR, TAF, PIREP), plus the daily forecast from
api.weather.gov (US ports) or Open-Meteo (ports abroad, and US fallback).

Request volume: per 5 min, one METAR + one PIREP call per group plus one METAR
call for the panel stations; per 30 min, one TAF call and one forecast per
port. That is far under aviationweather.gov's 100 requests/minute limit and
Open-Meteo's 10,000 requests/day free tier.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import Group, Port

AWC = "https://aviationweather.gov/api/data"
NWS = "https://api.weather.gov"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
HEADERS = {"User-Agent": "traffic-watch/1.0 (personal desktop app)", "Accept": "application/json"}
M_TO_FT = 3.28084

# WMO weather interpretation codes used by Open-Meteo.
WMO = {
    0: "Sunny / clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Light freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Light freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light rain showers",
    81: "Rain showers", 82: "Violent rain showers", 85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Thunderstorms with heavy hail",
}

WX_CODES = {
    "TS": "thunderstorm", "SH": "showers", "FZ": "freezing", "BL": "blowing", "DR": "drifting",
    "MI": "shallow", "BC": "patches of", "PR": "partial",
    "DZ": "drizzle", "RA": "rain", "SN": "snow", "SG": "snow grains", "IC": "ice crystals",
    "PL": "ice pellets", "GR": "hail", "GS": "small hail", "UP": "unknown precip",
    "BR": "mist", "FG": "fog", "FU": "smoke", "VA": "volcanic ash", "DU": "dust", "SA": "sand",
    "HZ": "haze", "PY": "spray", "PO": "dust whirls", "SQ": "squalls", "FC": "funnel cloud",
    "SS": "sandstorm", "DS": "duststorm", "VC": "nearby",
}
COVER_WORDS = {"SKC": "Clear", "CLR": "Clear", "CAVOK": "Clear", "NSC": "Clear", "FEW": "Mostly clear",
               "SCT": "Partly cloudy", "BKN": "Mostly cloudy", "OVC": "Overcast", "OVX": "Sky obscured"}
CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def decode_wx(wx: str | None) -> str | None:
    if not wx:
        return None
    phrases = []
    for token in wx.split():
        intensity = "light " if token.startswith("-") else "heavy " if token.startswith("+") else ""
        codes = re.findall(r"[A-Z]{2}", token)
        words = " ".join(WX_CODES.get(c, c) for c in codes)
        phrases.append((intensity + words).strip())
    text = ", ".join(p for p in phrases if p)
    return text[:1].upper() + text[1:] if text else None


def vis_feet(visib) -> tuple[float | None, str]:
    """aviationweather visib is statute miles: a number, or a string like '10+' / '6+' / '1/2'."""
    if visib is None or visib == "":
        return None, "n/a"
    s = str(visib).strip()
    plus = s.endswith("+")
    s = s.rstrip("+").replace("P", "").replace("M", "")
    try:
        miles = sum(float(p.split("/")[0]) / float(p.split("/")[1]) if "/" in p else float(p) for p in s.split())
    except (ValueError, ZeroDivisionError):
        return None, str(visib)
    ft = miles * 5280
    return ft, f"{ft:,.0f}{'+' if plus else ''} ft"


def conditions(metar: dict) -> str:
    wx = decode_wx(metar.get("wxString"))
    if wx:
        return wx
    covers = [c.get("cover") for c in metar.get("clouds") or []]
    worst = next((c for c in ("OVX", "OVC", "BKN", "SCT", "FEW") if c in covers), metar.get("cover") or "CLR")
    return COVER_WORDS.get(worst, worst)


def wind_text(wdir, wspd, wgst=None) -> str:
    if wspd in (None, 0) and wdir in (None, 0):
        return "Calm"
    d = "Variable" if wdir in ("VRB", None) else f"{int(wdir):03d}° {CARDINALS[int((int(wdir) % 360) / 22.5 + 0.5) % 16]}"
    g = f" G{wgst}" if wgst else ""
    return f"{d} @ {wspd} kt{g}"


class WeatherService:
    def __init__(self, ports: dict[str, Port], groups: dict[str, Group],
                 every: tuple[float, float] = (300, 1800)) -> None:
        # (METAR/PIREP refresh, TAF/forecast refresh); no-key mode uses (3600, 10800)
        self.fast_every, self.slow_every = every
        self.ports = ports
        self.groups = groups
        self.metars_by_group: dict[str, list[dict]] = {}
        self.pireps_by_group: dict[str, list[dict]] = {}
        self.metar_by_station: dict[str, dict] = {}
        self.taf_by_station: dict[str, dict] = {}
        self.forecast_by_port: dict[str, dict] = {}
        self._forecast_urls: dict[str, str] = {}
        self.last_fast = 0.0
        self.last_slow = 0.0
        self.last_error: str | None = None

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            while True:
                now = time.time()
                if now - self.last_fast >= self.fast_every:
                    await self._fast(client)
                    if self.last_slow and now - self.last_slow < self.slow_every:
                        await self._forecasts(client, only_missing=True)
                    self.last_fast = time.time()
                if now - self.last_slow >= self.slow_every:
                    await self._slow(client)
                    self.last_slow = time.time()
                await asyncio.sleep(10)

    async def _get(self, client: httpx.AsyncClient, url: str, quiet: bool = False, **params):
        try:
            r = await client.get(url, params=params)
            if r.status_code == 204:
                return []
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            if not quiet:
                self.last_error = f"{url.rsplit('/', 1)[-1]}: {e.__class__.__name__}"
            return None

    async def _fast(self, client: httpx.AsyncClient) -> None:
        self.last_error = None
        for gid, g in self.groups.items():
            b = g.bbox
            bbox = f"{b.lamin:.3f},{b.lomin:.3f},{b.lamax:.3f},{b.lomax:.3f}"
            metars = await self._get(client, f"{AWC}/metar", bbox=bbox, format="json")
            if metars is not None:
                self.metars_by_group[gid] = metars
            pireps = await self._get(client, f"{AWC}/pirep", bbox=bbox, format="json", age=2)
            if pireps is not None:
                self.pireps_by_group[gid] = pireps
        stations = ",".join(sorted({p.station for p in self.ports.values()}))
        panel = await self._get(client, f"{AWC}/metar", ids=stations, format="json")
        for m in panel or []:
            self.metar_by_station[m["icaoId"]] = m

    async def _slow(self, client: httpx.AsyncClient) -> None:
        stations = ",".join(sorted({p.taf_station for p in self.ports.values()}))
        tafs = await self._get(client, f"{AWC}/taf", ids=stations, format="json")
        for t in tafs or []:
            self.taf_by_station[t["icaoId"]] = t
        await self._forecasts(client)

    async def _forecasts(self, client: httpx.AsyncClient, only_missing: bool = False) -> None:
        """Forecasts are stored as {"src", "periods": [{name, temp, wind, cond}], "min_vis_ft"}."""
        for pid, p in self.ports.items():
            if only_missing and pid in self.forecast_by_port:
                continue
            fc = await self._nws(client, p) if p.country == "US" else None
            if fc is None:
                fc = await self._open_meteo(client, p)
            elif p.taf_station not in self.taf_by_station:
                # NWS gives no visibility; without a TAF, take Open-Meteo's for the min-visibility line.
                om = await self._open_meteo(client, p)
                fc["min_vis_ft"] = om["min_vis_ft"] if om else None
            if fc is not None:
                self.forecast_by_port[pid] = fc

    async def _fetch_periods(self, client: httpx.AsyncClient, url: str, quiet: bool) -> list | None:
        fc = await self._get(client, url, quiet=quiet)
        return ((fc or {}).get("properties") or {}).get("periods") or None

    async def _nws(self, client: httpx.AsyncClient, p: Port) -> dict | None:
        url = self._forecast_urls.get(p.id)
        periods = await self._fetch_periods(client, url, quiet=True) if url else None
        if not periods:
            # Port points can fall outside the NWS land grid (Detroit River: no point;
            # Hampton Roads: a marine cell with no forecast), so fall back to the station.
            points = [(p.lat, p.lon)]
            m = self.metar_by_station.get(p.station)
            if m and m.get("lat") is not None:
                points.append((m["lat"], m["lon"]))
            for lat, lon in points:
                pt = await self._get(client, f"{NWS}/points/{lat:.4f},{lon:.4f}", quiet=True)
                url = ((pt or {}).get("properties") or {}).get("forecast")
                periods = await self._fetch_periods(client, url, quiet=True) if url else None
                if periods:
                    self._forecast_urls[p.id] = url
                    break
        if not periods:
            return None
        return {"src": "NWS", "min_vis_ft": None, "periods": [
            {"name": f.get("name"), "temp": f"{f.get('temperature')}°{f.get('temperatureUnit', 'F')}",
             "wind": f"{f.get('windDirection', '')} {f.get('windSpeed', '')}".strip(), "cond": f.get("shortForecast")}
            for f in periods[:2]]}

    async def _open_meteo(self, client: httpx.AsyncClient, p: Port) -> dict | None:
        d = await self._get(client, OPEN_METEO, latitude=f"{p.lat:.4f}", longitude=f"{p.lon:.4f}",
                            daily="temperature_2m_max,temperature_2m_min,weather_code,"
                                  "wind_speed_10m_max,wind_direction_10m_dominant",
                            hourly="visibility", timezone=p.tz, forecast_days=2, wind_speed_unit="kn")
        daily = (d or {}).get("daily")
        if not daily or not daily.get("time"):
            return None
        periods = []
        for i, name in enumerate(("Today", "Tomorrow")[:len(daily["time"])]):
            hi, lo = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
            wdir, wspd = daily["wind_direction_10m_dominant"][i], daily["wind_speed_10m_max"][i]
            periods.append({
                "name": name,
                "temp": (f"High {hi:.0f}°C ({hi * 9 / 5 + 32:.0f}°F) / low {lo:.0f}°C ({lo * 9 / 5 + 32:.0f}°F)"
                         if hi is not None and lo is not None else "n/a"),
                "wind": (f"{CARDINALS[int((wdir % 360) / 22.5 + 0.5) % 16]} up to {wspd:.0f} kt"
                         if wdir is not None and wspd is not None else "n/a"),
                "cond": WMO.get(daily["weather_code"][i], "n/a"),
            })
        # Lowest forecast visibility over the rest of today (port local time).
        now_local = datetime.now(ZoneInfo(p.tz)).strftime("%Y-%m-%dT%H:00")
        hourly = d.get("hourly") or {}
        vis = [v for t, v in zip(hourly.get("time", []), hourly.get("visibility", []))
               if v is not None and t.startswith(daily["time"][0]) and t >= now_local]
        return {"src": "Open-Meteo", "periods": periods, "min_vis_ft": min(vis) * M_TO_FT if vis else None}

    # ---- views served to the map ----
    def layer(self, port: Port) -> dict:
        metars = [
            {"id": m.get("icaoId"), "lat": m["lat"], "lon": m["lon"], "cat": m.get("fltCat"),
             "name": m.get("name"), "raw": m.get("rawOb"),
             "temp": m.get("temp"), "wind": wind_text(m.get("wdir"), m.get("wspd"), m.get("wgst")),
             "vis": vis_feet(m.get("visib"))[1], "cond": conditions(m)}
            for m in self.metars_by_group.get(port.group, [])
            if m.get("lat") is not None and port.bbox.contains(m["lat"], m["lon"])
        ]
        pireps = [
            {"lat": p["lat"], "lon": p["lon"], "raw": p.get("rawOb"), "urgent": p.get("pirepType") == "Urgent PIREP"
             or " UUA " in f" {p.get('rawOb', '')} ", "fl": p.get("fltLvl"), "ac": p.get("acType"),
             "turb": p.get("tbInt1"), "ice": p.get("icgInt1")}
            for p in self.pireps_by_group.get(port.group, [])
            if p.get("lat") is not None and port.bbox.contains(p["lat"], p["lon"])
        ]
        return {"metars": metars, "pireps": pireps}

    def panel(self, port: Port) -> dict:
        out: dict = {"station": port.station}
        m = self.metar_by_station.get(port.station)
        if m:
            t = m.get("temp")
            out["now"] = {
                "obs_time": m.get("obsTime"),
                "temp": f"{t * 9 / 5 + 32:.0f}°F ({t:.0f}°C)" if t is not None else "n/a",
                "wind": wind_text(m.get("wdir"), m.get("wspd"), m.get("wgst")),
                "cond": conditions(m),
                "vis": vis_feet(m.get("visib"))[1],
                "cat": m.get("fltCat"),
            }
        fc = self.forecast_by_port.get(port.id) or {}
        out["forecast"] = fc.get("periods", [])
        out["forecast_src"] = fc.get("src")
        taf = self.taf_by_station.get(port.taf_station)
        if taf:
            end_of_day = datetime.now(ZoneInfo(port.tz)).replace(hour=23, minute=59, second=59).timestamp()
            vis = [vis_feet(f.get("visib")) for f in taf.get("fcsts", [])
                   if f.get("timeFrom", 0) <= end_of_day and f.get("visib") not in (None, "")]
            vis = [v for v in vis if v[0] is not None]
            if vis:
                out["min_vis"], out["min_vis_src"] = min(vis)[1], "TAF"
        if "min_vis" not in out and fc.get("min_vis_ft") is not None:
            ft = fc["min_vis_ft"]
            # Same 10-statute-mile ceiling the METAR/TAF figures use ("10+" -> 52,800+ ft).
            out["min_vis"] = "52,800+ ft" if ft >= 52_800 else f"{ft:,.0f} ft"
            out["min_vis_src"] = "Open-Meteo"
        return out

    def status(self) -> dict:
        return {"last_update_age": round(time.time() - self.last_fast, 0) if self.last_fast else None,
                "error": self.last_error}
