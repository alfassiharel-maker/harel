"""Entry point so `python -m backend.squeeze ...` works with nothing installed."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
