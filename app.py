"""Traffic Watch desktop app: starts the local server and opens it in a native window.

    python app.py              # native window (GTK WebKit)
    python app.py --browser    # use the default web browser instead
    python app.py --rebuild    # force-rebuild every base map first
"""
from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser

import httpx
import uvicorn


def free_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Traffic Watch")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--rebuild", action="store_true", help="rebuild all base maps")
    args = ap.parse_args()

    if args.rebuild:
        from trafficwatch.basemap import ensure_maps
        from trafficwatch.config import load_ports
        ensure_maps(load_ports()[0], force=True)

    from trafficwatch.server import app

    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_ready(url)

    if args.browser:
        webbrowser.open(url)
        print(f"Traffic Watch running at {url} (Ctrl+C to quit)")
        try:
            thread.join()
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
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
