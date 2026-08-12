//! Expressions.

use crate::name::Name;
use lml_diagnostics::Span;

/// A literal value as written in the source.
///
/// Literals are already parsed by the lexer: an `Int` here is in range and a
/// `Float` is finite, because a literal that is not representable never becomes
/// a token (`docs/02_FORMAL_SEMANTICS.md` §1.4).
#[derive(Debug, Clone, PartialEq)]
pub enum Literal {
    /// An integer literal.
    Int(i64),
    /// A finite float literal.
    Float(f64),
    /// `true` or `false`.
    Bool(bool),
    /// A string literal with escapes resolved.
    Str(String),
    /// The `null` literal: a *known* absence of a value (owner decision OD-2).
    ///
    /// There is no literal for `unknown`. `unknown` is the state of a name
    /// nothing has bound, so writing `fact a = unknown` would assert that a
    /// value is known to be not known, which is a contradiction. It is
    /// reachable only through the `is unknown` predicate.
    Null,
}

/// The state a `x is <state>` predicate tests for.
///
/// These are the language's existence predicates, and they are **total**: every
/// value is in exactly one of the three states, so the answer is always a plain
/// `Bool` and the predicate can never fail. They are what makes the strictness
/// of the rest of the model usable — a program that must cope with absence has
/// a way to ask about it (`docs/O2_SEMANTIC_ANALYSIS.md` §3, M3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StateTest {
    /// `is null` — known absence.
    Null,
    /// `is unknown` — not enough information.
    Unknown,
    /// `is known` — a concrete value.
    Known,
}

impl StateTest {
    /// How the test is written.
    #[must_use]
    pub const fn keyword(self) -> &'static str {
        match self {
            Self::Null => "null",
            Self::Unknown => "unknown",
            Self::Known => "known",
        }
    }
}

/// Prefix operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnaryOp {
    /// Arithmetic negation, `-`.
    Neg,
    /// Logical negation, `not`.
    Not,
}

impl UnaryOp {
    /// How the operator is written.
    #[must_use]
    pub const fn symbol(self) -> &'static str {
        match self {
            Self::Neg => "-",
            Self::Not => "not",
        }
    }
}

/// Infix operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BinaryOp {
    /// `+`
    Add,
    /// `-`
    Sub,
    /// `*`
    Mul,
    /// `/`
    Div,
    /// `%`
    Rem,
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
    /// `and`
    And,
    /// `or`
    Or,
}

impl BinaryOp {
    /// How the operator is written.
    #[must_use]
    pub const fn symbol(self) -> &'static str {
        match self {
            Self::Add => "+",
            Self::Sub => "-",
            Self::Mul => "*",
            Self::Div => "/",
            Self::Rem => "%",
            Self::Eq => "==",
            Self::Ne => "!=",
            Self::Lt => "<",
            Self::Le => "<=",
            Self::Gt => ">",
            Self::Ge => ">=",
            Self::And => "and",
            Self::Or => "or",
        }
    }

    /// Whether the operator compares two values, which the grammar allows only
    /// once per comparison (`a < b < c` does not parse).
    #[must_use]
    pub const fn is_comparison(self) -> bool {
        matches!(
            self,
            Self::Eq | Self::Ne | Self::Lt | Self::Le | Self::Gt | Self::Ge
        )
    }
}

/// An expression.
#[derive(Debug, Clone, PartialEq)]
pub enum Expr {
    /// A literal.
    Literal {
        /// The value.
        value: Literal,
        /// Where it was written.
        span: Span,
    },
    /// A reference to a name.
    Name(Name),
    /// A prefix operator applied to one operand.
    Unary {
        /// The operator.
        op: UnaryOp,
        /// The operand.
        operand: Box<Expr>,
        /// The whole expression, operator included.
        span: Span,
    },
    /// A state predicate: `x is null`, `x is unknown`, `x is known`.
    Is {
        /// The expression whose state is tested.
        operand: Box<Expr>,
        /// Which state.
        test: StateTest,
        /// The whole expression.
        span: Span,
    },
    /// An infix operator applied to two operands.
    Binary {
        /// The operator.
        op: BinaryOp,
        /// Left operand.
        left: Box<Expr>,
        /// Right operand.
        right: Box<Expr>,
        /// The whole expression.
        span: Span,
    },
}

impl Expr {
    /// Where the expression was written.
    #[must_use]
    pub const fn span(&self) -> Span {
        match self {
            Self::Literal { span, .. }
            | Self::Unary { span, .. }
            | Self::Is { span, .. }
            | Self::Binary { span, .. } => *span,
            Self::Name(name) => name.span,
        }
    }

    /// Call `visit` on every name the expression reads, in source order.
    pub fn visit_names(&self, visit: &mut impl FnMut(&Name)) {
        match self {
            Self::Literal { .. } => {}
            Self::Name(name) => visit(name),
            Self::Unary { operand, .. } | Self::Is { operand, .. } => operand.visit_names(visit),
            Self::Binary { left, right, .. } => {
                left.visit_names(visit);
                right.visit_names(visit);
            }
        }
    }
}
