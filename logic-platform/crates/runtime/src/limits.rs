//! Execution limits.
//!
//! Every bound the engine enforces has a name, a value, and a written reason.
//! A bare number in a condition — `if depth > 1000` — is not permitted anywhere
//! in the workspace (`docs/ENGINEERING_IMPLEMENTATION_SPEC.md` §56).
//!
//! Limits enforced before execution live with the stage that enforces them, so
//! that no early stage has to depend on the runtime:
//! `lml_lexer::MAX_SOURCE_BYTES`, `lml_parser::MAX_EXPR_DEPTH`,
//! `lml_semantic::MAX_TYPE_DEPENDENCY_DEPTH` and `lml_ir::MAX_EXPR_STACK`.
//! This module holds the ones execution itself enforces.

/// Most inference rounds the engine will run.
///
/// The semantics bound the number of rounds by the number of rules — each round
/// either fires a rule or ends the loop, and no rule fires twice — so this is a
/// backstop against a defect, not a semantic device. Reaching it means either a
/// program with 10_000+ rules or a bug in the engine, and both deserve an error
/// rather than a hang.
pub const MAX_INFERENCE_ROUNDS: usize = 10_000;

/// The bounds one execution runs under.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// See [`MAX_INFERENCE_ROUNDS`].
    pub max_inference_rounds: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_inference_rounds: MAX_INFERENCE_ROUNDS,
        }
    }
}

impl Limits {
    /// The default limits.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The same limits with a different round bound.
    ///
    /// Used by tests that need to observe the limit being hit without building
    /// a program with ten thousand rules.
    #[must_use]
    pub const fn with_max_inference_rounds(mut self, rounds: usize) -> Self {
        self.max_inference_rounds = rounds;
        self
    }
}
