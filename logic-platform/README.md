# logic-platform

A programming language and runtime in which **facts, rules, inference, data and
the program's own structure are explicit, executable objects** — and in which
every result can be traced back to the facts and rules that produced it.

This is the language/runtime project of the Master Specification (*Logic
Management, Reasoning & Code Intelligence Language*), built as a
systems-engineering project rather than a prototype: `docs/` is the contract,
`crates/` is the implementation, and the tests are what makes the difference
between "implemented" and "verified".

> **State: language version 0.1 — the first vertical slice, VERIFIED.**
> Source → lexer → parser → AST → semantic validation → IR → rule engine →
> runtime → output → trace, with 127 passing tests and zero dependencies.
> SQL, state, queries, the IDE and packaging are later phases and are **absent
> from the tree**, not stubbed.

---

## Try it

```bash
cd logic-platform
cargo run -q -p lml-cli -- run examples/temperature.lml
```

```lml
fact temperature = 31

rule heat:
    when temperature > 30
    then status = "hot"

output status
```

```
status = "hot"
```

The interesting part is not the answer. It is that the engine can be asked
**why**:

```bash
cargo run -q -p lml-cli -- why examples/temperature.lml status
```

```
status = "hot" by rule `heat`
  temperature = 31 declared
```

That explanation is reconstructed from the execution trace, never re-derived and
never guessed. If the trace cannot support an answer, the tool says so instead of
inventing one.

```bash
cargo run -q -p lml-cli -- trace examples/temperature.lml
```

```
   0 ExecutionStarted language=0.1 ir=1 program=ec777c22…
   1 FactDeclared temperature = 31
   2 RoundStarted 1
   3 ConditionEvaluated heat -> true
   4 RuleActivated heat reads temperature
   5 FactDerived status = "hot" by heat
   6 RoundFinished 1 fired=true
   7 RoundStarted 2
   8 RoundFinished 2 fired=false
   9 OutputEmitted status = "hot"
  10 ExecutionFinished rounds=2 facts=2 fired=1
```

No timestamps: two runs of one program produce byte-identical traces, which is
what makes a trace comparable, diffable and testable.

## Read these first, in order

| # | Document | What it decides |
|---|---|---|
| 1 | [`docs/01_LANGUAGE_CONSTITUTION.md`](docs/01_LANGUAGE_CONSTITUTION.md) | What the language *is*: fact, rule, inference, conflicts, determinism |
| 2 | [`docs/02_FORMAL_SEMANTICS.md`](docs/02_FORMAL_SEMANTICS.md) | The precise, testable rules — lexical, grammar, static, dynamic |
| 3 | [`docs/03_EXECUTION_MODEL.md`](docs/03_EXECUTION_MODEL.md) | The pipeline, the IR, state ownership, limits, the trace |
| 4 | [`docs/04_TYPE_AND_DATA_MODEL.md`](docs/04_TYPE_AND_DATA_MODEL.md) | The four types, `Unknown`, the operator table |
| 5 | [`docs/05_ARCHITECTURE.md`](docs/05_ARCHITECTURE.md) | Crates, responsibilities, the enforced dependency graph |
| — | [`docs/OPEN_DESIGN_DECISIONS.md`](docs/OPEN_DESIGN_DECISIONS.md) | What is decided, what is open, and three weaknesses in the spec |
| — | [`docs/STATUS.md`](docs/STATUS.md) | SPECIFIED / IMPLEMENTED / VERIFIED, per component |

## Three ideas the design is built on

**Unknown is not false.** A rule that reads a name nothing has derived yet is not
*false* — it is *not yet evaluable*, and it is retried. An output nothing derived
is `unknown`, not `0` and not `""`. This is what lets the engine distinguish "the
answer is no" from "I do not know", which is the distinction most rule engines
lose.

**A silent winner is a bug.** Two rules deriving different values for one name is
an error (`E4001`), not a race resolved by source order or an undeclared
priority. Any resolution strategy can be added later as an explicit opt-in
without changing what an existing program means.

**A trace is behaviour, not logging.** Two runtimes that agree on outputs but
disagree on traces are not both correct. There are no log levels in a trace, no
free text, and no clock.

## Layout

```
docs/                the specification: five binding documents + the registers
crates/
  diagnostics/       spans, error codes, diagnostics        (leaf)
  lexer/  ast/  parser/                                     text → tokens → tree
  types/  semantic/                                         types, names, checks
  ir/  compiler/                                            lowering, the runtime contract
  logic/                                                    facts, fact set, evaluation
  reasoning/                                                the fixed point, conflicts
  trace/                                                    events, JSON, explanation, SHA-256
  runtime/                                                  context, limits, the run loop
  cli/                                                      the `lml` binary
tests/runtime/       language fixtures: <name>.lml + <name>.expected
tests/e2e/           the harness that runs every fixture
benchmarks/          stage timings — measurement, never a basis for guessing
scripts/             the architecture lint
```

`crates/sql` and `crates/security` are not here because they are not built. A
directory containing a `todo!()` is a lie about the state of the system.

## Working on it

```bash
make verify        # fmt + clippy + architecture lint + every test
make test          # tests only
make lint-arch     # the dependency rules, enforced by grep
make bench         # stage timings
```

Rules that CI enforces, not conventions that reviewers remember:

- **Zero third-party dependencies.** `cargo test` works offline on a fresh
  clone; the supply-chain surface is empty. A test fails if one appears.
- **No `unsafe`.** `#![forbid(unsafe_code)]` in every crate.
- **No `unwrap`, `expect` or `panic!` in library code.** A runtime that aborts is
  a denial of service, not a diagnostic. Tests may panic; that is their job.
- **The dependency graph is checked.** No crate may learn about the CLI, a UI, or
  a database engine.

## Adding a language test

Two text files:

```bash
echo 'fact a = 1
rule r: when a > 0 then b = a + 1
output b' > tests/runtime/my_case.lml
echo 'b = 2' > tests/runtime/my_case.expected
cargo test -p lml-e2e
```

A fixture that expects rejection puts `error: E3011` in its `.expected`.

## What comes next

In order, and not before its predecessor is VERIFIED in `docs/STATUS.md`:

1. **0.2 — state and query.** Blocked on two open design decisions: the mutation
   model and query semantics. Neither will be guessed at.
2. **0.3 — relational facts.** Arity, variables, unification. This is what turns
   the engine from propositional into Datalog-class, and it should come *before*
   SQL rather than after it.
3. **Phase 6 — SQL**, behind a `DataSource` trait, with query results entering
   the trace so that determinism survives.
