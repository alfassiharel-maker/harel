#!/usr/bin/env python3
"""Runs the CCP pipeline on generated artifacts and reports what it measured.

The artifacts are synthetic and *position-aligned by construction*, which is
exactly the assumption CCP rests on — so the numbers this prints demonstrate that
the machinery works, and say nothing about whether real checkpoints behave this
way. That question needs real files; see `../CLAUDE.md` §23.2.
"""

from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

CCP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CCP_ROOT / "control"))

from ccp import benchmark  # noqa: E402
from ccp.config import Config  # noqa: E402
from ccp.engine import Engine  # noqa: E402

SIZE = 4 * 1024 * 1024


def generate(directory: Path) -> tuple[list[Path], list[str]]:
    rng = random.Random(20260820)
    base = bytearray(rng.getrandbits(8) for _ in range(SIZE))

    paths, names = [], []
    current = base
    for step in range(4):
        if step > 0:
            # An in-place change: bytes move value, not position. This is what a
            # checkpoint whose tensor layout is fixed would look like.
            current = bytearray(current)
            for offset in range(step * 13, SIZE, 4096):
                current[offset] ^= (step * 91) % 256
        name = f"checkpoint-v{step + 1}"
        path = directory / f"{name}.bin"
        path.write_bytes(bytes(current))
        paths.append(path)
        names.append(name)

    # A version that shares nothing with its predecessor. The system must store it
    # FULL; if it chose a delta here it would be storing five times the data.
    unrelated = bytearray(rng.getrandbits(8) for _ in range(SIZE))
    path = directory / "unrelated.bin"
    path.write_bytes(bytes(unrelated))
    paths.append(path)
    names.append("unrelated")

    return paths, names


def main() -> int:
    config = Config.discover()
    missing = config.missing_components()
    if missing:
        print("CCP is not built:\n  - " + "\n  - ".join(missing), file=sys.stderr)
        return 1

    engine = Engine(config)
    with tempfile.TemporaryDirectory(prefix="ccp-demo-") as tmp:
        directory = Path(tmp)
        print(f"generating {len(range(5))} artifacts of {SIZE // 1024 // 1024} MiB each...")
        paths, names = generate(directory)

        result = benchmark.run(
            engine, directory / "repo", paths, names, block_size=256 * 1024,
            workdir=directory / "work",
        )

        from ccp.cli import _summarise

        _summarise("benchmark", result)

        print("\nreconstruction check (byte-for-byte):")
        for path, name in zip(paths, names):
            out = directory / f"{name}.check"
            engine.reconstruct(directory / "repo", name, out)
            identical = out.read_bytes() == path.read_bytes()
            print(f"  {name:<18} {'IDENTICAL' if identical else 'MISMATCH'}")
            if not identical:
                return 1
            out.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
