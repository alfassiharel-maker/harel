//! Three-state value model: Unknown / Null / Known(KnownValue).
//!
//! Foundational invariant (§5.3, §6): these states must never be collapsed.
//!
//! Null equality semantics (§7):
//!   Null == Null       → Equal
//!   Null == Known(x)   → NotEqual
//!   involving Unknown  → Indeterminate
//!
//! Null arithmetic and Null ordering produce SemanticError (§7).

use std::fmt;
use std::hash::{Hash, Hasher};
use serde::{Deserialize, Serialize};

/// A concrete, fully-known value.  Cannot be Null or Unknown.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum KnownValue {
    Int(i64),
    /// Float identity uses bit-level representation for Hash/Eq.
    Float(f64),
    Bool(bool),
    Str(String),
}

impl Eq for KnownValue {}

impl Hash for KnownValue {
    fn hash<H: Hasher>(&self, state: &mut H) {
        match self {
            KnownValue::Int(n) => {
                0u8.hash(state);
                n.hash(state);
            }
            KnownValue::Float(f) => {
                1u8.hash(state);
                f.to_bits().hash(state);
            }
            KnownValue::Bool(b) => {
                2u8.hash(state);
                b.hash(state);
            }
            KnownValue::Str(s) => {
                3u8.hash(state);
                s.hash(state);
            }
        }
    }
}

impl fmt::Display for KnownValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            KnownValue::Int(n) => write!(f, "{n}"),
            KnownValue::Float(x) => write!(f, "{x}"),
            KnownValue::Bool(b) => write!(f, "{b}"),
            KnownValue::Str(s) => write!(f, "\"{s}\""),
        }
    }
}

/// The three-state value domain of the language (§6).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Value {
    /// System does not have enough information to determine the value.
    Unknown,
    /// System explicitly knows no value is present.
    Null,
    /// A concrete value is known.
    Known(KnownValue),
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Value::Unknown => write!(f, "unknown"),
            Value::Null => write!(f, "null"),
            Value::Known(v) => write!(f, "{v}"),
        }
    }
}

impl Value {
    pub fn is_unknown(&self) -> bool {
        matches!(self, Value::Unknown)
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }

    pub fn is_known(&self) -> bool {
        matches!(self, Value::Known(_))
    }

    pub fn as_known(&self) -> Option<&KnownValue> {
        if let Value::Known(v) = self {
            Some(v)
        } else {
            None
        }
    }
}

/// Result of an equality comparison under the language's semantics (§7).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EqualityResult {
    Equal,
    NotEqual,
    /// At least one operand is Unknown — cannot determine.
    Indeterminate,
}

pub fn value_eq(a: &Value, b: &Value) -> EqualityResult {
    match (a, b) {
        (Value::Unknown, _) | (_, Value::Unknown) => EqualityResult::Indeterminate,
        (Value::Null, Value::Null) => EqualityResult::Equal,
        (Value::Null, Value::Known(_)) | (Value::Known(_), Value::Null) => EqualityResult::NotEqual,
        (Value::Known(x), Value::Known(y)) => {
            if x == y {
                EqualityResult::Equal
            } else {
                EqualityResult::NotEqual
            }
        }
    }
}

/// Errors that arise from type-level semantic violations (§7).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SemanticTypeError {
    #[error("arithmetic on null is a semantic error (LGC0401)")]
    NullArithmetic,
    #[error("ordering comparison on null is a semantic error (LGC0402)")]
    NullOrdering,
    #[error("arithmetic on unknown is a semantic error (LGC0403)")]
    UnknownArithmetic,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn null_eq_null() {
        assert_eq!(value_eq(&Value::Null, &Value::Null), EqualityResult::Equal);
    }

    #[test]
    fn null_neq_known() {
        assert_eq!(
            value_eq(&Value::Null, &Value::Known(KnownValue::Int(0))),
            EqualityResult::NotEqual
        );
    }

    #[test]
    fn unknown_is_indeterminate() {
        assert_eq!(
            value_eq(&Value::Unknown, &Value::Null),
            EqualityResult::Indeterminate
        );
        assert_eq!(
            value_eq(&Value::Known(KnownValue::Bool(true)), &Value::Unknown),
            EqualityResult::Indeterminate
        );
    }

    #[test]
    fn known_values_equal_by_content() {
        let a = Value::Known(KnownValue::Str("Alice".to_string()));
        let b = Value::Known(KnownValue::Str("Alice".to_string()));
        let c = Value::Known(KnownValue::Str("Bob".to_string()));
        assert_eq!(value_eq(&a, &b), EqualityResult::Equal);
        assert_eq!(value_eq(&a, &c), EqualityResult::NotEqual);
    }

    #[test]
    fn value_state_predicates() {
        assert!(Value::Unknown.is_unknown());
        assert!(!Value::Unknown.is_null());
        assert!(!Value::Unknown.is_known());
        assert!(Value::Null.is_null());
        assert!(Value::Known(KnownValue::Int(1)).is_known());
    }

    #[test]
    fn float_hashes_by_bits() {
        use std::collections::HashSet;
        let mut set = HashSet::new();
        set.insert(Value::Known(KnownValue::Float(1.0)));
        assert!(set.contains(&Value::Known(KnownValue::Float(1.0))));
        assert!(!set.contains(&Value::Known(KnownValue::Float(2.0))));
    }
}
