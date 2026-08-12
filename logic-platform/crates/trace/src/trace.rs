//! Recording and reading a trace.

use crate::event::{Emitted, TraceEvent, TraceEventKind};
use lml_types::Value;

/// An append-only sequence of events.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Trace {
    events: Vec<TraceEvent>,
}

impl Trace {
    /// An empty trace.
    #[must_use]
    pub const fn new() -> Self {
        Self { events: Vec::new() }
    }

    /// Append an event, numbering it.
    pub fn record(&mut self, kind: TraceEventKind) {
        let seq = self.events.len() as u64;
        self.events.push(TraceEvent { seq, kind });
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
    pub fn output(&self, name: &str) -> Option<&Emitted> {
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
            format!("ExecutionStarted language={language_version} ir={ir_version} program={program_digest}")
        }
        TraceEventKind::FactDeclared { name, value } => format!("FactDeclared {name} = {value}"),
        TraceEventKind::RoundStarted { round } => format!("RoundStarted {round}"),
        TraceEventKind::RuleNotEvaluable { rule, missing } => {
            format!("RuleNotEvaluable {rule} waiting for {}", missing.join(", "))
        }
        TraceEventKind::ConditionEvaluated { rule, result } => {
            format!("ConditionEvaluated {rule} -> {result}")
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
        TraceEventKind::RoundFinished { round, fired_any } => {
            format!("RoundFinished {round} fired={fired_any}")
        }
        TraceEventKind::OutputEmitted { name, value } => match value {
            Emitted::Known(value) => format!("OutputEmitted {name} = {value}"),
            Emitted::Unknown => format!("OutputEmitted {name} = unknown"),
        },
        TraceEventKind::ExecutionFinished {
            rounds,
            facts_total,
            rules_fired,
        } => {
            format!("ExecutionFinished rounds={rounds} facts={facts_total} fired={rules_fired}")
        }
        TraceEventKind::ErrorRaised { code, message } => format!("ErrorRaised {code} {message}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sequence_numbers_start_at_zero_and_increment() {
        let mut trace = Trace::new();
        trace.record(TraceEventKind::RoundStarted { round: 1 });
        trace.record(TraceEventKind::RoundFinished {
            round: 1,
            fired_any: false,
        });
        assert_eq!(trace.events()[0].seq, 0);
        assert_eq!(trace.events()[1].seq, 1);
    }

    #[test]
    fn a_trace_carries_no_clock() {
        // Two traces of the same events are equal — which is only possible
        // because nothing in an event comes from the environment.
        let build = || {
            let mut trace = Trace::new();
            trace.record(TraceEventKind::FactDeclared {
                name: "a".into(),
                value: Value::Int(1),
            });
            trace
        };
        assert_eq!(build(), build());
    }
}
