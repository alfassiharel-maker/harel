# ENGINEERING IMPLEMENTATION SPEC — as it applies to this repository

Source: *Engineering Implementation Spec v1.0 — Development Environment, Code
Architecture, File Structure & Implementation Rules*, supplied with the project.

**This file is a restatement, not the issued document.** Where the two differ,
the issued document governs. It exists so that every rule the repository is
judged against is checkable from inside the repository, and so that each rule
can be traced to the thing that enforces it. Where the issued spec left a choice
open, the choice made here is named, with its reason.

Precedence: the **Master Specification** decides semantics; **this** decides how
the code is organised; `docs/01`–`docs/05` decide the language.

---

## What is enforced, and by what

| Issued rule | Where it lives here | Enforced by |
|---|---|---|
| §7 Repository architecture | `docs/`, `crates/`, `tests/`, `examples/`, `benchmarks/`, `scripts/`, `configs/` | the tree itself |
| §8 One responsibility per module | `docs/05_ARCHITECTURE.md` §2 | review + `scripts/check-architecture.sh` |
| §9–§25 crate responsibilities | `crates/{lexer,ast,parser,semantic,types,ir,compiler,logic,reasoning,runtime,trace,cli}` | the dependency lint |
| §19 State ownership in one place | `runtime::ExecutionContext` | no `static mut`, no singleton, no thread-local anywhere |
| §21 Logging is not trace | `crates/trace` has no levels; nothing else writes events | review; the trace type admits no free-text level |
| §29 `unsafe` | none exists | `#![forbid(unsafe_code)]` + lint rule 7 |
| §30 Dependency policy | zero third-party crates | `tests/e2e/tests/no_dependencies.rs` + lint rule 8 |
| §32 No god file | `main.rs` is 45 lines and dispatches | review; the file-size rule in `docs/05` §4 |
| §35 Core must not depend on UI | no crate names Tauri/TypeScript/React | lint rule 2 |
| §36 Core must not depend on a database | no crate names SQLite/Postgres/DuckDB | lint rule 3 |
| §37–§40 Test architecture | unit tests beside the code, integration tests per crate, `tests/runtime/*.lml` fixtures, snapshots for AST/IR/trace | `cargo test --workspace` |
| §42 CI | `.github/workflows/logic-platform.yml` runs fmt, clippy, the architecture lint, tests, a release build, and the canonical program | CI |
| §44 Commit policy | atomic, `feat(scope): …` | review |
| §45 Change management: docs → tests → code | `docs/01` §9 amendment procedure | review |
| §56 No magic numbers | every limit is a named constant with a written reason | `docs/03_EXECUTION_MODEL.md` §5 |
| §57 No `unwrap`/`expect`/`panic!` in production paths | denied workspace-wide, allowed in tests | `clippy.toml` + workspace lints |
| §58 Determinism | ordered maps, source order, no clock | fixture harness re-runs and compares |
| §59 Concurrency only after measurement | single-threaded | `docs/03` §9 |
| §60 Optimisation only after measurement | nothing is optimised | `benchmarks/` |
| §62 Configuration is centralised | `configs/README.md` explains why it is empty | — |
| §63 No secrets in the repository | none | — |

## Choices the issued spec left to the implementer

**Rust for the production core (§5).** Followed. Rust 1.75+, edition 2021.

**Language: Rust only, so far.** TypeScript, Tauri and Python are named in the
issued spec for the desktop product, research and tooling. None is used yet,
because §6 of the Master Specification's phase order puts the IDE at Phase 9 and
because there is nothing to prototype in Python that the Rust tests do not
already answer. Adding either is a decision to be recorded, not a default.

**Parser technology (§27 of the Master Spec).** Hand-written recursive descent.
The grammar is small and LL(1) but for one lookahead, and a hand-written parser
gives far better diagnostics than a generator's "unexpected token" — which
matters more here than the lines saved. Revisit if the grammar outgrows one
file (`docs/OPEN_DESIGN_DECISIONS.md` N).

**Prototype vs production (§46).** There is no `prototype/` directory. Nothing
in this repository is prototype code: the vertical slice is small, but it is
specified, tested and reviewable. If throwaway exploration is needed later it
goes in `prototype/` and never merges into `crates/` without a rewrite.

**One extra crate beyond the issued list.** `crates/diagnostics`, justified in
`docs/05_ARCHITECTURE.md` §3.1.

## The reporting format (§72)

Substantial changes report:

```
UNDERSTOOD     what the task changes
ARCHITECTURE   which modules are affected
IMPLEMENTATION files changed, and why each changed
TESTING        tests run, and their results
RISKS          what remains uncertain
STATUS         SPECIFIED / IMPLEMENTED / VERIFIED
```

`docs/STATUS.md` holds the standing answer to the last line.

## The standing instruction (§71, §74)

Build this as a programming-language and systems-engineering project, not as a
prototype application. Keep language semantics, compiler, logic engine,
reasoning engine, runtime, data access, UI and packaging strictly separate. Do
not move semantics into a UI or a script. Do not put database-specific logic in
the core. Do not create monolithic files. Do not add a feature without a
specification. Do not invent undefined semantics — mark `OPEN DESIGN DECISION`
instead. Do not confuse implemented code with verified behaviour. Correctness
and semantic integrity come before optimisation; commercial packaging is the
last layer, never the foundation.
