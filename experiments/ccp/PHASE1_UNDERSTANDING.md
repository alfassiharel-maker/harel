# CCP Forge — Phase 1: Deep Technical Understanding (before implementation)

> **מטרת המסמך (תקציר בעברית).** זהו תוצר שלב ההבנה. הוא מוכיח מה CCP עושה, מה
> הוא *לא* עושה, מהו האלגוריתם האמיתי, מהו הייצוג, מהו מנגנון הביצוע, מהו
> self‑management, מה הוא רק demo/oracle, ומהו ה‑code path האמיתי של המוצר.
> לאחר מכן הוא מציג ארכיטקטורה שמפרידה במפורש בין
> Research / Algorithm / Core Engine / Runtime / Integration / Product / UI,
> ועונה על 16 שאלות ה‑Stop Condition. כל טענה מסומנת כאחת משלוש:
> **DEFINED BY USER**, **PROPOSED ENGINEERING DESIGN**, או **OPEN DESIGN QUESTION**.
> אין כאן המצאה של חלקים חסרים. חלק אחד מרכזי — המעבר מ*ייצוג* לניהול *ביצוע* —
> מסומן כשאלת התכנון הפתוחה העליונה ודורש החלטה שלך לפני implementation.

This document does not add features, theories or layers. It is the stop point of
the understanding phase: enough to build and measure CCP for real, and no more.
Every empirical claim is traceable to a run in `reports/`, verified by SHA‑256;
every non‑empirical claim is tagged with its source of authority.

Sources of truth for this document:

* `CCP_Forge_Product_Direction.pdf` — the vision (the user's).
* `CCP_Technical_Clarifications.pdf` — the binding rules (the user's).
* `experiments/ccp/` — the existing, running proof‑of‑concept and its
  `FINDINGS.md`. This is the only part of CCP that has been *measured*.

---

## 0. The four‑way separation the clarifications demand

The clarifications open by requiring an absolute separation between four things.
Collapsing any two of them is the failure mode the whole project is guarding
against. Stated once, precisely, and used consistently for the rest of this doc:

| # | Thing | One‑sentence definition | State today |
| - | ----- | ----------------------- | ----------- |
| 1 | **CCP Principle** | Copy → Change → Paste: find shared structure, keep one **base**, store only the **deltas**, and *later* reuse that representation to manage execution. A theoretical idea, not code. | Defined by user |
| 2 | **CCP Algorithm** | The formal procedure that performs the principle on concrete data: how a base is chosen, how deltas are detected and represented, how Copy/Change/Paste run bit‑for‑bit. | Partially implemented (storage); execution part undefined |
| 3 | **CCP Core Implementation** | The real code whose actual execution *is* the algorithm — not a demo that prints an expected number. | Exists for storage in `ccp_full_experiment.py` |
| 4 | **Commercial Product (CCP Forge)** | The wrapper — ZIP import, analysis, build, packaging, UI — that lets a user apply CCP to their software. Not CCP itself. | Not started (correctly) |

Everything below keeps these four apart.

---

## 1. What CCP is, and what it is not (the required proof of understanding)

### What CCP *does*

CCP represents a body of data as **one shared base plus a set of changes
(deltas)** against that base, such that the original is reconstructed
losslessly — or within an explicitly defined semantic contract. When many units
of data share a base and each differs from it in only a few places, storing
`base + Σ deltas` costs less than storing every unit in full. That is the entire
mechanism, and it is real: `experiments/ccp/` encodes real files into a real
container, decodes them, and checks the result against the original SHA‑256.

### What CCP is **not** (per clarifications §8, and confirmed by measurement)

* **Not a compressor.** A compressor exploits the *marginal byte distribution*
  inside a window. CCP exploits *repeated whole structures* across long ranges.
  These are different properties of the same bytes — the GPT‑2 per‑tensor result
  (`reports/ccp_layer_gpt2.txt`) shows weights that gzip compresses 7% and CCP
  compresses 0.00%, because the redundancy is in the exponent byte‑plane, not in
  repeated regions. **Do not build CCP as `compression algorithm בלבד`.**
* **Not a Python optimizer, not "just a compiler."** (clarifications §8)
* **Not a `ZIP → EXE` wrapper.** (product direction §0)
* **Not `Claude Linux = Target Windows`.** The agent environment and the target
  environment are different machines. (product direction §11)
* **Not an "AI that understands code" by assumption.** ML/AI is *allowed* only if
  proven useful; the goal is understanding and managing logic, not using AI for
  its own sake. (clarifications §9)

### The real algorithm (as it exists today, storage form)

`Input bytes → split into regions → group regions that may share a base →
choose a base per group → for each region: cost delta‑vs‑verbatim, keep the
cheaper → serialise container → decode → verify SHA‑256.` Copy = reuse the base;
Change = the XOR delta list; Paste = `target[i] = base[i] ^ delta[i]`. This is
implemented, not simulated (§3 below).

### The representation

A binary container: `header │ region‑index │ bases │ deltas │ verbatim regions`.
A delta is a list of `(position, xor_value)` pairs. The **measured length of
this container on disk** is the reported CCP size — never a formula.

### The execution mechanism

**This is the load‑bearing gap.** Today CCP has a *storage/representation*
mechanism. It has **no execution‑management mechanism**. The container must be
fully decoded before the data (e.g. a model) can be used; no computation runs
*on* the CCP representation. The leap from "smaller representation" to "manages
how the program executes" is **not built and not evidenced** — see §3 item 11
and the Open Design Questions in §8.

### Self‑management

Defined by the user (product direction §4): a meta‑layer that may reshape the
management layer *within human‑defined constraints*. **Nothing of this exists.**
It presupposes an execution‑management layer that does not exist either. Marked
OPEN, not invented.

### What is only demo / oracle

In the experiment, the analytic cost model `model_delta_bits()` computes the
*expected* container size and is used **only** to cross‑check the encoder — it is
an oracle, not the algorithm. The reported number is always the real container.
This is exactly the demo‑vs‑implementation discipline the clarifications require
(§3): if a function computes an expected result for comparison, it is labelled
test/oracle and kept out of the algorithm's code path.

### The real product code path (target, once the Core is proven)

`Project/ZIP → Analysis → actual CCP processing → CCP Core → Runtime →
Build/Package → Application.` The UI/import/build/packaging are the wrapper. The
Core is the only place CCP itself lives.

---

## 2. The problem the product is meant to address — stated as hypothesis, not fact

The industrial framing (from the Phase‑1 brief) is the widening gap between the
growth of compute capability and the growth of data/AI computational demand —
data volume, memory, bandwidth, computation, storage, energy, infrastructure.
The hypothesis is that reducing *what must be represented, moved, stored and
executed* is a lever alongside faster hardware.

**This is a business/technical hypothesis to be measured, not a settled result.**
It is not this system's job to "solve the global AI crisis." The measurable
proxies CCP must be tested against are: data volume, memory footprint, memory
bandwidth, execution work, bit‑level representation cost, and energy‑related
compute proxies. The existing experiment already measures the first three and
part of the fourth (§4).

---

## 3. The algorithm, item by item (the clarifications §2 checklist)

For each required item: what it is, where it lives in code, and its authority
tag. Where an item is undefined, it is marked OPEN and **not** invented.

| # | Question (clarifications §2) | Answer / location | Tag |
| - | ---------------------------- | ----------------- | --- |
| 1 | **What is the input?** | A byte stream (a file, mmap'd). Unit‑of‑operation choice is separate — see §6. | DEFINED (storage) |
| 2 | **What is the output?** | A CCP container (bytes) + a verified lossless decode. | DEFINED |
| 3 | **What structure is searched for?** | Fixed‑size, offset‑aligned regions that are exact or near duplicates of one another. | DEFINED |
| 4 | **How is shared structure identified?** | Banded sketch over sampled byte positions (`region_signature`, `cluster_regions`), biased to find few‑byte‑difference regions. | PROPOSED ENGINEERING (a chosen heuristic, not the user's) |
| 5 | **How is a Base chosen?** | Medoid of a cluster up to a size cap, else lowest‑index member (`select_base`). | PROPOSED ENGINEERING |
| 6 | **How are Changes/Deltas identified?** | Bytewise XOR of region vs base; non‑zero positions are the changes (`delta_entries`). | DEFINED |
| 7 | **How are they represented?** | `(position, xor_value)` pairs; positions sized to region width. | DEFINED |
| 8 | **How is Copy performed?** | The base region is stored once and referenced by index from every member of its cluster. | DEFINED |
| 9 | **How is Change performed?** | The XOR delta list is the change; a region is stored as a delta **only if** `model_delta_bits(k) < region_size*8`, else verbatim. | DEFINED |
| 10 | **How is Paste performed?** | `apply_delta`: copy the base, then `out[pos] ^= xor_val` for each change. | DEFINED |
| 11 | **How does representation affect *execution*?** | **Undefined.** Today it affects *storage size only*; the data is decoded before any execution. Whether a base+delta form can reduce execution work is unproven. | **OPEN DESIGN QUESTION (top priority)** |
| 12 | **How is the management decision made?** | Per‑region cost comparison (delta vs verbatim) and per‑file region‑size sweep. There is no execution‑management decision yet. | DEFINED (storage) / OPEN (execution) |

Two clarifications‑mandated properties are already honoured by the code:

* **Bit‑level, not file‑size‑only** (clarifications §5): the cost accounting
  includes header, region index, change positions, changed values and per‑delta
  counts — nothing omitted to flatter the number.
* **Representation efficiency ≠ execution efficiency** (clarifications §6): both
  are measured separately — see §4.

---

## 4. Proof of Reality — what has actually been measured

From `FINDINGS.md` (every number verified by SHA‑256; 14/14 datasets round‑trip):

* **Single model checkpoint:** no real saving — under 0.1% on three of four real
  files; the fourth (GPT‑2, 5.99%) is deduplication of a constant attention‑mask
  buffer, **not** learned weights.
* **Two near‑identical checkpoints:** up to **49.99%** (against a 50% ceiling),
  but only while divergence stays in the low single‑digit percent of *bytes*.
  Ordinary fine‑tuning moves nearly all bytes and destroys the saving.
* **Representation vs execution:** the one advantage that survived is **speed at
  equal ratio** — CCP+gzip lands within 0.34 points of large‑dictionary LZMA
  while encoding ~12× and decoding ~4× faster. On ratio alone, LZMA‑512MB beats
  CCP on every row.
* **The negative control behaves:** incompressible data yields −0.00% (the
  container costs slightly more than the input), as it must.

The honest reading: **the storage representation is real, lossless, and useful in
one narrow band — collections of near‑duplicate binary checkpoints. It is not a
general compressor and provides, so far, no evidence about execution management.**

This is the "Proof of Reality" the Phase‑1 brief demands: original vs CCP
representation, original vs CCP execution cost, memory, data movement, bit‑level
representation, and correctness — all present in `reports/`.

---

## 5. Demo vs Algorithm Implementation — where the line is drawn

The brief forbids `run normal algorithm → compute expected CCP result → print
simulated result`. The existing experiment obeys this:

* **Real:** `encode()`/`decode()` write and read real container bytes; the
  reported size is `len(container)` on disk; the encoder asserts its own
  accounting matches the filesystem.
* **Oracle (labelled, kept out of the algorithm):** `model_delta_bits()` and the
  `SAVING/MARGINAL/NO SAVING` verdict logic — used to *check* the encoder, and
  flagged when the two disagree.

**Rule going forward:** any future component that computes an "expected" result
for comparison must live in `tests/` or be named `*_oracle`, never on the Core
code path.

---

## 6. Architecture — the seven layers, kept explicitly separate

The brief requires an architecture that separates Research / Algorithm / Core
Engine / Runtime / Integration / Product / UI. Here it is, with **what exists
today** marked, so no empty layer is mistaken for a working one.

```
┌─────────────────────────────────────────────────────────────────┐
│ 7. PRODUCT UI            professional desktop app, hides CCP      │  ✗ not started
├─────────────────────────────────────────────────────────────────┤
│ 6. COMMERCIAL PRODUCT    ZIP import, project history, packaging   │  ✗ not started
│    (CCP Forge)           reproducible builds, diagnostics         │
├─────────────────────────────────────────────────────────────────┤
│ 5. INTEGRATION           Build Orchestrator + Local Build Bridge  │  ✗ not started
│                          Linux agent ⇄ Windows/macOS build agent  │     (design DEFINED by user, §12 of brief)
├─────────────────────────────────────────────────────────────────┤
│ 4. RUNTIME               applies a chosen CCP strategy under a     │  ✗ not started
│                          semantic contract + human constraints    │
├─────────────────────────────────────────────────────────────────┤
│ 3. CORE ENGINE           the real CCP algorithm: base+delta encode│  ◑ storage form exists
│                          /decode, cost model, verify              │     (`ccp_full_experiment.py`)
├─────────────────────────────────────────────────────────────────┤
│ 2. ALGORITHM (spec)      formal Copy/Change/Paste definition,     │  ◑ storage defined;
│                          unit of operation, semantic contract     │     execution OPEN
├─────────────────────────────────────────────────────────────────┤
│ 1. RESEARCH / PoC        falsifiable experiments + FINDINGS       │  ✓ done, honest, measured
└─────────────────────────────────────────────────────────────────┘
```

Key architectural facts, tagged:

* **Agent vs Target environments are separate** (DEFINED BY USER, brief §11–13).
  Claude/Linux is the development environment; Windows is the target build
  environment; they communicate through a Build Bridge. CCP Forge must not depend
  on Claude Code. This belongs to layers 5–6 and is **not** a Core concern.
* **The Core must not know about ZIP, UI, or OS targets.** Layers 1–3 operate on
  bytes and semantics only. This keeps the honesty of the measurement intact.
* **Unit of Operation is an open axis** (clarifications §10): the algorithm could
  operate at instruction / function / basic‑block / trace / data‑structure /
  object / module / whole‑program granularity. Today it operates at
  **fixed‑size byte region** granularity only. Which level yields the largest
  advantage is unmeasured. Marked OPEN.

---

## 7. The 16 Stop‑Condition questions, answered

1. **Principle of CCP?** Keep one shared base, store only deltas, reuse that
   representation — losslessly or within a stated contract.
2. **The actual algorithm as defined so far?** The storage chain in §3 (regions →
   cluster → base → XOR delta → cost‑gated container → verify). Execution
   management is *not* yet an algorithm.
3. **Input?** A byte stream (file/mmap). Unit of operation is an open axis.
4. **Output?** A verified‑lossless CCP container, plus measured size/speed/memory.
5. **Internal representation?** `header │ index │ bases │ deltas │ verbatim`,
   deltas as `(position, xor)` pairs.
6. **Copy/Change/Paste at the bit level?** Copy = share one base region; Change =
   XOR delta list of changed byte positions; Paste = `base[i] ^ delta[i]`.
7. **How representation relates to execution management?** **It does not yet.**
   This is the top open question (§3.11, §8).
8. **CCP vs demo that simulates CCP?** Real encode/decode with on‑disk size and
   SHA‑256 verify vs. a labelled analytic oracle used only to cross‑check (§5).
9. **The Core that actually runs the algorithm?** `ccp_full_experiment.py`'s
   `encode`/`decode`/`RegionView`/`delta_entries`/`apply_delta`.
10. **Role of Python / the "brain" language?** Host and reference language for the
    Core and PoC; stdlib‑only so the algorithm stays dependency‑free and testable.
11. **Role of CCP in managing that language?** **OPEN.** No mechanism exists by
    which CCP manages the execution of Python (or any) programs yet.
12. **Role of the Meta‑Management layer?** DEFINED BY USER as self‑management
    under constraints (brief §4); **not built**, and blocked on Q7/Q11.
13. **Foundational rules, boundaries, permissions?** DEFINED BY USER: the human
    sets rules, constraints, permissions, goals and a flexibility space; CCP acts
    only inside it (brief §5). Not yet encoded anywhere.
14. **What is success?** Not "identical output" by default. Success = satisfy the
    stated constraints and semantic contract + achieve the desired result +
    improve data/bit representation + improve execution efficiency, weighted per
    system (brief §7, clarifications §7).
15. **How is saving measured — data, bits, execution?** Representation: real
    container size, bit‑level accounting incl. metadata. Execution: encode/decode
    time, throughput, peak RSS — measured *separately* from representation
    (clarifications §5–6). Both already in `reports/`.
16. **What is defined vs still open?** See the register in §8.

---

## 8. Classification register (clarifications §11 — no invented parts)

**DEFINED BY USER** (from the two PDFs — the original idea):

* CCP = Copy/Change/Paste: shared base + deltas, reused later for execution
  management.
* Human defines rules/constraints/permissions/goals/flexibility; CCP self‑manages
  only inside them.
* Success is relative to a system's constraints and goals, not fixed to identical
  output.
* Contradictory constraints must be represented and surfaced, not optimised away.
* Agent (Linux) vs Target (Windows) separation; Build Bridge/Orchestrator.
* Product = wrapper (ZIP import, analysis, build, package, UI); not CCP itself.
* Bit‑level accounting, and representation‑efficiency ≠ execution‑efficiency, must
  both be measured.

**PROPOSED ENGINEERING DESIGN** (ours, chosen; replaceable; not the user's idea):

* Fixed‑size offset‑aligned regions as the current unit of operation.
* Banded‑sketch clustering and medoid base selection heuristics.
* XOR byte‑granular deltas against a single (non‑chained) base.
* Cost‑gated storage (delta only when it beats verbatim) + per‑file size sweep.
* stdlib‑only Core so the algorithm stays dependency‑free and falsifiable.

**OPEN DESIGN QUESTION** (undefined; must be decided, must not be invented):

1. **[TOP] Representation → execution management.** How, if at all, does a
   base+delta representation reduce *execution* work rather than only storage?
   Until this is answered, CCP is a storage method, and layers 4–7 have no
   foundation. *This is the single question that blocks meaningful implementation.*
2. **Semantic contract** (clarifications §7): what must be preserved when CCP
   changes execution — allowed/required behaviour, allowed/forbidden transforms.
3. **Unit of operation** (clarifications §10): which granularity (instruction …
   whole‑program) gives the largest real advantage.
4. **Understanding threshold** (clarifications §8): the measurable criterion for
   "enough understanding to manage a language," separating training from
   validation on unseen programs.
5. **Self‑management / meta‑layer** mechanism (brief §4) — blocked on #1.
6. **Right opponent for storage claims** (FINDINGS §8): `zstd --long`,
   content‑addressed storage, dedup filesystems — none measured yet.
7. **Content‑defined chunking** — fixed regions miss shifted duplicates; the
   measured savings are a lower bound of unknown looseness.

---

## 9. The one decision that gates implementation

The storage side of CCP is understood well enough to implement and measure — in
fact it already is. The product vision, however, rests entirely on **Open
Question #1: does the CCP representation manage or reduce *execution*, or only
storage?** The measured evidence to date speaks only to storage, and narrowly.

Per the clarifications' rule against inventing missing parts, I will **not**
fabricate an execution‑management mechanism and present it as your idea. Instead,
before significant implementation proceeds, one decision is needed from you:

* **(A) Storage track** — build the Core/Runtime/Product around the proven
  base+delta *storage* representation for near‑duplicate checkpoint collections
  (the one shape the data supports today), closing the `zstd --long` / CAS /
  content‑defined‑chunking gaps first; **or**
* **(B) Execution track** — treat "representation → execution management" as the
  primary research target and design a falsifiable experiment for it *before* any
  product layer, exactly as the PoC was built for storage; **or**
* **(C) Both, sequenced** — ship (A) as the first commercial surface while (B)
  runs as research, with a hard rule that (B) is never surfaced in the product
  until it is measured the way storage was.

This is the stop point of the understanding phase. The mechanism is understood
well enough to build and measure; the remaining gap is a decision, not more
analysis.
