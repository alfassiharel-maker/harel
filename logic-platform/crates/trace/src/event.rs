//! Trace events.
//!
//! A trace is a *structured* record of what the engine did, not logging. There
//! are no levels, no free text meant for a human instead of a machine, and no
//! timestamps: a timestamp would make two runs of the same program produce
//! different traces, and comparing traces is the point (`docs/03_EXECUTION_MODEL.md`
//! §7). Timing is a metric, recorded elsewhere.
//!
//! Events name things by text rather than by id, so a trace can be read without
//! the IR that produced it.

use lml_types::Value;

/// What was emitted for an output name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Emitted {
    /// A value was derived.
    Known(Value),
    /// Nothing derived it. Not an error, and emphatically not `0`
    /// (`docs/02_FORMAL_SEMANTICS.md` §4.4).
    Unknown,
}

/// One thing the engine did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TraceEventKind {
    /// Execution began.
    ExecutionStarted {
        /// The language version the engine implements.
        language_version: String,
        /// The IR shape version of the program.
        ir_version: u32,
        /// SHA-256 of the canonical IR text, identifying the program.
        program_digest: String,
    },
    /// A `fact` declaration was loaded.
    FactDeclared {
        /// The name.
        name: String,
        /// Its value.
        value: Value,
    },
    /// An inference round began.
    RoundStarted {
        /// 1-based round number.
        round: usize,
    },
    /// A rule could not be tried because something it reads is unknown.
    RuleNotEvaluable {
        /// The rule's name.
        rule: String,
        /// What it is waiting for, in name order.
        missing: Vec<String>,
    },
    /// A rule's condition was evaluated.
    ConditionEvaluated {
        /// The rule's name.
        rule: String,
        /// The result.
        result: bool,
    },
    /// A rule fired.
    RuleActivated {
        /// The rule's name.
        rule: String,
        /// The names it read, in name order.
        ///
        /// Carried so that an explanation can be reconstructed from the trace
        /// alone, without the program (`docs/03_EXECUTION_MODEL.md` §7.1).
        reads: Vec<String>,
    },
    /// A rule derived a new fact.
    FactDerived {
        /// The name.
        name: String,
        /// Its value.
        value: Value,
        /// The rule that derived it.
        rule: String,
    },
    /// A rule derived a value a name already held. Not a conflict: two rules
    /// agreeing is fine, and worth recording.
    DerivationRedundant {
        /// The name.
        name: String,
        /// The value both rules produced.
        value: Value,
        /// The rule that re-derived it.
        rule: String,
    },
    /// An inference round ended.
    RoundFinished {
        /// The round number.
        round: usize,
        /// Whether any rule fired in it. `false` ends the fixed point.
        fired_any: bool,
    },
    /// An output was emitted.
    OutputEmitted {
        /// The name.
        name: String,
        /// Its value, or `Unknown`.
        value: Emitted,
    },
    /// Execution finished normally.
    ExecutionFinished {
        /// How many rounds ran.
        rounds: usize,
        /// How many facts are known at the end.
        facts_total: usize,
        /// How many rules fired.
        rules_fired: usize,
    },
    /// Execution stopped with an error. A partial trace is still produced, up
    /// to and including this event.
    ErrorRaised {
        /// The stable error code.
        code: String,
        /// The message.
        message: String,
    },
}

impl TraceEventKind {
    /// The event's kind name, used in the JSON form and by consumers that
    /// filter a trace.
    #[must_use]
    pub const fn name(&self) -> &'static str {
        match self {
            Self::ExecutionStarted { .. } => "ExecutionStarted",
            Self::FactDeclared { .. } => "FactDeclared",
            Self::RoundStarted { .. } => "RoundStarted",
            Self::RuleNotEvaluable { .. } => "RuleNotEvaluable",
            Self::ConditionEvaluated { .. } => "ConditionEvaluated",
            Self::RuleActivated { .. } => "RuleActivated",
            Self::FactDerived { .. } => "FactDerived",
            Self::DerivationRedundant { .. } => "DerivationRedundant",
            Self::RoundFinished { .. } => "RoundFinished",
            Self::OutputEmitted { .. } => "OutputEmitted",
            Self::ExecutionFinished { .. } => "ExecutionFinished",
            Self::ErrorRaised { .. } => "ErrorRaised",
        }
    }
}

/// An event and its position in the sequence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TraceEvent {
    /// Position in the trace, starting at 0 and incrementing by 1. A sequence
    /// number rather than a clock: deterministic, and enough to order events.
    pub seq: u64,
    /// What happened.
    pub kind: TraceEventKind,
}
