//! Reasoning engine: unification, fixed-point inference, conflict detection.
//!
//! §12  Unification — Robinson's algorithm, immutable substitutions.
//! §13  Multi-predicate rule bodies.
//! §14  Deterministic fixed-point (bottom-up naive evaluation).
//! §19  Conflict model — RaiseConflict is the default strategy.

pub mod unify;
pub mod inference;
pub mod conflict;

pub use unify::{Substitution, unify};
pub use inference::{evaluate, EvaluationResult};
pub use conflict::Conflict;
