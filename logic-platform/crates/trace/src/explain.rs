//! Explanation, reconstructed from a trace.
//!
//! `why(trace, name)` answers "why did this result happen?" by *reading the
//! trace*, never by re-running and never by inferring what probably happened.
//! If the trace does not support an answer, the answer is `None`
//! (`docs/03_EXECUTION_MODEL.md` §7.1) — an approximate explanation of a
//! decision is worse than no explanation.

use crate::event::TraceEventKind;
use crate::trace::Trace;
use lml_types::Value;
use std::collections::BTreeSet;

/// Why one name holds the value it holds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Explanation {
    /// The name.
    pub name: String,
    /// Its value.
    pub value: Value,
    /// The rule that derived it, or `None` if it was declared.
    pub rule: Option<String>,
    /// The explanations of what that rule read, in name order. Empty for a
    /// declared fact, which is where every chain ends.
    pub because: Vec<Explanation>,
}

impl Explanation {
    /// Render as an indented tree.
    #[must_use]
    pub fn to_text(&self) -> String {
        let mut out = String::new();
        self.write(&mut out, 0);
        out
    }

    fn write(&self, out: &mut String, depth: usize) {
        let indent = "  ".repeat(depth);
        match &self.rule {
            Some(rule) => {
                out.push_str(&format!(
                    "{indent}{} = {} by rule `{rule}`\n",
                    self.name, self.value
                ));
            }
            None => out.push_str(&format!(
                "{indent}{} = {} declared\n",
                self.name, self.value
            )),
        }
        for cause in &self.because {
            cause.write(out, depth + 1);
        }
    }
}

/// Explain `name` from `trace`.
///
/// `None` means the trace does not say — the name was never bound, or the trace
/// is partial because execution failed before binding it.
#[must_use]
pub fn why(trace: &Trace, name: &str) -> Option<Explanation> {
    let mut visited = BTreeSet::new();
    explain(trace, name, &mut visited)
}

fn explain(trace: &Trace, name: &str, visited: &mut BTreeSet<String>) -> Option<Explanation> {
    // A derivation graph cannot contain a cycle — a rule fires only once every
    // name it reads is known — but an explanation walker must not depend on
    // that to terminate when handed a hand-written or truncated trace.
    if !visited.insert(name.to_owned()) {
        return None;
    }

    for event in trace.events() {
        match &event.kind {
            TraceEventKind::FactDeclared {
                name: declared,
                value,
            } if declared == name => {
                return Some(Explanation {
                    name: name.to_owned(),
                    value: value.clone(),
                    rule: None,
                    because: Vec::new(),
                });
            }
            TraceEventKind::FactDerived {
                name: derived,
                value,
                rule,
            } if derived == name => {
                let reads = reads_of(trace, rule);
                let because = reads
                    .iter()
                    .filter_map(|read| explain(trace, read, visited))
                    .collect();
                return Some(Explanation {
                    name: name.to_owned(),
                    value: value.clone(),
                    rule: Some(rule.clone()),
                    because,
                });
            }
            _ => {}
        }
    }
    None
}

/// What a rule read, from its activation event.
fn reads_of(trace: &Trace, rule: &str) -> Vec<String> {
    trace
        .events()
        .iter()
        .find_map(|event| match &event.kind {
            TraceEventKind::RuleActivated {
                rule: activated,
                reads,
            } if activated == rule => Some(reads.clone()),
            _ => None,
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn trace() -> Trace {
        let mut trace = Trace::new();
        trace.record(TraceEventKind::FactDeclared {
            name: "temperature".into(),
            value: Value::int(31),
        });
        trace.record(TraceEventKind::RuleActivated {
            rule: "heat".into(),
            reads: vec!["temperature".into()],
        });
        trace.record(TraceEventKind::FactDerived {
            name: "status".into(),
            value: Value::string("hot"),
            rule: "heat".into(),
        });
        trace
    }

    #[test]
    fn an_explanation_ends_at_declared_facts() {
        let explanation = why(&trace(), "status").expect("the trace supports it");
        assert_eq!(
            explanation.to_text(),
            "status = \"hot\" by rule `heat`\n  temperature = 31 declared\n"
        );
    }

    #[test]
    fn what_the_trace_does_not_show_is_not_guessed() {
        assert_eq!(why(&trace(), "nothing"), None);
        assert_eq!(why(&Trace::new(), "status"), None);
    }
}
