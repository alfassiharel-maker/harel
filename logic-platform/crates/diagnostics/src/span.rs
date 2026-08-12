//! Source locations.
//!
//! A `Span` is a byte range into one source text. Line and column are computed
//! on demand rather than carried, because they are needed only when a
//! diagnostic is rendered — which is the rare path — and carrying them would
//! double the size of every token.

/// A half-open byte range `[start, end)` into a source text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Span {
    /// Byte offset of the first byte.
    pub start: usize,
    /// Byte offset one past the last byte.
    pub end: usize,
}

impl Span {
    /// A span covering `[start, end)`.
    #[must_use]
    pub const fn new(start: usize, end: usize) -> Self {
        Self { start, end }
    }

    /// The empty span at `offset`, used for positions rather than ranges.
    #[must_use]
    pub const fn point(offset: usize) -> Self {
        Self {
            start: offset,
            end: offset,
        }
    }

    /// The smallest span covering both `self` and `other`.
    #[must_use]
    pub fn merge(self, other: Self) -> Self {
        Self {
            start: self.start.min(other.start),
            end: self.end.max(other.end),
        }
    }

    /// Length in bytes.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.end.saturating_sub(self.start)
    }

    /// Whether the span covers no bytes.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.start >= self.end
    }
}

/// A 1-based line and column, resolved against a specific source text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LineColumn {
    /// 1-based line number.
    pub line: usize,
    /// 1-based column, counted in Unicode scalar values (not bytes, not
    /// display width) so that it matches what an editor's caret reports.
    pub column: usize,
}

impl LineColumn {
    /// Resolve a byte `offset` within `source`.
    ///
    /// An offset past the end of the source resolves to the position just after
    /// the last character; this cannot fail, because a diagnostic that cannot
    /// be printed is worse than one printed at an approximate place.
    #[must_use]
    pub fn resolve(source: &str, offset: usize) -> Self {
        let mut line = 1;
        let mut column = 1;
        for (index, ch) in source.char_indices() {
            if index >= offset {
                return Self { line, column };
            }
            if ch == '\n' {
                line += 1;
                column = 1;
            } else {
                column += 1;
            }
        }
        Self { line, column }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merge_covers_both() {
        assert_eq!(Span::new(2, 4).merge(Span::new(9, 11)), Span::new(2, 11));
        assert_eq!(Span::new(9, 11).merge(Span::new(2, 4)), Span::new(2, 11));
    }

    #[test]
    fn resolve_counts_lines_from_one() {
        let src = "fact a = 1\nfact b = 2\n";
        assert_eq!(
            LineColumn::resolve(src, 0),
            LineColumn { line: 1, column: 1 }
        );
        assert_eq!(
            LineColumn::resolve(src, 11),
            LineColumn { line: 2, column: 1 }
        );
        assert_eq!(
            LineColumn::resolve(src, 16),
            LineColumn { line: 2, column: 6 }
        );
    }

    #[test]
    fn resolve_counts_columns_in_characters_not_bytes() {
        // The string literal holds multi-byte characters; the column after it
        // must count characters, which is what an editor shows.
        let src = "fact a = \"מזג\"";
        // 14 characters, 17 bytes: the position after the last one is column 15.
        assert_eq!(src.chars().count(), 14);
        assert_eq!(LineColumn::resolve(src, src.len()).column, 15);
    }

    #[test]
    fn resolve_past_end_does_not_panic() {
        assert_eq!(
            LineColumn::resolve("ab", 999),
            LineColumn { line: 1, column: 3 }
        );
    }
}
