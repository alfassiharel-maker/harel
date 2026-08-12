//! Types and operator typing.
//!
//! This crate answers one question — *what type does this produce?* — and never
//! computes a value. It is the single source of the operator table in
//! `docs/04_TYPE_AND_DATA_MODEL.md` §5; the evaluator in `lml-logic`
//! implements exactly the combinations `binary_result` accepts, and a test in
//! that crate checks the two agree.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod value;

pub use value::Value;

use core::fmt;
use lml_ast::{BinaryOp, Literal, UnaryOp};

/// The types of LML 0.1. The set is closed: `docs/04_TYPE_AND_DATA_MODEL.md` §2
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
        }
    }

    /// Whether arithmetic is defined on the type.
    #[must_use]
    pub const fn is_numeric(self) -> bool {
        matches!(self, Self::Int | Self::Float)
    }

    /// Whether `<`, `<=`, `>`, `>=` are defined on the type.
    #[must_use]
    pub const fn is_ordered(self) -> bool {
        matches!(self, Self::Int | Self::Float | Self::String)
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
/// There is no implicit conversion, so both operands must already have the same
/// type: `1 + 1.0` has no result type (`docs/04_TYPE_AND_DATA_MODEL.md` §3).
#[must_use]
pub fn binary_result(op: BinaryOp, left: Type, right: Type) -> Option<Type> {
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
    fn plus_concatenates_strings() {
        assert_eq!(
            binary_result(BinaryOp::Add, Type::String, Type::String),
            Some(Type::String)
        );
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
