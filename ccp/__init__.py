"""CCP — Copy, Change, Paste.

Layering, enforced by import direction and by nothing being built above the line
that is not built yet:

    ccp.core          the CCP algorithm and its representation   [implemented]
    ccp.capabilities  work performed on that representation      [implemented]
    ccp.runtime       execution strategy under a contract        [not built]
    ccp.integration   project import, language analysis          [not built]
    ccp.product       build orchestration, packaging             [not built]
    ccp.ui            the commercial surface                     [not built]

`ccp.core` imports only the standard library. Layers that are not built are
absent from the tree rather than present and empty, so the code cannot be
mistaken for a working system with unfinished parts.
"""

from . import capabilities, core

__all__ = ["core", "capabilities"]
__version__ = "0.1.0"
