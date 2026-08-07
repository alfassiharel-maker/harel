# BitEngine — L1, the block engine

**Step 1 of the CCP tool. This is the innermost tier only.** L2 (goal → algorithm
matching) and L3 (the representation buffer) are not built yet, and nothing here
reads a file, holds state across blocks or talks to a user.

| File | Purpose |
| --- | --- |
| `l1.py` | The engine: residual statistics, the cost model, five codecs, encode, decode, savings. |
| `bench_l1.py` | Regenerates the table below. Every row round-trips before it is printed. |
| `tests/test_l1.py` | Anchor cases, the undefined case, hand-computed costs, round-trips, malformed input. |

Standard library only, and no imports from the rest of this repository.

```bash
cd bitengine
python3 -m unittest discover -s tests -t .   # 46 tests
python3 bench_l1.py --size 16MB --block 64KB
```

## The one design decision

L1 is not one delta format. It is five codecs, costed in closed form from three
statistics of the XOR residual, with the cheapest chosen per block.

| Codec | Payload | Cost in bytes | Wins when |
| --- | --- | --- | --- |
| `identical` | none | 1 | nothing changed |
| `sparse` | count, positions, values | `1 + w + k(w+1)` | few changes |
| `runs` | count, (gap, length)*, values | `1 + w + 2wr + k` | changes are clustered |
| `bitmap` | one bit per byte, then values | `1 + n/8 + k` | many scattered changes |
| `raw` | the block verbatim | `1 + n` | nothing else pays |

`n` = block size, `k` = changed bytes, `r` = runs of changed bytes, `w` = bytes
needed to address the block.

That structure is a response to a measured failure, not a preference.
`experiments/ccp` implements the `sparse` row alone, and
[`experiments/ccp/FINDINGS.md`](../experiments/ccp/FINDINGS.md) §2 records where
it breaks:

> a delta pays a fixed ~3 bytes for every changed byte, so cost grows linearly
> with the number of changes and crosses break-even at roughly a third of the
> region [...] At 10% divergence LZMA still returns 48.64% while CCP collapses
> to 1.22%.

A sparse entry carries its own position, so it stops paying at `k/n = 1/3`. A
bitmap carries position implicitly at one bit per byte, so it stops paying at
`k/n = 7/8`. Choosing between them per block costs one byte and moves the cliff
from **33.3% to 87.5%** of a block changed. Both figures are asserted in
`tests/test_l1.py:BreakEvenBoundary` against the cost model, not quoted.

`raw` is always available, so a block can never encode to more than `n + 1`
bytes. That bound is why the engine is safe to point at arbitrary data.

## Measured

`python3 bench_l1.py --size 16MB --block 64KB`, 8 cores, CPython 3.11. Every row
was decoded and compared against the input by SHA-256 before being printed.
`sparse-only` is what a single-codec delta would have stored for the same blocks
— the column this codec set exists to beat.

| shape | changed | codec | saving | sparse-only | encode | decode |
| --- | --- | --- | --- | --- | --- | --- |
| clustered | 0.1% | runs | 99.88% | 99.60% | 51 MB/s | 86 MB/s |
| clustered | 1% | runs | 98.97% | 96.01% | 50 MB/s | 332 MB/s |
| clustered | 10% | runs | **89.79%** | 60.15% | 54 MB/s | 347 MB/s |
| clustered | 25% | runs | **74.50%** | 0.38% | 57 MB/s | 272 MB/s |
| clustered | 40% | runs | **59.21%** | −0.00% | 59 MB/s | 241 MB/s |
| clustered | 70% | runs | **28.63%** | −0.00% | 66 MB/s | 180 MB/s |
| scattered | 1% | sparse | 96.00% | 96.00% | 56 MB/s | 169 MB/s |
| scattered | 10% | bitmap | **77.50%** | 60.00% | 77 MB/s | 69 MB/s |
| scattered | 25% | bitmap | **62.50%** | −0.00% | 81 MB/s | 44 MB/s |
| scattered | 40% | bitmap | **47.50%** | −0.00% | 78 MB/s | 31 MB/s |
| scattered | 95% | raw | −0.00% | −0.00% | 132 MB/s | 1953 MB/s |

`scattered` spreads every change as far from its neighbours as the block allows.
It is constructed to be the worst possible input for run-based coding, not a
realistic one; real edits are clustered. The last row is the negative control:
data with no exploitable structure must not compress, and −0.00% is the one-byte
codec tag.

## Two percentages, deliberately kept apart

The brief specifies savings as `(change size / total size) * 100`. That quantity
is reported as `Savings.change_pct`, but it is **not** the saving. It is a
property of the data and assumes the delta is free.

`Savings.saving_pct` is `(1 − encoded / total) * 100` — what the disk actually
sees, after the representation has been paid for. It is always the smaller
number, `overhead_bytes` is the gap, and it is the only one of the two that may
be shown to a user. `tests/test_l1.py` asserts `saving_pct <= 100 − change_pct`
across the whole range of `k`.

Both are `None`, never `0.0`, when there is no input to measure.

## Guardrails

* **No unbounded loop.** Every loop is a regex scan over a block whose length was
  checked against `MAX_BLOCK_BYTES` (4 MiB) on entry, or a `range()` over a count
  validated against the block length before use.
* **No allocation from unvalidated input.** Decoders check every count and offset
  against the block length before allocating. `CorruptBlock` is raised rather
  than a partial block returned — 15 malformed-input tests cover truncation,
  out-of-range positions, impossible counts, bits set past the end of the block,
  non-ascending positions, zero deltas and trailing bytes.
* **O(n) per block**, one pass for statistics and one to encode. `residual_stats`
  contains no Python-level loop at all: changed bytes, changed bits and run count
  are each one big-integer operation over the whole block.
* **The cost model is checked on every block, not in tests only.** `encode_block`
  raises if the bytes it produced differ from the size the model predicted, so a
  divergence surfaces as a failure rather than as a wrong savings figure.

## Known limits, for the Step 2 conversation

1. **Bitmap decode degrades on scattered data** — 31 MB/s at 40% scattered
   against 241 MB/s for the clustered equivalent. Placing values at isolated
   positions is inherently sequential and CPython has no vectorised scatter. Two
   strategies are implemented and dispatched on run density, which is worth 4–6×
   on this case, but it remains the slowest path. Whether to trade saving for
   decode speed is an L2 policy decision, which is why `BlockPlan.costs` exposes
   every codec's cost and not just the winner.
2. **Blocks are fixed-size and offset-aligned.** Content shifted by one byte is
   invisible. This is the same limitation `FINDINGS.md` §8.2 records for CCP;
   content-defined chunking is the standard fix and is not implemented.
3. **L1 compares one block against one base.** Choosing *which* base — and
   whether a block should be compared against a base at all — is L2's job.
4. **No entropy coding.** `FINDINGS.md` §1 shows the redundancy in fp32 weights
   lives in the marginal byte distribution, which only an entropy coder reaches.
   L1 is blind to it by construction, and the two are complementary rather than
   alternatives.
