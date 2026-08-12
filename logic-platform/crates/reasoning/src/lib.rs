//! Inference: the forward-chaining fixed point of
//! `docs/02_FORMAL_SEMANTICS.md` §4.3.
//!
//! This crate owns the *strategy* — which rule to try, in what order, how many
//! times, and what a conflict means — and nothing else. It does not know how a
//! value is represented or how an expression is evaluated; that is `lml-logic`.
//! The split is what makes a second strategy (backward chaining, for the `query`
//! form that does not exist yet) an addition rather than a rewrite.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use lml_diagnostics::{Code, Diagnostic};
use lml_ir::{IrProgram, IrRule, RuleId};
use lml_logic::{eval, Evaluated, FactSet, Insertion, Origin};
use lml_trace::{Trace, TraceEventKind};

/// What one inference produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InferenceReport {
    /// How many rounds ran, including the final round that fired nothing.
    pub rounds: usize,
    /// How many rules fired.
    pub rules_fired: usize,
}

/// Which rules have fired.
///
/// A rule fires at most once per execution: with an immutable fact set, a
/// second firing could only re-derive what it already derived, so firing once
/// keeps the trace finite and readable without changing the result
/// (`docs/01_LANGUAGE_CONSTITUTION.md` §3.2).
#[derive(Debug, Clone, PartialEq, Eq)]
struct RuleState {
    fired: Vec<bool>,
}

impl RuleState {
    fn new(rules: usize) -> Self {
        Self {
            fired: vec![false; rules],
        }
    }

    fn has_fired(&self, id: RuleId) -> bool {
        self.fired.get(id.index()).copied().unwrap_or(false)
    }

    fn mark(&mut self, id: RuleId) {
        if let Some(slot) = self.fired.get_mut(id.index()) {
            *slot = true;
        }
    }
}

/// Run inference to the fixed point.
///
/// `facts` starts with the declared facts and ends holding everything the
/// program can conclude. Events are appended to `trace` as they happen, so a
/// failed execution still leaves the trace it produced up to the failure.
///
/// # Errors
/// * `E4001` — two rules derived different values for one name.
/// * `E8001` — `max_rounds` was exhausted, which a correct engine never does.
/// * whatever evaluation raises: overflow, division by zero, a non-finite float.
pub fn infer(
    program: &IrProgram,
    facts: &mut FactSet,
    trace: &mut Trace,
    max_rounds: usize,
) -> Result<InferenceReport, Diagnostic> {
    let mut state = RuleState::new(program.rules.len());
    let mut rules_fired = 0;
    let mut rounds = 0;

    for round in 1..=max_rounds {
        rounds = round;
        trace.record(TraceEventKind::RoundStarted { round });
        let mut fired_any = false;

        // Source order. It is part of the language's observable behaviour, and
        // it is why a `Vec` is iterated here rather than any keyed collection.
        for rule in &program.rules {
            if state.has_fired(rule.id) {
                continue;
            }
            if !facts.knows_all(&rule.reads) {
                let missing = facts
                    .missing_from(&rule.reads)
                    .into_iter()
                    .map(|name| program.name_text(name))
                    .collect();
                trace.record(TraceEventKind::RuleNotEvaluable {
                    rule: rule.name.clone(),
                    missing,
                });
                continue;
            }

            match eval(&rule.condition, facts, rule.span)? {
                // Unreachable: every name the rule reads is known, and reads
                // cover the condition. Reported rather than assumed.
                Evaluated::NotEvaluable => {
                    return Err(Diagnostic::new(
                        Code::InternalInvariant,
                        rule.span,
                        format!(
                            "rule `{}` was evaluable but its condition was not",
                            rule.name
                        ),
                    ))
                }
                Evaluated::Known(value) => {
                    let Some(holds) = value.as_bool() else {
                        return Err(Diagnostic::new(
                            Code::InternalInvariant,
                            rule.span,
                            format!("the condition of rule `{}` is not a boolean", rule.name),
                        ));
                    };
                    trace.record(TraceEventKind::ConditionEvaluated {
                        rule: rule.name.clone(),
                        result: holds,
                    });
                    if !holds {
                        continue;
                    }
                }
            }

            activate(program, rule, facts, trace)?;
            state.mark(rule.id);
            rules_fired += 1;
            fired_any = true;
        }

        trace.record(TraceEventKind::RoundFinished { round, fired_any });
        if !fired_any {
            return Ok(InferenceReport {
                rounds,
                rules_fired,
            });
        }
    }

    debug_assert_eq!(rounds, max_rounds);
    Err(Diagnostic::new(
        Code::InferenceRoundLimit,
        program.rules.first().map_or(lml_diagnostics::Span::point(0), |rule| rule.span),
        format!("inference did not settle within {max_rounds} rounds"),
    )
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
) -> Result<(), Diagnostic> {
    let reads = rule
        .reads
        .iter()
        .map(|&name| program.name_text(name))
        .collect();
    trace.record(TraceEventKind::RuleActivated {
        rule: rule.name.clone(),
        reads,
    });

    for effect in &rule.effects {
        let value = match eval(&effect.code, facts, effect.span)? {
            Evaluated::Known(value) => value,
            // Impossible: reads cover the consequences too, and the rule was
            // evaluable (`docs/02_FORMAL_SEMANTICS.md` §4.2).
            Evaluated::NotEvaluable => {
                return Err(Diagnostic::new(
                    Code::InternalInvariant,
                    effect.span,
                    "a firing rule's consequence was not evaluable",
                ))
            }
        };

        let name = program.name_text(effect.name);
        match facts.insert(effect.name, value.clone(), Origin::Derived(rule.id)) {
            Insertion::Added => trace.record(TraceEventKind::FactDerived {
                name,
                value,
                rule: rule.name.clone(),
            }),
            Insertion::Redundant => trace.record(TraceEventKind::DerivationRedundant {
                name,
                value,
                rule: rule.name.clone(),
            }),
            Insertion::Conflict { existing, origin } => {
                let by = match origin {
                    Origin::Declared => "the fact declaration".to_owned(),
                    Origin::Derived(other) => program.rule(other).map_or_else(
                        || other.to_string(),
                        |other| format!("rule `{}`", other.name),
                    ),
                };
                return Err(Diagnostic::new(
                    Code::ConflictingDerivation,
                    effect.span,
                    format!(
                        "rule `{}` derives `{name}` = {value}, but {by} already gave it {existing}",
                        rule.name
                    ),
                )
                .with_help(
                    "0.1 has no rule priority: two rules deriving different values for one name is an error, \
                     because any silent winner would depend on something the author did not write down",
                ));
            }
        }
    }
    Ok(())
}
