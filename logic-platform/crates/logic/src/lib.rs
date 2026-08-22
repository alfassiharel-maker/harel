//! Core logical objects: Terms, Relations, Facts, Rules, Fact Store.
//!
//! Facts are immutable (§5.2).  The fact store deduplicates identical tuples.
//! Derived facts carry provenance so the system can explain their origin (§9).

use std::collections::HashMap;
use std::sync::Arc;

use serde::Serialize;
use types::Value;

/// Identifies a logic variable within a rule by its sequential index.
/// Bindings must never leak between independent rule evaluations (§11).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub struct VarId(pub u32);

/// A logic term: either a ground constant value or a variable placeholder.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub enum Term {
    Constant(Value),
    Variable(VarId),
}

/// A relation name — the name of a relation or predicate.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
pub struct RelName(pub String);

impl std::fmt::Display for RelName {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// A predicate application: relation name + argument terms.
/// May contain variables when inside a rule head or body.
#[derive(Debug, Clone, Serialize)]
pub struct Atom {
    pub relation: RelName,
    pub args: Vec<Term>,
}

/// How a fact came to exist.
#[derive(Debug, Clone, Serialize)]
pub enum Provenance {
    /// Asserted directly in the source program.
    Asserted,
    /// Derived by a rule.
    Derived {
        /// Index of the rule in the program's rule list.
        rule_idx: usize,
        /// The ground facts that matched the rule body.
        /// Skipped in serialisation to avoid recursive types; use `support_keys` for output.
        #[serde(skip)]
        support: Vec<Arc<GroundFact>>,
    },
}

/// A fully ground (variable-free) fact: relation + constant-value arguments.
/// Facts are immutable once created (§5.2, §9).
#[derive(Debug, Clone, Serialize)]
pub struct GroundFact {
    pub relation: RelName,
    pub args: Vec<Value>,
    pub provenance: Provenance,
}

impl GroundFact {
    /// The logical identity of a fact is its relation + argument tuple.
    /// Two facts with the same identity are logically identical regardless of provenance.
    pub fn identity(&self) -> (&RelName, &[Value]) {
        (&self.relation, &self.args)
    }
}

/// A compiled rule: head atom (may contain variables) + body atoms + variable count.
#[derive(Debug, Clone, Serialize)]
pub struct Rule {
    pub head: Atom,
    pub body: Vec<Atom>,
    /// Number of distinct variables in this rule.
    pub var_count: u32,
}

/// Stores all ground facts, deduplicated by (relation, args).
/// Facts are immutable; adding a logically identical fact is a no-op.
#[derive(Debug, Default)]
pub struct FactStore {
    /// Map from (relation, args) to the first asserted/derived fact with that identity.
    facts: HashMap<(RelName, Vec<Value>), Arc<GroundFact>>,
}

impl FactStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Insert a fact.  Returns `true` if the fact is new, `false` if already present.
    /// Does NOT mutate an existing fact (§5.2).
    pub fn insert(&mut self, fact: GroundFact) -> bool {
        let key = (fact.relation.clone(), fact.args.clone());
        if self.facts.contains_key(&key) {
            return false;
        }
        self.facts.insert(key, Arc::new(fact));
        true
    }

    /// Test whether a ground tuple is in the store.
    pub fn contains(&self, relation: &RelName, args: &[Value]) -> bool {
        self.facts
            .contains_key(&(relation.clone(), args.to_vec()))
    }

    /// Iterate all facts.
    pub fn iter(&self) -> impl Iterator<Item = &Arc<GroundFact>> {
        self.facts.values()
    }

    /// All facts for a given relation, in an unspecified order.
    pub fn facts_for(&self, relation: &RelName) -> Vec<Arc<GroundFact>> {
        self.facts
            .iter()
            .filter_map(|((rel, _), fact)| if rel == relation { Some(Arc::clone(fact)) } else { None })
            .collect()
    }

    pub fn len(&self) -> usize {
        self.facts.len()
    }

    pub fn is_empty(&self) -> bool {
        self.facts.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use types::{KnownValue, Value};

    fn alice() -> Value {
        Value::Known(KnownValue::Str("Alice".into()))
    }
    fn bob() -> Value {
        Value::Known(KnownValue::Str("Bob".into()))
    }

    #[test]
    fn insert_and_contains() {
        let mut store = FactStore::new();
        let fact = GroundFact {
            relation: RelName("parent".into()),
            args: vec![alice(), bob()],
            provenance: Provenance::Asserted,
        };
        assert!(store.insert(fact));
        assert!(store.contains(&RelName("parent".into()), &[alice(), bob()]));
    }

    #[test]
    fn duplicate_insert_is_noop() {
        let mut store = FactStore::new();
        let make = || GroundFact {
            relation: RelName("parent".into()),
            args: vec![alice(), bob()],
            provenance: Provenance::Asserted,
        };
        assert!(store.insert(make()));
        assert!(!store.insert(make()), "second insert must return false");
        assert_eq!(store.len(), 1, "store must not grow on duplicate");
    }

    #[test]
    fn facts_for_returns_only_matching_relation() {
        let mut store = FactStore::new();
        store.insert(GroundFact {
            relation: RelName("parent".into()),
            args: vec![alice(), bob()],
            provenance: Provenance::Asserted,
        });
        store.insert(GroundFact {
            relation: RelName("sibling".into()),
            args: vec![alice(), bob()],
            provenance: Provenance::Asserted,
        });
        assert_eq!(store.facts_for(&RelName("parent".into())).len(), 1);
    }

    #[test]
    fn derived_provenance_is_stored() {
        let mut store = FactStore::new();
        let base = Arc::new(GroundFact {
            relation: RelName("parent".into()),
            args: vec![alice(), bob()],
            provenance: Provenance::Asserted,
        });
        let derived = GroundFact {
            relation: RelName("ancestor".into()),
            args: vec![alice(), bob()],
            provenance: Provenance::Derived {
                rule_idx: 0,
                support: vec![Arc::clone(&base)],
            },
        };
        store.insert(derived);
        let facts = store.facts_for(&RelName("ancestor".into()));
        assert!(matches!(facts[0].provenance, Provenance::Derived { .. }));
    }
}
