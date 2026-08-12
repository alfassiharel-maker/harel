# VALUE AND LOGIC TRUTH TABLE

Status: **enumeration for decision — nothing here is approved**
Produced under: owner decisions O2.1 and O2.5 (2026-08-12); O2.3 explicitly deferred
Companion: `docs/O2_SEMANTIC_ANALYSIS.md` (which model, and why)

> **Per the instruction, this document does not finalise anything.** It
> enumerates every operator against every combination of `Known`, `Null` and
> `Unknown`, and shows what each candidate model answers. The `PROPOSED` column
> is a recommendation, marked **PROPOSED — REQUIRES OWNER APPROVAL**, and no
> implementation follows from it.
>
> `docs/SEMANTIC_DECISION_REGISTER.md`, `DESIGN_AUDIT.md`,
> `02_FORMAL_SEMANTICS.md`, `04_TYPE_AND_DATA_MODEL.md`,
> `03_EXECUTION_MODEL.md` and `SEMANTIC_IMPACT_MAP.md` have **not** been updated
> for O2.1 and O2.5 either — the instruction sequences those updates after the
> truth table is approved.

---

## 1. What is already settled

From the owner decisions, and not open here:

| | |
|---|---|
| **O2.1** | `Null` is a **first-class logical value/state** in the value model. It is not an integer, not a string, not an alias for `Unknown`, not a SQL-only concept, and **not** a per-type nullable modifier (`Int?`). |
| **O2.5** | A rule condition referencing a name known to be `Null` **is evaluable**. `value = Null` carries logical information. |
| **OD-2** | `Unknown` and `Null` are **never collapsed**. Predicate semantics must be the language's own, not inherited from SQL by default. |

The internal Rust representation is left to implementation, provided the three
states stay distinguishable.

## 2. The state space

Every operand, at evaluation time, is in exactly one of three conditions:

```
K   Known(v)     a concrete value of one of the four types
N   Null         known that there is no value
U   Unknown      not enough information to determine a value
```

For the logical operators the `K` state splits, because only booleans reach
them: `T` (`Known(true)`) and `F` (`Known(false)`).

## 3. Result classification

As instructed, every cell is classified as one of:

```
true      the predicate holds
false     the predicate does not hold
unknown   the result cannot be determined from the information available
error     a structured diagnostic; the program is wrong, or the run stops
```

**One gap in this classification, reported rather than resolved.** These four
suffice for predicates. They do not suffice for the *non-predicate* operators
(`+ - * / %` and unary `-`), whose result is a value, so a fifth outcome —
"the result is `Null`" — is needed there. §8 enumerates those operators with the
extended set and flags the mismatch as **OPEN DESIGN DECISION O2.13**.

A related question the classification raises: a cell answering `unknown` is not
the same as a cell answering `Null`. Under a model where `Null == Null` is
`unknown`, the result is *"cannot be determined"*, which is `Unknown` — so that
model turns a `Null` operand into an `Unknown` result. That is the collapse
OD-2 forbids, and it is why M1 below is flagged as non-compliant.

## 4. The three candidate models

Defined fully in `docs/O2_SEMANTIC_ANALYSIS.md`; summarised here so the tables
can be read on their own.

| | Model | One-line rule |
|---|---|---|
| **M1** | SQL-like three-valued logic | Any `Null` or `Unknown` operand makes a comparison `unknown`; `and`/`or` follow Kleene. |
| **M2** | Classical / null-as-value | `Null` is an ordinary, distinct value: equality is total, ordering is defined, booleans reject it. `Unknown` never reaches an operator. |
| **M3** | Language-specific hybrid | Equality is **total** over states (`Null` is information). Ordering and boolean use of `Null` are **errors** (a magnitude comparison against a known absence is a defect). `Unknown` propagates as `unknown`. |

`ordinary` in a cell means "the existing operator table decides", which for
mismatched types is already `error` (`E3012`).

## 5. Equality — `==` and `!=`

`==`, 9 cells:

| left | right | M1 | M2 | M3 | **PROPOSED** |
|---|---|---|---|---|---|
| K | K | ordinary | ordinary | ordinary | ordinary |
| K | N | unknown | false | false | **false** |
| K | U | unknown | unknown | unknown | **unknown** |
| N | K | unknown | false | false | **false** |
| N | N | unknown | true | true | **true** |
| N | U | unknown | unknown | unknown | **unknown** |
| U | K | unknown | unknown | unknown | **unknown** |
| U | N | unknown | unknown | unknown | **unknown** |
| U | U | unknown | unknown | unknown | **unknown** |

`!=`, 9 cells — the negation of `==` in every model, but stated rather than
implied:

| left | right | M1 | M2 | M3 | **PROPOSED** |
|---|---|---|---|---|---|
| K | K | ordinary | ordinary | ordinary | ordinary |
| K | N | unknown | true | true | **true** |
| K | U | unknown | unknown | unknown | **unknown** |
| N | K | unknown | true | true | **true** |
| N | N | unknown | false | false | **false** |
| N | U | unknown | unknown | unknown | **unknown** |
| U | K | unknown | unknown | unknown | **unknown** |
| U | N | unknown | unknown | unknown | **unknown** |
| U | U | unknown | unknown | unknown | **unknown** |

The decisive cell is `N == N`. M1 answers `unknown`, which says the system
cannot tell whether two known absences match — but under OD-2 it *can*: both
sides are known, and what is known is that neither has a value. M3 and M2 answer
`true` for that reason.

The second decisive cell is `U == U`. All three models answer `unknown`, and
they are right for the same reason M1 is wrong above: two unknowns may be
anything, including different things.

## 6. Ordering — `<`, `<=`, `>`, `>=`

All four behave identically within each model, so one table covers them. 9 cells
each, 36 in total.

| left | right | M1 | M2 | M3 | **PROPOSED** |
|---|---|---|---|---|---|
| K | K | ordinary | ordinary | ordinary | ordinary |
| K | N | unknown | false¹ | error | **error** |
| K | U | unknown | unknown | unknown | **unknown** |
| N | K | unknown | true¹ | error | **error** |
| N | N | unknown | false² | error | **error** |
| N | U | unknown | unknown | unknown | **unknown** |
| U | K | unknown | unknown | unknown | **unknown** |
| U | N | unknown | unknown | unknown | **unknown** |
| U | U | unknown | unknown | unknown | **unknown** |

¹ M2 treats `Null` as a bottom element that sorts before every value, so
`Null < 5` is `true` and `5 < Null` is `false`.
² With `Null` as a bottom element, `Null < Null` is `false` and `Null <= Null`
is `true`. The table shows `<`; `<=` differs in exactly this cell.

The argument for `error` in M3: `<` asks which of two magnitudes is larger.
A known absence has no magnitude, so the question is malformed. Answering
`unknown` (M1) hides the defect behind a rule that quietly never fires;
answering `false` (M2) invents an ordering the author never stated. An error
names the mistake at the point it is made — which is the same reasoning the
language already applies to `1 + 1.0`.

The cost of `error` is real and is stated in the analysis document: a program
that legitimately wants "treat absent as smallest" must say so, and the language
gives it no way to yet.

## 7. Logical operators — `and`, `or`, `not`

Operands here are `T`, `F`, `N`, `U`. A `Known` non-boolean is already a static
type error (`E3011`/`E3012`) and does not reach evaluation.

### 7.1 `and` — 16 cells

| left | right | M1 | M2 | M3 | **PROPOSED** |
|---|---|---|---|---|---|
| T | T | true | true | true | **true** |
| T | F | false | false | false | **false** |
| T | N | unknown | error | error | **error** |
| T | U | unknown | unknown | unknown | **unknown** |
| F | T | false | false | false | **false** |
| F | F | false | false | false | **false** |
| F | N | **false** | error | error | **error** |
| F | U | **false** | unknown | unknown | **unknown** |
| N | T | unknown | error | error | **error** |
| N | F | **false** | error | error | **error** |
| N | N | unknown | error | error | **error** |
| N | U | unknown | error | error | **error** |
| U | T | unknown | unknown | unknown | **unknown** |
| U | F | **false** | unknown | unknown | **unknown** |
| U | N | unknown | error | error | **error** |
| U | U | unknown | unknown | unknown | **unknown** |

The cells in bold under M1 are Kleene's absorbing rule: `false and anything` is
`false`, even when the other operand is unavailable. That rule is the reason M1
lets more rules fire earlier — and also the reason a rule's behaviour can depend
on which conjunct the author wrote first, which the current engine deliberately
avoids. See §9.2.

### 7.2 `or` — 16 cells

| left | right | M1 | M2 | M3 | **PROPOSED** |
|---|---|---|---|---|---|
| T | T | true | true | true | **true** |
| T | F | true | true | true | **true** |
| T | N | **true** | error | error | **error** |
| T | U | **true** | unknown | unknown | **unknown** |
| F | T | true | true | true | **true** |
| F | F | false | false | false | **false** |
| F | N | unknown | error | error | **error** |
| F | U | unknown | unknown | unknown | **unknown** |
| N | T | **true** | error | error | **error** |
| N | F | unknown | error | error | **error** |
| N | N | unknown | error | error | **error** |
| N | U | unknown | error | error | **error** |
| U | T | **true** | unknown | unknown | **unknown** |
| U | F | unknown | unknown | unknown | **unknown** |
| U | N | unknown | error | error | **error** |
| U | U | unknown | unknown | unknown | **unknown** |

### 7.3 `not` — 4 cells

| operand | M1 | M2 | M3 | **PROPOSED** |
|---|---|---|---|---|
| T | false | false | false | **false** |
| F | true | true | true | **true** |
| N | unknown | error | error | **error** |
| U | unknown | unknown | unknown | **unknown** |

## 8. Non-predicate operators — `+ - * / %`, unary `-`

Not in the required list, but they meet the same operands, so leaving them out
would leave the semantics incomplete. Their results are values, so the four-way
classification does not fit: a fifth outcome, **`null`** (the result *is* a known
absence), is needed.

> **OPEN DESIGN DECISION O2.13.** The classification set given for this document
> is `true / false / unknown / error`. Arithmetic needs `value / null / unknown /
> error`. This is reported, not resolved.

9 cells per binary operator; all five behave identically within each model.

| left | right | M1 | M2 | M3 | **PROPOSED** |
|---|---|---|---|---|---|
| K | K | ordinary | ordinary | ordinary | ordinary |
| K | N | null | error | error | **error** |
| K | U | unknown | unknown | unknown | **unknown** |
| N | K | null | error | error | **error** |
| N | N | null | error | error | **error** |
| N | U | unknown | unknown | unknown | **unknown** |
| U | K | unknown | unknown | unknown | **unknown** |
| U | N | unknown | error | error | **error** |
| U | U | unknown | unknown | unknown | **unknown** |

Unary `-`: `K` → ordinary; `N` → `null` (M1) / **error** (M2, M3, PROPOSED);
`U` → unknown in all.

M1's `null` propagation is SQL's rule and is internally consistent. The argument
against it here: the language already refuses to compute `1 + 1.0` rather than
guessing, and `1 + Null` is the same kind of question — an arithmetic operation
on something that is known not to be a number.

## 9. Cell count, and what the tables leave open

90 predicate cells (9 + 9 + 36 + 16 + 16 + 4) plus 46 arithmetic cells, each
answered by three models: 408 classifications.

Four questions the enumeration surfaced. **None is resolved here.**

### 9.1 O2.11 — what does a rule do when its condition evaluates to `unknown`?

Today a rule is either not evaluable (retried) or evaluates to `true`/`false`.
Under every model above, an evaluable rule's condition can now be a third thing:
`unknown`. Does that rule (a) not fire and never retry, (b) not fire but retry
next round, (c) raise, or (d) something else? Option (b) risks non-termination
if the `unknown` is permanent; option (a) makes `unknown` behave like `false` at
the activation boundary, which is the collapse OD-2 forbids, one level up.

**This is the most consequential unanswered question in this document.**

### 9.2 O2.4 revisited — Kleene absorption and operand order

M1's `false and U → false` lets a rule fire without knowing one of its operands.
That is more permissive and is standard. It also means the *evaluability gate*
(`knows_all(reads)`) and the operator table would disagree: the gate refuses to
evaluate a rule with an unknown read at all, so those cells are unreachable
today. Adopting M1's absorption is therefore also a decision to relax the gate —
two changes, not one.

### 9.3 O2.12 — does `Null` type-check?

O2.1 says `Null` is not a per-type modifier, so a name whose inferred type is
`Int` presumably may still hold `Null`. Then: does a rule deriving `Null` for a
name another rule derives as `Int` trigger `E3010 ConflictingNameType`? And does
`Null == "text"` type-check, given that cross-type equality is otherwise a
static error? The tables above assume `Null` is *type-compatible with every
comparison*, which is an assumption, not a decision.

### 9.4 O2.6 — can a program write `Null`?

Every table assumes `Null` can arise. Whether it arises only from data (Phase 6)
or can be written in source — `then status = null` — is unanswered. `null` is
already a reserved word, so either answer is available.

## 10. If the PROPOSED column is approved

The shape of the resulting language, in one paragraph, so the proposal can be
judged by what it feels like rather than by its cells:

*Equality treats `Null` as ordinary information: you may ask whether a value is
absent, and get a straight yes or no. Everything else refuses: you may not order
an absence, add to it, or use it as a truth value, and each attempt is a
diagnostic pointing at the operator. `Unknown` never produces a value — it
either keeps a rule waiting or, where it does reach an operator, produces
`unknown`. The two are never confusable, because one answers questions and the
other defers them.*

The rationale, the alternatives, and the costs are in
`docs/O2_SEMANTIC_ANALYSIS.md`.

**PROPOSED — REQUIRES OWNER APPROVAL.**
