use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("unknown {kind} value '{value}'")]
    UnknownEnum { kind: &'static str, value: String },
    #[error("invalid geometry: {0}")]
    Geometry(String),
    #[error("node '{0}' not found")]
    NodeNotFound(String),
    #[error("network contains a cycle involving node '{0}'")]
    Cycle(String),
    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, Error>;
