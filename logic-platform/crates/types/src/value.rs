//! Values.
//!
//! A value is what a fact holds. Construction is the only place where the
//! invariants of `docs/04_TYPE_AND_DATA_MODEL.md` are established, so they hold
//! everywhere else by construction:
//!
//! * a `Float` is always finite — `NaN` and the infinities are not values;
//! * `-0.0` is normalised to `0.0`, so the fact set cannot hold two zeroes that
//!   compare equal but print differently.
//!
//! With those two invariants, equality is total and reflexive, which is what
//! lets a `Value` be compared for the conflict check and be used as a map key.

use crate::Type;
use core::fmt;

/// A value of one of the four types.
#[derive(Debug, Clone, PartialOrd)]
pub enum Value {
    /// A 64-bit signed integer.
    Int(i64),
    /// A finite IEEE-754 binary64.
    Float(f64),
    /// A boolean.
    Bool(bool),
    /// UTF-8 text.
    Str(String),
}

impl Value {
    /// A float value, or `None` if it is not finite.
    ///
    /// Every arithmetic result goes through here, which is how `E5003` can be
    /// reported instead of a `NaN` escaping into the fact set.
    #[must_use]
    pub fn float(value: f64) -> Option<Self> {
        if !value.is_finite() {
            return None;
        }
        // `+0.0` and `-0.0` are equal but not interchangeable when printed.
        Some(Self::Float(if value == 0.0 { 0.0 } else { value }))
    }

    /// The value's type.
    #[must_use]
    pub const fn ty(&self) -> Type {
        match self {
            Self::Int(_) => Type::Int,
            Self::Float(_) => Type::Float,
            Self::Bool(_) => Type::Bool,
            Self::Str(_) => Type::String,
        }
    }

    /// The integer, if this is one.
    #[must_use]
    pub const fn as_int(&self) -> Option<i64> {
        match self {
            Self::Int(value) => Some(*value),
            _ => None,
        }
    }

    /// The boolean, if this is one.
    #[must_use]
    pub const fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(value) => Some(*value),
            _ => None,
        }
    }
}

/// Equality within a type. Values of different types are never equal — the
/// static checks make the comparison impossible in a program, but the runtime's
/// conflict check compares whatever it is given.
impl PartialEq for Value {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Int(left), Self::Int(right)) => left == right,
            (Self::Float(left), Self::Float(right)) => left == right,
            (Self::Bool(left), Self::Bool(right)) => left == right,
            (Self::Str(left), Self::Str(right)) => left == right,
            _ => false,
        }
    }
}

/// Sound because a `Float` is always finite: with `NaN` excluded, `==` is
/// reflexive.
impl Eq for Value {}

impl fmt::Display for Value {
    /// The canonical text form, shared by outputs, traces and the IR, so that
    /// one value has exactly one spelling everywhere.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Int(value) => write!(f, "{value}"),
            Self::Float(value) => {
                let text = value.to_string();
                if text.contains('.') {
                    f.write_str(&text)
                } else {
                    write!(f, "{text}.0")
                }
            }
            Self::Bool(value) => write!(f, "{value}"),
            Self::Str(value) => f.write_str(&lml_ast::print_string(value)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_finite_floats_are_not_values() {
        assert_eq!(Value::float(f64::NAN), None);
        assert_eq!(Value::float(f64::INFINITY), None);
        assert_eq!(Value::float(f64::NEG_INFINITY), None);
        assert!(Value::float(1.5).is_some());
    }

    #[test]
    fn negative_zero_is_normalised() {
        assert_eq!(
            Value::float(-0.0).map(|v| v.to_string()),
            Some("0.0".to_owned())
        );
    }

    #[test]
    fn equality_is_reflexive_for_every_value() {
        for value in [
            Value::Int(1),
            Value::Float(1.5),
            Value::Bool(true),
            Value::Str("x".into()),
        ] {
            assert_eq!(value, value.clone());
        }
    }

    #[test]
    fn values_of_different_types_are_not_equal() {
        assert_ne!(Value::Int(1), Value::Float(1.0));
        assert_ne!(Value::Bool(true), Value::Int(1));
    }

    #[test]
    fn display_never_blurs_int_and_float() {
        assert_eq!(Value::Int(1).to_string(), "1");
        assert_eq!(Value::Float(1.0).to_string(), "1.0");
        assert_eq!(Value::Str("a\"b".into()).to_string(), r#""a\"b""#);
    }
}
