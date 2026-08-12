//! Source text to tokens.
//!
//! The lexer recognises lexical *shape*: identifiers, keywords, literals,
//! operators, punctuation, comments and whitespace. It does not know what a
//! declaration is, does not evaluate anything, and has no opinion about whether
//! a program is valid. `docs/02_FORMAL_SEMANTICS.md` §1 is the specification it
//! implements.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod scanner;
mod token;

pub use scanner::{tokenize, MAX_SOURCE_BYTES};
pub use token::{keyword, Token, TokenKind, RESERVED_WORDS};
