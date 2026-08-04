"""CCP-AI compression engine — lossless base + delta storage for model weights.

Standard library only, by design: the engine is the asset, and it has to be
auditable and runnable by a due-diligence reviewer with nothing installed.
"""

__version__ = "0.1.0"

from . import codec, metrics, pack, safetensors  # noqa: F401

__all__ = ["codec", "metrics", "pack", "safetensors", "__version__"]
