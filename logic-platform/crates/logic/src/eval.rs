//! Expression evaluation.
//!
//! Implements `docs/02_FORMAL_SEMANTICS.md` §4.1 exactly, including the part
//! that is easy to get wrong: a name that is not known yields **not evaluable**,
//! which is different from `false`, and it propagates through every operator —
//! `and` and `or` included. Short-circuiting would make a rule's firing depend
//! on the order its author happened to write the operands in.
//!
//! Every arithmetic operation is checked. Nothing here can panic, wrap, or
//! produce a `NaN`.

use crate::facts::FactSet;
use lml_diagnostics::{Code, Diagnostic, Span};
use lml_ir::{ExprCode, Op};
use lml_types::Value;

/// The outcome of evaluating an expression.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Evaluated {
    /// A value.
    Known(Value),
    /// At least one name the expression reads is not known yet. The expression
    /// is neither true nor false: it has no value at all, so far.
    NotEvaluable,
}

impl Evaluated {
    /// The value, if there is one.
    #[must_use]
    pub const fn value(&self) -> Option<&Value> {
        match self {
            Self::Known(value) => Some(value),
            Self::NotEvaluable => None,
        }
    }

    /// Whether this is a known `true`.
    #[must_use]
    pub fn is_true(&self) -> bool {
        matches!(self, Self::Known(Value::Bool(true)))
    }
}

/// Evaluate `code` against `facts`.
///
/// `span` is the source range blamed for a runtime error. Operations do not
/// carry spans — the IR is deliberately position-free — so the whole expression
/// is blamed, which is accurate enough to find the line and honest about the
/// granularity available.
///
/// # Errors
/// A structured runtime diagnostic: overflow, division by zero, a non-finite
/// float, or an internal invariant that the type checker should have made
/// impossible.
pub fn eval(code: &ExprCode, facts: &FactSet, span: Span) -> Result<Evaluated, Diagnostic> {
    let mut stack: Vec<Value> = Vec::with_capacity(code.stack_depth());

    for op in code.ops() {
        // `pop` cannot fail: `ExprCode::new` checked that the sequence is
        // balanced. If it ever does, that is a defect in lowering, and the
        // engine reports it rather than panicking.
        match op {
            Op::Const(value) => stack.push(value.clone()),
            Op::Load(name) => match facts.get(*name) {
                Some(value) => stack.push(value.clone()),
                None => return Ok(Evaluated::NotEvaluable),
            },
            Op::Neg => {
                let operand = pop(&mut stack, span)?;
                stack.push(negate(&operand, span)?);
            }
            Op::Not => {
                let operand = pop(&mut stack, span)?;
                let Some(value) = operand.as_bool() else {
                    return Err(internal(span, "`not` applied to a non-boolean"));
                };
                stack.push(Value::Bool(!value));
            }
            binary => {
                let right = pop(&mut stack, span)?;
                let left = pop(&mut stack, span)?;
                stack.push(apply(binary, &left, &right, span)?);
            }
        }
    }

    match stack.pop() {
        Some(value) if stack.is_empty() => Ok(Evaluated::Known(value)),
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
        Value::Int(value) => value
            .checked_neg()
            .map(Value::Int)
            .ok_or_else(|| overflow(span, "-")),
        Value::Float(value) => float(-value, span),
        _ => Err(internal(span, "`-` applied to a non-numeric value")),
    }
}

fn apply(op: &Op, left: &Value, right: &Value, span: Span) -> Result<Value, Diagnostic> {
    use Value::{Bool, Float, Int, Str};
    match (op, left, right) {
        // ---- arithmetic ----------------------------------------------------
        (Op::Add, Int(a), Int(b)) => a
            .checked_add(*b)
            .map(Int)
            .ok_or_else(|| overflow(span, "+")),
        (Op::Sub, Int(a), Int(b)) => a
            .checked_sub(*b)
            .map(Int)
            .ok_or_else(|| overflow(span, "-")),
        (Op::Mul, Int(a), Int(b)) => a
            .checked_mul(*b)
            .map(Int)
            .ok_or_else(|| overflow(span, "*")),
        (Op::Div, Int(_), Int(0)) => Err(division_by_zero(span)),
        (Op::Rem, Int(_), Int(0)) => Err(division_by_zero(span)),
        // `checked_div` also covers `i64::MIN / -1`, which overflows.
        (Op::Div, Int(a), Int(b)) => a
            .checked_div(*b)
            .map(Int)
            .ok_or_else(|| overflow(span, "/")),
        (Op::Rem, Int(a), Int(b)) => a
            .checked_rem(*b)
            .map(Int)
            .ok_or_else(|| overflow(span, "%")),

        (Op::Add, Float(a), Float(b)) => float(a + b, span),
        (Op::Sub, Float(a), Float(b)) => float(a - b, span),
        (Op::Mul, Float(a), Float(b)) => float(a * b, span),
        (Op::Div | Op::Rem, Float(_), Float(b)) if *b == 0.0 => Err(division_by_zero(span)),
        (Op::Div, Float(a), Float(b)) => float(a / b, span),
        (Op::Rem, Float(a), Float(b)) => float(a % b, span),

        (Op::Add, Str(a), Str(b)) => Ok(Str(format!("{a}{b}"))),

        // ---- comparison ----------------------------------------------------
        (Op::Eq, a, b) => Ok(Bool(a == b)),
        (Op::Ne, a, b) => Ok(Bool(a != b)),
        (Op::Lt, Int(a), Int(b)) => Ok(Bool(a < b)),
        (Op::Le, Int(a), Int(b)) => Ok(Bool(a <= b)),
        (Op::Gt, Int(a), Int(b)) => Ok(Bool(a > b)),
        (Op::Ge, Int(a), Int(b)) => Ok(Bool(a >= b)),
        (Op::Lt, Float(a), Float(b)) => Ok(Bool(a < b)),
        (Op::Le, Float(a), Float(b)) => Ok(Bool(a <= b)),
        (Op::Gt, Float(a), Float(b)) => Ok(Bool(a > b)),
        (Op::Ge, Float(a), Float(b)) => Ok(Bool(a >= b)),
        (Op::Lt, Str(a), Str(b)) => Ok(Bool(a < b)),
        (Op::Le, Str(a), Str(b)) => Ok(Bool(a <= b)),
        (Op::Gt, Str(a), Str(b)) => Ok(Bool(a > b)),
        (Op::Ge, Str(a), Str(b)) => Ok(Bool(a >= b)),

        // ---- logic ---------------------------------------------------------
        (Op::And, Bool(a), Bool(b)) => Ok(Bool(*a && *b)),
        (Op::Or, Bool(a), Bool(b)) => Ok(Bool(*a || *b)),

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
