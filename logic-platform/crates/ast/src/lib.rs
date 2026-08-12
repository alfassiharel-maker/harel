//! The syntax tree.
//!
//! Data types and a canonical printer, nothing else. The AST does not evaluate,
//! does not validate and performs no I/O; it is the shape the parser produces
//! and the semantic pass consumes.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod expr;
mod name;
mod print;
mod program;

pub use expr::{BinaryOp, Expr, Literal, UnaryOp};
pub use name::{Name, MAX_NAME_LENGTH};
pub use print::{print_expr, print_literal, print_program, print_string};
pub use program::{Effect, FactDecl, Item, OutputDecl, Program, RuleDecl};
