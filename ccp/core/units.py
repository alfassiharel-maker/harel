"""The unit: what the CCP Core operates on.

The Core is deliberately indifferent to what a unit *means*. A unit is an
identified byte string. Whether it is a file, a function body, a serialised
tensor, a configuration record or a block of instructions is the caller's
concern, expressed through `UnitSource`.

That indifference is the point. `CCP_Technical_Clarifications` §10 leaves the
unit of operation open -- instruction, function, basic block, trace, data
structure, object, module, whole program -- and says the level at which CCP pays
best is to be found, not assumed. Binding the Core to one level would foreclose
that question in code. Instead the level is chosen by whichever `UnitSource`
is used, and the same Core runs unchanged underneath.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, Mapping, Protocol

UNIT_DIGEST_BYTES = 32


def unit_digest(data: bytes) -> bytes:
    """Strong identity of a unit's content, used to verify every reconstruction."""
    return hashlib.blake2b(data, digest_size=UNIT_DIGEST_BYTES).digest()


@dataclass(frozen=True)
class Unit:
    """One addressable item of content."""

    uid: str
    data: bytes
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.uid:
            raise ValueError("a unit needs a non-empty uid")

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def digest(self) -> bytes:
        return unit_digest(self.data)


class UnitSource(Protocol):
    """Anything that can present content as units.

    This is the seam between the Core and the world. An integration that wants
    CCP over functions, tensors or database rows implements this and changes
    nothing else.
    """

    def units(self) -> Iterator[Unit]:
        ...


class InMemoryUnitSource:
    """Units held in memory. The source used by tests and by small callers."""

    def __init__(self, units: Dict[str, bytes] | None = None) -> None:
        self._units: Dict[str, bytes] = dict(units or {})

    def add(self, uid: str, data: bytes) -> None:
        self._units[uid] = data

    def units(self) -> Iterator[Unit]:
        # Sorted so a build is reproducible: base selection depends on the order
        # units arrive in, and an unordered walk would make the representation
        # differ between runs over identical input.
        for uid in sorted(self._units):
            yield Unit(uid=uid, data=self._units[uid])


class DirectoryUnitSource:
    """Every file under a directory is one unit, keyed by its relative path.

    The whole-file level is the coarsest useful unit and the one a project import
    reaches for first. It is one `UnitSource` among several, not the Core's
    definition of a unit.
    """

    def __init__(self, root: str, follow_symlinks: bool = False) -> None:
        self.root = os.path.abspath(root)
        self.follow_symlinks = follow_symlinks

    def units(self) -> Iterator[Unit]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if not self.follow_symlinks and os.path.islink(path):
                    continue
                if not os.path.isfile(path):
                    continue
                # Path traversal guard: a symlinked or otherwise odd entry must
                # not let a unit claim an identity outside the root.
                relative = os.path.relpath(os.path.realpath(path), self.root)
                if relative.startswith(os.pardir):
                    continue
                with open(path, "rb") as handle:
                    data = handle.read()
                yield Unit(
                    uid=os.path.relpath(path, self.root).replace(os.sep, "/"),
                    data=data,
                )
