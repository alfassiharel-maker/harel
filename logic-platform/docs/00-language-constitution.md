# Language Constitution
Logic Management, Reasoning & Code Intelligence Language

## Foundational decisions (owner-level, §5)

| # | Decision | Status |
|---|----------|--------|
| 5.1 | Logic, rules and reasoning are first-class concepts | SPECIFIED |
| 5.2 | Facts are immutable — no in-place mutation | SPECIFIED + IMPLEMENTED |
| 5.3 | Unknown / Null / Known are distinct value states | SPECIFIED + IMPLEMENTED |
| 5.4 | Rule conflicts raise an explicit error — no hidden priority | SPECIFIED + IMPLEMENTED (infrastructure) |
| 5.5 | Production core is Rust | IMPLEMENTED |
| 5.6 | UI is outside the core | IMPLEMENTED (CLI only; no UI dep in core) |
| 5.7 | SQL is behind an abstraction | SPECIFIED (Phase 1; not yet built) |
| 5.8 | Correct, maintainable, verified > demo speed | ENFORCED |

## Language syntax (autonomous decision, §41)

Extension: `.lgc`

```
# Fact declaration — all arguments must be ground constants
fact relation_name(arg, ...);

# Rule declaration — variables are uppercase identifiers
rule head_relation(X, Y) when body1(X, Z), body2(Z, Y);

# Query — variables become output columns
query relation_name(X, Y);
```

Value literals: `"string"`, `42`, `3.14`, `true`, `false`, `null`, `unknown`  
Variables: `UpperCaseIdentifier`  
Relation names: `lowercaseidentifier`  
Comments: `# to end of line`

## Value model (§6, §7)

```
Value
├── Unknown   — system lacks information
├── Null      — system knows no value exists
└── Known(v)  — concrete value is known
    ├── Int(i64)
    ├── Float(f64)
    ├── Bool(bool)
    └── Str(String)
```

- `Null == Null` → Equal
- `Null == Known(x)` → NotEqual
- `Unknown` in any equality → Indeterminate
- Null in arithmetic or ordering → SemanticTypeError (LGC0401 / LGC0402)

## Inference model (§14)

Naive bottom-up fixed-point evaluation (deterministic, repeatable, traceable).

1. Assert all base facts into the fact store
2. For each rule, enumerate all substitutions that satisfy the body
3. For each satisfying substitution, derive the ground head and insert
4. Repeat until no new facts are derived (fixed point)

Termination guaranteed for positive Datalog (no negation, no aggregation) over finite input.

## Trace semantics (§17, §18)

Trace events are ordered by sequence number (logical ordering, not wall clock).  
Events include: ExecutionStarted, FactDeclared, RuleActivated, FactDerived,  
FactAlreadyKnown, QueryExecuted, QueryResult, ConflictRaised, ExecutionFinished.

## Conflict model (§19)

Default: RaiseConflict — no hidden priorities.  
Infrastructure: `ConflictError` with conflict ID, conflicting rules, conclusions.  
Current phase: conflict detection for attribute-valued facts (future work per §15).

## Diagnostic codes

| Range  | Category |
|--------|----------|
| LGC01xx | Parse errors |
| LGC02xx | Semantic errors |
| LGC03xx | Logic / runtime errors |
| LGC04xx | Type-system errors |
| LGC05xx | Internal errors |

## Component status

| Component | SPECIFIED | IMPLEMENTED | VERIFIED |
|-----------|-----------|-------------|---------|
| Value model (§6) | yes | yes | yes (6 tests) |
| Lexer | yes | yes | yes (6 tests) |
| AST | yes | yes | yes (structure) |
| Parser | yes | yes | yes (6 tests) |
| Fact store | yes | yes | yes (4 tests) |
| Unification (§12) | yes | yes | yes (8 tests) |
| Fixed-point inference (§14) | yes | yes | yes (4 tests) |
| Relational provenance (§9) | yes | yes | yes (end-to-end) |
| Structured trace (§17) | yes | yes | yes (2 tests) |
| Runtime / executor | yes | yes | yes (3 tests) |
| CLI (`lgc run / check`) | yes | yes | yes (manual) |
| SQL integration (§21) | yes | no | no |
| Conflict detection (§19) | yes | partial | no |
| IDE (§35) | no | no | no |
| Security (§34) | no | no | no |

**Acceptance test (§52):**  
`grandparent(X, Z) when parent(X, Y), parent(Y, Z)` derives  
`grandparent("Alice", "Charlie")` with provenance — VERIFIED ✓

## Next milestone (§51 remaining items)

- Recursive rules (ancestor)
- Negation-as-failure (marked as Phase 0.4 — do not introduce yet, §15)
- Semi-naïve evaluation (performance optimisation — profile first, §32)
- SQL adapter (Phase 1)
- Conflict detection for value-producing rules
- Security sandbox (Phase 3)
