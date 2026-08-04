"""Minimal reader/writer for the `.safetensors` container.

Zero dependencies on purpose: the compression engine must be auditable and
runnable on a bare Python 3.11 (same rule the analytics engine follows in this
repository).

Format, as published by Hugging Face:

    [ 8 bytes  ] little-endian u64 N — length of the header
    [ N bytes  ] UTF-8 JSON header: {name: {dtype, shape, data_offsets}, ...}
                 plus an optional "__metadata__" string map
    [ rest     ] tensor data, each tensor occupying [start, end) of this region

The engine never interprets tensor values — it works on the raw byte spans.
That is what makes the reconstruction bit-exact for every dtype, including ones
this file has never heard of.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

# Byte width per safetensors dtype tag. Used only to pick the byte-plane stride
# for the delta transform; an unknown dtype degrades to stride 1, never to an
# error, so a future dtype cannot break reconstruction.
DTYPE_ITEMSIZE: dict[str, int] = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "U16": 2,
    "I16": 2,
    "F16": 2,
    "BF16": 2,
    "U32": 4,
    "I32": 4,
    "F32": 4,
    "U64": 8,
    "I64": 8,
    "F64": 8,
}

_HEADER_LEN_STRUCT = struct.Struct("<Q")

# A header larger than this is refused. Untrusted files must not be able to make
# us allocate arbitrary memory from an 8-byte length prefix (OWASP A03/A05).
MAX_HEADER_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class TensorEntry:
    """One tensor's location and shape inside a safetensors file."""

    name: str
    dtype: str
    shape: tuple[int, ...]
    start: int
    end: int

    @property
    def nbytes(self) -> int:
        return self.end - self.start

    @property
    def itemsize(self) -> int:
        return DTYPE_ITEMSIZE.get(self.dtype, 1)

    def signature(self) -> tuple[str, tuple[int, ...]]:
        """What must match for two tensors to be delta-comparable."""
        return (self.dtype, self.shape)


class SafetensorsError(ValueError):
    """The file is not a well-formed safetensors container."""


@dataclass(frozen=True)
class SafetensorsFile:
    """A parsed safetensors file held as raw bytes plus a decoded index.

    `header_bytes` is kept verbatim rather than re-serialised. Two JSON encoders
    can produce different bytes for the same object, and the product's whole
    claim is a byte-identical rebuild — so the original header travels with the
    delta instead of being regenerated.
    """

    header_bytes: bytes
    header: dict
    entries: tuple[TensorEntry, ...]
    data: bytes

    @property
    def data_offset(self) -> int:
        """Absolute file offset where the tensor data region begins."""
        return _HEADER_LEN_STRUCT.size + len(self.header_bytes)

    @property
    def total_bytes(self) -> int:
        return self.data_offset + len(self.data)

    def by_name(self) -> dict[str, TensorEntry]:
        return {e.name: e for e in self.entries}

    def tensor_bytes(self, entry: TensorEntry) -> bytes:
        return self.data[entry.start : entry.end]

    def to_bytes(self) -> bytes:
        return _HEADER_LEN_STRUCT.pack(len(self.header_bytes)) + self.header_bytes + self.data


def parse(blob: bytes) -> SafetensorsFile:
    """Parse a safetensors file from memory. Raises SafetensorsError on garbage."""
    if len(blob) < _HEADER_LEN_STRUCT.size:
        raise SafetensorsError("file shorter than the 8-byte header length prefix")

    (header_len,) = _HEADER_LEN_STRUCT.unpack_from(blob, 0)
    if header_len > MAX_HEADER_BYTES:
        raise SafetensorsError(f"header length {header_len} exceeds the {MAX_HEADER_BYTES} cap")

    header_start = _HEADER_LEN_STRUCT.size
    header_end = header_start + header_len
    if header_end > len(blob):
        raise SafetensorsError("header length runs past the end of the file")

    header_bytes = blob[header_start:header_end]
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetensorsError(f"header is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise SafetensorsError("header JSON must be an object")

    data = blob[header_end:]
    entries: list[TensorEntry] = []
    for name, spec in header.items():
        if name == "__metadata__":
            continue
        if not isinstance(spec, dict):
            raise SafetensorsError(f"entry {name!r} is not an object")
        try:
            dtype = str(spec["dtype"])
            shape = tuple(int(d) for d in spec["shape"])
            start, end = (int(v) for v in spec["data_offsets"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SafetensorsError(f"entry {name!r} is malformed: {exc}") from exc
        if not 0 <= start <= end <= len(data):
            raise SafetensorsError(f"entry {name!r} offsets [{start},{end}) fall outside the data region")
        entries.append(TensorEntry(name=name, dtype=dtype, shape=shape, start=start, end=end))

    entries.sort(key=lambda e: (e.start, e.name))
    return SafetensorsFile(header_bytes=header_bytes, header=header, entries=tuple(entries), data=data)


def read(path: str) -> SafetensorsFile:
    with open(path, "rb") as fh:
        return parse(fh.read())


def build(tensors: list[tuple[str, str, tuple[int, ...], bytes]], metadata: dict[str, str] | None = None) -> bytes:
    """Serialise tensors into a safetensors blob.

    `tensors` is a list of (name, dtype, shape, raw_bytes) in the order they
    should occupy the data region. Used by the demo model generator and by
    tests; the engine itself never needs to author a file.
    """
    header: dict[str, object] = {}
    if metadata:
        header["__metadata__"] = dict(metadata)

    cursor = 0
    chunks: list[bytes] = []
    for name, dtype, shape, raw in tensors:
        header[name] = {"dtype": dtype, "shape": list(shape), "data_offsets": [cursor, cursor + len(raw)]}
        chunks.append(raw)
        cursor += len(raw)

    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    # safetensors pads the header to an 8-byte boundary so the data region is
    # aligned; real files do this and our reader must round-trip it.
    pad = (-len(header_bytes)) % 8
    header_bytes += b" " * pad
    return _HEADER_LEN_STRUCT.pack(len(header_bytes)) + header_bytes + b"".join(chunks)


def covered_spans(entries: tuple[TensorEntry, ...], data_len: int) -> list[tuple[int, int]]:
    """Return the gaps in the data region that no tensor covers.

    Alignment padding between tensors is legal. Those bytes are part of the
    file, so they are stored verbatim in the container — otherwise a rebuild
    would be "equivalent" rather than identical, and the lossless claim would
    quietly become a lossless-ish claim.
    """
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for entry in sorted(entries, key=lambda e: e.start):
        if entry.start > cursor:
            gaps.append((cursor, entry.start))
        cursor = max(cursor, entry.end)
    if cursor < data_len:
        gaps.append((cursor, data_len))
    return gaps
