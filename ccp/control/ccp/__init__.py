"""CCP control plane.

The application layer: CLI, orchestration, configuration, reporting. It drives the
Rust data engine and never touches stored bytes itself — there is deliberately no
bit processing, no block loop and no hashing of artifact content in this package.
Python's job is to decide *what* to run and to present the result.

The boundary to the engine is a subprocess speaking JSON (`engine.py`). A process
boundary rather than an FFI binding is a deliberate choice at this stage: it keeps
the engine's memory ownership entirely on the Rust side, makes a crash in the
engine a reportable error rather than an interpreter fault, and costs one process
launch per operation on work already measured in seconds.
"""

from .config import Config
from .engine import Engine, EngineError, IntegrityError

__all__ = ["Config", "Engine", "EngineError", "IntegrityError"]
__version__ = "0.1.0"
