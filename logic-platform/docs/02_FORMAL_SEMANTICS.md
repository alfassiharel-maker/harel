# FORMAL SEMANTICS — LML 0.1

Status: **Binding**
Companion to: `01_LANGUAGE_CONSTITUTION.md`

This document is the precise statement of the rules the Constitution states in
prose. Everything here is testable, and every clause marked **[T]** has at least
one test named after it.

> ⚠️ **PROVISIONAL — not approved language semantics.**
> A design audit (`docs/DESIGN_AUDIT.md`) found that most of the rules in this
> document were decided by the implementation, not by the specification. Read
> every rule here together with `docs/SEMANTIC_DECISION_REGISTER.md`, which
> classifies each one as EXPLICITLY_SPECIFIED, DERIVED, ASSUMED BY
> IMPLEMENTATION, or OPEN DESIGN DECISION. Anything marked ASSUMED or OPEN is a
> **proposal awaiting the owner's decision**, regardless of how this document
> phrases it. The word "decided" below means "decided in code", not "approved".

---

## 1. Lexical structure

### 1.1 Character set

Source is UTF-8. Outside of string literals and comments only ASCII is
permitted; a non-ASCII character elsewhere is `E1004 UnexpectedCharacter`. **[T]**

Line terminators are `\n` and `\r\n`. A lone `\r` is `E1004`. Line and column
numbers are 1-based; columns count **Unicode scalar values**, not bytes.

### 1.2 Whitespace and layout

Space (`U+0020`), horizontal tab (`U+0009`) and line terminators are whitespace.
**Layout is not significant.** The indentation in the example programs is
convention, not syntax; a program written on one line is the same program. **[T]**

A tab advances the column by exactly 1. (Chosen so that a column number is a
count of characters, which is what a diagnostic renderer needs; visual alignment
is an editor concern.)

### 1.3 Comments

`# ...` to end of line. No block comments in 0.1 — nesting rules for them are
unspecified, and an unspecified rule is not implemented. **[T]**

### 1.4 Tokens

```
IDENT       := ident_start ident_part* ( '.' ident_start ident_part* )*
ident_start := 'a'..'z' | 'A'..'Z' | '_'
ident_part  := ident_start | '0'..'9'

INT         := '0'..'9' ( '0'..'9' | '_' )*
FLOAT       := INT '.' ( '0'..'9' | '_' )+
STRING      := '"' ( char | escape )* '"'
escape      := '\\' ( '"' | '\\' | 'n' | 't' | 'r' )
BOOL        := 'true' | 'false'
```

- A dotted identifier is **one** token. `user.age` is a name; `user . age` is
  `E1005 SpaceInDottedName` — permitting both would create two spellings of one
  name. **[T]**
- `_` is a legal identifier and carries no special meaning in 0.1.
- Numeric `_` separators are allowed between digits, never leading or trailing:
  `1_000` is `1000`; `1_` is `E1002 MalformedNumber`. **[T]**
- An integer literal that does not fit in `i64` is `E1002`, at lex time, not a
  silent wrap. **[T]**
- There is no leading `-` in a literal: `-1` is unary minus applied to `1`.
- An unterminated string, or a string containing a raw line terminator, is
  `E1003 UnterminatedString`. **[T]**
- An unknown escape is `E1006 InvalidEscape` — it is not passed through. **[T]**

**Keywords** (reserved, may not be used as identifiers, and may not appear as a
segment of a dotted name — both are `E1007 ReservedWord`): `fact`, `rule`,
`when`, `then`, `output`, `and`, `or`, `not`, `true`, `false`. **[T]**

Reserved for future versions and rejected as identifiers with `E1007
ReservedWord` so that adding them later is not a breaking change: `state`,
`query`, `let`, `if`, `else`, `match`, `type`, `import`, `module`, `macro`,
`null`, `unknown`, `derive`, `assert`, `for`, `in`, `while`, `return`, `fn`.
**[T]**

**Operators and punctuation**: `= == != < <= > >= + - * / % ( ) : ,`

Maximal munch: `<=` is one token, never `<` then `=`. **[T]**

### 1.5 Token stream

The lexer emits a `Token { kind, span }` sequence terminated by exactly one
`Eof`. The lexer knows nothing else — it does not know that `fact` introduces a
declaration.

## 2. Grammar

EBNF. `{X}` is zero or more, `[X]` optional. Terminals are the tokens above.
The full grammar also lives in `crates/parser/grammar.ebnf`, which is the file
tests check against this document.

```ebnf
program      = { item } EOF ;

item         = fact_decl | rule_decl | output_decl ;

fact_decl    = "fact" IDENT "=" expr ;

rule_decl    = "rule" IDENT ":" "when" expr then_clause { then_clause } ;

then_clause  = "then" IDENT "=" expr ;

output_decl  = "output" IDENT { "," IDENT } ;

expr         = or_expr ;
or_expr      = and_expr { "or" and_expr } ;
and_expr     = not_expr { "and" not_expr } ;
not_expr     = "not" not_expr | comparison ;
comparison   = additive [ ( "==" | "!=" | "<" | "<=" | ">" | ">=" ) additive ] ;
additive     = multiplicative { ( "+" | "-" ) multiplicative } ;
multiplicative = unary { ( "*" | "/" | "%" ) unary } ;
unary        = "-" unary | primary ;
primary      = INT | FLOAT | STRING | BOOL | IDENT | "(" expr ")" ;
```

Consequences, all deliberate:

- **Comparison does not chain.** `a < b < c` is `E2005 ChainedComparison` with
  the suggestion to write `a < b and b < c`. It is not parsed as `(a<b)<c`. **[T]**
- **Precedence**, loosest to tightest: `or` < `and` < `not` < comparison <
  `+ -` < `* / %` < unary `-` < primary. `and`/`or` are left-associative.
- **No statement terminator.** A declaration ends where the next keyword begins.
  This is unambiguous because every keyword is reserved and no keyword can
  continue an expression. **[T]**
- A rule with no `then` clause does not parse (`E2004 RuleWithoutThen`). A rule
  that derives nothing is meaningless, so it is not expressible. **[T]**

## 3. Static semantics

Checked by `crates/semantic` over the AST, before any lowering. All checks are
performed and **all** diagnostics are reported — the compiler does not stop at
the first error.

### 3.1 Names

- **N1** Duplicate `fact` declaration of the same name ⇒ `E3001 DuplicateFact`,
  pointing at both spans. **[T]**
- **N2** Duplicate `rule` name ⇒ `E3002 DuplicateRule`. **[T]**
- **N3** A name that is read (in any expression) but never written (by any `fact`
  or any `then`) ⇒ `E3003 UnknownName`. **[T]**
  *Rationale:* a name that nothing can ever produce is a typo, not a fact
  awaiting data. Runtime-supplied inputs do not exist in 0.1; when they arrive,
  a declaration form will make them visible to this check.
- **N4** A `then` clause that writes a name also written by a `fact`
  declaration ⇒ `E3004 DerivationShadowsFact`. Facts are immutable (§Const 3.1),
  so this can only ever be a conflict or a redundancy. **[T]**
- **N5** `output` of a name that is never written ⇒ `E3003`. **[T]**
- **N6** Duplicate name in `output` ⇒ `E3005 DuplicateOutput`. Emitting the same
  name twice has no defensible meaning. **[T]**

- **N7** A `fact` whose value reads another name ⇒ `E3006 NonConstantFact`. **[T]**
  *Rationale:* a fact is *given*, not computed. Allowing `fact b = a + 1` would
  need an evaluation order for round 0 that these semantics do not define, and
  the thing it reaches for — one value derived from another — is exactly what a
  rule is. Round 0 therefore holds constants only, and derivation happens in
  exactly one place.

Order does not matter. `output status` before `fact status = 1` is legal; a
program is a set of declarations, not a sequence. **[T]**

### 3.2 Types

The type of every name and every expression is inferred statically; see
`04_TYPE_AND_DATA_MODEL.md` for the type lattice and the operator table.

- **T1** A name's type is the type of its defining expression. If a name is
  written by several rules with different types ⇒ `E3010 ConflictingNameType`,
  naming every writer. **[T]**
- **T2** A rule condition whose type is not `Bool` ⇒ `E3011 NonBooleanCondition`.
  There is no truthiness. `when 1` does not compile. **[T]**
- **T3** An operator applied to types it is not defined for ⇒ `E3012
  OperatorTypeMismatch`, showing the operator and both operand types. **[T]**
- **T4** No implicit conversion exists, in particular none between `Int` and
  `Float`. `1 + 1.0` is `E3012`. **[T]**
  *Rationale:* implicit numeric widening is the classic source of results that
  are wrong in the last digit and cannot be explained from the trace.
- **T5** Type inference is non-cyclic: a name's type may not depend on itself
  through a chain of rules ⇒ `E3013 CyclicTypeDependency`. **[T]**
  Note this restricts *types*, not values; a value-level cycle is fine (§Const 5).

### 3.3 The semantic model

The output of a successful semantic pass is a `SemanticModel`: the validated
declarations, a resolved type for every name, the writer(s) of every name, the
reads of every rule, and the rule dependency graph. It contains no spans-free
data — every element keeps its source span so runtime errors can point at source.

## 4. Dynamic semantics

Configuration: an *environment* `F` is a finite partial map from name to value,
together with each entry's origin.

### 4.1 Expression evaluation

`eval(e, F)` is defined only when every name in `e` is in `dom(F)`. Otherwise
evaluation yields **`NotEvaluable`**, which is distinct from `false` and from an
error. **[T]**

```
eval(literal v, F)        = v
eval(name n, F)           = F(n)                    if n ∈ dom(F)
                          = NotEvaluable            otherwise
eval(a ⊕ b, F)            = NotEvaluable            if either side is NotEvaluable
                          = apply(⊕, eval(a,F), eval(b,F))
eval(not a, F)            = NotEvaluable            if eval(a,F) is NotEvaluable
                          = ¬ eval(a, F)
```

`and` and `or` are **not** short-circuiting with respect to `NotEvaluable`:
`false and x` where `x` is unknown is `NotEvaluable`, not `false`. **[T]**
*Rationale:* short-circuiting would make a rule's firing depend on the textual
order of its conjuncts, which is exactly the kind of hidden dependence §1.3 of
the Constitution forbids. Once every name is known, `and`/`or` are ordinary
boolean operators and the distinction disappears.

Runtime failures of `apply`, all of which abort execution with a structured
error (no `NaN`, no wrapping, no silent truncation):

| Condition | Error |
|---|---|
| integer overflow in `+ - *` | `E5001 IntegerOverflow` **[T]** |
| `/` or `%` with integer zero divisor | `E5002 DivisionByZero` **[T]** |
| `0.0 / 0.0`, `x / 0.0` | `E5002` **[T]** |
| float operation producing non-finite | `E5003 NonFiniteFloat` **[T]** |

### 4.2 Rule application

A rule `r` is **evaluable** under `F` when every name it reads is in `dom(F)`.
A rule **fires** when it is evaluable, has not previously fired, and its
condition evaluates to `true`.

Firing `r` evaluates each `then` clause in source order and adds each binding:

```
add(F, n, v, r):
  n ∉ dom(F)                 ⇒ F ∪ { n ↦ (v, Derived r) }
  F(n) = (v, _)              ⇒ F                       (redundant; traced)
  F(n) = (w, _), w ≠ v       ⇒ E4001 ConflictingDerivation
```

A `then` clause whose expression is `NotEvaluable` at firing time is impossible:
reads are collected over the whole rule, condition and consequences alike, so an
evaluable rule has all of them. **[T]**

### 4.3 Fixed point

```
round 0:  F ← declared facts (in source order; a duplicate is E3001, so this
                              cannot conflict)
round k:  for each rule r in source order:
              if r has not fired and r is evaluable and eval(cond_r, F) = true:
                  fire r, updating F
          if no rule fired in round k: stop
```

- Facts derived in round *k* are visible to rules **later in the same round**
  as well as to subsequent rounds. This makes the result independent of how the
  rounds happen to be cut, which is what makes the fixed point *least* and
  unique. **[T]**
- The iteration terminates: each round either fires ≥1 rule or stops, no rule
  fires twice, so at most *R*+1 rounds run for *R* rules. **[T]**
- `MAX_INFERENCE_ROUNDS` (`03_EXECUTION_MODEL.md` §5) bounds this defensively;
  reaching it is `E8001 InferenceRoundLimit` and indicates a defect in the
  engine, never a legal program.

### 4.4 Output

After the fixed point, each `output n` emits `n` with its value, in source order.
`n` is guaranteed to be *writable* by the static checks, but may still be
**underivable** — every rule that could have written it failed to fire. That is
not an error: the output is emitted with the value `unknown`. **[T]**

This is the `None`-not-`0` rule of the Constitution made concrete: the system
reports that it does not know, and the trace says which rules did not fire and
what they were waiting for.

## 5. Observable result

An execution produces exactly:

1. `outputs`: an ordered list of `(name, Known(value) | Unknown)`;
2. `facts`: the final environment with origins, in name order;
3. `trace`: the event sequence (`03_EXECUTION_MODEL.md` §7);
4. or a single structured error, in which case a partial trace is still produced
   up to the failing event. **[T]**

Two implementations of this specification agree iff all four agree.
