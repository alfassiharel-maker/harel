# Changelog

Every entry that changes what a program means says so. Syntax and semantics are
frozen within a language version; `0.x` may break, `1.x` may not.

## Unreleased — the three-state value model

**Owner decisions OD-1, OD-2, OD-3 and the O2 follow-ups, implemented.**
This changes what programs mean. IR version 1 → 2; trace schema 1 → 2.

### Language

- **`Unknown`, `Null` and `Known(v)` are three distinct conditions.** `Unknown`
  is absence of information; `null` is a known absence of a value. Neither is
  `false`, and no operator, output or serialisation collapses them.
- **`null` is a literal.** `unknown` is not — writing it would assert that a
  value is known to be not known. Both words were already reserved, so nothing
  broke.
- **New: `x is null`, `x is unknown`, `x is known`.** Total predicates: every
  value is in exactly one state, so they always answer and never fail.
- **`null` answers existence questions and refuses everything else.**
  `null == null` is `true`, `null == 5` is `false`; ordering (`E5004`),
  arithmetic (`E5005`) and boolean use (`E5006`) are structured errors. The
  full cell-by-cell table is `docs/VALUE_AND_LOGIC_TRUTH_TABLE.md`.
- **Rules have three outcomes, not two:** fire, do not fire, or **pending**. A
  rule whose condition is `unknown` is reconsidered rather than treated as
  false, and the trace records `RulePending` beside `ConditionEvaluated`.
- **A rule reading a `null` is evaluable** and may fire — `null` is knowledge.
  The old evaluability gate is gone; the condition decides.
- `Type::Null` unifies with every type, so one rule may derive `null` for a name
  another derives as an `Int`.

### Engine

- Conflicts carry all seven elements OD-3 requires — id, both rules, both
  conclusions, relevant facts, relevant conditions, round, trace references —
  as a `ConflictReport` and a `ConflictRaised` event.
- `trait ConflictStrategy` with the single default `RaiseConflict`: the seam for
  future explicit strategies, with nothing unused built on top of it.
- Traces carry `TraceMetadata` — execution id, start time, duration — **excluded
  from trace equality**, so Master Spec §10's `Timing` and deterministic
  comparison hold together.
- JSON values are tagged (`{"state":"Unknown"}`), never bare `null`. The CLI's
  duplicate encoder is gone; `lml_trace::json_value` is the only one.
- `Diagnostic` gained `cause`, which Master Spec §25 required.

### Repository

- The architecture lint now checks the declared dependency graph against
  `docs/05` §3, and a grammar test checks `grammar.ebnf` against `docs/02` §2.
  Both were claimed and not implemented before the design audit.
- All nine audit contradictions (C1–C7, G1, D1–D3) are resolved.

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
