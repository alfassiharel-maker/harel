"""Serialising the CCP representation to bytes, and reading it back.

The container is what makes a representation real rather than notional. Its
length on disk is the size the Core reports -- there is no formula anywhere in
the reporting path, exactly as in the storage experiment, where the reported CCP
size was always the measured container length.

Layout, all integers little-endian or unsigned varints:

    magic      4 bytes  "CCP1"
    version    2 bytes
    config     4 varints   min_chunk, avg_chunk, max_chunk, min_similarity_ppm
    count      varint      number of unit records
    records    repeated:
        uid            varint length + utf-8 bytes
        kind           1 byte  (0 literal, 1 derived)
        size           varint  length of the reconstructed unit
        digest         32 bytes
        literal:  payload varint length + bytes
        derived:  base uid varint length + utf-8, program varint length + bytes

Every length is bounds-checked against the remaining buffer before it is used, so
a truncated or hostile container raises `CCPFormatError` instead of allocating on
a claimed length.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Tuple

from .change_program import (
    CCPFormatError,
    ChangeProgram,
    read_uvarint,
    write_uvarint,
)
from .representation import (
    KIND_DERIVED,
    KIND_LITERAL,
    BuildConfig,
    BuildStats,
    CCPModel,
    UnitRecord,
)
from .units import UNIT_DIGEST_BYTES

MAGIC = b"CCP1"
VERSION = 1

_KIND_TO_BYTE = {KIND_LITERAL: 0, KIND_DERIVED: 1}
_BYTE_TO_KIND = {value: key for key, value in _KIND_TO_BYTE.items()}

# Similarity is stored as parts-per-million so the container holds no floats and
# a build is bit-reproducible across platforms.
_PPM = 1_000_000


def _write_blob(out: bytearray, payload: bytes) -> None:
    write_uvarint(out, len(payload))
    out += payload


def _read_blob(data: bytes, pos: int) -> Tuple[bytes, int]:
    length, pos = read_uvarint(data, pos)
    if pos + length > len(data):
        raise CCPFormatError("blob length runs past the buffer")
    return bytes(data[pos : pos + length]), pos + length


def serialize(model: CCPModel) -> bytes:
    """Write the whole representation to a single byte string."""
    out = bytearray()
    out += MAGIC
    out += struct.pack("<H", VERSION)
    write_uvarint(out, model.config.min_chunk)
    write_uvarint(out, model.config.avg_chunk)
    write_uvarint(out, model.config.max_chunk)
    write_uvarint(out, int(round(model.config.min_similarity * _PPM)))

    uids = model.uids()
    write_uvarint(out, len(uids))
    for uid in uids:
        record = model.records[uid]
        _write_blob(out, uid.encode("utf-8"))
        out.append(_KIND_TO_BYTE[record.kind])
        write_uvarint(out, record.size)
        if len(record.digest) != UNIT_DIGEST_BYTES:
            raise CCPFormatError(f"unit {uid!r} has a malformed digest")
        out += record.digest
        if record.kind == KIND_LITERAL:
            _write_blob(out, model.literals[uid])
        else:
            if record.base_uid is None or record.program is None:
                raise CCPFormatError(f"derived record {uid!r} is incomplete")
            _write_blob(out, record.base_uid.encode("utf-8"))
            _write_blob(out, record.program.encode())
    return bytes(out)


def deserialize(data: bytes) -> CCPModel:
    """Read a container back into a model, rejecting anything malformed."""
    if len(data) < len(MAGIC) + 2:
        raise CCPFormatError("container is too short to hold a header")
    if data[: len(MAGIC)] != MAGIC:
        raise CCPFormatError("bad magic: not a CCP container")
    (version,) = struct.unpack_from("<H", data, len(MAGIC))
    if version != VERSION:
        raise CCPFormatError(f"unsupported container version {version}")

    pos = len(MAGIC) + 2
    min_chunk, pos = read_uvarint(data, pos)
    avg_chunk, pos = read_uvarint(data, pos)
    max_chunk, pos = read_uvarint(data, pos)
    similarity_ppm, pos = read_uvarint(data, pos)
    if not (0 < min_chunk <= avg_chunk <= max_chunk):
        raise CCPFormatError("container declares an inconsistent chunk configuration")

    config = BuildConfig(
        min_chunk=min_chunk,
        avg_chunk=avg_chunk,
        max_chunk=max_chunk,
        min_similarity=similarity_ppm / _PPM,
    )
    model = CCPModel(config=config)

    count, pos = read_uvarint(data, pos)
    if count > len(data):
        raise CCPFormatError("record count exceeds available bytes")

    derived_bases: List[Tuple[str, str]] = []
    stats = BuildStats()
    for _ in range(count):
        uid_bytes, pos = _read_blob(data, pos)
        uid = uid_bytes.decode("utf-8")
        if uid in model.records:
            raise CCPFormatError(f"duplicate unit id {uid!r} in container")
        if pos >= len(data):
            raise CCPFormatError("truncated record")
        kind_byte = data[pos]
        pos += 1
        kind = _BYTE_TO_KIND.get(kind_byte)
        if kind is None:
            raise CCPFormatError(f"unknown record kind 0x{kind_byte:02x}")
        size, pos = read_uvarint(data, pos)
        if pos + UNIT_DIGEST_BYTES > len(data):
            raise CCPFormatError("truncated digest")
        digest = bytes(data[pos : pos + UNIT_DIGEST_BYTES])
        pos += UNIT_DIGEST_BYTES

        stats.units += 1
        stats.original_bytes += size
        if kind == KIND_LITERAL:
            payload, pos = _read_blob(data, pos)
            if len(payload) != size:
                raise CCPFormatError(
                    f"unit {uid!r} declares size {size} but carries {len(payload)}"
                )
            model.records[uid] = UnitRecord(
                uid=uid, kind=kind, size=size, digest=digest
            )
            model.literals[uid] = payload
            stats.literals += 1
            stats.stored_payload_bytes += size
        else:
            base_bytes, pos = _read_blob(data, pos)
            program_bytes, pos = _read_blob(data, pos)
            program = ChangeProgram.decode(program_bytes)
            if program.output_length != size:
                raise CCPFormatError(
                    f"unit {uid!r} declares size {size} but its program "
                    f"produces {program.output_length}"
                )
            base_uid = base_bytes.decode("utf-8")
            model.records[uid] = UnitRecord(
                uid=uid,
                kind=kind,
                size=size,
                digest=digest,
                base_uid=base_uid,
                program=program,
            )
            derived_bases.append((uid, base_uid))
            stats.derived += 1
            stats.stored_payload_bytes += len(program_bytes)
            stats.copied_bytes += program.copied_bytes
            stats.added_bytes += program.added_bytes

    if pos != len(data):
        raise CCPFormatError("trailing bytes after the final record")

    # Structural check: every base must exist and must itself be stored in full.
    # This is what makes delta chains impossible to smuggle in through a crafted
    # container rather than merely absent from builds we produce.
    for uid, base_uid in derived_bases:
        if base_uid not in model.records:
            raise CCPFormatError(f"unit {uid!r} references missing base {base_uid!r}")
        if model.records[base_uid].kind != KIND_LITERAL:
            raise CCPFormatError(
                f"unit {uid!r} references {base_uid!r}, which is not stored in "
                f"full: chained deltas are rejected"
            )

    model.stats = stats
    return model


def container_overhead(model: CCPModel, container: bytes) -> Dict[str, int]:
    """Split a container's length into payload and index, both measured.

    Reported so the index is never left out of a saving claim -- the storage
    experiment's rule that the cost accounting includes everything.
    """
    payload = model.stats.stored_payload_bytes
    return {
        "container_bytes": len(container),
        "payload_bytes": payload,
        "index_bytes": len(container) - payload,
    }
