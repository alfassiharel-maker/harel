//! Execution traces.
//!
//! The trace is part of the engine's observable behaviour, not a debugging aid:
//! two runtimes that agree on outputs but disagree on traces are not both
//! correct (`docs/01_LANGUAGE_CONSTITUTION.md` §3.8). This crate defines the
//! events, records them, renders them, and reconstructs explanations from them.
//!
//! It is not a logger. There are no levels here, and no `log` call anywhere in
//! the workspace writes into a trace.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod digest;
mod event;
mod explain;
mod json;
mod trace;

pub use digest::{sha256, sha256_hex};
pub use event::{ConditionOutcome, ConflictReport, ConflictSide, TraceEvent, TraceEventKind};
pub use explain::{why, Explanation};
pub use json::{json_string, json_value, to_json, TRACE_VERSION};
pub use trace::{Trace, TraceMetadata};
