//! Conflict model (§19).
//!
//! Default strategy: RaiseConflict.
//! No hidden priority, no source-order winner, no last-write-wins.

use logic::RelName;
use serde::Serialize;
use types::Value;

/// A detected conflict: two rules derived incompatible conclusions.
#[derive(Debug, Clone, Serialize)]
pub struct Conflict {
    pub conflict_id: u64,
    pub rule_a: usize,
    pub rule_b: usize,
    pub relation: RelName,
    pub conclusion_a: Vec<Value>,
    pub conclusion_b: Vec<Value>,
}

impl std::fmt::Display for Conflict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "conflict #{}: rules {} and {} derived incompatible conclusions for relation {}",
            self.conflict_id, self.rule_a, self.rule_b, self.relation
        )
    }
}

/// Error returned when conflicts are detected during evaluation.
#[derive(Debug, thiserror::Error)]
#[error("evaluation raised {count} conflict(s)")]
pub struct ConflictError {
    pub conflicts: Vec<Conflict>,
    pub count: usize,
}

impl ConflictError {
    pub fn new(conflicts: Vec<Conflict>) -> Self {
        let count = conflicts.len();
        Self { conflicts, count }
    }
}
