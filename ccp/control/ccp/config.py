"""Configuration for the control plane.

Resolution order is environment, then defaults; the CLI passes explicit values
where a user supplied them. Paths are resolved relative to the repository layout
rather than the current directory, so the CLI behaves the same wherever it is run
from.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# control/ccp/config.py -> control/ccp -> control -> ccp root
CCP_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_BLOCK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Config:
    """Where the engine lives and how it should be invoked."""

    engine_path: Path
    strategy_script: Path
    julia: str
    block_size: int = DEFAULT_BLOCK_SIZE

    @classmethod
    def discover(cls, block_size: int | None = None) -> "Config":
        engine = os.environ.get("CCP_ENGINE")
        if engine:
            engine_path = Path(engine)
        else:
            # The release build is what the Makefile produces; a debug build is
            # accepted as a fallback so a developer mid-iteration is not blocked.
            release = CCP_ROOT / "dataeng/target/release/ccp-engine"
            debug = CCP_ROOT / "dataeng/target/debug/ccp-engine"
            engine_path = release if release.exists() else debug

        return cls(
            engine_path=engine_path,
            strategy_script=Path(
                os.environ.get("CCP_STRATEGY_SCRIPT", CCP_ROOT / "strategy/bin/ccp_strategy.jl")
            ),
            julia=os.environ.get("CCP_JULIA", "julia"),
            block_size=block_size if block_size is not None else DEFAULT_BLOCK_SIZE,
        )

    def missing_components(self) -> list[str]:
        """Names what is not installed, so a failure reports the cause rather than
        surfacing as a subprocess error the user has to interpret."""
        missing = []
        if not self.engine_path.exists():
            missing.append(f"data engine binary ({self.engine_path}) — run `make build`")
        if not self.strategy_script.exists():
            missing.append(f"strategy engine script ({self.strategy_script})")
        if shutil.which(self.julia) is None:
            missing.append(
                f"julia interpreter ({self.julia!r} not on PATH) — the strategy engine "
                "is required; CCP has no fallback cost model by design"
            )
        return missing
