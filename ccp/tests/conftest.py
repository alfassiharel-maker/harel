"""Fixtures for the CCP integration suite.

These tests drive the real stack end to end: Python orchestration, the Rust data
engine, the C++ bit engine linked into it, and the Julia strategy engine as a
subprocess. Nothing is mocked — a test that stubbed the engine would prove only
that the stub works, and the property under test (byte-identical reconstruction)
is exactly the one a stub cannot establish.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

CCP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CCP_ROOT / "control"))

from ccp.config import Config  # noqa: E402
from ccp.engine import Engine  # noqa: E402


@pytest.fixture(scope="session")
def config() -> Config:
    cfg = Config.discover()
    missing = cfg.missing_components()
    if missing:
        pytest.skip("CCP is not built: " + "; ".join(missing))
    return cfg


@pytest.fixture
def engine(config: Config) -> Engine:
    return Engine(config)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path / "repo"


def write_artifact(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def pseudo_random_bytes(size: int, seed: int) -> bytes:
    """Deterministic filler.

    A fixed seed rather than `os.urandom` so a failure is reproducible from the
    test alone — an intermittent failure in a byte-exactness test would be nearly
    impossible to diagnose otherwise.
    """
    return random.Random(seed).randbytes(size)


@pytest.fixture
def artifacts(tmp_path: Path):
    """A version series shaped like the workload CCP targets.

    `v1` is the base. `v2` changes a small number of bytes in place, which is the
    position-aligned, sparse case the design bets on. `v3` does the same again, to
    exercise a two-hop chain. `v4` is unrelated noise, which must be stored FULL —
    the case that proves the system measures rather than assumes.
    """
    size = 512 * 1024
    v1 = bytearray(pseudo_random_bytes(size, seed=1))

    v2 = bytearray(v1)
    for offset in range(0, size, 8192):
        v2[offset] ^= 0x5A

    v3 = bytearray(v2)
    for offset in range(64, size, 16384):
        v3[offset] ^= 0x11

    v4 = bytearray(pseudo_random_bytes(size, seed=2))

    return {
        "v1": write_artifact(tmp_path / "v1.bin", bytes(v1)),
        "v2": write_artifact(tmp_path / "v2.bin", bytes(v2)),
        "v3": write_artifact(tmp_path / "v3.bin", bytes(v3)),
        "v4": write_artifact(tmp_path / "v4.bin", bytes(v4)),
    }
