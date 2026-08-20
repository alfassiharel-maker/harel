//! Error type for the data engine.
//!
//! Integrity failures are their own variant and are never recoverable: no caller
//! may catch one and continue with the bytes it has. That is the point of the
//! guarantee.

use std::fmt;

#[derive(Debug)]
pub enum CcpError {
    Io(std::io::Error),
    /// The container is not readable as CCP v1: bad magic, truncation,
    /// inconsistent offsets, unknown block kind.
    Format(String),
    /// A container written by a newer format version. Never guessed at.
    UnsupportedVersion(u16),
    /// A reconstructed artifact does not match the hash recorded at ingest, or a
    /// container's own bytes do not match its footer. Always fatal.
    Integrity(String),
    /// The strategy engine was unreachable, failed, or returned something that is
    /// not a valid decision.
    Strategy(String),
    /// A version, base, or repository that does not exist.
    NotFound(String),
    /// Caller error: bad arguments, a base that is not in the repository, a
    /// block size of zero.
    Invalid(String),
}

impl fmt::Display for CcpError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CcpError::Io(e) => write!(f, "io error: {e}"),
            CcpError::Format(m) => write!(f, "container format error: {m}"),
            CcpError::UnsupportedVersion(v) => write!(
                f,
                "container uses CCP format version {v}, this build supports version {}",
                crate::format::FORMAT_VERSION
            ),
            CcpError::Integrity(m) => write!(f, "INTEGRITY FAILURE: {m}"),
            CcpError::Strategy(m) => write!(f, "strategy engine error: {m}"),
            CcpError::NotFound(m) => write!(f, "not found: {m}"),
            CcpError::Invalid(m) => write!(f, "invalid: {m}"),
        }
    }
}

impl std::error::Error for CcpError {}

impl From<std::io::Error> for CcpError {
    fn from(e: std::io::Error) -> Self {
        CcpError::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, CcpError>;
