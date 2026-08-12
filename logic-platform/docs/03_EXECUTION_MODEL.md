# EXECUTION MODEL — LML 0.1

Status: **Binding**
Companion to: `02_FORMAL_SEMANTICS.md`

The formal semantics say *what* an execution computes. This document says *how*
the implementation computes it, what the intermediate representation is, what the
runtime owns, and what the trace contains.

> ⚠️ **PROVISIONAL — not approved language semantics.**
> A design audit (`docs/DESIGN_AUDIT.md`) found that most of the rules in this
> document were decided by the implementation, not by the specification. Read
> every rule here together with `docs/SEMANTIC_DECISION_REGISTER.md`, which
> classifies each one as EXPLICITLY_SPECIFIED, DERIVED, ASSUMED BY
> IMPLEMENTATION, or OPEN DESIGN DECISION. Anything marked ASSUMED or OPEN is a
> **proposal awaiting the owner's decision**, regardless of how this document
> phrases it. The word "decided" below means "decided in code", not "approved".

---

## 1. The pipeline

```
source text
   │  crates/lexer          — text → tokens
   ▼
tokens
   │  crates/parser         — tokens → AST                (crates/ast)
   ▼
AST
   │  crates/semantic       — AST → SemanticModel         (crates/types)
   ▼
SemanticModel
   │  crates/compiler       — lowering
   ▼
IR                          (crates/ir)
   │  crates/runtime        — execution
   ▼
ExecutionResult + Trace     (crates/logic, crates/reasoning, crates/trace)
```

The flow is one-directional. `crates/compiler` orchestrates the first four steps
and owns lowering; it holds no analysis logic of its own beyond that.

Each arrow is a **total function that either succeeds or returns diagnostics**.
No stage panics on malformed input. No stage mutates its input.

## 2. Why an IR at all

A tree-walking interpreter over the AST would produce the same answers for 0.1.
The IR exists because of three requirements that are not 0.1-local:

1. **The IR is the contract with the runtime** (Master Spec §20). Syntax may
   change without the runtime changing.
2. **Inspectability.** `lml ir program.lml` prints it; it is a stable, versioned,
   textual artifact that snapshot tests can diff. Semantic drift becomes a
   visible diff instead of a behaviour change. (Engineering Spec §40.)
3. **Analysis.** Rule dependency analysis, conflict detection and (later) query
   planning operate on the IR, not on syntax.

The cost is one lowering pass and one extra crate. That is the justification;
if it stopped being true the IR would be removed rather than kept for form.

## 3. IR design

`IrProgram` is flat, explicit and free of source syntax:

```
IrProgram {
    ir_version:  u32,                  // bumped on any IR shape change
    names:       Vec<NameEntry>,       // interned; NameId is an index
    types:       Vec<Type>,            // one per name, indexed by NameId
    facts:       Vec<IrFact>,          // { name: NameId, value: Value, span }
    rules:       Vec<IrRule>,
    outputs:     Vec<NameId>,
}

IrRule {
    id:          RuleId,               // index; also the source order
    name:        String,
    condition:   ExprCode,
    effects:     Vec<IrEffect>,        // { name: NameId, code: ExprCode, span }
    reads:       Vec<NameId>,          // sorted, deduped — condition ∪ effects
    writes:      Vec<NameId>,
}
```

Names are **interned** to `NameId`. Every later stage compares small integers
instead of strings, and — more importantly — the runtime cannot invent a name
that was not statically known.

Expressions are lowered to `ExprCode`: a `Vec<Op>` in **post-order**, executed on
a small evaluation stack.

```
Op = Const(Value) | Load(NameId)
   | Neg | Not
   | Add | Sub | Mul | Div | Rem
   | Eq | Ne | Lt | Le | Gt | Ge
   | And | Or
```

`Const` carries a `Value`, not a literal: the four types are already the value
domain, and a separate constant pool would be a second place for a value to
exist and therefore a second place to get it wrong.

Post-order was chosen over a tree because it is trivially serialisable, has one
obvious execution rule, and makes stack depth statically computable — which is
how the engine bounds `MAX_EXPR_STACK` without a runtime check in the hot path.

`And` and `Or` are single ops with both operands already evaluated. This is not
an optimisation oversight: §4.1 of the formal semantics *requires* that both
sides be evaluated so that `NotEvaluable` propagates regardless of order.

Every op is total with respect to types because the semantic pass already
type-checked it. The evaluator still returns a structured error rather than
panicking if it meets an impossible state — `E5099 InternalInvariant` — because a
panic in a runtime is a denial of service, not a diagnostic.

## 4. State ownership

Exactly one owner (Engineering Spec §19):

```
Runtime
  └── ExecutionContext
        ├── FactSet          (crates/logic)      name → (Value, Origin)
        ├── RuleState        (crates/reasoning)  fired / not fired, per rule
        ├── Limits           (crates/runtime)    the constants below
        └── TraceRecorder    (crates/trace)      append-only event log
```

There is no global mutable state anywhere in the workspace, no `static mut`, no
lazily-initialised singleton, and no thread-local. Two executions in one process
share nothing, which is what makes the engine embeddable and testable.

`FactSet` is a `BTreeMap<NameId, ...>`: ordered iteration is a semantic
requirement (§Const 6), not a preference.

## 5. Limits — named constants, never literals

Each limit lives with the stage that enforces it, so that no early stage has to
depend on the runtime. `Limits` (in `crates/runtime`) carries the ones execution
enforces and is passed in by the embedder; there is no configuration file yet,
and `configs/` is reserved rather than pretending to be read.

| Constant | Value | Where | Chosen against |
|---|---|---|---|
| `MAX_INFERENCE_ROUNDS` | 10_000 | `runtime::limits` | Semantics bound rounds by rule count; this is a defect backstop. 10k rounds means ≥10k rules or an engine bug. |
| `MAX_EXPR_STACK` | 256 | `ir::code` | Checked once when `ExprCode` is built, so the evaluator needs no per-operation check. |
| `MAX_EXPR_DEPTH` | 128 | `parser` | Recursion guard. Prevents a stack overflow — which is a crash, not an error — on adversarial input like 100k nested parens. **A security boundary, not a style limit.** |
| `MAX_TYPE_DEPENDENCY_DEPTH` | 512 | `semantic` | Same reason, for the recursion in type inference. |
| `MAX_SOURCE_BYTES` | 16 MiB | `lexer` | Bounds lexer memory on untrusted input. |
| `MAX_NAME_LENGTH` | 255 | `ast` | Bounds diagnostic rendering and interning. |

Exceeding any of them is an `E8xxx` structured error naming the constant and its
value. None of these numbers appears as a literal anywhere else in the tree.

## 6. Execution algorithm

```
run(ir, limits) -> ExecutionResult:
    ctx ← ExecutionContext::new(limits)
    trace.execution_started(ir.ir_version, ir.digest)

    for f in ir.facts:                       # round 0
        ctx.facts.insert(f.name, f.value, Origin::Declared)
        trace.fact_declared(f.name, f.value)

    for round in 1..=limits.max_inference_rounds:
        trace.round_started(round)
        fired_any ← false
        for rule in ir.rules:                # source order
            if ctx.rule_state.has_fired(rule.id): continue
            if not ctx.facts.knows_all(rule.reads):
                trace.rule_not_evaluable(rule.id, missing_names)
                continue
            match eval(rule.condition, ctx.facts):
                NotEvaluable  ⇒ unreachable — reads were checked (E5099 if hit)
                False         ⇒ trace.rule_condition_false(rule.id)
                True          ⇒ trace.rule_activated(rule.id)
                                 for effect in rule.effects: apply
                                 mark fired; fired_any ← true
        trace.round_finished(round, fired_any)
        if not fired_any: break
    else:
        return Err(E8001 InferenceRoundLimit)

    for name in ir.outputs:
        emit(name, ctx.facts.get(name))      # Known | Unknown
    trace.execution_finished(...)
```

Note what is *not* here: no timing, no allocation of a thread, no I/O, no
environment access. The runtime is a pure function of `(IrProgram, Limits)`.

## 6a. Conflicts — OD-3

**OD-3 (2026-08-12) — APPROVED.** When two rules reach incompatible conclusions
for one name, execution enters an explicit `Conflict` condition. The runtime
must report:

```
Conflict ID
Conflicting Rules
Conflicting Conclusions
Relevant Facts
Relevant Conditions
Execution Context
Trace References
```

**Implemented today:** conflicting rules, conflicting conclusions, and a source
span, inside an `E4001` diagnostic. **Missing:** conflict id, relevant facts,
relevant conditions, execution context, trace references — five of seven.

The core must never resolve a conflict implicitly. Future strategies (priority,
specificity, first-match, all-results, custom) must be explicit language
constructs or configuration, and must not silently change the meaning of an
existing program. No such mechanism exists yet; where a strategy would be
*selected* is **OPEN DESIGN DECISION O3.5**, and whether a conflict is fatal is
**O3.1**.

## 7. Trace

The trace is a **structured event log**, not logging (Engineering Spec §21).
Log levels never appear in it; it never carries free text meant for a human to
read instead of a machine.

```
TraceEvent { seq: u64, kind: TraceEventKind }
```

`seq` starts at 0 and increments by 1. There is **no timestamp** — a timestamp
would make the trace non-deterministic and therefore not comparable, which is the
whole point of having it. Timings are metrics, emitted separately.

Event kinds in 0.1:

| Kind | Payload |
|---|---|
| `ExecutionStarted` | `ir_version`, `program_digest`, `language_version` |
| `FactDeclared` | `name`, `value` |
| `RoundStarted` | `round` |
| `RuleNotEvaluable` | `rule`, `missing: [name]` |
| `ConditionEvaluated` | `rule`, `result: bool` |
| `RuleActivated` | `rule`, `reads` — carried so an explanation needs only the trace |
| `FactDerived` | `name`, `value`, `rule` |
| `DerivationRedundant` | `name`, `value`, `rule` (same value already held) |
| `RoundFinished` | `round`, `fired_any` |
| `OutputEmitted` | `name`, `Known(value)` or `Unknown` |
| `ExecutionFinished` | `rounds`, `facts_total`, `rules_fired` |
| `ErrorRaised` | `code`, `message`, `span` |

A `Conflict` event carrying the seven elements of §6a does not exist yet; today
a conflict appears only as `ErrorRaised E4001`, which cannot carry them. Adding
it is required by OD-3 and is blocked only on the Conflict ID's form (O3.4).

`program_digest` is a SHA-256 over the canonical IR text. Two runs of the same
program produce the same digest, so a trace can be checked against the program it
claims to describe. (The digest implementation is our own SHA-256 in
`crates/trace/src/digest.rs` — 120 lines of standard-library code, chosen over a
dependency because it is the only hashing we need and the whole workspace being
dependency-free is a testable property, see Engineering Spec §30.)

### 7.1 Explanation

`why(name)` reconstructs an explanation *from the trace only*, never by
re-running: find the `FactDerived` for `name`, take its rule, take that rule's
reads, recurse. The result is a tree ending in `FactDeclared` leaves. If the
trace cannot support the explanation, `why` returns `None` — it does not
approximate.

## 8. Error handling and panics

- `unwrap`, `expect` and `panic!` are forbidden in all library crates on any
  path reachable from input. `#![deny(clippy::unwrap_used, clippy::expect_used,
  clippy::panic)]` is set crate-wide, so this is enforced by CI, not by review.
- Tests may use them freely (`#[cfg(test)]` is exempt).
- Arithmetic uses `checked_*`; `#![deny(clippy::arithmetic_side_effects)]` in the
  evaluator makes an accidental `a + b` a compile error.
- There is no `unsafe` in the workspace. `#![forbid(unsafe_code)]` is in every
  crate root. Adding any would require the justification of Engineering Spec §29.

## 9. Concurrency

None. Execution is single-threaded (Master Spec §21, Engineering Spec §59:
correctness, then measurement, then parallelism). The design does not preclude
it — `ExecutionContext` is owned, not shared, and the round structure is a
natural parallel boundary — but nothing will be parallelised before a benchmark
proves it necessary.

## 10. Performance

`benchmarks/` measures parse time, semantic time, lowering time, execution time
and trace overhead. No optimisation is made without a before/after number and a
regression test. There is currently no optimisation in the tree: 0.1 is a
correctness milestone.
