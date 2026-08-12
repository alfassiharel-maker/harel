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
| Facts | ✅ | ✅ | ✅ | `crates/logic/src/facts.rs` tests, runtime tests |
| Rules | ✅ | ✅ | ✅ | `crates/runtime/tests/execute.rs` |
| Inference (forward chaining, fixed point) | ✅ | ✅ | ✅ | `crates/runtime/tests/execute.rs` |
| Conflict semantics (`E4001`) | ✅ | ✅ | ✅ | runtime tests + `tests/runtime/conflicting_rules.lml` |
| Cycle behaviour | ✅ | ✅ | ✅ | `a_value_cycle_reaches_a_fixed_point` |
| Determinism | ✅ | ✅ | ✅ | fixture harness re-runs every fixture and compares traces |
| Error system (codes, spans, help) | ✅ | ✅ | ✅ | `crates/diagnostics` tests; every error path has a test |
| IR | ✅ | ✅ | ✅ | `crates/compiler/tests/compile.rs`, canonical snapshot |
| Runtime | ✅ | ✅ | ✅ | `crates/runtime/tests/execute.rs` (14 tests) |
| Regression tests | ✅ | ✅ | ✅ | `tests/runtime/` fixtures, run by `tests/e2e` |
| **State model** | ❌ | ❌ | ❌ | **OPEN DESIGN DECISION B.** Absent, not stubbed. |

## Reasoning system (Master Spec §51)

| Component | SPEC | IMPL | VERIFIED |
|---|:--:|:--:|:--:|
| Fact derivation | ✅ | ✅ | ✅ |
| Rule activation | ✅ | ✅ | ✅ |
| Dependencies represented | ✅ | ✅ | ✅ (`SemanticModel::readers_of`, `IrRule::reads`) |
| Trace | ✅ | ✅ | ✅ |
| Explanation generated from the trace | ✅ | ✅ | ✅ (`why`, and it returns `None` rather than guessing) |
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
make verify        # fmt, clippy, architecture lint, 127 tests
make run-example   # the canonical program, its trace, and its explanation
```

Counts are as of the commit that introduced this file; `make verify` prints the
current numbers, and CI fails if any of them regress.
