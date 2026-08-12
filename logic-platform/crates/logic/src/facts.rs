//! The fact set.
//!
//! A `BTreeMap`, not a `HashMap`: iteration order is part of the language's
//! observable behaviour (`docs/01_LANGUAGE_CONSTITUTION.md` §6), and a hash map
//! would make traces differ between runs.
//!
//! **A fact holds `Null` or a `Known` value, never `Unknown`.** Absence from
//! this map *is* `Unknown`, so storing it would create a second spelling of the
//! same state — exactly the collapse owner decision OD-2 forbids. The
//! constructor enforces it: [`FactSet::insert`] rejects `Unknown`.

use lml_ir::{NameId, RuleId};
use lml_types::Value;
use std::collections::BTreeMap;

/// Where a fact came from.
///
/// Owner decision OD-1 requires the language to distinguish `Fact`,
/// `Observation`, `Derived Fact` and `State`. Two of the four exist here.
/// `Observation` and `State` are **not** listed as variants: their
/// representation is a separate specification, and a variant nothing can
/// produce would be a placeholder pretending to be a category.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    /// A `fact` declaration in the source.
    Declared,
    /// The `then` clause of a rule that fired — a *derived fact*.
    Derived(RuleId),
}

/// A fact: a value, where it came from, and where that is recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fact {
    value: Value,
    origin: Origin,
    trace_seq: u64,
}

impl Fact {
    /// The value. Never `Unknown`.
    #[must_use]
    pub const fn value(&self) -> &Value {
        &self.value
    }

    /// What bound it.
    #[must_use]
    pub const fn origin(&self) -> Origin {
        self.origin
    }

    /// The sequence number of the trace event that recorded the binding, so a
    /// conflict report can point at it (`docs/03_EXECUTION_MODEL.md` §6a).
    #[must_use]
    pub const fn trace_seq(&self) -> u64 {
        self.trace_seq
    }
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
        /// The fact that was already held.
        existing: Fact,
    },
    /// `Unknown` was offered as a value. Absence from the set already means
    /// unknown, so this is an engine defect rather than a program error.
    RejectedUnknown,
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

    /// The value bound to `name`, if it is bound.
    ///
    /// `None` means **unknown** — not enough information. It is not `Null`,
    /// which is a bound value meaning *known to be absent*, and it is
    /// emphatically not `0`, `false` or `""`.
    #[must_use]
    pub fn get(&self, name: NameId) -> Option<&Value> {
        self.facts.get(&name).map(|fact| &fact.value)
    }

    /// The whole fact: value, origin and trace position.
    #[must_use]
    pub fn fact(&self, name: NameId) -> Option<&Fact> {
        self.facts.get(&name)
    }

    /// Whether `name` is bound to anything, including `Null`.
    #[must_use]
    pub fn knows(&self, name: NameId) -> bool {
        self.facts.contains_key(&name)
    }

    /// The names in `names` that are not bound, in the order given.
    ///
    /// What a pending rule is waiting for, for the trace. A name bound to
    /// `Null` is *not* missing: it is known, and owner decision O2.5 makes a
    /// rule reading it evaluable.
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
    /// Facts are immutable (owner decision OD-1): this never overwrites, and
    /// there is no operation that does. A conflicting binding is returned for
    /// the caller to resolve, because deciding what a conflict *means* is
    /// inference's job, not the fact set's.
    pub fn insert(
        &mut self,
        name: NameId,
        value: Value,
        origin: Origin,
        trace_seq: u64,
    ) -> Insertion {
        if value.is_unknown() {
            return Insertion::RejectedUnknown;
        }
        match self.facts.get(&name) {
            None => {
                self.facts.insert(
                    name,
                    Fact {
                        value,
                        origin,
                        trace_seq,
                    },
                );
                Insertion::Added
            }
            Some(existing) if existing.value == value => Insertion::Redundant,
            Some(existing) => Insertion::Conflict {
                existing: existing.clone(),
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
            .unwrap_or_else(|| unreachable!("just interned"))
    }

    #[test]
    fn a_fact_is_bound_once() {
        let mut facts = FactSet::new();
        assert_eq!(
            facts.insert(name(0), Value::int(1), Origin::Declared, 0),
            Insertion::Added
        );
        assert_eq!(
            facts.insert(name(0), Value::int(1), Origin::Derived(RuleId(0)), 1),
            Insertion::Redundant
        );
        assert!(matches!(
            facts.insert(name(0), Value::int(2), Origin::Derived(RuleId(0)), 2),
            Insertion::Conflict { .. }
        ));
        // The original value survives every attempt: facts are immutable.
        assert_eq!(facts.get(name(0)), Some(&Value::int(1)));
    }

    #[test]
    fn null_is_a_binding_but_unknown_is_not() {
        let mut facts = FactSet::new();
        assert_eq!(
            facts.insert(name(0), Value::Null, Origin::Declared, 0),
            Insertion::Added
        );
        assert!(facts.knows(name(0)), "a name bound to null is known");
        assert_eq!(facts.get(name(0)), Some(&Value::Null));

        assert_eq!(
            facts.insert(name(1), Value::Unknown, Origin::Declared, 1),
            Insertion::RejectedUnknown,
            "absence already means unknown; storing it would be a second spelling"
        );
        assert!(!facts.knows(name(1)));
    }

    #[test]
    fn unknown_is_none_not_a_default() {
        let facts = FactSet::new();
        assert_eq!(facts.get(name(0)), None);
        assert!(!facts.knows(name(0)));
    }

    #[test]
    fn a_null_binding_is_not_missing() {
        let mut facts = FactSet::new();
        facts.insert(name(1), Value::Null, Origin::Declared, 0);
        assert_eq!(
            facts.missing_from(&[name(0), name(1), name(2)]),
            vec![name(0), name(2)]
        );
    }
}
