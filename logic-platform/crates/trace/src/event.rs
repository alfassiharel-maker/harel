//! Trace events.
//!
//! A trace is a *structured* record of what the engine did, not logging. There
//! are no levels and no free text meant for a human instead of a machine.
//!
//! **Semantic identity excludes time.** Owner instruction §10: timing metadata
//! may exist, but semantic equality must not depend on wall-clock timestamps.
//! So events carry a sequence number and no clock, and the wall-clock data
//! lives in [`crate::TraceMetadata`], which is excluded from `Trace`'s
//! `PartialEq`. Two runs of one program produce equal traces and can still be
//! measured. This resolves audit contradiction C1, which arose from dropping
//! the specification's `Timing` field to protect comparability — both are
//! achievable at once.
//!
//! Events name things by text rather than by id, so a trace can be read without
//! the IR that produced it.

use lml_types::Value;

/// The outcome of evaluating a rule's condition.
///
/// Three outcomes, not two: `Unknown` is not `false` (owner decisions OD-2 and
/// O2.11), and the difference must stay visible in the trace.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConditionOutcome {
    /// The condition holds; the rule fires.
    True,
    /// The condition does not hold; the rule does not fire and will not be
    /// reconsidered unless the facts it reads change — which, facts being
    /// immutable, they cannot.
    False,
    /// Not enough information yet. The rule is **pending**: it does not fire,
    /// and it is reconsidered next round.
    Unknown,
}

impl ConditionOutcome {
    /// The name used in the trace and its JSON form.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::True => "true",
            Self::False => "false",
            Self::Unknown => "unknown",
        }
    }
}

/// One side of a conflict: who concluded what, and where that is recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConflictSide {
    /// The rule that concluded it, or `None` for a `fact` declaration.
    pub rule: Option<String>,
    /// What it concluded.
    pub value: Value,
    /// The trace event that recorded the conclusion.
    pub trace_seq: u64,
}

/// Everything owner decision OD-3 requires a conflict to report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConflictReport {
    /// Stable identifier, unique within the execution: `C1`, `C2`, …
    ///
    /// Deterministic by construction — it counts conflicts in the order they
    /// occur — so two runs of one program produce the same ids.
    pub id: String,
    /// The name the two conclusions disagree about.
    pub name: String,
    /// The conclusion already held.
    pub existing: ConflictSide,
    /// The conclusion that could not be added.
    pub incoming: ConflictSide,
    /// The facts both rules read, with their values at the moment of conflict.
    pub relevant_facts: Vec<(String, Value)>,
    /// The conditions of the rules involved, as the IR renders them.
    pub relevant_conditions: Vec<(String, String)>,
    /// The inference round it happened in.
    pub round: usize,
    /// Trace events that explain it: both conclusions, and the activations.
    pub trace_refs: Vec<u64>,
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
        /// Its value — which may be `null`.
        value: Value,
    },
    /// An inference round began.
    RoundStarted {
        /// 1-based round number.
        round: usize,
    },
    /// A rule's condition was evaluated, to one of three outcomes.
    ConditionEvaluated {
        /// The rule's name.
        rule: String,
        /// The outcome.
        outcome: ConditionOutcome,
    },
    /// A rule is **pending**: its condition is `Unknown`, so it neither fired
    /// nor failed, and it will be reconsidered.
    RulePending {
        /// The rule's name.
        rule: String,
        /// The names it is waiting for, in name order. Empty means the
        /// condition is unknown for a reason other than a missing name.
        waiting_for: Vec<String>,
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
    /// Two conclusions for one name could not both hold.
    ConflictRaised(Box<ConflictReport>),
    /// An inference round ended.
    RoundFinished {
        /// The round number.
        round: usize,
        /// Whether any rule fired in it. `false` ends the fixed point.
        fired_any: bool,
        /// How many rules are still pending — waiting for information that no
        /// longer can arrive, once this is the final round.
        pending: usize,
    },
    /// An output was emitted.
    OutputEmitted {
        /// The name.
        name: String,
        /// Its value: a concrete value, `null`, or `unknown`.
        value: Value,
    },
    /// Execution finished normally.
    ExecutionFinished {
        /// How many rounds ran.
        rounds: usize,
        /// How many facts are known at the end.
        facts_total: usize,
        /// How many rules fired.
        rules_fired: usize,
        /// How many rules never became evaluable.
        rules_pending: usize,
    },
    /// Execution stopped with an error. A partial trace is still produced, up
    /// to and including this event.
    ErrorRaised {
        /// The stable error code.
        code: String,
        /// The message.
        message: String,
        /// What produced it, when that is known.
        cause: Option<String>,
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
            Self::ConditionEvaluated { .. } => "ConditionEvaluated",
            Self::RulePending { .. } => "RulePending",
            Self::RuleActivated { .. } => "RuleActivated",
            Self::FactDerived { .. } => "FactDerived",
            Self::DerivationRedundant { .. } => "DerivationRedundant",
            Self::ConflictRaised(_) => "ConflictRaised",
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
