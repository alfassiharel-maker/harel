# CCP-AI — lossless base + delta storage for model weights

A working product, not a mockup. Real `.safetensors` checkpoints, a real
bit-level codec, real bytes on disk, and a switch that migrates them.

> **Zero dependencies.** Python 3.11 standard library only — no virtualenv, no
> `pip install`, no numpy, no torch. This is deliberate: the engine is the asset,
> and a due-diligence reviewer must be able to clone and run it in one command.

```bash
python3 ccp-ai/run.py            # console at http://127.0.0.1:8420
python3 ccp-ai/cli.py demo       # same measurement, in the terminal
python3 -m unittest discover -s tests -t .     # 111 tests, run from ccp-ai/
```

---

## The switch

The console's ON/OFF button is the demo. It is not a display toggle — it
migrates the store:

| Switch | What is on disk | Measured |
|---|---|---|
| **OFF** | a full checkpoint per variant, exactly as a model hub stores them | 73.31 MiB |
| **ON** | one base + one verified `.ccp` container per variant | 33.44 MiB |

Flip it and the numbers move because `os.stat` says so. In `full` mode the files
are ordinary loadable checkpoints; in `ccp` mode every variant is rebuilt from
its container on read and hash-checked against the digest recorded when it was
pushed.

## Measured results

Demo family: a 2,884,736-parameter transformer-shaped base and six derived
variants, all float32, all generated deterministically (`--seed`). Reproduce with
`python3 ccp-ai/cli.py demo`.

| Variant | What changed | Full | CCP | Saving | vs `gzip -6` |
|---|---|---|---|---|---|
| `support-classifier` | frozen backbone, LM head dropped, new task head | 7.01 MiB | 396.6 KiB | **94.5%** | 94.0% |
| `last-two-layers` | last two blocks fine-tuned, rest frozen | 11.01 MiB | 773.7 KiB | **93.1%** | 92.6% |
| `instruct-fft-light` | full fine-tune, every weight moved, s=1e-4 | 11.01 MiB | 4.45 MiB | **59.6%** | 56.4% |
| `vocab-extended` | vocabulary 8192 → 8448, embeddings reshaped | 11.26 MiB | 4.67 MiB | **58.6%** | 55.3% |
| `instruct-fft` | full fine-tune, s=1e-3 | 11.01 MiB | 5.45 MiB | **50.5%** | 46.7% |
| `code-fft-heavy` | domain retrain, s=1e-2 | 11.01 MiB | 6.73 MiB | **38.9%** | 34.1% |
| **fleet** | | 62.30 MiB | 22.44 MiB | **64.0%** | |

All six rebuild **bit-exact** (SHA-256 match, `cmp` clean).

**The spread is the honest headline.** Savings depend on how far the fine-tune
moved the weights, and the demo deliberately ships the unflattering cases next to
the flattering ones:

* Partial tuning, task heads and adapters — the majority of what a hub actually
  holds — land at **90%+**, because frozen tensors cost literally zero bytes.
* A genuine full fine-tune of every parameter lands at **40–60%**, because the
  low mantissa bits really are new information and lossless coding cannot invent
  redundancy that is not there.

Anyone claiming 90% on an aggressive full fine-tune with lossless reconstruction
is quoting a number this repository can disprove in thirty seconds. What CCP
actually breaks is the *tradeoff*: an organisation can train FFT, get FFT quality,
and still distribute at adapter-like volumes for every variant that isn't a
from-scratch retrain.

## Why it compresses

A fine-tune moves a weight by a fraction of its value. In IEEE-754 that leaves
the sign bit, the exponent byte and the high mantissa bits untouched. Two
transforms exploit that, and the packer builds both per tensor and keeps the
smaller:

1. **`intdelta+planes`** — element-wise difference of the two tensors' integer
   bit patterns, zigzagged. Within a sign, float bit patterns are monotone in
   value, so "close in value" means "close as an integer": a one-ULP move becomes
   the number `1`, not thirteen scattered bits.
2. **`xor+planes`** — XOR residual, for tensors where subtraction does not help.

Both are then **deinterleaved into byte planes** — all the high-order bytes
together, all the low mantissa bytes together — before entropy coding. This is
the step a general-purpose compressor cannot do for itself: interleaved, it sees
`zero, zero, noise, noise` repeating and cannot model it. Split, the exponent
plane collapses to a run of zeros. Measured on `instruct-fft-light`:

| Plane | Role | Raw | Stored | Zero bytes |
|---|---|---|---|---|
| 3 | sign + exponent | 1.00 MiB | 1.24 KiB | 100% |
| 2 | high mantissa | 1.00 MiB | 34.08 KiB | 99% |
| 1 | mid mantissa | 1.00 MiB | 695.3 KiB | 9% |
| 0 | low mantissa | 1.00 MiB | 1.00 MiB | 0% |

Every step is a bijection. There is no rounding, no quantisation and no error
tolerance anywhere in the codec.

## Beyond byte diffing

A `bsdiff`-style byte diff degenerates to a full copy the moment a tensor is
inserted or resized, because every following offset shifts. CCP plans **per
tensor**, so it handles what a model hub really contains:

* **frozen tensors** → recorded as a pointer into the base, **0 bytes stored**
* **tuned tensors** → delta encoded
* **new / dropped / retyped tensors** → stored whole, deltas skipped
* **resized first axis** (added vocabulary, extra experts) → the shared rows
  delta as a byte prefix and only the new rows are stored whole. This alone took
  `vocab-extended` from 26.6% to 58.6% savings.

## Architecture

```
ccp-ai/
  engine/          zero-dependency core — the asset
    safetensors.py container reader/writer, byte-span level
    codec.py       XOR + int-delta + byte planes + entropy coding
    pack.py        the .ccp container: plan, pack, rebuild, verify
    metrics.py     savings arithmetic and cost projection
  store/           on-disk model store
    repository.py  per-tenant repo, the two storage modes, the migration
    ledger.py      append-only hash-chained operation log
  server/          stdlib HTTP API + static host (transport only)
  web/             the console: the switch, the tables, the projection
  demo/            deterministic model family generator
  tests/           111 tests, no dependencies
  cli.py           pack / unpack / inspect / bench / demo
  run.py           start the console
```

`engine/` imports nothing from the rest of the tree and nothing third-party.
`server/` is transport: it validates, calls the store, and serialises.

## The `.ccp` container

```
"CCPK" | u32 plan length | JSON plan | concatenated block payloads
```

The plan lists one block per tensor with its op, its destination span, its
transform, its per-plane sizes and its SHA-256. Independent blocks are what make
**chunked reconstruction** possible: a client fetching a 400 GB model applies one
tensor at a time instead of materialising the whole thing.

`pack()` decodes its own output and compares SHA-256 before returning. A container
that cannot reproduce its input is never written.

## Verification and integrity

* **Per tensor** — every block carries its own digest, checked on rebuild.
* **Per file** — the rebuilt bytes are hashed against the digest recorded at push.
* **Wrong base** — refused with the expected digest, never rebuilt approximately.
* **Tampering** — a flipped bit anywhere raises; corruption is reported, never
  served as data.
* **Ledger** — every operation is appended and hash-chained, so a savings figure
  traces back to an immutable record instead of a mutable counter.

## Security posture

* **Tenant isolation.** Every path derives from a validated tenant id and is
  re-checked against that tenant's root after resolution. No cross-tenant read
  path, no admin bypass, no RLS-style exemption. There is a tenant-isolation
  matrix in `tests/test_api.py` with a row per endpoint — a new endpoint without
  a row is an incomplete endpoint.
* **Input validation.** Ids match `^[a-z0-9][a-z0-9._-]{0,62}$`; bodies are
  capped; header lengths are capped before allocation; JSON must be an object.
* **Loopback by default**, optional `CCP_API_TOKEN` bearer auth compared with
  `hmac.compare_digest`, warning printed if bound wider without one.
* **No leakage.** Stack traces go to the log, never to a response. Strict CSP,
  `nosniff`, `DENY` framing. The console writes every server value with
  `textContent` — no `innerHTML` path for model names.

## Cost model

Unit prices live in one place (`engine/metrics.py`) so a reviewer can change them
and watch every number move: **$0.08/GiB** egress and **$0.023/GiB-month**
storage (AWS S3 list, us-east-1, 2026-08).

The projection panel applies the *measured* ratio to a fleet you choose. It
charges the base honestly — fetched once per client, then one delta per variant —
so fleet savings are always below the per-variant delta ratio. At 7B params, 10
variants, 100k downloads each: **$1.04M → $480K egress per release cycle.**

Storage is the smaller prize at that size ($3/mo → $1.38/mo); it only becomes the
headline in the hundreds-of-variants range, which the panel will show you.

## Honest limits

* **The reference decoder is pure Python.** Rebuild throughput is ~16 MiB/s for
  full-delta variants and ~100 MiB/s for mostly-frozen ones, on one core. That is
  fine for a demo and not fine for a 14 GB checkpoint; the production decoder is
  native SIMD work. The *ratio* is the transferable claim, and it is
  implementation-independent.
* **Demo scale is small.** ~11 MiB checkpoints, so seeding takes seconds. The
  compression measurement does not depend on size, but the wall-clock numbers do.
* **The delta magnitude drives everything.** The demo states its perturbation
  model explicitly (`w' = w(1 + s·g)`) rather than hiding it in a flattering
  seed.
* **fp16/bf16 headroom is lower than fp32.** Fewer mantissa bits means less
  structural redundancy to harvest. The engine handles both; expect narrower
  margins on 2-byte weights.

## Next

The PoC proves the codec. Production is: native encoder/decoder, chunked
streaming reconstruction for 100 GB+ models, a client SDK that fetches deltas
transparently, and integrations with Hugging Face and cloud MLOps registries.
