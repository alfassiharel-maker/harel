//! Static analysis: name resolution, type inference and every check the
//! language performs before execution.
//!
//! Input is a parsed [`lml_ast::Program`]; output is a [`SemanticModel`] in
//! which every name has one type and every read is satisfiable. Nothing here
//! evaluates or lowers.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod check;
mod model;

pub use check::{analyze, MAX_TYPE_DEPENDENCY_DEPTH};
pub use model::{NameInfo, RuleInfo, SemanticModel, Writer};
