//! The intermediate representation.
//!
//! The IR is the contract between the language and the runtime
//! (`docs/03_EXECUTION_MODEL.md` §2–3): flat, explicit, versioned, printable and
//! free of source syntax. This crate holds the data types and the canonical
//! printer. It does not decide *how* to lower a program — that is the
//! compiler's job — and it does not execute.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod code;
mod name;
mod print;
mod program;

pub use code::{CodeError, ExprCode, Op, MAX_EXPR_STACK};
pub use name::{NameId, NameTable};
pub use print::{print_code, print_ir};
pub use program::{IrEffect, IrFact, IrProgram, IrRule, RuleId, IR_VERSION};
