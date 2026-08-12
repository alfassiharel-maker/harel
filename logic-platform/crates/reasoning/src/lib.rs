//! Inference: forward chaining to a least fixed point.
//!
//! This crate owns the *strategy* — which rule to try, in what order, how many
//! times, and what a conflict means — and nothing else. It does not know how a
//! value is represented or how an expression is evaluated; that is `lml-logic`.
//! The split is what makes a second strategy (backward chaining, for the `query`
//! form that does not exist yet) an addition rather than a rewrite.
//!
//! # The three outcomes of a rule
//!
//! Owner decision O2.11, implemented here and visible in the trace:
//!
//! ```text
//! condition true    → fire
//! condition false   → do not fire
//! condition unknown → PENDING: do not fire, reconsider next round
//! error             → stop, with a structured diagnostic
//! ```
//!
//! *Pending* is not *false*. A rule waiting for information that has not
//! arrived is a different thing from a rule whose condition was tested and did
//! not hold, and the difference survives into the trace so that "why did this
//! not fire?" has two distinguishable answers.
//!
//! Note what is **not** here any more: a gate that refuses to evaluate a rule
//! whose reads are not all bound. Under owner decision O2.5 a name bound to
//! `Null` is *known*, so a rule reading it is evaluable and may fire. The
//! condition itself now decides, and an unbound name reaches the evaluator as
//! `Unknown` and propagates.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod conflict;

pub use conflict::{ConflictStrategy, RaiseConflict, Resolution};

use conflict::ConflictParts;
use lml_diagnostics::{Code, Diagnostic, Span};
use lml_ir::{print_code, IrProgram, IrRule};
use lml_logic::{eval, FactSet, Insertion, Origin};
use lml_trace::{ConditionOutcome, ConflictSide, Trace, TraceEventKind};
use lml_types::Value;

/// What one inference produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InferenceReport {
    /// How many rounds ran, including the final round that fired nothing.
    pub rounds: usize,
    /// How many rules fired.
    pub rules_fired: usize,
    /// How many rules were still pending when the fixed point was reached —
    /// waiting for information that can no longer arrive.
    pub rules_pending: usize,
}

/// Which rules have fired.
///
/// A rule fires at most once per execution: with an immutable fact set (owner
/// decision OD-1), a second firing could only re-derive what it already
/// derived, so firing once keeps the trace finite and readable without changing
/// the result.
struct RuleState {
    fired: Vec<bool>,
}

impl RuleState {
    fn new(rules: usize) -> Self {
        Self {
            fired: vec![false; rules],
        }
    }

    fn has_fired(&self, index: usize) -> bool {
        self.fired.get(index).copied().unwrap_or(false)
    }

    fn mark(&mut self, index: usize) {
        if let Some(slot) = self.fired.get_mut(index) {
            *slot = true;
        }
    }
}

/// Run inference to the fixed point, with the default conflict strategy.
///
/// # Errors
/// See [`infer_with`].
pub fn infer(
    program: &IrProgram,
    facts: &mut FactSet,
    trace: &mut Trace,
    max_rounds: usize,
) -> Result<InferenceReport, Diagnostic> {
    infer_with(program, facts, trace, max_rounds, &RaiseConflict)
}

/// Run inference to the fixed point under an explicit conflict strategy.
///
/// `facts` starts with the declared facts and ends holding everything the
/// program can conclude. Events are appended to `trace` as they happen, so a
/// failed execution still leaves the trace it produced up to the failure.
///
/// # Errors
/// * `E4001` — two rules reached incompatible conclusions, and the strategy
///   raised rather than resolving.
/// * `E8001` — `max_rounds` was exhausted, which a correct engine never does.
/// * whatever evaluation raises: overflow, division by zero, a non-finite
///   float, or misuse of `null`.
pub fn infer_with(
    program: &IrProgram,
    facts: &mut FactSet,
    trace: &mut Trace,
    max_rounds: usize,
    strategy: &dyn ConflictStrategy,
) -> Result<InferenceReport, Diagnostic> {
    let mut state = RuleState::new(program.rules.len());
    let mut rules_fired = 0;
    let mut conflicts = 0;

    for round in 1..=max_rounds {
        trace.record(TraceEventKind::RoundStarted { round });
        let mut fired_any = false;
        let mut pending = 0;

        // Source order. It is part of the language's observable behaviour, and
        // it is why a `Vec` is iterated here rather than any keyed collection.
        for (index, rule) in program.rules.iter().enumerate() {
            if state.has_fired(index) {
                continue;
            }

            let outcome = eval(&rule.condition, facts, rule.span)?;
            match outcome {
                Value::Unknown => {
                    pending += 1;
                    let waiting_for = facts
                        .missing_from(&rule.reads)
                        .into_iter()
                        .map(|name| program.name_text(name))
                        .collect();
                    trace.record(TraceEventKind::ConditionEvaluated {
                        rule: rule.name.clone(),
                        outcome: ConditionOutcome::Unknown,
                    });
                    trace.record(TraceEventKind::RulePending {
                        rule: rule.name.clone(),
                        waiting_for,
                    });
                    continue;
                }
                // A `Bool`-typed name can hold `null` at run time, so a
                // condition can be `null` even though it type-checks. `null` is
                // neither true nor false, and guessing either way would collapse
                // two states the language keeps distinct.
                Value::Null => return Err(null_condition(rule)),
                Value::Known(_) => {}
            }

            let Some(holds) = outcome.as_bool() else {
                return Err(Diagnostic::new(
                    Code::InternalInvariant,
                    rule.span,
                    format!("the condition of rule `{}` is not a boolean", rule.name),
                ));
            };
            trace.record(TraceEventKind::ConditionEvaluated {
                rule: rule.name.clone(),
                outcome: if holds {
                    ConditionOutcome::True
                } else {
                    ConditionOutcome::False
                },
            });
            if !holds {
                continue;
            }

            activate(program, rule, facts, trace, strategy, round, &mut conflicts)?;
            state.mark(index);
            rules_fired += 1;
            fired_any = true;
        }

        trace.record(TraceEventKind::RoundFinished {
            round,
            fired_any,
            pending,
        });
        if !fired_any {
            return Ok(InferenceReport {
                rounds: round,
                rules_fired,
                rules_pending: pending,
            });
        }
    }

    Err(Diagnostic::new(
        Code::InferenceRoundLimit,
        program.rules.first().map_or(Span::point(0), |rule| rule.span),
        format!("inference did not settle within {max_rounds} rounds"),
    )
    .with_cause("each round either fires a rule or ends the loop, and no rule fires twice")
    .with_help(
        "the semantics bound rounds by the number of rules, so this indicates a defect in the engine",
    ))
}

/// Fire a rule: evaluate every consequence and add it.
fn activate(
    program: &IrProgram,
    rule: &IrRule,
    facts: &mut FactSet,
    trace: &mut Trace,
    strategy: &dyn ConflictStrategy,
    round: usize,
    conflicts: &mut usize,
) -> Result<(), Diagnostic> {
    let reads: Vec<String> = rule
        .reads
        .iter()
        .map(|&name| program.name_text(name))
        .collect();
    let activation_seq = trace.record(TraceEventKind::RuleActivated {
        rule: rule.name.clone(),
        reads,
    });

    for effect in &rule.effects {
        let value = eval(&effect.code, facts, effect.span)?;
        if value.is_unknown() {
            // A consequence whose value is unknown while the condition was
            // known is possible — the condition need not read every name the
            // consequences do. Deriving `unknown` is meaningless (absence from
            // the fact set already means that), so the rule contributes nothing
            // for this name and says so.
            trace.record(TraceEventKind::RulePending {
                rule: rule.name.clone(),
                waiting_for: facts
                    .missing_from(&rule.reads)
                    .into_iter()
                    .map(|name| program.name_text(name))
                    .collect(),
            });
            continue;
        }

        let name = program.name_text(effect.name);
        let next_seq = trace.next_seq();
        match facts.insert(
            effect.name,
            value.clone(),
            Origin::Derived(rule.id),
            next_seq,
        ) {
            Insertion::Added => {
                trace.record(TraceEventKind::FactDerived {
                    name,
                    value,
                    rule: rule.name.clone(),
                });
            }
            Insertion::Redundant => {
                trace.record(TraceEventKind::DerivationRedundant {
                    name,
                    value,
                    rule: rule.name.clone(),
                });
            }
            Insertion::RejectedUnknown => {
                return Err(Diagnostic::new(
                    Code::InternalInvariant,
                    effect.span,
                    "an unknown value reached the fact set",
                ))
            }
            Insertion::Conflict { existing } => {
                *conflicts += 1;
                let existing_rule = match existing.origin() {
                    Origin::Declared => None,
                    Origin::Derived(other) => program.rule(other).map(|other| other.name.clone()),
                };
                let report = ConflictParts {
                    id: format!("C{conflicts}"),
                    name: name.clone(),
                    existing: ConflictSide {
                        rule: existing_rule.clone(),
                        value: existing.value().clone(),
                        trace_seq: existing.trace_seq(),
                    },
                    incoming: ConflictSide {
                        rule: Some(rule.name.clone()),
                        value: value.clone(),
                        trace_seq: next_seq,
                    },
                    relevant_facts: relevant_facts(program, rule, facts),
                    relevant_conditions: relevant_conditions(program, rule, existing.origin()),
                    round,
                    trace_refs: &[existing.trace_seq(), activation_seq, next_seq],
                }
                .into_report();

                match strategy.resolve(&report) {
                    Resolution::Raise => {
                        let diagnostic = conflict_diagnostic(&report, effect.span, strategy.name());
                        trace.record(TraceEventKind::ConflictRaised(Box::new(report)));
                        return Err(diagnostic);
                    }
                }
            }
        }
    }
    Ok(())
}

/// The facts the conflicting rules read, with their values right now.
fn relevant_facts(program: &IrProgram, rule: &IrRule, facts: &FactSet) -> Vec<(String, Value)> {
    rule.reads
        .iter()
        .map(|&name| {
            let value = facts.get(name).cloned().unwrap_or(Value::Unknown);
            (program.name_text(name), value)
        })
        .collect()
}

/// The conditions of both rules involved, as the IR renders them.
fn relevant_conditions(
    program: &IrProgram,
    rule: &IrRule,
    existing: Origin,
) -> Vec<(String, String)> {
    let mut conditions = vec![(rule.name.clone(), print_code(&rule.condition, program))];
    if let Origin::Derived(other) = existing {
        if let Some(other) = program.rule(other) {
            if other.id != rule.id {
                conditions.push((other.name.clone(), print_code(&other.condition, program)));
            }
        }
    }
    conditions
}

fn conflict_diagnostic(
    report: &lml_trace::ConflictReport,
    span: Span,
    strategy: &str,
) -> Diagnostic {
    let existing_by = report
        .existing
        .rule
        .as_deref()
        .unwrap_or("the fact declaration");
    let incoming_by = report
        .incoming
        .rule
        .as_deref()
        .unwrap_or("the fact declaration");
    Diagnostic::new(
        Code::ConflictingDerivation,
        span,
        format!(
            "{} [{}]: `{}` is {} by `{incoming_by}` and {} by `{existing_by}`",
            report.id, report.name, report.name, report.incoming.value, report.existing.value
        ),
    )
    .with_cause(format!(
        "round {}, conflict strategy `{strategy}`; trace events {:?}",
        report.round, report.trace_refs
    ))
    .with_help(
        "there is no implicit priority: two rules reaching incompatible conclusions is an error, \
         because any silent winner would depend on something the author did not write down",
    )
}

fn null_condition(rule: &IrRule) -> Diagnostic {
    Diagnostic::new(
        Code::NullNotBoolean,
        rule.span,
        format!("the condition of rule `{}` evaluated to `null`", rule.name),
    )
    .with_cause("`null` is a known absence, which is neither true nor false")
    .with_help("test for it with `is null` or `is known` before using the value as a condition")
}
