# O2 SEMANTIC ANALYSIS — three models for `Known`, `Null` and `Unknown`

Status: **recommendation APPROVED by the owner on 2026-08-12 and implemented**
Companion: `docs/VALUE_AND_LOGIC_TRUTH_TABLE.md` (the cells)
Approved and implemented; kept as the record of why this model and not the other two.

---

## 1. The question

Owner decisions O2.1 and O2.5 established that `Null` is a first-class logical
state, distinct from `Unknown`, and that a rule reading a `Null` is evaluable.
They deliberately left the operator semantics open (O2.3), with one constraint
that rules out the easy answer:

> *"A comparison or predicate involving `Null` must be defined by the language's
> own operator semantics and must not automatically inherit SQL three-valued
> semantics. Unknown and Null must never be collapsed."*

So the question is not "what does SQL do", and not "what is easiest to
implement". It is: **what does this language mean when it says a value is
absent?**

## 2. Criteria

The five criteria used to judge the models, drawn from decisions and principles
that are already approved or already in the specification — not invented for
this comparison:

| # | Criterion | Source |
|---|---|---|
| **C1** | `Unknown` and `Null` are never collapsed, at the level a *program* can observe | OD-2, explicit |
| **C2** | No invented answers: where the engine cannot know, it says so or refuses; it never substitutes a plausible value | Master Spec §8, §54; `01_LANGUAGE_CONSTITUTION` §1.3 |
| **C3** | Explainability: every result is reconstructible from the trace | Master Spec §9, §10 |
| **C4** | Determinism of results | Master Spec §22 |
| **C5** | Forward compatibility: a future decision (SQL, state, relational facts) should not have to *change the meaning of existing programs* to be added | OD-3's own wording, applied generally |

## 3. The models

### M1 — SQL-like three-valued logic

*Any comparison with `Null` or `Unknown` yields `unknown`; `and`/`or` follow
Kleene's tables with absorption (`false and unknown = false`); arithmetic
propagates `Null`.*

**In its favour.** It is the most widely understood model in the world — every
database implements it, and every enterprise data engineer has it in their
fingers. It is total: no operator ever fails, so no program is rejected for
touching an absent value. It maps to a future SQL layer with zero impedance,
which matters for a language whose specification calls SQL a *first-class data
interface* (Master Spec §12).

**Against it — and this is decisive.** It **fails C1**. In M1, `Null` and
`Unknown` produce the identical result in every comparison cell: `unknown`.
Two states that are formally distinct but observationally identical *are*
collapsed, from the only vantage point that matters — a program's. OD-2 does not
merely say the two must be represented separately; it says the evaluator must
not silently collapse them, and M1 collapses them at the first operator.

It also strains C2 in a subtler way: `Null == Null → unknown` asserts that the
system cannot tell whether two known absences correspond, when by construction it
can. That is not caution; it is discarding information the engine holds.

**Verdict: non-compliant with OD-2 as written.** It is included because it is
the default everyone reaches for, and because rejecting it explicitly is more
useful than not considering it.

### M2 — classical, `Null` as an ordinary value

*`Null` is one more value, distinct from every other. Equality is total
(`Null == Null` is `true`, `Null == 5` is `false`). Ordering places `Null` as a
bottom element, below every value. Boolean operators reject it as a type error.
`Unknown` is not a value and never reaches an operator, because the evaluability
gate stops the rule first.*

**In its favour.** It is the simplest coherent model: every operator stays
total in the domain where it is defined, equality behaves the way a programmer
expects of a value, and `Unknown` keeps its distinct role of "wait". It satisfies
C1 cleanly — `Null` answers, `Unknown` defers, and no program can confuse them.
Sorting works, which matters the moment there are collections or query results
to order.

**Against it.** The bottom-element ordering is an invention. `Null < 5` being
`true` states that an absent value is *smaller than* five, which nothing in the
language justifies — it is a convenience borrowed from sort implementations, and
under C2 it is exactly the kind of plausible-looking answer the language is
supposed to refuse. It is also a forward-compatibility hazard (C5): if a later
decision wants "absent sorts last" or "absent is not orderable", existing
programs silently change meaning.

### M3 — the hybrid, and the recommendation

*Equality is total over states, exactly as in M2: `Null == Null` is `true`,
`Null == Known` is `false`. Everything else involving `Null` is an **error**:
ordering, arithmetic, and boolean use. `Unknown` propagates as `unknown` where
it is reachable, and otherwise keeps the rule waiting.*

The organising idea: **`Null` is information about existence, not about
magnitude, quantity or truth.** So the language answers existence questions about
it, and refuses every other kind.

- *Existence questions* — `==`, `!=` — are exactly the questions "is this value
  absent?" and "do these two agree about absence?". Both are answerable from what
  is known. They answer.
- *Magnitude questions* — `<`, `<=`, `>`, `>=` — presuppose a quantity. An
  absence has none, so the question is malformed, not merely unanswerable.
- *Arithmetic* — presupposes an operand. Same.
- *Truth* — a condition must be `true` or `false`; `Null` is neither, and
  treating it as either would be the collapse OD-2 forbids.

**In its favour.** It is the only model that satisfies all five criteria. C1: the
three states are pairwise distinguishable by a program — `Null` answers equality,
`Unknown` yields `unknown`, and neither is `false`. C2: nothing is invented; the
engine answers what it knows and names what it cannot. C3: an error carries a
code, a span and a cause, so an absent-value mistake is explained rather than
buried in a rule that quietly never fires. C5: it is the *most restrictive*
coherent model, and restriction is what is forward-compatible — every one of
these errors can later be relaxed into a defined answer without changing the
meaning of any program that runs today, whereas M1 and M2 would have to *change*
an answer.

That last point is the same argument that made `error` the right default for
rule conflicts under OD-3, and it is the strongest reason to prefer M3.

**Against it — stated plainly, because the cost is real.**

1. **It rejects programs the other models accept.** `when score > threshold`
   where `threshold` turns out `Null` becomes an error rather than a rule that
   does not fire. For a data-heavy rule base where absence is routine, that could
   be noisy, and the language currently offers no way to say "treat absent as
   not-greater" — because it has no `is_null`, no coalesce, no guard.
2. **It will collide with SQL.** A future data layer producing `NULL`-heavy rows
   will meet a language that errors on `NULL < 100`. Either the data layer
   converts, or the language gains guards, or Phase 6 revisits this decision.
3. **Errors are fatal today.** With execution stopping at the first error
   (`O3.1` is open), one absent value in one rule ends the run. If M3 is
   approved, whether these are fatal errors or recoverable conditions needs an
   answer with it.

**Mitigation, and a dependency worth naming.** M3 is most defensible if the
language *also* gains an explicit way to ask about absence — an `is_null`
predicate, or a guard form. That is register entry L and O2.6, both open. My
recommendation is M3 **on the assumption that such a form follows**; approving
M3 without ever adding one would leave authors unable to write the very programs
M3's errors are telling them to write.

## 4. Comparison against the criteria

| | M1 SQL-like | M2 classical | M3 hybrid |
|---|:--:|:--:|:--:|
| **C1** never collapse `Null`/`Unknown` | ❌ identical results everywhere | ✅ | ✅ |
| **C2** no invented answers | ⚠️ discards known information at `N == N` | ⚠️ invents an ordering | ✅ |
| **C3** explainability | ⚠️ silent non-firing | ✅ | ✅ errors name the site |
| **C4** determinism | ✅ | ✅ | ✅ |
| **C5** forward compatible | ❌ relaxing later changes answers | ❌ ordering is hard to retract | ✅ errors relax into answers |
| Familiarity | ✅ highest | ✅ | ⚠️ new |
| Rejects valid-looking programs | never | never | **yes — the main cost** |

## 5. Recommendation

**M3 — the language-specific hybrid.**

`Null` answers existence questions and refuses everything else. `Unknown` defers.
Neither is ever `false`.

**APPROVED 2026-08-12.** The three things that had to be decided with it were:

1. **O2.11** — answered by the owner: a condition evaluating to `unknown` makes
   the rule **pending**. Implemented, and visible in the trace.
2. **O3.1** — decided under delegated authority: a semantic error in a rule ends
   the execution, with a structured diagnostic and the partial trace. Continuing
   would silently drop that rule's contribution, making the result
   indistinguishable from one where the rule legitimately did not fire — which
   is the kind of silence the whole model exists to prevent. Static errors are
   still all reported together before anything runs.
3. **O2.6 / register L** — decided: `x is null`, `x is unknown` and `x is known`
   are total predicates, and `null` is a literal. Without them M3's errors would
   be unanswerable by an author; with them, the strictness is navigable.

## 6. What I did not do

- I did not choose M1 because it is standard, nor M2 because it is simplest.
- I did not resolve O2.3's cells as fact — the truth table's `PROPOSED` column
  is a column, not a ruling.
- I did not resolve O2.4, O2.6, O2.9, O2.11, O2.12, O2.13, O3.1, or any other
  secondary question this analysis surfaced. They are named so they can be
  decided, not folded into the recommendation.
- I did not touch `SEMANTIC_DECISION_REGISTER.md`, `DESIGN_AUDIT.md`,
  `02_FORMAL_SEMANTICS.md`, `03_EXECUTION_MODEL.md`,
  `04_TYPE_AND_DATA_MODEL.md` or `SEMANTIC_IMPACT_MAP.md` — the instruction
  sequences those updates after approval, and O2.1 and O2.5 are therefore still
  recorded as open in the register. That is a known, temporary inconsistency,
  noted here so it is not mistaken for an oversight.
- I did not change any `.rs` file, fixture, test or runtime behaviour.
