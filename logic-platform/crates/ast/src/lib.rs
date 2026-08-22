//! Abstract Syntax Tree for the Logic language.
//!
//! The AST is a faithful structural representation of parsed source text.
//! It carries span information for diagnostics.  No execution occurs here.

use diagnostics::Span;
use serde::Serialize;

/// A term as it appears in source text.
#[derive(Debug, Clone, Serialize)]
pub enum TermNode {
    /// Uppercase identifier — a logic variable.
    Variable { name: String, span: Span },
    /// String constant.
    ConstStr { value: String, span: Span },
    /// Integer constant.
    ConstInt { value: i64, span: Span },
    /// Float constant.
    ConstFloat { value: f64, span: Span },
    /// Boolean constant.
    ConstBool { value: bool, span: Span },
    /// The Null literal.
    Null { span: Span },
    /// The Unknown literal.
    Unknown { span: Span },
}

impl TermNode {
    pub fn span(&self) -> &Span {
        match self {
            TermNode::Variable { span, .. }
            | TermNode::ConstStr { span, .. }
            | TermNode::ConstInt { span, .. }
            | TermNode::ConstFloat { span, .. }
            | TermNode::ConstBool { span, .. }
            | TermNode::Null { span }
            | TermNode::Unknown { span } => span,
        }
    }

    pub fn is_variable(&self) -> bool {
        matches!(self, TermNode::Variable { .. })
    }
}

/// A predicate application: `relation_name(arg, arg, ...)`.
#[derive(Debug, Clone, Serialize)]
pub struct AtomNode {
    pub relation: String,
    pub args: Vec<TermNode>,
    pub span: Span,
}

/// A fact declaration: `fact relation(arg, ...);`
/// All args must be ground constants after semantic analysis.
#[derive(Debug, Clone, Serialize)]
pub struct FactDecl {
    pub atom: AtomNode,
    pub span: Span,
}

/// A rule declaration: `rule head(args) when body1(args), body2(args), ...;`
#[derive(Debug, Clone, Serialize)]
pub struct RuleDecl {
    pub head: AtomNode,
    pub body: Vec<AtomNode>,
    pub span: Span,
}

/// A query: `query relation(args);`
/// Args may contain variables (which become query outputs).
#[derive(Debug, Clone, Serialize)]
pub struct QueryDecl {
    pub atom: AtomNode,
    pub span: Span,
}

/// A top-level declaration in a program.
#[derive(Debug, Clone, Serialize)]
pub enum Decl {
    Fact(FactDecl),
    Rule(RuleDecl),
    Query(QueryDecl),
}

impl Decl {
    pub fn span(&self) -> &Span {
        match self {
            Decl::Fact(f) => &f.span,
            Decl::Rule(r) => &r.span,
            Decl::Query(q) => &q.span,
        }
    }
}

/// A complete parsed program.
#[derive(Debug, Clone, Serialize)]
pub struct Program {
    pub decls: Vec<Decl>,
    pub source_name: String,
}

impl Program {
    pub fn facts(&self) -> impl Iterator<Item = &FactDecl> {
        self.decls.iter().filter_map(|d| {
            if let Decl::Fact(f) = d {
                Some(f)
            } else {
                None
            }
        })
    }

    pub fn rules(&self) -> impl Iterator<Item = &RuleDecl> {
        self.decls.iter().filter_map(|d| {
            if let Decl::Rule(r) = d {
                Some(r)
            } else {
                None
            }
        })
    }

    pub fn queries(&self) -> impl Iterator<Item = &QueryDecl> {
        self.decls.iter().filter_map(|d| {
            if let Decl::Query(q) = d {
                Some(q)
            } else {
                None
            }
        })
    }
}
