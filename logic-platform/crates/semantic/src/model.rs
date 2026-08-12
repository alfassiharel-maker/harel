//! The validated semantic model.
//!
//! What a successful analysis produces, and the only thing lowering is allowed
//! to read. Everything in it has been checked: every name has exactly one type,
//! every name that is read is writable, every condition is `Bool`.

use lml_ast::Program;
use lml_diagnostics::Span;
use lml_types::Type;
use std::collections::{BTreeMap, BTreeSet};

/// What wrote a name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Writer {
    /// A `fact` declaration.
    Declaration {
        /// Where it is.
        span: Span,
    },
    /// A rule's `then` clause.
    Rule {
        /// Index into [`SemanticModel::rules`], which is also the rule's source
        /// order and its evaluation order within a round.
        index: usize,
        /// Where the clause is.
        span: Span,
    },
}

/// What is known about one name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NameInfo {
    /// The name's single type.
    pub ty: Type,
    /// Everything that can bind it, in source order. A name with a declaration
    /// has exactly one writer, because a rule may not shadow a fact (`E3004`).
    pub writers: Vec<Writer>,
}

impl NameInfo {
    /// Whether a `fact` declaration binds this name, which means it is known
    /// before inference starts.
    #[must_use]
    pub fn is_declared(&self) -> bool {
        self.writers
            .iter()
            .any(|writer| matches!(writer, Writer::Declaration { .. }))
    }
}

/// What is known about one rule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuleInfo {
    /// The rule's name, as it appears in traces and conflict reports.
    pub name: String,
    /// Every name the rule reads — condition *and* consequences.
    ///
    /// Collected over the whole rule, which is what makes a firing rule's
    /// consequences always evaluable (`docs/02_FORMAL_SEMANTICS.md` §4.2).
    pub reads: BTreeSet<String>,
    /// Every name the rule writes, in source order.
    pub writes: Vec<String>,
    /// Where the rule is.
    pub span: Span,
}

/// A program that has passed every static check.
#[derive(Debug, Clone, PartialEq)]
pub struct SemanticModel {
    /// The validated syntax tree. Kept whole so that lowering and tooling read
    /// one representation rather than a partial copy.
    pub program: Program,
    /// Every name in the program, in name order.
    pub names: BTreeMap<String, NameInfo>,
    /// Every rule, in source order. Index matches [`Writer::Rule::index`].
    pub rules: Vec<RuleInfo>,
    /// The names to emit, in source order, after de-duplication checks.
    pub outputs: Vec<String>,
}

impl SemanticModel {
    /// The type of `name`, if the program has one.
    #[must_use]
    pub fn type_of(&self, name: &str) -> Option<Type> {
        self.names.get(name).map(|info| info.ty)
    }

    /// The rules that read `name`, by index. The dependency graph in one query;
    /// analysis tooling and, later, incremental evaluation both need it.
    #[must_use]
    pub fn readers_of(&self, name: &str) -> Vec<usize> {
        self.rules
            .iter()
            .enumerate()
            .filter(|(_, rule)| rule.reads.contains(name))
            .map(|(index, _)| index)
            .collect()
    }
}
