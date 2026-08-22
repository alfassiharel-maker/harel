//! Parser: token stream → AST.
//!
//! No runtime, database, or UI dependencies.  Errors are collected in a
//! `DiagnosticBag`; parsing continues past recoverable errors.

use ast::{AtomNode, Decl, FactDecl, Program, QueryDecl, RuleDecl, TermNode};
use diagnostics::{
    DiagnosticBag, Span, PARSE_EXPECTED_SEMICOLON, PARSE_UNEXPECTED_EOF, PARSE_UNEXPECTED_TOKEN,
};
use lexer::{Token, TokenKind};

struct Parser<'a> {
    tokens: &'a [Token],
    pos: usize,
    bag: &'a mut DiagnosticBag,
    source_name: String,
}

impl<'a> Parser<'a> {
    fn new(tokens: &'a [Token], source_name: &str, bag: &'a mut DiagnosticBag) -> Self {
        Parser {
            tokens,
            pos: 0,
            bag,
            source_name: source_name.to_string(),
        }
    }

    fn peek(&self) -> &Token {
        &self.tokens[self.pos]
    }

    fn advance(&mut self) -> &Token {
        let tok = &self.tokens[self.pos];
        if self.pos + 1 < self.tokens.len() {
            self.pos += 1;
        }
        tok
    }

    fn at_eof(&self) -> bool {
        matches!(self.peek().kind, TokenKind::Eof)
    }

    fn expect_semi(&mut self) {
        if matches!(self.peek().kind, TokenKind::Semi) {
            self.advance();
        } else {
            self.bag.error(
                PARSE_EXPECTED_SEMICOLON,
                "expected ';'",
                Some(self.peek().span.clone()),
            );
        }
    }

    /// Parse: relation_name "(" term* ")"
    fn parse_atom(&mut self) -> Option<AtomNode> {
        let start_span = self.peek().span.clone();

        let relation = match &self.peek().kind {
            TokenKind::LowerIdent(name) => {
                let name = name.clone();
                self.advance();
                name
            }
            TokenKind::Eof => {
                self.bag
                    .error(PARSE_UNEXPECTED_EOF, "expected relation name", Some(start_span));
                return None;
            }
            _ => {
                self.bag.error(
                    PARSE_UNEXPECTED_TOKEN,
                    format!("expected relation name (lowercase identifier), got {:?}", self.peek().kind),
                    Some(start_span),
                );
                self.advance();
                return None;
            }
        };

        // "("
        if !matches!(self.peek().kind, TokenKind::LParen) {
            self.bag.error(
                PARSE_UNEXPECTED_TOKEN,
                "expected '(' after relation name",
                Some(self.peek().span.clone()),
            );
            return None;
        }
        self.advance();

        // arg list
        let mut args = Vec::new();
        while !matches!(self.peek().kind, TokenKind::RParen | TokenKind::Eof) {
            if let Some(term) = self.parse_term() {
                args.push(term);
            } else {
                break;
            }
            if matches!(self.peek().kind, TokenKind::Comma) {
                self.advance();
            }
        }

        // ")"
        if matches!(self.peek().kind, TokenKind::RParen) {
            self.advance();
        } else {
            self.bag.error(
                PARSE_UNEXPECTED_TOKEN,
                "expected ')'",
                Some(self.peek().span.clone()),
            );
        }

        let _end_span = self.peek().span.clone();
        Some(AtomNode {
            relation,
            args,
            span: Span {
                source: self.source_name.clone(),
                line: start_span.line,
                col: start_span.col,
            },
        })
    }

    fn parse_term(&mut self) -> Option<TermNode> {
        let tok = self.peek().clone();
        match &tok.kind {
            TokenKind::UpperIdent(name) => {
                let name = name.clone();
                self.advance();
                Some(TermNode::Variable { name, span: tok.span })
            }
            TokenKind::StrLit(s) => {
                let s = s.clone();
                self.advance();
                Some(TermNode::ConstStr { value: s, span: tok.span })
            }
            TokenKind::IntLit(n) => {
                let n = *n;
                self.advance();
                Some(TermNode::ConstInt { value: n, span: tok.span })
            }
            TokenKind::FloatLit(f) => {
                let f = *f;
                self.advance();
                Some(TermNode::ConstFloat { value: f, span: tok.span })
            }
            TokenKind::KwTrue => {
                self.advance();
                Some(TermNode::ConstBool { value: true, span: tok.span })
            }
            TokenKind::KwFalse => {
                self.advance();
                Some(TermNode::ConstBool { value: false, span: tok.span })
            }
            TokenKind::KwNull => {
                self.advance();
                Some(TermNode::Null { span: tok.span })
            }
            TokenKind::KwUnknown => {
                self.advance();
                Some(TermNode::Unknown { span: tok.span })
            }
            TokenKind::Eof => {
                self.bag
                    .error(PARSE_UNEXPECTED_EOF, "expected term", Some(tok.span));
                None
            }
            _ => {
                self.bag.error(
                    PARSE_UNEXPECTED_TOKEN,
                    format!("expected term, got {:?}", tok.kind),
                    Some(tok.span),
                );
                self.advance();
                None
            }
        }
    }

    /// Parse: "fact" atom ";"
    fn parse_fact(&mut self, start_span: Span) -> Option<Decl> {
        let atom = self.parse_atom()?;
        self.expect_semi();
        Some(Decl::Fact(FactDecl {
            span: start_span,
            atom,
        }))
    }

    /// Parse: "rule" head_atom "when" body_atom ("," body_atom)* ";"
    fn parse_rule(&mut self, start_span: Span) -> Option<Decl> {
        let head = self.parse_atom()?;

        if !matches!(self.peek().kind, TokenKind::KwWhen) {
            self.bag.error(
                PARSE_UNEXPECTED_TOKEN,
                "expected 'when' after rule head",
                Some(self.peek().span.clone()),
            );
            return None;
        }
        self.advance(); // consume 'when'

        let mut body = Vec::new();
        loop {
            if let Some(atom) = self.parse_atom() {
                body.push(atom);
            } else {
                break;
            }
            if matches!(self.peek().kind, TokenKind::Comma) {
                self.advance();
            } else {
                break;
            }
        }

        if body.is_empty() {
            self.bag.error(
                PARSE_UNEXPECTED_TOKEN,
                "rule body must have at least one predicate",
                Some(self.peek().span.clone()),
            );
            return None;
        }

        self.expect_semi();
        Some(Decl::Rule(RuleDecl { head, body, span: start_span }))
    }

    /// Parse: "query" atom ";"
    fn parse_query(&mut self, start_span: Span) -> Option<Decl> {
        let atom = self.parse_atom()?;
        self.expect_semi();
        Some(Decl::Query(QueryDecl { atom, span: start_span }))
    }

    fn parse_program(&mut self) -> Program {
        let mut decls = Vec::new();

        while !self.at_eof() {
            let tok = self.advance().clone();
            let decl = match &tok.kind {
                TokenKind::KwFact => self.parse_fact(tok.span),
                TokenKind::KwRule => self.parse_rule(tok.span),
                TokenKind::KwQuery => self.parse_query(tok.span),
                TokenKind::Eof => break,
                _ => {
                    self.bag.error(
                        PARSE_UNEXPECTED_TOKEN,
                        format!("expected 'fact', 'rule', or 'query', got {:?}", tok.kind),
                        Some(tok.span),
                    );
                    None
                }
            };
            if let Some(d) = decl {
                decls.push(d);
            }
        }

        Program {
            decls,
            source_name: self.source_name.clone(),
        }
    }
}

/// Parse a token stream into a `Program`.  Errors go to `bag`.
pub fn parse(tokens: &[Token], source_name: &str, bag: &mut DiagnosticBag) -> Program {
    let mut parser = Parser::new(tokens, source_name, bag);
    parser.parse_program()
}

/// Convenience: lex + parse in one call.
pub fn parse_source(source: &str, source_name: &str, bag: &mut DiagnosticBag) -> Program {
    let tokens = lexer::tokenize(source, source_name, bag);
    parse(&tokens, source_name, bag)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_ok(src: &str) -> Program {
        let mut bag = DiagnosticBag::new();
        let prog = parse_source(src, "<test>", &mut bag);
        assert!(
            !bag.has_errors(),
            "unexpected errors: {:?}",
            bag.diagnostics()
        );
        prog
    }

    #[test]
    fn parse_single_fact() {
        let prog = parse_ok(r#"fact parent("Alice", "Bob");"#);
        assert_eq!(prog.decls.len(), 1);
        assert!(matches!(prog.decls[0], Decl::Fact(_)));
        let facts: Vec<_> = prog.facts().collect();
        assert_eq!(facts[0].atom.relation, "parent");
        assert_eq!(facts[0].atom.args.len(), 2);
    }

    #[test]
    fn parse_rule_with_two_body_atoms() {
        let prog = parse_ok(
            "rule grandparent(X, Z) when parent(X, Y), parent(Y, Z);",
        );
        assert_eq!(prog.decls.len(), 1);
        let rules: Vec<_> = prog.rules().collect();
        assert_eq!(rules[0].head.relation, "grandparent");
        assert_eq!(rules[0].body.len(), 2);
    }

    #[test]
    fn parse_query() {
        let prog = parse_ok("query grandparent(X, Y);");
        let queries: Vec<_> = prog.queries().collect();
        assert_eq!(queries[0].atom.relation, "grandparent");
    }

    #[test]
    fn parse_full_grandparent_program() {
        let src = r#"
            fact parent("Alice", "Bob");
            fact parent("Bob", "Charlie");
            rule grandparent(X, Z) when parent(X, Y), parent(Y, Z);
            query grandparent(X, Y);
        "#;
        let prog = parse_ok(src);
        assert_eq!(prog.facts().count(), 2);
        assert_eq!(prog.rules().count(), 1);
        assert_eq!(prog.queries().count(), 1);
    }

    #[test]
    fn variable_terms_are_parsed_correctly() {
        let prog = parse_ok("rule a(X) when b(X);");
        let rules: Vec<_> = prog.rules().collect();
        assert!(matches!(rules[0].head.args[0], TermNode::Variable { .. }));
    }

    #[test]
    fn null_and_unknown_terms() {
        let prog = parse_ok(r#"fact status(null, unknown);"#);
        let facts: Vec<_> = prog.facts().collect();
        assert!(matches!(facts[0].atom.args[0], TermNode::Null { .. }));
        assert!(matches!(facts[0].atom.args[1], TermNode::Unknown { .. }));
    }
}
