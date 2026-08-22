//! Runtime: load a parsed program, run inference, answer queries.
//!
//! The runtime is the integration point between the front-end (AST) and the
//! back-end (logic engine + reasoning engine).  It owns the execution context.

pub mod loader;
pub mod executor;
pub mod output;

pub use executor::{ExecutionContext, ExecutionError};
pub use output::QueryAnswer;
