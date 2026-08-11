"""CCP — Copy, Change, Paste.

Layering, enforced by import direction and by nothing being built above the line
that is not built yet:

    ccp.core          the CCP algorithm and its representation   [implemented]
    ccp.capabilities  work performed on that representation      [implemented]
    ccp.runtime       loading, integrity, selective access,      [implemented]
                      work accounting, under a stated contract
    ccp.api           the stable public interface                [implemented]
    ccp.product       artifact lifecycle, typed errors,          [implemented]
                      per-operation observability
    ccp.integration   project import, language analysis          [not built]
    ccp.ui            the commercial surface                     [not built]

External callers use `ccp.product` for artifact-oriented access, or `ccp.api`
for the lower-level runtime surface. Neither reaches into `ccp.core`.

`ccp.core` imports only the standard library. Layers that are not built are
absent from the tree rather than present and empty, so the code cannot be
mistaken for a working system with unfinished parts.
"""

from . import api, capabilities, core, product, runtime

__all__ = ["core", "capabilities", "runtime", "api", "product"]
__version__ = "0.1.0"
