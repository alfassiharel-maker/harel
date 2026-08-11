"""Where the application keeps things, and how it writes them safely.

Two responsibilities, both of which have to be right before the product touches a
user's disk:

*   **predictable locations** — application data goes to the platform's
    conventional per-user directory, never next to the executable and never to a
    developer's absolute path;
*   **safe writing** — every write goes to a temporary file in the destination
    directory and is then atomically renamed into place, so an interrupted write
    cannot leave a truncated artifact where a good one used to be, and an
    existing file is never silently overwritten unless the caller says so.

Export paths are built from unit ids, and a unit id is untrusted: it may come
from a zip entry that names `../../.bashrc`. `safe_export_path` is the single
place that resolves such a name against its destination and refuses anything that
escapes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional

from .errors import ExportError

APP_NAME = "CCPForge"
RECENTS_FILENAME = "recent.json"
MAX_RECENTS = 12


def app_data_dir() -> str:
    """The per-user directory this application owns.

    Windows      %LOCALAPPDATA%\\CCPForge
    macOS        ~/Library/Application Support/CCPForge
    other        $XDG_DATA_HOME/CCPForge or ~/.local/share/CCPForge
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
    return os.path.join(base, APP_NAME)


def default_output_dir() -> str:
    """Where artifacts are saved unless the user chooses somewhere else."""
    return os.path.join(app_data_dir(), "artifacts")


def ensure_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as error:
        raise ExportError(f"could not create {path}: {error}", cause=error) from error
    return path


def atomic_write(path: str, data: bytes, overwrite: bool = False) -> str:
    """Write `data` to `path` atomically. Refuses to clobber unless asked.

    The temporary file is created in the destination directory so the final
    rename stays on one filesystem and is therefore atomic; a rename across
    filesystems is a copy, and a copy can be interrupted half-way.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(directory)
    if os.path.exists(path) and not overwrite:
        raise ExportError(
            f"{path} already exists; choose another name or allow overwriting"
        )

    handle = None
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix=".ccp-", suffix=".part", dir=directory
        )
        handle = os.fdopen(fd, "wb")
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temp_path, path)
        temp_path = None
        return path
    except OSError as error:
        raise ExportError(f"could not write {path}: {error}", cause=error) from error
    finally:
        if handle is not None:
            handle.close()
        # A failed write leaves no debris behind.
        if temp_path is not None and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                # Cleanup of a temp file must never mask the real error above.
                pass


def safe_export_path(destination_dir: str, name: str) -> str:
    """Resolve `name` inside `destination_dir`, refusing anything that escapes.

    `name` is untrusted — typically a unit id, which may have come from a zip
    entry. Absolute paths, drive letters and `..` segments are rejected outright,
    and the resolved result is checked to still be under the destination, which
    also catches escapes through an existing symlink.
    """
    if not name or name in (".", ".."):
        raise ExportError(f"invalid name: {name!r}")
    normalised = name.replace("\\", "/")
    if normalised.startswith("/"):
        raise ExportError(f"refusing an absolute path: {name!r}")
    if len(normalised) > 1 and normalised[1] == ":":
        raise ExportError(f"refusing a drive-qualified path: {name!r}")
    if any(part == ".." for part in normalised.split("/")):
        raise ExportError(f"refusing a path that escapes the destination: {name!r}")

    base = os.path.realpath(os.path.abspath(destination_dir))
    candidate = os.path.realpath(os.path.join(base, *normalised.split("/")))
    if candidate != base and not candidate.startswith(base + os.sep):
        raise ExportError(f"refusing a path outside the destination: {name!r}")
    return candidate


@dataclass(frozen=True)
class RecentEntry:
    """An artifact the user built or opened before."""

    path: str
    label: str
    units: int
    original_bytes: int
    artifact_bytes: int

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "label": self.label,
            "units": self.units,
            "original_bytes": self.original_bytes,
            "artifact_bytes": self.artifact_bytes,
        }


class RecentArtifacts:
    """A short list of previously saved artifacts, stored as JSON.

    Only artifacts the user explicitly saved to disk are listed, and an entry
    whose file has since disappeared is filtered out on read rather than offered
    and then failing to open.
    """

    def __init__(self, directory: Optional[str] = None) -> None:
        self._directory = directory or app_data_dir()
        self._path = os.path.join(self._directory, RECENTS_FILENAME)

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> List[RecentEntry]:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            # A corrupt or unreadable recents file is a convenience feature
            # failing, not the product failing: start from an empty list rather
            # than refusing to launch.
            return []
        if not isinstance(raw, list):
            return []

        entries: List[RecentEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not os.path.isfile(path):
                continue
            entries.append(
                RecentEntry(
                    path=path,
                    label=str(item.get("label") or os.path.basename(path)),
                    units=int(item.get("units") or 0),
                    original_bytes=int(item.get("original_bytes") or 0),
                    artifact_bytes=int(item.get("artifact_bytes") or 0),
                )
            )
        return entries[:MAX_RECENTS]

    def remember(self, entry: RecentEntry) -> None:
        entries = [item for item in self.load() if item.path != entry.path]
        entries.insert(0, entry)
        payload = json.dumps(
            [item.as_dict() for item in entries[:MAX_RECENTS]], indent=2
        ).encode("utf-8")
        try:
            ensure_dir(self._directory)
            atomic_write(self._path, payload, overwrite=True)
        except ExportError:
            # Failing to record a recent entry must never break the operation
            # the user actually asked for.
            pass

    def clear(self) -> None:
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ExportError(f"could not clear recents: {error}", cause=error) from error
