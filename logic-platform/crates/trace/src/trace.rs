//! Recording and reading a trace.

use crate::event::{TraceEvent, TraceEventKind};
use lml_types::Value;

/// Wall-clock data about an execution.
///
/// Deliberately **not** part of a trace's identity: `Trace`'s `PartialEq`
/// compares events only. That is what lets the specification's `Timing` and
/// `Execution ID` requirements (Master Spec §10) coexist with deterministic
/// trace comparison (owner instruction §10).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TraceMetadata {
    /// Identifies this run. Not stable across runs — that is the point of it.
    pub execution_id: String,
    /// When the run started, in nanoseconds since the Unix epoch.
    pub started_at_unix_nanos: u128,
    /// How long it took, in nanoseconds.
    pub duration_nanos: u128,
}

/// An append-only sequence of events, plus non-semantic metadata.
#[derive(Debug, Clone, Eq, Default)]
pub struct Trace {
    events: Vec<TraceEvent>,
    metadata: Option<TraceMetadata>,
}

/// Semantic identity is the event sequence. Metadata — execution id, timing —
/// is excluded, so two runs of one program compare equal while still carrying
/// their own measurements.
impl PartialEq for Trace {
    fn eq(&self, other: &Self) -> bool {
        self.events == other.events
    }
}

impl Trace {
    /// An empty trace.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            events: Vec::new(),
            metadata: None,
        }
    }

    /// Append an event, numbering it, and return the number assigned.
    ///
    /// The returned `seq` is what conflict reports and provenance use to point
    /// back at the event that recorded a binding.
    pub fn record(&mut self, kind: TraceEventKind) -> u64 {
        let seq = self.events.len() as u64;
        self.events.push(TraceEvent { seq, kind });
        seq
    }

    /// The sequence number the next recorded event will receive.
    #[must_use]
    pub fn next_seq(&self) -> u64 {
        self.events.len() as u64
    }

    /// Attach the wall-clock metadata for this run.
    pub fn set_metadata(&mut self, metadata: TraceMetadata) {
        self.metadata = Some(metadata);
    }

    /// The wall-clock metadata, if the runtime recorded any.
    #[must_use]
    pub const fn metadata(&self) -> Option<&TraceMetadata> {
        self.metadata.as_ref()
    }

    /// The events, in order.
    #[must_use]
    pub fn events(&self) -> &[TraceEvent] {
        &self.events
    }

    /// How many events were recorded.
    #[must_use]
    pub fn len(&self) -> usize {
        self.events.len()
    }

    /// Whether nothing was recorded.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    /// The value emitted for `name`, if the trace shows one.
    #[must_use]
    pub fn output(&self, name: &str) -> Option<&Value> {
        self.events
            .iter()
            .rev()
            .find_map(|event| match &event.kind {
                TraceEventKind::OutputEmitted {
                    name: emitted,
                    value,
                } if emitted == name => Some(value),
                _ => None,
            })
    }

    /// The rule that derived `name`, and the value, if the trace shows it.
    #[must_use]
    pub fn derivation(&self, name: &str) -> Option<(&str, &Value)> {
        self.events.iter().find_map(|event| match &event.kind {
            TraceEventKind::FactDerived {
                name: derived,
                value,
                rule,
            } if derived == name => Some((rule.as_str(), value)),
            _ => None,
        })
    }

    /// Every rule that fired, in the order they fired.
    #[must_use]
    pub fn activations(&self) -> Vec<&str> {
        self.events
            .iter()
            .filter_map(|event| match &event.kind {
                TraceEventKind::RuleActivated { rule, .. } => Some(rule.as_str()),
                _ => None,
            })
            .collect()
    }

    /// Every rule left pending at the end, with what it was waiting for.
    ///
    /// "Why did this rule not fire?" is one of the questions a trace must
    /// answer (owner instruction §10), and *pending* is a different answer from
    /// *its condition was false*.
    #[must_use]
    pub fn pending(&self) -> Vec<(&str, &[String])> {
        let mut last: Vec<(&str, &[String])> = Vec::new();
        for event in &self.events {
            if let TraceEventKind::RulePending { rule, waiting_for } = &event.kind {
                last.retain(|(name, _)| *name != rule.as_str());
                last.push((rule.as_str(), waiting_for.as_slice()));
            }
            if let TraceEventKind::RuleActivated { rule, .. } = &event.kind {
                last.retain(|(name, _)| *name != rule.as_str());
            }
        }
        last
    }

    /// Render as a readable, deterministic plain-text listing — one event per
    /// line, in order. For a machine, use [`crate::to_json`].
    #[must_use]
    pub fn to_text(&self) -> String {
        self.events
            .iter()
            .map(|event| format!("{:>4} {}", event.seq, describe(&event.kind)))
            .collect::<Vec<_>>()
            .join("\n")
    }
}

fn describe(kind: &TraceEventKind) -> String {
    match kind {
        TraceEventKind::ExecutionStarted {
            language_version,
            ir_version,
            program_digest,
        } => {
            format!(
                "ExecutionStarted language={language_version} ir={ir_version} program={program_digest}"
            )
        }
        TraceEventKind::FactDeclared { name, value } => format!("FactDeclared {name} = {value}"),
        TraceEventKind::RoundStarted { round } => format!("RoundStarted {round}"),
        TraceEventKind::ConditionEvaluated { rule, outcome } => {
            format!("ConditionEvaluated {rule} -> {}", outcome.name())
        }
        TraceEventKind::RulePending { rule, waiting_for } => {
            if waiting_for.is_empty() {
                format!("RulePending {rule}")
            } else {
                format!("RulePending {rule} waiting for {}", waiting_for.join(", "))
            }
        }
        TraceEventKind::RuleActivated { rule, reads } => {
            format!("RuleActivated {rule} reads {}", reads.join(", "))
        }
        TraceEventKind::FactDerived { name, value, rule } => {
            format!("FactDerived {name} = {value} by {rule}")
        }
        TraceEventKind::DerivationRedundant { name, value, rule } => {
            format!("DerivationRedundant {name} = {value} by {rule}")
        }
        TraceEventKind::ConflictRaised(report) => format!(
            "ConflictRaised {} {} = {} by {} vs {} by {}",
            report.id,
            report.name,
            report.incoming.value,
            report.incoming.rule.as_deref().unwrap_or("declaration"),
            report.existing.value,
            report.existing.rule.as_deref().unwrap_or("declaration"),
        ),
        TraceEventKind::RoundFinished {
            round,
            fired_any,
            pending,
        } => {
            format!("RoundFinished {round} fired={fired_any} pending={pending}")
        }
        TraceEventKind::OutputEmitted { name, value } => format!("OutputEmitted {name} = {value}"),
        TraceEventKind::ExecutionFinished {
            rounds,
            facts_total,
            rules_fired,
            rules_pending,
        } => {
            format!(
                "ExecutionFinished rounds={rounds} facts={facts_total} fired={rules_fired} pending={rules_pending}"
            )
        }
        TraceEventKind::ErrorRaised { code, message, .. } => {
            format!("ErrorRaised {code} {message}")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::ConditionOutcome;

    #[test]
    fn sequence_numbers_start_at_zero_and_increment() {
        let mut trace = Trace::new();
        assert_eq!(trace.record(TraceEventKind::RoundStarted { round: 1 }), 0);
        assert_eq!(
            trace.record(TraceEventKind::RoundFinished {
                round: 1,
                fired_any: false,
                pending: 0
            }),
            1
        );
        assert_eq!(trace.events()[1].seq, 1);
    }

    #[test]
    fn semantic_equality_ignores_timing() {
        // Owner instruction §10: timing may exist; identity must not depend on
        // it. This is that requirement, executable.
        let build = |id: &str, nanos: u128| {
            let mut trace = Trace::new();
            trace.record(TraceEventKind::FactDeclared {
                name: "a".into(),
                value: Value::int(1),
            });
            trace.set_metadata(TraceMetadata {
                execution_id: id.to_owned(),
                started_at_unix_nanos: nanos,
                duration_nanos: nanos,
            });
            trace
        };
        assert_eq!(build("run-1", 1), build("run-2", 999_999));
    }

    #[test]
    fn a_trace_carries_no_clock_in_its_events() {
        let build = || {
            let mut trace = Trace::new();
            trace.record(TraceEventKind::FactDeclared {
                name: "a".into(),
                value: Value::Null,
            });
            trace
        };
        assert_eq!(build(), build());
        assert!(
            !build().to_text().contains(':'),
            "no timestamp in the text form"
        );
    }

    #[test]
    fn pending_rules_are_readable_from_the_trace() {
        let mut trace = Trace::new();
        trace.record(TraceEventKind::RulePending {
            rule: "waiting".into(),
            waiting_for: vec!["x".into()],
        });
        trace.record(TraceEventKind::RulePending {
            rule: "later".into(),
            waiting_for: vec!["y".into()],
        });
        trace.record(TraceEventKind::RuleActivated {
            rule: "later".into(),
            reads: vec!["y".into()],
        });
        let pending = trace.pending();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].0, "waiting");
    }

    #[test]
    fn outcomes_are_three_not_two() {
        assert_eq!(ConditionOutcome::True.name(), "true");
        assert_eq!(ConditionOutcome::False.name(), "false");
        assert_eq!(ConditionOutcome::Unknown.name(), "unknown");
    }
}
