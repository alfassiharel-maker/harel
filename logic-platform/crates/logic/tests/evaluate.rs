//! Evaluation tests, against `docs/02_FORMAL_SEMANTICS.md` §4.1 and the
//! operator table of `docs/04_TYPE_AND_DATA_MODEL.md` §5.

// In a test, a panic is the reporting mechanism: `unwrap`, `expect` and
// `assert!` are how a failure is announced. They stay denied in library code
// (docs/03_EXECUTION_MODEL.md §8).
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lml_diagnostics::{Code, Span};
use lml_ir::{ExprCode, NameId, NameTable, Op};
use lml_logic::{eval, Evaluated, FactSet, Origin};
use lml_types::Value;

const SPAN: Span = Span::new(0, 1);

fn table() -> (NameTable, NameId, NameId) {
    let mut table = NameTable::new();
    let a = table.intern("a");
    let b = table.intern("b");
    (table, a, b)
}

fn run(ops: Vec<Op>, facts: &FactSet) -> Result<Evaluated, Code> {
    eval(&ExprCode::new(ops).expect("balanced"), facts, SPAN).map_err(|d| d.code)
}

fn known(ops: Vec<Op>) -> Value {
    match run(ops, &FactSet::new()) {
        Ok(Evaluated::Known(value)) => value,
        other => panic!("expected a value, got {other:?}"),
    }
}

fn int(value: i64) -> Op {
    Op::Const(Value::Int(value))
}

fn float(value: f64) -> Op {
    Op::Const(Value::Float(value))
}

#[test]
fn arithmetic() {
    assert_eq!(known(vec![int(2), int(3), Op::Add]), Value::Int(5));
    assert_eq!(known(vec![int(7), int(2), Op::Div]), Value::Int(3));
    // Truncation toward zero, matching every SQL target (docs/04 §5).
    assert_eq!(known(vec![int(-7), int(2), Op::Div]), Value::Int(-3));
    assert_eq!(known(vec![int(-7), int(2), Op::Rem]), Value::Int(-1));
    assert_eq!(
        known(vec![float(1.5), float(2.0), Op::Mul]),
        Value::Float(3.0)
    );
}

#[test]
fn overflow_is_an_error_not_a_wrap() {
    assert_eq!(
        run(vec![int(i64::MAX), int(1), Op::Add], &FactSet::new()),
        Err(Code::IntegerOverflow)
    );
    assert_eq!(
        run(vec![int(i64::MIN), Op::Neg], &FactSet::new()),
        Err(Code::IntegerOverflow)
    );
    assert_eq!(
        run(vec![int(i64::MIN), int(-1), Op::Div], &FactSet::new()),
        Err(Code::IntegerOverflow)
    );
}

#[test]
fn division_by_zero_is_an_error_not_an_infinity() {
    assert_eq!(
        run(vec![int(1), int(0), Op::Div], &FactSet::new()),
        Err(Code::DivisionByZero)
    );
    assert_eq!(
        run(vec![int(1), int(0), Op::Rem], &FactSet::new()),
        Err(Code::DivisionByZero)
    );
    assert_eq!(
        run(vec![float(1.0), float(0.0), Op::Div], &FactSet::new()),
        Err(Code::DivisionByZero)
    );
    assert_eq!(
        run(vec![float(0.0), float(0.0), Op::Div], &FactSet::new()),
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
    assert_eq!(known(vec![int(31), int(30), Op::Gt]), Value::Bool(true));
    assert_eq!(
        known(vec![
            Op::Const(Value::Str("a".into())),
            Op::Const(Value::Str("b".into())),
            Op::Lt
        ]),
        Value::Bool(true)
    );
    assert_eq!(
        known(vec![
            Op::Const(Value::Bool(true)),
            Op::Const(Value::Bool(false)),
            Op::And
        ]),
        Value::Bool(false)
    );
    assert_eq!(
        known(vec![Op::Const(Value::Bool(false)), Op::Not]),
        Value::Bool(true)
    );
}

#[test]
fn strings_concatenate() {
    assert_eq!(
        known(vec![
            Op::Const(Value::Str("ho".into())),
            Op::Const(Value::Str("t".into())),
            Op::Add
        ]),
        Value::Str("hot".into())
    );
}

#[test]
fn an_unknown_name_is_not_evaluable_not_false() {
    let (_table, a, _b) = table();
    assert_eq!(
        run(vec![Op::Load(a)], &FactSet::new()),
        Ok(Evaluated::NotEvaluable)
    );
    assert_eq!(
        run(vec![Op::Load(a), int(0), Op::Gt], &FactSet::new()),
        Ok(Evaluated::NotEvaluable)
    );
}

#[test]
fn not_evaluable_propagates_through_and_and_or() {
    // §4.1 — `and`/`or` do not short-circuit, so that a rule's firing does not
    // depend on the order its author wrote the operands in.
    let (_table, a, b) = table();
    let mut facts = FactSet::new();
    facts.insert(a, Value::Bool(false), Origin::Declared);

    assert_eq!(
        run(vec![Op::Load(a), Op::Load(b), Op::And], &facts),
        Ok(Evaluated::NotEvaluable),
        "`false and <unknown>` is unknown, not false"
    );
    assert_eq!(
        run(vec![Op::Load(b), Op::Load(a), Op::And], &facts),
        Ok(Evaluated::NotEvaluable),
        "and the same in the other order"
    );
}

#[test]
fn a_known_name_evaluates() {
    let (_table, a, _b) = table();
    let mut facts = FactSet::new();
    facts.insert(a, Value::Int(31), Origin::Declared);
    assert_eq!(
        run(vec![Op::Load(a), int(30), Op::Gt], &facts),
        Ok(Evaluated::Known(Value::Bool(true)))
    );
}

#[test]
fn evaluation_never_panics_on_ill_typed_code() {
    // The type checker makes this unreachable from source; the evaluator still
    // reports rather than panics (docs/03 §8).
    let ill_typed = vec![int(1), Op::Const(Value::Str("x".into())), Op::Add];
    assert_eq!(
        run(ill_typed, &FactSet::new()),
        Err(Code::InternalInvariant)
    );
}
