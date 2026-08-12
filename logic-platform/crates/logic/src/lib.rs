//! Logic objects and their evaluation.
//!
//! This crate owns the *representation* — what a fact is, what the set of known
//! facts is, and what an expression evaluates to over it. It does not own the
//! *strategy*: nothing here decides which rule to try, in what order, or how
//! many times. That separation is what lets a different inference strategy be
//! added later without touching this code (`docs/05_ARCHITECTURE.md` §3,
//! invariant 5).

#![forbid(unsafe_code)]
#![warn(missing_docs)]
// Every arithmetic operation in the evaluator must be checked. This makes an
// accidental `a + b` a compile error rather than a panic in production.
#![deny(clippy::arithmetic_side_effects)]

mod eval;
mod facts;

pub use eval::{eval, Evaluated};
pub use facts::{Fact, FactSet, Insertion, Origin};
