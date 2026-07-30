"""Garmin Health + Activity API adapter.

Every endpoint, scope and payload key lives in `constants.py` and must be verified
against the Garmin Developer Program documentation before build (`docs/13` §8).
"""

from backend.integrations.garmin.adapter import GarminAdapter

__all__ = ["GarminAdapter"]
