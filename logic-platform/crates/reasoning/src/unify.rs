//! Unification (§12).
//!
//! `unify(t1, t2, subst)` returns a new substitution extending `subst` if t1
//! and t2 can be made equal, or `None` on failure.
//!
//! Substitutions are immutable maps from VarId → Term (§12).
//! Bindings must never leak between independent rule evaluations (§11).

use std::collections::HashMap;
use logic::{Term, VarId};
use types::Value;

/// An immutable mapping from variable ids to terms.
pub type Substitution = HashMap<VarId, Term>;

/// Walk a term through a substitution to its terminal value.
fn walk(term: &Term, subst: &Substitution) -> Term {
    match term {
        Term::Variable(var) => {
            if let Some(bound) = subst.get(var) {
                walk(bound, subst)
            } else {
                term.clone()
            }
        }
        Term::Constant(_) => term.clone(),
    }
}

/// Attempt to unify `t1` and `t2` under `subst`, returning the extended
/// substitution on success or `None` on failure.
///
/// Implements Robinson's algorithm (autonomous decision §41).
pub fn unify(t1: &Term, t2: &Term, subst: &Substitution) -> Option<Substitution> {
    let t1 = walk(t1, subst);
    let t2 = walk(t2, subst);

    match (&t1, &t2) {
        // Both constants — unify by value equality.
        (Term::Constant(v1), Term::Constant(v2)) => {
            if v1 == v2 {
                Some(subst.clone())
            } else {
                None
            }
        }

        // Variable on the left — bind it.
        (Term::Variable(var), _) => {
            let mut new_subst = subst.clone();
            new_subst.insert(*var, t2);
            Some(new_subst)
        }

        // Variable on the right — bind it.
        (_, Term::Variable(var)) => {
            let mut new_subst = subst.clone();
            new_subst.insert(*var, t1);
            Some(new_subst)
        }
    }
}

/// Apply a substitution to a term, replacing variables with their bindings.
pub fn apply(term: &Term, subst: &Substitution) -> Term {
    walk(term, subst)
}

/// Apply a substitution to an atom's argument list, returning ground values.
/// Returns `None` if any term remains unbound (a variable with no substitution).
pub fn ground_args(args: &[Term], subst: &Substitution) -> Option<Vec<Value>> {
    args.iter()
        .map(|t| {
            let resolved = apply(t, subst);
            match resolved {
                Term::Constant(v) => Some(v),
                Term::Variable(_) => None,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use logic::VarId;
    use types::{KnownValue, Value};

    fn str_val(s: &str) -> Term {
        Term::Constant(Value::Known(KnownValue::Str(s.to_string())))
    }
    fn var(id: u32) -> Term {
        Term::Variable(VarId(id))
    }

    #[test]
    fn unify_constant_with_itself() {
        let subst = Substitution::new();
        let result = unify(&str_val("Alice"), &str_val("Alice"), &subst);
        assert!(result.is_some());
    }

    #[test]
    fn unify_different_constants_fails() {
        let subst = Substitution::new();
        let result = unify(&str_val("Alice"), &str_val("Bob"), &subst);
        assert!(result.is_none());
    }

    #[test]
    fn unify_variable_with_constant() {
        let subst = Substitution::new();
        let result = unify(&var(0), &str_val("Alice"), &subst).unwrap();
        assert_eq!(result.get(&VarId(0)), Some(&str_val("Alice")));
    }

    #[test]
    fn unify_constant_with_variable() {
        let subst = Substitution::new();
        let result = unify(&str_val("Bob"), &var(1), &subst).unwrap();
        assert_eq!(result.get(&VarId(1)), Some(&str_val("Bob")));
    }

    #[test]
    fn unify_variable_with_bound_variable() {
        let mut subst = Substitution::new();
        subst.insert(VarId(0), str_val("Alice"));
        // Var(0) is already bound to "Alice"; unifying Var(0) with "Alice" must succeed.
        let result = unify(&var(0), &str_val("Alice"), &subst);
        assert!(result.is_some());
    }

    #[test]
    fn bindings_do_not_leak() {
        // Two independent substitutions must not share state.
        let subst1 = Substitution::new();
        let subst2 = unify(&var(0), &str_val("X"), &subst1).unwrap();
        let subst3 = unify(&var(0), &str_val("Y"), &subst1).unwrap();
        // subst2 and subst3 are both derived from subst1; they must be independent.
        assert_eq!(subst2.get(&VarId(0)), Some(&str_val("X")));
        assert_eq!(subst3.get(&VarId(0)), Some(&str_val("Y")));
    }

    #[test]
    fn ground_args_resolves_variables() {
        let mut subst = Substitution::new();
        subst.insert(VarId(0), str_val("Alice"));
        subst.insert(VarId(1), str_val("Bob"));
        let args = vec![var(0), var(1)];
        let ground = ground_args(&args, &subst).unwrap();
        assert_eq!(
            ground,
            vec![
                Value::Known(KnownValue::Str("Alice".into())),
                Value::Known(KnownValue::Str("Bob".into())),
            ]
        );
    }

    #[test]
    fn ground_args_returns_none_for_unbound_variable() {
        let subst = Substitution::new();
        let args = vec![var(0)];
        assert!(ground_args(&args, &subst).is_none());
    }
}
