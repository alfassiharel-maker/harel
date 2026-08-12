# TYPE AND DATA MODEL — LML 0.1

Status: **Binding**

---

## 1. Types in 0.1

Exactly four, and they are closed:

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

`Null`, `List`, `Map`, `Record`, `Set`, `Tuple`, `Enum`, `Option`, `Result`,
`Fact`, `Rule`, `Query`, `State`. The Master Specification §17 lists these as
*candidates* and requires a decision before implementation. None is decided, so
none exists. There is no partially working list type.

`Null` deserves its own note: it is **deliberately absent**, and its job is done
by `Unknown` (§4), which is not a value and cannot be stored in a fact or
compared. The distinction is the whole of the `None`-not-`0` discipline: absence
is a property of the *environment*, not an inhabitant of a type.

## 3. Type discipline

- **Static.** Every expression's type is known after `crates/semantic`; the
  runtime never dispatches on a type it did not expect.
- **Monomorphic.** No generics, no polymorphism, no subtyping, no coercion.
- **Inferred, not annotated.** A name's type comes from its defining expression.
  There is no type-annotation syntax in 0.1 — adding one later is additive.
- **No conversion, implicit or explicit.** There is no `int(x)`, because there
  are no functions. `1 + 1.0` does not compile.

## 4. `Unknown`

`Unknown` is not a value and not a type. It is the state of a name that no rule
has bound. It arises in exactly two places:

1. **During inference** — `eval` yields `NotEvaluable` when a read name is not in
   the environment (Formal Semantics §4.1). This is why an unfired rule is
   *pending*, not *false*.
2. **At output** — an output name that was never derived is emitted as
   `Unknown`, and the trace shows which rules were waiting and for what.

`Unknown` cannot be compared, stored, or operated on. There is no `is_unknown`
operator in 0.1 (it would let a program branch on ignorance, and what that means
is unspecified). It is an **OPEN DESIGN DECISION** for 0.2.

## 5. Operator table

`I`=Int, `F`=Float, `B`=Bool, `S`=String. Any combination not listed is `E3012
OperatorTypeMismatch`.

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

- Equality is only defined **within** a type. `1 == 1.0` is `E3012`, not `false`.
  Cross-type equality that returns `false` hides bugs.
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

SQL `NULL` maps to `Unknown` — absence of a fact — rather than to a value. That
correspondence is why `Null` was not made a value type: the two models line up
exactly, and three-valued SQL logic can be expressed by the pending/`NotEvaluable`
mechanism that already exists.
