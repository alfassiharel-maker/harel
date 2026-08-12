//! Recursive-descent parser for the grammar in `grammar.ebnf`.
//!
//! Hand-written rather than generated: the grammar is small, LL(1) except for
//! one lookahead in `primary`, and a hand-written parser produces far better
//! diagnostics than a generator's "unexpected token" — which matters more here
//! than saving 400 lines. If the grammar grows past what one file can hold
//! clearly, that trade-off is worth revisiting (`docs/OPEN_DESIGN_DECISIONS.md` N).
//!
//! On an error the parser records a diagnostic and resynchronises at the next
//! declaration keyword, so one run reports every declaration's problems.

use lml_ast::{
    BinaryOp, Effect, Expr, FactDecl, Item, Literal, Name, OutputDecl, Program, RuleDecl,
    StateTest, UnaryOp,
};
use lml_diagnostics::{Code, Diagnostic, Diagnostics, Span};
use lml_lexer::{Token, TokenKind};

/// Deepest an expression may nest.
///
/// This is a security boundary, not a style limit: recursive descent uses the
/// native stack, and adversarial input like 100_000 nested parentheses would
/// overflow it. A stack overflow is a crash — no diagnostic, no recovery — so
/// the depth is bounded and reported as `E2003` instead. 128 is far beyond any
/// expression a person writes and far below the ~10k frames a default 8 MiB
/// stack allows for these frame sizes.
pub const MAX_EXPR_DEPTH: usize = 128;

/// Parse a token stream into a [`Program`].
///
/// # Errors
/// Returns every syntax diagnostic found, in source order.
pub fn parse(tokens: &[Token]) -> Result<Program, Diagnostics> {
    Parser {
        tokens,
        pos: 0,
        depth: 0,
        diagnostics: Diagnostics::new(),
    }
    .run()
}

struct Parser<'a> {
    tokens: &'a [Token],
    pos: usize,
    depth: usize,
    diagnostics: Diagnostics,
}

/// A parse step that failed. The diagnostic is already recorded; this only
/// tells the caller to stop and resynchronise.
struct Failed;

type Parsed<T> = Result<T, Failed>;

impl<'a> Parser<'a> {
    fn run(mut self) -> Result<Program, Diagnostics> {
        let mut items = Vec::new();
        while !self.at_end() {
            match self.item() {
                Ok(item) => items.push(item),
                Err(Failed) => self.recover(),
            }
        }
        self.diagnostics.sort_by_position();
        self.diagnostics.into_result(Program { items })
    }

    // ---- token access ------------------------------------------------------

    fn peek(&self) -> &TokenKind {
        self.tokens
            .get(self.pos)
            .map_or(&TokenKind::Eof, |token| &token.kind)
    }

    fn span(&self) -> Span {
        self.tokens.get(self.pos).map_or_else(
            || {
                self.tokens
                    .last()
                    .map_or(Span::point(0), |token| token.span)
            },
            |token| token.span,
        )
    }

    fn at_end(&self) -> bool {
        matches!(self.peek(), TokenKind::Eof)
    }

    fn advance(&mut self) -> TokenKind {
        let kind = self.peek().clone();
        if !self.at_end() {
            self.pos += 1;
        }
        kind
    }

    fn eat(&mut self, expected: &TokenKind) -> bool {
        if self.peek() == expected {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    /// Consume `expected` or report what was found instead.
    fn expect(&mut self, expected: &TokenKind, context: &str) -> Parsed<Span> {
        let span = self.span();
        if self.eat(expected) {
            return Ok(span);
        }
        let found = self.peek().describe();
        let code = if self.at_end() {
            Code::UnexpectedEndOfInput
        } else {
            Code::UnexpectedToken
        };
        self.fail(Diagnostic::new(
            code,
            span,
            format!("expected {} {context}, found {found}", expected.describe()),
        ))
    }

    fn expect_name(&mut self, context: &str) -> Parsed<Name> {
        let span = self.span();
        if let TokenKind::Ident(text) = self.peek().clone() {
            self.pos += 1;
            if text.chars().count() > lml_ast::MAX_NAME_LENGTH {
                return self.fail(Diagnostic::new(
                    Code::UnexpectedToken,
                    span,
                    format!(
                        "name is longer than {} characters",
                        lml_ast::MAX_NAME_LENGTH
                    ),
                ));
            }
            return Ok(Name::new(text, span));
        }
        let found = self.peek().describe();
        self.fail(Diagnostic::new(
            Code::UnexpectedToken,
            span,
            format!("expected a name {context}, found {found}"),
        ))
    }

    fn fail<T>(&mut self, diagnostic: Diagnostic) -> Parsed<T> {
        self.diagnostics.push(diagnostic);
        Err(Failed)
    }

    /// Skip to the start of the next declaration, so one bad declaration does
    /// not suppress the diagnostics of every later one.
    fn recover(&mut self) {
        while !self.at_end() && !self.peek().starts_declaration() {
            self.pos += 1;
        }
    }

    // ---- declarations ------------------------------------------------------

    fn item(&mut self) -> Parsed<Item> {
        match self.peek() {
            TokenKind::Fact => self.fact_decl().map(Item::Fact),
            TokenKind::Rule => self.rule_decl().map(Item::Rule),
            TokenKind::Output => self.output_decl().map(Item::Output),
            other => {
                let found = other.describe();
                let span = self.span();
                self.advance();
                self.fail(
                    Diagnostic::new(
                        Code::UnexpectedToken,
                        span,
                        format!("expected a declaration, found {found}"),
                    )
                    .with_help("a program is made of `fact`, `rule` and `output` declarations"),
                )
            }
        }
    }

    fn fact_decl(&mut self) -> Parsed<FactDecl> {
        let start = self.span();
        self.pos += 1; // `fact`
        let name = self.expect_name("after `fact`")?;
        self.expect(&TokenKind::Assign, "after the fact's name")?;
        let value = self.expr()?;
        Ok(FactDecl {
            span: start.merge(value.span()),
            name,
            value,
        })
    }

    fn rule_decl(&mut self) -> Parsed<RuleDecl> {
        let start = self.span();
        self.pos += 1; // `rule`
        let name = self.expect_name("after `rule`")?;
        self.expect(&TokenKind::Colon, "after the rule's name")?;
        self.expect(&TokenKind::When, "to start the rule's condition")?;
        let condition = self.expr()?;

        let mut effects = Vec::new();
        while self.peek() == &TokenKind::Then {
            let then_span = self.span();
            self.pos += 1;
            let name = self.expect_name("after `then`")?;
            self.expect(&TokenKind::Assign, "after the derived name")?;
            let value = self.expr()?;
            effects.push(Effect {
                span: then_span.merge(value.span()),
                name,
                value,
            });
        }

        if effects.is_empty() {
            let span = start.merge(condition.span());
            return self.fail(
                Diagnostic::new(
                    Code::RuleWithoutThen,
                    span,
                    format!("rule `{name}` has no `then` clause"),
                )
                .with_help("a rule that derives nothing has no effect; add `then <name> = <expr>`"),
            );
        }

        let end = effects
            .last()
            .map_or(condition.span(), |effect| effect.span);
        Ok(RuleDecl {
            span: start.merge(end),
            name,
            condition,
            effects,
        })
    }

    fn output_decl(&mut self) -> Parsed<OutputDecl> {
        let start = self.span();
        self.pos += 1; // `output`
        let mut names = vec![self.expect_name("after `output`")?];
        while self.eat(&TokenKind::Comma) {
            names.push(self.expect_name("after `,`")?);
        }
        let end = names.last().map_or(start, |name| name.span);
        Ok(OutputDecl {
            span: start.merge(end),
            names,
        })
    }

    // ---- expressions -------------------------------------------------------

    fn expr(&mut self) -> Parsed<Expr> {
        self.depth += 1;
        if self.depth > MAX_EXPR_DEPTH {
            let span = self.span();
            self.depth -= 1;
            return self.fail(
                Diagnostic::new(
                    Code::ExpressionTooDeep,
                    span,
                    format!("expression nests deeper than {MAX_EXPR_DEPTH}"),
                )
                .with_help("split it into several rules"),
            );
        }
        let result = self.or_expr();
        self.depth -= 1;
        result
    }

    fn or_expr(&mut self) -> Parsed<Expr> {
        let mut left = self.and_expr()?;
        while self.eat(&TokenKind::Or) {
            let right = self.and_expr()?;
            left = binary(BinaryOp::Or, left, right);
        }
        Ok(left)
    }

    fn and_expr(&mut self) -> Parsed<Expr> {
        let mut left = self.not_expr()?;
        while self.eat(&TokenKind::And) {
            let right = self.not_expr()?;
            left = binary(BinaryOp::And, left, right);
        }
        Ok(left)
    }

    fn not_expr(&mut self) -> Parsed<Expr> {
        if self.peek() == &TokenKind::Not {
            let start = self.span();
            self.pos += 1;
            self.depth += 1;
            if self.depth > MAX_EXPR_DEPTH {
                self.depth -= 1;
                return self.fail(Diagnostic::new(
                    Code::ExpressionTooDeep,
                    start,
                    format!("expression nests deeper than {MAX_EXPR_DEPTH}"),
                ));
            }
            let operand = self.not_expr();
            self.depth -= 1;
            let operand = operand?;
            let span = start.merge(operand.span());
            return Ok(Expr::Unary {
                op: UnaryOp::Not,
                operand: Box::new(operand),
                span,
            });
        }
        self.comparison()
    }

    fn comparison(&mut self) -> Parsed<Expr> {
        let left = self.additive()?;

        // `x is null` — an existence predicate. It sits at comparison level
        // because it answers the same kind of question and, like a comparison,
        // does not chain: `a is null is known` does not parse.
        if self.peek() == &TokenKind::Is {
            self.pos += 1;
            let span = self.span();
            let test = match self.peek() {
                TokenKind::Null => StateTest::Null,
                TokenKind::Unknown => StateTest::Unknown,
                TokenKind::Known => StateTest::Known,
                other => {
                    let found = other.describe();
                    return self.fail(
                        Diagnostic::new(
                            Code::UnexpectedToken,
                            span,
                            format!(
                                "expected `null`, `unknown` or `known` after `is`, found {found}"
                            ),
                        )
                        .with_help(
                            "the state predicates are `is null`, `is unknown` and `is known`",
                        ),
                    );
                }
            };
            self.pos += 1;
            let whole = left.span().merge(span);
            return Ok(Expr::Is {
                operand: Box::new(left),
                test,
                span: whole,
            });
        }

        let Some(op) = comparison_op(self.peek()) else {
            return Ok(left);
        };
        self.pos += 1;
        let right = self.additive()?;

        // §2 — comparison does not chain. Catching it here, with a suggestion,
        // is the difference between a clear error and a confusing type error
        // about comparing a `Bool` with an `Int`.
        if let Some(second) = comparison_op(self.peek()) {
            let span = self.span();
            return self.fail(
                Diagnostic::new(
                    Code::ChainedComparison,
                    span,
                    format!("`{}` cannot follow another comparison", second.symbol()),
                )
                .with_help("write `a < b and b < c` instead of `a < b < c`"),
            );
        }

        Ok(binary(op, left, right))
    }

    fn additive(&mut self) -> Parsed<Expr> {
        let mut left = self.multiplicative()?;
        loop {
            let op = match self.peek() {
                TokenKind::Plus => BinaryOp::Add,
                TokenKind::Minus => BinaryOp::Sub,
                _ => return Ok(left),
            };
            self.pos += 1;
            let right = self.multiplicative()?;
            left = binary(op, left, right);
        }
    }

    fn multiplicative(&mut self) -> Parsed<Expr> {
        let mut left = self.unary()?;
        loop {
            let op = match self.peek() {
                TokenKind::Star => BinaryOp::Mul,
                TokenKind::Slash => BinaryOp::Div,
                TokenKind::Percent => BinaryOp::Rem,
                _ => return Ok(left),
            };
            self.pos += 1;
            let right = self.unary()?;
            left = binary(op, left, right);
        }
    }

    fn unary(&mut self) -> Parsed<Expr> {
        if self.peek() == &TokenKind::Minus {
            let start = self.span();
            self.pos += 1;
            self.depth += 1;
            if self.depth > MAX_EXPR_DEPTH {
                self.depth -= 1;
                return self.fail(Diagnostic::new(
                    Code::ExpressionTooDeep,
                    start,
                    format!("expression nests deeper than {MAX_EXPR_DEPTH}"),
                ));
            }
            let operand = self.unary();
            self.depth -= 1;
            let operand = operand?;
            let span = start.merge(operand.span());
            return Ok(Expr::Unary {
                op: UnaryOp::Neg,
                operand: Box::new(operand),
                span,
            });
        }
        self.primary()
    }

    fn primary(&mut self) -> Parsed<Expr> {
        let span = self.span();
        let literal = match self.peek().clone() {
            TokenKind::Null => Some(Literal::Null),
            TokenKind::Int(value) => Some(Literal::Int(value)),
            TokenKind::Float(value) => Some(Literal::Float(value)),
            TokenKind::Bool(value) => Some(Literal::Bool(value)),
            TokenKind::Str(value) => Some(Literal::Str(value)),
            _ => None,
        };
        if let Some(value) = literal {
            self.pos += 1;
            return Ok(Expr::Literal { value, span });
        }

        if let TokenKind::Ident(text) = self.peek().clone() {
            self.pos += 1;
            return Ok(Expr::Name(Name::new(text, span)));
        }

        if self.eat(&TokenKind::LParen) {
            let inner = self.expr()?;
            self.expect(&TokenKind::RParen, "to close the group")?;
            return Ok(inner);
        }

        let found = self.peek().describe();
        let code = if self.at_end() {
            Code::UnexpectedEndOfInput
        } else {
            Code::UnexpectedToken
        };
        let diagnostic =
            Diagnostic::new(code, span, format!("expected an expression, found {found}"));
        // A declaration keyword here means the previous declaration was cut
        // short, which is a much more useful thing to say.
        let diagnostic = if self.peek().starts_declaration() {
            diagnostic.with_help("the previous declaration is missing its value")
        } else {
            diagnostic
        };
        self.fail(diagnostic)
    }
}

fn binary(op: BinaryOp, left: Expr, right: Expr) -> Expr {
    let span = left.span().merge(right.span());
    Expr::Binary {
        op,
        left: Box::new(left),
        right: Box::new(right),
        span,
    }
}

fn comparison_op(kind: &TokenKind) -> Option<BinaryOp> {
    match kind {
        TokenKind::Eq => Some(BinaryOp::Eq),
        TokenKind::Ne => Some(BinaryOp::Ne),
        TokenKind::Lt => Some(BinaryOp::Lt),
        TokenKind::Le => Some(BinaryOp::Le),
        TokenKind::Gt => Some(BinaryOp::Gt),
        TokenKind::Ge => Some(BinaryOp::Ge),
        _ => None,
    }
}
