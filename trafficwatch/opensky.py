"""OpenSky Network poller with a daily credit budget.

One /states/all bounding-box call per group. Credits refill continuously at
(90% of the daily allowance) / 86400 per second, so a full day of running never
exceeds the free tier. The group on screen is polled first and most often;
the others are kept warm with whatever budget is left.

Without an API client (anonymous / no-key mode) OpenSky allows 400 credits a
day, so only the group on screen is fetched, once per `lite_interval_s`
(default hourly), and polling stops when the server's remaining count runs low.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from .config import STATE_DIR, Group, Settings
from .store import Obs, TrackStore

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
STATES_URL = "https://opensky-network.org/api/states/all"

ACTIVE_MIN_INTERVAL = 40   # s between polls of the group on screen
OTHER_MIN_INTERVAL = 90    # s between polls of background groups
BUCKET_CAPACITY = 6        # credits that may be spent in a burst (e.g. at startup)
ACTIVE_RESERVE = 1         # credits background polls must leave for the group on screen
ANONYMOUS_DAILY = 400      # OpenSky's allowance without an account
ANONYMOUS_FLOOR = 5        # stop anonymous polling when the server reports this few credits left
USAGE_FILE = STATE_DIR / "opensky_usage.json"


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class OpenSkyPoller:
    def __init__(self, groups: dict[str, Group], store: TrackStore, settings: Settings) -> None:
        self.groups = groups
        self.store = store
        self.settings = settings
        self.active_group: str | None = None
        self.last_poll: dict[str, float] = {g: 0.0 for g in groups}
        self.last_count: dict[str, int] = {g: 0 for g in groups}
        self.anonymous = settings.anonymous
        self.daily_credits = ANONYMOUS_DAILY if self.anonymous else settings.opensky_daily_credits
        self.rate = self.daily_credits * 0.9 / 86400.0
        self.on_poll = None   # callback(group_id) after each successful fetch (history sampling)
        self.tokens = float(BUCKET_CAPACITY)
        self.used_today, self.usage_date = self._load_usage()
        self.remaining_header: int | None = None
        self.blocked_until = 0.0
        self.last_error: str | None = None
        self._token: str | None = None
        self._token_exp = 0.0

    # ---- persistence so restarts don't double-spend the daily allowance ----
    def _load_usage(self) -> tuple[int, str]:
        try:
            d = json.loads(USAGE_FILE.read_text())
            if d.get("date") == _utc_date():
                return int(d["used"]), d["date"]
        except (OSError, ValueError, KeyError):
            pass
        return 0, _utc_date()

    def _save_usage(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps({"date": self.usage_date, "used": self.used_today}))

    async def _auth(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = await client.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.settings.opensky_id,
            "client_secret": self.settings.opensky_secret,
        })
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = time.time() + float(d.get("expires_in", 1800))
        return self._token

    def _pick_group(self, now: float) -> Group | None:
        """The group on screen whenever it is due; otherwise the stalest background group.
        Anonymous mode only ever fetches the group on screen, once per lite interval."""
        active = self.groups.get(self.active_group or "")
        if self.anonymous:
            due = active and now - self.last_poll[active.id] >= self.settings.lite_interval_s
            return active if due else None
        if active and now - self.last_poll[active.id] >= ACTIVE_MIN_INTERVAL:
            return active
        due = [g for gid, g in self.groups.items()
               if gid != self.active_group and now - self.last_poll[gid] >= OTHER_MIN_INTERVAL]
        return min(due, key=lambda g: self.last_poll[g.id], default=None)

    async def run(self) -> None:
        last = time.monotonic()
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                await asyncio.sleep(1)
                mono = time.monotonic()
                self.tokens = min(BUCKET_CAPACITY, self.tokens + (mono - last) * self.rate)
                last = mono
                if self.usage_date != _utc_date():
                    self.used_today, self.usage_date = 0, _utc_date()
                now = time.time()
                if now < self.blocked_until:
                    continue
                g = self._pick_group(now)
                if g is None:
                    continue
                if self.anonymous:
                    # The interval is the budget (24/day hourly); also trust the server's own count.
                    if self.remaining_header is not None and self.remaining_header <= ANONYMOUS_FLOOR:
                        self.last_error = "anonymous daily limit nearly used; resumes after midnight UTC"
                        continue
                else:
                    # Background polls leave a credit in reserve so switching ports refreshes at once.
                    reserve = 0 if g.id == self.active_group else ACTIVE_RESERVE
                    if self.tokens < g.cost + reserve:
                        continue
                if self.used_today + g.cost > self.daily_credits:
                    self.last_error = "daily credit allowance used up"
                    continue
                await self._poll(client, g)

    async def _poll(self, client: httpx.AsyncClient, g: Group) -> None:
        b = g.bbox
        params = {"lamin": b.lamin, "lomin": b.lomin, "lamax": b.lamax, "lomax": b.lomax, "extended": 1}
        try:
            headers = {} if self.anonymous else {"Authorization": f"Bearer {await self._auth(client)}"}
            r = await client.get(STATES_URL, params=params, headers=headers)
        except httpx.HTTPError as e:
            self.last_error = f"network: {e.__class__.__name__}"
            # retry in ~15 s (anonymous: in 2 min, to stay gentle on the shared allowance)
            retry_in = 120 if self.anonymous else 15
            interval = self.settings.lite_interval_s if self.anonymous else OTHER_MIN_INTERVAL
            self.last_poll[g.id] = time.time() - interval + retry_in
            return
        self.last_poll[g.id] = time.time()
        if "X-Rate-Limit-Remaining" in r.headers:
            self.remaining_header = int(r.headers["X-Rate-Limit-Remaining"])
        if r.status_code == 429:
            wait = float(r.headers.get("X-Rate-Limit-Retry-After-Seconds", 600))
            self.blocked_until = time.time() + wait
            self.last_error = f"rate limited by OpenSky; pausing {int(wait)} s"
            return
        if r.status_code == 401:
            self._token = None
            self.last_error = "auth rejected; refreshing token"
            return
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}"
            return
        self.tokens -= g.cost
        self.used_today += g.cost
        self._save_usage()
        self.last_error = None
        states = r.json().get("states") or []
        self.last_count[g.id] = len(states)
        for s in states:
            lon, lat = s[5], s[6]
            if lat is None or lon is None:
                continue
            t = s[3] or s[4]
            self.store.add(
                s[0],
                Obs(float(t), lat, lon, None if s[8] else s[9], s[10]),
                callsign=(s[1] or "").strip(),
                country=s[2],
                alt_baro=s[7],
                alt_geo=s[13],
                on_ground=s[8],
                velocity=s[9],
                vrate=s[11],
                squawk=s[14],
                category=s[17] if len(s) > 17 else None,
            )
        if self.on_poll:
            self.on_poll(g.id)

    def status(self) -> dict:
        return {
            "anonymous": self.anonymous,
            "interval_s": self.settings.lite_interval_s if self.anonymous else ACTIVE_MIN_INTERVAL,
            "credits_used_today": self.used_today,
            "credits_daily": self.daily_credits,
            "credits_remaining_reported": self.remaining_header,
            "last_poll": self.last_poll,
            "last_count": self.last_count,
            "error": self.last_error,
        }
