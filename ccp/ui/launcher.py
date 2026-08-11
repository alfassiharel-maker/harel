"""Launching CCP Forge: start the local application host and open its window.

This is the entry point a packaged executable runs. It starts the server, opens
the system browser at the application URL, and stays alive until interrupted.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from typing import Optional, Sequence

from .. import __version__
from .controller import AppController
from .server import AppServer

BANNER = r"""
   ___ ___ ___   ___
  / __/ __| _ \ | __|__ _ _ __ _ ___
 | (_| (__|  _/ | _/ _ \ '_/ _` / -_)
  \___\___|_|   |_|\___/_| \__, \___|
                           |___/
"""


def launch(
    open_browser: bool = True,
    port: int = 0,
    controller: Optional[AppController] = None,
) -> AppServer:
    """Start the application host and return it. Does not block."""
    server = AppServer(controller=controller, port=port)
    server.start()
    if open_browser:
        # Opening a browser can block for a moment on some desktops; do it off
        # the main thread so the application is responsive immediately.
        threading.Thread(
            target=lambda: webbrowser.open(server.url), daemon=True
        ).start()
    return server


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccp-forge", description="CCP Forge — build and explore CCP artifacts"
    )
    parser.add_argument("--port", type=int, default=0, help="port (0 picks a free one)")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    parser.add_argument("--version", action="version", version=f"CCP Forge {__version__}")
    args = parser.parse_args(argv)

    # flush on every line: when the app is packaged and its output is redirected
    # (a log file, a launcher, a service wrapper) Python block-buffers stdout, and
    # a user would not see the URL they need until the process exited.
    def say(text: str = "") -> None:
        print(text, flush=True)

    say(BANNER)
    say(f"  CCP Forge {__version__}")
    server = launch(open_browser=not args.no_browser, port=args.port)
    say(f"  Application running at: {server.url}")
    say("  Keep this window open while you use CCP Forge. Press Ctrl+C to quit.\n")
    try:
        while True:
            # Sleep in the main thread; the server runs on its own.
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        say("\n  Shutting down…")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
