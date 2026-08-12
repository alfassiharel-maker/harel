# OPEN DESIGN DECISIONS

Master Specification §54 lists decisions that no agent may resolve silently. This
file is the register. Every item is either **OPEN** (unresolved — nothing in the
tree may depend on an answer) or **DECIDED for 0.1** with the alternatives that
were rejected and why.

Moving an item from OPEN to DECIDED requires the amendment procedure in
`01_LANGUAGE_CONSTITUTION.md` §9.

---

## Decided for 0.1

| # | Decision | Resolution | Rejected | Where |
|---|---|---|---|---|
| 1 | What is a Rule? | Pure implication, fires at most once, no side effects | procedure, trigger, constraint | Const §3.2 |
| 2 | What is a Fact? | Typed, ground, immutable, named binding with an origin | mutable state, relation/tuple | Const §3.1 |
| 3 | What is Inference? | Forward chaining to a least fixed point over a monotone fact set | backward chaining, hybrid, search | Const §3.4 |
| 4 | Unit of execution | The program | rule set, transaction, goal | Const §3.4 |
| 5 | Conflict resolution | Different values for one name ⇒ error `E4001` | priority, specificity, source order, multi-result | Const §4 |
| 6 | Cycle semantics | No special handling needed; monotone iteration terminates. Round limit is a defect backstop | error on cycle, iteration limit as semantics, explicit recursion | Const §5 |
| 7 | Evaluation strategy | Eager, both operands of `and`/`or` always evaluated so `NotEvaluable` propagates | short-circuit | Formal §4.1 |
| 8 | Determinism | Total; ordered maps, source-order rules, no clock, no randomness | opt-in determinism | Const §6 |
| 9 | Type system (0.1 subset) | Four closed types, static, monomorphic, no coercion | `Null`, collections, inference with subtyping | Types §1–3 |
| 10 | Absence model | `Unknown` is a property of the environment, not a value | a `Null` value | Types §4 |
| 11 | Language surface | Exactly `fact`, `rule`, `output` | reserving keywords that half-work | Const §2 |
| 12 | Core language | Rust, zero third-party dependencies | any crate not yet justified | Arch §3 |

## Open — nothing may depend on these

| # | Decision | What must be answered first |
|---|---|---|
| A | **Language name** | Product decision. Confined to the CLI binary name and `.lml`; codename LML until resolved. |
| B | **Mutation / state model** | What a transition is; whether rules may cause one; how a rule that read the old value is explained; what "fixed point" means once the fact set is not monotone. |
| C | **Query semantics** | Is a query a goal (backward chaining), a filter over derived facts, or SQL? Does it observe the fixed point or drive it? |
| D | **Relational facts** | Arity, variables, unification, join strategy, safety/range restriction, negation (stratified?). |
| E | **Side-effect model** | Whether any rule may ever cause an effect, and if so how determinism and the trace survive it. |
| F | **Concurrency model** | Deferred until a benchmark proves a need (Eng §59). |
| G | **Memory model** | Only relevant with large fact sets; no data yet. |
| H | **Meta-programming / macro model** | What code may know about itself (AST? IR? runtime state? execution history?), and the sandbox boundary. |
| I | **AI integration boundary** | Where a model's output stops being untrusted text and becomes a fact; the validation gate. Master Spec §36. |
| J | **Plugin architecture** | ABI vs. source plugins; capability model. |
| K | **Distributed execution** | Explicit MVP non-goal (Master Spec §46). |
| L | **`is_unknown` / introspection on absence** | What it means for a program to branch on its own ignorance. |
| M | **Arbitrary-precision integers** | Literal syntax, comparison, and SQL mapping. |
| N | **Parser technology at scale** | Hand-written recursive descent is right for this grammar; a generator becomes a question only if the grammar grows. Master Spec §27. |

## Weak points in the specification, flagged rather than implemented

Master Specification §59 asks for architectural push-back rather than blind
implementation. Three items:

1. **"SQL as a first-class data interface" is in tension with determinism.**
   A query against a live database makes a run non-reproducible, so the same
   source + input can produce different results. Before Phase 6, the trace must
   record either the query results themselves or a snapshot identifier;
   otherwise the explainability guarantee (Const §1.2) silently weakens the
   moment SQL is enabled. Proposed: SQL results enter as *declared facts with a
   source origin*, recorded verbatim in the trace.

2. **The named-fact model does not scale to enterprise rule bases.** A fact per
   name is fine for a vertical slice and hopeless for `10⁶` customer records.
   Item D above is the real answer, and it should come *before* SQL rather than
   after it — otherwise the SQL layer will be designed against a fact model that
   is about to be replaced.

3. **"Self-analysis" (Master Spec §35) needs a boundary before it needs code.**
   A program that can read its own execution history can encode decisions that no
   longer follow from its rules, which would break the explainability guarantee.
   Recommendation: analysis is available to *tooling* over the IR, and to
   *programs* not at all, until item H is resolved.
