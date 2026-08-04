"""The model repository — where the toggle actually moves bytes.

A repository holds one base checkpoint and any number of derived variants, per
tenant. It has two storage modes and the product's central switch flips between
them:

* `full` — every variant is a complete checkpoint on disk. This is how a model
  registry works today, and it is the honest baseline.
* `ccp`  — every variant is a `.ccp` container: a plan plus deltas against the
  base.

Flipping the mode **migrates the bytes on disk**. It does not change a display
setting. Turn the switch off and the directory really does grow back to full
checkpoints; turn it on and the full copies are re-encoded and removed. Every
number the dashboard shows is `os.stat` on what is actually there, which is the
only kind of number worth putting in front of an investor.

Tenant isolation: every path is derived from a validated tenant id and then
re-checked against that tenant's root after resolution. There is no admin
override and no cross-tenant read path — not because this demo has attackers,
but because the production version of this service holds other companies'
proprietary weights, and that property has to be in the design from the first
commit.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Literal

from engine import metrics, pack
from engine.codec import sha256

from .ledger import Ledger

Mode = Literal["ccp", "full"]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

BASE_FILENAME = "base.safetensors"
INDEX_FILENAME = "index.json"
LEDGER_FILENAME = "ledger.jsonl"


class RepositoryError(RuntimeError):
    """A repository operation was refused."""


class IdentifierError(ValueError):
    """A tenant, base or variant id failed validation."""


def validate_id(value: str, kind: str) -> str:
    """Accept only lowercase slugs. Rejects traversal, absolute paths, unicode
    lookalikes and anything else that could escape a tenant directory."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise IdentifierError(
            f"invalid {kind}: must be 1-63 chars of [a-z0-9._-] starting alphanumeric"
        )
    if ".." in value:
        raise IdentifierError(f"invalid {kind}: path traversal")
    return value


@dataclass
class VariantRecord:
    """Everything known about one derived model.

    Both representations' sizes are recorded whichever mode is active, so the
    dashboard can show what the switch is worth *before* it is pressed. The
    measurement is never guessed: `container_bytes` is the length of a container
    that was really built and really verified.
    """

    variant_id: str
    label: str
    kind: str
    raw_bytes: int
    raw_digest: str
    container_bytes: int
    gzip_bytes: int
    tensors: dict[str, int]
    pack_stats: dict = field(default_factory=dict)
    created_unix: float = 0.0
    verified_unix: float | None = None
    verify_seconds: float | None = None

    @property
    def container_ratio(self) -> float | None:
        if self.raw_bytes == 0:
            return None
        return self.container_bytes / self.raw_bytes

    def stored_bytes_for(self, mode: Mode) -> int:
        return self.container_bytes if mode == "ccp" else self.raw_bytes

    def as_dict(self) -> dict[str, object]:
        return {
            "variant_id": self.variant_id,
            "label": self.label,
            "kind": self.kind,
            "raw_bytes": self.raw_bytes,
            "raw_digest": self.raw_digest,
            "container_bytes": self.container_bytes,
            "gzip_bytes": self.gzip_bytes,
            "container_ratio": self.container_ratio,
            "savings_ratio": (1 - self.container_ratio) if self.container_ratio is not None else None,
            "savings_ratio_vs_gzip": (
                (self.gzip_bytes - self.container_bytes) / self.gzip_bytes if self.gzip_bytes else None
            ),
            "tensors": self.tensors,
            "pack_stats": self.pack_stats,
            "created_unix": self.created_unix,
            "verified_unix": self.verified_unix,
            "verify_seconds": self.verify_seconds,
        }


class Repository:
    """One tenant's model store."""

    def __init__(self, root: str, tenant_id: str) -> None:
        self.tenant_id = validate_id(tenant_id, "tenant id")
        self._root = os.path.realpath(os.path.join(root, "tenants", self.tenant_id))
        os.makedirs(os.path.join(self._root, "variants"), exist_ok=True)
        self._lock = threading.RLock()
        self.ledger = Ledger(self._path(LEDGER_FILENAME))
        self._index = self._load_index()

    # ---- paths -------------------------------------------------------------

    def _path(self, *parts: str) -> str:
        """Join under the tenant root and refuse anything that escapes it."""
        candidate = os.path.realpath(os.path.join(self._root, *parts))
        if candidate != self._root and not candidate.startswith(self._root + os.sep):
            raise RepositoryError("path escapes the tenant root")
        return candidate

    def _variant_path(self, variant_id: str, mode: Mode) -> str:
        validate_id(variant_id, "variant id")
        suffix = ".ccp" if mode == "ccp" else ".safetensors"
        return self._path("variants", f"{variant_id}{suffix}")

    @property
    def root(self) -> str:
        return self._root

    @property
    def base_path(self) -> str:
        return self._path(BASE_FILENAME)

    # ---- index -------------------------------------------------------------

    def _load_index(self) -> dict:
        path = self._path(INDEX_FILENAME)
        if not os.path.exists(path):
            return {"mode": "ccp", "base": None, "variants": {}}
        with open(path, "r", encoding="utf-8") as fh:
            try:
                index = json.load(fh)
            except json.JSONDecodeError as exc:
                raise RepositoryError(f"index for tenant {self.tenant_id} is unreadable: {exc}") from exc
        index.setdefault("mode", "ccp")
        index.setdefault("base", None)
        index.setdefault("variants", {})
        return index

    def _save_index(self) -> None:
        path = self._path(INDEX_FILENAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._index, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @property
    def mode(self) -> Mode:
        return "ccp" if self._index.get("mode") == "ccp" else "full"

    def records(self) -> list[VariantRecord]:
        return [self._record(vid) for vid in sorted(self._index["variants"])]

    def _record(self, variant_id: str) -> VariantRecord:
        row = self._index["variants"][variant_id]
        return VariantRecord(
            variant_id=variant_id,
            label=row.get("label", variant_id),
            kind=row.get("kind", "variant"),
            raw_bytes=int(row["raw_bytes"]),
            raw_digest=row["raw_digest"],
            container_bytes=int(row["container_bytes"]),
            gzip_bytes=int(row.get("gzip_bytes", 0)),
            tensors=dict(row.get("tensors", {})),
            pack_stats=dict(row.get("pack_stats", {})),
            created_unix=float(row.get("created_unix", 0.0)),
            verified_unix=row.get("verified_unix"),
            verify_seconds=row.get("verify_seconds"),
        )

    # ---- base --------------------------------------------------------------

    def set_base(self, blob: bytes, *, base_id: str, label: str = "") -> dict:
        validate_id(base_id, "base id")
        parsed = pack.safetensors.parse(blob)  # rejects a malformed upload before it touches disk
        with self._lock:
            with open(self.base_path, "wb") as fh:
                fh.write(blob)
            self._index["base"] = {
                "base_id": base_id,
                "label": label or base_id,
                "bytes": len(blob),
                "digest": sha256(blob),
                "tensor_count": len(parsed.entries),
                "gzip_bytes": len(gzip.compress(blob, 6)),
                "created_unix": round(time.time(), 3),
            }
            self._index["variants"] = {}
            self._save_index()
            for stale in self._iter_variant_files():
                os.remove(stale)
            self.ledger.append(
                "base.set",
                {"base_id": base_id, "bytes": len(blob), "digest": sha256(blob)[:16], "tensors": len(parsed.entries)},
            )
            return dict(self._index["base"])

    def base(self) -> dict | None:
        base = self._index.get("base")
        return dict(base) if base else None

    def base_blob(self) -> bytes:
        if not self._index.get("base"):
            raise RepositoryError("this repository has no base model yet")
        with open(self.base_path, "rb") as fh:
            return fh.read()

    # ---- variants ----------------------------------------------------------

    def add_variant(self, blob: bytes, *, variant_id: str, label: str = "", kind: str = "variant") -> VariantRecord:
        """Register a derived model.

        The container is always built and always verified, whatever the mode.
        In `full` mode it is measured and discarded, which is what lets the
        dashboard show the pending saving while the switch is off — a measured
        figure, not a forecast.
        """
        validate_id(variant_id, "variant id")
        base_blob = self.base_blob()

        with self._lock:
            container, stats = pack.pack(
                base_blob,
                blob,
                base_id=str(self._index["base"]["base_id"]),
                variant_id=variant_id,
                verify=True,
            )
            parsed = pack.safetensors.parse(blob)
            record = VariantRecord(
                variant_id=variant_id,
                label=label or variant_id,
                kind=kind,
                raw_bytes=len(blob),
                raw_digest=sha256(blob),
                container_bytes=len(container),
                gzip_bytes=len(gzip.compress(blob, 6)),
                tensors={"count": len(parsed.entries)},
                pack_stats=stats.as_dict(),
                created_unix=round(time.time(), 3),
            )

            if self.mode == "ccp":
                self._write(self._variant_path(variant_id, "ccp"), container)
            else:
                self._write(self._variant_path(variant_id, "full"), blob)

            self._index["variants"][variant_id] = record.as_dict() | {"raw_digest": record.raw_digest}
            self._save_index()
            self.ledger.append(
                "variant.add",
                {
                    "variant_id": variant_id,
                    "mode": self.mode,
                    "raw_bytes": record.raw_bytes,
                    "container_bytes": record.container_bytes,
                    "savings_ratio": record.as_dict()["savings_ratio"],
                    "digest": record.raw_digest[:16],
                },
            )
            return record

    def materialise(self, variant_id: str) -> tuple[bytes, float]:
        """Return the variant's original file bytes, and how long that took.

        In `ccp` mode this is a real reconstruction: read the container, apply
        every block against the base, verify the digest. In `full` mode it is a
        file read. The dashboard shows both timings, because "how slow is the
        rebuild" is the first technical objection any buyer raises.
        """
        validate_id(variant_id, "variant id")
        with self._lock:
            if variant_id not in self._index["variants"]:
                raise RepositoryError(f"unknown variant {variant_id!r}")
            record = self._record(variant_id)
            started = time.perf_counter()
            if self.mode == "ccp":
                with open(self._variant_path(variant_id, "ccp"), "rb") as fh:
                    container = fh.read()
                blob = pack.unpack(container, self.base_blob(), verify=True)
            else:
                with open(self._variant_path(variant_id, "full"), "rb") as fh:
                    blob = fh.read()
            elapsed = time.perf_counter() - started

        if sha256(blob) != record.raw_digest:
            raise RepositoryError(f"variant {variant_id!r} failed its digest check — refusing to serve it")
        return blob, elapsed

    def verify_all(self) -> dict[str, object]:
        """Rebuild every variant and check it against the digest recorded at push
        time. This is the lossless claim, executed rather than asserted."""
        results: list[dict[str, object]] = []
        ok = True
        started = time.perf_counter()
        for record in self.records():
            try:
                blob, elapsed = self.materialise(record.variant_id)
                matched = sha256(blob) == record.raw_digest
                results.append(
                    {
                        "variant_id": record.variant_id,
                        "label": record.label,
                        "lossless": matched,
                        "bytes": len(blob),
                        "seconds": round(elapsed, 4),
                        "throughput_mib_s": round((len(blob) / (1024**2)) / elapsed, 1) if elapsed > 0 else None,
                        "digest": record.raw_digest[:16],
                    }
                )
                ok = ok and matched
                with self._lock:
                    row = self._index["variants"][record.variant_id]
                    row["verified_unix"] = round(time.time(), 3)
                    row["verify_seconds"] = round(elapsed, 4)
                    self._save_index()
            # Corruption is a finding to report on, not a reason for the whole
            # verification pass to abort: an operator needs to know which
            # variant is damaged, and that the others are fine.
            except (pack.ContainerError, pack.CodecError, RepositoryError, OSError) as exc:
                ok = False
                results.append(
                    {
                        "variant_id": record.variant_id,
                        "label": record.label,
                        "lossless": False,
                        "error": str(exc),
                    }
                )

        chain_ok, chain_error = self.ledger.verify_chain()
        summary = {
            "mode": self.mode,
            "all_lossless": ok,
            "variants_checked": len(results),
            "total_seconds": round(time.perf_counter() - started, 4),
            "results": results,
            "ledger_chain_ok": chain_ok,
            "ledger_chain_error": chain_error,
        }
        self.ledger.append(
            "verify.all",
            {"mode": self.mode, "variants": len(results), "all_lossless": ok},
        )
        return summary

    # ---- the switch --------------------------------------------------------

    def set_mode(self, mode: Mode) -> dict[str, object]:
        """Flip storage mode and migrate every variant on disk.

        Migration is write-then-delete per variant: the replacement file is
        written and, in the CCP direction, verified before the previous
        representation is removed. A crash mid-migration therefore leaves both
        copies rather than neither.
        """
        if mode not in ("ccp", "full"):
            raise RepositoryError(f"unknown mode {mode!r}")

        with self._lock:
            previous = self.mode
            if previous == mode:
                return {"mode": mode, "changed": False, "migrated": 0, "seconds": 0.0}

            started = time.perf_counter()
            before = self.disk_bytes()
            migrated = 0

            for record in self.records():
                if mode == "full":
                    # CCP -> full: rebuild the whole checkpoint on disk.
                    container_path = self._variant_path(record.variant_id, "ccp")
                    with open(container_path, "rb") as fh:
                        container = fh.read()
                    blob = pack.unpack(container, self.base_blob(), verify=True)
                    if sha256(blob) != record.raw_digest:
                        raise RepositoryError(
                            f"aborting migration: {record.variant_id!r} did not rebuild to its recorded digest"
                        )
                    self._write(self._variant_path(record.variant_id, "full"), blob)
                    os.remove(container_path)
                else:
                    # full -> CCP: re-encode and verify before dropping the copy.
                    full_path = self._variant_path(record.variant_id, "full")
                    with open(full_path, "rb") as fh:
                        blob = fh.read()
                    container, stats = pack.pack(
                        self.base_blob(),
                        blob,
                        base_id=str(self._index["base"]["base_id"]),
                        variant_id=record.variant_id,
                        verify=True,
                    )
                    self._write(self._variant_path(record.variant_id, "ccp"), container)
                    os.remove(full_path)
                    row = self._index["variants"][record.variant_id]
                    row["container_bytes"] = len(container)
                    row["pack_stats"] = stats.as_dict()
                migrated += 1

            self._index["mode"] = mode
            self._save_index()
            after = self.disk_bytes()
            elapsed = time.perf_counter() - started

            self.ledger.append(
                "mode.set",
                {
                    "from": previous,
                    "to": mode,
                    "migrated": migrated,
                    "disk_bytes_before": before,
                    "disk_bytes_after": after,
                    "seconds": round(elapsed, 4),
                },
            )
            return {
                "mode": mode,
                "changed": True,
                "migrated": migrated,
                "disk_bytes_before": before,
                "disk_bytes_after": after,
                "seconds": round(elapsed, 4),
            }

    # ---- measurement -------------------------------------------------------

    def _iter_variant_files(self) -> Iterator[str]:
        directory = self._path("variants")
        if not os.path.isdir(directory):
            return
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                yield path

    def disk_bytes(self) -> int:
        """Actual bytes occupied by this tenant's models — base plus variants.

        `os.stat` on every file, not a running total. A counter can drift; the
        filesystem cannot.
        """
        total = 0
        if os.path.exists(self.base_path):
            total += os.stat(self.base_path).st_size
        for path in self._iter_variant_files():
            total += os.stat(path).st_size
        return total

    def savings(self) -> metrics.StoreSavings:
        """What a registry would hold versus what is held now."""
        base = self.base()
        records = self.records()
        base_bytes = int(base["bytes"]) if base else 0
        base_gzip = int(base.get("gzip_bytes", 0)) if base else 0

        raw = base_bytes + sum(r.raw_bytes for r in records)
        stored = self.disk_bytes()
        gzip_baseline = base_gzip + sum(r.gzip_bytes for r in records)
        return metrics.StoreSavings(
            raw_bytes=raw,
            stored_bytes=stored,
            baseline_gzip_bytes=gzip_baseline or None,
        )

    def variant_ratio(self) -> float | None:
        """Bytes-weighted stored/raw across variants only.

        Variants only, because that is the ratio that scales: a client fetches
        the base once and then a delta per variant, so folding the base into this
        number would understate the saving on a large fleet and overstate it on a
        fleet of one.
        """
        records = self.records()
        raw = sum(r.raw_bytes for r in records)
        if raw == 0:
            return None
        return sum(r.container_bytes for r in records) / raw

    def summary(self) -> dict[str, object]:
        savings = self.savings()
        ratio = self.variant_ratio()
        records = self.records()
        potential = self.base()["bytes"] if self.base() else 0
        potential += sum(r.container_bytes for r in records)
        return {
            "tenant_id": self.tenant_id,
            "mode": self.mode,
            "base": self.base(),
            "variant_count": len(records),
            "disk_bytes": self.disk_bytes(),
            "disk_bytes_if_ccp": potential,
            "disk_bytes_if_full": (self.base()["bytes"] if self.base() else 0) + sum(r.raw_bytes for r in records),
            "variant_ratio": ratio,
            "variant_savings_ratio": (1 - ratio) if ratio is not None else None,
            "savings": savings.as_dict(),
            "variants": [r.as_dict() for r in records],
        }

    @staticmethod
    def _write(path: str, blob: bytes) -> None:
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def reset(self) -> None:
        """Drop this tenant's models. The ledger survives — it is append-only."""
        with self._lock:
            for path in self._iter_variant_files():
                os.remove(path)
            if os.path.exists(self.base_path):
                os.remove(self.base_path)
            self._index = {"mode": self._index.get("mode", "ccp"), "base": None, "variants": {}}
            self._save_index()
            self.ledger.append("repository.reset", {})


class RepositoryRegistry:
    """Tenant id -> Repository, created on first use.

    The only way to reach a Repository. A caller that has not presented a tenant
    id cannot obtain one, so there is no code path that reads across tenants.
    """

    def __init__(self, root: str) -> None:
        self._root = os.path.realpath(root)
        os.makedirs(self._root, exist_ok=True)
        self._repos: dict[str, Repository] = {}
        self._lock = threading.Lock()

    @property
    def root(self) -> str:
        return self._root

    def get(self, tenant_id: str) -> Repository:
        tenant_id = validate_id(tenant_id, "tenant id")
        with self._lock:
            repo = self._repos.get(tenant_id)
            if repo is None:
                repo = Repository(self._root, tenant_id)
                self._repos[tenant_id] = repo
            return repo

    def tenants(self) -> list[str]:
        directory = os.path.join(self._root, "tenants")
        if not os.path.isdir(directory):
            return []
        return sorted(name for name in os.listdir(directory) if os.path.isdir(os.path.join(directory, name)))

    def destroy(self, tenant_id: str) -> None:
        """Remove a tenant's directory entirely. Used by tests and the demo reset."""
        tenant_id = validate_id(tenant_id, "tenant id")
        with self._lock:
            self._repos.pop(tenant_id, None)
        path = os.path.realpath(os.path.join(self._root, "tenants", tenant_id))
        if not path.startswith(os.path.join(self._root, "tenants")):
            raise RepositoryError("refusing to remove a path outside the store root")
        shutil.rmtree(path, ignore_errors=True)
