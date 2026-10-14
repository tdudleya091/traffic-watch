"""Traffic Watch desktop app: starts the local server and opens it in a native window.

    python app.py              # native window (GTK WebKit)
    python app.py --browser    # use the default web browser instead
    python app.py --headless   # background collector: server only, no window (run by the systemd service)
    python app.py --rebuild    # force-rebuild every base map first

If a background collector is already running, the window opens on it instead of
starting a second server, so only one process ever polls the APIs and writes history.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

import httpx
import uvicorn

from trafficwatch.config import COLLECTOR_PORT


def bind_socket(preferred: int, fallback: bool) -> socket.socket | None:
    """Bind the listening socket ourselves, before the app starts: uvicorn runs the app's
    startup (pollers, history) before binding, so a lost port race would otherwise poll twice."""
    for port in ([preferred, 0] if fallback else [preferred]):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return s
        except OSError:
            s.close()
    return None


def running_collector(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.5)
        return r.status_code == 200 and r.json().get("app") == "traffic-watch"
    except (httpx.HTTPError, ValueError):
        return False


def wait_ready(url: str, timeout: float = 90) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url + "/api/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise SystemExit("server did not start")


def make_server(sock: socket.socket) -> uvicorn.Server:
    from trafficwatch.server import app   # imported only once we own a socket (it opens the history DB)
    return uvicorn.Server(uvicorn.Config(app, log_level="warning"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Traffic Watch")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--headless", action="store_true", help="run the background collector (no window)")
    ap.add_argument("--port", type=int, default=COLLECTOR_PORT)
    ap.add_argument("--rebuild", action="store_true", help="rebuild all base maps")
    args = ap.parse_args()

    if args.rebuild:
        from trafficwatch.basemap import ensure_maps
        from trafficwatch.config import load_ports
        ensure_maps(load_ports()[0], force=True)

    if args.headless:
        sock = bind_socket(args.port, fallback=False)
        if sock is None:
            # The app window's own server (or another collector) has the port; systemd retries in 30 s.
            print(f"port {args.port} is in use; not starting a second collector", file=sys.stderr)
            sys.exit(1)
        os.environ["TW_HEADLESS"] = "1"
        make_server(sock).run(sockets=[sock])
        return

    server = thread = None
    if running_collector(args.port):
        url = f"http://127.0.0.1:{args.port}"   # attach to the background collector
    else:
        sock = bind_socket(args.port, fallback=True)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = make_server(sock)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
    wait_ready(url)

    if args.browser:
        webbrowser.open(url)
        print(f"Traffic Watch running at {url} (Ctrl+C to quit)")
        try:
            while thread is None or thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        import webview
        from trafficwatch import VERSION
        # "More info" links in the detail popups open in the system browser, not in the app window.
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.create_window(f"Traffic Watch {VERSION}", url, width=1440, height=900, min_size=(800, 560),
                              resizable=True, zoomable=True, background_color="#0b0e14")
        webview.start(gui="gtk")
    if server is not None:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
