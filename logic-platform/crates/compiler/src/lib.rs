//! The compiler: orchestration and lowering.
//!
//! ```text
//! source ──lexer──▶ tokens ──parser──▶ AST ──semantic──▶ model ──lower──▶ IR
//! ```
//!
//! Each stage is a total function that either succeeds or returns diagnostics,
//! and the compiler runs them in order, stopping at the first stage that fails.
//! It deliberately contains no analysis of its own: everything it knows about
//! the language it learned from `lml-semantic`.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod lower;

pub use lower::lower;

use lml_ast::Program;
use lml_diagnostics::Diagnostics;
use lml_ir::IrProgram;
use lml_semantic::SemanticModel;

/// The result of compiling, with the intermediate artefacts kept.
///
/// Tooling wants them — `lml ast` prints the tree, `lml ir` prints the IR — and
/// keeping them here means the CLI never re-runs a stage to get one.
#[derive(Debug, Clone)]
pub struct Compilation {
    /// The validated model, which owns the syntax tree.
    pub model: SemanticModel,
    /// The lowered program.
    pub ir: IrProgram,
}

impl Compilation {
    /// The syntax tree.
    #[must_use]
    pub const fn program(&self) -> &Program {
        &self.model.program
    }
}

/// Compile source text to IR.
///
/// # Errors
/// The diagnostics of the first stage that failed. Later stages do not run: a
/// program that does not parse has no meaningful type errors, and reporting
/// invented ones wastes the reader's attention.
pub fn compile(source: &str) -> Result<Compilation, Diagnostics> {
    let program = lml_parser::parse_source(source)?;
    let model = lml_semantic::analyze(program)?;
    let ir = lower(&model)?;
    Ok(Compilation { model, ir })
}

/// Check source text without keeping the result.
///
/// # Errors
/// The diagnostics of the first stage that failed.
pub fn check(source: &str) -> Result<(), Diagnostics> {
    compile(source).map(|_| ())
}
