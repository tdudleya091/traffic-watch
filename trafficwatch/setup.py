"""First-run setup: enter API keys (OpenSky, AISStream) or choose the no-key mode.

Keys are written to ~/.config/traffic-watch/keys.env (mode 600), never into the
app folder. Saving restarts the background services so the change takes effect
without relaunching the app.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import websockets
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import CONFIG_HOME, KEY_NAMES, KEYS_FILE, ROOT, load_settings, save_mode
from .opensky import TOKEN_URL

router = APIRouter()
STATIC = Path(__file__).parent / "static"
AIS_URL = "wss://stream.aisstream.io/v0/stream"
TEST_BOX = [[[33.0, -119.0], [34.5, -117.5]]]   # busy LA/Long Beach waters: a valid key gets data fast


class TestRequest(BaseModel):
    service: str                 # "opensky" | "ais"
    opensky_id: str = ""
    opensky_secret: str = ""
    ais_key: str = ""


class SaveRequest(BaseModel):
    opensky_mode: str            # "key" | "anonymous"
    opensky_id: str = ""
    opensky_secret: str = ""     # blank = keep the saved secret
    ais_mode: str                # "key" | "off"
    ais_key: str = ""            # blank = keep the saved key
    interval_min: int = 60


@router.get("/setup")
def setup_page():
    return FileResponse(STATIC / "setup.html", headers={"Cache-Control": "no-store"})


@router.get("/api/setup/state")
def setup_state():
    """What is configured now. Secrets are never sent back, only whether one is saved."""
    s = load_settings()
    return {
        "configured": s.configured,
        "opensky_mode": "anonymous" if s.anonymous else "key",
        "opensky_id": s.opensky_id,
        "opensky_secret_saved": bool(s.opensky_secret),
        "ais_mode": "key" if s.ships else "off",
        "ais_key_saved": bool(s.ais_key),
        "interval_min": int(s.lite_interval_s // 60),
        "keys_file": str(KEYS_FILE),
    }


async def _test_opensky(client_id: str, secret: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(TOKEN_URL, data={"grant_type": "client_credentials",
                                                   "client_id": client_id, "client_secret": secret})
    except httpx.HTTPError as e:
        return False, f"Could not reach OpenSky ({e.__class__.__name__}). Check your internet connection."
    if r.status_code == 200 and "access_token" in r.text:
        return True, "OpenSky accepted these credentials."
    return False, f"OpenSky rejected these credentials (HTTP {r.status_code}). Check the client ID and secret."


async def _test_ais(key: str) -> tuple[bool, str]:
    try:
        async with asyncio.timeout(12):
            async with websockets.connect(AIS_URL) as ws:
                await ws.send(json.dumps({"APIKey": key, "BoundingBoxes": TEST_BOX,
                                          "FilterMessageTypes": ["PositionReport"]}))
                while True:
                    msg = json.loads(await ws.recv())
                    if "error" in msg:
                        return False, f"AISStream rejected the key: {msg['error']}"
                    if msg.get("MetaData"):
                        return True, "AISStream accepted the key and sent live ship data."
    except TimeoutError:
        return False, "No ship data within 12 s. The key may be wrong, or AISStream is busy; try again."
    except websockets.ConnectionClosed:
        return False, "AISStream closed the connection, which usually means the key is not valid."
    except OSError as e:
        return False, f"Could not reach AISStream ({e.__class__.__name__}). Check your internet connection."


@router.post("/api/setup/test")
async def setup_test(req: TestRequest):
    saved = load_settings()
    if req.service == "opensky":
        ok, msg = await _test_opensky(req.opensky_id.strip(), req.opensky_secret.strip() or saved.opensky_secret)
    elif req.service == "ais":
        ok, msg = await _test_ais(req.ais_key.strip() or saved.ais_key)
    else:
        ok, msg = False, "unknown service"
    return {"ok": ok, "message": msg}


def _write_private(path: Path, text: str) -> None:
    CONFIG_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


@router.post("/api/setup/save")
async def setup_save(req: SaveRequest, request: Request):
    saved = load_settings()
    keys = {k: "" for k in KEY_NAMES}   # explicit empties switch a service off even if the repo .env has a key
    if req.opensky_mode == "key":
        keys["OPENSKY_CLIENT_ID"] = req.opensky_id.strip()
        keys["OPENSKY_CLIENT_SECRET"] = req.opensky_secret.strip() or saved.opensky_secret
        if not (keys["OPENSKY_CLIENT_ID"] and keys["OPENSKY_CLIENT_SECRET"]):
            return {"ok": False, "message": "Enter both the OpenSky client ID and secret, or choose No key."}
    if req.ais_mode == "key":
        keys["AISSTREAM_API_KEY"] = req.ais_key.strip() or saved.ais_key
        if not keys["AISSTREAM_API_KEY"]:
            return {"ok": False, "message": "Enter the AISStream API key, or choose Skip."}
    _write_private(KEYS_FILE, "# Written by Traffic Watch setup. Keep private.\n"
                   + "".join(f"{k}={_quote(v)}\n" for k, v in keys.items()))
    save_mode({"configured": True, "lite_interval_min": min(max(int(req.interval_min), 15), 120)})
    services = getattr(request.app.state, "services", None)
    if services is not None:
        await services.restart()
    return {"ok": True, "message": "Saved. Traffic Watch is now using these settings."}


# ---------------------------------------------------------------- background collector
# A systemd *user* service that runs `app.py --headless`, so history keeps recording while the
# window is closed. The window attaches to it when it is running (see app.py).
UNIT_NAME = "traffic-watch-collector.service"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
UNIT_FILE = UNIT_DIR / UNIT_NAME


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def collector_status() -> dict:
    if not shutil.which("systemctl"):
        return {"available": False, "enabled": False, "active": False}
    try:
        enabled = UNIT_FILE.exists() and _systemctl("is-enabled", UNIT_NAME).stdout.strip() == "enabled"
        active = _systemctl("is-active", UNIT_NAME).stdout.strip() in ("active", "activating")
        return {"available": True, "enabled": enabled, "active": active}
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "enabled": False, "active": False}


def _unit_text() -> str:
    # Carry test/second-install overrides (port, data folder, config folder) into the service.
    env = "".join(f"Environment={k}={os.environ[k]}\n" for k in
                  ("TRAFFIC_WATCH_PORT", "TRAFFIC_WATCH_STATE", "TRAFFIC_WATCH_CONFIG") if os.environ.get(k))
    return (
        "[Unit]\n"
        "Description=Traffic Watch background collector (records traffic history)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"WorkingDirectory={ROOT}\n"
        f'ExecStart="{sys.executable}" "{ROOT / "app.py"}" --headless\n'   # quoted: install paths may contain spaces
        f"{env}"
        # Exits at once if the app window's own server holds the port; retry until it is free.
        "Restart=on-failure\n"
        "RestartSec=30\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


class CollectorRequest(BaseModel):
    enabled: bool


@router.get("/api/collector")
def collector_get():
    return collector_status()


@router.post("/api/collector")
async def collector_set(req: CollectorRequest, request: Request):
    from .server import HEADLESS   # whether this very request is being served by the collector

    if not shutil.which("systemctl"):
        return {"ok": False, "message": "systemd is not available on this system.", **collector_status()}
    if req.enabled:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        UNIT_FILE.write_text(_unit_text())
        r = await asyncio.to_thread(lambda: (_systemctl("daemon-reload"), _systemctl("enable", "--now", UNIT_NAME))[1])
        if r.returncode != 0:
            return {"ok": False, "message": f"Could not enable the collector: {r.stderr.strip()[:200]}",
                    **collector_status()}
        return {"ok": True, "message": "Background recording is on. It keeps running after you close this window.",
                **collector_status()}

    def disable() -> None:
        # Stop last: when the collector serves this request, stopping kills this very process.
        _systemctl("disable", UNIT_NAME)
        UNIT_FILE.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        _systemctl("stop", UNIT_NAME)

    if HEADLESS:
        # This page is served by the collector itself: answer first, then stop it.
        threading.Timer(1.0, disable).start()
        return {"ok": True, "restart": True, "enabled": False, "available": True, "active": False,
                "message": "Background recording is off. Close and reopen Traffic Watch to keep using the map."}
    await asyncio.to_thread(disable)
    return {"ok": True, "message": "Background recording is off.", **collector_status()}
