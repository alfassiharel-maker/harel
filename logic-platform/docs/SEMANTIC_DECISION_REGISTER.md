# SEMANTIC DECISION REGISTER

Status: **audit output — nothing here is approved language semantics**
Produced by: the design audit of `docs/DESIGN_AUDIT.md`
Governs: how every claim in `docs/01`–`docs/05` must be read

---

## How to read this file

Every semantic decision present in the repository is listed, whether it came
from the specification or from me. The `Source` column is the honest provenance,
not the justification. The four statuses are:

| Status | Meaning |
|---|---|
| **EXPLICITLY_SPECIFIED** | The Master Specification states it. I implemented what it said. |
| **DERIVED FROM SPECIFICATION** | The specification does not state it, but it follows from something it does state, with no real freedom left. The derivation is named in `Consequence`. |
| **ASSUMED BY IMPLEMENTATION** | I chose it. The specification is silent, or asks the question without answering it. **Not approved.** |
| **OPEN DESIGN DECISION** | The specification names it as a decision to be made, and forbids an agent from making it. Where code exists, it embodies a *proposal*, not a ruling. |

A separate table at the end lists **implementation choices** — decisions with no
semantic content, which may be changed without a language decision. The
distinction matters and is the subject of §3 of the audit.

**The specification's own words on this** (Master Spec §8, on the rule model):
*"אין להמציא answers"* — do not invent answers. §23 on conflicts: *"אין לבחור
אחת לפני Design Review"*. §54: *"No agent may silently choose values for these
decisions."* Where the tables below say ASSUMED or OPEN, I did exactly what
those sentences forbid, and the audit records it as a defect rather than a
feature.

---

## 1. Facts

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| A fact binds a name to a value | `fact temperature = 31` binds `temperature` | Master Spec §7 gives exactly these examples | **EXPLICITLY_SPECIFIED** | The one part of the fact model that is settled. |
| Facts are **immutable** within an execution | A name is bound at most once; no update, no retraction | Master Spec §7 says *"אין להניח שכל Fact הוא mutable. ה־semantic model חייב להגדיר זאת במפורש"* — it requires an explicit decision and does not make one | **ASSUMED BY IMPLEMENTATION** | Load-bearing for nearly everything else: monotonicity, the fixed point, termination without cycle detection, and the conflict rule all rest on it. If mutability is approved, the inference engine is redesigned, not patched. |
| Facts are **ground** (no variables, no stored expressions) | `fact a = 1 + 2` stores `3` | Not stated. §7's examples are all ground, which is weak evidence | **ASSUMED BY IMPLEMENTATION** | Blocks relational/Datalog-style facts until decision D of the open register is made. |
| Facts are **named, not relational**; `user.age` is one atomic name | The dot has no structure | §7 uses `user.age` and `account.status` without saying what the dot means | **ASSUMED BY IMPLEMENTATION** | Will not scale to an enterprise rule base. Flagged as weakness 2 in `OPEN_DESIGN_DECISIONS.md`. |
| A fact carries its **origin** (declared / derived by rule R) | `Origin::Declared \| Derived(RuleId)` | Derivable from §10, which requires the trace to distinguish loaded from derived facts | **DERIVED FROM SPECIFICATION** | Needed for explanation; low risk. |
| A `fact` declaration's value must be **constant** (`E3006`) | `fact b = a + 1` is rejected | Nothing in the specification. My invention, to avoid defining an evaluation order for round 0 | **ASSUMED BY IMPLEMENTATION** | Rejects programs the specification never forbade. The alternative — evaluating declarations in the fixed point like everything else — is entirely viable and was not considered in writing before it was implemented. |

## 2. Rules

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| A rule relates a condition to a consequence | `when <cond> then <name> = <expr>` | Master Spec §8 and §56 give this shape | **EXPLICITLY_SPECIFIED** | Settled. |
| A rule is an **implication**, not a procedure/trigger/constraint/relation | Pure derivation of bindings | §55.A asks *which* of those a rule is, and says these questions *"define the actual identity of the language"* | **OPEN DESIGN DECISION** | The single largest unapproved decision in the repository. It shapes the AST, the IR, the engine and the trace. |
| Rules are **pure** — no side effects | A rule cannot perform I/O or mutate | §8 asks *"האם rules טהורים או יכולים לגרום side effects"* and forbids inventing an answer | **OPEN DESIGN DECISION** | Also register entry E. Purity is what makes the current trace complete; effects would require an effect log. |
| A rule fires **at most once** per execution | `RuleState` marks it fired | §8 asks *"האם rule יכול לפעול יותר מפעם אחת"* — unanswered | **ASSUMED BY IMPLEMENTATION** | Observationally equivalent to firing-until-stable *only while facts are immutable*. If mutability is approved, this becomes a visible, contestable choice. |
| A rule is evaluable only when **every** name it reads is known — condition and consequences alike | `knows_all(rule.reads)` | Not specified. Follows from my `Unknown != false` choice | **ASSUMED BY IMPLEMENTATION** | Determines when rules fire, and therefore what a program computes. |
| Rule **names** are mandatory and unique | `rule heat:` ; duplicates are `E3002` | §56's example names its rule; uniqueness is mine | **DERIVED FROM SPECIFICATION** (naming) / **ASSUMED** (uniqueness) | Low risk. Names are required for the trace and conflict reports. |
| A rule with no `then` does not parse (`E2004`) | Rejected at parse time | Not specified | **ASSUMED BY IMPLEMENTATION** | Low risk, easily reversed. |
| A rule may derive **several** names | Multiple `then` clauses | Not specified | **ASSUMED BY IMPLEMENTATION** | Low risk; additive either way. |

## 3. Unknown values and truth evaluation

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| **`Unknown` is not `false`** | A rule reading an underived name is *pending*, retried next round | **Nothing in the Master Specification.** Supported by the repository convention in `CLAUDE.md` (*"Missing data is `None`, never `0`"*), which governs a different project | **ASSUMED BY IMPLEMENTATION** | The decision the audit was called over, and correctly so. It changes which rules fire and therefore what programs mean. Every alternative (unknown-is-false; three-valued logic with an explicit `unknown` value; failure) yields a different language. |
| `Unknown` is **not a value** — it is a property of the environment | Cannot be stored, compared, or operated on; no `Null` type | §17 lists `Null` as a **candidate primitive type**, and §17 requires deciding which entities are real types *before implementation* | **ASSUMED BY IMPLEMENTATION**, and it **narrows** an option the specification kept open | If `Null` is approved as a value, `Value`, the operator table, the fact set and the output model all change. |
| An output nothing derived is `unknown`, not an error and not a default | `status = unknown` | Not specified. §10 requires a Final Result; it does not say what an underivable one is | **ASSUMED BY IMPLEMENTATION** | Observable in every program's output. |
| A condition must be of type `Bool`; there is no truthiness | `when 1` is `E3011` | Not specified | **ASSUMED BY IMPLEMENTATION** | Rejects programs the specification never forbade. |

## 4. Conjunction and disjunction

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| `and` / `or` **do not short-circuit** over `Unknown` | `false and <unknown>` is `NotEvaluable`, not `false` | Not specified. Follows from `Unknown != false` | **ASSUMED BY IMPLEMENTATION** | Directly changes rule firing. The opposite choice — Kleene three-valued logic, where `false and unknown = false` — is at least as defensible and is what SQL does. **I did not present this alternative before implementing.** |
| `and`/`or` are left-associative, `or` looser than `and` | Standard precedence | Not specified; conventional | **ASSUMED BY IMPLEMENTATION** | Low risk, but it is syntax, and syntax is open (§54). |
| Comparison does not chain (`a < b < c` is `E2005`) | Rejected with a suggestion | Not specified | **ASSUMED BY IMPLEMENTATION** | Low risk. |

## 5. Rule conflicts

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| Two rules deriving **different** values for one name is an error (`E4001`) | Execution stops | §23 lists six options — priority, specificity, explicit ordering, **conflict error**, multi-result, custom — and states *"אין לבחור אחת לפני Design Review"* | **OPEN DESIGN DECISION** | `docs/01` §4 claimed *"This **is** that review"*. It was not: a design review is an act of the owner, not of the agent performing the work. The claim is withdrawn by this audit. The behaviour remains in code as a proposal. |
| Two rules deriving the **same** value is not a conflict | Recorded as `DerivationRedundant` | Not specified | **ASSUMED BY IMPLEMENTATION** | Follows from the above; falls with it. |
| A rule may not derive a name a `fact` declares (`E3004`) | Rejected statically | Not specified | **ASSUMED BY IMPLEMENTATION** | Depends on fact immutability. |

## 6. Rule ordering and scheduling

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| Rules are evaluated in **source order** within a round | `for rule in &program.rules` | §21 requires *evaluation order* and *rule scheduling* to be defined explicitly **before implementation**; §54 lists Rule Scheduling as an open decision | **OPEN DESIGN DECISION** | Currently unobservable in results (the fixed point is order-independent while facts are immutable) but fully observable **in the trace**, and it becomes result-visible the moment mutability or side effects are approved. |
| Facts derived earlier in a round are visible later in the same round | Immediate visibility | Not specified | **ASSUMED BY IMPLEMENTATION** | Affects round counts and trace shape; not results, under immutability. |

## 7. Inference strategy

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| Inference runs **forward** (facts → rules → derived facts) | Forward chaining | Master Spec §3 and §9 draw exactly this flow | **DERIVED FROM SPECIFICATION** | Well supported. |
| The strategy is forward chaining **to a least fixed point**, not backward, hybrid, or search | Round-based iteration | §55.C asks precisely this question and reserves it for the design phase | **OPEN DESIGN DECISION** | A `query` form (Phase 6) most naturally wants backward chaining. The engine is structured to allow adding it (`lml-reasoning` is separate from `lml-logic`), but the current answer is a proposal. |
| The unit of execution is the **program** | One `run` per program | §55.E asks whether it is program, rule set, transaction, query or goal | **OPEN DESIGN DECISION** | Shapes the public API of the runtime. |

## 8. Cycles

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| A cyclic rule dependency is **not an error** and needs no detection | Reaches a fixed point | §24 requires a defined policy — *error*, *iteration limit*, *fixed point*, *explicit recursion* — and says *"לא לפני specification"* | **OPEN DESIGN DECISION** | The current answer is *fixed point*, which is one of the four the specification lists. It is a proposal, not a ruling. |
| Cycles are not **reported** to the author | No cycle diagnostic | §24 says the system must *detect or control* cycles; the current engine controls without detecting | **ASSUMED BY IMPLEMENTATION** | Arguably under-delivers on §24: an author with an unintended cycle gets `unknown` outputs and no explanation of why. |

## 9. Fixed point and termination

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| Termination is guaranteed by monotonicity, not by the round limit | ≤ R+1 rounds for R rules | Derived from immutability + finite rules | **DERIVED FROM SPECIFICATION** *(conditional on the immutability assumption, which is itself ASSUMED)* | If immutability falls, so does this proof. |
| Exhausting `MAX_INFERENCE_ROUNDS` raises `E8001` | Structured error | §21 requires termination to be defined; the specific mechanism is mine | **ASSUMED BY IMPLEMENTATION** | A program can observe `E8001`, so this limit is not purely internal. |

## 10. Determinism

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| Same source + input + data + state + version + config ⇒ same **result** | Guaranteed | Master Spec §22, almost verbatim | **EXPLICITLY_SPECIFIED** | Settled. |
| The **trace** is also byte-identical between runs | No clock; sequence numbers only | §22 speaks of *"אותה תוצאה"* — the same *result*. It does not mention the trace | **ASSUMED BY IMPLEMENTATION** | The second decision the audit was called over. |
| The trace therefore carries **no timestamps** | `seq` only; timing is a metric elsewhere | **This contradicts Master Spec §10**, whose trace model explicitly lists **Timing** — and also **Execution ID**, which is likewise absent | **ASSUMED BY IMPLEMENTATION — and it conflicts with an explicit requirement** | See audit §5, contradiction C1. A trace with timing and a trace that is byte-comparable are both achievable (timing in a side channel keyed by `seq`), but I removed a specified field instead of designing for both. |

## 11. State mutation

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| There is no mutable state in 0.1 | Not implemented, absent from the tree | §54 lists Mutation Model as open; §39 Phase 0 requires State to be defined before code | **OPEN DESIGN DECISION** | Correctly handled: absent rather than half-built. |
| `state` is a **reserved word** rejected as `E1007` | `fact state = 1` fails | Mine | **ASSUMED BY IMPLEMENTATION** | Also contradicts `docs/01` §3.5, which claims there is *"no reserved word pretending to be one"*. See audit contradiction C2. |

## 12. Output semantics

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| `output <name>` emits a named result | As specified | §56's example program ends `output status` | **EXPLICITLY_SPECIFIED** | Settled. |
| Outputs are emitted in source order, after the fixed point | Fixed | Not specified | **ASSUMED BY IMPLEMENTATION** | Observable ordering of results. |
| Naming one name twice in `output` is an error (`E3005`) | Rejected | Not specified | **ASSUMED BY IMPLEMENTATION** | Low risk. |
| `output` of a name nothing can write is a static error (`E3003`) | Rejected | Not specified; and it presumes there is no runtime-supplied input, which §7 contemplates (*"runtime input"*) | **ASSUMED BY IMPLEMENTATION** | Will need revisiting the moment inputs exist, which §7 says they will. |

## 13. Trace semantics

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| Execution produces a structured, machine-readable trace | 12 event kinds | §10, §51 | **EXPLICITLY_SPECIFIED** | Settled. |
| The trace must support reconstructing the reasoning path | `why` walks the trace | §9: the engine must retain enough to reconstruct the path | **EXPLICITLY_SPECIFIED** | Settled. |
| An explanation is derived **only** from the trace, never re-derived, and returns nothing when the trace cannot support it | `why` returns `None` | §10's intent (*explanation from the trace, not from guessing*) | **DERIVED FROM SPECIFICATION** | Well supported. |
| The trace is **part of correctness** — two engines agreeing on outputs but not traces are not both correct | Asserted in `docs/01` §3.8 | Not specified. §10 requires a trace to exist and be sufficient; it does not make it a conformance criterion | **ASSUMED BY IMPLEMENTATION** | The third decision the audit was called over. It raises the cost of every future engine change: any reordering that preserves results still breaks conformance. |
| Specified trace fields **not** implemented: Execution ID, Timing, Initial State, Intermediate Results, SQL Queries, State Changes | Absent | §10 lists all of them | **DEVIATION** (the last three are legitimately absent — no state, no SQL — the first three are not) | See audit contradiction C1. |
| `RuleActivated` carries the names the rule read | Added so `why` needs only the trace | Mine | **ASSUMED BY IMPLEMENTATION** | Low risk; additive. |

## 14. Error semantics

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| Errors carry code, location, message, context, suggested resolution | `Diagnostic` | §25 lists exactly these fields | **EXPLICITLY_SPECIFIED** | Implemented — except **Cause**, which §25 also lists and which has no field. See audit gap G1. |
| Error categories (lexical, syntax, type, semantic, logic, runtime, SQL, data, security, resource) | `E1xxx`–`E8xxx` ranges | §25 lists the categories; the numbering scheme is mine | **DERIVED** (categories) / **implementation choice** (numbering) | Low risk. |
| Every stage reports **all** its diagnostics, not the first | Multi-error reporting | Not specified | **ASSUMED BY IMPLEMENTATION** | Developer-visible, semantically inert. |
| Arithmetic overflow, division by zero and non-finite floats are **errors**, never wrapped, infinite or `NaN` values | `E5001`, `E5002`, `E5003` | Not specified. §25 provides the category, not the policy | **ASSUMED BY IMPLEMENTATION** | Strongly defensible, and still a language decision: an alternative language returns `unknown` for `1/0` rather than failing the run. |

## 15. Type semantics

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| The language is typed | Yes | §17: *"המערכת צריכה להיות typed"* | **EXPLICITLY_SPECIFIED** | Settled. |
| Exactly four types: `Int`, `Float`, `Bool`, `String` | Closed set | §17 lists eight candidate primitives (including `Null`, `List`, `Map`, `Record`) and requires a decision before implementation | **OPEN DESIGN DECISION** — my four are a proposal | The subset is defensible for a slice, but §17 asked for a ruling and I supplied one. |
| Types are **static, inferred, monomorphic**; no annotations | Inference from defining expression | §54 lists *Type System Details* as open | **OPEN DESIGN DECISION** | Affects every future extension. |
| **No implicit conversion**, in particular `Int`/`Float` | `1 + 1.0` is `E3012` | Not specified | **ASSUMED BY IMPLEMENTATION** | Rejects programs many users would expect to work. Defensible, unapproved. |
| Cross-type equality is a **type error**, not `false` | `1 == 1.0` is `E3012` | Not specified | **ASSUMED BY IMPLEMENTATION** | Same. |
| `Int` is 64-bit; overflow is an error | `i64` | Not specified; §54 lists no size decision, §17 says *Integer* | **ASSUMED BY IMPLEMENTATION** | Arbitrary precision remains possible; see register entry M. |
| `Float` excludes `NaN` and infinities | Rejected at construction | Not specified | **ASSUMED BY IMPLEMENTATION** | Makes `Value` totally ordered — a property the fact set and conflict check rely on. Reversing it is not local. |
| Integer division truncates toward zero | `-7 / 2 == -3` | Not specified; chosen to match SQL targets | **ASSUMED BY IMPLEMENTATION** | Observable in results. |
| `+` concatenates strings | `"a" + "b"` | Not specified | **ASSUMED BY IMPLEMENTATION** | Low risk. |
| `String` ordering is by Unicode scalar value, not locale | Fixed | Not specified; required by determinism | **DERIVED FROM SPECIFICATION** (§22) | Well supported. |

## 16. Query semantics

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| No `query` form exists | Absent; `query` is a reserved word | §54 lists Query Semantics as open; §39 puts SQL at Phase 6 | **OPEN DESIGN DECISION** | Correctly handled — absent, not stubbed. |
| How SQL enters the logical model | Not implemented | §55.F asks: query → facts? relation as first-class value? external source? | **OPEN DESIGN DECISION** | Blocks Phase 6. Also interacts with determinism — see `OPEN_DESIGN_DECISIONS.md` weakness 1. |

## 17. Syntax (the whole surface)

| Decision | Current behavior | Source of decision | Status | Consequence |
|---|---|---|---|---|
| The concrete grammar: keywords, layout-insensitivity, `#` comments, dotted names, no terminators, operator set | `crates/parser/grammar.ebnf` | §54 lists **Exact Grammar** and **Syntax Style** as open decisions. §56 supplies one example program, which the grammar accepts | **OPEN DESIGN DECISION** — the grammar is a *proposal consistent with the specification's example* | Everything downstream is replaceable if the syntax changes, because the IR is the runtime's contract; the AST and parser would be rewritten. |
| Language name **LML**, extension `.lml` | Used throughout | §54 lists Language Name as open; `.lml` follows the Engineering Spec §39 fixture names | **OPEN DESIGN DECISION** (name) / **DERIVED** (extension) | Confined to two places, as `docs/01` §0 states. |
| Reserved-word list for future versions | 19 words rejected as `E1007` | Mine | **ASSUMED BY IMPLEMENTATION** | Pre-commits the language to a vocabulary; cheap to change now, expensive later. |

---

## Implementation choices — no semantic content

These may be changed at any time without a language decision, **provided** the
semantic decisions they currently support are not themselves approved in a form
that promotes them. The distinction is exactly §3 of the audit brief.

| Choice | Why it is not semantics | What would promote it to semantics |
|---|---|---|
| `BTreeMap` for the fact set | Iteration order affects no result | It **already** affects the trace; if "trace determinism is correctness" is approved, this becomes semantics. |
| Interning names to `NameId` | Invisible outside the engine | Nothing foreseeable. |
| Post-order `ExprCode` and a stack evaluator | An AST walk would compute the same values | Nothing, unless the IR is exposed to user programs (meta-programming, register entry H). |
| SHA-256 program digest | Identity of a compiled artefact | If a program can query its own digest. |
| `MAX_EXPR_STACK`, `MAX_EXPR_DEPTH`, `MAX_TYPE_DEPENDENCY_DEPTH`, `MAX_SOURCE_BYTES`, `MAX_NAME_LENGTH` | Resource guards against adversarial input | Each is observable as an `E2003`/`E8002`/`E8004` rejection, so each already draws a line around the set of legal programs. Treat as **semantics-adjacent**. |
| `MAX_INFERENCE_ROUNDS` | Defect backstop | Already observable as `E8001` (see §9 above). |
| Recursive-descent parser rather than a generator | Same language either way | Nothing. |
| Zero third-party dependencies | Supply chain and build property | Nothing. |
| Multi-error reporting, diagnostic rendering format | Developer experience | Nothing. |
| Crate boundaries and the dependency graph | Code organisation | Nothing — but see audit §6, where two boundaries have drifted. |

---

## Summary of counts

| Status | Count |
|---|---|
| EXPLICITLY_SPECIFIED | 8 |
| DERIVED FROM SPECIFICATION | 6 |
| **ASSUMED BY IMPLEMENTATION** | **31** |
| **OPEN DESIGN DECISION** | **13** |

Thirty-one assumptions and thirteen decisions the specification reserved for its
owner are embedded in working, tested code. The tests prove the engine does what
the documents say. They cannot prove the documents say the right thing, and the
audit exists because I blurred that line.
