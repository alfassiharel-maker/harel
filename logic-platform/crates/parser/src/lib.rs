//! Tokens to syntax tree.
//!
//! The parser owns the grammar (`grammar.ebnf`, normative copy in
//! `docs/02_FORMAL_SEMANTICS.md` §2) and nothing else: it does not resolve
//! names, does not check types, and does not evaluate.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod parser;

pub use parser::{parse, MAX_EXPR_DEPTH};

use lml_ast::Program;
use lml_diagnostics::Diagnostics;

/// Lex and parse `source` in one step.
///
/// Lexical errors are reported without attempting to parse: a token stream with
/// holes produces syntax errors that are artefacts of the lexical ones.
///
/// # Errors
/// Returns the lexical diagnostics, or the syntax diagnostics.
pub fn parse_source(source: &str) -> Result<Program, Diagnostics> {
    let tokens = lml_lexer::tokenize(source)?;
    parse(&tokens)
}
