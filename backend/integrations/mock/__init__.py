"""Deterministic synthetic provider, used to develop and test the ingest pipeline
without depending on Garmin approval. See `adapter.py` for why it is a real adapter
rather than a stub.
"""

from backend.integrations.mock.adapter import MockAdapter, synthesise_activity, synthesise_wellness

__all__ = ["MockAdapter", "synthesise_activity", "synthesise_wellness"]
