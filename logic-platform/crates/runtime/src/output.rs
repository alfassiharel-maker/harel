//! Output types — the structured result of a query.

use logic::{GroundFact, RelName};
use serde::Serialize;
use std::sync::Arc;
use trace::ProvenanceSummary;
use types::Value;

/// The answer to a single query atom.
#[derive(Debug, Clone, Serialize)]
pub struct QueryAnswer {
    pub relation: RelName,
    pub results: Vec<QueryRow>,
}

/// One result row: the ground argument values plus provenance.
#[derive(Debug, Clone, Serialize)]
pub struct QueryRow {
    pub args: Vec<Value>,
    pub provenance: ProvenanceSummary,
}

impl QueryRow {
    pub fn from_fact(fact: &Arc<GroundFact>) -> Self {
        QueryRow {
            args: fact.args.clone(),
            provenance: ProvenanceSummary::from_fact(fact),
        }
    }
}

impl QueryAnswer {
    /// Render a human-readable summary suitable for CLI output.
    pub fn display(&self) -> String {
        let mut lines = vec![format!("{}:", self.relation)];
        if self.results.is_empty() {
            lines.push("  (no results)".to_string());
        }
        for row in &self.results {
            let args: Vec<String> = row.args.iter().map(|v| v.to_string()).collect();
            let prov = match &row.provenance {
                ProvenanceSummary::Asserted => "asserted".to_string(),
                ProvenanceSummary::Derived { rule_idx, support } => {
                    let support_str: Vec<String> = support
                        .iter()
                        .map(|(rel, args)| {
                            let a: Vec<String> = args.iter().map(|v| v.to_string()).collect();
                            format!("{}({})", rel, a.join(", "))
                        })
                        .collect();
                    format!("derived by rule #{} from [{}]", rule_idx, support_str.join(", "))
                }
            };
            lines.push(format!("  ({}) — {}", args.join(", "), prov));
        }
        lines.join("\n")
    }
}
