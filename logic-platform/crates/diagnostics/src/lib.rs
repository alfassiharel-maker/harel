//! Diagnostic infrastructure — error codes, spans, severity levels.
//!
//! LGC01xx  parse errors
//! LGC02xx  semantic errors
//! LGC03xx  logic / runtime errors
//! LGC04xx  type-system errors
//! LGC05xx  internal errors

use serde::Serialize;

use std::fmt;

/// Source location attached to a diagnostic.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
pub struct Span {
    /// Source file path or "<repl>".
    pub source: String,
    /// 1-based line.
    pub line: u32,
    /// 1-based column.
    pub col: u32,
}

impl fmt::Display for Span {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}:{}", self.source, self.line, self.col)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Severity {
    Error,
    Warning,
    Info,
}

/// A typed diagnostic code in the `LGC` namespace.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct DiagnosticCode(pub u32);

impl fmt::Display for DiagnosticCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "LGC{:04}", self.0)
    }
}

// Parse
pub const PARSE_UNEXPECTED_TOKEN: DiagnosticCode = DiagnosticCode(101);
pub const PARSE_UNEXPECTED_EOF: DiagnosticCode = DiagnosticCode(102);
pub const PARSE_INVALID_LITERAL: DiagnosticCode = DiagnosticCode(103);
pub const PARSE_EXPECTED_SEMICOLON: DiagnosticCode = DiagnosticCode(104);

// Semantic
pub const SEMANTIC_UNBOUND_VAR_IN_HEAD: DiagnosticCode = DiagnosticCode(201);
pub const SEMANTIC_ARITY_MISMATCH: DiagnosticCode = DiagnosticCode(202);
pub const SEMANTIC_DUPLICATE_FACT: DiagnosticCode = DiagnosticCode(203);

// Logic / runtime
pub const LOGIC_CONFLICT: DiagnosticCode = DiagnosticCode(301);
pub const LOGIC_FACT_MUTATION: DiagnosticCode = DiagnosticCode(302);

// Type
pub const TYPE_NULL_ARITHMETIC: DiagnosticCode = DiagnosticCode(401);
pub const TYPE_NULL_ORDERING: DiagnosticCode = DiagnosticCode(402);
pub const TYPE_UNKNOWN_IN_ARITHMETIC: DiagnosticCode = DiagnosticCode(403);

#[derive(Debug, Clone)]
pub struct Diagnostic {
    pub code: DiagnosticCode,
    pub severity: Severity,
    pub message: String,
    pub span: Option<Span>,
}

impl fmt::Display for Diagnostic {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let severity = match self.severity {
            Severity::Error => "error",
            Severity::Warning => "warning",
            Severity::Info => "info",
        };
        if let Some(span) = &self.span {
            write!(f, "[{}] {} at {}: {}", self.code, severity, span, self.message)
        } else {
            write!(f, "[{}] {}: {}", self.code, severity, self.message)
        }
    }
}

/// Accumulates diagnostics during a compilation or execution phase.
#[derive(Debug, Default)]
pub struct DiagnosticBag {
    diagnostics: Vec<Diagnostic>,
}

impl DiagnosticBag {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, d: Diagnostic) {
        self.diagnostics.push(d);
    }

    pub fn error(&mut self, code: DiagnosticCode, message: impl Into<String>, span: Option<Span>) {
        self.push(Diagnostic {
            code,
            severity: Severity::Error,
            message: message.into(),
            span,
        });
    }

    pub fn warning(&mut self, code: DiagnosticCode, message: impl Into<String>, span: Option<Span>) {
        self.push(Diagnostic {
            code,
            severity: Severity::Warning,
            message: message.into(),
            span,
        });
    }

    pub fn has_errors(&self) -> bool {
        self.diagnostics
            .iter()
            .any(|d| d.severity == Severity::Error)
    }

    pub fn diagnostics(&self) -> &[Diagnostic] {
        &self.diagnostics
    }

    pub fn into_diagnostics(self) -> Vec<Diagnostic> {
        self.diagnostics
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn code_display() {
        assert_eq!(PARSE_UNEXPECTED_TOKEN.to_string(), "LGC0101");
        assert_eq!(LOGIC_CONFLICT.to_string(), "LGC0301");
    }

    #[test]
    fn bag_has_errors_only_on_error_severity() {
        let mut bag = DiagnosticBag::new();
        bag.warning(SEMANTIC_DUPLICATE_FACT, "dup", None);
        assert!(!bag.has_errors());
        bag.error(PARSE_UNEXPECTED_TOKEN, "tok", None);
        assert!(bag.has_errors());
    }
}
