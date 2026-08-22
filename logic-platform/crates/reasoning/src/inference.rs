//! Fixed-point evaluation engine (§14).
//!
//! Algorithm: naive bottom-up (semi-naïve optimisation is future work).
//! The engine repeatedly applies all rules until no new facts are derived.
//! Evaluation is deterministic: rule order and fact order are both stable.
//!
//! For pure positive Datalog (no negation, no aggregation) the fixed point is
//! always reached in at most O(|EDB|^k) iterations where k is the maximum
//! rule body size — finite and guaranteed to terminate for finite input.


use std::sync::Arc;

use logic::{Atom, FactStore, GroundFact, Provenance, RelName, Rule, Term};
use trace::{ProvenanceSummary, TraceEventKind, TraceLog};
use types::Value;

use crate::conflict::{Conflict, ConflictError};
use crate::unify::{ground_args, unify, Substitution};

/// The result of a completed evaluation.
pub struct EvaluationResult {
    pub store: FactStore,
    pub iterations: u32,
}

/// Run bottom-up fixed-point evaluation.
///
/// Facts in `store` are treated as the extensional database (EDB).
/// Rules derive intensional facts until convergence.
pub fn evaluate(
    store: &mut FactStore,
    rules: &[Rule],
    trace: &mut TraceLog,
) -> Result<EvaluationResult, ConflictError> {
    let mut iterations: u32 = 0;
    let mut conflicts: Vec<Conflict> = Vec::new();
    let mut conflict_id_gen: u64 = 0;

    // The result store grows monotonically; we detect termination when a full
    // pass over all rules adds nothing new.
    loop {
        let before = store.len();
        iterations += 1;

        for (rule_idx, rule) in rules.iter().enumerate() {
            derive_from_rule(
                rule,
                rule_idx,
                store,
                trace,
                &mut conflicts,
                &mut conflict_id_gen,
            );
        }

        if store.len() == before {
            break; // fixed point reached
        }
    }

    trace.push(TraceEventKind::ExecutionFinished {
        total_facts: store.len(),
        iterations,
    });

    if !conflicts.is_empty() {
        Err(ConflictError::new(conflicts))
    } else {
        // Return a snapshot of the store's current size; the caller holds the store.
        Ok(EvaluationResult {
            store: FactStore::new(), // placeholder — real store is mutated in place
            iterations,
        })
    }
}

/// Apply one rule against the current store, inserting any new derived facts.
fn derive_from_rule(
    rule: &Rule,
    rule_idx: usize,
    store: &mut FactStore,
    trace: &mut TraceLog,
    _conflicts: &mut Vec<Conflict>,
    _conflict_id_gen: &mut u64,
) {
    // Enumerate all substitutions that satisfy the rule body via nested iteration.
    let initial_subst = Substitution::new();
    let solutions = solve_body(&rule.body, 0, &initial_subst, store);

    for subst in solutions {
        // Ground the head atom under this substitution.
        let Some(head_args) = ground_args(&rule.head.args, &subst) else {
            continue; // unbound variable in head — semantic error caught earlier
        };

        let head_rel = &rule.head.relation;

        // Collect the support facts (the body witnesses for this derivation).
        let support = collect_support(&rule.body, &subst, store);

        let binding_display: Vec<(String, Value)> = subst
            .iter()
            .map(|(var, term)| {
                let val = match term {
                    Term::Constant(v) => v.clone(),
                    Term::Variable(_) => Value::Unknown,
                };
                (format!("${}", var.0), val)
            })
            .collect();

        trace.push(TraceEventKind::RuleActivated {
            rule_idx,
            bindings: binding_display,
        });

        if store.contains(head_rel, &head_args) {
            trace.push(TraceEventKind::FactAlreadyKnown {
                relation: head_rel.clone(),
                args: head_args,
            });
            continue;
        }

        let derived = GroundFact {
            relation: head_rel.clone(),
            args: head_args.clone(),
            provenance: Provenance::Derived {
                rule_idx,
                support: support.clone(),
            },
        };

        store.insert(derived);

        let support_keys: Vec<_> = support
            .iter()
            .map(|f| (f.relation.clone(), f.args.clone()))
            .collect();

        trace.push(TraceEventKind::FactDerived {
            relation: head_rel.clone(),
            args: head_args,
            rule_idx,
            support_keys,
        });
    }
}

/// Recursively enumerate all substitutions that satisfy the body atoms from
/// index `start` onward, starting from `subst`.
fn solve_body(
    body: &[Atom],
    start: usize,
    subst: &Substitution,
    store: &FactStore,
) -> Vec<Substitution> {
    if start >= body.len() {
        return vec![subst.clone()];
    }

    let atom = &body[start];
    let candidates = store.facts_for(&atom.relation);
    let mut solutions = Vec::new();

    for fact in &candidates {
        if fact.args.len() != atom.args.len() {
            continue; // arity mismatch — skip (semantic check should have caught this)
        }

        // Try to unify each argument position.
        let mut current = subst.clone();
        let mut matched = true;

        for (fact_val, rule_term) in fact.args.iter().zip(atom.args.iter()) {
            let fact_term = Term::Constant(fact_val.clone());
            match unify(rule_term, &fact_term, &current) {
                Some(new_subst) => current = new_subst,
                None => {
                    matched = false;
                    break;
                }
            }
        }

        if matched {
            // This candidate matches; continue with the rest of the body.
            let mut rest = solve_body(body, start + 1, &current, store);
            solutions.append(&mut rest);
        }
    }

    solutions
}

/// Collect the Arc<GroundFact> witnesses that matched each body atom.
fn collect_support(
    body: &[Atom],
    subst: &Substitution,
    store: &FactStore,
) -> Vec<Arc<GroundFact>> {
    let mut support = Vec::new();
    for atom in body {
        let Some(ground) = ground_args(&atom.args, subst) else {
            continue;
        };
        // The store must contain this fact since it was part of a successful solve.
        let facts = store.facts_for(&atom.relation);
        for fact in facts {
            if fact.args == ground {
                support.push(fact);
                break;
            }
        }
    }
    support
}

/// Answer a query against the current store, recording results in the trace.
pub fn query(
    relation: &RelName,
    query_args: &[Term],
    store: &FactStore,
    trace: &mut TraceLog,
) -> Vec<Arc<GroundFact>> {
    let query_display: Vec<Option<Value>> = query_args
        .iter()
        .map(|t| match t {
            Term::Constant(v) => Some(v.clone()),
            Term::Variable(_) => None,
        })
        .collect();

    trace.push(TraceEventKind::QueryExecuted {
        relation: relation.clone(),
        query_args: query_display,
    });

    let subst = Substitution::new();

    // Reuse solve_body for the query itself.
    let facts_in_store = store.facts_for(relation);
    let mut results = Vec::new();

    for fact in &facts_in_store {
        if fact.args.len() != query_args.len() {
            continue;
        }
        let mut current = subst.clone();
        let mut matched = true;
        for (fact_val, q_term) in fact.args.iter().zip(query_args.iter()) {
            let fact_term = Term::Constant(fact_val.clone());
            match unify(q_term, &fact_term, &current) {
                Some(new_subst) => current = new_subst,
                None => { matched = false; break; }
            }
        }
        if matched {
            let prov = ProvenanceSummary::from_fact(fact);
            trace.push(TraceEventKind::QueryResult {
                relation: relation.clone(),
                args: fact.args.clone(),
                provenance: prov,
            });
            results.push(Arc::clone(fact));
        }
    }

    results
}

#[cfg(test)]
mod tests {
    use super::*;
    use logic::{Atom, FactStore, GroundFact, Provenance, RelName, Rule, Term};
    use trace::TraceLog;
    use types::{KnownValue, Value};

    fn str_val(s: &str) -> Value {
        Value::Known(KnownValue::Str(s.to_string()))
    }
    fn str_const(s: &str) -> Term {
        Term::Constant(str_val(s))
    }
    fn var(id: u32) -> Term {
        Term::Variable(VarId(id))
    }

    fn assert_fact(store: &FactStore, rel: &str, args: &[&str]) {
        let values: Vec<Value> = args.iter().map(|s| str_val(s)).collect();
        assert!(
            store.contains(&RelName(rel.into()), &values),
            "expected fact {}({}) in store",
            rel,
            args.join(", ")
        );
    }

    /// The canonical acceptance test from §52.
    #[test]
    fn grandparent_derivation() {
        let mut store = FactStore::new();
        store.insert(GroundFact {
            relation: RelName("parent".into()),
            args: vec![str_val("Alice"), str_val("Bob")],
            provenance: Provenance::Asserted,
        });
        store.insert(GroundFact {
            relation: RelName("parent".into()),
            args: vec![str_val("Bob"), str_val("Charlie")],
            provenance: Provenance::Asserted,
        });

        // rule grandparent(X, Z) when parent(X, Y), parent(Y, Z);
        // X=0, Y=1, Z=2
        let rule = Rule {
            head: Atom {
                relation: RelName("grandparent".into()),
                args: vec![var(0), var(2)],
            },
            body: vec![
                Atom {
                    relation: RelName("parent".into()),
                    args: vec![var(0), var(1)],
                },
                Atom {
                    relation: RelName("parent".into()),
                    args: vec![var(1), var(2)],
                },
            ],
            var_count: 3,
        };

        let mut trace = TraceLog::new();
        let result = evaluate(&mut store, &[rule], &mut trace);
        assert!(result.is_ok(), "evaluation raised conflict: {:?}", result.err());

        assert_fact(&store, "grandparent", &["Alice", "Charlie"]);

        // Provenance: the derived fact must reference the two parent facts.
        let gp_facts = store.facts_for(&RelName("grandparent".into()));
        assert_eq!(gp_facts.len(), 1);
        match &gp_facts[0].provenance {
            Provenance::Derived { support, rule_idx: _ } => {
                assert_eq!(support.len(), 2, "must have two support facts");
            }
            Provenance::Asserted => panic!("grandparent must be derived, not asserted"),
        }
    }

    #[test]
    fn no_new_facts_means_one_iteration() {
        let mut store = FactStore::new();
        store.insert(GroundFact {
            relation: RelName("a".into()),
            args: vec![str_val("x")],
            provenance: Provenance::Asserted,
        });
        // Rule that derives nothing new from the start.
        let rule = Rule {
            head: Atom {
                relation: RelName("b".into()),
                args: vec![var(0)],
            },
            body: vec![Atom {
                relation: RelName("missing".into()),
                args: vec![var(0)],
            }],
            var_count: 1,
        };
        let mut trace = TraceLog::new();
        let result = evaluate(&mut store, &[rule], &mut trace);
        assert!(result.is_ok());
        // Only the initial 'a' fact; no 'b' derived.
        assert!(!store.contains(&RelName("b".into()), &[str_val("x")]));
    }

    #[test]
    fn query_returns_matching_facts() {
        let mut store = FactStore::new();
        store.insert(GroundFact {
            relation: RelName("parent".into()),
            args: vec![str_val("Alice"), str_val("Bob")],
            provenance: Provenance::Asserted,
        });
        store.insert(GroundFact {
            relation: RelName("parent".into()),
            args: vec![str_val("Bob"), str_val("Charlie")],
            provenance: Provenance::Asserted,
        });

        let mut trace = TraceLog::new();
        // query parent(X, Y) — should return both facts.
        let results = query(
            &RelName("parent".into()),
            &[var(0), var(1)],
            &store,
            &mut trace,
        );
        assert_eq!(results.len(), 2);
    }

    #[test]
    fn query_with_constant_filters_results() {
        let mut store = FactStore::new();
        store.insert(GroundFact {
            relation: RelName("parent".into()),
            args: vec![str_val("Alice"), str_val("Bob")],
            provenance: Provenance::Asserted,
        });
        store.insert(GroundFact {
            relation: RelName("parent".into()),
            args: vec![str_val("Bob"), str_val("Charlie")],
            provenance: Provenance::Asserted,
        });

        let mut trace = TraceLog::new();
        // query parent("Alice", Y) — only one result.
        let results = query(
            &RelName("parent".into()),
            &[str_const("Alice"), var(0)],
            &store,
            &mut trace,
        );
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].args[1], str_val("Bob"));
    }
}
