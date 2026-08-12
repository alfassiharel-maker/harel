//! Interned names.
//!
//! After lowering, the engine works with indices rather than strings. That is
//! partly for speed and mostly for safety: a `NameId` can only be obtained by
//! interning a name that the semantic model already knew about, so the runtime
//! cannot invent a name at execution time.

use core::fmt;
use std::collections::BTreeMap;

/// A name, as an index into a [`NameTable`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct NameId(u32);

impl NameId {
    /// The index, for use by containers that store one entry per name.
    #[must_use]
    pub const fn index(self) -> usize {
        self.0 as usize
    }
}

impl fmt::Display for NameId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "#{}", self.0)
    }
}

/// Every name in a program, in the order they were first interned.
///
/// Interning order is the canonical source order — facts, then rules, then
/// outputs — so the same program always produces the same table, and therefore
/// the same IR text and the same digest.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct NameTable {
    names: Vec<String>,
    index: BTreeMap<String, NameId>,
}

impl NameTable {
    /// An empty table.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The id for `name`, assigning one if this is its first appearance.
    ///
    /// A program with more than `u32::MAX` names is not representable; the
    /// counter saturates rather than wrapping, and the duplicate id would be
    /// caught by the balance checks. In practice `MAX_SOURCE_BYTES` makes this
    /// unreachable — a 16 MiB source cannot hold four billion distinct names.
    pub fn intern(&mut self, name: &str) -> NameId {
        if let Some(&id) = self.index.get(name) {
            return id;
        }
        let id = NameId(u32::try_from(self.names.len()).unwrap_or(u32::MAX));
        self.names.push(name.to_owned());
        self.index.insert(name.to_owned(), id);
        id
    }

    /// The id for `name`, if it has one.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<NameId> {
        self.index.get(name).copied()
    }

    /// The name behind an id.
    ///
    /// `None` is only possible for an id from a different program, which the
    /// type system does not prevent; callers render it as `#n` rather than
    /// failing, so a malformed IR is inspectable rather than fatal.
    #[must_use]
    pub fn text(&self, id: NameId) -> Option<&str> {
        self.names.get(id.index()).map(String::as_str)
    }

    /// How many names the program has.
    #[must_use]
    pub fn len(&self) -> usize {
        self.names.len()
    }

    /// Whether the program has no names at all.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }

    /// Every name, in id order.
    pub fn iter(&self) -> impl Iterator<Item = (NameId, &str)> {
        self.names.iter().enumerate().map(|(index, name)| {
            (
                NameId(u32::try_from(index).unwrap_or(u32::MAX)),
                name.as_str(),
            )
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interning_is_stable_and_ordered_by_first_appearance() {
        let mut table = NameTable::new();
        let temperature = table.intern("temperature");
        let status = table.intern("status");
        assert_eq!(table.intern("temperature"), temperature);
        assert_eq!(temperature.index(), 0);
        assert_eq!(status.index(), 1);
        assert_eq!(table.text(status), Some("status"));
    }
}
