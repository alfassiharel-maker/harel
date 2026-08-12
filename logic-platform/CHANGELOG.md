# Changelog

Every entry that changes what a program means says so. Syntax and semantics are
frozen within a language version; `0.x` may break, `1.x` may not.

## 0.1.0 — the first vertical slice

The milestone of Master Specification §57: *a small but formally defined
language that can express facts and rules, execute them deterministically,
produce derived facts and output, and expose a machine-readable reasoning
trace.*

### Language

- Three declarations: `fact`, `rule`, `output`. Nothing else parses.
- Four types: `Int`, `Float`, `Bool`, `String`. Static, monomorphic, no implicit
  conversion of any kind.
- Facts are typed, ground and immutable, and carry their origin.
- Rules are pure implications that fire at most once, to a least fixed point.
- **Unknown is not false**: a rule that reads an underived name is pending, and
  an output nothing derived is `unknown`.
- Conflicting derivations are an error (`E4001`). There is no priority
  mechanism, deliberately.
- Determinism is total: ordered maps, source-order evaluation, no clock, no
  randomness, no non-finite floats.

### Implementation

- 13 crates from `diagnostics` to `cli`, with an enforced dependency graph.
- Structured diagnostics with stable codes, spans, related locations and help.
- A versioned, printable IR, identified by the SHA-256 of its canonical text.
- Structured traces, JSON output, and `why` — explanation reconstructed from the
  trace alone.
- The `lml` CLI: `check`, `run`, `trace`, `why`, `ast`, `ir`, `version`.
- 127 tests, zero third-party dependencies, no `unsafe`.

### Deliberately absent

`state`, `query`, SQL, mutation, functions, variables, unification, collections,
macros, concurrency, the IDE, packaging and licensing. Each is a later phase or
an open design decision in `docs/OPEN_DESIGN_DECISIONS.md`, and none of them is
stubbed.
