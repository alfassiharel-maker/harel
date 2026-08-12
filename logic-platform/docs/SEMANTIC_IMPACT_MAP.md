# SEMANTIC IMPACT MAP — owner decisions 1–3

Status: **implemented** — the O2 decisions were applied on 2026-08-12; this
document is kept as the record of what they touched, with each row marked
Input: the three owner decisions of 2026-08-12 (fact mutability, `Unknown`/`Null`/`Known`, conflict policy)
Output: what each decision requires of each subsystem, and what it cannot require yet

---

## 0a. What has since been implemented

| Requirement | State |
|---|---|
| Three-state `Value` (`Unknown` / `Null` / `Known`) | **DONE** — `lml_types::Value` |
| `null` literal, `is null` / `is unknown` / `is known` | **DONE** — lexer, parser, AST, IR, evaluator |
| Operator semantics of the approved truth table | **DONE** — `crates/logic/src/eval.rs`, tested cell by cell |
| `E5004` / `E5005` / `E5006` for `Null` misuse | **DONE** |
| Pending rules (O2.11) | **DONE** — `crates/reasoning`, `RulePending` in the trace |
| Tagged JSON: no state encoded as bare `null` | **DONE** — `crates/trace/src/json.rs` |
| Conflict report with OD-3's seven elements | **DONE** — `ConflictReport`, `ConflictRaised` |
| `ConflictStrategy` seam with `RaiseConflict` default | **DONE** — `crates/reasoning/src/conflict.rs` |
| Execution id + timing, excluded from trace equality | **DONE** — `TraceMetadata` (resolves C1) |
| `Observation` / `State` as `Origin` categories | **NOT DONE** — deliberately: O1.1 and O1.2 are unspecified, and a variant nothing can produce is a placeholder |
| SQL `NULL` mapping (O2.8) | **OPEN** — blocks Phase 6, not this work |

## 0. Summary

| Decision | Relationship to the current implementation |
|---|---|
| **1 — Facts are immutable** | **Confirms** what is implemented. Adds a *new* requirement — the four-way distinction `Fact` / `Observation` / `Derived Fact` / `State` — of which the engine implements two. |
| **2 — `Unknown` ≠ `Null` ≠ `Known(Value)`** | **Contradicts** the current implementation. `Null` does not exist: `docs/04` §2 removed it deliberately and said `Unknown` did its job. Every layer that handles a value is affected. |
| **3 — No implicit conflict resolution; explicit `Conflict` condition** | **Confirms** the direction (`E4001` raises rather than resolves) but the required conflict *report* is far richer than the diagnostic that exists, and the architectural seam for future strategies is not yet built. |

One decision confirms, one contradicts, one extends. The contradiction is the
work.

**Nothing below is scheduled.** This document maps consequences and names the
secondary questions each decision opens. Per the instruction, no secondary
ambiguity discovered here has been resolved: each is marked
`OPEN DESIGN DECISION` and carries an identifier so it can be decided by name.

---

## 1. The approved decisions, restated as engineering requirements

### Decision 1 — Fact immutability

| # | Requirement | Status in code |
|---|---|---|
| 1a | A fact's identity and value cannot be mutated in place | **Holds.** `FactSet::insert` never overwrites (`crates/logic/src/facts.rs`). |
| 1b | A later observation or computation does not overwrite the original | **Holds** in the sense that nothing overwrites; but there is no *later observation* concept at all, so the requirement is untested against the case it was written for. |
| 1c | No API that semantically behaves as `fact.value = new_value`, unless explicitly a State operation | **Holds.** No setter exists; `Fact.value` is a public field on a struct only ever constructed inside `insert`. See risk R1 below. |
| 1d | The language distinguishes **Fact / Observation / Derived Fact / State** | **Does not hold.** `Origin` has two variants: `Declared`, `Derived(RuleId)`. `Observation` and `State` do not exist. |
| 1e | Provenance is preserved — a derived or observed value is traceable to the event or rule that produced it | **Holds for derived** (`Origin::Derived(RuleId)`, `FactDerived` trace events, `why`). **Undefined for observed**, since observations do not exist. |

### Decision 2 — three distinct conditions

| # | Requirement | Status in code |
|---|---|---|
| 2a | `Unknown` = not enough information to determine a value | **Holds.** `Evaluated::NotEvaluable`, and absence from `FactSet`. |
| 2b | `Null` = known absence of a value; a *known* logical state | **Absent.** No `Value` variant, no `Type`, no literal, no operator behaviour. |
| 2c | `Known(Value)` = a concrete value | **Holds.** `Evaluated::Known(Value)`. |
| 2d | `Unknown ≠ Null ≠ false`, and the evaluator must not silently collapse them | **Violated in one place today**: the trace's JSON writes `Unknown` as JSON `null` (`crates/trace/src/json.rs`, `OutputEmitted`). Once `Null` exists, that encoding collapses exactly the two states this decision separates. |
| 2e | Truth evaluation, comparison, logical operators and rule activation define behaviour for **all three** states | **Not defined.** Two-state behaviour is implemented; the three-state matrix is open — see §3. |

### Decision 3 — conflict policy

| # | Requirement | Status in code |
|---|---|---|
| 3a | The core must not silently resolve incompatible conclusions | **Holds.** `crates/reasoning/src/lib.rs` raises rather than picking. |
| 3b | No implicit priority, source order, race winner, or last-write-wins | **Holds.** None exists. |
| 3c | The runtime reports Conflict ID, conflicting rules, conflicting conclusions, relevant facts, relevant conditions, execution context, trace references | **Partially.** The `E4001` diagnostic carries both rules by name, both values, and a span. It carries **no** conflict id, no relevant facts, no relevant conditions, no execution context and no trace references. |
| 3d | The architecture supports *future explicit* strategies without silently changing existing programs | **Structurally ready, not built.** Resolution is one match arm; `FactSet::insert` already only *reports* a conflict. There is no strategy interface and no way to select one. |

---

## 2. Impact by subsystem

Each table: what the decision requires, which decision drives it, where in the
code, and what it breaks.

### 2.1 AST — `crates/ast`

| Change required | From | Site | Breaks |
|---|---|---|---|
| A literal for `Null` — **if** `Null` is writable in source | 2b | `Literal` (`src/expr.rs`), and the lexer/parser behind it | Nothing yet: `null` is **already a reserved word** (`crates/lexer/src/token.rs`), so adding it is not a breaking change. That reservation now pays for itself. |
| Possibly a form for declaring an `Observation` distinct from a `fact` | 1d | `Item`, `program.rs` | Additive |
| Possibly a form for `State` | 1d | same | Additive |

Blocked on **O2.6** (can a program write `Null`?), **O1.1**, **O1.2**.
If `Null` is only ever produced by the data layer and never written in source,
the AST is untouched by decision 2.

### 2.2 Type system — `crates/types`

This is where decision 2 costs the most, because it forces a question the
implementation currently answers by omission.

| Change required | From | Site | Breaks |
|---|---|---|---|
| `Null` gains a place in the type lattice | 2b | `Type` (`src/lib.rs`) | Every `match` on `Type` |
| `Value` gains a representation for `Null` | 2b | `Value` (`src/value.rs`) | Every `match` on `Value` — 6 sites in `logic`, 2 in `trace`, 1 in `cli`, 1 in `ir` |
| The operator table defines every operator against `Null` | 2e | `binary_result`, `unary_result` | The 5 unit tests that assert the table's current shape |
| `Value`'s `Eq`/`Ord` must define `Null` | 2b, 3c | `impl PartialEq for Value` | The conflict check and the fact set both rely on total equality |

Blocked on **O2.1** — and this one gates all the others:

> **O2.1 OPEN DESIGN DECISION.** Is `Null` (a) an inhabitant of every type, as in
> SQL, so `Int` means *integer or null*; (b) its own type `Null`, so a name is
> either `Int` or `Null` and never both; or (c) a modifier, `Int?`, making
> nullability part of a name's declared type?
> Each answer produces a different type system. (a) makes every operator partial
> and every rule condition three-valued. (b) makes `E3010 ConflictingNameType`
> fire whenever a rule derives `Null` for a name another rule gives an `Int` —
> which would make `Null` nearly unusable without a union type. (c) is the most
> expressive and the most work, and needs annotation syntax the language does
> not have.

### 2.3 IR — `crates/ir`

| Change required | From | Site | Breaks |
|---|---|---|---|
| `Op::Const(Value)` carries `Null` if `Value` does | 2b | `src/code.rs` | Nothing structurally — `Const` holds a `Value`, so it follows the type crate |
| `IR_VERSION` bump | 2b | `src/program.rs` | The IR snapshot in `crates/compiler/tests/compile.rs`; the digest of every program, hence every recorded `program_digest` |
| Possibly `Op::IsNull` / `Op::IsUnknown` | 2e | `src/code.rs` | Additive |

Blocked on **O2.1**, and on **L** in `OPEN_DESIGN_DECISIONS.md` (is there an
`is_unknown` operator at all — a program branching on its own ignorance).

Note the IR's insulation working as intended: the IR changes *because the value
domain changed*, not because syntax or the engine changed.

### 2.4 Logic engine — `crates/logic`

The largest concentration of change.

| Change required | From | Site | Breaks |
|---|---|---|---|
| `Evaluated` becomes three-state, or `Value` absorbs `Null` while `Evaluated` stays two-state | 2a–2c | `Evaluated` (`src/eval.rs`) | Every caller: `reasoning::infer`, `reasoning::activate`, `compiler::lower_facts` |
| `eval` propagates `Null` distinctly from `NotEvaluable` | 2d, 2e | `eval()` loop, `Op::Load` arm | The 10 evaluation tests |
| `apply()` defines ~13 operators × the `Null` cases | 2e | `apply()` — currently ~30 match arms | Same |
| `Origin` gains `Observed` (and possibly a state-transition origin) | 1d | `Origin` (`src/facts.rs`) | `reasoning`, `runtime`, `trace`, `why` |
| `FactSet::get` must distinguish "bound to `Null`" from "not bound" | 2d | `FactSet` | `knows_all`, `missing_from`, and therefore rule activation |

Blocked on **O2.2**, **O2.3**, **O2.4**, **O2.5**, **O1.1**.

> **O2.2 OPEN DESIGN DECISION** — arithmetic with `Null`. Is `1 + Null` equal to
> `Null` (SQL-style propagation), an error (`E5xxx`), or `Unknown`? Note that
> "propagates to `Null`" and "propagates to `Unknown`" are *different answers*
> under decision 2, and the current code has no way to express the difference.
>
> **O2.3 OPEN DESIGN DECISION** — comparison with `Null`. Is `Null == Null` true
> (`Null` is a known state, so knowing both sides are absent is knowing they
> match), or `Null` (SQL's answer), or a type error? Decision 2's own wording —
> *"`Null` is a known logical state"* — points at `true`, which is the opposite
> of SQL. This must be decided explicitly, not inherited from SQL by habit.
>
> **O2.4 OPEN DESIGN DECISION** — the truth lattice. With three conditions and a
> boolean, a condition can be `true`, `false`, `Null` or `Unknown`. Is the logic
> three-valued (`Null` and `Unknown` collapse for `and`/`or` purposes but not
> elsewhere) or genuinely four-valued? What is `true and Null`? `false and
> Unknown`? A full 4×4 table for `and`, `or` and `not` is required, and I will
> not fill it in.
>
> **O2.5 OPEN DESIGN DECISION** — *the central question*. A rule reads a name
> that is bound to `Null`. Is the rule **evaluable**? `Unknown` means "retry
> later"; `Null` means "the answer is: there is nothing". The natural reading is
> that a name bound to `Null` *is* known, so the rule is evaluable and its
> condition evaluates to whatever O2.4 says. But this is exactly the decision
> that determines which rules fire, and therefore what programs mean.

### 2.5 Reasoning engine — `crates/reasoning`

| Change required | From | Site | Breaks |
|---|---|---|---|
| Structured conflict record replacing a formatted message | 3c | the `Insertion::Conflict` arm of `activate()` | The `E4001` fixture only checks the code, so the fixture survives; the runtime test checks the code only |
| Conflict id, relevant facts, relevant conditions, execution context, trace references gathered at the conflict site | 3c | same | Needs data `reasoning` currently discards — the rule's reads are available, their *values* are in the `FactSet`, and trace references need the `seq` of the events involved |
| A strategy seam: resolution becomes a policy object rather than a hard-coded raise | 3d | same arm | Nothing today; the default policy is the current behaviour |
| The evaluability gate `knows_all(reads)` re-examined for `Null` | 2e, O2.5 | `infer()` | Rule firing |

Blocked on **O3.1**, **O3.2**, **O3.4**, **O3.5**, **O2.5**.

> **O3.1 OPEN DESIGN DECISION** — is a `Conflict` **fatal**? Decision 3 says
> execution "enters an explicit `Conflict` condition" and lists what must be
> reported. It does not say whether the run stops. Today it stops. The
> alternatives — continue with the name left unbound, continue with the name
> marked conflicted, or collect all conflicts and stop at the end — produce
> different programs and different traces.
>
> **O3.2 OPEN DESIGN DECISION** — one conflict per run, or many? Follows from
> O3.1.
>
> **O3.3 OPEN DESIGN DECISION** — what is "logically incompatible"? Two rules
> deriving the *same* value is currently redundancy, not conflict, which matches
> decision 3's wording. But with `Null` present: is `Null` vs `Known(5)` a
> conflict? Is `Null` vs `Null` redundancy? Almost certainly yes and yes, and
> "almost certainly" is not a decision.
>
> **O3.4 OPEN DESIGN DECISION** — the form and scope of a Conflict ID. Per run,
> or globally stable across runs? Deterministic (a hash of the participants) or
> sequential? A sequential id is simple; a content-derived id lets two runs of
> one program be compared, which matters if trace comparison stays a criterion.
>
> **O3.5 OPEN DESIGN DECISION** — where do future strategies attach: a language
> construct (`rule heat priority 10:`), a per-program declaration, or external
> configuration? Decision 3 permits "explicit language constructs **or**
> configuration". The choice determines whether `crates/parser` and the AST are
> involved at all.

### 2.6 Runtime — `crates/runtime`

| Change required | From | Site | Breaks |
|---|---|---|---|
| `Output.value` gains a third case | 2b, 2d | `Output`, `Execution::to_text()` (`src/execute.rs`) | The plain-text result format — `to_text` prints `name = unknown`; a `Null` needs a distinct rendering |
| Execution context assembled for a conflict report | 3c | `run()`, `ExecutionContext` | Additive |
| `Observation` ingestion path, if observations enter at run time | 1d | `run()` — currently takes only `(IrProgram, Limits)` | The runtime's signature, and its purity: an observation arriving from outside is the first thing that could make a run non-reproducible |

Blocked on **O1.1**, **O1.3**, **O2.7**.

> **O2.7 OPEN DESIGN DECISION** — how are the three conditions *rendered*?
> `status = unknown` exists today. `Null` needs its own spelling in the plain
> text format, in JSON, and in the trace, and the three must not be confusable —
> which is requirement 2d applied to output rather than to evaluation.
>
> **O1.3 OPEN DESIGN DECISION** — do observations enter *before* a run (making
> the runtime still a pure function of its inputs) or *during* one? The second
> would interact with determinism, which Master Spec §22 fixes for results.

### 2.7 Trace — `crates/trace`

| Change required | From | Site | Breaks |
|---|---|---|---|
| `Emitted` gains `Null` | 2b | `src/event.rs` | `runtime`, `cli` |
| **The JSON encoding must stop writing `Unknown` as `null`** | 2d | `src/json.rs`, `OutputEmitted` | The current encoding is a direct violation of decision 2 the moment `Null` exists — two distinct semantic states, one JSON token |
| `FactObserved` event, and an origin for it | 1d, 1e | `TraceEventKind` | Additive |
| A `Conflict` event carrying id, rules, conclusions, facts, conditions, trace references | 3c | `TraceEventKind`, and `ErrorRaised` which currently carries only code and message | Additive; the JSON writer follows |
| `why` explains a `Null` and distinguishes it from "no derivation found" | 1e, 2d | `src/explain.rs` — `why` returns `Option`, and `None` currently means "the trace does not say" | `why` would need to distinguish *"derived as `Null`"* from *"not derived"*, which is the same three-state distinction one level up |

Blocked on **O2.7**, **O3.4**, and the still-open question of whether the trace
is a correctness criterion (register §13, unchanged by these decisions).

### 2.8 Tests — `crates/*/tests`, `tests/runtime`, `tests/e2e`

| Test group | Effect |
|---|---|
| `crates/types` (5 operator tests) | Rewritten once O2.1–O2.4 are answered |
| `crates/logic` (10 evaluation tests) | Rewritten; `not_evaluable_propagates_through_and_and_or` becomes a three- or four-state matrix |
| `crates/semantic` (17 tests) | Mostly survive; type tests change with the lattice |
| `crates/runtime` (14 tests) | `a_rule_whose_condition_is_false_does_not_fire` and the pending tests need a `Null` sibling |
| `crates/compiler` (8 tests) | The IR snapshot changes with `IR_VERSION` |
| `tests/runtime` (11 fixtures) | All 11 still pass — none uses `Null`. **New fixtures required**: one per cell of the O2.4 truth table, one for `Null` vs `Unknown` output, one for conflict reporting |
| `tests/e2e` | The harness is unaffected; `.expected` gains a `null` spelling |
| **New** | A test asserting `Unknown`, `Null` and `false` are pairwise distinguishable in *every* output format — the direct executable form of requirement 2d |

---

## 3. What cannot proceed, and what can

**Blocked on the open sub-decisions above** (O1.1–O1.5, O2.1–O2.9, O3.1–O3.5):
every code change listed in §2. Implementing `Null` without O2.1 would repeat
exactly the failure the audit was called over — an implementation choosing what
the language means.

**Safe to proceed now, because these follow only from what is already approved:**

1. **Fix the JSON `null` collision** (2d). Writing `Unknown` as JSON `null` is
   already wrong under decision 2, independent of how `Null` is finally
   represented. Needs a spelling decision (O2.7) to pick the replacement.
2. **Structured conflict record** (3c). What must be reported is fully
   specified. Only the id's form (O3.4) and whether it is fatal (O3.1) are open,
   and a record can be built without settling either.
3. **The strategy seam** (3d). Making resolution a named policy with exactly one
   implementation — "raise" — changes no behaviour and adds no semantics.
4. **`Origin` widened to name the four categories** (1d), with `Observation` and
   `State` present as *documented, unimplemented* categories rather than
   silently missing — provided nothing pretends to produce them.

Items 1–4 are not started. This document is analysis; the next step is the
owner's, not mine.

---

## 4. Risks the decisions expose in existing code

**R1 — `Fact.value` is a public field.** `crates/logic/src/facts.rs` declares
`pub struct Fact { pub value: Value, pub origin: Origin }`. Nothing mutates it,
and `FactSet` hands out `&Fact`, so no mutation is reachable through the public
API today. But decision 1c asks for an API that cannot *semantically* behave as
`fact.value = new_value`, and a public field on a struct an embedder could
construct is weaker than the decision deserves. Recommend (not applied): make
the fields read-only through accessors.

**R2 — `docs/04` §9 now contradicts decision 2.** It maps SQL `NULL` to
`Unknown`, and argues `Null` was not needed as a value *because* of that
mapping. Under decision 2 those are different states, and the mapping is
almost certainly wrong. Flagged as **O2.8 OPEN DESIGN DECISION**: does SQL
`NULL` become `Null` (known absence), or `Unknown` (no information)? A column
that is `NULL` is known to be absent, which argues for `Null` — but a row that
was never fetched is `Unknown`, and the data layer must distinguish them.

**R3 — `Unknown` is currently unrepresentable as a value, by design.**
`docs/04` §4 says so explicitly. Decision 2 does not say whether that survives:
it defines `Unknown` as a *condition*, and `Null` as a *known state*, which
suggests `Null` is a value and `Unknown` is not. **O2.9 OPEN DESIGN DECISION**
— confirm that asymmetry, because it determines whether `Unknown` can be stored
in a fact, passed to an operator, or written in source.

**R4 — decision 1d's four-way distinction has no boundary yet.** `Observation`
and `State` are to be "specified separately". Until they are, the honest state
of `Origin` is: two of four categories implemented, two absent. Widening the
enum before those specifications exist would create exactly the kind of
half-built vocabulary the audit criticised, so it has not been done.
