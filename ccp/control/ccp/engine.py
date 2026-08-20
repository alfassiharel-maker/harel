"""The boundary to the Rust data engine.

Every call is a subprocess invocation that returns parsed JSON. The engine
distinguishes error classes by exit code and by an `error` field, and this module
preserves that distinction: an integrity failure must never be caught by a
`except EngineError` that also swallows "file not found".
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import Config


class EngineError(RuntimeError):
    """The engine reported a failure. `kind` carries its classification."""

    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


class IntegrityError(EngineError):
    """Stored data did not verify.

    Its own type because it means something categorically different from every
    other error: the bytes on disk are wrong. Nothing in this codebase may
    recover from it or retry past it.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="integrity")


# The engine returns this specifically for an integrity failure, so the control
# plane can react to it without matching on message text.
INTEGRITY_EXIT_CODE = 3


class Engine:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.discover()

    def _run(self, args: list[str]) -> dict[str, Any]:
        missing = self.config.missing_components()
        if missing:
            raise EngineError(
                "CCP is not fully built:\n  - " + "\n  - ".join(missing), kind="not_built"
            )

        env = dict(os.environ)
        env["CCP_STRATEGY_SCRIPT"] = str(self.config.strategy_script)
        env["CCP_JULIA"] = self.config.julia

        proc = subprocess.run(
            [str(self.config.engine_path), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        if proc.returncode != 0:
            kind, message = "unknown", proc.stderr.strip() or proc.stdout.strip()
            # The engine reports errors as JSON on stderr; fall back to raw text
            # if it died before it could (a signal, for instance).
            try:
                payload = json.loads(proc.stderr)
                kind = payload.get("error", kind)
                message = payload.get("message", message)
            except (json.JSONDecodeError, ValueError):
                pass
            if proc.returncode == INTEGRITY_EXIT_CODE or kind == "integrity":
                raise IntegrityError(message)
            raise EngineError(message, kind=kind)

        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise EngineError(
                f"engine returned output that is not JSON: {exc}\n{proc.stdout[:500]}",
                kind="protocol",
            ) from exc

    # ---- operations ----------------------------------------------------

    def info(self) -> dict[str, Any]:
        return self._run(["engine-info"])

    def store(
        self,
        repo: Path,
        artifact: Path,
        name: str,
        base: str | None = None,
        block_size: int | None = None,
    ) -> dict[str, Any]:
        args = [
            "store",
            "--repo",
            str(repo),
            "--file",
            str(artifact),
            "--name",
            name,
            "--block-size",
            str(block_size or self.config.block_size),
        ]
        if base:
            args += ["--base", base]
        return self._run(args)

    def reconstruct(self, repo: Path, version: str, out: Path) -> dict[str, Any]:
        return self._run(
            ["reconstruct", "--repo", str(repo), "--version", version, "--out", str(out)]
        )

    def list_versions(self, repo: Path) -> dict[str, Any]:
        return self._run(["list", "--repo", str(repo)])

    def inspect(self, repo: Path, version: str) -> dict[str, Any]:
        return self._run(["inspect", "--repo", str(repo), "--version", version])

    def verify(self, repo: Path, version: str | None = None) -> dict[str, Any]:
        args = ["verify", "--repo", str(repo)]
        if version:
            args += ["--version", version]
        return self._run(args)
