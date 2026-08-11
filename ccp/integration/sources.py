"""Input adapters and analysis for real user input.

The Core already defines what a unit is and how a directory becomes units
(`DirectoryUnitSource`), reached here through the public API rather than by
importing the Core directly. This module adds what a product needs on top:

*   **analysis** — look at an input and report what it contains *before*
    committing to a build, so the application can show the user what it is about
    to do and refuse an input that exceeds the supported resource model;
*   **a ZIP adapter** — the input the product direction names first ("Drop
    Project ZIP"), implemented as one more `UnitSource` so the Core is unchanged.

Both adapters treat the input as untrusted. A zip entry may name any path it
likes, including `../../etc/passwd`, and may claim to be small while expanding to
gigabytes. Entries that try either are skipped with a recorded reason rather than
silently dropped or blindly honoured.
"""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from typing import Iterator, List, Tuple

from ..api import DirectoryUnitSource, Unit
from .errors import InputError

# Directory entries the product never treats as project content. Including a
# version-control directory would bury the real files under thousands of objects
# and make the analysis meaningless.
EXCLUDED_DIRECTORIES = frozenset({".git", ".hg", ".svn", "__pycache__", ".venv", "venv"})


@dataclass(frozen=True)
class InputLimits:
    """The resource envelope an input must fit in to be accepted.

    Stated as data, and checked before a build rather than discovered during one:
    an input that cannot be completed safely is refused with a reason, not
    started and then killed by the allocator.
    """

    max_units: int = 200_000
    max_total_bytes: int = 2 << 30      # 2 GiB of input content
    max_unit_bytes: int = 512 << 20     # one unit that must fit in memory
    max_archive_ratio: float = 200.0    # decompressed / compressed, zip-bomb guard

    def describe(self) -> str:
        return (
            f"at most {self.max_units} units, {self.max_total_bytes} total bytes, "
            f"{self.max_unit_bytes} bytes per unit"
        )


@dataclass(frozen=True)
class InputAnalysis:
    """What an input contains, measured without building anything."""

    kind: str                       # "directory" or "zip"
    path: str
    unit_count: int
    total_bytes: int
    largest_unit_bytes: int
    skipped: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def average_unit_bytes(self) -> float:
        """None-safe average: 0.0 units means no meaningful average."""
        return self.total_bytes / self.unit_count if self.unit_count else 0.0


def _is_excluded(relative: str) -> bool:
    return any(part in EXCLUDED_DIRECTORIES for part in relative.split("/"))


# --------------------------------------------------------------------------
# directories
# --------------------------------------------------------------------------


class ProjectDirectorySource:
    """A directory as units, excluding version-control and cache directories.

    Wraps `DirectoryUnitSource` rather than replacing it: the walk, the symlink
    handling and the traversal guard stay in the Core, and this adds only the
    product's exclusion policy.
    """

    def __init__(self, root: str, limits: InputLimits = InputLimits()) -> None:
        self.root = os.path.abspath(root)
        self.limits = limits
        self._inner = DirectoryUnitSource(self.root)

    def units(self) -> Iterator[Unit]:
        seen = 0
        total = 0
        for unit in self._inner.units():
            if _is_excluded(unit.uid):
                continue
            seen += 1
            total += unit.size
            if seen > self.limits.max_units:
                raise InputError(
                    f"input has more than {self.limits.max_units} files, which is "
                    f"above the supported limit"
                )
            if unit.size > self.limits.max_unit_bytes:
                raise InputError(
                    f"{unit.uid!r} is {unit.size} bytes, above the "
                    f"{self.limits.max_unit_bytes}-byte per-file limit"
                )
            if total > self.limits.max_total_bytes:
                raise InputError(
                    f"input exceeds the {self.limits.max_total_bytes}-byte total limit"
                )
            yield unit


def analyse_directory(path: str, limits: InputLimits = InputLimits()) -> InputAnalysis:
    """Count and size a directory without reading file contents into memory."""
    root = os.path.abspath(path)
    if not os.path.isdir(root):
        raise InputError(f"not a directory: {path}")

    count = 0
    total = 0
    largest = 0
    skipped: List[Tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRECTORIES)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            relative = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                skipped.append((relative, "symbolic link"))
                continue
            if not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError as error:
                skipped.append((relative, f"unreadable: {error.strerror or error}"))
                continue
            count += 1
            total += size
            largest = max(largest, size)

    _check_analysis(count, total, largest, limits, path)
    return InputAnalysis(
        kind="directory",
        path=root,
        unit_count=count,
        total_bytes=total,
        largest_unit_bytes=largest,
        skipped=tuple(skipped),
    )


# --------------------------------------------------------------------------
# zip archives
# --------------------------------------------------------------------------


def _zip_entry_rejection(info: zipfile.ZipInfo) -> str:
    """Why this entry must not be trusted, or "" if it is fine.

    A zip is untrusted input. An entry may name an absolute path, escape the
    extraction root with `..`, or be a symlink pointing anywhere on the machine.
    None of those are extracted here — units are held in memory, never written to
    disk by this module — but a malicious name would still become a *unit id*,
    and a unit id is later used to build export paths. Rejecting at the source is
    the only place that reliably covers every downstream use.
    """
    name = info.filename
    if info.is_dir():
        return "directory entry"
    if not name:
        return "empty name"
    if name.startswith("/") or name.startswith("\\"):
        return "absolute path"
    if len(name) > 1 and name[1] == ":":
        return "drive-qualified path"
    normalised = name.replace("\\", "/")
    parts = normalised.split("/")
    if any(part == ".." for part in parts):
        return "path traversal"
    # Unix mode lives in the high 16 bits of external_attr; 0xA000 is S_IFLNK.
    if (info.external_attr >> 16) & 0xF000 == 0xA000:
        return "symbolic link"
    return ""


class ZipProjectSource:
    """A zip archive as units, one per file entry.

    Entries are decompressed one at a time, so peak memory is bounded by the
    largest single entry rather than by the archive.
    """

    def __init__(self, path: str, limits: InputLimits = InputLimits()) -> None:
        self.path = os.path.abspath(path)
        self.limits = limits

    def units(self) -> Iterator[Unit]:
        try:
            with zipfile.ZipFile(self.path) as archive:
                total = 0
                count = 0
                for info in sorted(archive.infolist(), key=lambda i: i.filename):
                    if _zip_entry_rejection(info):
                        continue
                    if _is_excluded(info.filename.replace("\\", "/")):
                        continue
                    if info.file_size > self.limits.max_unit_bytes:
                        raise InputError(
                            f"{info.filename!r} expands to {info.file_size} bytes, "
                            f"above the {self.limits.max_unit_bytes}-byte per-file limit"
                        )
                    count += 1
                    total += info.file_size
                    if count > self.limits.max_units:
                        raise InputError(
                            f"archive holds more than {self.limits.max_units} files"
                        )
                    if total > self.limits.max_total_bytes:
                        raise InputError(
                            f"archive expands beyond the "
                            f"{self.limits.max_total_bytes}-byte total limit"
                        )
                    with archive.open(info) as handle:
                        data = handle.read()
                    # The declared size is metadata and can lie; what was actually
                    # read is the truth, so the limit is enforced again on it.
                    if len(data) > self.limits.max_unit_bytes:
                        raise InputError(
                            f"{info.filename!r} read {len(data)} bytes, above the "
                            f"per-file limit"
                        )
                    yield Unit(uid=info.filename.replace("\\", "/"), data=data)
        except zipfile.BadZipFile as error:
            raise InputError(f"not a readable zip archive: {error}") from error
        except OSError as error:
            raise InputError(f"could not read {self.path}: {error}") from error


def analyse_zip(path: str, limits: InputLimits = InputLimits()) -> InputAnalysis:
    """Read a zip's central directory to report its contents, decompressing nothing."""
    full = os.path.abspath(path)
    if not os.path.isfile(full):
        raise InputError(f"not a file: {path}")
    try:
        with zipfile.ZipFile(full) as archive:
            count = 0
            total = 0
            largest = 0
            compressed = 0
            skipped: List[Tuple[str, str]] = []
            for info in sorted(archive.infolist(), key=lambda i: i.filename):
                reason = _zip_entry_rejection(info)
                if reason:
                    if reason != "directory entry":
                        skipped.append((info.filename, reason))
                    continue
                if _is_excluded(info.filename.replace("\\", "/")):
                    continue
                count += 1
                total += info.file_size
                compressed += info.compress_size
                largest = max(largest, info.file_size)
    except zipfile.BadZipFile as error:
        raise InputError(f"not a readable zip archive: {error}") from error
    except OSError as error:
        raise InputError(f"could not read {path}: {error}") from error

    # Zip-bomb guard: a tiny archive claiming an enormous expansion is refused
    # before a single entry is decompressed.
    if compressed > 0 and total / compressed > limits.max_archive_ratio:
        raise InputError(
            f"archive expands {total / compressed:.0f}x, above the "
            f"{limits.max_archive_ratio:.0f}x limit; refusing to open it"
        )
    _check_analysis(count, total, largest, limits, path)
    return InputAnalysis(
        kind="zip",
        path=full,
        unit_count=count,
        total_bytes=total,
        largest_unit_bytes=largest,
        skipped=tuple(skipped),
    )


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def _check_analysis(
    count: int, total: int, largest: int, limits: InputLimits, path: str
) -> None:
    if count == 0:
        raise InputError(f"no usable files found in {path}")
    if count > limits.max_units:
        raise InputError(
            f"{path} holds {count} files, above the supported limit of {limits.max_units}"
        )
    if total > limits.max_total_bytes:
        raise InputError(
            f"{path} holds {total} bytes, above the supported limit of "
            f"{limits.max_total_bytes}"
        )
    if largest > limits.max_unit_bytes:
        raise InputError(
            f"{path} contains a {largest}-byte file, above the per-file limit of "
            f"{limits.max_unit_bytes}"
        )


def analyse_input(path: str, limits: InputLimits = InputLimits()) -> InputAnalysis:
    """Analyse a directory or a zip archive, chosen by what the path actually is."""
    if os.path.isdir(path):
        return analyse_directory(path, limits)
    if os.path.isfile(path):
        if zipfile.is_zipfile(path):
            return analyse_zip(path, limits)
        raise InputError(
            f"{os.path.basename(path)} is a file but not a zip archive; "
            f"choose a project folder or a .zip"
        )
    raise InputError(f"no such file or folder: {path}")


def source_for(path: str, limits: InputLimits = InputLimits()):
    """The `UnitSource` for an input path."""
    if os.path.isdir(path):
        return ProjectDirectorySource(path, limits)
    if os.path.isfile(path) and zipfile.is_zipfile(path):
        return ZipProjectSource(path, limits)
    raise InputError(f"unsupported input: {path}")
