//! Token definitions.
//!
//! A token is a lexical shape and a span. It carries no meaning: `TokenKind::Fact`
//! records that the word `fact` was written, not that a declaration follows.

use core::fmt;
use lml_diagnostics::Span;

/// What a token is.
#[derive(Debug, Clone, PartialEq)]
pub enum TokenKind {
    // ---- keywords ----------------------------------------------------------
    /// `fact`
    Fact,
    /// `rule`
    Rule,
    /// `when`
    When,
    /// `then`
    Then,
    /// `output`
    Output,
    /// `and`
    And,
    /// `or`
    Or,
    /// `not`
    Not,
    /// `is` — introduces a state predicate, `x is null`.
    Is,
    /// `null` — the known-absence literal, and the operand of `is null`.
    Null,
    /// `unknown` — the operand of `is unknown`. Not a literal: see
    /// [`lml_ast::Literal::Null`] for why there is no `unknown` value.
    Unknown,
    /// `known` — the operand of `is known`.
    Known,

    // ---- literals ----------------------------------------------------------
    /// A dotted identifier, e.g. `user.age`.
    Ident(String),
    /// An integer literal, already parsed and range-checked.
    Int(i64),
    /// A float literal, already parsed and checked finite.
    Float(f64),
    /// A string literal, with escapes already resolved.
    Str(String),
    /// `true` or `false`.
    Bool(bool),

    // ---- operators ---------------------------------------------------------
    /// `=`
    Assign,
    /// `==`
    Eq,
    /// `!=`
    Ne,
    /// `<`
    Lt,
    /// `<=`
    Le,
    /// `>`
    Gt,
    /// `>=`
    Ge,
    /// `+`
    Plus,
    /// `-`
    Minus,
    /// `*`
    Star,
    /// `/`
    Slash,
    /// `%`
    Percent,

    // ---- punctuation -------------------------------------------------------
    /// `(`
    LParen,
    /// `)`
    RParen,
    /// `:`
    Colon,
    /// `,`
    Comma,

    /// End of input. Emitted exactly once, last.
    Eof,
}

impl TokenKind {
    /// How the token is written, for use in diagnostics.
    ///
    /// Literals describe their shape rather than their value, because a
    /// diagnostic that says "expected an expression, found `31`" reads worse
    /// than one that says "found an integer literal".
    #[must_use]
    pub fn describe(&self) -> String {
        match self {
            Self::Fact => "`fact`".into(),
            Self::Rule => "`rule`".into(),
            Self::When => "`when`".into(),
            Self::Then => "`then`".into(),
            Self::Output => "`output`".into(),
            Self::And => "`and`".into(),
            Self::Or => "`or`".into(),
            Self::Not => "`not`".into(),
            Self::Is => "`is`".into(),
            Self::Null => "`null`".into(),
            Self::Unknown => "`unknown`".into(),
            Self::Known => "`known`".into(),
            Self::Ident(name) => format!("name `{name}`"),
            Self::Int(_) => "an integer literal".into(),
            Self::Float(_) => "a float literal".into(),
            Self::Str(_) => "a string literal".into(),
            Self::Bool(_) => "a boolean literal".into(),
            Self::Assign => "`=`".into(),
            Self::Eq => "`==`".into(),
            Self::Ne => "`!=`".into(),
            Self::Lt => "`<`".into(),
            Self::Le => "`<=`".into(),
            Self::Gt => "`>`".into(),
            Self::Ge => "`>=`".into(),
            Self::Plus => "`+`".into(),
            Self::Minus => "`-`".into(),
            Self::Star => "`*`".into(),
            Self::Slash => "`/`".into(),
            Self::Percent => "`%`".into(),
            Self::LParen => "`(`".into(),
            Self::RParen => "`)`".into(),
            Self::Colon => "`:`".into(),
            Self::Comma => "`,`".into(),
            Self::Eof => "end of input".into(),
        }
    }

    /// Whether this token starts a top-level declaration.
    ///
    /// Declarations have no terminator (`docs/02_FORMAL_SEMANTICS.md` §2), so
    /// the parser needs to know where an expression must stop. Because every
    /// keyword is reserved and none can continue an expression, this test is
    /// sufficient.
    #[must_use]
    pub const fn starts_declaration(&self) -> bool {
        matches!(self, Self::Fact | Self::Rule | Self::Output)
    }
}

/// A token and where it came from.
#[derive(Debug, Clone, PartialEq)]
pub struct Token {
    /// The lexical shape.
    pub kind: TokenKind,
    /// The source range it covers.
    pub span: Span,
}

impl Token {
    /// A token of `kind` covering `span`.
    #[must_use]
    pub const fn new(kind: TokenKind, span: Span) -> Self {
        Self { kind, span }
    }
}

impl fmt::Display for TokenKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.describe())
    }
}

/// Words that are reserved for future language versions.
///
/// They are rejected as identifiers so that introducing them later cannot break
/// an existing program (`docs/02_FORMAL_SEMANTICS.md` §1.4).
pub const RESERVED_WORDS: &[&str] = &[
    "assert", "derive", "else", "fn", "for", "if", "import", "in", "let", "macro", "match",
    "module", "query", "return", "state", "type", "while",
];

/// The keyword for `word`, if it is one.
#[must_use]
pub fn keyword(word: &str) -> Option<TokenKind> {
    match word {
        "fact" => Some(TokenKind::Fact),
        "rule" => Some(TokenKind::Rule),
        "when" => Some(TokenKind::When),
        "then" => Some(TokenKind::Then),
        "output" => Some(TokenKind::Output),
        "and" => Some(TokenKind::And),
        "or" => Some(TokenKind::Or),
        "not" => Some(TokenKind::Not),
        "is" => Some(TokenKind::Is),
        "null" => Some(TokenKind::Null),
        "unknown" => Some(TokenKind::Unknown),
        "known" => Some(TokenKind::Known),
        "true" => Some(TokenKind::Bool(true)),
        "false" => Some(TokenKind::Bool(false)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserved_words_are_sorted_and_unique() {
        // Sorted so that the list stays reviewable as it grows.
        let mut sorted = RESERVED_WORDS.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted, RESERVED_WORDS);
    }

    #[test]
    fn no_word_is_both_keyword_and_reserved() {
        for word in RESERVED_WORDS {
            assert!(
                keyword(word).is_none(),
                "`{word}` is both a keyword and reserved"
            );
        }
    }
}
