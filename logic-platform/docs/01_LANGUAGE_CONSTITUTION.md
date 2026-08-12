# LANGUAGE CONSTITUTION

Status: **Foundational — binding**
Version: 0.1 (language version `0.1`)
Applies to: the whole `logic-platform/` tree
Supersedes: nothing
Superseded by: nothing

This document defines *what the language is*. It is the highest authority on
semantics. `02_FORMAL_SEMANTICS.md` states the same rules formally,
`03_EXECUTION_MODEL.md` states how they are executed, `04_TYPE_AND_DATA_MODEL.md`
defines the value domain, and `05_ARCHITECTURE.md` defines where each rule is
implemented. Where the Engineering Implementation Spec and this document
disagree, **this document decides semantics; the engineering spec decides how the
code is organised.**

> ⚠️ **PROVISIONAL — not approved language semantics.**
> A design audit (`docs/DESIGN_AUDIT.md`) found that most of the rules in this
> document were decided by the implementation, not by the specification. Read
> every rule here together with `docs/SEMANTIC_DECISION_REGISTER.md`, which
> classifies each one as EXPLICITLY_SPECIFIED, DERIVED, ASSUMED BY
> IMPLEMENTATION, or OPEN DESIGN DECISION. Anything marked ASSUMED or OPEN is a
> **proposal awaiting the owner's decision**, regardless of how this document
> phrases it. The word "decided" below means "decided in code", not "approved".

---

## 0. Naming

The product name is an **OPEN DESIGN DECISION** (Master Specification §54). It is
not chosen here.

Until it is resolved, the working codename is **LML** — *Logic Management
Language* — and source files use the extension `.lml`. This is not an invention:
the Engineering Implementation Spec §39 already names fixtures
`tests/runtime/basic_rule.lml`. Everything that depends on the name is confined
to two places (`crates/cli` command name, file extension constant), so renaming
is a mechanical change and not a semantic one.

---

## 1. What the language is

LML is a language in which **facts, rules, inference, state, data and the
program's own structure are first-class, explicit objects**. It is not a general
purpose language and does not try to become one.

A program is not a sequence of statements to run. A program is a **declaration of
what is known and what follows from it**. Executing a program means computing
everything that follows, deterministically, and recording *why* each conclusion
was reached.

Three properties are constitutional. They may not be traded away for
convenience, performance, or a feature:

1. **Determinism.** Same source + same input + same runtime version + same
   configuration ⇒ byte-identical result *and* byte-identical trace. Any
   deviation must be declared in this document. None is declared in 0.1.
2. **Explainability.** Every derived value is reachable from the trace back to
   the facts and rules that produced it. A result the runtime cannot explain is a
   defect, not a result.
3. **No invented answers.** Where the specification is silent, the
   implementation raises a structured error or refuses to compile. It never
   guesses a value. Absence of information is `unknown`, never `0`, never `false`,
   never `""`.

## 2. Scope of language version 0.1

Version 0.1 is the **vertical slice** required by Master Specification §56: a
small, formally defined language that expresses facts and rules, executes them
deterministically, derives new facts, produces output and emits a machine
readable reasoning trace.

The canonical 0.1 program is exactly the one in the Master Specification:

```lml
fact temperature = 31

rule heat:
    when temperature > 30
    then status = "hot"

output status
```

Result: `status = "hot"`, plus a trace showing that `heat` activated, that its
condition evaluated true, that `status` was derived by `heat`, and that `status`
was emitted as output.

**In 0.1 the language has exactly three top-level forms**: `fact`, `rule`,
`output`. Nothing else parses. This is deliberate — an unimplemented keyword that
parses is worse than one that does not exist.

## 3. The entities

### 3.1 Fact — *decided*

> A **fact** is a typed, ground, immutable binding of a **name** to a **value**,
> valid for the duration of one execution.

- **Typed**: every fact has exactly one type from §04, known statically.
- **Ground**: a fact contains no variables and no unevaluated expression. `fact
  a = 1 + 2` stores the value `3`; the expression is not retained as a fact.
- **Immutable**: within one execution a name is bound at most once. There is no
  assignment, no update, no retraction in 0.1.
- **Named, not relational**: a fact name is an atomic dotted identifier
  (`temperature`, `user.age`, `account.status`). The dot is part of the name and
  carries no structural meaning in 0.1 — `user.age` is *not* field access on
  `user`.

Facts have an **origin**, which is part of the fact and appears in the trace:

| Origin | Created by |
|---|---|
| `Declared` | a `fact` declaration in the source |
| `Derived { rule }` | the `then` clause of a rule that fired |

Rejected alternatives, and why:
- *Fact as mutable state* — destroys monotonicity, makes the fixed point
  order-dependent, and makes explanation ambiguous ("which value did rule R
  see?"). Mutability is deferred to the state model (§3.5), where it can be given
  explicit semantics instead of arriving by accident.
- *Fact as a relation / tuple* (Datalog style) — strictly more expressive and
  almost certainly the right 0.3 answer, but it requires unification, variable
  binding and a join strategy, none of which are specified yet. Adding it later
  is additive: a named fact is a nullary relation.

### 3.2 Rule — *decided*

> A **rule** is a named, pure **implication**: when a condition holds over the
> known facts, the named bindings in its `then` clauses hold too.

- A rule is **not** a procedure, **not** a trigger, **not** a constraint.
  It has no side effects, cannot perform I/O, and cannot fail partially.
- A rule fires **at most once per execution**. Facts are immutable, so a second
  firing could only re-derive what it already derived. (Formally: rule
  application is idempotent under a monotone fact set, so "fires once" and
  "fires until stable" are observationally identical. Firing once is chosen
  because it makes the trace finite and readable.)
- A rule's condition is evaluated only when **every** name it reads is known.
  A rule that reads an unknown name is *not* false — it is **not yet
  evaluable**, and is retried on the next inference round. This is the single
  most important rule in the language: **unknown is not false.**
- A rule that is never evaluable simply never fires. That is not an error. It is
  reported in the trace as `RuleNeverEvaluated` with the names it was waiting for.

### 3.3 Condition — *decided*

A condition is a total, side-effect-free expression over known facts and
literals, of type `Bool` (§04). It is checked statically: a condition whose type
is not `Bool` is a semantic error at compile time, never a runtime surprise.

### 3.4 Inference — *decided: forward chaining to a least fixed point*

Inference in 0.1 is **forward chaining**, iterated to a **least fixed point**,
over a **monotonically growing** fact set:

```
F₀ = declared facts
Fₙ₊₁ = Fₙ ∪ { derivations of every rule that is evaluable and true under Fₙ
              and has not yet fired }
result = the first Fₖ with Fₖ₊₁ = Fₖ
```

Rejected alternatives:
- *Backward chaining* — the natural fit for `query`, which does not exist in 0.1.
  It is the expected 0.2 addition and does not conflict with this model.
- *Search / backtracking* — requires non-ground facts and a choice point model.
  Neither is specified.

Because facts are immutable and the rule set is finite, the fixed point exists
and is unique, and the iteration terminates in at most *R* rounds for *R* rules.
A round limit still exists as a defensive constant (§03), not as semantics.

### 3.5 State — *OPEN DESIGN DECISION*

0.1 has **no mutable state**. The Master Specification (§11) requires that
`INPUT`, `FACT`, `DERIVED FACT`, `STATE` and `OUTPUT` be distinguished; 0.1
distinguishes the four it implements (input/declared facts, derived facts,
outputs) and **does not implement `STATE` at all**. There is no `state` keyword,
no partially-working state, and no reserved word pretending to be one.

Introducing mutation requires answering, in writing, before any code:
what a transition is, whether rules may cause one, how a rule that observed the
old value is explained, and how the fixed point is defined when the fact set is
no longer monotone. Until then: absent.

### 3.6 Query — *OPEN* (Phase 6, with SQL). Not in 0.1.
### 3.7 Decision — *not a separate entity in 0.1*. A decision is a derived fact that an `output` names.

### 3.8 Trace — *decided*

The trace is **part of the language's observable behaviour**, not a debugging
aid. Two runtimes that agree on outputs but disagree on traces are not both
correct. Trace content is specified in `03_EXECUTION_MODEL.md` §7 and is
versioned independently (`trace_version`).

## 4. Conflicts — *decided for 0.1: conflict is an error*

If two rules derive the same name with **equal** values, that is not a conflict;
the second derivation is redundant and is recorded as such.

If two rules derive the same name with **different** values, execution fails with
`E4001 ConflictingDerivation`, naming both rules, the name, and both values.

The Master Specification §23 lists priority, specificity, explicit ordering,
error, multi-result and custom strategies, and forbids choosing before a design
review.

> **WITHDRAWN.** This document previously claimed *"This **is** that review."*
> It was not: a design review is an act of the language's owner, and an agent
> cannot convene one over its own proposal. The behaviour below stands in code
> as a **proposal**, and the decision is OPEN
> (`docs/SEMANTIC_DECISION_REGISTER.md` §5).

The reasoning offered in support of the proposal:

- Any silent winner (priority, source order, specificity) makes the language's
  answer depend on something the author did not write down. That violates §1.3.
- `error` is the only option that is **forward-compatible with all the others**:
  every later strategy can be introduced as an explicit opt-in that turns a
  program which is an error today into one with defined behaviour, and no
  existing program changes meaning.
- Multi-result requires a fact to hold a set of values, which contradicts §3.1.

So: error now, `priority` as the first candidate extension, opt-in, in 0.2.

## 5. Cycles — *decided*

A cyclic dependency between rules is **not an error** in 0.1 and does not
require detection, because monotone forward chaining over immutable facts
terminates regardless. `A → B → C → A` simply reaches a fixed point.

What *is* forbidden is unbounded execution. The runtime enforces
`MAX_INFERENCE_ROUNDS` (§03) and fails with a structured error rather than
looping. That constant is a safety net against implementation defects, not a
semantic device: a correct implementation never reaches it.

Cycle *reporting* — showing the author that their rule graph has a cycle — is a
Phase 8 analysis feature, not runtime behaviour.

## 6. Determinism — *decided, and enforced*

Guaranteed by construction:

- Facts are stored in an ordered map keyed by name. `HashMap` iteration order is
  forbidden in every crate that can influence a result or a trace.
- Rules are evaluated in **source order** within each round. The round structure
  and the order are both visible in the trace.
- No clock, no randomness, no thread scheduling, no environment lookup takes part
  in evaluation. The trace carries no wall-clock timestamps; it carries a
  monotonic **event sequence number**. (Timing belongs to metrics, §61 of the
  engineering spec, not to the trace.)
- Floating point: IEEE-754 binary64, no fast-math, no reassociation, no `NaN`
  literal. `NaN` can only arise from `0.0/0.0`, which is a runtime error instead
  (§04).

## 7. Errors — *decided*

Every error carries a **stable code**, a **source location**, a **message**, and
where applicable a **cause** and a **suggestion**. Codes are grouped by phase and
are never reused for a different meaning:

| Range | Phase |
|---|---|
| `E1xxx` | lexical |
| `E2xxx` | syntax |
| `E3xxx` | semantic and type |
| `E4xxx` | logic / inference |
| `E5xxx` | runtime |
| `E6xxx` | data / SQL (reserved, unused in 0.1) |
| `E7xxx` | security (reserved, unused in 0.1) |
| `E8xxx` | resource limits |

Reserved ranges are documented but unallocated. An unallocated code is never
emitted.

## 8. What 0.1 deliberately does not have

`state`, `query`, SQL, mutation, retraction, functions, variables, unification,
lists, maps, records, modules, imports, macros, metaprogramming, concurrency,
I/O of any kind, an IDE, an installer, licensing.

Each is either an explicitly deferred phase in the Master Specification or an
OPEN DESIGN DECISION. None of them is stubbed, keyword-reserved-and-broken, or
half-parsed. **The absence is the specification.**

## 9. Amendment

A change to this document is a change to the language. It requires, in order:
1. an entry in `docs/OPEN_DESIGN_DECISIONS.md` moving from OPEN to DECIDED, with
   the rejected alternatives and the reason;
2. an update to `02_FORMAL_SEMANTICS.md` and, if execution changes,
   `03_EXECUTION_MODEL.md`;
3. tests that fail before the code change and pass after it;
4. only then, the implementation;
5. a `CHANGELOG.md` entry.

Syntax and semantics are frozen within a language version. `0.x` may break;
`1.x` may not.
