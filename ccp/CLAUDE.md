# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Scope: the `ccp/` subtree only.** The repository root is a different product
(`../CLAUDE.md` — AI Sports Coach Platform). Nothing in this document applies to it,
and nothing in it applies here. Do not merge the two.

---

## ⚠️ READ THIS FIRST — the status of this document

**No CCP code exists.** Not partially, not in a branch, not in another directory.
Verified 2026-08-20 against: the working tree, every commit on every branch
(`git rev-list --all` → zero `.rs`, `.cpp`, `.hpp`, `.jl`, `Cargo.*`, `CMakeLists.txt`),
and the account's connected repository list (one repo: `alfassiharel-maker/harel`,
which is the sports platform).

Therefore this document is **100% specification, 0% code map**. Every statement below
traces to exactly one source: the maintainer's product brief of 2026-08-20. There is no
code to cross-check it against, so there are no code-vs-doc contradictions to report —
and no discovered architecture either.

**What this means for you, the next agent:**

1. Treat every "layout", "path", and "file name" in §8 as **PROPOSED, NOT DECIDED**. They
   are placeholders so we have vocabulary. Nothing has been approved. Do not cite them as
   though they were found in the repository.
2. The maintainer's brief is authoritative on *product definition, invariants, and language
   responsibilities* (§2–§7). It is silent on most engineering decisions. Where it is silent,
   **§25 Open Questions is the answer** — not your own judgement.
3. Anything in §23 marked **DANGEROUS ASSUMPTION** will break the product if it goes
   unresolved. Several of them are mathematical facts, not opinions. Read §23 before writing
   a single line of CCP code.
4. When real code lands, **rewrite the sections it makes obsolete** — do not append a second
   version alongside them. A context document with two answers is worse than none.

---

## 1. Project identity

| | |
|---|---|
| **Name** | CCP — Copy, Change, Paste |
| **Category** | Differential Binary Versioning & Storage Infrastructure |
| **Not** | "an XOR compression script" |
| **Repository** | none yet; currently drafted inside `alfassiharel-maker/harel` under `ccp/` |
| **Languages planned** | Python (control), C++ (bit execution), Rust (data/storage), Julia (strategy) |
| **Maintainer language** | writes in Hebrew; **repository artefacts — code, comments, docs, commit messages — stay in English** |

---

## 2. Product definition

Instead of storing every version of a binary artifact in full, CCP identifies what changed
between an existing version and a new one and stores the change:

```
Base + Delta → Target Version
```

The product is the **infrastructure around that idea**: versioned binary storage,
differential representation, cost-driven encoding choice, reconstruction, and integrity
verification. XOR is one low-level primitive inside the engine — the smallest part of the
product, and the least valuable on its own.

**Target workloads** (where many large, highly similar versions exist):

- AI/ML checkpoints
- model artifacts
- large binary datasets
- large versioned binary files

**The commercial thesis being tested:** that in these workloads, storage and data movement
can be materially reduced. This is a hypothesis to *measure*, not an assumption to build on.

---

## 3. Core mathematical model

At the bit primitive:

```
D = A XOR B          # delta between base A and target B
B = A XOR D          # reconstruction — the load-bearing direction
A = B XOR D          # XOR is its own inverse
```

Properties that matter and are used by the design:

- **Self-inverse / symmetric.** No separate "apply" algorithm is needed; reconstruction *is*
  the same operation. This is why XOR was chosen over a general diff algorithm.
- **`popcount(D)` = Hamming distance** = the exact number of changed bits. This is the
  cheapest possible measure of "how different are these two versions", and it is the natural
  input to the cost model (§12, §4).
- **Position-aligned.** XOR compares byte *i* of A to byte *i* of B. It is blind to
  insertions and shifts. See §23.2 — this is the single largest technical risk in the concept.
- **Length-sensitive.** `A XOR B` is only defined for equal lengths. Version pairs of
  different sizes need a decided policy. See §23.3 and §25.

---

## 4. Core invariants

These are the properties the system must never violate. A change that breaks one of these is
a product defect regardless of how well it performs.

| # | Invariant | Enforcement |
|---|---|---|
| **I1** | `B == A XOR D` for every stored delta, bit for bit | round-trip test on every write path; must be a hard gate, not a sampled check |
| **I2** | Reconstruction is **lossless**. Never approximate, never lossy, never "close enough" | integrity verification (§12) on every reconstruct |
| **I3** | A stored version is retrievable **byte-identical** to what was ingested | content hash recorded at ingest, verified after reconstruct |
| **I4** | The system **never assumes DELTA is better**. It measures, then chooses FULL or DELTA/other encoding | the cost model is mandatory on the write path, not an optimisation |
| **I5** | A FULL version is always a valid, self-sufficient reconstruction root | version graph must guarantee every node reaches a FULL ancestor |
| **I6** | Python never performs the bulk bit processing | review rule; see §9 |
| **I7** | Integrity failure is a **hard error**, never a warning, never silently repaired | error contract |

**I4 restated because it is the commercial heart of the product:** a differential system that
blindly deltas is worse than no system. The measurement *is* the product.

---

## 5. Real system goals

The eventual end-to-end pipeline, exactly as specified by the maintainer:

```
Input
→ Streaming
→ Blocks
→ Comparison
→ Bit execution
→ Change representation
→ Cost analysis
→ FULL/DELTA decision
→ Storage
→ Version graph
→ Reconstruction
→ Integrity verification
```

Goals, stated as measurable outcomes rather than features:

1. Store N similar large versions in materially less space than N full copies — **with the
   reduction measured, per artifact, not assumed**.
2. Reduce **data movement**, not only bytes at rest (this is a distinct goal; a delta that is
   small but requires reading the whole base chain moves more data, not less).
3. Reconstruct any stored version losslessly and verifiably.
4. Make the FULL-vs-DELTA decision **explainable** — for any stored version, we can say what
   the alternatives cost and why this one was chosen.
5. Keep the bit-execution path fast enough that the cost model, not the XOR, is the bottleneck.

---

## 6. Non-goals

- **Not** a general-purpose compressor competing with zstd/xz on single files.
- **Not** a text/source diff tool — this is binary, position-based.
- **Not** a lossy or approximate store. See I2.
- **Not** a distributed system, replication layer, or object-store replacement (at least not
  in any currently specified scope).
- **Not** a Python bit-processing engine. See I6, §9.
- **Not** Julia as the file-storage layer. Julia decides; it does not own bytes on disk.
- **Not** direct control of CPU cache residency. See §9 "Cache rule".

---

## 7. Product terminology

Use these words consistently. Inconsistent naming here is the main drift risk in a
multi-language project.

| Term | Meaning |
|---|---|
| **Base** | the existing version a delta is computed against (`A`) |
| **Target** | the new version being stored (`B`) |
| **Delta** | the change representation (`D`), *not necessarily* raw `A XOR B` — see Encoding |
| **FULL** | a version stored in its entirety; a reconstruction root |
| **DELTA** | a version stored as Base + Delta |
| **Block** | fixed-size unit of comparison and bit execution |
| **Encoding** | how a computed change is *represented* on disk (raw XOR, sparse index, RLE, compressed…). Distinct from the FULL/DELTA decision |
| **Cost model** | the mathematical evaluation choosing between representations |
| **Representation decision** | the output of the cost model for one version |
| **Version graph** | the structure recording which version derives from which |
| **Reconstruction** | materialising a version from its FULL ancestor plus the delta chain |
| **Chain length** | number of deltas traversed to reconstruct — the cost that grows silently |
| **Integrity verification** | proof that a reconstruction equals what was ingested |
| **CCP format** | the on-disk container format. **Does not exist. Not specified.** See §13 |

Naming collisions to avoid: "delta" (the concept) vs "DELTA" (the storage decision) vs the
encoded bytes. Prefer *change representation* for the concept and *DELTA decision* for the
choice.

---

## 8. Current repository structure — and the PROPOSED layout

**Actual, today:**

```
ccp/
└── CLAUDE.md          ← this file. The only CCP artifact that exists.
```

**PROPOSED — NOT DECIDED, NOT APPROVED, NOT BUILT.** Written only so §9–§11 have names to
refer to. Anyone may still change all of it; do not treat a path here as a commitment:

```
ccp/
├── control/     (Python)  API, CLI, orchestration, jobs, config, monitoring
├── bitexec/     (C++)     XOR, POPCOUNT, comparison, masks, SIMD
├── dataeng/     (Rust)    streaming, I/O, blocks, storage, CCP format, reconstruction, version mgmt
├── strategy/    (Julia)   cost model, thresholds, policy, encoding selection
├── docs/                  specifications and ADRs
└── tests/                 cross-language conformance, round-trip, integrity
```

---

## 9. Module responsibility map

Authoritative source: maintainer's brief, verbatim intent. This is the one part of the design
that **is** decided.

### Python — Application / Control Plane
Owns: API, CLI, orchestration, jobs, configuration, the UI/API boundary, monitoring.
**Must NOT become the core bit-processing engine.** Python coordinates; it does not loop over
buffers.

### C++ — Bit Execution Engine
Owns: XOR, POPCOUNT, comparisons, masks, bit operations, SIMD, native performance.
**Must perform real operations on real buffers** — not a wrapper, not a stub, not a
demonstration.

### Rust — Data / Storage Engine
Owns: streaming, file I/O, buffers, block processing, storage, the CCP format,
reconstruction, integrity, version management.
**Must perform real data management** — it owns the bytes.

### Julia — Strategy / Mathematical Engine
Owns: cost model, representation decision, thresholds, policy, encoding selection.
**Decides mathematically.** Does **not** become the main file-storage layer.

### Cache / CPU rule — read this, it corrects an earlier wording

Earlier framing said "C++ = L1/L2, Rust = L3/RAM". **Do not interpret that literally.** We do
not directly control ordinary CPU cache residency; the hardware decides. The correct
interpretation is that C++ should use:

- cache-friendly access patterns
- fixed-size blocks
- contiguous buffers
- alignment where useful
- SIMD where useful
- streaming / locality-aware execution

Any code, comment, or doc asserting direct control over cache levels is wrong and should be
corrected.

---

## 10. Dependency map

Intended direction of dependency (derived from §9's responsibilities; **the boundaries are
decided, the transport mechanism is not** — see §25):

```
                  ┌────────────────────────────┐
   Python  ───────▶  orchestrates everything   │
  (control)         └────────────────────────────┘
       │
       ├──────────▶ Rust  (dataeng)  ── owns bytes, blocks, format, versions
       │                   │
       │                   ├────────▶ C++   (bitexec)  ── XOR / POPCOUNT on buffers
       │                   │
       └───────────────────┴────────▶ Julia (strategy) ── cost model → FULL/DELTA
```

Rules that follow, and must hold:

- **C++ depends on nothing.** It receives buffers and returns results. No I/O, no storage
  awareness, no policy. This is what makes it testable and replaceable — protect it the way
  a pure library is protected.
- **Julia depends on nothing but numbers.** It receives measurements (sizes, popcounts,
  chain lengths) and returns a decision. It must not read files.
- **Rust must not embed the policy.** It measures and stores; it asks Julia what to do.
- **Python must not bypass Rust** to touch storage, nor bypass C++ to compute bits.
- No cycles. If a design needs C++ to call back into Rust, the design is wrong.

---

## 11. Data-flow map

Write path (ingest a new version):

```
artifact bytes
  → [Rust] stream in, split into fixed-size blocks
  → [Rust] select candidate Base from the version graph          ← policy input, see §25
  → [C++]  per block: XOR(base_block, target_block), POPCOUNT
  → [Rust] assemble the change representation
  → [Julia] cost analysis: size(FULL) vs size(encoded delta) + chain cost
  → [Julia] decision: FULL | DELTA | other encoding
  → [Rust] write to storage in CCP format; record hash
  → [Rust] update version graph
```

Read path (reconstruct a version):

```
version id
  → [Rust] walk version graph to the FULL ancestor
  → [Rust] load FULL + the delta chain
  → [C++]  XOR-apply each delta block by block
  → [Rust] integrity verification against the recorded hash   ← I3, hard gate
  → bytes out
```

**The asymmetry to keep in mind:** the write path is where the cleverness lives, but the read
path is where correctness is proved and where chain length silently hurts. Every write-path
decision should be evaluated by what it does to the read path.

---

## 12. Version / storage model

Decided in principle, undecided in every detail:

- Versions form a **graph** (or forest): FULL nodes are roots, DELTA nodes point at a base.
- Every DELTA node must reach a FULL ancestor (I5).
- **Chain length is a first-class cost.** Reconstruction cost grows with it, so the cost model
  must price it, not only the bytes. Without this, the system optimises storage into
  unusable read latency — a classic and fatal failure mode for delta stores.
- Integrity is verified by a content hash recorded at ingest and checked after reconstruction.
  Algorithm not chosen (§25).

Undecided: graph vs strict chain, rebasing/re-anchoring strategy, garbage collection,
retention, concurrency, whether deltas may chain against other deltas at all.

---

## 13. CCP format status

**DOES NOT EXIST. NOT SPECIFIED. NOT DESIGNED.**

No container layout, no header, no magic bytes, no versioning scheme, no block descriptor, no
checksum placement. When it is designed it becomes the most stability-critical artifact in the
product — a format mistake is permanent in a way code is not, because it is written into
stored data. It deserves a written specification and an explicit format-version field before
any bytes are written to disk in anger.

---

## 14–17. Implementation status

| Layer | Status |
|---|---|
| Python control plane | ⏳ **specification only** — nothing exists |
| C++ bit execution | ⏳ **specification only** — nothing exists |
| Rust data/storage | ⏳ **specification only** — nothing exists |
| Julia strategy | ⏳ **specification only** — nothing exists |
| CCP format | ⏳ **not even specified** (§13) |
| Tests | ⏳ none |
| Build system | ⏳ none |
| CI | ⏳ none |

- **Actually implemented (§15):** nothing.
- **Partial (§16):** nothing.
- **Specification only (§17):** everything above, plus every statement in this document.

There is no legacy code, no generated artifact, and no deprecated path — the one advantage of
this starting point. Keep it: the first structural decisions will be the hardest to reverse.

---

## 18. Known technical debt

None in code (there is none). Debt already present in the *specification*:

1. **The CCP format is undesigned** while the pipeline that depends on it is specified (§13).
2. **The cost model has no defined inputs.** "Cost" is named but never quantified — bytes at
   rest? bytes moved? reconstruction latency? all three, weighted? Julia cannot be
   implemented until this is answered (§25).
3. **Base selection is unspecified.** §11 needs a Base and the brief never says how one is
   chosen. This decision probably dominates the compression ratio in practice.
4. **The four-language boundary has no transport decision** (FFI, IPC, files, shared memory).
   This is a build-system and performance decision that will be expensive to change late.
5. **"Cache" wording drift** — the corrected interpretation in §9 must be applied anywhere the
   old L1/L2 framing reappears.

---

## 19. Known contradictions

**Between documents and code: none — there is no code.** This section exists to be filled the
moment code lands, and to record the one contradiction that already exists:

| # | Contradiction | Which source wins |
|---|---|---|
| C1 | Earlier wording: "C++ = L1/L2, Rust = L3/RAM" vs the corrected rule that hardware decides cache residency | **The corrected rule wins** (§9). The maintainer superseded the old wording explicitly in the 2026-08-20 brief. |
| C2 | "CCP is a differential *versioning and storage engine*" vs any framing of it as XOR compression | **Engine framing wins.** XOR is a primitive, not the product (§2). |

Record future disagreements here rather than silently editing a source. **Do not "fix" one
side of a contradiction without the maintainer's decision.**

---

## 20. Authoritative-source hierarchy

Highest authority first. When two sources disagree, the higher one wins and the disagreement
gets a row in §19.

1. **The maintainer's explicit decision** (in conversation, in Hebrew or English).
2. **Tests** — once they exist, a passing round-trip test outranks any prose about behaviour.
3. **Code** — for *how something actually behaves* (algorithms, formulas, formats as
   implemented).
4. **The CCP format specification** — for on-disk layout, once written. Outranks code, because
   stored bytes outlive code.
5. **ADRs** — for *why* a choice was made and what was rejected. None exist yet.
6. **This document** — for boundaries, terminology, invariants, and status.
7. **README / prose docs** — lowest. Assume stale unless corroborated.

Note the deliberate split, mirroring the sibling project's convention: **code is truth for
behaviour; the specification is truth for boundaries and formats.**

---

## 21. Build / test commands — actually verified

**None. There is nothing to build and nothing to run.**

Do not invent commands in this section. When a build system lands, put only commands here
that you have **executed successfully in this repository** and record what they output. A
command in a context document that does not work is worse than an absent one — the next agent
will trust it and waste a cycle.

Two properties worth designing the test setup *for*, before writing it:

- **A round-trip property test is the primary correctness gate** (I1/I2): for arbitrary A and
  B, `A XOR (A XOR B) == B`, and end-to-end `reconstruct(store(B)) == B`. This is cheap,
  exhaustive-by-property, and catches the failure that matters most.
- **Cross-language conformance tests** are unavoidable in a four-language system: the same
  block XOR'd by C++ and by a reference implementation must agree bit for bit.

---

## 22. Important files and why they matter

| File | Purpose | Layer | Authoritative? | Type |
|---|---|---|---|---|
| `ccp/CLAUDE.md` | this context document | — | yes, for boundaries/terminology/status (§20 rank 6) | spec |
| `../CLAUDE.md` | **a different product** (AI Sports Coach). Not related to CCP despite living in the same repo. | — | yes, for that product | spec |

That is the complete list. The file index (**map C**) is intentionally two rows long — this is
the anti-dispersion property the maintainer asked for, and it is worth preserving: keep the
number of files an agent must remember small, and keep this table current as the authority on
which files matter.

---

## 23. Dangerous assumptions

**Read this section before writing CCP code.** Items 1–3 are mathematical facts, not
opinions, and each can invalidate the product thesis if built on unexamined.

### 23.1 DANGEROUS ASSUMPTION — "XOR reduces size"
**It does not.** `A XOR B` is *exactly* the same length as B. Raw XOR saves nothing at all.
The saving comes entirely from the fact that D is *sparse* (mostly zero bytes) and therefore
compresses or sparse-encodes well. **The encoding step is where the product's value is
realised, not the XOR.** Any plan whose savings come from "XOR" without a named encoding is
storing the same number of bytes with extra steps.

### 23.2 DANGEROUS ASSUMPTION — "similar versions produce sparse deltas"
Only true when changes are **position-aligned**. A single inserted byte near the start shifts
every subsequent byte, and `A XOR B` becomes dense noise — often *less* compressible than B
itself. Whether the target workloads are actually alignment-preserving is an **empirical
question that should be measured on real artifacts before the engine is built around XOR.**
For ML checkpoints this is plausible (fixed tensor layouts, same shapes, weights change in
place) — plausible is not measured. Content-defined chunking / shift-tolerant deltas are a
different technique with different costs; adopting one is a maintainer decision (§25), not an
implementation detail.

### 23.3 DANGEROUS ASSUMPTION — "versions are the same length"
`A XOR B` is undefined otherwise. Padding, truncation, and block-level tail handling all
change the result and all need a *decided, documented* policy. Silent padding is a
correctness bug waiting to violate I3.

### 23.4 DANGEROUS ASSUMPTION — "delta chains are cheap to read"
Each hop is I/O plus an XOR pass over the full artifact. A long chain can make reconstruction
slower than having stored FULL copies, while the storage graph still looks like a win. **Price
chain length in the cost model** (§12) or the system optimises itself into unusability.

### 23.5 DANGEROUS ASSUMPTION — "Python is fine for the first version"
A Python prototype of the bit path will set the block sizes, buffer shapes, and API that C++
then has to inherit — and Python's convenient shapes are frequently the ones that defeat SIMD
and cache locality. I6 exists for this reason. If a prototype is written in Python, treat it
as **throwaway measurement, never as the reference implementation.**

### 23.6 DANGEROUS ASSUMPTION — "four languages is a starting point"
Four languages means four toolchains, four test runners, three FFI boundaries, and a CI matrix
before the first byte is stored. The boundaries in §9 are the maintainer's decision and stand
— but the *order of construction* is open, and the sequencing question in §25 is the highest
leverage decision available right now.

---

## 24. Rules future coding agents must not violate

1. **Do not claim a component exists.** Check, then state. §14–17 is the status register —
   update it, don't contradict it.
2. **Do not put bulk bit processing in Python** (I6). Do not put storage in Julia. Do not put
   policy in Rust. Do not put I/O in C++.
3. **Never break I1/I2/I3.** No lossy path, no approximate reconstruction, no "close enough".
   A round-trip gate on every write path is not optional.
4. **Never assume DELTA wins** (I4). The cost model runs; the decision is recorded and
   explainable.
5. **Integrity failure is a hard error** (I7). Never warn-and-continue, never auto-repair.
6. **Do not invent architecture.** Where this document says "undecided", the answer is to ask,
   not to choose. Add to §25.
7. **Do not silently reconcile a contradiction.** Record it in §19 with which source wins.
8. **Do not write to the CCP format before it is specified** (§13). Include a format-version
   field from the first byte.
9. **Repository artefacts stay in English** — code, comments, docs, commit messages. Reply to
   the maintainer in Hebrew when they write in Hebrew.
10. **Do not touch the repository root or the sports platform.** Different product, different
    context document, different conventions.
11. **Keep this document honest.** When code lands, rewrite the sections it obsoletes.
    Never leave two competing descriptions of the same thing.

---

## 25. Open questions

Blocking, roughly in order of leverage. Each one is a decision this document deliberately
refuses to make on its own.

**Concept validation (highest leverage — these decide whether the rest is worth building)**
1. Are real target artifacts (ML checkpoints in particular) **position-aligned** between
   versions? What does a measurement on real files show for `popcount(A XOR B)` and for the
   compressed size of D vs B? (§23.2)
2. What **encoding** turns a sparse D into a real saving — sparse block index, RLE,
   general-purpose compression, something else? This is where the value is (§23.1).

**Cost model (blocks the Julia layer entirely)**
3. What exactly is "cost"? Bytes at rest, bytes moved, reconstruction latency — which,
   weighted how?
4. Does chain length enter the cost function, and with what weight? (§23.4)
5. What are the FULL-vs-DELTA thresholds, and are they static, per-artifact, or learned?

**Storage & format**
6. Does the CCP format need designing before or after a throwaway prototype? (§13)
7. Block size — fixed at what value, and configurable per artifact or global?
8. How are unequal-length versions handled? (§23.3)
9. Integrity hash algorithm, and is it per-block, per-version, or both?
10. Version graph vs strict chain; may a delta chain against another delta? Rebasing? GC?

**Base selection**
11. How is a Base chosen for a new version — immediate predecessor, nearest by some cheap
    distance measure, most recent FULL? (§18.3)

**Engineering**
12. What is the transport across the four languages — FFI, IPC, shared memory, files?
13. What is the build system, and does it drive all four toolchains or one per layer?
14. **What order do we build in?** Which layer first, and what is the smallest end-to-end
    slice that proves the thesis? (§23.6)
15. Does CCP live in this repository long-term, or move to its own?

---

## 26. Current milestone / status

**Milestone: pre-implementation. Context established; nothing built.**

- Product concept, invariants, and language boundaries: **captured and decided** (§2–§9).
- Everything else: **open** (§25).
- Code: **none**. Build: **none**. Tests: **none**. Format: **unspecified**.

The maintainer's stated sequence is understood: understanding first, then "implement feature
X" with the agent already knowing where it belongs, what governs it, and how it is tested.
This document is the "understanding" deliverable.

**The recommended next step is not implementation.** It is question 1 in §25 — a measurement,
on real artifacts, of whether XOR deltas are actually sparse for the target workload. It is
cheap, it needs no architecture, and it is the one result that could invalidate or validate
the entire design before any of the four toolchains is stood up.
