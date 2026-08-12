//! Names.

use lml_diagnostics::Span;

/// The longest name the language accepts, in characters.
///
/// Bounds diagnostic rendering and interning on adversarial input. A name this
/// long is not readable by a person, which is the point of a name.
pub const MAX_NAME_LENGTH: usize = 255;

/// A dotted identifier as written, with its source location.
///
/// The dot has no structural meaning in 0.1: `user.age` is one atomic name, not
/// field access (`docs/01_LANGUAGE_CONSTITUTION.md` §3.1). Comparison is by text
/// and is case-sensitive.
#[derive(Debug, Clone)]
pub struct Name {
    /// The name as written.
    pub text: String,
    /// Where it was written.
    pub span: Span,
}

impl Name {
    /// A name with its span.
    #[must_use]
    pub fn new(text: impl Into<String>, span: Span) -> Self {
        Self {
            text: text.into(),
            span,
        }
    }
}

/// Two names are equal when their text is equal; the span is provenance, not
/// identity. Without this, the same name written twice would not be the same
/// name.
impl PartialEq for Name {
    fn eq(&self, other: &Self) -> bool {
        self.text == other.text
    }
}

impl Eq for Name {}

impl core::fmt::Display for Name {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(&self.text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_ignores_position() {
        assert_eq!(
            Name::new("a", Span::new(0, 1)),
            Name::new("a", Span::new(40, 41))
        );
        assert_ne!(
            Name::new("a", Span::new(0, 1)),
            Name::new("A", Span::new(0, 1))
        );
    }
}
