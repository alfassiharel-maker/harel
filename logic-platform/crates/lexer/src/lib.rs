//! Lexer: source text → token stream.
//!
//! Responsibilities: tokenize only.  No semantics.
//!
//! Language syntax (autonomous decision §41):
//!   fact   parent("Alice", "Bob");
//!   rule   grandparent(X, Z) when parent(X, Y), parent(Y, Z);
//!   query  grandparent(X, Y);
//!
//! Variables are uppercase identifiers.
//! Relation names and keywords are lowercase.
//! Comments: # to end of line.

use diagnostics::{DiagnosticBag, Span, PARSE_INVALID_LITERAL, PARSE_UNEXPECTED_TOKEN};

/// Every distinct token kind.
#[derive(Debug, Clone, PartialEq)]
pub enum TokenKind {
    // Keywords
    KwFact,
    KwRule,
    KwWhen,
    KwQuery,
    KwNull,
    KwUnknown,
    KwTrue,
    KwFalse,
    KwIs,

    // Literals
    IntLit(i64),
    FloatLit(f64),
    StrLit(String),

    // Identifiers
    /// Starts with uppercase — a variable in rule context.
    UpperIdent(String),
    /// Starts with lowercase — a relation name or keyword.
    LowerIdent(String),

    // Punctuation
    LParen,
    RParen,
    Comma,
    Semi,

    /// End of input.
    Eof,
}

#[derive(Debug, Clone)]
pub struct Token {
    pub kind: TokenKind,
    pub span: Span,
}

/// Tokenize `source` and return the token stream.
/// Errors are appended to `bag`; tokenization continues past errors.
pub fn tokenize(source: &str, source_name: &str, bag: &mut DiagnosticBag) -> Vec<Token> {
    let mut tokens = Vec::new();
    let mut chars = source.char_indices().peekable();
    let mut line: u32 = 1;
    let mut line_start: usize = 0;

    macro_rules! span {
        ($byte_pos:expr) => {
            Span {
                source: source_name.to_string(),
                line,
                col: ($byte_pos - line_start + 1) as u32,
            }
        };
    }

    while let Some((pos, ch)) = chars.next() {
        match ch {
            // Whitespace
            '\n' => {
                line += 1;
                line_start = pos + 1;
            }
            ' ' | '\t' | '\r' => {}

            // Comments
            '#' => {
                for (_, c) in chars.by_ref() {
                    if c == '\n' {
                        line += 1;
                        line_start = pos + 1; // approximate — good enough for diagnostics
                        break;
                    }
                }
            }

            // Punctuation
            '(' => tokens.push(Token { kind: TokenKind::LParen, span: span!(pos) }),
            ')' => tokens.push(Token { kind: TokenKind::RParen, span: span!(pos) }),
            ',' => tokens.push(Token { kind: TokenKind::Comma, span: span!(pos) }),
            ';' => tokens.push(Token { kind: TokenKind::Semi, span: span!(pos) }),

            // String literal
            '"' => {
                let start = pos;
                let sp = span!(pos);
                let mut s = String::new();
                let mut closed = false;
                for (_, c) in chars.by_ref() {
                    if c == '"' {
                        closed = true;
                        break;
                    }
                    if c == '\n' {
                        line += 1;
                    }
                    s.push(c);
                }
                if !closed {
                    bag.error(
                        PARSE_INVALID_LITERAL,
                        "unterminated string literal",
                        Some(span!(start)),
                    );
                } else {
                    tokens.push(Token { kind: TokenKind::StrLit(s), span: sp });
                }
            }

            // Numbers
            c if c.is_ascii_digit() || (c == '-' && chars.peek().map(|(_, d)| d.is_ascii_digit()).unwrap_or(false)) => {
                let sp = span!(pos);
                let mut s = c.to_string();
                let mut is_float = false;
                while let Some((_, d)) = chars.peek() {
                    if d.is_ascii_digit() {
                        s.push(*d);
                        chars.next();
                    } else if *d == '.' && !is_float {
                        is_float = true;
                        s.push(*d);
                        chars.next();
                    } else {
                        break;
                    }
                }
                if is_float {
                    match s.parse::<f64>() {
                        Ok(f) => tokens.push(Token { kind: TokenKind::FloatLit(f), span: sp }),
                        Err(_) => bag.error(PARSE_INVALID_LITERAL, format!("invalid float literal: {s}"), Some(sp)),
                    }
                } else {
                    match s.parse::<i64>() {
                        Ok(n) => tokens.push(Token { kind: TokenKind::IntLit(n), span: sp }),
                        Err(_) => bag.error(PARSE_INVALID_LITERAL, format!("invalid integer literal: {s}"), Some(sp)),
                    }
                }
            }

            // Identifiers and keywords
            c if c.is_alphabetic() || c == '_' => {
                let sp = span!(pos);
                let mut s = c.to_string();
                while let Some((_, d)) = chars.peek() {
                    if d.is_alphanumeric() || *d == '_' {
                        s.push(*d);
                        chars.next();
                    } else {
                        break;
                    }
                }
                let kind = match s.as_str() {
                    "fact"    => TokenKind::KwFact,
                    "rule"    => TokenKind::KwRule,
                    "when"    => TokenKind::KwWhen,
                    "query"   => TokenKind::KwQuery,
                    "null"    => TokenKind::KwNull,
                    "unknown" => TokenKind::KwUnknown,
                    "true"    => TokenKind::KwTrue,
                    "false"   => TokenKind::KwFalse,
                    "is"      => TokenKind::KwIs,
                    _ if c.is_uppercase() => TokenKind::UpperIdent(s),
                    _ => TokenKind::LowerIdent(s),
                };
                tokens.push(Token { kind, span: sp });
            }

            other => {
                bag.error(
                    PARSE_UNEXPECTED_TOKEN,
                    format!("unexpected character: {other:?}"),
                    Some(span!(pos)),
                );
            }
        }
    }

    // Compute last position for EOF span
    let eof_span = Span {
        source: source_name.to_string(),
        line,
        col: (source.len().saturating_sub(line_start) + 1) as u32,
    };
    tokens.push(Token { kind: TokenKind::Eof, span: eof_span });
    tokens
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lex(src: &str) -> Vec<TokenKind> {
        let mut bag = DiagnosticBag::new();
        let toks = tokenize(src, "<test>", &mut bag);
        assert!(!bag.has_errors(), "unexpected errors: {:?}", bag.diagnostics());
        toks.into_iter().map(|t| t.kind).collect()
    }

    #[test]
    fn lex_fact_declaration() {
        let kinds = lex(r#"fact parent("Alice", "Bob");"#);
        assert_eq!(
            kinds,
            vec![
                TokenKind::KwFact,
                TokenKind::LowerIdent("parent".into()),
                TokenKind::LParen,
                TokenKind::StrLit("Alice".into()),
                TokenKind::Comma,
                TokenKind::StrLit("Bob".into()),
                TokenKind::RParen,
                TokenKind::Semi,
                TokenKind::Eof,
            ]
        );
    }

    #[test]
    fn lex_rule_with_variables() {
        let kinds = lex("rule grandparent(X, Z) when parent(X, Y), parent(Y, Z);");
        assert!(kinds.contains(&TokenKind::KwRule));
        assert!(kinds.contains(&TokenKind::KwWhen));
        assert!(kinds.contains(&TokenKind::UpperIdent("X".into())));
    }

    #[test]
    fn lex_keywords() {
        let kinds = lex("null unknown true false");
        assert_eq!(
            kinds,
            vec![
                TokenKind::KwNull,
                TokenKind::KwUnknown,
                TokenKind::KwTrue,
                TokenKind::KwFalse,
                TokenKind::Eof,
            ]
        );
    }

    #[test]
    fn comments_are_dropped() {
        let kinds = lex("# this is a comment\nfact a();");
        assert!(!kinds.contains(&TokenKind::LowerIdent("this".into())));
        assert!(kinds.contains(&TokenKind::KwFact));
    }

    #[test]
    fn integer_and_float_literals() {
        let kinds = lex("42 3.14");
        assert_eq!(
            kinds,
            vec![
                TokenKind::IntLit(42),
                TokenKind::FloatLit(3.14),
                TokenKind::Eof,
            ]
        );
    }

    #[test]
    fn unterminated_string_is_an_error() {
        let mut bag = DiagnosticBag::new();
        tokenize(r#"fact a("oops);"#, "<test>", &mut bag);
        assert!(bag.has_errors());
    }
}
