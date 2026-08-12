# STATUS

Master Specification §49: every component is **SPECIFIED**, **IMPLEMENTED** or
**VERIFIED**, and *"never report a component as completed merely because source
code exists."*

Here, the three mean:

| State | Means |
|---|---|
| SPECIFIED | written down in `docs/`, precisely enough to implement or refute |
| IMPLEMENTED | code exists and compiles |
| VERIFIED | tests exist that fail if the behaviour is wrong, and they pass |

Nothing below is marked VERIFIED because it "looked right".

> **Provenance matters as much as the tick.** Rows marked ✅ VERIFIED for
> owner-approved semantics (OD-1, OD-2, OD-3 and the O2 follow-ups) are verified
> against decisions the owner made. The rest are verified against decisions the
> implementation made, which `docs/SEMANTIC_DECISION_REGISTER.md` classifies one
> by one. The tests prove the engine does what the documents say; only the
> register says who decided what they say.

---

## Language core (Master Spec §50)

| Component | SPEC | IMPL | VERIFIED | Evidence |
|---|:--:|:--:|:--:|---|
| Grammar | ✅ | ✅ | ✅ | `crates/parser/grammar.ebnf`, `crates/parser/tests/parse.rs` |
| Lexer | ✅ | ✅ | ✅ | `crates/lexer/tests/tokenize.rs` (15 tests) |
| Parser | ✅ | ✅ | ✅ | `crates/parser/tests/parse.rs` (14 tests) |
| AST | ✅ | ✅ | ✅ | canonical printer + snapshot in the parser tests |
| Semantic rules N1–N7 | ✅ | ✅ | ✅ | `crates/semantic/tests/analyze.rs`, one test per rule |
| Type system T1–T5 | ✅ | ✅ | ✅ | same file + `crates/types` unit tests |
| Facts — immutable (OD-1) | ✅ | ✅ | ✅ | `crates/logic/src/facts.rs` tests, runtime tests |
| **Value model — `Unknown` / `Null` / `Known` (OD-2)** | ✅ | ✅ | ✅ | `crates/types/src/value.rs`, `crates/logic/tests/evaluate.rs`, 4 fixtures |
| **Existence predicates `is null` / `is unknown` / `is known`** | ✅ | ✅ | ✅ | lexer, parser, IR and evaluator tests; `state_predicates.lml` |
| **Pending rules (O2.11)** | ✅ | ✅ | ✅ | `crates/runtime/tests/execute.rs`, `pending_rule.lml`, `RulePending` events |
| Rules | ✅ | ✅ | ✅ | `crates/runtime/tests/execute.rs` |
| Inference (forward chaining, fixed point) | ✅ | ✅ | ✅ | `crates/runtime/tests/execute.rs` |
| Conflict semantics (OD-3): explicit, seven-element report | ✅ | ✅ | ✅ | `the_conflict_report_carries_what_od3_requires`, `conflicting_rules.lml` |
| Conflict strategy seam (`trait ConflictStrategy`) | ✅ | ✅ | ✅ | `crates/reasoning/src/conflict.rs` |
| Cycle behaviour | ✅ | ✅ | ✅ | `a_value_cycle_reaches_a_fixed_point` |
| Determinism | ✅ | ✅ | ✅ | fixture harness re-runs every fixture and compares traces |
| Error system (codes, spans, help) | ✅ | ✅ | ✅ | `crates/diagnostics` tests; every error path has a test |
| IR | ✅ | ✅ | ✅ | `crates/compiler/tests/compile.rs`, canonical snapshot |
| Runtime | ✅ | ✅ | ✅ | `crates/runtime/tests/execute.rs` (14 tests) |
| Regression tests | ✅ | ✅ | ✅ | `tests/runtime/` fixtures, run by `tests/e2e` |
| **State model** | ❌ | ❌ | ❌ | **OPEN DESIGN DECISION B / O1.2.** Absent, not stubbed. |
| **`Observation` as a distinct origin** | ⚠️ | ❌ | ❌ | Required by OD-1; its representation is **O1.1**, unspecified. `Origin` has two of the four categories, and no placeholder for the others. |

## Reasoning system (Master Spec §51)

| Component | SPEC | IMPL | VERIFIED |
|---|:--:|:--:|:--:|
| Fact derivation | ✅ | ✅ | ✅ |
| Rule activation | ✅ | ✅ | ✅ |
| Dependencies represented | ✅ | ✅ | ✅ (`SemanticModel::readers_of`, `IrRule::reads`) |
| Trace | ✅ | ✅ | ✅ |
| Explanation generated from the trace | ✅ | ✅ | ✅ (`why`, and it returns `None` rather than guessing) |
| Why a rule did **not** fire: false vs pending | ✅ | ✅ | ✅ (`ConditionEvaluated` beside `RulePending`) |
| Execution id and timing, without breaking comparability | ✅ | ✅ | ✅ (`TraceMetadata`, excluded from `PartialEq`) |
| Cycles have defined behaviour | ✅ | ✅ | ✅ |
| Conflicts have defined behaviour | ✅ | ✅ | ✅ |
| Determinism tested | ✅ | ✅ | ✅ |

## SQL (Master Spec §52)

Every row is ❌. **Phase 6, not started.** `crates/sql` does not exist, and the
`E6xxx` error range is reserved and unallocated. See
`docs/OPEN_DESIGN_DECISIONS.md` — the SQL layer must not be designed against the
current named-fact model, and the determinism question there is unresolved.

## Product (Master Spec §53)

| Layer | State |
|---|---|
| Core, compiler, runtime, logic, reasoning, trace | VERIFIED |
| CLI | IMPLEMENTED (argument parsing is unit-tested; the commands are exercised by hand and by CI) |
| SQL, IDE, debugger, security, installer, signing, licensing, updates | NOT STARTED — later phases, deliberately |

## How to reproduce every claim above

```bash
cd logic-platform
make verify        # fmt, clippy, architecture lint, 159 tests
make run-example   # the canonical program, its trace, and its explanation
```

Counts are as of the commit that introduced this file; `make verify` prints the
current numbers, and CI fails if any of them regress.
