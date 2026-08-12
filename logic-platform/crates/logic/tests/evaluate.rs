//! Evaluation tests, against `docs/VALUE_AND_LOGIC_TRUTH_TABLE.md`.
//!
//! The truth table's cells are the specification; these are the cells.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lml_diagnostics::{Code, Span};
use lml_ir::{ExprCode, NameId, NameTable, Op};
use lml_logic::{eval, FactSet, Origin};
use lml_types::Value;

const SPAN: Span = Span::new(0, 1);

fn table() -> (NameTable, NameId, NameId) {
    let mut table = NameTable::new();
    let a = table.intern("a");
    let b = table.intern("b");
    (table, a, b)
}

fn run(ops: Vec<Op>, facts: &FactSet) -> Result<Value, Code> {
    eval(&ExprCode::new(ops).expect("balanced"), facts, SPAN).map_err(|d| d.code)
}

fn value(ops: Vec<Op>) -> Value {
    run(ops, &FactSet::new()).expect("evaluates")
}

fn int(value: i64) -> Op {
    Op::Const(Value::int(value))
}

fn float(value: f64) -> Op {
    Op::Const(Value::float(value).expect("finite"))
}

fn null() -> Op {
    Op::Const(Value::Null)
}

fn boolean(value: bool) -> Op {
    Op::Const(Value::bool(value))
}

// ---- known values, unchanged by the three-state model ----------------------

#[test]
fn arithmetic() {
    assert_eq!(value(vec![int(2), int(3), Op::Add]), Value::int(5));
    assert_eq!(value(vec![int(7), int(2), Op::Div]), Value::int(3));
    // Truncation toward zero, matching every SQL target (docs/04 §5).
    assert_eq!(value(vec![int(-7), int(2), Op::Div]), Value::int(-3));
    assert_eq!(value(vec![int(-7), int(2), Op::Rem]), Value::int(-1));
    assert_eq!(
        value(vec![float(1.5), float(2.0), Op::Mul]),
        Value::float(3.0).unwrap()
    );
}

#[test]
fn overflow_is_an_error_not_a_wrap() {
    let facts = FactSet::new();
    assert_eq!(
        run(vec![int(i64::MAX), int(1), Op::Add], &facts),
        Err(Code::IntegerOverflow)
    );
    assert_eq!(
        run(vec![int(i64::MIN), Op::Neg], &facts),
        Err(Code::IntegerOverflow)
    );
    assert_eq!(
        run(vec![int(i64::MIN), int(-1), Op::Div], &facts),
        Err(Code::IntegerOverflow)
    );
}

#[test]
fn division_by_zero_is_an_error_not_an_infinity() {
    let facts = FactSet::new();
    assert_eq!(
        run(vec![int(1), int(0), Op::Div], &facts),
        Err(Code::DivisionByZero)
    );
    assert_eq!(
        run(vec![int(1), int(0), Op::Rem], &facts),
        Err(Code::DivisionByZero)
    );
    assert_eq!(
        run(vec![float(1.0), float(0.0), Op::Div], &facts),
        Err(Code::DivisionByZero)
    );
    assert_eq!(
        run(vec![float(0.0), float(0.0), Op::Div], &facts),
        Err(Code::DivisionByZero)
    );
}

#[test]
fn a_non_finite_result_is_an_error() {
    assert_eq!(
        run(
            vec![float(f64::MAX), float(f64::MAX), Op::Add],
            &FactSet::new()
        ),
        Err(Code::NonFiniteFloat)
    );
}

#[test]
fn comparison_and_logic() {
    assert_eq!(value(vec![int(31), int(30), Op::Gt]), Value::bool(true));
    assert_eq!(
        value(vec![
            Op::Const(Value::string("a")),
            Op::Const(Value::string("b")),
            Op::Lt
        ]),
        Value::bool(true)
    );
    assert_eq!(
        value(vec![boolean(true), boolean(false), Op::And]),
        Value::bool(false)
    );
    assert_eq!(value(vec![boolean(false), Op::Not]), Value::bool(true));
}

#[test]
fn strings_concatenate() {
    assert_eq!(
        value(vec![
            Op::Const(Value::string("ho")),
            Op::Const(Value::string("t")),
            Op::Add
        ]),
        Value::string("hot")
    );
}

// ---- equality over the three states — truth table §5 -----------------------

#[test]
fn equality_null_cells() {
    // null == null → true: both sides are known, and what is known is that
    // neither has a value.
    assert_eq!(value(vec![null(), null(), Op::Eq]), Value::bool(true));
    assert_eq!(value(vec![null(), null(), Op::Ne]), Value::bool(false));
    // null == known → false: one is absent, the other is not.
    assert_eq!(value(vec![null(), int(5), Op::Eq]), Value::bool(false));
    assert_eq!(value(vec![int(5), null(), Op::Eq]), Value::bool(false));
    assert_eq!(value(vec![null(), int(5), Op::Ne]), Value::bool(true));
}

#[test]
fn equality_unknown_cells() {
    let (_table, a, b) = table();
    let facts = FactSet::new();
    // Two unknowns may be anything, including different things.
    assert_eq!(
        run(vec![Op::Load(a), Op::Load(b), Op::Eq], &facts),
        Ok(Value::Unknown)
    );
    assert_eq!(
        run(vec![Op::Load(a), int(5), Op::Eq], &facts),
        Ok(Value::Unknown)
    );
    assert_eq!(
        run(vec![Op::Load(a), null(), Op::Eq], &facts),
        Ok(Value::Unknown)
    );
    assert_eq!(
        run(vec![Op::Load(a), Op::Load(b), Op::Ne], &facts),
        Ok(Value::Unknown)
    );
}

#[test]
fn unknown_and_null_are_never_the_same_answer() {
    // The executable form of OD-2: the two states are distinguishable through
    // the operators, not merely representable apart.
    let (_table, a, _b) = table();
    let facts = FactSet::new();
    let with_unknown = run(vec![Op::Load(a), int(5), Op::Eq], &facts).expect("evaluates");
    let with_null = value(vec![null(), int(5), Op::Eq]);
    assert_eq!(with_unknown, Value::Unknown);
    assert_eq!(with_null, Value::bool(false));
    assert_ne!(with_unknown, with_null);
}

// ---- null refuses everything else — truth table §6, §7, §8 -----------------

#[test]
fn null_has_no_magnitude() {
    let facts = FactSet::new();
    for op in [Op::Lt, Op::Le, Op::Gt, Op::Ge] {
        assert_eq!(
            run(vec![null(), int(1), op.clone()], &facts),
            Err(Code::NullNotOrdered)
        );
        assert_eq!(
            run(vec![int(1), null(), op.clone()], &facts),
            Err(Code::NullNotOrdered)
        );
        assert_eq!(
            run(vec![null(), null(), op], &facts),
            Err(Code::NullNotOrdered)
        );
    }
}

#[test]
fn null_has_no_quantity() {
    let facts = FactSet::new();
    for op in [Op::Add, Op::Sub, Op::Mul, Op::Div, Op::Rem] {
        assert_eq!(
            run(vec![null(), int(1), op.clone()], &facts),
            Err(Code::NullNotArithmetic)
        );
        assert_eq!(
            run(vec![int(1), null(), op], &facts),
            Err(Code::NullNotArithmetic)
        );
    }
    assert_eq!(
        run(vec![null(), Op::Neg], &facts),
        Err(Code::NullNotArithmetic)
    );
}

#[test]
fn null_has_no_truth_value() {
    let facts = FactSet::new();
    assert_eq!(
        run(vec![null(), boolean(true), Op::And], &facts),
        Err(Code::NullNotBoolean)
    );
    assert_eq!(
        run(vec![boolean(false), null(), Op::Or], &facts),
        Err(Code::NullNotBoolean)
    );
    assert_eq!(
        run(vec![null(), Op::Not], &facts),
        Err(Code::NullNotBoolean)
    );
}

// ---- unknown propagates — truth table, every U cell ------------------------

#[test]
fn an_unknown_name_is_unknown_not_false() {
    let (_table, a, _b) = table();
    assert_eq!(run(vec![Op::Load(a)], &FactSet::new()), Ok(Value::Unknown));
    assert_eq!(
        run(vec![Op::Load(a), int(0), Op::Gt], &FactSet::new()),
        Ok(Value::Unknown)
    );
}

#[test]
fn unknown_propagates_through_and_and_or() {
    // `and`/`or` do not short-circuit, so a rule's firing does not depend on
    // the order its author wrote the operands in.
    let (_table, a, b) = table();
    let mut facts = FactSet::new();
    facts.insert(a, Value::bool(false), Origin::Declared, 0);

    assert_eq!(
        run(vec![Op::Load(a), Op::Load(b), Op::And], &facts),
        Ok(Value::Unknown),
        "`false and <unknown>` is unknown, not false"
    );
    assert_eq!(
        run(vec![Op::Load(b), Op::Load(a), Op::And], &facts),
        Ok(Value::Unknown),
        "and the same in the other order"
    );
}

#[test]
fn unknown_beats_null_in_propagation() {
    // An `Unknown` operand short-circuits the whole expression before `null`'s
    // refusals are reached: we cannot know that a rule is malformed until we
    // know what its operands are.
    let (_table, a, _b) = table();
    assert_eq!(
        run(vec![Op::Load(a), null(), Op::Lt], &FactSet::new()),
        Ok(Value::Unknown)
    );
}

// ---- the state predicates are total ----------------------------------------

#[test]
fn state_predicates_answer_for_every_state() {
    let (_table, a, _b) = table();
    let mut facts = FactSet::new();
    facts.insert(a, Value::Null, Origin::Declared, 0);

    assert_eq!(
        run(vec![Op::Load(a), Op::IsNull], &facts),
        Ok(Value::bool(true))
    );
    assert_eq!(
        run(vec![Op::Load(a), Op::IsUnknown], &facts),
        Ok(Value::bool(false))
    );
    assert_eq!(
        run(vec![Op::Load(a), Op::IsKnown], &facts),
        Ok(Value::bool(false))
    );

    let (_table, _a, b) = table();
    assert_eq!(
        run(vec![Op::Load(b), Op::IsUnknown], &facts),
        Ok(Value::bool(true))
    );
    assert_eq!(
        run(vec![Op::Load(b), Op::IsNull], &facts),
        Ok(Value::bool(false))
    );

    assert_eq!(value(vec![int(1), Op::IsKnown]), Value::bool(true));
    assert_eq!(value(vec![int(1), Op::IsNull]), Value::bool(false));
}

#[test]
fn state_predicates_never_fail() {
    // They are the escape hatch that makes the strictness usable, so they must
    // be total: no input produces an error.
    let (_table, a, _b) = table();
    let mut facts = FactSet::new();
    facts.insert(a, Value::Null, Origin::Declared, 0);
    for op in [Op::IsNull, Op::IsUnknown, Op::IsKnown] {
        for operand in [Op::Load(a), int(1), null()] {
            assert!(run(vec![operand, op.clone()], &facts).is_ok());
        }
    }
}

#[test]
fn a_known_name_evaluates() {
    let (_table, a, _b) = table();
    let mut facts = FactSet::new();
    facts.insert(a, Value::int(31), Origin::Declared, 0);
    assert_eq!(
        run(vec![Op::Load(a), int(30), Op::Gt], &facts),
        Ok(Value::bool(true))
    );
}

#[test]
fn evaluation_never_panics_on_ill_typed_code() {
    // The type checker makes this unreachable from source; the evaluator still
    // reports rather than panics (docs/03 §8).
    let ill_typed = vec![int(1), Op::Const(Value::string("x")), Op::Add];
    assert_eq!(
        run(ill_typed, &FactSet::new()),
        Err(Code::InternalInvariant)
    );
}
