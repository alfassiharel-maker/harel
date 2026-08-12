//! Expression evaluation over the three-state value model.
//!
//! Implements `docs/VALUE_AND_LOGIC_TRUTH_TABLE.md` exactly. The two rules that
//! matter, and that the rest follows from:
//!
//! * **`Unknown` propagates.** Any operand that is `Unknown` makes the whole
//!   expression `Unknown` — including `and` and `or`, which therefore do not
//!   short-circuit. Short-circuiting would make a rule's firing depend on the
//!   order its author happened to write the operands in.
//! * **`Null` answers existence questions and refuses everything else.**
//!   `==` and `!=` treat it as ordinary information: `null == null` is `true`,
//!   `null == 5` is `false`. Ordering, arithmetic and the logical operators
//!   reject it with a structured diagnostic, because an absence has no
//!   magnitude, no quantity and no truth value.
//!
//! The state predicates `is null` / `is unknown` / `is known` are total: they
//! are the way a program copes with the strictness above.
//!
//! Every arithmetic operation is checked. Nothing here can panic, wrap, or
//! produce a `NaN`.

use crate::facts::FactSet;
use lml_diagnostics::{Code, Diagnostic, Span};
use lml_ir::{ExprCode, Op};
use lml_types::{Known, Value};

/// Evaluate `code` against `facts`.
///
/// The result is a [`Value`], so *not evaluable* is not a separate channel: it
/// is [`Value::Unknown`], which is exactly what it means.
///
/// `span` is the source range blamed for a runtime error. Operations do not
/// carry spans — the IR is deliberately position-free — so the whole expression
/// is blamed, which is accurate enough to find the line and honest about the
/// granularity available.
///
/// # Errors
/// A structured runtime diagnostic: overflow, division by zero, a non-finite
/// float, misuse of `null`, or an internal invariant the type checker should
/// have made impossible.
pub fn eval(code: &ExprCode, facts: &FactSet, span: Span) -> Result<Value, Diagnostic> {
    let mut stack: Vec<Value> = Vec::with_capacity(code.stack_depth());

    for op in code.ops() {
        match op {
            Op::Const(value) => stack.push(value.clone()),
            // A name nothing has bound reads as `Unknown` — absence from the
            // fact set *is* the third state, not a special case beside it.
            Op::Load(name) => stack.push(facts.get(*name).cloned().unwrap_or(Value::Unknown)),
            Op::IsNull | Op::IsUnknown | Op::IsKnown => {
                let operand = pop(&mut stack, span)?;
                let answer = match op {
                    Op::IsNull => operand.is_null(),
                    Op::IsUnknown => operand.is_unknown(),
                    _ => operand.is_known(),
                };
                stack.push(Value::bool(answer));
            }
            Op::Neg => {
                let operand = pop(&mut stack, span)?;
                stack.push(negate(&operand, span)?);
            }
            Op::Not => {
                let operand = pop(&mut stack, span)?;
                stack.push(negate_truth(&operand, span)?);
            }
            binary => {
                let right = pop(&mut stack, span)?;
                let left = pop(&mut stack, span)?;
                stack.push(apply(binary, &left, &right, span)?);
            }
        }
    }

    match stack.pop() {
        Some(value) if stack.is_empty() => Ok(value),
        _ => Err(internal(
            span,
            "expression did not evaluate to exactly one value",
        )),
    }
}

fn pop(stack: &mut Vec<Value>, span: Span) -> Result<Value, Diagnostic> {
    stack
        .pop()
        .ok_or_else(|| internal(span, "expression ran out of operands"))
}

/// An engine defect, reported instead of panicked (`docs/03_EXECUTION_MODEL.md` §8).
fn internal(span: Span, what: &str) -> Diagnostic {
    Diagnostic::new(
        Code::InternalInvariant,
        span,
        format!("internal invariant violated: {what}"),
    )
    .with_help("this is a defect in the engine, not in the program; please report it")
}

fn negate(operand: &Value, span: Span) -> Result<Value, Diagnostic> {
    match operand {
        Value::Unknown => Ok(Value::Unknown),
        Value::Null => Err(null_arithmetic(span, "-")),
        Value::Known(Known::Int(value)) => value
            .checked_neg()
            .map(Value::int)
            .ok_or_else(|| overflow(span, "-")),
        Value::Known(Known::Float(value)) => float(-value, span),
        Value::Known(_) => Err(internal(span, "`-` applied to a non-numeric value")),
    }
}

fn negate_truth(operand: &Value, span: Span) -> Result<Value, Diagnostic> {
    match operand {
        Value::Unknown => Ok(Value::Unknown),
        Value::Null => Err(null_boolean(span, "not")),
        Value::Known(Known::Bool(value)) => Ok(Value::bool(!value)),
        Value::Known(_) => Err(internal(span, "`not` applied to a non-boolean")),
    }
}

fn apply(op: &Op, left: &Value, right: &Value, span: Span) -> Result<Value, Diagnostic> {
    // 1. `Unknown` propagates through every operator, including `and` and `or`.
    if left.is_unknown() || right.is_unknown() {
        return Ok(Value::Unknown);
    }

    // 2. Equality is total over the remaining states: `null` is information,
    //    and asking whether a value is absent always has an answer.
    match op {
        Op::Eq => return Ok(Value::bool(left == right)),
        Op::Ne => return Ok(Value::bool(left != right)),
        _ => {}
    }

    // 3. Every other operator refuses `null`.
    if left.is_null() || right.is_null() {
        return Err(match op {
            Op::Lt | Op::Le | Op::Gt | Op::Ge => null_ordering(span, op.mnemonic()),
            Op::And | Op::Or => null_boolean(span, op.mnemonic()),
            _ => null_arithmetic(span, op.mnemonic()),
        });
    }

    let (Some(left), Some(right)) = (left.as_known(), right.as_known()) else {
        return Err(internal(
            span,
            "operand was neither known, null nor unknown",
        ));
    };
    apply_known(op, left, right, span)
}

fn apply_known(op: &Op, left: &Known, right: &Known, span: Span) -> Result<Value, Diagnostic> {
    use Known::{Bool, Float, Int, Str};
    match (op, left, right) {
        // ---- arithmetic ----------------------------------------------------
        (Op::Add, Int(a), Int(b)) => a
            .checked_add(*b)
            .map(Value::int)
            .ok_or_else(|| overflow(span, "+")),
        (Op::Sub, Int(a), Int(b)) => a
            .checked_sub(*b)
            .map(Value::int)
            .ok_or_else(|| overflow(span, "-")),
        (Op::Mul, Int(a), Int(b)) => a
            .checked_mul(*b)
            .map(Value::int)
            .ok_or_else(|| overflow(span, "*")),
        (Op::Div | Op::Rem, Int(_), Int(0)) => Err(division_by_zero(span)),
        // `checked_div` also covers `i64::MIN / -1`, which overflows.
        (Op::Div, Int(a), Int(b)) => a
            .checked_div(*b)
            .map(Value::int)
            .ok_or_else(|| overflow(span, "/")),
        (Op::Rem, Int(a), Int(b)) => a
            .checked_rem(*b)
            .map(Value::int)
            .ok_or_else(|| overflow(span, "%")),

        (Op::Add, Float(a), Float(b)) => float(a + b, span),
        (Op::Sub, Float(a), Float(b)) => float(a - b, span),
        (Op::Mul, Float(a), Float(b)) => float(a * b, span),
        (Op::Div | Op::Rem, Float(_), Float(b)) if *b == 0.0 => Err(division_by_zero(span)),
        (Op::Div, Float(a), Float(b)) => float(a / b, span),
        (Op::Rem, Float(a), Float(b)) => float(a % b, span),

        (Op::Add, Str(a), Str(b)) => Ok(Value::string(format!("{a}{b}"))),

        // ---- ordering ------------------------------------------------------
        (Op::Lt, Int(a), Int(b)) => Ok(Value::bool(a < b)),
        (Op::Le, Int(a), Int(b)) => Ok(Value::bool(a <= b)),
        (Op::Gt, Int(a), Int(b)) => Ok(Value::bool(a > b)),
        (Op::Ge, Int(a), Int(b)) => Ok(Value::bool(a >= b)),
        (Op::Lt, Float(a), Float(b)) => Ok(Value::bool(a < b)),
        (Op::Le, Float(a), Float(b)) => Ok(Value::bool(a <= b)),
        (Op::Gt, Float(a), Float(b)) => Ok(Value::bool(a > b)),
        (Op::Ge, Float(a), Float(b)) => Ok(Value::bool(a >= b)),
        (Op::Lt, Str(a), Str(b)) => Ok(Value::bool(a < b)),
        (Op::Le, Str(a), Str(b)) => Ok(Value::bool(a <= b)),
        (Op::Gt, Str(a), Str(b)) => Ok(Value::bool(a > b)),
        (Op::Ge, Str(a), Str(b)) => Ok(Value::bool(a >= b)),

        // ---- logic ---------------------------------------------------------
        (Op::And, Bool(a), Bool(b)) => Ok(Value::bool(*a && *b)),
        (Op::Or, Bool(a), Bool(b)) => Ok(Value::bool(*a || *b)),

        // The type checker accepted only the combinations above, so anything
        // else means lowering produced code that does not match its types.
        (op, a, b) => Err(internal(
            span,
            &format!(
                "`{}` applied to `{}` and `{}`",
                op.mnemonic(),
                a.ty(),
                b.ty()
            ),
        )),
    }
}

/// Wrap a float result, rejecting non-finite values rather than letting a `NaN`
/// or an infinity into the fact set (`docs/04_TYPE_AND_DATA_MODEL.md` §1).
fn float(value: f64, span: Span) -> Result<Value, Diagnostic> {
    Value::float(value).ok_or_else(|| {
        Diagnostic::new(
            Code::NonFiniteFloat,
            span,
            "this float operation produced a value that is not finite",
        )
        .with_help("`Float` holds finite values only; there is no NaN and no infinity")
    })
}

fn overflow(span: Span, op: &str) -> Diagnostic {
    Diagnostic::new(
        Code::IntegerOverflow,
        span,
        format!("`{op}` left the range of `Int`"),
    )
    .with_help("`Int` holds -9223372036854775808 to 9223372036854775807")
}

fn division_by_zero(span: Span) -> Diagnostic {
    Diagnostic::new(Code::DivisionByZero, span, "division by zero")
        .with_help("the engine refuses rather than producing an infinity or an undefined value")
}

fn null_ordering(span: Span, op: &str) -> Diagnostic {
    Diagnostic::new(
        Code::NullNotOrdered,
        span,
        format!("`{op}` compares magnitudes, and one side is `null`"),
    )
    .with_cause("`null` records that a value is absent, so it has no magnitude to compare")
    .with_help("test for absence with `is null`, or derive a value before comparing")
}

fn null_arithmetic(span: Span, op: &str) -> Diagnostic {
    Diagnostic::new(
        Code::NullNotArithmetic,
        span,
        format!("`{op}` needs a quantity, and one side is `null`"),
    )
    .with_cause("`null` records that a value is absent, so there is nothing to compute with")
    .with_help("guard the rule with `is known`, or derive a value first")
}

fn null_boolean(span: Span, op: &str) -> Diagnostic {
    Diagnostic::new(
        Code::NullNotBoolean,
        span,
        format!("`{op}` needs a truth value, and one side is `null`"),
    )
    .with_cause("`null` is neither true nor false; treating it as either would collapse two states the language keeps distinct")
    .with_help("test for absence explicitly with `is null` or `is known`")
}
