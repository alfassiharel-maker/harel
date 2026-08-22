# Logic Platform

A new programming language and runtime for logic, rules, reasoning, data and
decision systems with inspectable execution.

## Quick start

```bash
cargo build --release
./target/release/lgc run examples/grandparent.lgc
./target/release/lgc run examples/grandparent.lgc --trace
./target/release/lgc check examples/grandparent.lgc
```

## Language

Programs are `.lgc` files.  Three declaration forms:

```
# Assert a base fact (all arguments must be ground constants)
fact parent("Alice", "Bob");
fact parent("Bob", "Charlie");

# Declare a rule (uppercase = variable, lowercase = relation name)
rule grandparent(X, Z) when parent(X, Y), parent(Y, Z);

# Query — variables are output columns
query grandparent(X, Y);
```

Running the above produces:

```
grandparent:
  ("Alice", "Charlie") — derived by rule #0 from [parent("Alice", "Bob"), parent("Bob", "Charlie")]
```

### Value model

Three states, never collapsed:

| State | Meaning |
|-------|---------|
| `unknown` | System lacks information |
| `null` | System knows no value is present |
| `"text"`, `42`, `3.14`, `true`, `false` | Concrete known values |

## Architecture

```
crates/
  diagnostics/  — error codes, spans, severity (LGC01xx–LGC05xx)
  types/        — three-state value model (Unknown/Null/Known)
  lexer/        — source text → tokens
  ast/          — token stream → AST nodes
  parser/       — AST construction with error recovery
  logic/        — ground facts, rules, fact store, provenance
  trace/        — structured semantic execution events (NDJSON)
  reasoning/    — unification, fixed-point inference, conflict model
  runtime/      — AST→logic loader, execution context, query answers
  cli/          — `lgc` binary (run / check / --trace)
```

Dependency direction: `cli → runtime → reasoning → logic → types`.  
Core crates have no UI, database, or network dependencies.

## Tests

```bash
cargo test           # 41 tests, 0 failures
cargo clippy         # warnings are zero
```

## Docs

- `docs/00-language-constitution.md` — foundational decisions and component status

## Development phases

Phase 0.1–0.3 (this milestone): vertical slice + three-state semantics + relational logic.  
Phase 1: SQL/data adapter.  Phase 2: CLI maturation.  Phase 3: security sandbox.  
Phase 4: IDE.  Phase 5: metaprogramming.  Phase 6: AI integration.
