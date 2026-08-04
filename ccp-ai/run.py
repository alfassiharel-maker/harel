#!/usr/bin/env python3
"""Start the CCP-AI console.

    python3 ccp-ai/run.py            # http://127.0.0.1:8420
    python3 ccp-ai/run.py --port 9000 --store /tmp/ccp-store

No virtualenv, no packages. If `python3 --version` works, this works.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.app import build_server  # noqa: E402

DEFAULT_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".store")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CCP-AI console — lossless model delta storage")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--store", default=DEFAULT_STORE, help="where model bytes live")
    args = parser.parse_args(argv)

    token = os.environ.get("CCP_API_TOKEN") or None
    httpd = build_server(args.host, args.port, args.store, token=token)

    print(f"CCP-AI console   http://{args.host}:{args.port}")
    print(f"store            {os.path.realpath(args.store)}")
    print(f"auth             {'bearer token required (CCP_API_TOKEN)' if token else 'open (loopback demo)'}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # Binding wide with no token would expose a write API that can consume
        # disk. Say so loudly rather than quietly doing it.
        print(
            "WARNING: bound to a non-loopback address."
            + ("" if token else " Set CCP_API_TOKEN before exposing this beyond localhost.")
        )
    print("Ctrl-C to stop.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
