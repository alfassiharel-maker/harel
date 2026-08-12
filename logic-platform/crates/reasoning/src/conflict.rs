//! Conflict detection and resolution policy.
//!
//! Owner decision OD-3: incompatible conclusions raise an explicit `Conflict`.
//! There is no implicit priority, no source-order winner, no last-write-wins.
//!
//! The policy is a trait with one implementation, [`RaiseConflict`], which is
//! the default and the only behaviour today. The trait exists so that a future
//! *explicit* strategy — priority, specificity, first-match, all-results — is
//! an addition here rather than a change threaded through the engine. It does
//! **not** exist to make the current behaviour configurable: nothing selects a
//! different strategy yet, because no strategy has been specified.

use lml_trace::{ConflictReport, ConflictSide};

/// What to do about a conflict.
///
/// One variant today. Future strategies add variants (`KeepExisting`,
/// `UseIncoming`, `Collect`), and each addition is a language decision with its
/// own specification — not a default that quietly changes what programs mean.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum Resolution {
    /// Stop and report. The conclusions stand as they are: nothing is
    /// overwritten, because facts are immutable.
    Raise,
}

/// How the engine resolves incompatible conclusions.
pub trait ConflictStrategy {
    /// Decide what to do about `conflict`.
    ///
    /// The report is complete before this is called, so a strategy can inspect
    /// the rules, the values and the facts that led to the disagreement.
    fn resolve(&self, conflict: &ConflictReport) -> Resolution;

    /// The strategy's name, for the trace and for diagnostics.
    fn name(&self) -> &'static str;
}

/// The default and only strategy: report the conflict, resolve nothing.
#[derive(Debug, Clone, Copy, Default)]
pub struct RaiseConflict;

impl ConflictStrategy for RaiseConflict {
    fn resolve(&self, _conflict: &ConflictReport) -> Resolution {
        Resolution::Raise
    }

    fn name(&self) -> &'static str {
        "raise"
    }
}

/// Assemble the seven elements owner decision OD-3 requires.
///
/// Every element comes from state the engine already holds at the moment of the
/// conflict; nothing is reconstructed afterwards, so the report cannot disagree
/// with what happened.
pub(crate) struct ConflictParts<'a> {
    pub id: String,
    pub name: String,
    pub existing: ConflictSide,
    pub incoming: ConflictSide,
    pub relevant_facts: Vec<(String, lml_types::Value)>,
    pub relevant_conditions: Vec<(String, String)>,
    pub round: usize,
    pub trace_refs: &'a [u64],
}

impl ConflictParts<'_> {
    pub(crate) fn into_report(self) -> ConflictReport {
        ConflictReport {
            id: self.id,
            name: self.name,
            existing: self.existing,
            incoming: self.incoming,
            relevant_facts: self.relevant_facts,
            relevant_conditions: self.relevant_conditions,
            round: self.round,
            trace_refs: self.trace_refs.to_vec(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lml_types::Value;

    fn report() -> ConflictReport {
        ConflictReport {
            id: "C1".into(),
            name: "status".into(),
            existing: ConflictSide {
                rule: Some("a".into()),
                value: Value::int(1),
                trace_seq: 3,
            },
            incoming: ConflictSide {
                rule: Some("b".into()),
                value: Value::int(2),
                trace_seq: 7,
            },
            relevant_facts: Vec::new(),
            relevant_conditions: Vec::new(),
            round: 1,
            trace_refs: vec![3, 7],
        }
    }

    #[test]
    fn the_default_strategy_never_picks_a_winner() {
        assert_eq!(RaiseConflict.resolve(&report()), Resolution::Raise);
        assert_eq!(RaiseConflict.name(), "raise");
    }
}
