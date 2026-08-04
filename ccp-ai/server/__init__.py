"""HTTP layer for the CCP-AI console. Transport only — no compression logic."""

from .app import Api, build_server

__all__ = ["Api", "build_server"]
