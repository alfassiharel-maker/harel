# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Scope: the `ccp/` subtree only — the CCP project.**
The repository root is an **unrelated product** (see §0 and §22). Its own context document is
`../CLAUDE.md`. Never merge the two, never copy architecture between them.

---

## §0. STOP — read before any CCP work

**Verified 2026-08-20. No CCP implementation exists anywhere reachable from this session.**

Evidence, so the next agent does not repeat the search:

| Check | Result |
|---|---|
| `grep -ri "copy, change, paste\|differential binary\|ccp"` over the whole tree | 0 hits |
| `find` for `*.rs *.cpp *.hpp *.cc *.jl Cargo.toml CMakeLists.txt` | 0 files |
| Same search across **every commit on every branch** (`git rev-list --all`) | 0 files |
| Filesystem outside the workspace (`/home`, `/root`, `/workspace`) | no CCP directory |
| Connected repositories for this account (`list_repos`) | exactly one: `alfassiharel-maker/harel` — the sports platform |

Therefore: **everything in this document that describes CCP behaviour is intent, not fact.**
Every claim carries a status label (§ below). `[SPECIFIED]` means *the maintainer decided it*,
never *the code does it*.

### Status labels used throughout

| Label | Meaning |
|---|---|
| `[SPECIFIED]` | Decided by the maintainer's CCP specification. Authoritative for intent. No code. |
| `[IMPLEMENTED]` | Verified working code exists. **Currently used zero times in this document.** |
| `[PARTIAL]` | Some real code exists, incomplete. **Currently used zero times.** |
| `[NOT YET IMPLEMENTED]` | Specified, but no code. |
| `[UNKNOWN]` | The specification does not settle this. **The answer is to ask the maintainer, not to choose.** |
| `[CONFLICT]` | Two sources disagree; recorded with which one wins. |

**Never upgrade a label without verifying the code.** Turning `[SPECIFIED]` into
`[IMPLEMENTED]` on the strength of intent is exactly the drift this document exists to prevent.

---

## §1. CCP project identity

| | | Status |
|---|---|---|
| **Name** | CCP — Copy, Change, Paste | `[SPECIFIED]` |
| **Category** | Differential Binary Versioning & Storage Infrastructure | `[SPECIFIED]` |
| **Explicitly NOT** | an XOR demo / an XOR compression script | `[SPECIFIED]` |
| **Home repository** | none — no dedicated CCP repo exists. Drafted here under `ccp/`. | `[UNKNOWN]` (see §24) |
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

## §9. Intended architecture · `[SPECIFIED]` boundaries, `[NOT YET IMPLEMENTED]` everywhere

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

Cross-language transport (FFI / IPC / shared memory / files): `[UNKNOWN]` — §24.

---

## §10. Intended responsibility of each language · `[SPECIFIED]`

### Python — Application / Control Plane · `[NOT YET IMPLEMENTED]`
CLI, API, orchestration, jobs, configuration, UI/API boundary, monitoring.
**Must not become the native bit-processing core.** Python coordinates; it does not loop over
buffers.

### C++ — Bit Execution Engine · `[NOT YET IMPLEMENTED]`
XOR, POPCOUNT, comparison, masks, bit operations, SIMD, native execution.
**Must perform real operations on real buffers** — not a wrapper, not a stub, not a demo.

### Rust — Data / Storage Engine · `[NOT YET IMPLEMENTED]`
Streaming, file I/O, buffer management, block processing, storage, CCP format, reconstruction,
integrity, version management. **It owns the bytes.**

### Julia — Strategy / Mathematical Engine · `[NOT YET IMPLEMENTED]`
Cost model, representation selection, thresholds, policy, encoding decisions.
**Decides mathematically. Must not become the primary file-storage layer.**

---

## §11. Intended data flow · `[SPECIFIED]`

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
  → [Rust]  select candidate Base from the version graph      ← selection policy [UNKNOWN], §24
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

## §12. Intended block model · `[SPECIFIED]` in principle, `[UNKNOWN]` in every parameter

Specified: comparison and bit execution operate on **fixed-size blocks**, with contiguous
buffers and cache-friendly access (§20).

Not settled — do not choose these unilaterally:

- Block size (a specific value), and whether it is global, per-artifact, or configurable.
- Tail handling when an artifact is not a whole multiple of the block size.
- Whether block boundaries are fixed-offset (implied by XOR's position alignment) or
  content-defined. **Content-defined chunking is a different technique with different costs;
  adopting it is a maintainer decision, not an implementation detail.** See §23.
- Whether per-block popcount is retained as metadata or discarded after the decision.

---

## §13. Intended storage model · `[SPECIFIED]` in principle, `[UNKNOWN]` in detail

Specified: Rust owns storage; each version is stored either FULL or as Base + Delta, chosen by
cost; integrity is verifiable.

Not settled: on-disk organisation, whether deltas are stored inline or separately, retention,
garbage collection, concurrency, and whether an artifact's blocks can be shared across
versions.

---

## §14. Intended version model · `[SPECIFIED]` in principle, `[UNKNOWN]` in detail

- Versions form a **version graph**. FULL nodes are roots; DELTA nodes reference a Base.
- Every DELTA node must reach a FULL ancestor (I5).
- **Chain length is a first-class cost.** Reconstruction cost grows with it, so the cost model
  must price it, not only the bytes. Without this, the system optimises storage into unusable
  read latency — the classic fatal failure mode of delta stores.

Not settled: graph vs strict chain; may a delta chain against another delta; re-anchoring /
rebasing strategy; garbage collection; branching semantics.

---

## §15. Intended CCP format · `[SPECIFIED]` that it exists, `[NOT YET IMPLEMENTED]`, and **undesigned**

There is **no** container layout, header, magic bytes, block descriptor, checksum placement, or
format-version scheme. Nothing about the format has been designed.

**Why this is the most stability-critical artifact in the product:** a format mistake is written
into stored data and is not fixable the way code is. It deserves a written specification, and a
**format-version field from the very first byte**, before anything is written to disk in anger.

---

## §16. Intended MVP · `[UNKNOWN]` — not defined by the specification

The specification defines the full pipeline (§11) but **does not define an MVP scope**. I am
deliberately not inventing one.

What an MVP decision must answer — the maintainer's call:

1. Which languages are in the first slice? All four at once means four toolchains and three FFI
   boundaries before the first byte is stored.
2. Is the first slice **end-to-end and thin** (ingest → one delta → store → reconstruct →
   verify, single artifact, fixed block size, one encoding) or **one layer deep**?
3. Does the MVP include the Julia cost model, or a hard-coded threshold standing in for it?
   Note I4 — a hard-coded threshold is a *stand-in*, and must be labelled as such, not shipped
   as the cost model.
4. Is the MVP required to prove the *thesis* (measurable saving on real checkpoints) or the
   *mechanism* (correct round-trip)? These lead to very different first tasks.

See §25 for what I would do first, and why it is not code.

---

## §17. Intended benchmark model · `[UNKNOWN]` — not defined by the specification

Not settled, and it matters more than usual here because the product's *claim* is quantitative.
A benchmark model needs to name:

- The **corpus**: real ML checkpoints from real training runs, at minimum. Synthetic
  "similar" files will validate the mechanism and tell you nothing about the thesis, because
  synthetic similarity is position-aligned by construction (§23).
- The **metrics**: bytes at rest, bytes moved on write, bytes moved on read, reconstruct
  latency vs chain length, and `popcount(D)/bits(B)` as the sparsity measure.
- The **baselines** — and CCP must beat all three to matter: (a) N full copies, (b) each
  version compressed independently with a standard compressor, (c) a standard compressor over
  the concatenation, or an existing delta tool.

Baseline (b) is the one that most often kills naive delta schemes, and it must be measured, not
argued about.

---

## §18. Intended testing model · derived from the invariants, `[NOT YET IMPLEMENTED]`

The specification does not lay out a test strategy. Two properties, however, follow directly
from §7 rather than from my judgement:

- **Round-trip property test is the primary correctness gate** (I1, I2, I3). For arbitrary A
  and B: `A XOR (A XOR B) == B`, and end-to-end `reconstruct(store(B)) == B` byte-identical.
  Cheap, exhaustive by property, and catches the failure that matters most.
- **Cross-language conformance tests are unavoidable** in a four-language system: the same
  block XOR'd by the C++ engine and by a reference implementation must agree bit for bit.
  Without this, a SIMD bug becomes silent data corruption.

Everything else — framework choices, CI shape, coverage policy, how the four test runners are
driven — is `[UNKNOWN]`.

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

### Anti-drift procedure — when asked to implement a CCP feature

1. Read this document first.
2. Identify the target component from §9/§10.
3. Check whether that component actually exists (it currently does not — §22).
4. Do **not** search unrelated repository areas. This map says what belongs to CCP; nothing
   outside `ccp/` does.
5. If the component is missing, create it in the CCP structure (§21-B).
6. Update this document when the structure changes materially.

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

### A. CURRENT REALITY — what actually exists in this workspace

```
/home/user/harel/                  ← repository: alfassiharel-maker/harel
├── CLAUDE.md, README.md, docs/00-22, backend/, database/, tests/, apps/, ml/
│      ▲ UNRELATED PROJECT: "AI Sports Coach Platform"
│        An AI coach for triathletes/runners/cyclists — Python + FastAPI + Postgres.
│        NOT CCP. Not a component of CCP. Shares no code, concept, or dependency.
│        Do not modify. Do not reinterpret as CCP. Do not copy its architecture into CCP.
│
└── ccp/
    └── CLAUDE.md                  ← this file. THE ONLY CCP ARTIFACT THAT EXISTS.
```

**CCP implementation found: none.** No Python, C++, Rust, or Julia CCP source. No build system.
No tests. No benchmarks. No format. No dedicated CCP repository.

### B. CCP TARGET SYSTEM — what CCP is intended to contain · all `[NOT YET IMPLEMENTED]`

Component list from the specification. **The directory names below are placeholders for
vocabulary only — the layout is `[UNKNOWN]` and unapproved.** Do not cite a path here as though
it were discovered or decided.

```
CCP TARGET SYSTEM
├── Python application / control plane   CLI, API, orchestration, jobs, config, monitoring
├── C++ bit execution engine            XOR, POPCOUNT, compare, masks, SIMD
├── Rust data / storage engine          streaming, I/O, blocks, storage, format,
│                                       reconstruction, integrity, version management
├── Julia strategy engine               cost model, thresholds, policy, encoding selection
├── CCP binary format                   container layout — undesigned (§15)
├── Integration tests                   round-trip, cross-language conformance (§18)
└── Benchmarks                          corpus, metrics, baselines (§17)
```

**Never mix layer A and layer B.** A component in B does not exist merely because it is listed.

---

## §22. Current implementation reality

| Component | Status |
|---|---|
| Python control plane | `[NOT YET IMPLEMENTED]` |
| C++ bit execution engine | `[NOT YET IMPLEMENTED]` |
| Rust data / storage engine | `[NOT YET IMPLEMENTED]` |
| Julia strategy engine | `[NOT YET IMPLEMENTED]` |
| CCP binary format | `[NOT YET IMPLEMENTED]` — and undesigned (§15) |
| Version graph | `[NOT YET IMPLEMENTED]` |
| Cost model | `[NOT YET IMPLEMENTED]` — inputs undefined (§24) |
| Tests | `[NOT YET IMPLEMENTED]` |
| Benchmarks | `[NOT YET IMPLEMENTED]` |
| Build system | `[NOT YET IMPLEMENTED]` |
| CI | `[NOT YET IMPLEMENTED]` |
| Dedicated CCP repository | `[UNKNOWN]` (§24) |

**Build / test / lint commands: none exist, because nothing is built.**
Do not invent commands for this section. When a build system lands, record only commands you
have **executed successfully**, with their real output. A command in a context document that
does not work is worse than an absent one — the next agent will trust it and lose a cycle.

There is no legacy code, no generated artifact, and no deprecated path. That is the one
advantage of this starting point; the first structural decisions will be the hardest to reverse.

---

## §23. Security / integrity requirements, and dangerous assumptions

### Integrity requirements · `[SPECIFIED]`

- Lossless, byte-identical reconstruction, always (I2, I3).
- Content hash recorded at ingest, verified after every reconstruction. **Hash algorithm, and
  whether it is per-block, per-version or both: `[UNKNOWN]`.**
- Integrity failure is a hard error (I7).
- Format-version field present from the first byte written (§15).

Security beyond integrity — encryption at rest, access control, multi-tenant isolation, untrusted
input hardening: **`[UNKNOWN]`, not addressed by the specification.** Worth noting that a
delta-decoder consuming attacker-influenced files is a memory-safety surface, and that this is
precisely the argument for Rust owning I/O and C++ receiving only validated, sized buffers.

### DANGEROUS ASSUMPTIONS — read before writing CCP code

Items 1–3 are mathematical facts, not opinions. Each can invalidate the product thesis if built
on unexamined.

**23.1 — "XOR reduces size." It does not.**
`A XOR B` is *exactly* the same length as B. Raw XOR saves nothing at all. The saving comes
entirely from D being **sparse** (mostly zero) and therefore compressing or sparse-encoding
well. **The encoding step is where the product's value is realised, not the XOR.** Any plan
whose savings come from "XOR" without a named encoding is storing the same number of bytes with
extra steps.

**23.2 — "Similar versions produce sparse deltas." Only if changes are position-aligned.**
One inserted byte near the start shifts every subsequent byte, and `A XOR B` becomes dense noise
— often *less* compressible than B itself. Whether the target artifacts are alignment-preserving
is an **empirical question that should be measured on real files before the engine is built
around XOR.** For ML checkpoints it is plausible — fixed tensor layouts, same shapes, weights
changing in place — and plausible is not measured.

**23.3 — "Versions are the same length." XOR is undefined otherwise.**
Padding, truncation and tail handling all change the result and all need a decided, documented
policy. Silent padding is a correctness bug waiting to violate I3.

**23.4 — "Delta chains are cheap to read." They are not.**
Each hop is I/O plus an XOR pass over the full artifact. A long chain can make reconstruction
slower than having stored FULL copies while the storage graph still looks like a win. Price
chain length in the cost model (§14) or the system optimises itself into unusability.

**23.5 — "Python is fine for the first version."**
A Python prototype of the bit path will set the block sizes, buffer shapes and API that C++ then
inherits — and Python's convenient shapes are frequently the ones that defeat SIMD and locality.
I6 exists for this reason. A Python prototype is **throwaway measurement, never the reference
implementation.**

**23.6 — "Four languages is a starting point."**
Four languages means four toolchains, three FFI boundaries and a CI matrix before the first byte
is stored. The boundaries in §10 are the maintainer's decision and stand — but the **order of
construction is `[UNKNOWN]`**, and it is the highest-leverage open decision right now.

---

## §24. Open questions

Each is a decision this document deliberately refuses to make. Roughly ordered by leverage.

**Concept validation — these decide whether the rest is worth building**
1. Are real target artifacts (ML checkpoints especially) **position-aligned** between versions?
   What do `popcount(A XOR B)` and `compressed_size(D)` vs `compressed_size(B)` actually show on
   real files? (§23.2)
2. What **encoding** turns a sparse D into a real saving — sparse block index, RLE, general
   compression, something else? This is where the value is (§23.1).
3. Does CCP beat "just compress each version independently"? (§17 baseline b)

**Cost model — blocks the Julia layer entirely**
4. What exactly is "cost"? Bytes at rest, bytes moved, reconstruct latency — which, weighted how?
5. Does chain length enter the cost function, with what weight? (§23.4)
6. FULL-vs-DELTA thresholds: static, per-artifact, or learned?

**Format, blocks, storage**
7. Design the CCP format before or after a throwaway prototype? (§15)
8. Block size, and global vs per-artifact? Tail handling? (§12)
9. Unequal-length versions — what policy? (§23.3)
10. Integrity hash algorithm; per-block, per-version, or both?
11. Version graph vs strict chain; may a delta chain against a delta; rebasing; GC? (§14)

**Base selection**
12. How is a Base chosen for a new version — immediate predecessor, nearest by a cheap distance
    measure, most recent FULL? This likely dominates the achieved ratio in practice.

**Engineering**
13. Cross-language transport: FFI, IPC, shared memory, files? (§9)
14. Build system — one driver for all four toolchains, or one per layer?
15. **Build order:** which layer first, and what is the smallest end-to-end slice that proves
    the thesis? (§16, §23.6)
16. Does CCP stay in this repository long-term, or move to its own? A dedicated repo would let
    `ccp/CLAUDE.md` become a root `CLAUDE.md` and remove the unrelated-neighbour hazard entirely.

---

## §25. Project roadmap / status

**Current milestone: pre-implementation. Context established; nothing built.**

- Product concept, invariants, terminology, language boundaries: **`[SPECIFIED]`** (§2–§11, §19, §20).
- Block/storage/version models: `[SPECIFIED]` in principle, `[UNKNOWN]` in every parameter (§12–§14).
- CCP format: `[NOT YET IMPLEMENTED]` and undesigned (§15).
- MVP, benchmark model: `[UNKNOWN]` — not defined by the specification (§16, §17).
- Code, build, tests, CI: **none** (§22).

**The recommended next step is not implementation.** It is open question 1 — a measurement, on
real artifacts, of whether XOR deltas are actually sparse for the target workload, and how they
compare against per-version compression. It is cheap, needs no architecture, requires none of
the four toolchains, and it is the one result that could validate or invalidate the entire design
before anything is stood up. Building the engine first and measuring afterwards risks four
toolchains of work resting on §23.2.

That measurement is throwaway code by definition (§23.5) and should be labelled as such.
