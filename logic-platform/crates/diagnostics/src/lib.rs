//! Spans, error codes and diagnostics — the vocabulary every other crate uses
//! to report a problem.
//!
//! This crate is a leaf: it depends on nothing, knows nothing about tokens, the
//! syntax tree or values, and can therefore be depended on by every layer
//! without creating an unwanted edge. See `docs/05_ARCHITECTURE.md` §3.1 for why
//! it exists at all.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod code;
mod diagnostic;
mod span;

pub use code::{Code, Phase};
pub use diagnostic::{Diagnostic, Diagnostics, Label};
pub use span::{LineColumn, Span};
