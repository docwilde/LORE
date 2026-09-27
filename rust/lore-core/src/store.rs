//! Compatible WAL database. Migrations inspect schema rather than claiming
//! SQLite user_version, which is shared with existing DOXA carriers.
use crate::{config::Config, files, Error, Result};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::{fs, path::Path};

pub fn connect(config: &Config) -> Result<Connection> {
    files::private_dir(&config.root)?;
    let path = checked_path(config)?;
    let mut connection = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NOFOLLOW
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    connection.set_limit(
        rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH,
        8 * 1024 * 1024,
    )?;
    connection.busy_timeout(config.timeout)?;
    connection.pragma_update(None, "journal_mode", "WAL")?;
    connection.pragma_update(None, "synchronous", "FULL")?;
    migrate(&mut connection)?;
    Ok(connection)
}
fn checked_path(config: &Config) -> Result<std::path::PathBuf> {
    let path = config.root.join("state.db");
    for ancestor in config.root.ancestors() {
        if fs::symlink_metadata(ancestor).is_ok_and(|meta| meta.file_type().is_symlink()) {
            return Err(Error::UnsafePath);
        }
    }
    for candidate in [
        path.clone(),
        config.root.join("state.db-wal"),
        config.root.join("state.db-shm"),
    ] {
        if let Ok(meta) = fs::symlink_metadata(&candidate) {
            if !meta.is_file() {
                return Err(Error::UnsafePath);
            }
            #[cfg(unix)]
            if meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1 {
                return Err(Error::UnsafePath);
            }
        }
    }
    Ok(path)
}
/// A status probe never creates a store, migrates it or mints an identity.
pub fn read_only(config: &Config) -> Result<Connection> {
    let path = checked_path(config)?;
    let connection = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_NOFOLLOW
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.set_limit(
        rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH,
        8 * 1024 * 1024,
    )?;
    connection.busy_timeout(config.timeout)?;
    Ok(connection)
}
fn migrate(connection: &mut Connection) -> Result<()> {
    let sql: Vec<String> =
        serde_json::from_str(include_str!("schema.json")).map_err(|_| Error::Unavailable)?;
    let tx = connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    for statement in sql {
        if statement.starts_with("ALTER TABLE ") {
            let parts: Vec<_> = statement.split_whitespace().collect();
            let table = parts[2];
            let column = parts[5];
            let mut query = tx.prepare(&format!("PRAGMA table_info({table})"))?;
            let exists = query
                .query_map([], |row| row.get::<_, String>(1))?
                .collect::<std::result::Result<Vec<_>, _>>()?
                .iter()
                .any(|name| name == column);
            if exists {
                continue;
            }
        }
        if statement.starts_with("CREATE UNIQUE INDEX IF NOT EXISTS sync_ops_slot") {
            let old: String = tx.query_row(
                "SELECT sql FROM sqlite_master WHERE name='sync_ops'",
                [],
                |r| r.get(0),
            )?;
            if old.contains("UNIQUE(machine_id, machine_seq)") {
                tx.execute_batch("CREATE TABLE sync_ops_rebuilt(seq INTEGER PRIMARY KEY,op_id TEXT NOT NULL UNIQUE,machine_id TEXT NOT NULL,machine_seq INTEGER NOT NULL,lamport INTEGER NOT NULL,class TEXT NOT NULL,op TEXT NOT NULL,project_key TEXT,payload TEXT NOT NULL,mac TEXT,created TEXT NOT NULL,applied INTEGER NOT NULL DEFAULT 0);INSERT INTO sync_ops_rebuilt SELECT * FROM sync_ops;DROP TABLE sync_ops;ALTER TABLE sync_ops_rebuilt RENAME TO sync_ops;CREATE INDEX sync_ops_order ON sync_ops(lamport,machine_id,machine_seq)")?;
            }
        }
        tx.execute_batch(&statement)?;
    }
    for table in ["beliefs", "belief_outcomes"] {
        loop {
            let mut statement = tx.prepare(&format!(
                "SELECT id FROM {table} WHERE uid IS NULL LIMIT 256"
            ))?;
            let ids = statement
                .query_map([], |r| r.get::<_, i64>(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            if ids.is_empty() {
                break;
            }
            for id in ids {
                tx.execute(
                    &format!("UPDATE {table} SET uid=? WHERE id=? AND uid IS NULL"),
                    rusqlite::params![uuid::Uuid::new_v4().to_string(), id],
                )?;
            }
        }
    }
    tx.commit()?;
    Ok(())
}
pub fn record_project(connection: &Connection, cwd: &Path) -> Result<()> {
    let slug = crate::config::project_slug(cwd);
    let key = crate::config::project_key(cwd);
    connection.execute(
        "INSERT OR IGNORE INTO sync_projects(project_key,slug,origin,created) VALUES(?,?,?,?)",
        rusqlite::params![
            key,
            slug,
            if key == slug { None } else { Some(key.clone()) },
            crate::utcnow()
        ],
    )?;
    Ok(())
}
pub fn project_key_for_slug(connection: &Connection, slug: &str) -> Result<String> {
    Ok(connection
        .query_row(
            "SELECT project_key FROM sync_projects WHERE slug=?",
            [slug],
            |r| r.get(0),
        )
        .optional()?
        .unwrap_or_else(|| slug.into()))
}
pub fn machine_id(connection: &Connection) -> Result<String> {
    if let Some(id) = connection
        .query_row("SELECT machine_id FROM sync_machine LIMIT 1", [], |r| {
            r.get(0)
        })
        .optional()?
    {
        return Ok(id);
    }
    let id = std::env::var("LORE_MACHINE_ID")
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control))
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let mut hostname = vec![0u8; 256];
    #[cfg(unix)]
    unsafe {
        libc::gethostname(hostname.as_mut_ptr().cast(), hostname.len());
    }
    let label = String::from_utf8_lossy(
        &hostname[..hostname
            .iter()
            .position(|c| *c == 0)
            .unwrap_or(hostname.len())],
    )
    .into_owned();
    connection.execute(
        "INSERT INTO sync_machine(machine_id,label,lamport) VALUES(?,?,0)",
        rusqlite::params![id, label],
    )?;
    Ok(id)
}

/// Append inside the caller's mutation transaction; this never commits it.
pub fn append_op(
    config: &Config,
    connection: &Connection,
    class: &str,
    op: &str,
    project_key: Option<&str>,
    payload: &serde_json::Value,
) -> Result<()> {
    let configured = match class {
        "belief" => "beliefs",
        "skill" => "skills",
        "session" => "sessions",
        other => other,
    };
    if !config.sync.enabled || !config.sync.classes.contains(configured) {
        return Ok(());
    }
    if connection.is_autocommit() {
        return Err(Error::InvalidRequest);
    }
    let machine = machine_id(connection)?;
    let lamport: i64 = connection.query_row(
        "SELECT lamport FROM sync_machine WHERE machine_id=?",
        [&machine],
        |r| r.get(0),
    )?;
    let lamport = lamport.checked_add(1).ok_or(Error::TooLarge)?;
    let seq: i64 = connection.query_row(
        "SELECT coalesce(max(machine_seq),0) FROM sync_ops WHERE machine_id=?",
        [&machine],
        |r| r.get(0),
    )?;
    let seq = seq.checked_add(1).ok_or(Error::TooLarge)?;
    let payload = crate::scrub::scrub_json(payload)?;
    let id = uuid::Uuid::new_v4().to_string();
    let tuple = serde_json::json!([id, machine, seq, lamport, class, op, project_key, payload]);
    let mac = config
        .sync
        .key
        .as_ref()
        .map(|key| canonical_mac(&tuple, key))
        .transpose()?;
    connection.execute(
        "UPDATE sync_machine SET lamport=? WHERE machine_id=?",
        rusqlite::params![lamport, machine],
    )?;
    connection.execute("INSERT INTO sync_ops(op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created,applied) VALUES(?,?,?,?,?,?,?,?,?,?,1)",rusqlite::params![id,machine,seq,lamport,class,op,project_key,serde_json::to_string(&payload).map_err(|_|Error::Unavailable)?,mac,crate::utcnow()])?;
    Ok(())
}
pub fn canonical_mac(tuple: &serde_json::Value, key: &str) -> Result<String> {
    use hmac::{Hmac, Mac};
    let bytes = canonical_bytes(tuple)?;
    let mut mac =
        Hmac::<sha2::Sha256>::new_from_slice(key.as_bytes()).map_err(|_| Error::InvalidRequest)?;
    mac.update(&bytes);
    Ok(mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}
/// Protocol S2 uses Python shortest-round-trip float spelling, including
/// signed/padded exponents. Ordinary serde JSON is not byte-identical here.
pub fn canonical_bytes(value: &serde_json::Value) -> Result<Vec<u8>> {
    fn write(value: &serde_json::Value, out: &mut Vec<u8>, depth: usize) -> Result<()> {
        use serde_json::Value;
        if depth > 32 {
            return Err(Error::TooLarge);
        }
        match value {
            Value::Number(n) if n.is_f64() => {
                let number = n.as_f64().ok_or(Error::InvalidRequest)?;
                if !number.is_finite() {
                    return Err(Error::InvalidRequest);
                }
                let raw = format!("{number:?}");
                let text = if let Some((mantissa, exponent)) = raw.split_once('e') {
                    let exp: i32 = exponent.parse().map_err(|_| Error::InvalidRequest)?;
                    format!(
                        "{mantissa}e{}{abs:02}",
                        if exp < 0 { "-" } else { "+" },
                        abs = exp.unsigned_abs()
                    )
                } else {
                    raw
                };
                out.extend_from_slice(text.as_bytes());
            }
            Value::Array(rows) => {
                out.push(b'[');
                for (index, row) in rows.iter().enumerate() {
                    if index > 0 {
                        out.push(b',');
                    }
                    write(row, out, depth + 1)?;
                }
                out.push(b']');
            }
            Value::Object(rows) => {
                out.push(b'{');
                let mut keys: Vec<_> = rows.keys().collect();
                keys.sort();
                for (index, key) in keys.iter().enumerate() {
                    if index > 0 {
                        out.push(b',');
                    }
                    serde_json::to_writer(&mut *out, key).map_err(|_| Error::InvalidRequest)?;
                    out.push(b':');
                    write(&rows[*key], out, depth + 1)?;
                }
                out.push(b'}');
            }
            _ => serde_json::to_writer(&mut *out, value).map_err(|_| Error::InvalidRequest)?,
        }
        if out.len() > crate::MAX_FRAME_BYTES {
            return Err(Error::TooLarge);
        }
        Ok(())
    }
    let mut output = Vec::new();
    write(value, &mut output, 0)?;
    Ok(output)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn schema_is_idempotent_and_old_uid_rows_receive_real_identity() {
        let temp = tempfile::tempdir().unwrap();
        let config = Config::for_root(temp.path().join("lore"));
        let db = connect(&config).unwrap();
        db.execute(
            "INSERT INTO beliefs(subject,claim,confidence) VALUES('user','fixture',0.8)",
            [],
        )
        .unwrap();
        drop(db);
        let db = connect(&config).unwrap();
        let uid: String = db
            .query_row("SELECT uid FROM beliefs", [], |r| r.get(0))
            .unwrap();
        assert!(uuid::Uuid::parse_str(&uid).is_ok());
        drop(db);
        let db = connect(&config).unwrap();
        assert_eq!(
            db.query_row("SELECT uid FROM beliefs", [], |r| r.get::<_, String>(0))
                .unwrap(),
            uid
        );
        assert!(db
            .query_row(
                "SELECT count(*) FROM msg WHERE msg MATCH 'fixture'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .is_ok());
    }
}
