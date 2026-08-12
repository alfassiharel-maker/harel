# DESIGN AUDIT — LML 0.1

Date of audit: 2026-08-12
Scope: the whole `logic-platform/` tree, at commit `b048d74`
Trigger: a design audit ordered before any further subsystem is implemented
Outcome: **implementation halted; no code changed by this audit**

---

## 0. The finding that matters

The three decisions named in the audit order — `Unknown != false`, `E4001` for
conflicting derivations, and byte-identical traces as a correctness criterion —
are **not specified anywhere in the Master Specification**. I made them, and
then wrote them into documents marked *Binding*, which made them read as
approved language semantics.

Worse than making them was the way `docs/01_LANGUAGE_CONSTITUTION.md` §4 handled
the conflict rule. The Master Specification §23 says a conflict strategy may not
be chosen *"before Design Review"*. My document said:

> "This *is* that review."

That sentence is withdrawn. A design review is an act of the owner of the
language; an agent cannot convene one over itself and grant its own proposal.

The audit found the problem is broader than the three named decisions:
**31 assumptions and 13 reserved decisions** are embedded in tested, working
code (`docs/SEMANTIC_DECISION_REGISTER.md`). The engine is not wrong — the tests
show it does what the documents say — but the documents say things nobody
approved.

---

## 1. What is formally decided

Traceable to the Master Specification, quoted in the register:

| Decision | Source |
|---|---|
| A fact binds a name to a value | §7 |
| A rule relates a condition to a consequence, in `when`/`then` shape | §8, §56 |
| `output <name>` emits a named result | §56 |
| The language is typed | §17 |
| Execution produces a structured, machine-readable trace | §10, §51 |
| The trace must retain enough to reconstruct the reasoning path | §9 |
| Errors carry code, location, message, context, suggested resolution | §25 |
| Same source + input + data + state + version + config ⇒ same **result** | §22 |
| The canonical program yields `status = "hot"` with the trace §56 sketches | §56 |
| The pipeline: source → lexer → parser → AST → semantic → IR → runtime | §18, §26 |
| Rust for the production core; the crate layout of Engineering Spec §7 | Eng §5, §7 |

Eight semantic entries. Everything else in the language came from somewhere
else.

## 2. What is implemented

All of §1, plus the vertical slice of §56: lexing, parsing, static analysis,
type inference, lowering to a versioned IR, forward-chaining inference, output,
trace and explanation, behind a CLI. 13 crates, no third-party dependencies, no
`unsafe`, no `unwrap`/`expect`/`panic` in library code.

## 3. What is verified

127 tests pass: unit tests beside the code, integration tests per crate against
public APIs only, and 11 language fixtures with output and trace snapshots.
`cargo fmt --check`, `clippy -D warnings` and the architecture lint pass.

**What "verified" does and does not mean here.** It means: the implementation
does what `docs/01`–`docs/05` say, and a change that breaks that fails the
build. It does **not** mean the behaviour is approved, and `docs/STATUS.md`
should not have implied otherwise by marking rows VERIFIED without noting that
the specification they were verified against is largely self-authored. Treat
every VERIFIED row as *"verified against a provisional specification"* until the
register is reviewed.

## 4. What was assumed

Full detail in `docs/SEMANTIC_DECISION_REGISTER.md`. The load-bearing ones,
ordered by how much would have to change if the decision goes the other way:

| # | Assumption | If reversed |
|---|---|---|
| 1 | **Facts are immutable** | The inference engine is redesigned, not patched: monotonicity, the termination proof, "fires at most once", and the conflict rule all depend on it. |
| 2 | **`Unknown` is not `false`** | Rule firing changes, so program results change. `Evaluated::NotEvaluable`, the `knows_all` gate and the non-short-circuiting operators all exist to serve it. |
| 3 | **`and`/`or` do not short-circuit over `Unknown`** | Follows from 2. The alternative (Kleene three-valued logic, as SQL uses) is at least as defensible and was not presented before it was implemented. |
| 4 | **`Unknown` is not a value; there is no `Null`** | §17 lists `Null` as a candidate primitive. Restoring it changes `Value`, the operator table, the fact set and outputs. |
| 5 | **A `fact`'s value must be constant (`E3006`)** | Pure invention. Rejects programs the specification never forbade; the alternative (declarations participate in the fixed point) was never written down. |
| 6 | **No implicit `Int`/`Float` conversion; cross-type equality is a type error** | Rejects programs many users expect to work. |
| 7 | **Arithmetic failures are errors, not `unknown`** | A language that returns `unknown` for `1/0` is coherent and different. |
| 8 | **The trace is a correctness criterion** | Raises the cost of every future engine change: any reordering that preserves results still breaks conformance. |
| 9 | **Rules fire at most once** | Observationally inert *only while* assumption 1 holds. |
| 10 | **Static, monomorphic, inferred types with four members** | §17 asked for a ruling on which entities are language-level types; I supplied one. |

## 5. What is contradictory

Found by re-reading the five documents against each other and against the code.
**Per the audit order, these are reported and not fixed.** Proposed corrections
are given so the decision is a yes/no, not a design task.

**C1 — The trace omits fields the specification lists.**
Master Spec §10's trace model includes **Execution ID** and **Timing**. Neither
exists: `docs/03` §7 states *"There is no timestamp — a timestamp would make the
trace non-deterministic"*, which overrides an explicit requirement with an
unapproved one. `Initial State` and `Intermediate Results` are also absent.
(`State Changes` and `SQL Queries` are legitimately absent — neither subsystem
exists.)
*Proposed correction:* carry an execution id and a timing side-channel keyed by
`seq`, excluded from trace equality. Both requirements are satisfiable at once;
I did not try.

**C2 — `docs/01` §3.5 contradicts `docs/02` §1.4 and the lexer.**
§3.5: *"There is no `state` keyword … and no reserved word pretending to be
one."* §1.4 reserves `state` and 18 other words, and `crates/lexer` rejects them
with `E1007`.
*Proposed correction:* delete the clause in §3.5; the reservation is the more
defensible half, but it is a language-vocabulary decision and belongs in the
register (it is there, §17).

**C3 — `docs/03` §6 names a trace event that does not exist.**
The pseudocode calls `trace.rule_condition_false(rule.id)`; the implementation
and §7's own table have `ConditionEvaluated { rule, result }` for both outcomes.
*Proposed correction:* fix the pseudocode.

**C4 — `docs/05` §2 places `Value` in the wrong crate.**
The table says `logic` owns *"`Value`, `FactSet`, expression evaluation"*.
`Value` lives in `crates/types` (`src/value.rs`). The dependency graph in §3 was
updated for this; the responsibility table was not.
*Proposed correction:* move the `Value` entry to the `types` row — **and decide
whether that placement is right**, since it makes `types` own both the type
lattice and the value representation, which is two responsibilities in a crate
whose stated rule is one.

**C5 — `docs/05` §3 overstates what the architecture lint does.**
Invariant 4 claims *"the script checks the intended edges too"*. It does not: it
checks eight prohibitions, not the declared graph.
*Proposed correction:* either weaken the claim or implement the check. The
second is ~20 lines and is the better answer.

**C6 — `docs/05` §5 claims IR snapshots that the harness does not support.**
It advertises `.expected-ir` fixtures; `tests/e2e` reads only `.expected` and
`.expected-trace`. (An IR snapshot does exist, but inside
`crates/compiler/tests/compile.rs`.)

**C7 — `crates/parser/grammar.ebnf` claims a test that does not exist.**
Its header says *"tests/grammar.rs checks that this file and that section
agree"*. There is no such file, and nothing checks the grammar file against
`docs/02` §2. The two agree today by inspection only.

**G1 — a specified error field is missing.**
Master Spec §25 requires each error to carry **Cause** where possible.
`Diagnostic` has code, span, message, labels (context) and help (suggested
resolution) — no `cause`.

## 6. Architecture review

Checked against the audit's list, by reading the code rather than the docs.

| Requirement | Verdict |
|---|---|
| Lexer performs only lexical analysis | **Holds, with one note.** It also rejects 19 reserved words (`E1007`). That is a language-vocabulary policy expressed lexically — defensible, but it is policy, and it is registered. |
| Parser only constructs syntax | **Holds.** It also enforces `MAX_NAME_LENGTH`; a resource bound, not a type or name rule. |
| AST only represents syntax | **Holds.** Data plus a canonical printer; no evaluation, no validation, no I/O. |
| Semantic layer performs semantic validation | **Holds.** All seven name rules and five type rules; no lowering, no execution. |
| Type system performs type logic | **Drifted.** `crates/types` also owns `Value` — the runtime data representation — so it holds both "what type is this?" and "what is a value?". See C4. Not a layering violation (nothing evaluates there), but it is two responsibilities. |
| IR represents execution form | **Holds.** Flat, versioned, position-free, printable. |
| Logic engine manages logic objects | **Holds.** `FactSet`, `Origin`, evaluation. No scheduling. |
| Reasoning engine manages inference | **Holds.** Fixed point, rule state, conflict detection. It also writes trace events, which is declared and justified in `docs/05` §3. |
| Runtime orchestrates execution | **Holds.** Owns `ExecutionContext` and `Limits`; no parsing, no lowering. |
| Trace records structured execution events | **Holds.** No levels, no free-text logging. |
| CLI remains an interface | **Drifted.** `crates/cli/src/run.rs` contains its own JSON encoding of a `Value`, duplicating `crates/trace/src/json.rs`. Two places now define how a value is written as JSON, and they can drift. That encoding is arguably part of the external contract, not of the CLI. |
| No core crate depends on UI | **Holds.** Enforced by lint rule 2. |
| No logic crate depends on a database implementation | **Holds.** Enforced by lint rule 3. |

## 7. Dependency graph verification

Extracted from the manifests and from the imports, then compared with
`docs/05` §3. **Three discrepancies; reported, not corrected.**

| # | Discrepancy | Detail |
|---|---|---|
| D1 | `lml-reasoning` declares `lml-types`, never uses it | Unused edge in a manifest the documentation treats as the source of truth. |
| D2 | `lml-trace` declares `lml-diagnostics`, never uses it | Same. Also means the documented `trace → diagnostics` edge is fictional. |
| D3 | `lml-runtime` declares `lml-types`, never uses it | Same. |

Two further points where the documented graph is incomplete rather than wrong:

- **Dev-dependency edges are undocumented.** `lml-semantic` dev-depends on
  `lml-parser`, and `lml-runtime` dev-depends on `lml-compiler`. Both are
  test-only, neither creates a cycle, and `docs/05` §3 does not mention that the
  graph it draws is the non-test graph.
- **The lint does not verify the graph** (C5), so these three unused edges
  survived a lint that the documentation claims would have caught them.

Everything else matches: `diagnostics` is a leaf; nothing depends on `cli`;
`logic` does not depend on `reasoning`; the early stages do not depend on the
late ones; no cycles.

## 8. Consistency of the five design documents

Mutual contradictions are C2 (01 vs 02), C3, C4, C5, C6 (internal to 03 and 05)
in §5 above. Beyond those, one systemic problem:

**The documents present provisional decisions in the register of settled law.**
`docs/01` is headed *"Status: Foundational — binding"*, `docs/02`–`docs/05`
*"Binding"*, and each states its decisions with rationale and rejected
alternatives — the form of a ruling, not of a proposal. The rationale is
genuine; the authority is not.

This audit adds a status banner to all five documents pointing at the register.
That is the only change made to them, and it changes no rule.

One further gap, not a contradiction: `docs/01` §3.1 says a fact is *"ground"*
and shows `fact a = 1 + 2` storing `3`. It does not say a fact may not read
another name — that rule (`E3006`) appears only in `docs/02` §3.1 N7, added
during implementation. The constitution and the semantics do not disagree; the
constitution is silent where its own summary implies completeness.

## 9. Which implementation pieces depend on open semantics

For each open or high-impact assumed decision, where it actually lives, and what
changing it would cost. This is the map the audit brief asks for in place of the
refactoring it forbids.

| Open semantic | Where it is decided in code | Cost of changing it |
|---|---|---|
| Conflict = error (`E4001`) | `crates/reasoning/src/lib.rs`, `activate()`, the `Insertion::Conflict` arm — **one match arm** | **Low.** `FactSet::insert` already *reports* a conflict rather than resolving it; the resolution is a single site. Priority, specificity or last-wins would slot in here. This seam is already correct. |
| `Unknown != false` | `crates/logic/src/eval.rs` (`Evaluated::NotEvaluable`, the `Op::Load` arm, `And`/`Or`), plus the `knows_all(reads)` gate in `reasoning` | **High.** Two crates, and every fixture's expected output. |
| `and`/`or` non-short-circuit | `crates/logic/src/eval.rs` `apply()` — the `And`/`Or` arms — plus the "both operands evaluated" property baked into `ExprCode` lowering | **Medium.** Kleene semantics need no new op, but the IR comment that both operands are always evaluated becomes wrong. |
| Fact immutability | `crates/logic/src/facts.rs` (`insert` never overwrites), `reasoning` (`fired` state), the termination argument | **Very high.** This is the assumption the engine is built on. |
| Fixed point / forward chaining | `crates/reasoning/src/lib.rs`, `infer()` — the whole file, 200 lines | **Medium.** Deliberately isolated: `lml-logic` (representation) has no dependency on `lml-reasoning` (strategy), so a second strategy is additive. This separation was the right call and survives the audit. |
| Rules fire at most once | `RuleState` in `crates/reasoning/src/lib.rs` | **Low** while facts are immutable. |
| Trace = correctness | `tests/e2e/tests/fixtures.rs` (trace snapshots, re-run comparison), `crates/runtime/tests/execute.rs` | **Low to reverse** (delete assertions), **high to have relied on** — every future engine change is constrained by it until it is decided. |
| No timestamps in the trace (C1) | `crates/trace/src/event.rs`, `trace.rs` | **Low.** Additive: a timing side-channel keyed by `seq` does not disturb event equality. |
| Fact values must be constant (`E3006`) | `crates/semantic/src/check.rs` `check_facts_are_constant`, and constant folding in `crates/compiler/src/lower.rs` | **Medium.** Removing the check requires declarations to enter the fixed point. |
| Four types / no `Null` | `crates/types` (`Type`, `Value`), the operator table, `crates/logic/src/eval.rs` | **High.** Adding `Null` touches every match on `Value`. |
| The concrete grammar | `crates/lexer`, `crates/parser`, `grammar.ebnf` | **Medium and bounded.** The IR is the runtime's contract, so a syntax change stops at the compiler. This is exactly what the IR was for, and it is the audit's main piece of good news. |

## 10. Which pieces are safe to continue developing

Safe — no dependence on any open semantic decision:

- `crates/diagnostics` — spans, codes, rendering. (Adding a `cause` field for G1 is a specified requirement.)
- `crates/trace` — the JSON form, the digest, and the C1 additions (execution id, timing side-channel).
- The architecture lint (C5) and a grammar-agreement test (C7): both close gaps between what the docs claim and what is checked.
- The test harness itself: `.expected-ir` support (C6), more fixtures for *already specified* behaviour.
- `benchmarks/` — measurement changes no meaning.
- Removing the three unused dependency edges (D1–D3), once acknowledged.

Not safe until the register is reviewed:

- Anything in `crates/logic`, `crates/reasoning`, `crates/semantic` that changes what a program computes.
- Any new language feature — every one of them would rest on the same unapproved foundation.
- SQL, state, queries, the IDE, security, packaging, licensing, AI integration. These were already out of scope, and the audit adds a reason: SQL in particular must not be designed against a fact model whose shape is an open decision.

## 11. What this audit changed

Documentation only. No `.rs` file, no manifest, no fixture, no test was touched;
`make verify` passes exactly as before, with the same 127 tests.

- **Added** `docs/SEMANTIC_DECISION_REGISTER.md` — every semantic decision, classified.
- **Added** this file.
- **Added** a status banner to `docs/01`–`docs/05` pointing at the register, so no reader takes a provisional rule for an approved one.
- **Changed** the heading in `docs/OPEN_DESIGN_DECISIONS.md` from *"Decided for 0.1"* to *"Proposed for 0.1 — NOT APPROVED"*, and withdrew the *"This is that review"* claim.

C1–C7, G1 and D1–D3 are **reported and left in place**, per the audit order.

## 12. What is needed from the owner

The register is the agenda. In priority order, because later decisions depend on
earlier ones:

1. **Fact mutability** (register §1). Everything else rests on it.
2. **`Unknown` semantics** (§3, §4): pending-vs-false, and whether `Null` is a value. This settles the `and`/`or` question with it.
3. **Conflict resolution** (§5): the §23 design review that has not happened. My proposal is `error`, on the grounds that it is the only option forward-compatible with all the others — but it is a proposal.
4. **Inference strategy and unit of execution** (§7), which gate any future `query`.
5. **Type system details** (§15): the four types, and no implicit conversion.
6. **Whether the trace is a correctness criterion** (§13), and C1.
7. **Syntax and the language name** (§17), which can be answered last: the IR insulates the engine from them.

Until 1–3 are answered, further language implementation would add more code
resting on assumptions, which is the failure mode this audit exists to stop.
