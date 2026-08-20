# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Scope: the `ccp/` subtree only — the CCP project.**
The repository root is an **unrelated product** (see §0 and §22). Its own context document is
`../CLAUDE.md`. Never merge the two, never copy architecture between them.

---

## §0. Status of this document — read first

**A working vertical slice exists.** All four layers are implemented and the full
pipeline runs end to end: real base and target → streaming → blocks → C++
XOR/POPCOUNT → Julia cost decision → FULL/DELTA → CCP container → reconstruction →
SHA-256 → byte-for-byte verification. Verified 2026-08-20 by `make all` from a
clean tree (§22).

What that does **not** mean: the product thesis is unvalidated. Everything runs on
synthetic, position-aligned artifacts. The measurement that would tell us whether
CCP helps on real ML checkpoints has not been done (§23.2, §24 question 1). Working
machinery and a validated product are different claims, and this document keeps
them apart.

### Status labels used throughout

| Label | Meaning |
|---|---|
| `[IMPLEMENTED]` | Verified working code, exercised by a passing test. |
| `[PARTIAL]` | Real code exists and works, but the component is incomplete against its intent. |
| `[SPECIFIED]` | Decided by the maintainer's CCP specification; no code yet. |
| `[NOT YET IMPLEMENTED]` | Specified, no code. |
| `[UNKNOWN]` | Not settled by the specification. **Ask the maintainer; do not choose.** |
| `[DECIDED-BY-BUILD]` | An open question the implementation had to answer to exist. Working, and **awaiting ratification** — see §26. |
| `[CONFLICT]` | Two sources disagree; recorded with which one wins. |

**Never upgrade a label without running the code.** Turning `[SPECIFIED]` into
`[IMPLEMENTED]` on the strength of intent is the drift this document exists to
prevent.

## §1. CCP project identity

| | | Status |
|---|---|---|
| **Name** | CCP — Copy, Change, Paste | `[SPECIFIED]` |
| **Category** | Differential Binary Versioning & Storage Infrastructure | `[SPECIFIED]` |
| **Explicitly NOT** | an XOR demo / an XOR compression script | `[SPECIFIED]` |
| **Home repository** | none of its own; lives under `ccp/` inside an unrelated repository | `[UNKNOWN]` (§24 q16) |
| **Languages** | Python (control), C++ (bit execution), Rust (data/storage), Julia (strategy) | `[SPECIFIED]` |
| **Maintainer language** | writes Hebrew; **repository artefacts stay in English** — code, comments, docs, commit messages | `[SPECIFIED]` |

---

## §2. Product definition · `[SPECIFIED]`

Rather than storing every version of a binary artifact in full, CCP identifies what changed
between an existing version and a new one, and stores the change:

```
Base + Delta → Target Version
```

The product is the **infrastructure**: versioned binary storage, differential representation,
cost-driven representation choice, reconstruction, and integrity verification. XOR is one
low-level primitive inside the engine — the smallest and least valuable part on its own.

---

## §3. User intent · `[SPECIFIED]`

What the maintainer is actually trying to achieve, in their own framing:

1. **Test and build** a system that can show an advantage where many large, similar versions of
   data exist — specifically AI/ML checkpoints, model artifacts, large binary datasets, large
   versioned binary files.
2. Reduce **storage** *and* **data movement**. These are separate goals; a delta can shrink
   bytes-at-rest while increasing bytes-moved on read.
3. Build it as **real infrastructure across four languages with hard responsibility
   boundaries**, not as a script that grows.
4. Keep the engineering honest: **the system must measure cost and choose**, never assume delta
   wins.

Intent that governs *how I should behave*: build understanding first, so that a later
"implement feature X" lands in the right component, under the right specification, without
rediscovering the project.

---

## §4. Product goal · `[SPECIFIED]`

Stated as measurable outcomes rather than features:

1. Store N similar large versions in materially less space than N full copies — **measured per
   artifact, not assumed**.
2. Reduce data movement, as a goal distinct from bytes at rest.
3. Reconstruct any stored version **losslessly and verifiably**.
4. Make every FULL-vs-DELTA decision **explainable**: for any stored version, state what the
   alternatives would have cost and why this one won.
5. Keep bit execution fast enough that the cost model, not the XOR, is the bottleneck.

---

## §5. Non-goals · `[SPECIFIED]`

- **Not** a general-purpose compressor competing with zstd/xz on single files.
- **Not** a text or source diff tool. CCP is binary and position-based.
- **Not** lossy or approximate storage under any circumstance.
- **Not** a distributed system, replication layer, or object-store replacement.
- **Not** a Python bit-processing engine.
- **Not** Julia as the primary file-storage layer.
- **Not** direct L1/L2/L3 cache control (§20).

---

## §6. Mathematical model · `[SPECIFIED]`

```
A = Base
B = Target
D = A XOR B
```

Reconstruction invariant:

```
B = A XOR D
A = B XOR D        # XOR is its own inverse
```

Properties the design relies on:

- **Self-inverse / symmetric.** Reconstruction *is* the same operation as delta computation —
  no separate "apply" algorithm. This is the reason XOR was chosen over a general diff.
- **`popcount(D)` = Hamming distance** = exact count of changed bits. The cheapest possible
  "how different are these two versions", and the natural input to the cost model.
- **Position-aligned.** Byte *i* of A is compared to byte *i* of B. Blind to insertion and
  shift. See §23 — the largest risk in the entire concept.
- **Length-sensitive.** Defined only for equal lengths. See §23.

---

## §7. Core invariants · `[SPECIFIED]`

A change breaking any of these is a product defect regardless of its performance.

| # | Invariant |
|---|---|
| **I1** | `B == A XOR D` for every stored delta, bit for bit. Hard gate on every write path — not a sampled check. |
| **I2** | Reconstruction is **lossless**. Never approximate, never "close enough". |
| **I3** | A stored version is retrievable **byte-identical** to what was ingested. |
| **I4** | The system **never assumes DELTA is better**. It measures, then chooses FULL or DELTA/other encoding. |
| **I5** | Every DELTA node reaches a FULL ancestor. A FULL version is always a self-sufficient reconstruction root. |
| **I6** | Python never performs the bulk bit processing. |
| **I7** | Integrity failure is a **hard error** — never a warning, never a silent repair. |

**I4 is the commercial heart of the product.** A differential system that blindly deltas is
worse than no system at all. The measurement *is* the product.

---

## §8. Terminology · `[SPECIFIED]`

Consistent naming is the main drift risk in a four-language project. Use these words.

| Term | Meaning |
|---|---|
| **Base** | the existing version a delta is computed against (`A`) |
| **Target** | the new version being stored (`B`) |
| **Delta** | the change representation (`D`) — *not necessarily* raw `A XOR B`; see Encoding |
| **FULL** | a version stored in its entirety; a reconstruction root |
| **DELTA** | a version stored as Base + Delta |
| **Block** | fixed-size unit of comparison and bit execution |
| **Encoding** | how a change is *represented on disk* (raw XOR, sparse index, RLE, compressed…). Distinct from the FULL/DELTA decision. |
| **Cost model** | the mathematical evaluation choosing between representations |
| **Representation decision** | the cost model's output for one version |
| **Version graph** | the structure recording which version derives from which |
| **Chain length** | number of deltas traversed to reconstruct — the cost that grows silently |
| **Reconstruction** | materialising a version from its FULL ancestor plus the delta chain |
| **Integrity verification** | proof that a reconstruction equals what was ingested |
| **CCP format** | the on-disk container format |

**Collision to avoid:** "delta" the concept vs "DELTA" the storage decision vs the encoded
bytes. Prefer *change representation* for the concept, *DELTA decision* for the choice.

---

## §9. Architecture · `[IMPLEMENTED]`

```
                  ┌─────────────────────────────────┐
   Python  ────────▶  orchestrates the whole flow   │   control plane
  (control)          └─────────────────────────────────┘
       │
       ├───────────▶ Rust  (data/storage) ── owns bytes, blocks, format, versions
       │                    │
       │                    ├──────────▶ C++   (bit execution) ── XOR / POPCOUNT on buffers
       │                    │
       └────────────────────┴──────────▶ Julia (strategy) ── cost model → FULL/DELTA
```

Dependency rules that follow, and must hold:

- **C++ depends on nothing.** It receives buffers, returns results. No I/O, no storage
  awareness, no policy. This is what keeps it testable and replaceable — protect it the way a
  pure library is protected.
- **Julia depends on nothing but numbers.** It receives measurements (sizes, popcounts, chain
  lengths) and returns a decision. **It must not read files.**
- **Rust must not embed the policy.** It measures and stores; it asks Julia what to do.
- **Python must not bypass Rust** to touch storage, nor bypass C++ to compute bits.
- **No cycles.** If a design needs C++ to call back into Rust, the design is wrong.

**Cross-language transport** — `[DECIDED-BY-BUILD]`, §26:
* Rust → C++: **static linking over the C ABI**. `dataeng/build.rs` compiles
  `bitexec/src/bitexec.cpp` and links it; all `unsafe` lives in `dataeng/src/bitexec.rs`.
* Python → Rust and Rust → Julia: **subprocess boundaries speaking JSON**. Chosen
  because it keeps memory ownership entirely inside Rust, turns an engine crash into
  a reportable error rather than an interpreter fault, and costs one process launch
  on work measured in seconds. The Julia launch is 0.47s and is currently the
  dominant cost of a small store (§22) — the first thing to revisit if write
  throughput matters.

---

## §10. Responsibility of each language · `[SPECIFIED]` intent, `[IMPLEMENTED]` code

### Python — Application / Control Plane · `[IMPLEMENTED]` (`control/`)
CLI, API, orchestration, jobs, configuration, UI/API boundary, monitoring.
**Must not become the native bit-processing core.** Python coordinates; it does not loop over
buffers.

### C++ — Bit Execution Engine · `[IMPLEMENTED]` (`bitexec/`)
XOR, POPCOUNT, comparison, masks, bit operations, SIMD, native execution.
**Must perform real operations on real buffers** — not a wrapper, not a stub, not a demo.

### Rust — Data / Storage Engine · `[IMPLEMENTED]` (`dataeng/`)
Streaming, file I/O, buffer management, block processing, storage, CCP format, reconstruction,
integrity, version management. **It owns the bytes.**

### Julia — Strategy / Mathematical Engine · `[IMPLEMENTED]` (`strategy/`)
Cost model, representation selection, thresholds, policy, encoding decisions.
**Decides mathematically. Must not become the primary file-storage layer.**

---

## §11. Data flow · `[IMPLEMENTED]`

Full specified pipeline:

```
Ingest → Streaming → Blocks → Compare → Native Bit Execution
→ Change Representation → Cost Analysis → Representation Decision
→ Store → Version Graph → Reconstruction → Integrity Verification
```

Mapped onto the layers — **write path:**

```
artifact bytes
  → [Rust]  stream in, split into fixed-size blocks
  → [Rust]  the Base is named by the caller (--base)          ← automatic selection [UNKNOWN], §24 q12
  → [C++]   per block: XOR(base_block, target_block), POPCOUNT
  → [Rust]  assemble the change representation
  → [Julia] cost analysis: FULL vs encoded delta vs other, including chain cost
  → [Julia] representation decision: FULL | DELTA | other encoding
  → [Rust]  write in CCP format; record content hash
  → [Rust]  update version graph
```

**Read path:**

```
version id
  → [Rust] walk the version graph to the FULL ancestor
  → [Rust] load FULL + the delta chain
  → [C++]  XOR-apply each delta, block by block
  → [Rust] integrity verification against the recorded hash   ← I3, hard gate
  → bytes out
```

**The asymmetry to hold in mind:** the write path is where the cleverness lives; the read path
is where correctness is proved and where chain length silently hurts. Evaluate every write-path
decision by what it does to the read path.

---

## §12. Block model · `[IMPLEMENTED]`, parameters `[DECIDED-BY-BUILD]`

Fixed-offset blocks; each block makes its own representation decision, so a large
artifact with one changed region does not force one choice for the whole thing.

| Parameter | Value | Status |
|---|---|---|
| Default block size | 1 MiB (`store::DEFAULT_BLOCK_SIZE`), `--block-size` overrides | `[DECIDED-BY-BUILD]` — large enough that the 32-byte index entry is negligible, small enough that one changed region does not force a large FULL block. **Not tuned against real workloads.** |
| Boundaries | fixed offset, not content-defined | `[IMPLEMENTED]` — follows from XOR's position alignment (§23.2) |
| Tail handling | a short final block is a block of its logical length; nothing is padded | `[IMPLEMENTED]`, tested at sizes 1, 2, `bs-1`, `bs`, `bs+1`, `2bs+7` |
| Per-block measurements | `changed_bits` and `changed_bytes` retained in the container | `[IMPLEMENTED]` — so a decision stays explainable without recomputation |

Content-defined chunking remains **`[UNKNOWN]`** and is a maintainer decision, not
an implementation detail: it is a different technique with different costs.

---

## §13. Storage model · `[IMPLEMENTED]`

```
<repo>/manifest.json          the version graph, JSON, written atomically via rename
<repo>/objects/<id>.ccp       one container per version
```

Version ids are derived, not random: `SHA-256(content_hash ‖ name ‖ created_at)`
truncated to 16 bytes, so there is no RNG dependency.

`[NOT YET IMPLEMENTED]`: garbage collection, retention, concurrent access (two
writers on one repository are not serialised), block sharing across versions,
remote or object storage.

---

## §14. Version model · `[IMPLEMENTED]`

- Versions form a graph. Roots hold only FULL blocks; a delta names its base.
- Every delta reaches a root; `Repository::chain` walks it and **rejects cycles**.
- **Chain depth is priced by the cost model** (§16), which is what stops the system
  from optimising storage into unusable read latency.

`[NOT YET IMPLEMENTED]`: rebasing / re-anchoring an existing version, branching
semantics beyond "many versions may share a base", automatic re-rooting when a
chain grows too deep.

---

## §15. CCP format · `[IMPLEMENTED]`, version 1

**`docs/format-v1.md` is authoritative for the layout.** `dataeng/src/format.rs`
implements it, and its tests assert the constants against the spec.

```
Header 128 B │ Block index 32 B × N │ Payloads │ Footer 40 B
```

- `format_version` at byte 8. Every reader **rejects** an unknown version, unknown
  flags, bad magic, or offsets that disagree with the header rather than guessing.
- Block kinds: `FULL`, `DELTA_RAW`, `DELTA_SPARSE`, `IDENTICAL` (empty payload).
- `DELTA_SPARSE` payload: `u32 count`, then `count × (u32 offset, u8 xor_value)` —
  5 bytes per changed byte, which is the cost the strategy engine prices.
- Footer carries SHA-256 of every preceding byte, detecting truncation and
  corruption independently of whether the content hash matches.
- `apply_sparse` bounds-checks every offset, so a corrupt or hostile container
  cannot write outside its block.

**Unequal lengths** — the policy is explicit, with no padding or truncation
anywhere: a block is delta-eligible only when base and target supply the same
number of bytes; otherwise it is stored FULL. A version that grows or shrinks
stores the affected tail block in full and every whole block before it can still
be a delta.

---

## §16. MVP — a working vertical slice · `[IMPLEMENTED]`

The specification never defined an MVP scope (it was `[UNKNOWN]`). What was built
is the thin end-to-end slice: **all four languages, one artifact series, real
files, byte-exact reconstruction**. `[DECIDED-BY-BUILD]`, §26.

In it:

| Capability | Status |
|---|---|
| Streaming ingest, memory independent of artifact size | `[IMPLEMENTED]` |
| C++ XOR / POPCOUNT with runtime AVX2 dispatch | `[IMPLEMENTED]` |
| Julia cost model choosing FULL / DELTA_SPARSE / DELTA_RAW / IDENTICAL | `[IMPLEMENTED]` |
| CCP container v1, version graph, multi-hop chains | `[IMPLEMENTED]` |
| Reconstruction + SHA-256 + byte-for-byte verification | `[IMPLEMENTED]` |
| Corruption and truncation detection | `[IMPLEMENTED]` |
| Python CLI, `doctor`, benchmarking harness | `[IMPLEMENTED]` |
| Unequal-length artifacts | `[IMPLEMENTED]` |

Deliberately **not** in it: automatic base selection, GC, concurrency, an HTTP API,
observability beyond CLI output, CI, object storage, content-defined chunking, and
any measurement on real ML checkpoints.

### The cost model, as implemented (`strategy/src/CCPStrategy.jl`)

```
effective_cost = storage_weight × stored_bytes
               + read_amplification_weight × hops × logical_len
```

where `hops = chain_depth + 1` for any representation that extends the chain, and 0
for FULL. The lowest-cost **valid** candidate wins; ties go to FULL, because not
lengthening the chain is strictly better for every future read.

| Parameter | Default | Status |
|---|---|---|
| `storage_weight` | 1.0 (the unit) | `[IMPLEMENTED]` |
| `read_amplification_weight` | 0.05 | `[DECIDED-BY-BUILD]` — **not calibrated against any workload**, an honest placeholder |
| `max_chain_depth` | 16 | `[DECIDED-BY-BUILD]` — a bound to keep reconstruction finite, not an optimum |

Overridable per run via `CCP_STORAGE_WEIGHT`, `CCP_READ_AMPLIFICATION_WEIGHT`,
`CCP_MAX_CHAIN_DEPTH`. The chosen parameters are echoed in every response, so a
stored decision can be re-explained against the exact policy that produced it.

Validity is a correctness question, never economic: FULL is always valid; deltas
require an equal-length base block; `IDENTICAL` additionally requires zero changed
bytes. Rust **re-checks** the returned decision against those rules rather than
trusting the engine, because an invalid choice would produce an unreconstructable
container.

---

## §17. Benchmark model · `[IMPLEMENTED]` harness, `[NOT YET IMPLEMENTED]` real corpus

`control/ccp/benchmark.py`, driven by `python3 -m ccp benchmark`. Measures against
three baselines, because "beats storing full copies" is the easy comparison:

1. **Full copies** — the naive system CCP improves on.
2. **Independent zlib per version** — the baseline that most often defeats a naive
   delta scheme, and the honest bar to clear.
3. **Concatenated zlib** — bounds what a compressor could recover from
   cross-version redundancy. Not a usable versioning system (no random access).

Also reports `changed_bits / total_bits` per version — the sparsity measure that
predicts whether position-aligned XOR deltas can pay off at all — plus encode and
decode time and throughput. **Decode is timed too**, deliberately: an encode-only
benchmark flatters a scheme whose read path is the expensive half.

**The corpus is the missing half.** `make demo` runs on synthetic artifacts that
are position-aligned by construction *and incompressible*, which makes both zlib
baselines look artificially weak — random bytes do not compress, real checkpoints
do. Those numbers demonstrate the machinery, and say nothing about the thesis.
See §24 question 1.

---

## §18. Testing model · `[IMPLEMENTED]`

Four suites, all run by `make test`. Nothing is mocked: a stub would prove only
that the stub works, and byte-exactness is the one property a stub cannot
establish.

| Suite | Command | What it guards |
|---|---|---|
| C++ | `make test-bitexec` | `B = A XOR D` and every measurement against a naive reference, at lengths straddling word and vector boundaries (0,1,7,8,9,…,32,33,…,65537). A SIMD tail bug is silent corruption — the worst failure this project has. |
| Rust | `make test-dataeng` (27 tests) | SHA-256 against NIST vectors incl. the 1,000,000-`a` case, streaming vs one-shot in 9 chunk sizes; format round-trips and rejection of future versions / unknown flags / bad offsets / corrupt sparse payloads; JSON; the FFI is wired to the real engine; the strategy protocol rejects invalid decisions. |
| Julia | `make test-strategy` | The decision boundaries, computed exactly: sparse wins at 777 changed bytes and FULL at 778 in a 4 KiB block at depth 0. Also dense→FULL, unchanged→IDENTICAL, no-base→FULL, deep chains→FULL, ties→FULL. |
| Integration | `make test-integration` (21 tests) | The whole stack on real files: byte-for-byte round-trips, a 10-hop chain, unequal lengths both directions, unaligned tails, corruption and truncation detection, manifest survival across reopen, and that every decision carries the strategy engine's own reasoning. |

`assert_roundtrip` in `tests/test_vertical_slice.py` is the load-bearing helper:
compares bytes, compares hashes, and checks the engine's recorded hash agrees —
which catches a container that verifies against a wrong-but-consistent digest.

Fixtures are seeded, never `urandom`: an intermittent failure in a byte-exactness
test is close to undiagnosable.

---

## §19. Engineering rules — future agents must not violate these

1. **Do not claim a component exists.** Verify, then state. §22 is the status register: update
   it, never contradict it.
2. **Respect the language boundaries** (§10): no bulk bit processing in Python (I6), no storage
   in Julia, no policy in Rust, no I/O in C++.
3. **Never break I1/I2/I3.** No lossy path, no approximate reconstruction. A round-trip gate on
   every write path is not optional.
4. **Never assume DELTA wins** (I4). The cost model runs; the decision is recorded and
   explainable.
5. **Integrity failure is a hard error** (I7). Never warn-and-continue, never auto-repair.
6. **Do not invent architecture.** Where this document says `[UNKNOWN]`, the answer is to ask —
   then record the decision here. Do not choose silently.
7. **Do not write to the CCP format before it is specified** (§15). Format-version field from
   the first byte.
8. **Do not silently reconcile a conflict.** Record it as `[CONFLICT]` with which source wins.
9. **Never treat an unrelated project's file as a CCP component.** If a required component does
   not exist, create it inside the CCP structure (§21-B) — do not repurpose the sports
   platform's code, and never modify it.
10. **Do not touch the repository root** or anything outside `ccp/`.
11. **Repository artefacts stay in English.** Reply to the maintainer in Hebrew when they write
    in Hebrew.
12. **Keep this document honest.** When code lands, rewrite the sections it obsoletes and
    upgrade the labels. Never leave two competing descriptions of the same thing.

Rules the existing code depends on — breaking one of these breaks something real:

13. **The cost model lives only in `strategy/src/CCPStrategy.jl`.** If a threshold, a
    weight, or a size comparison that decides a representation appears in Rust, C++
    or Python, the boundary is broken. `dataeng/src/strategy.rs` sends measurements
    and validates the answer; it must never compute a cost.
14. **`docs/format-v1.md` is authoritative for the container.** Change the document
    and `format.rs` together, and bump `format_version` for any layout change —
    stored containers must never be reinterpreted under a new meaning.
15. **All `unsafe` stays in `dataeng/src/bitexec.rs`.** Its wrappers take slices and
    pass a length no larger than the shortest buffer; keep that property.
16. **Never return unverified bytes.** Reconstruction hashes its output and deletes
    it on mismatch. Do not add a fast path that skips verification.
17. **Do not add a fallback cost model** when Julia is missing (D8). Fail loudly.
18. **Keep the precompilation workload** at the bottom of `CCPStrategy.jl`. Removing
    it silently costs ~1.5s per store operation (§22).
19. **Run `make` before claiming anything works.** Four toolchains, and the
    integration suite is the only thing that exercises all of them together.

### Anti-drift procedure — when asked to implement a CCP feature

1. Read this document first, then §21-A for where things live.
2. Identify the target component from §9/§10 and its file from §21-A.
3. Check whether it already exists — much now does (§22), so extend rather than
   recreate.
4. Do **not** search unrelated repository areas. Nothing outside `ccp/` belongs to
   CCP.
5. If a `[DECIDED-BY-BUILD]` choice from §25 is in the way, changing it is allowed —
   deliberately, and with this document updated.
6. Run `make` and update the status labels and measurements in §22.

---

## §20. CPU / cache interpretation · `[SPECIFIED]`, and it **corrects** an earlier wording

**`[CONFLICT]` C1** — earlier framing said "C++ = L1/L2, Rust = L3/RAM".
**Do not model the product as direct cache control. The corrected interpretation wins**, by the
maintainer's explicit supersession.

Correct interpretation — what C++ should actually do:

- cache-friendly block sizes
- contiguous buffers
- locality
- alignment where useful
- SIMD
- streaming access
- memory reuse

**Actual cache residency remains under CPU hardware control.** Any code, comment, or document
asserting direct control over cache levels is wrong and should be corrected on sight.

---

## §21. Project Map — two strictly separate layers

### A. CURRENT REALITY — what exists in this workspace

```
/home/user/harel/                  ← repository: alfassiharel-maker/harel
├── CLAUDE.md, README.md, docs/00-22, backend/, database/, tests/, apps/, ml/
│      ▲ UNRELATED PROJECT: "AI Sports Coach Platform" (Python/FastAPI/Postgres).
│        NOT CCP, not a component of CCP, shares no code or concept.
│        Do not modify. Do not reinterpret as CCP.
│
└── ccp/                           ← THE CCP PROJECT
    ├── CLAUDE.md                  this file — context and status register
    ├── README.md                  how to build and use it
    ├── Makefile                   the one build entry point for all four toolchains
    ├── pytest.ini                 isolated so the parent repo's config is not picked up
    ├── docs/format-v1.md          AUTHORITATIVE for the container layout
    ├── bitexec/                   C++ bit execution engine
    │   ├── include/ccp_bitexec.h  the C ABI — the whole contract
    │   ├── src/bitexec.cpp        scalar + AVX2 paths, runtime dispatch
    │   ├── tests/test_bitexec.cpp
    │   └── CMakeLists.txt
    ├── dataeng/                   Rust data / storage engine
    │   ├── build.rs               compiles and links the C++ engine
    │   └── src/
    │       ├── store.rs           MOST IMPORTANT FILE: repository, write path, read path
    │       ├── format.rs          container v1 — implements docs/format-v1.md
    │       ├── bitexec.rs         the only `unsafe` in the project
    │       ├── strategy.rs        the Julia bridge; contains no cost arithmetic
    │       ├── sha256.rs          in-tree, NIST-vector tested
    │       ├── json.rs, error.rs, lib.rs, main.rs
    ├── strategy/                  Julia strategy engine
    │   ├── Project.toml            a real package — precompilation, see §22
    │   ├── src/CCPStrategy.jl      THE COST MODEL — the only place policy lives
    │   ├── src/MiniJSON.jl         submodule
    │   ├── bin/ccp_strategy.jl     stdin JSON → stdout JSON
    │   └── test/runtests.jl
    ├── control/                   Python control plane
    │   └── ccp/{cli,engine,config,benchmark}.py
    ├── scripts/demo.py
    └── tests/                     integration suite over the real stack
```

### B. CCP TARGET SYSTEM — intended scope, and what is still missing

| Component | Status |
|---|---|
| Python control plane | `[IMPLEMENTED]` (CLI); HTTP API, jobs, monitoring `[NOT YET IMPLEMENTED]` |
| C++ bit engine | `[IMPLEMENTED]` (XOR, POPCOUNT, masks, bit ops, AVX2); AVX-512, threading `[NOT YET IMPLEMENTED]` |
| Rust data/storage engine | `[IMPLEMENTED]`; GC, concurrency, remote storage `[NOT YET IMPLEMENTED]` |
| Julia strategy engine | `[IMPLEMENTED]`; calibrated weights, learned policy `[NOT YET IMPLEMENTED]` |
| CCP binary format | `[IMPLEMENTED]` v1 |
| Integration tests | `[IMPLEMENTED]` |
| Benchmarks | `[PARTIAL]` — harness done, real corpus missing (§17) |
| CI | `[NOT YET IMPLEMENTED]` |
| Observability | `[NOT YET IMPLEMENTED]` beyond CLI/JSON output |
| Automatic base selection | `[NOT YET IMPLEMENTED]` — the caller names `--base` |

**Never mix layer A and layer B.**

---

## §22. Current implementation reality — verified commands and measurements

Verified on 2026-08-20, x86_64 Linux, 4 cores, from a clean tree (`make clean && make all`).

### Commands that actually work

```bash
make                  # build everything + run all four suites   ← the one command to know
make build            # C++ engine + Rust engine (Julia/Python need no build)
make test             # all four suites
make doctor           # confirm every component present and runnable
make demo             # pipeline on generated artifacts + baseline comparison

make test-bitexec     # C++ only
make test-dataeng     # Rust only  (27 tests)
make test-strategy    # Julia only (11 testsets)
make test-integration # Python end-to-end (21 tests)
```

Single test, per layer:

```bash
cd dataeng && cargo test --release sha256::tests::nist_vectors
PYTHONPATH=control python3 -m pytest -c pytest.ini tests -q \
    -k test_unrelated_data_is_stored_full_not_delta
julia --startup-file=no strategy/test/runtests.jl     # whole file; no per-testset selection
./bitexec/build/test_bitexec                          # whole binary
```

Note `-c pytest.ini`: without it pytest finds the parent repository's config and
runs nothing.

### Toolchain versions used

Rust 1.94.1 · g++ 13.3.0 (C++17) · CMake 3.28.3 · Julia 1.11.3 · Python 3.11.15.
**No third-party libraries in any layer** — SHA-256 and both JSON implementations
are in-tree, deliberately (see the note in `dataeng/Cargo.toml`).

### Measurements — real numbers, from the demo above

On five 4 MiB synthetic artifacts, 256 KiB blocks, four in-place-modified versions
plus one unrelated:

| | |
|---|---|
| Sparse version stored | ~0.01 MiB of 4 MiB (deltas chosen) |
| Unrelated version | stored FULL — the cost model **refused** a delta that would have cost ~5× |
| Whole series | 8.02 MiB stored for 20 MiB logical |
| Encode | 7.0 MiB/s |
| Decode | 42.9 MiB/s |
| Reconstruction | byte-for-byte identical, all five |
| Corrupted container | detected, exit code 3 |

**Read the encode number honestly.** 7 MiB/s is *not* the bit engine's speed; it is
dominated by the Julia process launch at ~0.47s per store operation. That 0.47s is
itself down from 2.0s: the strategy engine was made a real Julia package with a
precompilation workload in the module body (`strategy/src/CCPStrategy.jl`), which
moves first-call JIT into the build. Removing that workload silently costs 1.5s per
store.

Largest artifact exercised end to end: 8 MiB. The streaming design means memory
does not scale with artifact size, but **multi-gigabyte artifacts have not been
tested**, and neither has anything near the 100 GB the specification targets.

---

## §23. Security / integrity requirements, and dangerous assumptions

### Integrity requirements · `[IMPLEMENTED]`

- Lossless, byte-identical reconstruction (I2, I3) — asserted by every integration test.
- **SHA-256 per version**, recorded at ingest and verified after every reconstruction.
  A mismatch **deletes the output** rather than returning an unverified file.
  Per-block hashing remains `[UNKNOWN]` and was not needed for v1.
- **SHA-256 of the container itself** in the footer, checked before any read, so
  truncation and corruption are caught independently of the content hash.
- Integrity failure is a hard error (I7): its own Rust variant, its own Python
  exception type, and **exit code 3** so the control plane can distinguish "stored
  data is wrong" from "you asked for the wrong thing".
- `format_version` at byte 8, present from the first byte ever written (§15).
- `apply_sparse` bounds-checks every offset: a hostile container cannot write out of
  bounds. All `unsafe` is confined to `dataeng/src/bitexec.rs`, whose wrappers take
  slices and pass a length no larger than the shortest buffer.

Beyond integrity — encryption at rest, access control, multi-tenant isolation,
signed containers: `[NOT YET IMPLEMENTED]`, and not addressed by the specification.
The relevant hardening already in place is structural: Rust owns all I/O and
parsing, and C++ only ever receives validated, sized buffers.


### DANGEROUS ASSUMPTIONS — read before writing CCP code

Items 1–3 are mathematical facts, not opinions. Each can invalidate the product thesis if built
on unexamined.

**23.1 — "XOR reduces size." It does not.** `[still true, and now handled]`
`A XOR B` is *exactly* the same length as B. Raw XOR saves nothing at all. The saving comes
entirely from D being **sparse** (mostly zero) and therefore compressing or sparse-encoding
well. **The encoding step is where the product's value is realised, not the XOR.** Any plan
whose savings come from "XOR" without a named encoding is storing the same number of bytes with
extra steps. This is why `DELTA_SPARSE` exists and why `DELTA_RAW` almost never wins: raw XOR
ties with FULL on size and loses on chain cost.

**23.2 — "Similar versions produce sparse deltas." Only if changes are position-aligned.** `[UNVALIDATED — the single biggest open risk]`
One inserted byte near the start shifts every subsequent byte, and `A XOR B` becomes dense noise
— often *less* compressible than B itself. Whether the target artifacts are alignment-preserving
is an **empirical question that should be measured on real files before the engine is built
around XOR.** For ML checkpoints it is plausible — fixed tensor layouts, same shapes, weights
changing in place — and plausible is not measured. **The working vertical slice does not change
this at all:** every artifact it has been run on was position-aligned by construction, so the
demo's results are a property of the test data, not evidence about the workload.

**23.3 — "Versions are the same length." XOR is undefined otherwise.** `[handled]`
Padding, truncation and tail handling all change the result and all need a decided, documented
policy. The policy is now explicit (§15) and tested in both directions: unequal-length blocks are
stored FULL, and nothing is ever padded or truncated.

**23.4 — "Delta chains are cheap to read." They are not.** `[priced, with an uncalibrated weight]`
Each hop is I/O plus an XOR pass over the full artifact. A long chain can make reconstruction
slower than having stored FULL copies while the storage graph still looks like a win. The cost model prices it (§16) via `read_amplification_weight`, and `max_chain_depth`
bounds it absolutely — but that weight is an uncalibrated placeholder, so *how well* it is priced
is still unknown.

**23.5 — "Python is fine for the first version."** `[avoided]`
A Python prototype of the bit path will set the block sizes, buffer shapes and API that C++ then
inherits — and Python's convenient shapes are frequently the ones that defeat SIMD and locality.
I6 exists for this reason. It was honoured: there is no bit loop anywhere in `control/`, and the
block sizes and buffer shapes were set by the Rust and C++ layers.

**23.6 — "Four languages is a starting point."** `[paid, and it showed up exactly where predicted]`
Four languages means four toolchains, three FFI boundaries and a CI matrix before the first byte
is stored. The boundaries in §10 are the maintainer's decision and stand. The cost landed on the
cross-language boundary as predicted: the Julia launch is now the dominant cost of a small store
(§22), and diagnosing it took a real measurement rather than a guess.

---

## §24. Open questions

Roughly by leverage. Answered ones are marked; the rest are still maintainer calls.

**Concept validation — still decides whether any of this is worth building**
1. ⬜ **THE question.** Are real target artifacts (ML checkpoints especially)
   position-aligned between versions? What do `popcount(A XOR B)/total_bits` and
   `stored_delta` vs `compressed_size(B)` show on *real* checkpoint pairs? The
   harness to answer this exists (`ccp benchmark`); the corpus does not. (§23.2)
2. 🟡 **Partly answered.** `DELTA_SPARSE` at 5 bytes per changed byte is
   implemented and works. Whether a bitmap, RLE, or compressed delta beats it on
   real change patterns is unmeasured — the format has room for more kinds.
3. ⬜ Does CCP beat "compress each version independently" on real data? The
   baseline is measured, but only on incompressible synthetic data so far (§17).

**Cost model**
4. 🟡 Cost is `storage + read_amplification_weight × hops × bytes`. Whether that
   is the right shape, and what the weight should be, needs a real workload. (§16)
5. ✅ Chain length enters the cost function, weighted, plus a hard depth bound.
6. ⬜ Should thresholds be static, per-artifact, or learned?

**Format, blocks, storage**
7. ✅ The format was specified before any bytes were written (`docs/format-v1.md`).
8. 🟡 Block size defaults to 1 MiB, overridable. Not tuned. (§12)
9. ✅ Unequal lengths: eligible only on equal-length blocks; no padding. (§15)
10. 🟡 SHA-256 per version and per container. Per-block hashing not implemented —
    is it wanted?
11. 🟡 A version graph with cycle detection exists; deltas may chain on deltas.
    Rebasing, GC and branching semantics are open.

**Base selection**
12. ⬜ The caller names `--base`. How should a base be chosen automatically —
    predecessor, nearest by a cheap distance measure, most recent FULL? This
    likely dominates the achieved ratio in practice.

**Engineering**
13. ✅ Transport: static C ABI link Rust↔C++; JSON subprocesses elsewhere. Revisit
    the Julia boundary if write throughput matters (§22).
14. ✅ One `Makefile` drives all four toolchains.
15. ✅ Build order answered by building the thin end-to-end slice first.
16. ⬜ Does CCP stay in this repository, or move to its own? Moving would let
    `ccp/CLAUDE.md` become a root `CLAUDE.md` and remove the unrelated-neighbour
    hazard entirely.

**New, raised by the implementation**
17. ⬜ Concurrency: two writers on one repository are not serialised. Needs a lock
    or a documented single-writer constraint.
18. ⬜ Storing against a base reconstructs that base first, so a store at depth *d*
    walks *d* hops. Acceptable now; a cache or a re-anchoring policy may be needed.
19. ⬜ Should the strategy engine become a long-lived process (or an in-process
    library) to remove the 0.47s per-store launch?

---

## §25. Decisions made by the implementation — awaiting ratification

These were `[UNKNOWN]` in the specification and had to be answered for code to
exist. Each works and is tested; **none has been approved.** Change any of them
freely if the maintainer decides differently — but change them deliberately, and
update this document.

| # | Decision | Where | Cost of changing it later |
|---|---|---|---|
| D1 | Container layout v1 (128 B header, 32 B index entries, 40 B footer) | `docs/format-v1.md`, `format.rs` | **High** — written into stored data. Mitigated by `format_version` from byte 8. |
| D2 | `DELTA_SPARSE` = 5 bytes per changed byte | `format.rs` | Medium — a new kind can be added alongside it. |
| D3 | Cost = storage + `0.05 ×` read amplification; `max_chain_depth` 16 | `CCPStrategy.jl` | **Low** — one file, env-overridable. The right place to iterate. |
| D4 | Default block size 1 MiB | `store.rs` | Low — per-store flag, recorded in each container. |
| D5 | Transport: static link Rust↔C++, JSON subprocess elsewhere | `build.rs`, `strategy.rs`, `engine.py` | Medium. |
| D6 | No third-party libraries; SHA-256 and JSON in-tree | `Cargo.toml`, `MiniJSON.jl` | Low. |
| D7 | Version ids = `SHA-256(content ‖ name ‖ time)[0..16]` | `store.rs` | Medium — ids appear in the manifest and in filenames. |
| D8 | Julia is a hard dependency; **no fallback cost model** | `strategy.rs` | Low, and deliberate: a second policy implementation would drift from the real one and mask its absence. |
| D9 | Ties in the cost model resolve to FULL | `CCPStrategy.jl` | Low. |

---

## §26. Project roadmap / status

**Current milestone: working vertical slice. Thesis unvalidated.**

| | |
|---|---|
| All four layers, end to end, byte-exact | ✅ `[IMPLEMENTED]` |
| CCP container format v1, specified then implemented | ✅ |
| Cost-driven FULL/DELTA decision, refusing bad deltas | ✅ |
| Integrity: SHA-256 content + container, hard failures | ✅ |
| 4 test suites green from a clean tree (`make all`) | ✅ |
| Benchmark harness with three real baselines | ✅ |
| **Measured on real ML checkpoints** | ❌ **the gap that matters** |
| Scale beyond 8 MiB artifacts | ❌ |
| CI, observability, API, GC, concurrency | ❌ |

### What to do next, and why it is not more code

The highest-value next step is **§24 question 1**: run `ccp benchmark` over real
consecutive checkpoints from a real training run. It needs no new architecture, it
uses the harness that already exists, and it is the one result that can validate
or invalidate the whole design. Every engineering decision downstream —
encoding choice, block size, the cost weights, whether content-defined chunking is
needed at all — is currently being guessed at, and that measurement replaces the
guesses.

Build more only after it: if checkpoint deltas turn out dense, the position-aligned
XOR approach needs rethinking before any of it is optimised, and finding that out
after building the fast paths would be the expensive order.

The second-highest is calibrating `read_amplification_weight` (D3) against measured
reconstruct latency, since it currently steers every decision on a placeholder.
