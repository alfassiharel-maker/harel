//! Types, values, and operator typing.
//!
//! This crate answers *what type does this produce?* and holds the value model
//! it produces values in. It never evaluates. It is the single source of the
//! operator table in `docs/04_TYPE_AND_DATA_MODEL.md` §5; the evaluator in
//! `lml-logic` implements exactly the combinations `binary_result` accepts, and
//! a test there checks the two agree.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod value;

pub use value::{Known, Value};

use core::fmt;
use lml_ast::{BinaryOp, Literal, UnaryOp};

/// The types of LML. The set is closed: `docs/04_TYPE_AND_DATA_MODEL.md` §2
/// lists what is deliberately absent and why.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Type {
    /// 64-bit signed integer.
    Int,
    /// Finite IEEE-754 binary64.
    Float,
    /// `true` or `false`.
    Bool,
    /// UTF-8 text.
    String,
    /// The type of the `null` literal: a *known absence*.
    ///
    /// `Null` is not a nullability modifier — there is no `Int?`, per owner
    /// decision O2.1. It is the type of one literal, and it [unifies](Type::unify)
    /// with every other type so that a name written by one rule as `null` and by
    /// another as an `Int` has a single, useful type.
    Null,
}

impl Type {
    /// The name used in diagnostics and in the IR.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Int => "Int",
            Self::Float => "Float",
            Self::Bool => "Bool",
            Self::String => "String",
            Self::Null => "Null",
        }
    }

    /// Whether arithmetic is defined on the type.
    #[must_use]
    pub const fn is_numeric(self) -> bool {
        matches!(self, Self::Int | Self::Float)
    }

    /// Whether `<`, `<=`, `>`, `>=` are defined on the type.
    ///
    /// `Null` is not ordered: it records existence, not magnitude
    /// (`docs/VALUE_AND_LOGIC_TRUTH_TABLE.md` §6).
    #[must_use]
    pub const fn is_ordered(self) -> bool {
        matches!(self, Self::Int | Self::Float | Self::String)
    }

    /// The single type covering both, if there is one.
    ///
    /// `Null` unifies with everything, because a name that is sometimes absent
    /// and sometimes an `Int` is an `Int` that can be absent — which is exactly
    /// what the three-state value model expresses. Two different concrete types
    /// do not unify; that is `E3010`.
    #[must_use]
    pub const fn unify(self, other: Self) -> Option<Self> {
        match (self, other) {
            (Self::Null, ty) | (ty, Self::Null) => Some(ty),
            (Self::Int, Self::Int) => Some(Self::Int),
            (Self::Float, Self::Float) => Some(Self::Float),
            (Self::Bool, Self::Bool) => Some(Self::Bool),
            (Self::String, Self::String) => Some(Self::String),
            _ => None,
        }
    }
}

impl fmt::Display for Type {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The type of a literal.
#[must_use]
pub const fn type_of_literal(literal: &Literal) -> Type {
    match literal {
        Literal::Int(_) => Type::Int,
        Literal::Float(_) => Type::Float,
        Literal::Bool(_) => Type::Bool,
        Literal::Str(_) => Type::String,
        Literal::Null => Type::Null,
    }
}

/// The result type of a prefix operator, or `None` if it is not defined for the
/// operand type.
#[must_use]
pub const fn unary_result(op: UnaryOp, operand: Type) -> Option<Type> {
    match (op, operand) {
        (UnaryOp::Neg, Type::Int) => Some(Type::Int),
        (UnaryOp::Neg, Type::Float) => Some(Type::Float),
        (UnaryOp::Not, Type::Bool) => Some(Type::Bool),
        _ => None,
    }
}

/// The result type of an infix operator, or `None` if it is not defined for the
/// operand types.
///
/// The rules, from `docs/VALUE_AND_LOGIC_TRUTH_TABLE.md`:
///
/// * `==` and `!=` accept `Null` against anything — asking whether a value is
///   absent is always a legitimate question, and the answer is a `Bool`;
/// * ordering, arithmetic and the logical operators reject `Null`, because
///   absence has no magnitude, no quantity and no truth value;
/// * otherwise both operands must already have the same type: there is no
///   implicit conversion, so `1 + 1.0` has no result type.
#[must_use]
pub fn binary_result(op: BinaryOp, left: Type, right: Type) -> Option<Type> {
    // Existence questions are total: `x == null` type-checks whatever `x` is.
    if matches!(op, BinaryOp::Eq | BinaryOp::Ne) && (left == Type::Null || right == Type::Null) {
        return Some(Type::Bool);
    }
    if left != right {
        return None;
    }
    let operand = left;
    match op {
        BinaryOp::Add if operand == Type::String => Some(Type::String),
        BinaryOp::Add | BinaryOp::Sub | BinaryOp::Mul | BinaryOp::Div | BinaryOp::Rem => {
            operand.is_numeric().then_some(operand)
        }
        BinaryOp::Eq | BinaryOp::Ne => Some(Type::Bool),
        BinaryOp::Lt | BinaryOp::Le | BinaryOp::Gt | BinaryOp::Ge => {
            operand.is_ordered().then_some(Type::Bool)
        }
        BinaryOp::And | BinaryOp::Or => (operand == Type::Bool).then_some(Type::Bool),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arithmetic_needs_matching_numeric_operands() {
        assert_eq!(
            binary_result(BinaryOp::Add, Type::Int, Type::Int),
            Some(Type::Int)
        );
        assert_eq!(
            binary_result(BinaryOp::Add, Type::Float, Type::Float),
            Some(Type::Float)
        );
        assert_eq!(binary_result(BinaryOp::Add, Type::Int, Type::Float), None);
        assert_eq!(
            binary_result(BinaryOp::Sub, Type::String, Type::String),
            None
        );
    }

    #[test]
    fn equality_accepts_null_against_anything() {
        // "is this absent?" is always a fair question.
        assert_eq!(
            binary_result(BinaryOp::Eq, Type::Int, Type::Null),
            Some(Type::Bool)
        );
        assert_eq!(
            binary_result(BinaryOp::Ne, Type::Null, Type::String),
            Some(Type::Bool)
        );
        assert_eq!(
            binary_result(BinaryOp::Eq, Type::Null, Type::Null),
            Some(Type::Bool)
        );
    }

    #[test]
    fn null_has_no_magnitude_no_quantity_and_no_truth() {
        assert_eq!(binary_result(BinaryOp::Lt, Type::Int, Type::Null), None);
        assert_eq!(binary_result(BinaryOp::Add, Type::Int, Type::Null), None);
        assert_eq!(binary_result(BinaryOp::And, Type::Bool, Type::Null), None);
        assert_eq!(unary_result(UnaryOp::Not, Type::Null), None);
        assert_eq!(unary_result(UnaryOp::Neg, Type::Null), None);
    }

    #[test]
    fn null_unifies_with_every_type() {
        assert_eq!(Type::Null.unify(Type::Int), Some(Type::Int));
        assert_eq!(Type::Int.unify(Type::Null), Some(Type::Int));
        assert_eq!(Type::Null.unify(Type::Null), Some(Type::Null));
        assert_eq!(Type::Int.unify(Type::Int), Some(Type::Int));
        assert_eq!(Type::Int.unify(Type::String), None);
    }

    #[test]
    fn equality_is_within_a_type_only() {
        assert_eq!(
            binary_result(BinaryOp::Eq, Type::Int, Type::Int),
            Some(Type::Bool)
        );
        // `1 == 1.0` has no type: cross-type equality that silently answers
        // `false` hides bugs (docs/04 §6).
        assert_eq!(binary_result(BinaryOp::Eq, Type::Int, Type::Float), None);
    }

    #[test]
    fn ordering_is_not_defined_on_bool() {
        assert_eq!(binary_result(BinaryOp::Lt, Type::Bool, Type::Bool), None);
        assert_eq!(
            binary_result(BinaryOp::Lt, Type::String, Type::String),
            Some(Type::Bool)
        );
    }

    #[test]
    fn there_is_no_truthiness() {
        assert_eq!(binary_result(BinaryOp::And, Type::Int, Type::Int), None);
        assert_eq!(unary_result(UnaryOp::Not, Type::Int), None);
    }
}
