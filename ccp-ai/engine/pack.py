"""The `.ccp` container: pack a variant against a base, rebuild it bit-exactly.

A container is a plan, not a diff of a byte stream. Each tensor is planned
independently — copied from the base for free, delta-encoded, or stored whole —
which is what lets a variant change its architecture (a new classification head,
a resized vocabulary, an extra layer) and still ship as a delta. Byte-stream
diffing cannot do that: one inserted tensor shifts every offset that follows and
the diff degenerates to a full copy.

Layout on disk:

    [ 4 bytes ] magic "CCPK"
    [ 4 bytes ] little-endian u32 header length
    [ N bytes ] UTF-8 JSON plan (below)
    [ rest    ] block payloads, concatenated in plan order

Independent per-tensor blocks are also what makes chunked reconstruction
possible: a client fetching a 400 GB model streams and applies blocks one at a
time, holding one tensor in memory rather than the whole model.
"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass, field
from typing import Iterator

from . import safetensors
from .codec import (
    DEFAULT_ZLIB_LEVEL,
    CodecError,
    Compressor,
    EncodedBlock,
    decode_block,
    encode_delta,
    encode_literal,
    sha256,
)

MAGIC = b"CCPK"
CONTAINER_VERSION = 1
ENGINE_ID = "ccp-engine/0.1"

_LEN = struct.Struct("<I")
MAX_PLAN_BYTES = 256 * 1024 * 1024


class ContainerError(ValueError):
    """The container is not readable, or does not match the base it names."""


@dataclass
class PackStats:
    """What the pack actually cost and saved. Every field is measured, not modelled."""

    variant_raw_bytes: int
    container_bytes: int
    tensors_total: int = 0
    tensors_identical: int = 0
    tensors_delta: int = 0
    tensors_literal: int = 0
    bytes_identical: int = 0
    bytes_delta_raw: int = 0
    bytes_delta_stored: int = 0
    bytes_literal_raw: int = 0
    bytes_literal_stored: int = 0
    encode_seconds: float = 0.0

    @property
    def savings_ratio(self) -> float | None:
        """Fraction of the variant's bytes not shipped. None if there is nothing to ship."""
        if self.variant_raw_bytes == 0:
            return None
        return 1.0 - (self.container_bytes / self.variant_raw_bytes)

    def as_dict(self) -> dict[str, object]:
        return {
            "variant_raw_bytes": self.variant_raw_bytes,
            "container_bytes": self.container_bytes,
            "savings_ratio": self.savings_ratio,
            "tensors": {
                "total": self.tensors_total,
                "identical_to_base": self.tensors_identical,
                "delta_encoded": self.tensors_delta,
                "stored_whole": self.tensors_literal,
            },
            "bytes": {
                "identical_to_base": self.bytes_identical,
                "delta_raw": self.bytes_delta_raw,
                "delta_stored": self.bytes_delta_stored,
                "literal_raw": self.bytes_literal_raw,
                "literal_stored": self.bytes_literal_stored,
            },
            "encode_seconds": round(self.encode_seconds, 4),
        }


@dataclass
class _Block:
    meta: dict[str, object]
    payload: bytes = b""
    stats_key: str = ""
    raw_bytes: int = 0


@dataclass
class Plan:
    """Decoded container plan plus the payload region it indexes."""

    header: dict
    payload: bytes = field(repr=False, default=b"")

    @property
    def base_digest(self) -> str:
        return str(self.header["base"]["digest"])

    @property
    def variant_digest(self) -> str:
        return str(self.header["variant"]["digest"])

    @property
    def blocks(self) -> list[dict]:
        return list(self.header["blocks"])


def _row_aligned(base_entry: safetensors.TensorEntry, variant_entry: safetensors.TensorEntry) -> bool:
    """True when the two tensors differ only in the length of their first axis.

    Restricted to identical dtype and identical trailing dimensions, because
    those are the conditions under which row-major layout guarantees that the
    shared rows are a byte-prefix of both buffers. Anything looser would be
    guessing at memory layout, and a wrong guess here would produce a container
    that fails its own verification — never silent corruption, but a failed push
    is still a bad product.
    """
    if base_entry.dtype != variant_entry.dtype:
        return False
    if len(base_entry.shape) != len(variant_entry.shape) or not base_entry.shape:
        return False
    return base_entry.shape[1:] == variant_entry.shape[1:] and base_entry.shape[0] != variant_entry.shape[0]


def _emit(block: EncodedBlock) -> dict[str, object]:
    return {
        "stored_bytes": block.stored_bytes,
        "raw_bytes": block.raw_bytes,
        "plane_sizes": list(block.plane_sizes),
        "itemsize": block.itemsize,
        "transform": block.transform,
        "compressor": block.compressor,
        "digest": block.digest,
    }


def pack(
    base_blob: bytes,
    variant_blob: bytes,
    *,
    base_id: str,
    variant_id: str,
    compressor: Compressor = "zlib",
    level: int = DEFAULT_ZLIB_LEVEL,
    verify: bool = True,
) -> tuple[bytes, PackStats]:
    """Encode `variant_blob` as a container against `base_blob`.

    With `verify=True` (the default, and the only mode the product ships) the
    container is decoded again in-process and its SHA-256 compared to the
    original. A container that cannot reproduce its input is never returned —
    the lossless guarantee is enforced at write time, not promised at read time.
    """
    started = time.perf_counter()
    base = safetensors.parse(base_blob)
    variant = safetensors.parse(variant_blob)
    base_by_name = base.by_name()

    stats = PackStats(variant_raw_bytes=len(variant_blob), container_bytes=0)
    blocks: list[_Block] = []

    # The variant's own header travels verbatim. Regenerating it from the parsed
    # index would risk a different key order or spacing, and the guarantee is
    # byte-identical, not semantically-equal.
    header_block = encode_literal(variant.header_bytes, 1, compressor, level)
    blocks.append(
        _Block(
            meta={"kind": "header", "op": "literal", **_emit(header_block)},
            payload=header_block.payload,
        )
    )

    for entry in variant.entries:
        stats.tensors_total += 1
        v_bytes = variant.tensor_bytes(entry)
        counterpart = base_by_name.get(entry.name)

        if counterpart is not None and counterpart.signature() == entry.signature():
            b_bytes = base.tensor_bytes(counterpart)
            if b_bytes == v_bytes:
                # Frozen tensor. Costs zero bytes: the container records where to
                # copy it from in the base the client already has.
                stats.tensors_identical += 1
                stats.bytes_identical += entry.nbytes
                blocks.append(
                    _Block(
                        meta={
                            "kind": "tensor",
                            "name": entry.name,
                            "op": "copy_base",
                            "dtype": entry.dtype,
                            "shape": list(entry.shape),
                            "dst": [entry.start, entry.end],
                            "base_span": [counterpart.start, counterpart.end],
                            "raw_bytes": entry.nbytes,
                            "stored_bytes": 0,
                            "digest": sha256(v_bytes),
                        }
                    )
                )
                continue

            encoded = encode_delta(b_bytes, v_bytes, entry.itemsize, compressor, level)
            if encoded.stored_bytes < entry.nbytes:
                stats.tensors_delta += 1
                stats.bytes_delta_raw += entry.nbytes
                stats.bytes_delta_stored += encoded.stored_bytes
                blocks.append(
                    _Block(
                        meta={
                            "kind": "tensor",
                            "name": entry.name,
                            "op": "delta",
                            "dtype": entry.dtype,
                            "shape": list(entry.shape),
                            "dst": [entry.start, entry.end],
                            "base_span": [counterpart.start, counterpart.end],
                            **_emit(encoded),
                        },
                        payload=encoded.payload,
                    )
                )
                continue
            # A delta that grew is a delta we do not ship. Falls through to the
            # literal path below so the container is never worse than a copy.

        if counterpart is not None and _row_aligned(counterpart, entry):
            # The tensor was resized along its first axis — an extended
            # vocabulary, extra experts, a widened head. safetensors is
            # row-major, so the rows the two versions share occupy a byte prefix
            # of both tensors and delta-encode normally; only the new rows are
            # genuinely new. Emitting this as two blocks (delta prefix + literal
            # tail) is what keeps "we added 256 tokens" from costing a full copy
            # of the embedding matrix.
            overlap = min(counterpart.nbytes, entry.nbytes)
            prefix = encode_delta(
                base.data[counterpart.start : counterpart.start + overlap],
                v_bytes[:overlap],
                entry.itemsize,
                compressor,
                level,
            )
            stats.tensors_delta += 1
            stats.bytes_delta_raw += overlap
            stats.bytes_delta_stored += prefix.stored_bytes
            blocks.append(
                _Block(
                    meta={
                        "kind": "tensor",
                        "name": entry.name,
                        "op": "delta",
                        "dtype": entry.dtype,
                        "shape": list(entry.shape),
                        "resized_from": list(counterpart.shape),
                        "dst": [entry.start, entry.start + overlap],
                        "base_span": [counterpart.start, counterpart.start + overlap],
                        **_emit(prefix),
                    },
                    payload=prefix.payload,
                )
            )
            if entry.nbytes > overlap:
                tail = encode_literal(v_bytes[overlap:], entry.itemsize, compressor, level)
                stats.bytes_literal_raw += entry.nbytes - overlap
                stats.bytes_literal_stored += tail.stored_bytes
                blocks.append(
                    _Block(
                        meta={
                            "kind": "tensor_tail",
                            "name": entry.name,
                            "op": "literal",
                            "dst": [entry.start + overlap, entry.end],
                            **_emit(tail),
                        },
                        payload=tail.payload,
                    )
                )
            continue

        encoded = encode_literal(v_bytes, entry.itemsize, compressor, level)
        stats.tensors_literal += 1
        stats.bytes_literal_raw += entry.nbytes
        stats.bytes_literal_stored += encoded.stored_bytes
        blocks.append(
            _Block(
                meta={
                    "kind": "tensor",
                    "name": entry.name,
                    "op": "literal",
                    "dtype": entry.dtype,
                    "shape": list(entry.shape),
                    "dst": [entry.start, entry.end],
                    **_emit(encoded),
                },
                payload=encoded.payload,
            )
        )

    # Alignment padding and any region no tensor claims.
    for start, end in safetensors.covered_spans(variant.entries, len(variant.data)):
        gap = variant.data[start:end]
        encoded = encode_literal(gap, 1, compressor, level)
        blocks.append(
            _Block(
                meta={"kind": "gap", "op": "literal", "dst": [start, end], **_emit(encoded)},
                payload=encoded.payload,
            )
        )

    payload_parts: list[bytes] = []
    cursor = 0
    for block in blocks:
        if block.payload:
            block.meta["payload_offset"] = cursor
            payload_parts.append(block.payload)
            cursor += len(block.payload)
        else:
            block.meta["payload_offset"] = cursor
            block.meta.setdefault("plane_sizes", [])

    plan = {
        "container_version": CONTAINER_VERSION,
        "engine": ENGINE_ID,
        "created_unix": int(time.time()),
        "codec": {"compressor": compressor, "level": level},
        "base": {"id": base_id, "digest": sha256(base_blob), "bytes": len(base_blob)},
        "variant": {
            "id": variant_id,
            "digest": sha256(variant_blob),
            "bytes": len(variant_blob),
            "data_offset": variant.data_offset,
            "data_bytes": len(variant.data),
        },
        "blocks": [b.meta for b in blocks],
    }
    plan_bytes = json.dumps(plan, separators=(",", ":")).encode("utf-8")
    container = MAGIC + _LEN.pack(len(plan_bytes)) + plan_bytes + b"".join(payload_parts)

    stats.container_bytes = len(container)
    stats.encode_seconds = time.perf_counter() - started

    if verify:
        rebuilt = unpack(container, base_blob)
        if sha256(rebuilt) != plan["variant"]["digest"]:
            raise ContainerError(
                f"refusing to emit container for {variant_id!r}: verification rebuild does not match the source"
            )

    return container, stats


def read_plan(container: bytes) -> Plan:
    if len(container) < len(MAGIC) + _LEN.size:
        raise ContainerError("container shorter than its own header")
    if container[: len(MAGIC)] != MAGIC:
        raise ContainerError("not a .ccp container (bad magic)")
    (plan_len,) = _LEN.unpack_from(container, len(MAGIC))
    if plan_len > MAX_PLAN_BYTES:
        raise ContainerError(f"plan length {plan_len} exceeds the {MAX_PLAN_BYTES} cap")
    start = len(MAGIC) + _LEN.size
    end = start + plan_len
    if end > len(container):
        raise ContainerError("plan length runs past the end of the container")
    try:
        plan = json.loads(container[start:end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContainerError(f"plan is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("container_version") != CONTAINER_VERSION:
        raise ContainerError(f"unsupported container version: {plan.get('container_version') if isinstance(plan, dict) else '?'}")
    for required in ("base", "variant", "blocks", "codec"):
        if required not in plan:
            raise ContainerError(f"plan is missing {required!r}")
    return Plan(header=plan, payload=container[end:])


def unpack(container: bytes, base_blob: bytes, *, verify: bool = True) -> bytes:
    """Rebuild the variant's original file bytes from a container plus its base."""
    plan = read_plan(container)

    if verify and sha256(base_blob) != plan.base_digest:
        raise ContainerError(
            "base model does not match the one this container was built against "
            f"(expected {plan.base_digest[:12]}…)"
        )

    base = safetensors.parse(base_blob)
    variant_meta = plan.header["variant"]
    data = bytearray(int(variant_meta["data_bytes"]))
    header_bytes: bytes | None = None
    payload = plan.payload

    for meta in plan.blocks:
        kind = meta.get("kind")
        op = meta.get("op")
        offset = int(meta.get("payload_offset", 0))
        stored = int(meta.get("stored_bytes", 0))
        blob = payload[offset : offset + stored]

        if kind == "header":
            header_bytes = decode_block(
                blob,
                meta.get("plane_sizes", []),
                int(meta["raw_bytes"]),
                int(meta.get("itemsize", 1)),
                str(meta["transform"]),
                str(plan.header["codec"]["compressor"]),
            )
            continue

        dst_start, dst_end = (int(v) for v in meta["dst"])
        if not 0 <= dst_start <= dst_end <= len(data):
            raise ContainerError(f"block destination [{dst_start},{dst_end}) falls outside the data region")

        if op == "copy_base":
            b_start, b_end = (int(v) for v in meta["base_span"])
            if not 0 <= b_start <= b_end <= len(base.data):
                raise ContainerError("copy_base span falls outside the base data region")
            chunk = base.data[b_start:b_end]
        elif op in ("delta", "literal"):
            base_chunk: bytes | None = None
            if op == "delta":
                b_start, b_end = (int(v) for v in meta["base_span"])
                if not 0 <= b_start <= b_end <= len(base.data):
                    raise ContainerError("delta base span falls outside the base data region")
                base_chunk = base.data[b_start:b_end]
            chunk = decode_block(
                blob,
                meta.get("plane_sizes", []),
                int(meta["raw_bytes"]),
                int(meta.get("itemsize", 1)),
                str(meta["transform"]),
                str(plan.header["codec"]["compressor"]),
                base=base_chunk,
            )
        else:
            raise ContainerError(f"unknown block op {op!r}")

        if len(chunk) != dst_end - dst_start:
            raise ContainerError(f"block produced {len(chunk)} bytes for a {dst_end - dst_start}-byte slot")
        if verify and "digest" in meta and kind == "tensor" and sha256(chunk) != meta["digest"]:
            raise ContainerError(f"tensor {meta.get('name')!r} failed its per-tensor digest check")
        data[dst_start:dst_end] = chunk

    if header_bytes is None:
        raise ContainerError("container has no header block")

    rebuilt = safetensors._HEADER_LEN_STRUCT.pack(len(header_bytes)) + header_bytes + bytes(data)
    if verify and sha256(rebuilt) != plan.variant_digest:
        raise ContainerError("rebuilt file does not match the container's recorded digest")
    return rebuilt


def iter_blocks(container: bytes) -> Iterator[dict]:
    """Walk a container's plan without decoding payloads — used by the inspector."""
    yield from read_plan(container).blocks


def describe(container: bytes) -> dict[str, object]:
    """Summarise a container for the UI: what was copied, deltaed, stored whole."""
    plan = read_plan(container)
    per_op: dict[str, dict[str, int]] = {}
    tensors: list[dict[str, object]] = []
    for meta in plan.blocks:
        op = str(meta.get("op"))
        bucket = per_op.setdefault(op, {"count": 0, "raw_bytes": 0, "stored_bytes": 0})
        bucket["count"] += 1
        bucket["raw_bytes"] += int(meta.get("raw_bytes", 0))
        bucket["stored_bytes"] += int(meta.get("stored_bytes", 0))
        if meta.get("kind") == "tensor":
            raw = int(meta.get("raw_bytes", 0))
            stored = int(meta.get("stored_bytes", 0))
            tensors.append(
                {
                    "name": meta.get("name"),
                    "op": op,
                    "dtype": meta.get("dtype"),
                    "shape": meta.get("shape"),
                    "raw_bytes": raw,
                    "stored_bytes": stored,
                    "ratio": (stored / raw) if raw else None,
                }
            )
    tensors.sort(key=lambda t: int(t["raw_bytes"] or 0), reverse=True)
    return {
        "base": plan.header["base"],
        "variant": plan.header["variant"],
        "codec": plan.header["codec"],
        "by_op": per_op,
        "tensors": tensors,
    }


__all__ = [
    "CodecError",
    "ContainerError",
    "PackStats",
    "Plan",
    "describe",
    "iter_blocks",
    "pack",
    "read_plan",
    "unpack",
]
