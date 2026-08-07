# BitEngine (CCP) — bit-level delta optimisation

A local, dependency-free CLI that stores a file as the bit-level difference from
something it already resembles: a previous frame, a previous checkpoint, a fixed
reference block.

```bash
cd bitengine
streamlit run app.py                       # the dashboard (needs streamlit)
python3 bundle.py --with-ui                # -> dist/bitengine.pyz, one file, ~77 KB
python3 cli.py goals                       # what the engine knows how to do
python3 cli.py probe  video.bin            # measure candidate goals, rank them
python3 cli.py pack   video.bin out.bite   # --auto by default
python3 cli.py unpack out.bite restored.bin
python3 cli.py inspect out.bite

python3 -m unittest discover -s tests -t .  # 202 tests
python3 bench_l1.py --size 16MB --block 64KB
```

The engine (`l1`, `l2`, `l3`) and the CLI use the **standard library only** and
import nothing from the rest of this repository — that is what lets the bundled
`.pyz` run anywhere with no install. The dashboard needs `streamlit` and
`pandas`, and `zstandard` is optional; see [INSTALL.md](INSTALL.md).

## The three tiers

| Tier | File | Question it answers |
| --- | --- | --- |
| **L1** | `l1.py` | Given this base and this block, what is the cheapest representation? |
| **L2** | `l2.py` | Which base, which block size, which codecs? |
| **L3** | `l3.py` | Where does it live, and how is block *i* served without replaying the rest? |
| — | `cli.py` | Command line: goals, probe, pack, unpack, inspect. |
| — | `webui.py` | Dashboard logic — real engine calls, no framework, fully tested. |
| — | `app.py` | Thin Streamlit layer over `webui.py`. |
| — | `bundle.py` | Packaging. See [INSTALL.md](INSTALL.md). |

### L1 — five codecs, not one delta format

Costed in closed form from three statistics of the XOR residual; only the winner
is encoded.

| Codec | Cost in bytes | Wins when |
| --- | --- | --- |
| `identical` | 1 | nothing changed |
| `sparse` | `1 + w + k(w+1)` | few changes |
| `runs` | `1 + w + 2wr + k` | changes are clustered |
| `bitmap` | `1 + n/8 + k` | many scattered changes |
| `raw` | `1 + n` | nothing else pays |

`n` = block size, `k` = changed bytes, `r` = runs of changed bytes, `w` = bytes
to address the block.

This structure answers a measured failure. `experiments/ccp` implements the
`sparse` row alone, and [`FINDINGS.md`](../experiments/ccp/FINDINGS.md) §2
records where it breaks: a sparse entry carries its own position, so it stops
paying at `k/n = 1/3`. A bitmap carries position implicitly, so it stops at
`k/n = 7/8`. Choosing per block costs one byte and moves the cliff from **33.3%
to 87.5%**. Both are asserted against the cost model in
`tests/test_l1.py:BreakEvenBoundary`.

`raw` is always available, so no block encodes to more than `n + 1` bytes.

### L2 — the controller, and where the saving is actually won

Three axes: **base strategy** (`PAIRED` a second stream, `PRECEDING` the block
*stride* earlier, `ANCHOR` block 0), **block size**, and **codec policy**.

`probe()` does not guess from the file extension. It runs a bounded sample of
the real data through each candidate at every block size in
`DEFAULT_BLOCK_SIZES` and ranks by what they actually saved. On a stream of 16KB
frames:

| block size | saving |
| --- | --- |
| 4 KB | **−0.02%** |
| 16 KB (one frame) | **82.04%** |
| 64 KB (four frames) | 45.94% |

An 82-point swing from block size alone, not monotonic, with the optimum at the
repeat period. The `video-frame-delta` goal's own default is 64 KB — so without
the sweep a user would silently accept 45.94%. That table is the argument for L2
existing.

### L3 — the container and the buffer

A `.bite` file is a header, the payloads, and a block index written last. The
index turns "decode block *i*" from a linear replay into a seek, and a bounded
LRU keeps reconstructed blocks so nearby access is free.

The honest limit is chain depth. `PAIRED` needs 1 decode, `ANCHOR` 2, and
`PRECEDING` needs *i/stride* — a linked list. `FINDINGS.md` §3 flags exactly
this and declines to claim random access as a result. `--keyframe-interval N`
stores every Nth block standalone and bounds the walk to N, and the cost is
visible in the saving rather than hidden:

| 4 MB synthetic video, 64 frames | saving | open | cold random access | warm (buffered) |
| --- | --- | --- | --- | --- |
| no keyframes | **89.22%** | 0.13 ms | 12.83 ms | 0.3 µs |
| `--keyframe-interval 8` | 79.31% | 0.15 ms | **1.31 ms** | 0.3 µs |

Open time is independent of container size — only the header and index are read.
`inspect` prints the chain depth and tells you to repack if it is unbounded.

## Measured — L1 in isolation

`python3 bench_l1.py --size 16MB --block 64KB`. Every row was decoded and
compared against the input by SHA-256 before being printed. `sparse-only` is
what a single-codec delta would have stored for the same blocks.

| shape | changed | codec | saving | sparse-only | encode | decode |
| --- | --- | --- | --- | --- | --- | --- |
| clustered | 1% | runs | 98.97% | 96.01% | 50 MB/s | 332 MB/s |
| clustered | 10% | runs | **89.79%** | 60.15% | 54 MB/s | 347 MB/s |
| clustered | 25% | runs | **74.50%** | 0.38% | 57 MB/s | 272 MB/s |
| clustered | 40% | runs | **59.21%** | −0.00% | 59 MB/s | 241 MB/s |
| scattered | 10% | bitmap | **77.50%** | 60.00% | 77 MB/s | 69 MB/s |
| scattered | 25% | bitmap | **62.50%** | −0.00% | 81 MB/s | 44 MB/s |
| scattered | 40% | bitmap | **47.50%** | −0.00% | 78 MB/s | 31 MB/s |
| scattered | 95% | raw | −0.00% | −0.00% | 132 MB/s | 1953 MB/s |

`scattered` spreads every change as far from its neighbours as the block allows
— constructed to be the worst possible input for run-based coding, not a
realistic one. The last row is the negative control: data with no exploitable
structure must not compress, and −0.00% is the one-byte codec tag.

## Two percentages, deliberately kept apart

The brief specifies savings as `(change size / total size) * 100`. That is
reported as `Savings.change_pct`, but it is **not** the saving — it assumes the
delta is free.

`Savings.saving_pct` is `(1 − encoded / total) * 100`: what the disk sees after
the representation is paid for. It is always smaller, `overhead_bytes` is the
gap, and it is the only one shown to a user. Tests assert
`saving_pct <= 100 − change_pct` across the whole range of `k`.

Both are `None`, never `0.0`, when there is nothing to measure. The CLI prints
`n/a`, because an unmeasured saving and a measured zero are different facts.

## Guardrails

* **No unbounded loop.** Every loop is a regex scan over a block checked against
  `MAX_BLOCK_BYTES` (4 MiB), or a `range()` over a count validated first.
  `MAX_BLOCKS` bounds a stream, `MAX_CHAIN_WALK` bounds a base chain.
* **Bounded memory.** Streams are never held whole; the `PRECEDING` window is a
  `deque(maxlen=...)`, so over-retention is not expressible. `Goal` refuses a
  configuration exceeding `MAX_WINDOW_BYTES`.
* **Nothing trusted on read.** Container headers, indices and block payloads are
  untrusted input; every offset, length and count is validated against the file
  size before allocation. 15 malformed-block tests and 15 malformed-container
  tests cover truncation, out-of-range offsets, impossible counts, unknown
  codecs, and bits set past the end of a block.
* **The cost model is checked on every block.** `encode_block` raises if its
  output differs from the predicted size, so a divergence surfaces as a failure
  rather than a wrong savings figure.
* **`pack` verifies before it reports.** The container is reopened and decoded
  against the input's SHA-256; a mismatch exits non-zero. A saving that cannot
  be reversed is not a saving.

## Known limits

1. **Bitmap decode degrades on scattered data** — 31 MB/s against 241 MB/s
   clustered. Two scatter strategies are dispatched on run density (worth 4–6×),
   but placing values at isolated positions is inherently sequential. `--fast-decode`
   bars the codec and accepts a smaller saving.
2. **Blocks are fixed-size and offset-aligned.** Content shifted by one byte is
   invisible. Same limitation as `FINDINGS.md` §8.2; content-defined chunking is
   the standard fix and is not implemented.
3. **No entropy coding.** `FINDINGS.md` §1 shows the redundancy in fp32 weights
   lives in the marginal byte distribution, which only an entropy coder reaches.
   L1 is blind to it by construction. The two are complementary — CCP followed
   by gzip was the strongest combination in that study, and pairing them here is
   not yet built.
4. **`probe()` reads a prefix.** A file whose character changes halfway through
   will be matched on its opening. Sampling from several offsets is not
   implemented.
5. **Against the wrong opponent.** `FINDINGS.md` §8.1 is still true and still
   unaddressed: `zstd --long` and content-addressed storage are the real
   production alternatives for this shape of data and are not measured anywhere
   in this repository. No external speed or ratio claim should be made until
   they are.
