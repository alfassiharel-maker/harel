# CCP execution — what the minimal experiment measured

This is the Track‑B (Execution) result. It tests one hypothesis and stops.
Companion to `FINDINGS.md`, which covers storage. Every number here comes from a
run in `reports/ccp_execution_*.txt`, is an exact operation count or a
best‑of‑*R* wall‑clock time on real execution, and every CCP result was checked
against full recomputation for bit‑exact equality. The tests round‑trip 68/68.

## The one question

> The storage experiment proved a base+delta representation can *store*
> near‑duplicate data in less space. Can that same representation make *executing*
> a computation over those inputs cheaper — compute the base once, then update by
> the delta — with a result identical to full recomputation?

Stated as the four things Track B required be kept apart:

| | |
| --- | --- |
| **Proven already** | base+delta *storage* representation (`FINDINGS.md`). |
| **Hypothesised** | the representation can also drive cheaper *execution*. |
| **What the experiment tests** | `y_i = y0 + A·Δ_i` vs `y_i = A·x_i`, on one equal substrate, over a sparsity sweep. |
| **Success** | bit‑exact **and** fewer multiply‑adds **and** less wall‑clock, over a non‑trivial band. |
| **Failure** | not exact, or not fewer ops, or the op‑saving does not survive into time, or the band is too narrow to matter. |

## The answer, in one paragraph

**Supported, with a sharp scope limit.** For a fixed linear operator `A`, reusing
one base result `y0 = A·x0` and applying `A·Δ` for each sparse input delta
produces a **bit‑identical** result while doing far less work: at 0.8 % of the
input changed it did **25.6× fewer multiply‑adds and ran 23.5× faster**; the
saving decays smoothly to break‑even as the delta fills in. Two things bound the
claim hard. First, the wall‑clock win is always **smaller than the operation win**
and the gap widens with density — at 50 % divergence the operation count still
favours CCP 1.94× but wall‑clock is essentially parity (1.09×). Second, the exact
identity holds **only because `A` is linear**; the nonlinear control breaks
correctness outright. So the mechanism is real and sound for the linear part of a
computation, and undemonstrated for anything else.

---

## 1. The sparsity sweep

`reports/ccp_execution_128.txt` — operator `A` is 128×128 int8‑range, one base
reused by 32 derived inputs, pure‑Python scalar multiply‑add substrate for both
methods, timing best‑of‑7. `ops x` and `time x` are CCP speedups (full ÷ ccp);
above 1 means CCP did less.

| input changed (k/n) | ops full | ops ccp | ops speedup | time speedup | bit‑exact | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 0.8 % (k=1) | 524,288 | 20,480 | **25.60×** | 23.5× | yes | SUPPORTED |
| 2.3 % (k=3) | 524,288 | 28,672 | 18.29× | 15.5× | yes | SUPPORTED |
| 4.7 % (k=6) | 524,288 | 40,960 | 12.80× | 10.3× | yes | SUPPORTED |
| 10.2 % (k=13) | 524,288 | 69,632 | 7.53× | 5.5× | yes | SUPPORTED |
| 20.3 % (k=26) | 524,288 | 122,880 | 4.27× | 3.1× | yes | SUPPORTED |
| 50.0 % (k=64) | 524,288 | 278,528 | 1.88× | 1.27× | yes | SUPPORTED |

The operation count is not measured noise — it is exact and equal to the analytic
model `m·n + N·k·m`, cross‑checked on every trial (the run asserts the counter
and the model agree, the same discipline the storage encoder uses against its
container size). The speedup is therefore `1 / (1/N + k/n)`: it is driven by the
**sparsity of the change** and the **reuse count**, and nothing else.

### It holds at size, and low‑sparsity gets better

`reports/ccp_execution_256.txt` — 256×256, reused 64×:

| input changed | ops speedup | time speedup |
| --- | --- | --- |
| 0.4 % (k=1) | **51.20×** | 43.9× |
| 10.2 % (k=26) | 8.53× | 5.1× |
| 50.0 % (k=128) | 1.94× | **1.09×** |

Doubling the operator and the reuse count doubled the low‑sparsity win (25.6×→51×
ops). The 50 % row is the important one: operations still favour CCP 1.94×, but
wall‑clock has almost completely closed to 1.09×. That divergence is the finding
in §3.

---

## 2. Copy / Change / Paste, at the execution level

The mechanism is deliberately the smallest thing that consumes the CCP
representation, not an "execution engine":

* **Copy** — `y0 = A·x0` is computed once and reused by every derived input.
  This is the whole source of the saving; the no‑reuse control below removes it
  and the saving vanishes.
* **Change** — each input arrives as a sparse **arithmetic** delta `Δ_i`
  (`position → value change`). Note this is *not* the XOR byte delta storage
  uses: storage deltas are magnitude‑blind because storage only cares about bit
  patterns, execution needs magnitude. The Copy‑Change‑Paste *principle*
  transfers to execution; the concrete byte encoding does not. That is a concrete
  answer to the Phase‑1 question of whether the representation can move from data
  to execution — it can, but it must be re‑encoded to carry value, not bits.
* **Paste** — `y_i = y0 + A·Δ_i`, touching only the columns of `A` named by the
  delta. `k·m` multiply‑adds instead of `m·n`.

Correctness is exact by integer distributivity: `A·(x0+Δ) == A·x0 + A·Δ` with no
rounding, so "identical to full recomputation" is a bit‑for‑bit check, not an
epsilon argument. Real quantized inference is int8, so the integer regime is not
a toy.

---

## 3. The load‑bearing caveat: fewer operations ≠ less time

This is the execution‑level restatement of the storage study's
"representation efficiency ≠ execution efficiency", and it showed up as measured
data rather than as a warning. The wall‑clock speedup trails the operation
speedup at **every** point, and the gap grows as the delta fills in:

| input changed | ops speedup | time speedup | fraction of the op‑win realised |
| --- | --- | --- | --- |
| 0.8 % | 25.60× | 23.5× | 92 % |
| 10.2 % | 7.53× | 5.5× | 73 % |
| 50.0 % | 1.88× | 1.27× | 68 % |

The lost fraction is per‑input overhead: copying `y0`, iterating the delta,
gathering scattered columns of `A`. At high sparsity the update is tiny and that
overhead is most of it. This is why the useful band is **low** sparsity, not
merely "sparse".

**And the substrate caveat is bigger than the overhead one.** Both methods ran on
the *same* pure‑Python scalar loop, on purpose, so the comparison isolates the
algorithmic work difference. That is exactly why the time win is *not* a
deployment claim: a production dense matmul runs on BLAS/GPU kernels that execute
the full `m·n` product far faster per element than an irregular
gather‑scatter incremental update ever could. The honest reading is that the
operation reduction is real and hardware‑independent, and whether it beats an
optimized dense kernel in real seconds is **unmeasured** — the same gap the
storage findings flagged when the right opponent (`zstd --long`) was missing from
the table. Measuring against a real BLAS baseline is the first thing Track B would
need next, and it is not done here.

---

## 4. The controls — where it must fail, and does

| control | what it removes | ops | time | correct | verdict |
| --- | --- | --- | --- | --- | --- |
| dense delta (k=n) | all sparsity | 0.97× | 0.68× | yes | NO SAVING |
| no reuse (N=1) | base amortisation | 0.99× | ~1.0× | yes | NO SAVING |
| nonlinearity (ReLU) | linearity of `A` | — | — | **no** | IDENTITY BROKEN |

* **Dense delta**: when every position changes, the incremental update costs more
  than a fresh matvec (it also pays for the base). CCP loses, as it must — a
  method that appeared to win here would be reporting a bug. Mirrors the storage
  result that dense byte updates destroy the saving.
* **No reuse**: a single input cannot amortise the one base matvec, so
  `m·n + k·m > m·n`. CCP loses. Mirrors the storage result that a single
  checkpoint yields nothing — the win is in the *reuse*, not the representation
  alone.
* **Nonlinearity**: the sharpest result. Put a ReLU‑style clamp after the linear
  map and reuse the *activated* base output, and `y0_activated + A·Δ` no longer
  equals `clamp(A·x_i)` — the exact contract breaks and the run reports it as a
  correctness failure, not a smaller number. **The exact base+delta identity is a
  property of linear operators only.** In a neural network the matmuls are linear
  (and most of the FLOPs), but activations, normalisation and attention softmax
  are not, and this mechanism does not touch them. Incremental execution through a
  nonlinearity would need either recomputation or an approximation with its own
  error budget — neither attempted, and neither claimed.

---

## 5. What survives, and what Track B would need next

**Survives, measured:** a base+delta representation can execute a linear operator
over reused, sparsely‑varying inputs with **fewer operations and, on equal
substrate, less time**, bit‑exactly. The Phase‑1 hypothesis — that the CCP
representation can move from efficient *data representation* to a lever on
*execution* — is **supported for the linear case** and not refuted. That is a
real, if narrow, positive result, and it is the first evidence in this repository
that CCP is more than a storage scheme.

**Does not survive as a deployment claim, and must not be presented as one:**

1. **The substrate is pure Python.** The operation reduction is hardware‑
   independent; the time reduction is not a win against an optimized dense kernel,
   which was not measured. This is the single biggest open item.
2. **Exactness is linear‑only.** Real pipelines are not. The scope of the proven
   contract is one matmul, not a model.
3. **The useful band is low sparsity.** Ordinary fine‑tuning moves nearly every
   value — the same shape that killed storage. The favourable workload is sparse,
   structured input change (adapter‑style edits, partial updates), not dense drift.
4. **One operator shape, one dtype.** 128²/256² int8. Nothing here speaks to float
   error accumulation, to larger operators, or to a chain of stages.

Per the Track‑B stop condition: there is now a minimal, measurable, reproducible
execution experiment that answers the question — **the hypothesis is supported for
the linear case, on equal substrate, and unproven beyond it.** No product,
runtime, or abstraction layer was built on top of it, and none should be until
item 1 (a real dense‑kernel baseline) is closed and the scope in items 2–4 is
either widened by measurement or accepted as the deployment envelope.
