//! The IR program.

use crate::code::ExprCode;
use crate::name::{NameId, NameTable};
use lml_diagnostics::Span;
use lml_types::{Type, Value};

/// The IR shape version.
///
/// Bumped whenever the IR's structure changes, so a stored or transmitted IR
/// can be rejected rather than misread. It is independent of the language
/// version: a syntax change that lowers to the same IR does not bump it.
pub const IR_VERSION: u32 = 1;

/// A rule, as an index in source order.
///
/// Source order is also evaluation order within a round, so this is both an
/// identity and a schedule.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct RuleId(pub u32);

impl RuleId {
    /// The index.
    #[must_use]
    pub const fn index(self) -> usize {
        self.0 as usize
    }
}

impl core::fmt::Display for RuleId {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(f, "r{}", self.0)
    }
}

/// A declared fact: a name bound to a value before inference begins.
///
/// The value is already computed. A `fact` declaration's expression is
/// evaluated during lowering, because it can only read literals — nothing else
/// is known at that point — so the runtime never evaluates it.
#[derive(Debug, Clone, PartialEq)]
pub struct IrFact {
    /// The name being bound.
    pub name: NameId,
    /// Its value.
    pub value: Value,
    /// Where it was declared.
    pub span: Span,
}

/// One consequence of a rule.
#[derive(Debug, Clone, PartialEq)]
pub struct IrEffect {
    /// The name to derive.
    pub name: NameId,
    /// How to compute its value.
    pub code: ExprCode,
    /// Where the clause is.
    pub span: Span,
}

/// A rule.
#[derive(Debug, Clone, PartialEq)]
pub struct IrRule {
    /// Its identity and its place in the evaluation order.
    pub id: RuleId,
    /// Its name, for traces and conflict reports.
    pub name: String,
    /// The condition.
    pub condition: ExprCode,
    /// The consequences, in source order.
    pub effects: Vec<IrEffect>,
    /// Every name the rule reads, sorted and deduplicated.
    ///
    /// Collected over condition *and* consequences: a rule is evaluable only
    /// when all of these are known, which is what guarantees that a firing rule
    /// can always compute its consequences.
    pub reads: Vec<NameId>,
    /// Every name the rule writes, in source order.
    pub writes: Vec<NameId>,
    /// Where the rule is.
    pub span: Span,
}

/// A whole program, ready to execute.
#[derive(Debug, Clone, PartialEq)]
pub struct IrProgram {
    /// The IR shape version this program was built for.
    pub ir_version: u32,
    /// Every name.
    pub names: NameTable,
    /// The type of every name, indexed by [`NameId::index`].
    ///
    /// Carried so that tooling and the trace can report types without rerunning
    /// inference. The runtime does not consult it: the code is already typed.
    pub types: Vec<Type>,
    /// Declared facts, in source order.
    pub facts: Vec<IrFact>,
    /// Rules, in source order.
    pub rules: Vec<IrRule>,
    /// Names to emit, in source order.
    pub outputs: Vec<NameId>,
}

impl IrProgram {
    /// The type of a name, if the program knows it.
    #[must_use]
    pub fn type_of(&self, name: NameId) -> Option<Type> {
        self.types.get(name.index()).copied()
    }

    /// The name behind an id, or its numeric form if the id is foreign.
    #[must_use]
    pub fn name_text(&self, id: NameId) -> String {
        self.names
            .text(id)
            .map_or_else(|| id.to_string(), ToOwned::to_owned)
    }

    /// The rule with this id.
    #[must_use]
    pub fn rule(&self, id: RuleId) -> Option<&IrRule> {
        self.rules.get(id.index())
    }
}
