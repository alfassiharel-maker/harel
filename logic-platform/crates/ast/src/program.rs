//! Declarations and the program.

use crate::expr::Expr;
use crate::name::Name;
use lml_diagnostics::Span;

/// `fact <name> = <expr>`
#[derive(Debug, Clone, PartialEq)]
pub struct FactDecl {
    /// The name being bound.
    pub name: Name,
    /// The expression giving its value.
    pub value: Expr,
    /// The whole declaration.
    pub span: Span,
}

/// `then <name> = <expr>` — one consequence of a rule.
#[derive(Debug, Clone, PartialEq)]
pub struct Effect {
    /// The name being derived.
    pub name: Name,
    /// The expression giving its value.
    pub value: Expr,
    /// The whole clause.
    pub span: Span,
}

/// `rule <name>: when <expr> then ...`
#[derive(Debug, Clone, PartialEq)]
pub struct RuleDecl {
    /// The rule's name, which appears in traces and conflict reports.
    pub name: Name,
    /// The condition. Must be of type `Bool` (`02_FORMAL_SEMANTICS.md` §3.2).
    pub condition: Expr,
    /// One or more consequences. A rule with none does not parse: a rule that
    /// derives nothing has no meaning.
    pub effects: Vec<Effect>,
    /// The whole declaration.
    pub span: Span,
}

/// `output <name>{, <name>}`
#[derive(Debug, Clone, PartialEq)]
pub struct OutputDecl {
    /// The names to emit, in the order written.
    pub names: Vec<Name>,
    /// The whole declaration.
    pub span: Span,
}

/// A top-level declaration.
#[derive(Debug, Clone, PartialEq)]
pub enum Item {
    /// A fact declaration.
    Fact(FactDecl),
    /// A rule declaration.
    Rule(RuleDecl),
    /// An output declaration.
    Output(OutputDecl),
}

impl Item {
    /// Where the declaration was written.
    #[must_use]
    pub const fn span(&self) -> Span {
        match self {
            Self::Fact(decl) => decl.span,
            Self::Rule(decl) => decl.span,
            Self::Output(decl) => decl.span,
        }
    }
}

/// A whole source file.
///
/// A program is a *set* of declarations, not a sequence: order affects the
/// order of evaluation within a round and the order of outputs, but a name may
/// be used before the declaration that binds it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Program {
    /// The declarations, in source order.
    pub items: Vec<Item>,
}

impl Program {
    /// The fact declarations, in source order.
    pub fn facts(&self) -> impl Iterator<Item = &FactDecl> {
        self.items.iter().filter_map(|item| match item {
            Item::Fact(decl) => Some(decl),
            _ => None,
        })
    }

    /// The rule declarations, in source order — which is also the order the
    /// engine evaluates them in within a round.
    pub fn rules(&self) -> impl Iterator<Item = &RuleDecl> {
        self.items.iter().filter_map(|item| match item {
            Item::Rule(decl) => Some(decl),
            _ => None,
        })
    }

    /// The output declarations, in source order.
    pub fn outputs(&self) -> impl Iterator<Item = &OutputDecl> {
        self.items.iter().filter_map(|item| match item {
            Item::Output(decl) => Some(decl),
            _ => None,
        })
    }
}
