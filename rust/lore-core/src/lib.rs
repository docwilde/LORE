//! Canonical native LORE core, sharing the Python carrier's store formats.
//! Caller identity and human review are authority; stored text is only data.
pub mod agents;
pub mod beliefs;
pub mod config;
pub mod context;
mod core;
pub mod dream;
pub mod filemap;
pub mod files;
pub mod gate;
pub mod graph;
pub mod history;
pub mod index;
pub mod memory;
pub mod pending;
pub mod review;
pub mod scrub;
pub mod skills;
pub mod store;
pub mod sync;
pub mod sync_apply;
pub mod sync_network;
pub mod sync_admin;
pub mod standalone;
pub mod standalone_ops;
pub mod worker;
pub use core::Core;

pub const MAX_FRAME_BYTES: usize = 1024 * 1024;
pub const MAX_REVIEW_BYTES: usize = (MAX_FRAME_BYTES - 512) / 6;
pub type Result<T> = std::result::Result<T, Error>;

/// Errors expose fixed diagnostic codes, never source text or credentials.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    InvalidRequest,
    Unavailable,
    UnsafePath,
    TooLarge,
    Timeout,
    Changed,
    OverCap,
    Untrusted,
    Unsupported,
    MayHaveApplied,
}
impl Error {
    pub fn code(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid_request",
            Self::Unavailable => "operation_failed",
            Self::UnsafePath => "unsafe_path",
            Self::TooLarge => "output_too_large",
            Self::Timeout => "timeout",
            Self::Changed => "review_changed",
            Self::OverCap => "over_cap",
            Self::Untrusted => "untrusted_write",
            Self::Unsupported => "unavailable_operation",
            Self::MayHaveApplied => "may_have_applied",
        }
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.code())
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(_: std::io::Error) -> Self {
        Self::Unavailable
    }
}
impl From<rusqlite::Error> for Error {
    fn from(e: rusqlite::Error) -> Self {
        match e.sqlite_error_code() {
            Some(rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked) => {
                Self::Timeout
            }
            Some(rusqlite::ErrorCode::TooBig) => Self::TooLarge,
            _ => Self::Unavailable,
        }
    }
}

pub fn digest(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}
pub fn utcnow() -> String {
    time::OffsetDateTime::now_utc()
        .format(time::macros::format_description!(
            "[year]-[month]-[day]T[hour]:[minute]:[second]Z"
        ))
        .unwrap_or_default()
}
