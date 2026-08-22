//! Structured reasoning trace (§17, §18).
//!
//! Trace is NOT logging.  It records semantic execution events so the system
//! can answer "why did this result occur?".  Logical ordering is deterministic;
//! wall-clock timing metadata is separate from semantic comparison.

use logic::{GroundFact, RelName};
use serde::Serialize;
use types::Value;

/// A single semantic event in the execution trace.
/// The sequence number determines logical ordering (§18).
#[derive(Debug, Clone, Serialize)]
pub struct TraceEvent {
    /// Stable sequence position — logical ordering, not wall clock.
    pub seq: u64,
    pub kind: TraceEventKind,
}

#[derive(Debug, Clone, Serialize)]
pub enum TraceEventKind {
    ExecutionStarted {
        source_name: String,
    },
    FactDeclared {
        relation: RelName,
        args: Vec<Value>,
    },
    RuleActivated {
        rule_idx: usize,
        /// Variable bindings that fired the rule.
        bindings: Vec<(String, Value)>,
    },
    FactDerived {
        relation: RelName,
        args: Vec<Value>,
        rule_idx: usize,
        /// The ground-fact identities that supported the derivation.
        support_keys: Vec<(RelName, Vec<Value>)>,
    },
    FactAlreadyKnown {
        relation: RelName,
        args: Vec<Value>,
    },
    QueryExecuted {
        relation: RelName,
        query_args: Vec<Option<Value>>,
    },
    QueryResult {
        relation: RelName,
        args: Vec<Value>,
        provenance: ProvenanceSummary,
    },
    ConflictRaised {
        conflict_id: u64,
        rule_a: usize,
        rule_b: usize,
        conclusion_a: Vec<Value>,
        conclusion_b: Vec<Value>,
    },
    ExecutionFinished {
        total_facts: usize,
        iterations: u32,
    },
}

/// A compact, serialisable summary of provenance for query results.
#[derive(Debug, Clone, Serialize)]
pub enum ProvenanceSummary {
    Asserted,
    Derived {
        rule_idx: usize,
        support: Vec<(RelName, Vec<Value>)>,
    },
}

impl ProvenanceSummary {
    pub fn from_fact(fact: &GroundFact) -> Self {
        match &fact.provenance {
            logic::Provenance::Asserted => ProvenanceSummary::Asserted,
            logic::Provenance::Derived { rule_idx, support } => ProvenanceSummary::Derived {
                rule_idx: *rule_idx,
                support: support
                    .iter()
                    .map(|f| (f.relation.clone(), f.args.clone()))
                    .collect(),
            },
        }
    }
}

/// Accumulates trace events during a single execution.
#[derive(Debug, Default)]
pub struct TraceLog {
    events: Vec<TraceEvent>,
    next_seq: u64,
}

impl TraceLog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, kind: TraceEventKind) {
        let seq = self.next_seq;
        self.next_seq += 1;
        self.events.push(TraceEvent { seq, kind });
    }

    pub fn events(&self) -> &[TraceEvent] {
        &self.events
    }

    /// Render the trace as a newline-delimited JSON sequence (stable output).
    pub fn to_ndjson(&self) -> String {
        self.events
            .iter()
            .map(|e| serde_json::to_string(e).unwrap_or_else(|_| "{}".to_string()))
            .collect::<Vec<_>>()
            .join("\n")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sequence_numbers_are_monotone() {
        let mut log = TraceLog::new();
        log.push(TraceEventKind::ExecutionStarted { source_name: "<test>".into() });
        log.push(TraceEventKind::ExecutionFinished { total_facts: 0, iterations: 0 });
        let events = log.events();
        assert_eq!(events[0].seq, 0);
        assert_eq!(events[1].seq, 1);
    }

    #[test]
    fn ndjson_contains_one_line_per_event() {
        let mut log = TraceLog::new();
        log.push(TraceEventKind::ExecutionStarted { source_name: "<test>".into() });
        log.push(TraceEventKind::ExecutionFinished { total_facts: 1, iterations: 2 });
        let out = log.to_ndjson();
        assert_eq!(out.lines().count(), 2);
    }
}
