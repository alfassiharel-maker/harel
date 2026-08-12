//! The value model.
//!
//! Owner decision OD-2 and its follow-up define **three** semantically distinct
//! conditions, and they are modelled here exactly as the decision draws them:
//!
//! ```text
//! Value
//! ├── Unknown        not enough information to determine a value
//! ├── Null           known that there is no value
//! └── Known(Known)   a concrete value of one of the four types
//! ```
//!
//! One type, three branches, so the states cannot be confused by construction:
//! there is no way to obtain a value that is "sort of" both, and no encoding
//! step where one collapses into the other.
//!
//! `Known` carries the invariants of `docs/04_TYPE_AND_DATA_MODEL.md`:
//!
//! * a `Float` is always finite — `NaN` and the infinities are not values;
//! * `-0.0` is normalised to `0.0`.
//!
//! With those, equality is total and reflexive over the whole of `Value`, which
//! is what lets it be compared for the conflict check and stored in a map.

use crate::Type;
use core::fmt;

/// A concrete value of one of the four types.
#[derive(Debug, Clone, PartialOrd)]
pub enum Known {
    /// A 64-bit signed integer.
    Int(i64),
    /// A finite IEEE-754 binary64.
    Float(f64),
    /// A boolean.
    Bool(bool),
    /// UTF-8 text.
    Str(String),
}

impl Known {
    /// The type of this value.
    #[must_use]
    pub const fn ty(&self) -> Type {
        match self {
            Self::Int(_) => Type::Int,
            Self::Float(_) => Type::Float,
            Self::Bool(_) => Type::Bool,
            Self::Str(_) => Type::String,
        }
    }
}

impl PartialEq for Known {
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
impl Eq for Known {}

impl fmt::Display for Known {
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

/// One of the three conditions a value can be in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    /// Not enough information to determine a value.
    ///
    /// A property of the environment, not an inhabitant of a type: a name that
    /// nothing has bound reads as `Unknown`. It is never stored in the fact set
    /// — absence from the set *is* `Unknown`.
    Unknown,
    /// Known that there is no value.
    ///
    /// A *known* logical state, not a lack of knowledge. `Null` is stored, is
    /// compared, and answers existence questions.
    Null,
    /// A concrete value.
    Known(Known),
}

impl Value {
    /// An integer value.
    #[must_use]
    pub const fn int(value: i64) -> Self {
        Self::Known(Known::Int(value))
    }

    /// A boolean value.
    #[must_use]
    pub const fn bool(value: bool) -> Self {
        Self::Known(Known::Bool(value))
    }

    /// A string value.
    #[must_use]
    pub fn string(value: impl Into<String>) -> Self {
        Self::Known(Known::Str(value.into()))
    }

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
        Some(Self::Known(Known::Float(if value == 0.0 {
            0.0
        } else {
            value
        })))
    }

    /// The type, if the value is known.
    ///
    /// `Null` has type [`Type::Null`]; `Unknown` has no type, because a value
    /// nobody knows has nothing to have a type.
    #[must_use]
    pub const fn ty(&self) -> Option<Type> {
        match self {
            Self::Unknown => None,
            Self::Null => Some(Type::Null),
            Self::Known(known) => Some(known.ty()),
        }
    }

    /// The concrete value, if there is one.
    #[must_use]
    pub const fn as_known(&self) -> Option<&Known> {
        match self {
            Self::Known(known) => Some(known),
            _ => None,
        }
    }

    /// The boolean, if this is a known boolean.
    #[must_use]
    pub const fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Known(Known::Bool(value)) => Some(*value),
            _ => None,
        }
    }

    /// Whether this is `Unknown`.
    #[must_use]
    pub const fn is_unknown(&self) -> bool {
        matches!(self, Self::Unknown)
    }

    /// Whether this is `Null`.
    #[must_use]
    pub const fn is_null(&self) -> bool {
        matches!(self, Self::Null)
    }

    /// Whether this is a concrete value.
    #[must_use]
    pub const fn is_known(&self) -> bool {
        matches!(self, Self::Known(_))
    }
}

impl From<Known> for Value {
    fn from(known: Known) -> Self {
        Self::Known(known)
    }
}

impl fmt::Display for Value {
    /// The canonical text form, shared by outputs, traces and the IR, so that
    /// one value has exactly one spelling everywhere. `null` and `unknown` are
    /// the source spellings of those two states, so a printed result reads back
    /// as the thing it describes.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unknown => f.write_str("unknown"),
            Self::Null => f.write_str("null"),
            Self::Known(known) => write!(f, "{known}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_three_states_are_distinct() {
        // OD-2: never collapsed. This is that requirement, executable.
        assert_ne!(Value::Unknown, Value::Null);
        assert_ne!(Value::Unknown, Value::bool(false));
        assert_ne!(Value::Null, Value::bool(false));
        assert_ne!(Value::Null, Value::int(0));
        assert_ne!(Value::Null, Value::string(""));
    }

    #[test]
    fn each_state_reports_only_itself() {
        assert!(
            Value::Unknown.is_unknown() && !Value::Unknown.is_null() && !Value::Unknown.is_known()
        );
        assert!(Value::Null.is_null() && !Value::Null.is_unknown() && !Value::Null.is_known());
        assert!(
            Value::int(1).is_known() && !Value::int(1).is_null() && !Value::int(1).is_unknown()
        );
    }

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
    fn equality_is_reflexive_for_every_state() {
        for value in [
            Value::Unknown,
            Value::Null,
            Value::int(1),
            Value::float(1.5).unwrap_or(Value::Null),
            Value::bool(true),
            Value::string("x"),
        ] {
            assert_eq!(value, value.clone());
        }
    }

    #[test]
    fn display_never_blurs_the_states() {
        assert_eq!(Value::Unknown.to_string(), "unknown");
        assert_eq!(Value::Null.to_string(), "null");
        assert_eq!(Value::int(1).to_string(), "1");
        assert_eq!(
            Value::float(1.0).map(|v| v.to_string()),
            Some("1.0".to_owned())
        );
        assert_eq!(Value::string("a\"b").to_string(), r#""a\"b""#);
    }

    #[test]
    fn only_null_and_known_have_types() {
        assert_eq!(Value::Unknown.ty(), None);
        assert_eq!(Value::Null.ty(), Some(Type::Null));
        assert_eq!(Value::int(1).ty(), Some(Type::Int));
    }
}
