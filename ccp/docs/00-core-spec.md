# CCP Core — specification of what is implemented

> This describes the Core that exists in `ccp/core/` and `ccp/capabilities/`.
> It is a specification of working code, not a proposal. Anything not
> implemented is marked as such and is absent from the tree rather than stubbed.

Classification follows the rule in `CCP_Technical_Clarifications` §11: every
element is **DEFINED BY USER**, **ENGINEERING DECISION**, or **OPEN DESIGN
QUESTION**. Nothing missing has been invented and presented as the original idea.

---

## 1. Input

A stream of **units**. A unit is an identified byte string: `(uid, data)`.

The Core is indifferent to what a unit means — file, function, tensor, record,
basic block. That indifference is deliberate: the clarifications (§10) leave the
unit of operation open and say the level at which CCP pays best must be found,
not assumed. Binding the Core to one level would settle that question in code.
The level is chosen by the `UnitSource` implementation; the Core runs unchanged
underneath.

* `InMemoryUnitSource` — units held in memory.
* `DirectoryUnitSource` — one unit per file under a root.
* Any other level is a new `UnitSource` and changes nothing else.

**DEFINED BY USER**: that CCP operates on shared structure between things.
**ENGINEERING DECISION**: representing "a thing" as `(uid, bytes)` and making the
unit level a plug point rather than a constant.

## 2. Output

A **CCP representation** — in memory a `CCPModel`, on disk a container of real
bytes (`CCP1`). From it, any unit can be reconstructed exactly, and questions can
be answered about units without reconstructing them.

The reported size of a representation is always the **measured length of the
serialised container**, index included. There is no formula in the reporting path.

## 3. Internal representation

```
CCPModel
  config      chunking parameters, similarity threshold
  records     uid -> UnitRecord
  literals    uid -> bytes          (units stored in full)

UnitRecord
  uid, size, digest
  kind = literal                    the bytes are stored
       | derived  -> base_uid + ChangeProgram
```

Each record carries the digest of the unit it must produce. Reconstruction is
verified against it; a mismatch raises rather than returning a plausible unit.

## 4. Base structure

A base is **one of the units, stored in full** — not a synthesised artefact.

Selection (`ccp/core/similarity.py`): the incoming unit is chunked; each chunk
digest is looked up in an index of already-stored units; a candidate scores the
**number of bytes** it shares (not chunks — one long shared chunk is worth more
than several short ones); the highest scorer above a similarity threshold wins.
If nothing clears the threshold the answer is `None` and the unit is stored in
full. "No base is worth using" is a real answer.

**Only full-stored units are indexed as candidates.** Every derived unit is
therefore exactly one hop from real bytes, and delta chains are impossible by
construction rather than by a check. The container reader re-verifies this
property so a crafted container cannot smuggle a chain in.

**ENGINEERING DECISION**: byte-weighted scoring, a 0.20 similarity threshold,
depth-1 (no chains), and the per-digest and per-candidate caps that keep the
build linear on degenerate input.

## 5. Change / delta structure

A change is a **change program** — an ordered instruction sequence:

```
COPY(src_offset, length)    reuse a run of the base
ADD(literal_bytes)          bytes unique to this unit
```

Encoded as opcode + unsigned varints. `encoded_size()` is exact and is
cross-checked against the real encoder on every encode, so a drift between the
cost model and the bytes cannot silently corrupt storage decisions.

**Why a program rather than the XOR pair list the storage experiment used.**
`experiments/ccp/FINDINGS.md` measured two structural failures of that encoding,
and this representation is chosen against them:

| Measured failure of XOR-over-fixed-regions | How the change program answers it |
| --- | --- |
| A duplicate shifted by one byte is invisible (fixed offsets stop lining up). | A shifted run is one `COPY` with a different `src_offset`. Tested: a one-byte prepend still yields >95% reuse. |
| Cost is ~3 bytes per changed byte, so dense change collapses. | A changed span is one `ADD`, whose cost is its length, not 3× its byte count. |
| Blind to redundancy below region granularity. | Chunks are content-defined and matches extend byte-wise in both directions. |

The deeper reason is that a program can be **read without being run**. An XOR
blob can only be applied. Every capability in §7 exists because instruction
spans are known without executing any instruction.

## 6. Copy / Change / Paste

| Step | Implementation | Location |
| --- | --- | --- |
| **Copy** | The base is stored once; each derived unit references it by uid, and each `COPY` instruction reuses a run of its bytes. | `similarity.py`, `change_program.Copy` |
| **Change** | Content-defined chunking finds shared structure; the differ confirms candidate matches **by byte comparison** (never by digest alone), extends them forward and backward maximally, and emits `COPY`/`ADD`. | `chunking.py`, `differ.py` |
| **Paste** | `paste(base, program)` executes the instruction sequence to produce the unit, bounds-checking every `COPY` against the base. | `change_program.paste` |

**The decision.** A unit is stored as a change program **only when the encoded
program is strictly smaller than the unit**; otherwise it is stored in full. A
unit is never forced into a delta to improve a statistic — the same rule the
storage engine enforced, and a test asserts it holds for every derived record.

## 7. Execution capability that is real today

These are the operations that fall directly out of the representation. Each
reports `bytes_touched`, counted on the real path, against the unit size a full
reconstruction would have touched.

* **`read_range(uid, offset, length)`** — materialise only the requested window
  by resolving only the instructions overlapping it. Measured on a real
  16,017-byte derived unit: a 256-byte read **touched 256 bytes (1.60%) and
  visited 1 of 3 instructions**. On a 27,347-byte unit stored as a single `COPY`:
  256 bytes touched (0.94%), 1 of 1 instruction.
* **`units_equal`** — settled from digests without reconstructing either unit.
  Returns `None` where the representation genuinely cannot settle it.
* **`shared_base_groups`** — which units are expressed against the same base;
  the grouping any batch operation would schedule around.
* **`reuse_report`** — how much of a unit is base and how much is its own.

This is the property that distinguishes the representation from compression, and
it is the honest form of the claim: **a compressed stream must be inflated from
the beginning to reach byte 8,000; a CCP unit reaches it by executing one
instruction.** That is a capability difference, not a ratio claim — and on ratio
alone gzip beats the Core on the measured corpus (see §10).

**Not implemented, deliberately**: anything that decides *how a target program
runs*, rewrites execution strategy, or manages itself. Those remain Open Design
Questions (§8). Building them to fill out a layer is the specific failure this
project guards against.

## 8. Open Design Questions

Unchanged from `experiments/ccp/PHASE1_UNDERSTANDING.md` except where the Core
has now closed one:

1. **Execution management** — how a representation should decide the *manner* of
   execution of a target program. Still open. The Core provides representation
   and partial execution over it; it does not schedule or rewrite anything.
2. **Semantic contract** — what must be preserved when execution is altered.
   Still open. The Core's contract today is the strict one: exact bytes,
   verified. That is a floor, not the general answer.
3. **Unit of operation** — which level pays best. Now *testable* rather than
   open in principle: the Core accepts any level through `UnitSource`, so the
   question can be answered by measurement instead of argument.
4. **Understanding threshold** — when analysis of a language is sufficient to
   manage it. Untouched; the Core does not analyse languages.
5. **Self-management / meta-layer** — blocked on #1.

## 9. Layering

```
ccp.core          the algorithm and its representation      IMPLEMENTED
ccp.capabilities  work performed on the representation      IMPLEMENTED
ccp.runtime       execution strategy under a contract       NOT BUILT
ccp.integration   project import, language analysis         NOT BUILT
ccp.product       build orchestration, packaging            NOT BUILT
ccp.ui            the commercial surface                    NOT BUILT
```

`ccp.core` imports **only the standard library** and nothing from the rest of the
repository, so the algorithm stays independently testable. `ccp.capabilities`
imports `ccp.core` and nothing above it. `ccp/cli.py` is transport only: it parses
arguments, calls the Core, prints what the Core measured. Layers that are not
built are **absent from the tree**, not present and empty.

## 10. Measured behaviour of the Core

Real input: five successive revisions of this repository's `backend/` tree, 111
files, 723.14 KB — the near-duplicate shape the product targets.

| | measured |
| --- | --- |
| units | 111 (60 stored in full, 51 as change programs) |
| original | 723.14 KB |
| container | 449.95 KB (payload 440.86 KB + index 9.09 KB) |
| **saving** | **37.78%**, index included |
| reconstruction | 111/111 verified against recorded digests |

Beside general-purpose compressors on the same input, stated plainly:

| | bytes |
| --- | --- |
| tar | 849,920 |
| **ccp** | **460,745** |
| gzip‑6 | 213,495 |
| xz‑6 | 92,416 |
| ccp + gzip‑6 | 135,564 |

**gzip and xz both beat the Core on size, and that is the expected result.** The
Core is not a compressor: it removes duplication *between* units and does nothing
about the redundancy *inside* a unit, which is what an entropy coder exploits.
The two are complementary — CCP followed by gzip (135,564) beats gzip alone
(213,495) by 36% — and composition, not replacement, is the honest framing. Any
claim that CCP supersedes compression is contradicted by this table.
