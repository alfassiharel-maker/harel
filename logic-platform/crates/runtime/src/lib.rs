//! The runtime: what actually executes a program.
//!
//! It joins the pieces — the IR, the fact set, inference and the trace — and
//! owns the state and the limits while they run. It does not parse, does not
//! lower, and does not know what a token is.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod execute;
mod limits;

pub use execute::{digest, run, Execution, ExecutionContext, Failure, Output, LANGUAGE_VERSION};
pub use limits::{Limits, MAX_INFERENCE_ROUNDS};
