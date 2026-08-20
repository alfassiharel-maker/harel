//! CCP data and storage engine.
//!
//! This crate owns the bytes: streaming, blocking, the container format, the
//! version graph, reconstruction and integrity. It calls the C++ bit execution
//! engine for the bit work (`bitexec`) and the Julia strategy engine for every
//! representation decision (`strategy`). It contains no cost model and no bit
//! loops of its own — where either appears here, a boundary has been crossed.

pub mod bitexec;
pub mod error;
pub mod format;
pub mod json;
pub mod sha256;
pub mod store;
pub mod strategy;

pub use error::{CcpError, Result};
pub use store::{Repository, DEFAULT_BLOCK_SIZE};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
