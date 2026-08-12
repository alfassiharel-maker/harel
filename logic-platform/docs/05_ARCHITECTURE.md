# ARCHITECTURE — logic-platform

Status: **Binding**
Implements: the Engineering Implementation Spec (`ENGINEERING_IMPLEMENTATION_SPEC.md`)

> ⚠️ **PROVISIONAL — not approved language semantics.**
> A design audit (`docs/DESIGN_AUDIT.md`) found that most of the rules in this
> document were decided by the implementation, not by the specification. Read
> every rule here together with `docs/SEMANTIC_DECISION_REGISTER.md`, which
> classifies each one as EXPLICITLY_SPECIFIED, DERIVED, ASSUMED BY
> IMPLEMENTATION, or OPEN DESIGN DECISION. Anything marked ASSUMED or OPEN is a
> **proposal awaiting the owner's decision**, regardless of how this document
> phrases it. The word "decided" below means "decided in code", not "approved".

---

## 1. Layers

```
┌──────────────────────────────────────────┐
│ app/  (TypeScript + Tauri)   NOT BUILT   │  Phase 9
├──────────────────────────────────────────┤
│ crates/cli                   BUILT       │  Phase 7
├──────────────────────────────────────────┤
│ crates/runtime               BUILT       │  Phase 3
│   crates/reasoning           BUILT       │  Phase 2
│   crates/logic               BUILT       │  Phase 2
│   crates/trace               BUILT       │  Phase 5
├──────────────────────────────────────────┤
│ crates/compiler              BUILT       │  Phase 4
│   crates/ir                  BUILT       │
├──────────────────────────────────────────┤
│ crates/semantic  crates/types            │  Phase 1
├──────────────────────────────────────────┤
│ crates/lexer  crates/parser  crates/ast  │  Phase 1
├──────────────────────────────────────────┤
│ crates/diagnostics           BUILT       │
├──────────────────────────────────────────┤
│ crates/sql        NOT BUILT              │  Phase 6
│ crates/security   NOT BUILT              │  Phase 10
└──────────────────────────────────────────┘
```

Layers that are not built are **absent from the tree**, not stubbed. A directory
containing a `todo!()` is a lie about the state of the system.

## 2. Crates and their single responsibility

| Crate | Owns | Must never contain |
|---|---|---|
| `diagnostics` | `Span`, `Diagnostic`, error codes, rendering | any knowledge of tokens, AST, or values |
| `lexer` | text → tokens | any notion of declarations or meaning |
| `ast` | the syntax tree data types | evaluation, validation, I/O |
| `parser` | tokens → AST, the grammar | type rules, name resolution |
| `types` | the type lattice, **the three-state `Value` model**, operator typing | evaluation, AST traversal |
| `semantic` | name resolution, type inference, all static checks | lowering, execution |
| `ir` | the IR data types, canonical printing, digesting | lowering *policy*, execution |
| `compiler` | orchestration of the four stages + lowering | any analysis of its own |
| `logic` | `FactSet`, `Origin`, expression evaluation | the value model itself, inference strategy, scheduling |
| `reasoning` | the fixed point, rule state, conflict detection | value representation, I/O |
| `trace` | trace events, JSON output, `why`, SHA-256 digest | log levels, timestamps |
| `runtime` | `ExecutionContext`, limits, the run loop | parsing, lowering |
| `cli` | argument parsing, file I/O, human output | any semantics whatsoever |

The right-hand column is the part that is enforceable, and it is enforced:
`scripts/check-architecture.sh` fails the build on a forbidden dependency edge,
and each rule below is one grep.

## 3. Dependency graph — enforced

```
diagnostics                     (leaf: depends on nothing)
ast         ──► diagnostics
lexer       ──► diagnostics
parser      ──► lexer, ast, diagnostics
types       ──► ast                       (operators are named by the AST)
semantic    ──► ast, types, diagnostics
ir          ──► types, diagnostics
logic       ──► ir, types, diagnostics
trace       ──► types, diagnostics
reasoning   ──► logic, ir, types, trace, diagnostics
compiler    ──► parser, semantic, ast, ir, logic, types, diagnostics
runtime     ──► reasoning, logic, ir, trace, types, diagnostics
cli         ──► compiler, runtime, trace, ir, ast, types, diagnostics
```

Two edges are not obvious and are therefore justified where they are declared,
in the manifest:

* **`types ──► ast`** — the operator table is keyed by the AST's operator enums.
  Duplicating them to avoid the edge would create two definitions of `+` that
  could drift; `ast` is pure data, so the edge costs nothing.
* **`compiler ──► logic`** — constant folding of a `fact`'s value uses the same
  evaluator the runtime uses. Without it, `fact a = 1 / 0` and a rule deriving
  `1 / 0` could disagree about what division by zero means.
* **`reasoning ──► trace`** — which rules were tried, in what order, and what
  they were waiting for is decided in `reasoning` and nowhere else, so nothing
  else can record it. `runtime` still *owns* the trace; `reasoning` appends to
  the one it is handed.

Invariants, each checked by `make lint-arch`:

1. **No crate depends on `cli`.** The CLI is a consumer, never a component.
2. **No crate depends on any UI, and no core crate names Tauri, TypeScript, or a
   window.** (Engineering Spec §35.)
3. **No core crate names a database.** `logic` may not mention SQLite, Postgres
   or DuckDB; when `sql` exists it will sit behind a `DataSource` trait defined
   in `logic`, with adapters below it. (Engineering Spec §36.)
4. **No cycles, and no undeclared edges.** Cargo rejects cycles; the script
   compares every crate's declared dependencies against the graph above, so an
   unintended-but-acyclic edge fails the build. (It did not, until the design
   audit found the gap — contradiction C5 — which is how three unused edges
   survived.)
5. **`logic` does not depend on `reasoning`.** Representation does not know about
   strategy. This is what makes a future backward-chaining engine an addition
   rather than a rewrite.
6. **Zero third-party dependencies in the entire workspace.** Not a stunt: it
   means `git clone && cargo test` works offline, on any machine, forever, and
   makes the supply-chain surface empty. Every dependency added later needs the
   §30 justification in writing, in `docs/DEPENDENCIES.md`.

### 3.1 Why `diagnostics` exists as its own crate

The Engineering Spec's crate list does not name it. It is added because the error
system (Master Spec §25) is genuinely cross-layer: `Span` and `Diagnostic` are
needed by the lexer *and* by the runtime. The alternatives were to define them in
`lexer` (making every crate depend on the lexer for an unrelated reason) or to
duplicate them (guaranteeing drift). A leaf crate with no dependencies is the
smallest thing that works. This is exactly the justification Engineering Spec §70
demands for the existence of a module.

## 4. File-level rules

- No file over ~500 lines. When one grows past that, it is doing two jobs.
- `main.rs` is a thin entry point: parse args, dispatch, map error to exit code.
  All of it is under 120 lines and contains no logic. (Engineering Spec §32.)
- One concept per file: `token.rs` has tokens, `span.rs` has spans, `value.rs`
  has values. A file named `utils.rs` or `helpers.rs` is not permitted — it is a
  place where responsibility goes to hide.

## 5. Testing architecture

```
crates/<name>/src/**       unit tests in #[cfg(test)] modules, next to the code
crates/<name>/tests/       integration tests against the crate's public API only
tests/runtime/*.lml        fixtures: source + .expected  (Engineering Spec §39)
tests/e2e/                 the harness that runs every fixture end to end
benchmarks/                measurement, not correctness
```

The fixture format is deliberately dumb: `basic_rule.lml` next to
`basic_rule.expected`, where the `.expected` file is the exact stdout of
`lml run --format=plain`. Adding a language test is adding two text files, so
there is no excuse not to add one. Fixtures are also the snapshot tests for the
IR and the trace (`.expected-ir`, `.expected-trace` where present).

**No randomness in any fixture or test.** A flaky test in an engine whose selling
point is determinism is a contradiction.

## 6. Build, check, run

```bash
cargo check --workspace              # fast
cargo test  --workspace              # everything, including fixtures
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all --check
make -C logic-platform verify        # all of the above + architecture lint
```

CI (`.github/workflows/logic-platform.yml`) runs exactly that list, on every push
that touches `logic-platform/**`. A red CI is a broken build, not a warning.

## 7. What comes next, in order

Per the Master Specification's phases; each is a separate, reviewable step:

1. **0.2 — state and query.** Requires resolving the mutation model and the
   query semantics (both OPEN). This is the largest open design item.
2. **0.3 — relational facts.** Facts with arguments, unification, joins. Turns
   the engine from propositional into Datalog-class.
3. **Phase 6 — SQL.** `DataSource` trait in `logic`, one adapter (SQLite),
   permissions, resource limits, SQL events in the trace.
4. **Phase 8 — meta layer.** `inspect`/`analyze` over the IR, sandboxed.
5. **Phase 9 — IDE.** Only once the CLI and the IR are stable.

No step begins before its predecessor is VERIFIED in `STATUS.md`.
