#!/usr/bin/env python3
"""CCP-AI — single-file visual demo. Copy this one file, run it, open the browser.

    python ccp_app.py            ->  http://127.0.0.1:8420

Standard library only: no pip install, no venv, no numpy, no torch.

This is a self-contained subset of the full product in `ccp-ai/` (which splits the
same engine across engine/, store/, server/ and web/, and adds multi-tenancy, an
append-only ledger, a CLI and 111 tests). Everything the demo *claims* is real
here: real .safetensors checkpoints, real bit-level delta coding, real bytes on
disk measured with os.stat, and a switch that migrates them.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import random
import struct
import sys
import threading
import time
import zlib
from array import array
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8420
STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".store-standalone")

# AWS S3 list prices, us-east-1, 2026-08. In one place so a reviewer can change
# them and watch every number on the page move.
EGRESS_USD_PER_GIB = 0.08
STORAGE_USD_PER_GIB_MONTH = 0.023
GIB = 1024**3

# ─────────────────────────────────────────────────────────────────────────────
# safetensors — read/write, at the byte-span level
# ─────────────────────────────────────────────────────────────────────────────

HDR = struct.Struct("<Q")
ITEMSIZE = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8, "I32": 4, "I16": 2, "I8": 1, "U8": 1, "BOOL": 1}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def st_parse(blob: bytes) -> dict:
    """Parse a .safetensors file into {header_bytes, entries, data}.

    The header travels verbatim rather than being re-serialised: two JSON
    encoders can produce different bytes for the same object, and the guarantee
    is byte-identical, not semantically-equal.
    """
    if len(blob) < HDR.size:
        raise ValueError("file shorter than its 8-byte header length prefix")
    (n,) = HDR.unpack_from(blob, 0)
    if n > 128 * 1024 * 1024 or HDR.size + n > len(blob):
        raise ValueError("header length is implausible or runs past the end of the file")
    header_bytes = blob[HDR.size : HDR.size + n]
    header = json.loads(header_bytes.decode("utf-8"))
    data = blob[HDR.size + n :]

    entries = []
    for name, spec in header.items():
        if name == "__metadata__":
            continue
        start, end = (int(v) for v in spec["data_offsets"])
        if not 0 <= start <= end <= len(data):
            raise ValueError(f"tensor {name!r} lies outside the data region")
        entries.append(
            {
                "name": name,
                "dtype": str(spec["dtype"]),
                "shape": tuple(int(d) for d in spec["shape"]),
                "start": start,
                "end": end,
                "itemsize": ITEMSIZE.get(str(spec["dtype"]), 1),
            }
        )
    entries.sort(key=lambda e: (e["start"], e["name"]))
    return {"header_bytes": header_bytes, "entries": entries, "data": data}


def st_build(tensors: list[tuple[str, str, tuple[int, ...], bytes]], metadata: dict | None = None) -> bytes:
    header: dict = {}
    if metadata:
        header["__metadata__"] = dict(metadata)
    cursor, chunks = 0, []
    for name, dtype, shape, raw in tensors:
        header[name] = {"dtype": dtype, "shape": list(shape), "data_offsets": [cursor, cursor + len(raw)]}
        chunks.append(raw)
        cursor += len(raw)
    hb = json.dumps(header, separators=(",", ":")).encode("utf-8")
    hb += b" " * ((-len(hb)) % 8)  # real files pad the header to an 8-byte boundary
    return HDR.pack(len(hb)) + hb + b"".join(chunks)


# ─────────────────────────────────────────────────────────────────────────────
# The codec — this is the actual invention
# ─────────────────────────────────────────────────────────────────────────────
#
# A fine-tune moves a weight by a fraction of its value. In IEEE-754 that leaves
# the sign bit, the exponent byte and the high mantissa bits untouched. Two
# transforms harvest that redundancy; the packer builds both per tensor and keeps
# whichever is smaller. Both are bijections — nothing is rounded or quantised.

INT_CODE = {2: "H", 4: "I", 8: "Q"}


def xor_bytes(a: bytes, b: bytes) -> bytes:
    """Byte-wise XOR, via one C-level bignum op rather than a Python loop."""
    if len(a) != len(b):
        raise ValueError("XOR operands differ in length")
    if not a:
        return b""
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(len(a), "little")


def int_delta(base: bytes, variant: bytes, itemsize: int) -> bytes | None:
    """Element-wise difference of the two buffers' integer bit patterns, zigzagged.

    Why this beats XOR: XOR zeroes the bits that did not change but is blind to
    magnitude — a one-ULP move from 0x1000 to 0x0FFF XORs to 0x1FFF, thirteen set
    bits of noise. Subtraction keeps one ULP as the number 1. Within a sign,
    float bit patterns are monotone in value, so "close in value" really does mean
    "close as an integer".
    """
    code = INT_CODE.get(itemsize)
    if code is None or len(base) != len(variant) or len(base) % itemsize:
        return None
    bits = itemsize * 8
    mask = (1 << bits) - 1
    shift = bits - 1
    a, b = array(code), array(code)
    a.frombytes(base)
    b.frombytes(variant)
    if sys.byteorder == "big":
        a.byteswap()
        b.byteswap()
    # `d` is wrapped into the word before zigzagging: Python ints are unbounded,
    # so y-x can exceed the word range and the encoding would not be reversible.
    z = array(code, [(((d := (y - x) & mask) << 1) & mask) ^ (-(d >> shift) & mask) for x, y in zip(a, b)])
    if sys.byteorder == "big":
        z.byteswap()
    return z.tobytes()


def int_undelta(base: bytes, residual: bytes, itemsize: int) -> bytes:
    code = INT_CODE[itemsize]
    bits = itemsize * 8
    mask = (1 << bits) - 1
    a, z = array(code), array(code)
    a.frombytes(base)
    z.frombytes(residual)
    if sys.byteorder == "big":
        a.byteswap()
        z.byteswap()
    out = array(code, [(x + ((u >> 1) ^ (-(u & 1) & mask))) & mask for x, u in zip(a, z)])
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def split_planes(buf: bytes, itemsize: int) -> list[bytes]:
    """Deinterleave into byte planes: plane k holds byte k of every element.

    The step a general-purpose compressor cannot do for itself. Interleaved it
    sees `zero, zero, noise, noise` repeating and cannot model it; split, the
    exponent plane collapses into a run of zeros.
    """
    if itemsize <= 1 or len(buf) % itemsize:
        return [buf]
    return [buf[k::itemsize] for k in range(itemsize)]


def join_planes(planes: list[bytes], itemsize: int, total: int) -> bytes:
    if itemsize <= 1 or len(planes) == 1:
        return planes[0]
    out = bytearray(total)
    for k, plane in enumerate(planes):
        out[k::itemsize] = plane
    return bytes(out)


def encode(residual: bytes, itemsize: int) -> tuple[bytes, list[int]]:
    parts = [zlib.compress(p, 6) for p in split_planes(residual, itemsize)]
    return b"".join(parts), [len(p) for p in parts]


def decode(payload: bytes, sizes: list[int], raw_bytes: int, itemsize: int) -> bytes:
    cursor, planes = 0, []
    for size in sizes:
        if size < 0 or cursor + size > len(payload):
            raise ValueError("plane sizes do not fit the stored payload")
        planes.append(zlib.decompress(payload[cursor : cursor + size]))
        cursor += size
    return join_planes(planes, itemsize, raw_bytes)


def plane_report(base: bytes, variant: bytes, itemsize: int) -> list[dict]:
    """Per-plane compressibility — the data behind the "why it works" panel."""
    roles = {4: {3: "sign + exponent", 2: "high mantissa", 1: "mid mantissa", 0: "low mantissa"},
             2: {1: "sign + exponent", 0: "mantissa"}}
    residual = int_delta(base, variant, itemsize) or xor_bytes(base, variant)
    rows = []
    for k, plane in enumerate(split_planes(residual, itemsize)):
        rows.append(
            {
                "plane": k,
                "role": roles.get(itemsize, {}).get(k, f"byte {k}"),
                "raw_bytes": len(plane),
                "stored_bytes": len(zlib.compress(plane, 6)),
                "zero_fraction": (plane.count(0) / len(plane)) if plane else None,
            }
        )
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# The .ccp container: "CCPK" | u32 plan length | JSON plan | block payloads
# ─────────────────────────────────────────────────────────────────────────────
#
# The plan is per tensor, not a byte diff of the whole file. That is what lets a
# variant change its architecture — drop the LM head, add a task head, extend the
# vocabulary — and still ship as a delta. A byte diff degenerates to a full copy
# the moment one insertion shifts every following offset.

MAGIC = b"CCPK"
LEN32 = struct.Struct("<I")


def ccp_pack(base_blob: bytes, variant_blob: bytes, verify: bool = True) -> tuple[bytes, dict]:
    base, variant = st_parse(base_blob), st_parse(variant_blob)
    by_name = {e["name"]: e for e in base["entries"]}
    blocks, payloads, cursor = [], [], 0
    stats = {"copied": 0, "delta": 0, "whole": 0, "bytes_copied": 0, "bytes_delta_raw": 0,
             "bytes_delta_stored": 0, "bytes_whole_raw": 0, "bytes_whole_stored": 0}

    def emit(meta: dict, payload: bytes) -> None:
        nonlocal cursor
        meta["at"] = cursor
        meta["stored"] = len(payload)
        blocks.append(meta)
        if payload:
            payloads.append(payload)
            cursor += len(payload)

    # The variant's own header, stored verbatim.
    payload, sizes = encode(variant["header_bytes"], 1)
    emit({"kind": "header", "op": "literal", "sizes": sizes, "raw": len(variant["header_bytes"]), "is": 1}, payload)

    for e in variant["entries"]:
        vb = variant["data"][e["start"] : e["end"]]
        raw = len(vb)
        peer = by_name.get(e["name"])
        same_sig = peer and peer["dtype"] == e["dtype"] and peer["shape"] == e["shape"]

        if same_sig:
            bb = base["data"][peer["start"] : peer["end"]]
            if bb == vb:
                # Frozen tensor: zero bytes stored, just a pointer into the base
                # the client already holds.
                stats["copied"] += 1
                stats["bytes_copied"] += raw
                emit({"kind": "t", "name": e["name"], "op": "copy_base", "dtype": e["dtype"],
                      "shape": list(e["shape"]), "dst": [e["start"], e["end"]],
                      "src": [peer["start"], peer["end"]], "raw": raw, "digest": sha256(vb)}, b"")
                continue

            candidates = [("xor", xor_bytes(bb, vb))]
            arithmetic = int_delta(bb, vb, e["itemsize"])
            if arithmetic is not None:
                candidates.append(("intdelta", arithmetic))
            best = min((encode(r, e["itemsize"]) + (name,) for name, r in candidates), key=lambda c: len(c[0]))
            payload, sizes, transform = best
            if len(payload) < raw:
                stats["delta"] += 1
                stats["bytes_delta_raw"] += raw
                stats["bytes_delta_stored"] += len(payload)
                emit({"kind": "t", "name": e["name"], "op": "delta", "tf": transform, "dtype": e["dtype"],
                      "shape": list(e["shape"]), "dst": [e["start"], e["end"]],
                      "src": [peer["start"], peer["end"]], "sizes": sizes, "raw": raw,
                      "is": e["itemsize"], "digest": sha256(vb)}, payload)
                continue
            # A delta that grew is a delta we do not ship — fall through.

        if peer and peer["dtype"] == e["dtype"] and len(peer["shape"]) == len(e["shape"]) \
                and peer["shape"][1:] == e["shape"][1:] and peer["shape"][:1] != e["shape"][:1]:
            # Resized first axis — extended vocabulary, extra experts. Row-major
            # layout means the shared rows are a byte prefix of both tensors, so
            # they delta normally and only the new rows are genuinely new.
            overlap = min(peer["end"] - peer["start"], raw)
            bb = base["data"][peer["start"] : peer["start"] + overlap]
            residual = int_delta(bb, vb[:overlap], e["itemsize"]) or xor_bytes(bb, vb[:overlap])
            transform = "intdelta" if int_delta(bb, vb[:overlap], e["itemsize"]) is not None else "xor"
            payload, sizes = encode(residual, e["itemsize"])
            stats["delta"] += 1
            stats["bytes_delta_raw"] += overlap
            stats["bytes_delta_stored"] += len(payload)
            emit({"kind": "t", "name": e["name"], "op": "delta", "tf": transform, "dtype": e["dtype"],
                  "shape": list(e["shape"]), "resized_from": list(peer["shape"]),
                  "dst": [e["start"], e["start"] + overlap],
                  "src": [peer["start"], peer["start"] + overlap], "sizes": sizes, "raw": overlap,
                  "is": e["itemsize"], "digest": sha256(vb[:overlap])}, payload)
            if raw > overlap:
                tail, tsizes = encode(vb[overlap:], e["itemsize"])
                stats["bytes_whole_raw"] += raw - overlap
                stats["bytes_whole_stored"] += len(tail)
                emit({"kind": "tail", "name": e["name"], "op": "literal",
                      "dst": [e["start"] + overlap, e["end"]], "sizes": tsizes,
                      "raw": raw - overlap, "is": e["itemsize"], "digest": sha256(vb[overlap:])}, tail)
            continue

        payload, sizes = encode(vb, e["itemsize"])
        stats["whole"] += 1
        stats["bytes_whole_raw"] += raw
        stats["bytes_whole_stored"] += len(payload)
        emit({"kind": "t", "name": e["name"], "op": "literal", "dtype": e["dtype"], "shape": list(e["shape"]),
              "dst": [e["start"], e["end"]], "sizes": sizes, "raw": raw, "is": e["itemsize"],
              "digest": sha256(vb)}, payload)

    # Alignment padding and any region no tensor claims. Stored verbatim, or the
    # rebuild would be "equivalent" rather than identical.
    covered, gaps = 0, []
    for e in sorted(variant["entries"], key=lambda x: x["start"]):
        if e["start"] > covered:
            gaps.append((covered, e["start"]))
        covered = max(covered, e["end"])
    if covered < len(variant["data"]):
        gaps.append((covered, len(variant["data"])))
    for start, end in gaps:
        chunk = variant["data"][start:end]
        payload, sizes = encode(chunk, 1)
        emit({"kind": "gap", "op": "literal", "dst": [start, end], "sizes": sizes, "raw": len(chunk), "is": 1}, payload)

    plan = {
        "v": 1,
        "base": {"digest": sha256(base_blob), "bytes": len(base_blob)},
        "variant": {"digest": sha256(variant_blob), "bytes": len(variant_blob), "data_bytes": len(variant["data"])},
        "blocks": blocks,
    }
    pb = json.dumps(plan, separators=(",", ":")).encode("utf-8")
    container = MAGIC + LEN32.pack(len(pb)) + pb + b"".join(payloads)

    stats["raw_bytes"] = len(variant_blob)
    stats["container_bytes"] = len(container)
    stats["savings_ratio"] = 1 - len(container) / len(variant_blob) if variant_blob else None

    if verify:
        # The lossless guarantee is enforced at write time, not promised at read
        # time: a container that cannot reproduce its input is never returned.
        if sha256(ccp_unpack(container, base_blob)) != plan["variant"]["digest"]:
            raise ValueError("refusing to emit a container that does not rebuild its source")
    return container, stats


def ccp_unpack(container: bytes, base_blob: bytes, verify: bool = True) -> bytes:
    if container[:4] != MAGIC:
        raise ValueError("not a .ccp container (bad magic)")
    (n,) = LEN32.unpack_from(container, 4)
    plan = json.loads(container[8 : 8 + n].decode("utf-8"))
    payload = container[8 + n :]

    if verify and sha256(base_blob) != plan["base"]["digest"]:
        raise ValueError("base model does not match the one this container was built against")

    base = st_parse(base_blob)
    data = bytearray(int(plan["variant"]["data_bytes"]))
    header_bytes = None

    for b in plan["blocks"]:
        blob = payload[b["at"] : b["at"] + b["stored"]]
        if b["kind"] == "header":
            header_bytes = decode(blob, b["sizes"], b["raw"], b.get("is", 1))
            continue
        lo, hi = (int(v) for v in b["dst"])
        if not 0 <= lo <= hi <= len(data):
            raise ValueError("block destination lies outside the data region")
        if b["op"] == "copy_base":
            s0, s1 = (int(v) for v in b["src"])
            if not 0 <= s0 <= s1 <= len(base["data"]):
                raise ValueError("copy_base span lies outside the base")
            chunk = base["data"][s0:s1]
        else:
            joined = decode(blob, b["sizes"], b["raw"], b.get("is", 1))
            if b["op"] == "delta":
                s0, s1 = (int(v) for v in b["src"])
                bb = base["data"][s0:s1]
                chunk = int_undelta(bb, joined, b["is"]) if b.get("tf") == "intdelta" else xor_bytes(bb, joined)
            else:
                chunk = joined
        if len(chunk) != hi - lo:
            raise ValueError("block produced the wrong number of bytes for its slot")
        if verify and "digest" in b and sha256(chunk) != b["digest"]:
            raise ValueError(f"block {b.get('name')!r} failed its digest check")
        data[lo:hi] = chunk

    if header_bytes is None:
        raise ValueError("container has no header block")
    rebuilt = HDR.pack(len(header_bytes)) + header_bytes + bytes(data)
    if verify and sha256(rebuilt) != plan["variant"]["digest"]:
        raise ValueError("rebuilt file does not match the recorded digest")
    return rebuilt


# ─────────────────────────────────────────────────────────────────────────────
# Demo model family — real float32 checkpoints, generated deterministically
# ─────────────────────────────────────────────────────────────────────────────
#
# The perturbation model is stated rather than hidden, because it is what decides
# the compression ratio:   w' = w * (1 + s*g),  g ~ N(0,1)
# A light instruction tune moves weights by ~1e-4 relative; a hard domain retrain
# by ~1e-2. Both are shipped below, so the demo shows its worst case too.

D_MODEL, N_LAYERS, FFN, VOCAB, SEED = 128, 4, 512, 8192, 20260804

VARIANTS = [
    {"id": "instruct-fft-light", "label": "Instruct · full fine-tune (light, s=1e-4)", "kind": "full-fine-tune",
     "step": 1e-4, "only": (), "note": "Every weight in the model moved — the case LoRA exists to avoid shipping."},
    {"id": "instruct-fft", "label": "Instruct · full fine-tune (s=1e-3)", "kind": "full-fine-tune",
     "step": 1e-3, "only": (), "note": "A longer tune: deeper mantissa churn, so a lower ratio. Shown, not hidden."},
    {"id": "code-fft-heavy", "label": "Code · domain retrain (heavy, s=1e-2)", "kind": "full-fine-tune",
     "step": 1e-2, "only": (), "note": "Near the worst case for delta coding: a large step on every parameter."},
    {"id": "support-classifier", "label": "Support classifier · frozen backbone + new head", "kind": "task-head",
     "step": 1e-3, "only": ("layers.3", "model.norm"), "add": [("score.weight", (8, D_MODEL))],
     "drop": ("lm_head.weight",), "note": "Architecture change: LM head gone, classification head new."},
    {"id": "last-two-layers", "label": "Partial fine-tune · last two layers only", "kind": "partial-fine-tune",
     "step": 1e-3, "only": ("layers.2", "layers.3", "model.norm"),
     "note": "Frozen tensors cost zero bytes: the container points into the base."},
    {"id": "vocab-extended", "label": "Vocabulary extended · 8192 → 8448 tokens", "kind": "architecture-change",
     "step": 1e-4, "only": (), "vocab": 8448,
     "note": "Reshaped tensors delta their shared row prefix; only new rows are stored whole."},
]


def specs() -> list[tuple[str, tuple[int, ...]]]:
    out = [("model.embed_tokens.weight", (VOCAB, D_MODEL))]
    for i in range(N_LAYERS):
        p = f"model.layers.{i}"
        out += [(f"{p}.self_attn.q_proj.weight", (D_MODEL, D_MODEL)),
                (f"{p}.self_attn.k_proj.weight", (D_MODEL, D_MODEL)),
                (f"{p}.self_attn.v_proj.weight", (D_MODEL, D_MODEL)),
                (f"{p}.self_attn.o_proj.weight", (D_MODEL, D_MODEL)),
                (f"{p}.mlp.up_proj.weight", (FFN, D_MODEL)),
                (f"{p}.mlp.down_proj.weight", (D_MODEL, FFN)),
                (f"{p}.input_layernorm.weight", (D_MODEL,)),
                (f"{p}.post_attention_layernorm.weight", (D_MODEL,))]
    return out + [("model.norm.weight", (D_MODEL,)), ("lm_head.weight", (VOCAB, D_MODEL))]


def to_le(values: array) -> bytes:
    if sys.byteorder == "big":
        copy = array("f", values)
        copy.byteswap()
        return copy.tobytes()
    return values.tobytes()


def build_base(progress=None) -> tuple[bytes, dict]:
    rng = random.Random(SEED)
    tensors, payload = {}, []
    layout = specs()
    for i, (name, shape) in enumerate(layout):
        count = math.prod(shape)
        if name.endswith("layernorm.weight") or name == "model.norm.weight":
            values = array("f", [1.0] * count)  # norms initialise at 1.0 and barely move
        else:
            scale = 1.0 / math.sqrt(shape[-1])
            values = array("f", bytes(4 * count))
            for j in range(count):
                values[j] = rng.gauss(0.0, scale)
        tensors[name] = values
        payload.append((name, "F32", shape, to_le(values)))
        if progress:
            progress(f"base · {name}", (i + 1) / len(layout))
    return st_build(payload, {"format": "pt", "ccp_demo": "base"}), tensors


def build_variant(tensors: dict, spec: dict, progress=None) -> bytes:
    rng = random.Random(f"{SEED}:{spec['id']}")
    names = [n for n in tensors if n not in spec.get("drop", ())]
    payload, total = [], len(names) + len(spec.get("add", []))

    def tuned(name: str) -> bool:
        return not spec["only"] or any(part in name for part in spec["only"])

    for i, name in enumerate(names):
        values, shape = tensors[name], shape_of(name, len(tensors[name]))
        if spec.get("vocab") and name in ("model.embed_tokens.weight", "lm_head.weight"):
            extra = (spec["vocab"] - VOCAB) * D_MODEL
            grown = array("f", values)
            scale = 1.0 / math.sqrt(D_MODEL)
            grown.extend(array("f", [rng.gauss(0.0, scale) for _ in range(extra)]))
            values, shape = grown, (spec["vocab"], D_MODEL)
        if tuned(name):
            step = spec["step"]
            values = array("f", [v * (1.0 + step * rng.gauss(0.0, 1.0)) for v in values])
        payload.append((name, "F32", shape, to_le(values)))
        if progress:
            progress(f"{spec['id']} · {name}", (i + 1) / total)

    for name, shape in spec.get("add", []):
        scale = 1.0 / math.sqrt(shape[-1])
        values = array("f", [rng.gauss(0.0, scale) for _ in range(math.prod(shape))])
        payload.append((name, "F32", shape, to_le(values)))
    return st_build(payload, {"format": "pt", "derived_from": "base", "relative_step": f"{spec['step']:g}"})


def shape_of(name: str, count: int) -> tuple[int, ...]:
    if name in ("model.embed_tokens.weight", "lm_head.weight"):
        return (VOCAB, D_MODEL)
    if name.endswith("layernorm.weight") or name == "model.norm.weight":
        return (count,)
    if "up_proj" in name:
        return (FFN, D_MODEL)
    if "down_proj" in name:
        return (D_MODEL, FFN)
    return (D_MODEL, D_MODEL)


# ─────────────────────────────────────────────────────────────────────────────
# The store — where the switch actually moves bytes
# ─────────────────────────────────────────────────────────────────────────────


class Store:
    """One base checkpoint plus N variants, in one of two storage modes.

    `full` is how a model registry works today: a complete checkpoint per
    variant. `ccp` keeps one base plus a verified container per variant. Flipping
    the mode migrates the files on disk — write the replacement, verify it, then
    delete the previous representation, so a crash leaves both rather than
    neither. Every figure reported comes from os.stat, never a running counter.
    """

    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)
        os.makedirs(os.path.join(self.root, "variants"), exist_ok=True)
        self.lock = threading.RLock()
        self.index = self._load()

    # -- paths and index --

    def _path(self, *parts: str) -> str:
        p = os.path.realpath(os.path.join(self.root, *parts))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ValueError("path escapes the store root")
        return p

    def _vpath(self, vid: str, mode: str) -> str:
        if not vid.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise ValueError("invalid variant id")  # no traversal, no separators
        return self._path("variants", vid + (".ccp" if mode == "ccp" else ".safetensors"))

    def _load(self) -> dict:
        path = self._path("index.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        return {"mode": "ccp", "base": None, "variants": {}}

    def _save(self) -> None:
        path = self._path("index.json")
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(self.index, fh, indent=1)
        os.replace(path + ".tmp", path)

    @staticmethod
    def _write(path: str, blob: bytes) -> None:
        with open(path + ".tmp", "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(path + ".tmp", path)

    @property
    def mode(self) -> str:
        return "ccp" if self.index.get("mode") == "ccp" else "full"

    # -- writes --

    def set_base(self, blob: bytes) -> None:
        st_parse(blob)  # reject a malformed upload before it touches disk
        with self.lock:
            self._write(self._path("base.safetensors"), blob)
            self.index = {"mode": self.mode,
                          "base": {"bytes": len(blob), "digest": sha256(blob),
                                   "gzip_bytes": len(gzip.compress(blob, 6))},
                          "variants": {}}
            for name in os.listdir(self._path("variants")):
                os.remove(self._path("variants", name))
            self._save()

    def base_blob(self) -> bytes:
        if not self.index.get("base"):
            raise ValueError("this store has no base model yet")
        with open(self._path("base.safetensors"), "rb") as fh:
            return fh.read()

    def add_variant(self, blob: bytes, vid: str, label: str, kind: str, note: str, planes=None) -> dict:
        with self.lock:
            container, stats = ccp_pack(self.base_blob(), blob, verify=True)
            row = {
                "id": vid, "label": label, "kind": kind, "note": note,
                "raw_bytes": len(blob), "digest": sha256(blob),
                "container_bytes": len(container),
                "gzip_bytes": len(gzip.compress(blob, 6)),
                "stats": stats, "planes": planes,
            }
            row["savings_ratio"] = 1 - row["container_bytes"] / row["raw_bytes"]
            row["savings_vs_gzip"] = 1 - row["container_bytes"] / row["gzip_bytes"]
            # The container is built and measured whichever mode is active, so the
            # page can show what the switch is worth before it is pressed — a
            # measured figure, not a forecast.
            self._write(self._vpath(vid, self.mode), container if self.mode == "ccp" else blob)
            self.index["variants"][vid] = row
            self._save()
            return row

    def set_mode(self, mode: str) -> dict:
        if mode not in ("ccp", "full"):
            raise ValueError("unknown mode")
        with self.lock:
            if mode == self.mode:
                return {"mode": mode, "changed": False, "migrated": 0, "seconds": 0.0}
            t0, before, migrated = time.perf_counter(), self.disk_bytes(), 0
            base = self.base_blob()
            for vid, row in self.index["variants"].items():
                if mode == "full":
                    with open(self._vpath(vid, "ccp"), "rb") as fh:
                        blob = ccp_unpack(fh.read(), base, verify=True)
                    if sha256(blob) != row["digest"]:
                        raise ValueError(f"aborting migration: {vid} did not rebuild to its digest")
                    self._write(self._vpath(vid, "full"), blob)
                    os.remove(self._vpath(vid, "ccp"))
                else:
                    with open(self._vpath(vid, "full"), "rb") as fh:
                        blob = fh.read()
                    container, stats = ccp_pack(base, blob, verify=True)
                    self._write(self._vpath(vid, "ccp"), container)
                    os.remove(self._vpath(vid, "full"))
                    row["container_bytes"], row["stats"] = len(container), stats
                migrated += 1
            self.index["mode"] = mode
            self._save()
            return {"mode": mode, "changed": True, "migrated": migrated,
                    "disk_bytes_before": before, "disk_bytes_after": self.disk_bytes(),
                    "seconds": round(time.perf_counter() - t0, 3)}

    def reset(self) -> None:
        with self.lock:
            for name in os.listdir(self._path("variants")):
                os.remove(self._path("variants", name))
            base = self._path("base.safetensors")
            if os.path.exists(base):
                os.remove(base)
            self.index = {"mode": self.mode, "base": None, "variants": {}}
            self._save()

    # -- reads and measurement --

    def materialise(self, vid: str) -> tuple[bytes, float]:
        with self.lock:
            row = self.index["variants"].get(vid)
            if row is None:
                raise ValueError(f"unknown variant {vid!r}")
            t0 = time.perf_counter()
            with open(self._vpath(vid, self.mode), "rb") as fh:
                raw = fh.read()
            blob = ccp_unpack(raw, self.base_blob(), verify=True) if self.mode == "ccp" else raw
            elapsed = time.perf_counter() - t0
        if sha256(blob) != row["digest"]:
            raise ValueError(f"{vid} failed its digest check — refusing to serve it")
        return blob, elapsed

    def verify_all(self) -> dict:
        results, ok, t0 = [], True, time.perf_counter()
        for vid, row in self.index["variants"].items():
            try:
                blob, elapsed = self.materialise(vid)
                matched = sha256(blob) == row["digest"]
                ok = ok and matched
                results.append({"id": vid, "label": row["label"], "lossless": matched, "bytes": len(blob),
                                "seconds": round(elapsed, 4),
                                "mib_s": round((len(blob) / 1024**2) / elapsed, 1) if elapsed else None})
            except (ValueError, OSError, zlib.error) as exc:
                # Corruption is a finding to report, not a reason to abort the pass.
                ok = False
                results.append({"id": vid, "label": row["label"], "lossless": False, "error": str(exc)})
        return {"mode": self.mode, "all_lossless": ok, "checked": len(results),
                "total_seconds": round(time.perf_counter() - t0, 3), "results": results}

    def disk_bytes(self) -> int:
        total = 0
        base = self._path("base.safetensors")
        if os.path.exists(base):
            total += os.stat(base).st_size
        directory = self._path("variants")
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                total += os.stat(path).st_size
        return total

    def summary(self) -> dict:
        rows = list(self.index["variants"].values())
        base_bytes = self.index["base"]["bytes"] if self.index.get("base") else 0
        raw = sum(r["raw_bytes"] for r in rows)
        ccp = sum(r["container_bytes"] for r in rows)
        # Variants only: the base is fetched once per client, so folding it in
        # would overstate a small fleet and understate a large one.
        ratio = (ccp / raw) if raw else None
        return {
            "mode": self.mode,
            "seeded": self.index.get("base") is not None,
            "base": self.index.get("base"),
            "variant_count": len(rows),
            "disk_bytes": self.disk_bytes(),
            "disk_if_ccp": base_bytes + ccp,
            "disk_if_full": base_bytes + raw,
            "variant_ratio": ratio,
            "variant_savings": (1 - ratio) if ratio is not None else None,
            "gzip_baseline": (self.index["base"]["gzip_bytes"] if self.index.get("base") else 0)
            + sum(r["gzip_bytes"] for r in rows),
            "variants": sorted(rows, key=lambda r: -r["savings_ratio"]),
        }


def project(ratio: float, params_b: float, variants: int, downloads: int) -> dict:
    """Apply a measured ratio to a hypothetical fleet. Explicitly an extrapolation.

    The base is charged honestly — fetched once per client, then one delta per
    variant — so fleet savings are always below the per-variant delta ratio.
    """
    if not 0 < ratio <= 1 or params_b <= 0 or variants < 1 or downloads < 1:
        raise ValueError("projection inputs out of range")
    model = int(params_b * 1e9 * 2)  # fp16/bf16: two bytes per parameter
    delta = int(model * ratio)
    total_dl = variants * downloads
    base_egress, ccp_egress = model * total_dl, model * downloads + delta * total_dl
    base_store, ccp_store = model * variants, model + delta * variants
    usd = lambda b, rate: (b / GIB) * rate  # noqa: E731
    return {
        "assumptions": {"params_billions": params_b, "model_bytes": model, "variants": variants,
                        "downloads_per_variant": downloads, "ratio": ratio,
                        "egress_usd_per_gib": EGRESS_USD_PER_GIB,
                        "storage_usd_per_gib_month": STORAGE_USD_PER_GIB_MONTH},
        "egress": {"baseline_usd": usd(base_egress, EGRESS_USD_PER_GIB),
                   "ccp_usd": usd(ccp_egress, EGRESS_USD_PER_GIB),
                   "saved_usd": usd(base_egress - ccp_egress, EGRESS_USD_PER_GIB),
                   "baseline_bytes": base_egress, "ccp_bytes": ccp_egress},
        "storage": {"baseline_usd_month": usd(base_store, STORAGE_USD_PER_GIB_MONTH),
                    "ccp_usd_month": usd(ccp_store, STORAGE_USD_PER_GIB_MONTH),
                    "saved_usd_month": usd(base_store - ccp_store, STORAGE_USD_PER_GIB_MONTH),
                    "baseline_bytes": base_store, "ccp_bytes": ccp_store},
        "savings_ratio": 1 - ccp_egress / base_egress,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Background job — seeding is real arithmetic and takes ~25s, so it cannot block
# ─────────────────────────────────────────────────────────────────────────────

JOB = {"state": "idle", "message": "", "fraction": 0.0, "steps": [], "error": None, "started": 0.0}
JOB_LOCK = threading.Lock()


def seed_job(store: Store) -> None:
    def work() -> None:
        try:
            units = 1 + len(VARIANTS)
            tick = lambda i, f, m: JOB.update(  # noqa: E731
                fraction=min(0.999, (i + f) / units), message=m)
            store.reset()
            JOB["steps"].append("generating base checkpoint")
            base_blob, tensors = build_base(lambda m, f: tick(0, f, m))
            store.set_base(base_blob)
            JOB["steps"].append(f"base registered · {fmt_bytes(len(base_blob))}")

            base = st_parse(base_blob)
            base_by_name = {e["name"]: e for e in base["entries"]}
            for i, spec in enumerate(VARIANTS, start=1):
                JOB["steps"].append(f"deriving {spec['label']}")
                blob = build_variant(tensors, spec, lambda m, f: tick(i, f * 0.5, m))
                tick(i, 0.6, f"packing {spec['id']}")
                planes = largest_changed_planes(base, base_by_name, blob, spec)
                row = store.add_variant(blob, spec["id"], spec["label"], spec["kind"], spec["note"], planes)
                JOB["steps"].append(
                    f"{spec['id']} · {fmt_bytes(row['raw_bytes'])} → {fmt_bytes(row['container_bytes'])}"
                    f" ({row['savings_ratio']:.1%})")
            JOB.update(state="done", fraction=1.0, message="complete")
        except Exception as exc:  # noqa: BLE001 — a job thread must record, never vanish
            JOB.update(state="failed", message="failed", error=f"{type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()

    with JOB_LOCK:
        if JOB["state"] == "running":
            raise RuntimeError("a job is already running")
        JOB.update(state="running", message="starting", fraction=0.0, steps=[], error=None, started=time.time())
    threading.Thread(target=work, daemon=True).start()


def largest_changed_planes(base: dict, base_by_name: dict, variant_blob: bytes, spec: dict):
    """Byte planes of the biggest tensor that actually changed — the explainer data."""
    variant = st_parse(variant_blob)
    best = None
    for e in variant["entries"]:
        peer = base_by_name.get(e["name"])
        if not peer or peer["dtype"] != e["dtype"] or peer["shape"] != e["shape"]:
            continue
        vb = variant["data"][e["start"] : e["end"]]
        bb = base["data"][peer["start"] : peer["end"]]
        if bb == vb:
            continue
        if best is None or len(vb) > len(best[0]):
            best = (vb, bb, e)
    if best is None:
        return None
    vb, bb, e = best
    return {"tensor": e["name"], "rows": plane_report(bb, vb, e["itemsize"])}


def fmt_bytes(n) -> str:
    if n is None:
        return "—"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.2f} {unit}"
        value /= 1024
    return f"{value:,.2f} TiB"


# ─────────────────────────────────────────────────────────────────────────────
# The console — HTML, CSS and JS, embedded so this stays one file
# ─────────────────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="en" dir="ltr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CCP-AI · lossless model delta storage</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%2337d39f'/%3E%3C/svg%3E">
<style>
:root{--bg:#0b0f16;--panel:#121826;--panel2:#172033;--line:#24304a;--text:#e8edf7;--muted:#8d9bb8;
--accent:#37d39f;--accent2:#1d7a5e;--warn:#f0a441;--danger:#f2626b;--base:#52607d;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
@media(prefers-color-scheme:light){:root{--bg:#f4f6fb;--panel:#fff;--panel2:#f0f3f9;--line:#dde3ee;
--text:#131a26;--muted:#5d6b85;--accent:#0f9c72;--accent2:#0b7a59;--base:#97a3b8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.5}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 28px;
border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:14px}
.mark{display:grid;place-items:center;width:46px;height:46px;border-radius:12px;
background:linear-gradient(140deg,var(--accent),var(--accent2));color:#04120d;font:700 15px/1 var(--mono)}
h1{margin:0;font-size:19px}.tagline{margin:0;color:var(--muted);font-size:13px}
.pill{padding:4px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font:12px/1.6 var(--mono)}
main{max-width:1180px;margin:0 auto;padding:24px 20px 64px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:20px}
.panel h2{margin:0 0 4px;font-size:17px}.panel .sub{margin:0 0 16px;color:var(--muted);font-size:13.5px;max-width:80ch}
.switch-row{display:grid;grid-template-columns:1fr auto;align-items:center;gap:18px 24px}
.switch-row h2{font-size:22px}#switchState{margin:4px 0 0;color:var(--muted);font-size:14px}
.note{grid-column:1/-1;margin:0;color:var(--muted);font-size:13px;max-width:84ch}
.switch{position:relative;width:168px;height:62px;border:1px solid var(--line);border-radius:999px;
background:var(--panel2);cursor:pointer;padding:0;transition:background .22s}
.switch[aria-checked=true]{background:linear-gradient(120deg,var(--accent2),var(--accent));border-color:transparent}
.switch:disabled{opacity:.55;cursor:progress}
.knob{position:absolute;top:5px;inset-inline-start:5px;width:50px;height:50px;border-radius:50%;background:#fff;
box-shadow:0 6px 16px rgba(0,0,0,.35);transition:transform .24s cubic-bezier(.22,1,.36,1)}
.switch[aria-checked=true] .knob{transform:translateX(106px)}
.stext{position:absolute;top:50%;transform:translateY(-50%);font:700 13px/1 var(--mono);letter-spacing:1px}
.stext.off{inset-inline-end:22px;color:var(--muted)}.stext.on{inset-inline-start:22px;color:#04120d;opacity:0}
.switch[aria-checked=true] .stext.off{opacity:0}.switch[aria-checked=true] .stext.on{opacity:1}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:24px 0 18px}
.metric{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px 16px;
display:flex;flex-direction:column;gap:4px}
.metric span.l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.6px}
.metric strong{font:600 24px/1.2 var(--sans);font-variant-numeric:tabular-nums}
.metric.big strong{font-size:30px}.metric.acc strong{color:var(--accent)}
.metric .s{color:var(--muted);font:12px/1.4 var(--mono)}
.ok{color:var(--accent)}.bad{color:var(--danger)}
.bars{display:grid;gap:10px;margin-bottom:20px}
.brow{display:grid;grid-template-columns:120px 1fr 108px;align-items:center;gap:12px}
.btag{color:var(--muted);font-size:12.5px}
.bar{height:20px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.fill{height:100%;width:0;border-radius:5px;transition:width .5s cubic-bezier(.22,1,.36,1)}
.fill.f1{background:var(--base)}.fill.f2{background:linear-gradient(90deg,var(--accent2),var(--accent))}
.bval{font:12.5px/1 var(--mono);color:var(--muted);text-align:right}
.actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
button{font:500 13.5px/1 var(--sans);padding:10px 16px;border-radius:9px;border:1px solid var(--line);
background:var(--panel2);color:var(--text);cursor:pointer}
button:hover:not(:disabled){filter:brightness(1.12)}button:disabled{opacity:.5;cursor:not-allowed}
button.primary{background:linear-gradient(120deg,var(--accent2),var(--accent));color:#04120d;
border-color:transparent;font-weight:600}
button.ghost{background:transparent;color:var(--muted)}
#note{color:var(--muted);font-size:12.5px}
#job{margin-top:18px;background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.jhead{display:flex;justify-content:space-between;font:13px/1.4 var(--mono);color:var(--muted);margin-bottom:8px}
.prog{height:8px;background:var(--bg);border-radius:4px;overflow:hidden}
.prog>div{height:100%;width:0;background:linear-gradient(90deg,var(--accent2),var(--accent));transition:width .3s}
#jobSteps{margin:10px 0 0;padding:0;list-style:none;max-height:130px;overflow-y:auto;
font:12px/1.7 var(--mono);color:var(--muted)}
#switchResult{margin-top:16px;padding:12px 14px;border-radius:10px;border:1px solid var(--accent2);
font:13px/1.6 var(--mono)}
.hidden{display:none!important}.muted{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
td.n,th.n{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
tbody tr:hover{background:var(--panel2)}
.vname{display:flex;flex-direction:column;gap:2px;white-space:normal}
.vname small{color:var(--muted);font:11.5px/1.4 var(--mono)}
.kind{font:11.5px/1.6 var(--mono);color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:2px 8px}
.save{color:var(--accent);font-weight:600}
.comp{display:flex;height:14px;width:150px;border-radius:4px;overflow:hidden;border:1px solid var(--line)}
.comp i{display:block;height:100%}.c1{background:var(--accent)}.c2{background:var(--warn)}.c3{background:var(--base)}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin-top:14px;color:var(--muted);font-size:12.5px}
.legend span{display:inline-flex;align-items:center;gap:7px}
.legend i{width:12px;height:12px;border-radius:3px}
.controls{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin-bottom:18px}
.controls label{display:flex;flex-direction:column;gap:6px;font-size:12.5px;color:var(--muted)}
.controls input{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:9px;
padding:9px 11px;font:14px/1 var(--mono);width:168px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h3{margin:0 0 12px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.crow{display:flex;justify-content:space-between;gap:12px;padding:6px 0;font-size:14px}
.crow strong{font-family:var(--mono);font-variant-numeric:tabular-nums}
.crow.tot{border-top:1px solid var(--line);margin-top:6px;padding-top:10px}.crow.tot strong{color:var(--accent)}
.cnote{margin:10px 0 0;color:var(--muted);font:11.5px/1.6 var(--mono)}
.card ul{margin:0;padding-left:18px;color:var(--muted);font-size:12.5px;line-height:1.8}
.prow{display:grid;grid-template-columns:150px 1fr 200px;align-items:center;gap:12px;margin-bottom:10px}
.plabel{color:var(--muted);font:12.5px/1.4 var(--mono)}
.ptrack{height:22px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;overflow:hidden}
.pfill{height:100%;background:linear-gradient(90deg,var(--accent2),var(--accent));transition:width .4s}
.pfill.dense{background:linear-gradient(90deg,var(--base),var(--warn))}
.pval{font:12px/1.4 var(--mono);color:var(--muted);text-align:right}
.vrow{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid var(--line);font-size:13px}
.vrow .r{font:12px/1.4 var(--mono)}
footer{color:var(--muted);font-size:12.5px;max-width:90ch}
</style></head><body>
<header>
  <div class="brand"><span class="mark">CCP</span>
    <div><h1>CCP-AI</h1><p class="tagline">Lossless base + delta storage for model weights</p></div></div>
  <div style="display:flex;gap:10px;align-items:center">
    <span class="pill" id="enginePill">standalone</span><span class="pill" id="modePill">—</span></div>
</header>
<main>

<section class="panel">
  <div class="switch-row">
    <div><h2>CCP savings</h2><p id="switchState">—</p></div>
    <button id="sw" class="switch" role="switch" aria-checked="false" type="button">
      <span class="knob"></span><span class="stext off">OFF</span><span class="stext on">ON</span></button>
    <p class="note">Flipping this migrates real bytes on disk. OFF writes full checkpoints back out;
      ON re-encodes them as verified deltas. Nothing here is a display setting.</p>
  </div>

  <div class="metrics">
    <div class="metric big"><span class="l">On disk now</span><strong id="mNow">—</strong>
      <span class="s" id="mNowCost">—</span></div>
    <div class="metric"><span class="l">Registry baseline</span><strong id="mFull">—</strong>
      <span class="s" id="mFullCost">—</span></div>
    <div class="metric acc"><span class="l">Fleet saving (measured)</span><strong id="mSave">—</strong>
      <span class="s" id="mSaveB">—</span></div>
    <div class="metric"><span class="l">Reconstruction</span><strong id="mLoss">not checked</strong>
      <span class="s" id="mLossS">—</span></div>
  </div>

  <div class="bars">
    <div class="brow"><span class="btag">full copies</span><div class="bar"><div class="fill f1" id="bFull"></div></div>
      <span class="bval" id="bFullV">—</span></div>
    <div class="brow"><span class="btag">CCP deltas</span><div class="bar"><div class="fill f2" id="bCcp"></div></div>
      <span class="bval" id="bCcpV">—</span></div>
  </div>

  <div class="actions">
    <button id="seedBtn" class="primary" type="button">Seed model family</button>
    <button id="verifyBtn" type="button">Verify losslessness</button>
    <button id="resetBtn" class="ghost" type="button">Reset</button>
    <span id="note"></span>
  </div>

  <div id="job" class="hidden">
    <div class="jhead"><span id="jobMsg">working…</span><span id="jobPct">0%</span></div>
    <div class="prog"><div id="jobBar"></div></div><ul id="jobSteps"></ul></div>
  <div id="switchResult" class="hidden"></div>
</section>

<section class="panel">
  <h2>Model family</h2>
  <p class="sub">Every row is a real checkpoint on disk. Savings are measured per variant, never modelled.</p>
  <div style="overflow-x:auto"><table>
    <thead><tr><th>Variant</th><th>Kind</th><th class="n">Full size</th><th class="n">gzip -6</th>
      <th class="n">CCP</th><th class="n">Saving</th><th class="n">vs gzip</th><th>Composition</th><th></th></tr></thead>
    <tbody id="vbody"><tr><td colspan="9" class="muted" style="text-align:center;padding:22px">
      Nothing stored yet — seed the model family.</td></tr></tbody></table></div>
  <div class="legend"><span><i class="c1"></i>copied from base (0 bytes)</span>
    <span><i class="c2"></i>delta encoded</span><span><i class="c3"></i>stored whole</span></div>
</section>

<section class="panel">
  <h2>Projection to production scale</h2>
  <p class="sub">The measured per-variant ratio above, applied to a fleet you choose. This is an extrapolation —
    the ratio is real, the fleet is hypothetical, and the unit prices are AWS S3 list.</p>
  <div class="controls">
    <label><span>Model size (B params)</span><input id="pParams" type="number" min="0.1" max="2000" step="0.5" value="7"></label>
    <label><span>Variants published</span><input id="pVariants" type="number" min="1" max="100000" value="10"></label>
    <label><span>Downloads per variant</span><input id="pDl" type="number" min="1" step="1000" value="100000"></label>
    <button id="pBtn" type="button">Recalculate</button>
  </div>
  <div class="cards">
    <div class="card"><h3>Egress — one release cycle</h3>
      <div class="crow"><span>Today</span><strong id="eBase">—</strong></div>
      <div class="crow"><span>With CCP</span><strong id="eCcp">—</strong></div>
      <div class="crow tot"><span>Saved</span><strong id="eSaved">—</strong></div>
      <p class="cnote" id="eBytes">—</p></div>
    <div class="card"><h3>Storage — per month</h3>
      <div class="crow"><span>Today</span><strong id="sBase">—</strong></div>
      <div class="crow"><span>With CCP</span><strong id="sCcp">—</strong></div>
      <div class="crow tot"><span>Saved</span><strong id="sSaved">—</strong></div>
      <p class="cnote" id="sBytes">—</p></div>
    <div class="card"><h3>Assumptions</h3><ul id="pAssume"><li>—</li></ul></div>
  </div>
</section>

<section class="panel">
  <h2>Why it compresses</h2>
  <p class="sub">A fine-tune moves a weight by a fraction of its value. In IEEE-754 that leaves the sign, the
    exponent and the top mantissa bits untouched. Split the residual into byte planes and the redundancy becomes
    visible — and compressible. Below: the actual planes of the largest changed tensor.</p>
  <div id="planes"><p class="muted">Seed the family to see the byte planes.</p></div>
</section>

<section class="panel">
  <h2>Lossless verification</h2>
  <p class="sub">Each variant is rebuilt from its container and hashed against the digest recorded when it was
    pushed. Bit-exact or it fails.</p>
  <div id="verifyBox"><p class="muted">Not run yet.</p></div>
</section>

<footer><p>Numbers on this page come from <code>os.stat</code> on real files and from containers built and
verified in process. Figures labelled <em>projection</em> apply a measured ratio to a hypothetical fleet.</p></footer>
</main>
<script>
const $=id=>document.getElementById(id);
const U=['B','KiB','MiB','GiB','TiB','PiB'];
function fb(n){if(n==null)return '—';let v=Number(n),u=0;
  while(Math.abs(v)>=1024&&u<U.length-1){v/=1024;u++}
  const d=u===0?0:(v>=100?1:2);return v.toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d})+' '+U[u]}
function fu(n){if(n==null)return '—';const v=Number(n);
  if(Math.abs(v)>=1e6)return '$'+(v/1e6).toFixed(2)+'M';
  if(Math.abs(v)>=1e3)return '$'+(v/1e3).toFixed(1)+'K';
  if(v!==0&&Math.abs(v)<0.01)return '$'+v.toFixed(5);return '$'+v.toFixed(2)}
function fp(r,d=1){return r==null?'—':(r*100).toFixed(d)+'%'}
function el(t,c,x){const n=document.createElement(t);if(c)n.className=c;if(x!=null)n.textContent=String(x);return n}
function clear(n){while(n.firstChild)n.removeChild(n.firstChild)}
async function api(p,o={}){const r=await fetch(p,{headers:{'Content-Type':'application/json'},...o});
  const j=await r.json().catch(()=>({error:'HTTP '+r.status}));
  if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
function note(m,bad){const n=$('note');n.textContent=m||'';n.style.color=bad?'var(--danger)':'var(--muted)'}
let LAST=null,POLL=null;

function render(s){
  LAST=s;const r=s.repository,on=r.mode==='ccp';
  $('modePill').textContent='mode '+r.mode;
  $('sw').setAttribute('aria-checked',String(on));
  $('switchState').textContent=on?'ON — variants stored as verified deltas':'OFF — full checkpoints on disk';
  $('mNow').textContent=fb(r.disk_bytes);
  $('mNowCost').textContent=fu(s.costs.now)+' / month at S3 list';
  $('mFull').textContent=fb(r.disk_if_full);
  $('mFullCost').textContent=fu(s.costs.if_full)+' / month at S3 list';
  $('mSave').textContent=fp(r.variant_savings);
  $('mSaveB').textContent=r.variant_count?fb(r.disk_if_full-r.disk_if_ccp)+' of '+fb(r.disk_if_full):'—';
  const m=Math.max(r.disk_if_full||1,r.disk_if_ccp||1);
  $('bFull').style.width=((r.disk_if_full||0)/m*100)+'%';
  $('bCcp').style.width=((r.disk_if_ccp||0)/m*100)+'%';
  $('bFullV').textContent=fb(r.disk_if_full);$('bCcpV').textContent=fb(r.disk_if_ccp);
  renderVariants(r.variants||[]);renderProjection(s.projection);renderJob(s.job);
  const first=(r.variants||[]).find(v=>v.planes);
  if(first)renderPlanes(first);
}

function renderVariants(vs){
  const b=$('vbody');clear(b);
  if(!vs.length){const tr=el('tr');const td=el('td','muted','Nothing stored yet — seed the model family.');
    td.colSpan=9;td.style.textAlign='center';td.style.padding='22px';tr.appendChild(td);b.appendChild(tr);return}
  for(const v of vs){
    const tr=el('tr');
    const c1=el('td'),w=el('div','vname');w.appendChild(el('span',null,v.label));
    w.appendChild(el('small',null,v.id));c1.appendChild(w);tr.appendChild(c1);
    const c2=el('td');c2.appendChild(el('span','kind',v.kind));tr.appendChild(c2);
    tr.appendChild(el('td','n',fb(v.raw_bytes)));
    tr.appendChild(el('td','n',fb(v.gzip_bytes)));
    tr.appendChild(el('td','n',fb(v.container_bytes)));
    tr.appendChild(el('td','n save',fp(v.savings_ratio)));
    tr.appendChild(el('td','n',fp(v.savings_vs_gzip)));
    const c8=el('td'),st=v.stats||{};
    const parts=[['c1',st.bytes_copied||0],['c2',st.bytes_delta_raw||0],['c3',st.bytes_whole_raw||0]];
    const tot=parts.reduce((a,p)=>a+p[1],0)||1;
    const bar=el('div','comp');
    bar.title='copied '+fb(parts[0][1])+' · delta '+fb(parts[1][1])+' · whole '+fb(parts[2][1]);
    for(const [cls,val] of parts){if(val<=0)continue;const i=el('i',cls);i.style.width=(val/tot*100)+'%';bar.appendChild(i)}
    c8.appendChild(bar);tr.appendChild(c8);
    const c9=el('td'),btn=el('button','ghost','Planes');btn.type='button';
    btn.addEventListener('click',()=>renderPlanes(v));c9.appendChild(btn);tr.appendChild(c9);
    b.appendChild(tr);
  }
}

function renderPlanes(v){
  const box=$('planes');clear(box);
  if(!v.planes){box.appendChild(el('p','muted','No shared tensor changed in this variant.'));return}
  box.appendChild(el('p','muted',v.label+' · '+v.planes.tensor));
  const rows=v.planes.rows,mx=Math.max(...rows.map(r=>r.stored_bytes))||1;
  for(const r of [...rows].reverse()){
    const row=el('div','prow');
    row.appendChild(el('span','plabel','byte '+r.plane+' · '+r.role));
    const tr=el('div','ptrack'),f=el('div',(r.stored_bytes/r.raw_bytes)>0.5?'pfill dense':'pfill');
    f.style.width=(r.stored_bytes/mx*100)+'%';tr.appendChild(f);row.appendChild(tr);
    row.appendChild(el('span','pval',fb(r.stored_bytes)+' / '+fb(r.raw_bytes)+' · '+
      (r.zero_fraction==null?'—':fp(r.zero_fraction,0))+' zero bytes'));
    box.appendChild(row);
  }
}

function renderProjection(p){
  if(!p)return;const a=p.assumptions;
  $('eBase').textContent=fu(p.egress.baseline_usd);$('eCcp').textContent=fu(p.egress.ccp_usd);
  $('eSaved').textContent=fu(p.egress.saved_usd)+' · '+fp(p.savings_ratio);
  $('eBytes').textContent=fb(p.egress.baseline_bytes)+' → '+fb(p.egress.ccp_bytes);
  $('sBase').textContent=fu(p.storage.baseline_usd_month);$('sCcp').textContent=fu(p.storage.ccp_usd_month);
  $('sSaved').textContent=fu(p.storage.saved_usd_month)+' / mo · '+fu(p.storage.saved_usd_month*12)+' / yr';
  $('sBytes').textContent=fb(p.storage.baseline_bytes)+' → '+fb(p.storage.ccp_bytes);
  const ul=$('pAssume');clear(ul);
  [a.params_billions+'B params × 2 bytes = '+fb(a.model_bytes)+' per checkpoint',
   a.variants+' variants × '+a.downloads_per_variant.toLocaleString()+' downloads',
   'measured delta ratio '+fp(a.ratio,2)+' of full size',
   'egress $'+a.egress_usd_per_gib+'/GiB · storage $'+a.storage_usd_per_gib_month+'/GiB-month',
   'base fetched once per client, then one delta per variant'].forEach(t=>ul.appendChild(el('li',null,t)));
}

function renderJob(j){
  const box=$('job');
  if(!j||j.state==='idle'){box.classList.add('hidden');return}
  const settled=j.state!=='running';
  if(settled&&j.age>12){box.classList.add('hidden');return}
  box.classList.remove('hidden');
  $('jobMsg').textContent=j.error||j.message||j.state;
  $('jobPct').textContent=Math.round((j.fraction||0)*100)+'% · '+Math.round(j.elapsed||0)+'s';
  $('jobBar').style.width=((j.fraction||0)*100)+'%';
  const ul=$('jobSteps');clear(ul);(j.steps||[]).forEach(s=>ul.appendChild(el('li',null,s)));
}

function busy(b){['seedBtn','verifyBtn','resetBtn','sw','pBtn'].forEach(i=>$(i).disabled=b)}
async function refresh(){try{const s=await api('/api/state');render(s);return s}catch(e){note(e.message,1);return null}}
function poll(){if(POLL)return;POLL=setInterval(async()=>{
  const j=await api('/api/job').catch(()=>null);if(!j)return;renderJob(j);
  if(j.state!=='running'){clearInterval(POLL);POLL=null;busy(false);
    note(j.state==='failed'?j.error:'');await refresh()}},500)}

$('sw').addEventListener('click',async()=>{
  if(!LAST||!LAST.repository.seeded){note('Seed the family first.',1);return}
  const enabled=$('sw').getAttribute('aria-checked')!=='true';
  busy(true);$('switchState').textContent='migrating bytes on disk…';
  try{const r=await api('/api/mode',{method:'POST',body:JSON.stringify({enabled})});
    render(r.state);const s=r.switch,box=$('switchResult');box.classList.remove('hidden');
    box.textContent='migrated '+s.migrated+' variants in '+s.seconds+'s · '+
      fb(s.disk_bytes_before)+' → '+fb(s.disk_bytes_after);note('')}
  catch(e){note(e.message,1);await refresh()}finally{busy(false)}});

$('seedBtn').addEventListener('click',async()=>{busy(true);
  note('generating real checkpoints — real arithmetic, not a spinner');
  try{renderJob(await api('/api/seed',{method:'POST',body:'{}'}));poll()}
  catch(e){note(e.message,1);busy(false)}});

$('verifyBtn').addEventListener('click',async()=>{
  if(!LAST||!LAST.repository.seeded){note('Seed the family first.',1);return}
  busy(true);const box=$('verifyBox');clear(box);box.appendChild(el('p','muted','…'));
  try{const rep=await api('/api/verify',{method:'POST',body:'{}'});clear(box);
    box.appendChild(el('p',rep.all_lossless?'ok':'bad',
      rep.all_lossless?rep.checked+' variants rebuilt and hash-matched in '+rep.total_seconds+'s'
      :'one or more variants did not rebuild bit-exactly'));
    for(const r of rep.results){const row=el('div','vrow');row.appendChild(el('span',null,r.label));
      row.appendChild(el('span','r '+(r.lossless?'ok':'bad'),r.lossless
        ?'bit-exact · '+fb(r.bytes)+' in '+r.seconds+'s'+(r.mib_s?' · '+r.mib_s+' MiB/s':'')
        :'FAILED'+(r.error?' · '+r.error:'')));box.appendChild(row)}
    $('mLoss').textContent=rep.all_lossless?'bit-exact':'FAILED';
    $('mLoss').className=rep.all_lossless?'ok':'bad';
    $('mLossS').textContent=rep.checked+' variants · '+rep.total_seconds+'s · mode '+rep.mode}
  catch(e){clear(box);box.appendChild(el('p','muted',e.message))}finally{busy(false)}});

$('resetBtn').addEventListener('click',async()=>{busy(true);
  try{render(await api('/api/reset',{method:'POST',body:'{}'}));
    clear($('verifyBox'));$('verifyBox').appendChild(el('p','muted','Not run yet.'));
    $('mLoss').textContent='not checked';$('mLoss').className='';$('mLossS').textContent='—';
    $('switchResult').classList.add('hidden');
    clear($('planes'));$('planes').appendChild(el('p','muted','Seed the family to see the byte planes.'))}
  catch(e){note(e.message,1)}finally{busy(false)}});

$('pBtn').addEventListener('click',async()=>{
  try{renderProjection(await api('/api/projection',{method:'POST',body:JSON.stringify({
    params_billions:Number($('pParams').value),variants:Number($('pVariants').value),
    downloads_per_variant:Number($('pDl').value)})}));note('')}
  catch(e){note(e.message,1)}});

refresh().then(s=>{if(s&&s.job&&s.job.state==='running'){busy(true);poll()}});
</script></body></html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# HTTP layer — transport only, no compression logic
# ─────────────────────────────────────────────────────────────────────────────

STORE_OBJ: Store


def state_payload() -> dict:
    summary = STORE_OBJ.summary()
    ratio = summary["variant_ratio"]
    with JOB_LOCK:
        job = dict(JOB)
    job["elapsed"] = (time.time() - job["started"]) if job["started"] else 0
    job["age"] = 0 if job["state"] == "running" else job["elapsed"]
    return {
        "repository": summary,
        "costs": {
            "now": (summary["disk_bytes"] / GIB) * STORAGE_USD_PER_GIB_MONTH,
            "if_full": (summary["disk_if_full"] / GIB) * STORAGE_USD_PER_GIB_MONTH,
            "if_ccp": (summary["disk_if_ccp"] / GIB) * STORAGE_USD_PER_GIB_MONTH,
        },
        "projection": project(ratio, 7.0, 10, 100_000) if ratio else None,
        "job": job if job["state"] != "idle" else None,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ccp-ai-standalone"
    protocol_version = "HTTP/1.1"

    def _send(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._sec()
        self.end_headers()
        self.wfile.write(body)

    def _sec(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        # The page loads no third-party anything, so the policy can be strict.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length is not a number")
        if n > 8192:
            raise ValueError("request body too large")
        if n <= 0:
            return {}
        parsed = json.loads(self.rfile.read(n).decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {fmt % args}\n")

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._sec()
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/state":
                return self._send(HTTPStatus.OK, state_payload())
            if self.path == "/api/job":
                with JOB_LOCK:
                    job = dict(JOB)
                job["elapsed"] = (time.time() - job["started"]) if job["started"] else 0
                job["age"] = 0 if job["state"] == "running" else job["elapsed"]
                return self._send(HTTPStatus.OK, job)
            self._send(HTTPStatus.NOT_FOUND, {"error": "no such endpoint"})
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._body()
            if self.path == "/api/seed":
                seed_job(STORE_OBJ)
                return self._send(HTTPStatus.ACCEPTED, dict(JOB))
            if self.path == "/api/mode":
                if not isinstance(body.get("enabled"), bool):
                    raise ValueError('body must be {"enabled": true|false}')
                if JOB["state"] == "running":
                    raise RuntimeError("a job is running; wait for it to finish")
                switch = STORE_OBJ.set_mode("ccp" if body["enabled"] else "full")
                return self._send(HTTPStatus.OK, {"switch": switch, "state": state_payload()})
            if self.path == "/api/verify":
                return self._send(HTTPStatus.OK, STORE_OBJ.verify_all())
            if self.path == "/api/reset":
                STORE_OBJ.reset()
                return self._send(HTTPStatus.OK, state_payload())
            if self.path == "/api/projection":
                ratio = STORE_OBJ.summary()["variant_ratio"]
                if not ratio:
                    raise ValueError("nothing measured yet — seed the repository first")
                return self._send(HTTPStatus.OK, project(
                    ratio,
                    max(0.1, min(2000.0, float(body.get("params_billions", 7)))),
                    max(1, min(100_000, int(body.get("variants", 10)))),
                    max(1, min(10**9, int(body.get("downloads_per_variant", 100_000)))),
                ))
            self._send(HTTPStatus.NOT_FOUND, {"error": "no such endpoint"})
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        if isinstance(exc, BrokenPipeError):
            return
        if isinstance(exc, (ValueError, RuntimeError, KeyError, OSError, zlib.error)):
            # Expected refusal. The message is safe; a stack trace would not be.
            return self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        import traceback
        traceback.print_exc()
        self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})


def main() -> int:
    global STORE_OBJ
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    STORE_OBJ = Store(STORE)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    print(f"CCP-AI console   http://127.0.0.1:{port}")
    print(f"store            {STORE_OBJ.root}")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
