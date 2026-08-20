# CCP container format — version 1

Status: **implemented**. This document is authoritative for the on-disk layout;
`dataeng/src/format.rs` implements it and `dataeng/src/format.rs` tests assert
the constants here.

All integers are **little-endian**. All offsets are byte offsets from the start
of the file unless stated otherwise. One container holds exactly one version of
one artifact.

A format mistake outlives the code that wrote it, so `format_version` is present
from byte 8 and every reader must reject a version it does not understand rather
than guess.

## Layout

```
+--------------------------------+  0
| Header (128 bytes)             |
+--------------------------------+  header_size
| Block index (32 bytes x N)     |
+--------------------------------+  payload_offset
| Payloads (variable)            |
+--------------------------------+  footer_offset
| Footer (40 bytes)              |
+--------------------------------+  EOF
```

## Header — 128 bytes

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 8 | `magic` | `CCPFMT\0\0` |
| 8 | 2 | `format_version` | 1 |
| 10 | 2 | `header_size` | 128 |
| 12 | 4 | `flags` | reserved, must be 0 |
| 16 | 8 | `block_size` | logical block size used when writing |
| 24 | 8 | `original_size` | exact byte length of the reconstructed artifact |
| 32 | 8 | `block_count` | number of block index entries |
| 40 | 8 | `base_size` | byte length of the base artifact; 0 when this version is a root |
| 48 | 16 | `version_id` | this version's id |
| 64 | 16 | `base_id` | base version's id; all-zero means **root** (no base) |
| 80 | 32 | `content_sha256` | SHA-256 of the *reconstructed artifact*, recorded at ingest |
| 112 | 8 | `index_offset` | always equals `header_size` in v1 |
| 120 | 8 | `payload_offset` | start of the payload region |

A root container (`base_id` all-zero) must contain only `FULL` blocks: it has no
base to delta against, and this is what makes it a self-sufficient
reconstruction root.

## Block index entry — 32 bytes

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | `kind` | see below |
| 1 | 3 | `reserved` | must be 0 |
| 4 | 4 | `payload_len` | bytes stored for this block |
| 8 | 8 | `payload_off` | offset **relative to `payload_offset`** |
| 16 | 4 | `logical_len` | bytes this block contributes to the artifact |
| 20 | 4 | `changed_bits` | popcount of `base XOR target` for this block |
| 24 | 4 | `changed_bytes` | count of differing bytes |
| 28 | 4 | `reserved2` | must be 0 |

`changed_bits` / `changed_bytes` are retained deliberately: they are the
measurements the representation decision was made from, so a stored version can
explain why it chose what it chose (product goal 4) without recomputing anything.

### Block kinds

| Value | Kind | Payload | Reconstruction |
|---|---|---|---|
| 0 | `FULL` | the target block verbatim (`payload_len == logical_len`) | copy |
| 1 | `DELTA_RAW` | `base_block XOR target_block`, verbatim | `target = base XOR payload` |
| 2 | `DELTA_SPARSE` | sparse encoding of the XOR (below) | apply listed bytes onto a copy of base |
| 3 | `IDENTICAL` | empty (`payload_len == 0`) | `target = base` |

Only `FULL` is valid when the block has no corresponding base block.

### `DELTA_SPARSE` payload encoding

```
u32  count                 number of differing bytes
count x {
    u32 offset             offset within the block
    u8  xor_value          base[offset] XOR target[offset], always non-zero
}
```

Encoded size is `4 + 5 * count`. This is the cost the strategy engine prices
against `logical_len` — a sparse delta only wins when the change is sparse
enough that 5 bytes per differing byte still beats storing the block.

## Footer — 40 bytes

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 8 | `magic` | `CCPEND\0\0` |
| 8 | 32 | `container_sha256` | SHA-256 of every byte of the file before the footer |

The footer detects a truncated or corrupted container independently of whether
the reconstructed content hash matches. Both are checked; neither substitutes for
the other.

## Unequal artifact lengths

XOR is defined only for equal lengths, so the policy is explicit rather than
implicit (there is no padding and no truncation anywhere in the writer):

For block `i` the base slice is `base[i*bs .. min((i+1)*bs, base_size))` and the
target slice is `target[i*bs .. min((i+1)*bs, original_size))`. If the two slices
differ in length — including when the base slice is empty because the target is
longer — the block is **not delta-eligible and is stored `FULL`**. Trailing base
bytes beyond the target's length are simply not referenced.

This means a version that grows or shrinks stores the affected tail block in
full, and every whole block before it can still be a delta. It also means an
insertion near the start makes every subsequent block differ, which is the
position-alignment limit recorded in `../CLAUDE.md` §23.2 — a real property of
the design, not a bug in the format.
