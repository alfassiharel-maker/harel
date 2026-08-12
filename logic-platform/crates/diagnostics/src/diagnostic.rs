//! The diagnostic type every stage of the pipeline reports with.

use crate::code::Code;
use crate::span::{LineColumn, Span};
use core::fmt;

/// A secondary location that helps explain the primary one, e.g. the first
/// declaration of a name that was declared twice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Label {
    /// Where the related code is.
    pub span: Span,
    /// What is notable about it.
    pub message: String,
}

/// One error, with everything the Master Specification §25 requires: a code, a
/// location, a message, context and — where one exists — a suggestion.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Diagnostic {
    /// The stable identity of the error.
    pub code: Code,
    /// Where the error is.
    pub span: Span,
    /// What went wrong, in one sentence, with no trailing period.
    pub message: String,
    /// Related locations.
    pub labels: Vec<Label>,
    /// What produced the error, when that is a different thing from what the
    /// message states — the value that was `null`, the operator that rejected
    /// it, the rule that was running. Required by Master Spec §25.
    pub cause: Option<String>,
    /// What the author could do about it, if that is knowable.
    pub help: Option<String>,
}

impl Diagnostic {
    /// A diagnostic with a code, a span and a message.
    #[must_use]
    pub fn new(code: Code, span: Span, message: impl Into<String>) -> Self {
        Self {
            code,
            span,
            message: message.into(),
            labels: Vec::new(),
            cause: None,
            help: None,
        }
    }

    /// Attach a related location.
    #[must_use]
    pub fn with_label(mut self, span: Span, message: impl Into<String>) -> Self {
        self.labels.push(Label {
            span,
            message: message.into(),
        });
        self
    }

    /// Attach the cause — the state of the world that produced the error.
    #[must_use]
    pub fn with_cause(mut self, cause: impl Into<String>) -> Self {
        self.cause = Some(cause.into());
        self
    }

    /// Attach a suggested resolution.
    #[must_use]
    pub fn with_help(mut self, help: impl Into<String>) -> Self {
        self.help = Some(help.into());
        self
    }

    /// Render against the source text it refers to: one line per location, in
    /// the `path:line:column: CODE message` shape editors and CI already parse.
    #[must_use]
    pub fn render(&self, source: &str, path: &str) -> String {
        let at = LineColumn::resolve(source, self.span.start);
        let mut out = format!(
            "{path}:{}:{}: {} {}",
            at.line, at.column, self.code, self.message
        );
        for label in &self.labels {
            let at = LineColumn::resolve(source, label.span.start);
            out.push_str(&format!(
                "\n  {path}:{}:{}: {}",
                at.line, at.column, label.message
            ));
        }
        if let Some(cause) = &self.cause {
            out.push_str(&format!("\n  cause: {cause}"));
        }
        if let Some(help) = &self.help {
            out.push_str(&format!("\n  help: {help}"));
        }
        out
    }
}

impl fmt::Display for Diagnostic {
    /// Without the source text, only the code and message can be shown.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {}", self.code, self.message)
    }
}

impl std::error::Error for Diagnostic {}

/// A non-empty collection of diagnostics.
///
/// Stages report *every* problem they find rather than stopping at the first,
/// so the caller can fix a file in one pass.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Diagnostics(Vec<Diagnostic>);

impl Diagnostics {
    /// An empty collection.
    #[must_use]
    pub const fn new() -> Self {
        Self(Vec::new())
    }

    /// Add one.
    pub fn push(&mut self, diagnostic: Diagnostic) {
        self.0.push(diagnostic);
    }

    /// Move all of `other` into `self`.
    pub fn extend(&mut self, other: Self) {
        self.0.extend(other.0);
    }

    /// Whether anything was reported.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// How many were reported.
    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// The diagnostics, in the order they were reported.
    #[must_use]
    pub fn as_slice(&self) -> &[Diagnostic] {
        &self.0
    }

    /// `Ok(value)` if nothing was reported, otherwise `Err(self)`.
    ///
    /// # Errors
    /// Returns the collected diagnostics when any were reported.
    pub fn into_result<T>(self, value: T) -> Result<T, Self> {
        if self.is_empty() {
            Ok(value)
        } else {
            Err(self)
        }
    }

    /// Sort by source position, so output order does not depend on the order
    /// the checks happen to run in. Determinism applies to diagnostics too.
    pub fn sort_by_position(&mut self) {
        self.0.sort_by_key(|d| (d.span.start, d.span.end, d.code));
    }

    /// Render every diagnostic against its source.
    #[must_use]
    pub fn render(&self, source: &str, path: &str) -> String {
        self.0
            .iter()
            .map(|d| d.render(source, path))
            .collect::<Vec<_>>()
            .join("\n")
    }
}

impl From<Diagnostic> for Diagnostics {
    fn from(diagnostic: Diagnostic) -> Self {
        Self(vec![diagnostic])
    }
}

impl IntoIterator for Diagnostics {
    type Item = Diagnostic;
    type IntoIter = std::vec::IntoIter<Diagnostic>;
    fn into_iter(self) -> Self::IntoIter {
        self.0.into_iter()
    }
}

impl fmt::Display for Diagnostics {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (index, diagnostic) in self.0.iter().enumerate() {
            if index > 0 {
                writeln!(f)?;
            }
            write!(f, "{diagnostic}")?;
        }
        Ok(())
    }
}

impl std::error::Error for Diagnostics {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_points_at_line_and_column() {
        let source = "fact a = 1\nfact a = 2\n";
        let diagnostic =
            Diagnostic::new(Code::DuplicateFact, Span::new(16, 17), "duplicate fact `a`")
                .with_label(Span::new(5, 6), "first declared here")
                .with_help("remove one of the declarations");
        assert_eq!(
            diagnostic.render(source, "x.lml"),
            "x.lml:2:6: E3001 duplicate fact `a`\n  \
             x.lml:1:6: first declared here\n  \
             help: remove one of the declarations"
        );
    }

    #[test]
    fn into_result_distinguishes_empty() {
        assert_eq!(Diagnostics::new().into_result(7), Ok(7));
        let mut diagnostics = Diagnostics::new();
        diagnostics.push(Diagnostic::new(Code::UnknownToken, Span::point(0), "x"));
        assert!(diagnostics.into_result(7).is_err());
    }

    #[test]
    fn sorting_is_by_position_then_code() {
        let mut diagnostics = Diagnostics::new();
        diagnostics.push(Diagnostic::new(Code::UnknownName, Span::new(10, 11), "b"));
        diagnostics.push(Diagnostic::new(Code::UnknownToken, Span::new(2, 3), "a"));
        diagnostics.sort_by_position();
        assert_eq!(diagnostics.as_slice()[0].message, "a");
    }
}
