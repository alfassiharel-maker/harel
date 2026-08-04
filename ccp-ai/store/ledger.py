"""Append-only, hash-chained operation log.

Every byte the product claims to have saved is the result of an operation that
is written here first. The chain exists so that a savings figure shown to an
investor — or later to a customer being billed on saved egress — can be
re-derived from an immutable record rather than from a mutable counter that some
code path could have incremented twice.

One file per tenant. Appends only: no update path, no delete path.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

GENESIS = "0" * 64


@dataclass(frozen=True)
class Entry:
    seq: int
    unix: float
    action: str
    detail: dict
    prev_hash: str
    entry_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "unix": self.unix,
            "action": self.action,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


def _hash_entry(seq: int, unix: float, action: str, detail: dict, prev_hash: str) -> str:
    body = json.dumps(
        {"seq": seq, "unix": unix, "action": action, "detail": detail, "prev_hash": prev_hash},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Ledger:
    """A single tenant's log. Thread-safe: the API server is threaded."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    @property
    def path(self) -> str:
        return self._path

    def append(self, action: str, detail: dict | None = None) -> Entry:
        with self._lock:
            entries = self._read_unlocked()
            prev_hash = entries[-1].entry_hash if entries else GENESIS
            seq = len(entries) + 1
            unix = round(time.time(), 3)
            payload = detail or {}
            entry = Entry(
                seq=seq,
                unix=unix,
                action=action,
                detail=payload,
                prev_hash=prev_hash,
                entry_hash=_hash_entry(seq, unix, action, payload, prev_hash),
            )
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.as_dict(), separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return entry

    def read(self, limit: int | None = None) -> list[Entry]:
        with self._lock:
            entries = self._read_unlocked()
        return entries[-limit:] if limit else entries

    def verify_chain(self) -> tuple[bool, str | None]:
        """Recompute every link. Returns (ok, first_failure_description)."""
        prev = GENESIS
        for entry in self.read():
            if entry.prev_hash != prev:
                return False, f"entry {entry.seq} points at {entry.prev_hash[:12]}…, expected {prev[:12]}…"
            expected = _hash_entry(entry.seq, entry.unix, entry.action, entry.detail, entry.prev_hash)
            if expected != entry.entry_hash:
                return False, f"entry {entry.seq} hash does not match its contents"
            prev = entry.entry_hash
        return True, None

    def _read_unlocked(self) -> list[Entry]:
        if not os.path.exists(self._path):
            return []
        entries: list[Entry] = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    entries.append(
                        Entry(
                            seq=int(row["seq"]),
                            unix=float(row["unix"]),
                            action=str(row["action"]),
                            detail=dict(row.get("detail") or {}),
                            prev_hash=str(row["prev_hash"]),
                            entry_hash=str(row["entry_hash"]),
                        )
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    # A truncated tail line means the process died mid-append. Stop
                    # at the last intact entry rather than discarding the log or
                    # pretending the damaged line parsed.
                    raise LedgerCorruption(f"{self._path}: line {len(entries) + 1} is unreadable: {exc}") from exc
        return entries


class LedgerCorruption(RuntimeError):
    """The log on disk cannot be read as a chain."""
