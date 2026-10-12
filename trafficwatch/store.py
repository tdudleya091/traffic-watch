"""Timestamped position history with delayed, dead-reckoned snapshots.

Every observation is kept for a few minutes. The map asks for the picture at
T = now - delay; for each track we take the latest report at or before T and
project it forward along its course/speed to T. That keeps everything drawn
exactly `delay` seconds behind real time regardless of polling cadence.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .config import BBox

M_PER_DEG_LAT = 111_320.0


@dataclass(slots=True)
class Obs:
    t: float
    lat: float
    lon: float
    speed: float | None   # m/s
    course: float | None  # degrees true


class Track:
    __slots__ = ("obs", "meta")

    def __init__(self) -> None:
        self.obs: deque[Obs] = deque(maxlen=48)
        self.meta: dict = {}


def project(o: Obs, dt: float) -> tuple[float, float]:
    if not o.speed or o.course is None or dt <= 0:
        return o.lat, o.lon
    dist = o.speed * dt
    rad = math.radians(o.course)
    lat = o.lat + dist * math.cos(rad) / M_PER_DEG_LAT
    lon = o.lon + dist * math.sin(rad) / (M_PER_DEG_LAT * max(math.cos(math.radians(o.lat)), 0.05))
    return lat, lon


class TrackStore:
    def __init__(self, keep_s: float, max_age_s: float, max_extrap_s: float) -> None:
        self.keep_s = keep_s            # history retained per track
        self.max_age_s = max_age_s      # drop tracks whose last report is older than this at T
        self.max_extrap_s = max_extrap_s
        self.tracks: dict[str, Track] = {}

    def add(self, key: str, o: Obs, **meta) -> None:
        tr = self.tracks.get(key)
        if tr is None:
            tr = self.tracks[key] = Track()
        if tr.obs and o.t <= tr.obs[-1].t:
            if o.t < tr.obs[-1].t:
                return  # out-of-order; keep history monotonic
            tr.obs[-1] = o
        else:
            tr.obs.append(o)
        if meta:
            tr.meta.update(meta)

    def update_meta(self, key: str, **meta) -> None:
        tr = self.tracks.get(key)
        if tr is None:
            tr = self.tracks[key] = Track()
        tr.meta.update({k: v for k, v in meta.items() if v not in (None, "")})

    def snapshot(self, t_view: float, bbox: BBox) -> list[tuple[str, float, float, Obs, dict]]:
        out = []
        for key, tr in self.tracks.items():
            o = None
            for cand in reversed(tr.obs):
                if cand.t <= t_view:
                    o = cand
                    break
            if o is None:
                continue
            age = t_view - o.t
            if age > self.max_age_s:
                continue
            lat, lon = project(o, min(age, self.max_extrap_s))
            if bbox.contains(lat, lon):
                out.append((key, lat, lon, o, tr.meta))
        return out

    def prune(self, now: float) -> None:
        cutoff = now - self.keep_s
        dead = []
        for key, tr in self.tracks.items():
            while len(tr.obs) > 1 and tr.obs[1].t < cutoff:
                tr.obs.popleft()
            if not tr.obs or tr.obs[-1].t < cutoff:
                dead.append(key)
        for key in dead:
            del self.tracks[key]

    def __len__(self) -> int:
        return len(self.tracks)
