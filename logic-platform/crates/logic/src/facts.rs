//! The fact set.
//!
//! A `BTreeMap`, not a `HashMap`: iteration order is part of the language's
//! observable behaviour (`docs/01_LANGUAGE_CONSTITUTION.md` §6), and a hash map
//! would make traces differ between runs.

use lml_ir::{NameId, RuleId};
use lml_types::Value;
use std::collections::BTreeMap;

/// Where a fact came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    /// A `fact` declaration in the source.
    Declared,
    /// The `then` clause of a rule that fired.
    Derived(RuleId),
}

/// A fact: a value and where it came from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fact {
    /// The value.
    pub value: Value,
    /// What bound it.
    pub origin: Origin,
}

/// What happened when a binding was added.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Insertion {
    /// The name was not bound, and now is.
    Added,
    /// The name was already bound to an equal value. Recorded, not an error:
    /// two rules agreeing is not a conflict.
    Redundant,
    /// The name was bound to a different value.
    Conflict {
        /// The value already held.
        existing: Value,
        /// What bound it.
        origin: Origin,
    },
}

/// Every fact known at a point in an execution.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct FactSet {
    facts: BTreeMap<NameId, Fact>,
}

impl FactSet {
    /// An empty set.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The value bound to `name`, if any.
    ///
    /// `None` means *unknown*, which is not a value and is emphatically not
    /// `0`, `false` or `""` (`docs/04_TYPE_AND_DATA_MODEL.md` §4).
    #[must_use]
    pub fn get(&self, name: NameId) -> Option<&Value> {
        self.facts.get(&name).map(|fact| &fact.value)
    }

    /// The whole fact, value and origin.
    #[must_use]
    pub fn fact(&self, name: NameId) -> Option<&Fact> {
        self.facts.get(&name)
    }

    /// Whether `name` is bound.
    #[must_use]
    pub fn knows(&self, name: NameId) -> bool {
        self.facts.contains_key(&name)
    }

    /// Whether every name in `names` is bound — the test for whether a rule is
    /// evaluable.
    #[must_use]
    pub fn knows_all(&self, names: &[NameId]) -> bool {
        names.iter().all(|&name| self.knows(name))
    }

    /// The names in `names` that are not bound, in the order given. What a rule
    /// is waiting for, for the trace.
    #[must_use]
    pub fn missing_from(&self, names: &[NameId]) -> Vec<NameId> {
        names
            .iter()
            .copied()
            .filter(|&name| !self.knows(name))
            .collect()
    }

    /// Bind `name`, reporting what happened.
    ///
    /// Facts are immutable: this never overwrites. A conflicting binding is
    /// returned for the caller to report as `E4001`, because deciding what a
    /// conflict *means* is inference's job, not the fact set's.
    pub fn insert(&mut self, name: NameId, value: Value, origin: Origin) -> Insertion {
        match self.facts.get(&name) {
            None => {
                self.facts.insert(name, Fact { value, origin });
                Insertion::Added
            }
            Some(existing) if existing.value == value => Insertion::Redundant,
            Some(existing) => Insertion::Conflict {
                existing: existing.value.clone(),
                origin: existing.origin,
            },
        }
    }

    /// How many facts are known.
    #[must_use]
    pub fn len(&self) -> usize {
        self.facts.len()
    }

    /// Whether nothing is known.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.facts.is_empty()
    }

    /// Every fact, in name-id order — which is source order of first
    /// appearance, and therefore stable across runs.
    pub fn iter(&self) -> impl Iterator<Item = (NameId, &Fact)> {
        self.facts.iter().map(|(&name, fact)| (name, fact))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn name(index: u32) -> NameId {
        let mut table = lml_ir::NameTable::new();
        for i in 0..=index {
            table.intern(&format!("n{i}"));
        }
        table
            .get(&format!("n{index}"))
            .unwrap_or_else(|| unreachable!())
    }

    #[test]
    fn a_fact_is_bound_once() {
        let mut facts = FactSet::new();
        assert_eq!(
            facts.insert(name(0), Value::Int(1), Origin::Declared),
            Insertion::Added
        );
        assert_eq!(
            facts.insert(name(0), Value::Int(1), Origin::Derived(RuleId(0))),
            Insertion::Redundant
        );
        assert_eq!(
            facts.insert(name(0), Value::Int(2), Origin::Derived(RuleId(0))),
            Insertion::Conflict {
                existing: Value::Int(1),
                origin: Origin::Declared
            }
        );
        // The original value survives every attempt.
        assert_eq!(facts.get(name(0)), Some(&Value::Int(1)));
    }

    #[test]
    fn unknown_is_none_not_a_default() {
        let facts = FactSet::new();
        assert_eq!(facts.get(name(0)), None);
        assert!(!facts.knows(name(0)));
    }

    #[test]
    fn missing_names_are_reported_in_order() {
        let mut facts = FactSet::new();
        facts.insert(name(1), Value::Int(1), Origin::Declared);
        assert_eq!(
            facts.missing_from(&[name(0), name(1), name(2)]),
            vec![name(0), name(2)]
        );
    }
}
