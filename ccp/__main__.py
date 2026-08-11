"""`python -m ccp` — launch the CCP Forge application.

The application is the default thing this package does. The developer-facing
command line stays available at `python -m ccp.cli`.
"""

import sys

from .ui.launcher import main

if __name__ == "__main__":
    sys.exit(main())
