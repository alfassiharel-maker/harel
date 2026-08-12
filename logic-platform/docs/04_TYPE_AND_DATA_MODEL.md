# TYPE AND DATA MODEL — LML 0.1

Status: **Binding**

> ⚠️ **PROVISIONAL — not approved language semantics.**
> A design audit (`docs/DESIGN_AUDIT.md`) found that most of the rules in this
> document were decided by the implementation, not by the specification. Read
> every rule here together with `docs/SEMANTIC_DECISION_REGISTER.md`, which
> classifies each one as EXPLICITLY_SPECIFIED, DERIVED, ASSUMED BY
> IMPLEMENTATION, or OPEN DESIGN DECISION. Anything marked ASSUMED or OPEN is a
> **proposal awaiting the owner's decision**, regardless of how this document
> phrases it. The word "decided" below means "decided in code", not "approved".

---

## 0. The value model — owner decision OD-2

Every value is in exactly one of three conditions:

```text
Value
├── Unknown        not enough information to determine a value
├── Null           known that there is no value
└── Known(Known)   a concrete value of one of the four types
```

`Unknown ≠ Null ≠ false`, and no operator, output format or serialisation may
collapse them. `Unknown` is a property of the environment — a name nothing has
bound — and is never stored in the fact set, because absence from the set *is*
`Unknown`. `Null` is a stored, comparable, first-class state.

Implemented as `lml_types::Value`, with `Known` holding the four types below.

## 1. Types

Exactly four concrete types, and they are closed:

| Type | Domain | Representation |
|---|---|---|
| `Int` | ℤ ∩ [−2⁶³, 2⁶³−1] | `i64` |
| `Float` | IEEE-754 binary64, **finite only** | `f64` |
| `Bool` | `true`, `false` | `bool` |
| `String` | finite UTF-8 sequences | `String` |

`Int` is 64-bit because a logic engine that silently changes an amount is worse
than one that refuses; arbitrary precision is a real candidate for a later
version but it needs a decision about literals, comparison and SQL mapping first,
so it is not guessed at now.

**Non-finite floats are not values.** `NaN`, `+∞` and `−∞ `cannot be written and
cannot be produced: an operation that would produce one raises `E5003
NonFiniteFloat`. This keeps `Float` totally ordered and equality reflexive, which
in turn keeps the fact set a well-defined map and comparison sound.

## 2. What is *not* a type in 0.1

`List`, `Map`, `Record`, `Set`, `Tuple`, `Enum`, `Option`, `Result`, `Fact`,
`Rule`, `Query`, `State`. The Master Specification §17 lists these as
*candidates* and requires a decision before implementation. None is decided, so
none exists. There is no partially working list type.

**`Null` is not one of them: it is a state, not a collection type.** This
section once argued `Null` was *"deliberately absent"* because `Unknown` did its
job; owner decision OD-2 overruled that, and §0 is the result.

`Type::Null` is the type of the `null` literal. It is **not** a nullability
modifier — there is no `Int?`, per owner decision O2.1 — and it **unifies with
every other type**: a name one rule derives as `null` and another derives as an
`Int` has type `Int`, because "an `Int` that can be absent" is precisely what
the three-state model expresses. Two different concrete types still do not
unify; that remains `E3010`.

## 3. Type discipline

- **Static.** Every expression's type is known after `crates/semantic`; the
  runtime never dispatches on a type it did not expect.
- **Monomorphic.** No generics, no polymorphism, no subtyping, no coercion.
- **Inferred, not annotated.** A name's type comes from its defining expression.
  There is no type-annotation syntax in 0.1 — adding one later is additive.
- **No conversion, implicit or explicit.** There is no `int(x)`, because there
  are no functions. `1 + 1.0` does not compile.

## 4. `Unknown` — and its relationship to `Null`

| Condition | Meaning | Storable in a fact | Writable in source |
|---|---|---|---|
| `Known(v)` | a concrete value | yes | yes |
| `Null` | known that there is no value | yes | yes — the `null` literal |
| `Unknown` | not enough information | **no** | **no** |

The asymmetry is deliberate and follows from what the two states *are*: `Null`
is knowledge, so it can be recorded and written down; `Unknown` is the absence
of knowledge, so recording it would be a second spelling of a name simply not
being bound. `fact a = unknown` would assert that a value is known to be not
known, which is a contradiction, and the grammar has no way to write it.

The three are asked about with the total predicates `x is null`,
`x is unknown` and `x is known`.

`Unknown` is not a value and not a type. It is the state of a name that no rule
has bound. It arises in exactly two places:

1. **During inference** — `eval` yields `NotEvaluable` when a read name is not in
   the environment (Formal Semantics §4.1). This is why an unfired rule is
   *pending*, not *false*.
2. **At output** — an output name that was never derived is emitted as
   `Unknown`, and the trace shows which rules were waiting and for what.

`Unknown` cannot be stored or operated on, but it **can** be asked about:
`x is unknown` answers `true` or `false` and never fails. Letting a program
branch on its own ignorance was once listed as an open question; the strict
model of OD-2 makes it a necessity rather than a curiosity, because without it a
program has no way to cope with the errors `Null` and `Unknown` produce.

## 5. Operator table

`I`=Int, `F`=Float, `B`=Bool, `S`=String. Any combination not listed is `E3012
OperatorTypeMismatch`.

The table below is for `Known` operands. The three-state cells — every
combination involving `Null` or `Unknown` — are in
`docs/VALUE_AND_LOGIC_TRUTH_TABLE.md`, and in summary:

| | `Null` operand | `Unknown` operand |
|---|---|---|
| `==` `!=` | answers: `null == null` is `true`, `null == 5` is `false` | `unknown` |
| `<` `<=` `>` `>=` | `E5004` — an absence has no magnitude | `unknown` |
| `+ - * / %`, unary `-` | `E5005` — an absence has no quantity | `unknown` |
| `and` `or` `not` | `E5006` — an absence has no truth value | `unknown` |
| `is null` `is unknown` `is known` | answers | answers |

| Operator | Operands | Result | Notes |
|---|---|---|---|
| `+` | I,I | I | checked; overflow ⇒ `E5001` |
| `+` | F,F | F | result must be finite |
| `+` | S,S | S | concatenation |
| `-` `*` | I,I | I | checked |
| `-` `*` | F,F | F | |
| `/` | I,I | I | truncating toward zero; zero divisor ⇒ `E5002` |
| `/` | F,F | F | zero divisor ⇒ `E5002`, not `∞` |
| `%` | I,I | I | sign follows the dividend (Rust/C semantics); zero ⇒ `E5002` |
| `%` | F,F | F | |
| unary `-` | I | I | `-i64::MIN` ⇒ `E5001` |
| unary `-` | F | F | |
| `==` `!=` | T,T (same T) | B | see §6 |
| `<` `<=` `>` `>=` | I,I / F,F / S,S | B | strings compare by Unicode scalar order |
| `and` `or` | B,B | B | both operands always evaluated (§Formal 4.1) |
| `not` | B | B | |

Integer `/` truncates toward zero rather than flooring: it is what every target
database and every C-family language does, and a logic engine that disagreed with
the SQL layer about `-7 / 2` would be a permanent source of subtle wrongness.

## 6. Equality and ordering

- Equality is only defined **within** a type, plus `Null` against anything:
  `1 == 1.0` is `E3012`, not `false`, but `x == null` type-checks for every `x`
  because "is this absent?" is always a fair question.
- `Float` equality is bitwise on finite values, which is ordinary IEEE equality
  once `NaN` is excluded. `0.0 == -0.0` is `true`; `-0.0` is normalised to `0.0`
  on construction so that the fact set cannot hold two distinguishable zeroes.
- `String` ordering is by Unicode scalar value — not locale-aware. Locale
  collation is a data-layer concern and would be non-deterministic across hosts.
- The **conflict check** (`E4001`) compares values with this same equality, so
  two rules deriving `1` and `1.0` for one name is caught earlier still, as
  `E3010 ConflictingNameType`.

## 7. Names

A name is a dotted identifier, at most 255 characters, and is **case-sensitive**.
`Temperature` and `temperature` are different names; there is no case-insensitive
matching anywhere in the system.

Names are interned to `NameId` during lowering. Interning is done in a
deterministic order (first appearance in the canonical source order: facts, then
rules, then outputs) so the IR of a program is byte-stable.

## 8. Literals

| Literal | Type |
|---|---|
| `31`, `1_000` | `Int` |
| `3.5`, `1_000.0` | `Float` |
| `true`, `false` | `Bool` |
| `"hot"` | `String` |

There is no float exponent syntax (`1e9`) in 0.1 — it is additive and
uncontroversial, but unspecified syntax is not implemented. There is no
character type, no byte string, no raw string.

## 9. Relationship to SQL types (forward-looking, not implemented)

Recorded here so that the four types above are not chosen in a way that boxes in
Phase 6, **not** as an implemented mapping:

| LML | SQLite | PostgreSQL |
|---|---|---|
| `Int` | `INTEGER` | `BIGINT` |
| `Float` | `REAL` | `DOUBLE PRECISION` |
| `Bool` | `INTEGER 0/1` | `BOOLEAN` |
| `String` | `TEXT` | `TEXT` |
| `Unknown` | `NULL` | `NULL` |

| `Null` | `NULL` | `NULL` | — see below |

**Which state a SQL `NULL` becomes is `OPEN DESIGN DECISION O2.8`.** The earlier
mapping — `NULL` → `Unknown` — was withdrawn when OD-2 made the two distinct. A
column that *is* `NULL` is known to hold no value, which argues for `Null`; a row
never fetched is `Unknown`, and the data layer must be able to produce both.
Phase 6 does not begin until this is decided.
