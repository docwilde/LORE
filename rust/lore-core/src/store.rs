//! Compatible WAL database. Migrations inspect schema rather than claiming
//! SQLite user_version, which is shared with existing DOXA carriers.
use std::{fs,path::Path};
#[cfg(unix)]
use std::os::unix::fs::{MetadataExt,PermissionsExt};
use rusqlite::{Connection,OpenFlags};
use crate::{config::Config,files,Error,Result};

pub fn connect(config:&Config)->Result<Connection> {
    files::private_dir(&config.root)?;
    let path=config.root.join("state.db");
    for candidate in [path.clone(),config.root.join("state.db-wal"),config.root.join("state.db-shm")] {
        if let Ok(meta)=fs::symlink_metadata(&candidate) {
            if !meta.is_file(){return Err(Error::UnsafePath);}
            #[cfg(unix)] if meta.uid()!=unsafe{libc::geteuid()}||meta.nlink()!=1{return Err(Error::UnsafePath);}
        }
    }
    let mut connection=Connection::open_with_flags(&path,OpenFlags::SQLITE_OPEN_READ_WRITE|OpenFlags::SQLITE_OPEN_CREATE|OpenFlags::SQLITE_OPEN_NOFOLLOW|OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    #[cfg(unix)] fs::set_permissions(path,fs::Permissions::from_mode(0o600))?;
    connection.busy_timeout(config.timeout)?;
    connection.pragma_update(None,"journal_mode","WAL")?;
    connection.pragma_update(None,"synchronous","FULL")?;
    migrate(&mut connection)?;
    Ok(connection)
}
fn migrate(connection:&mut Connection)->Result<()> {
    let sql:Vec<String>=serde_json::from_str(include_str!("schema.json")).map_err(|_|Error::Unavailable)?;
    let tx=connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    for statement in sql {
        if statement.starts_with("ALTER TABLE ") {
            let parts:Vec<_>=statement.split_whitespace().collect();
            let table=parts[2];let column=parts[5];
            let mut query=tx.prepare(&format!("PRAGMA table_info({table})"))?;
            let exists=query.query_map([],|row|row.get::<_,String>(1))?.collect::<std::result::Result<Vec<_>,_>>()?.iter().any(|name|name==column);
            if exists {continue;}
        }
        if statement.starts_with("CREATE UNIQUE INDEX IF NOT EXISTS sync_ops_slot") {
            let old:String=tx.query_row("SELECT sql FROM sqlite_master WHERE name='sync_ops'",[],|r|r.get(0))?;
            if old.contains("UNIQUE(machine_id, machine_seq)") {
                tx.execute_batch("CREATE TABLE sync_ops_rebuilt(seq INTEGER PRIMARY KEY,op_id TEXT NOT NULL UNIQUE,machine_id TEXT NOT NULL,machine_seq INTEGER NOT NULL,lamport INTEGER NOT NULL,class TEXT NOT NULL,op TEXT NOT NULL,project_key TEXT,payload TEXT NOT NULL,mac TEXT,created TEXT NOT NULL,applied INTEGER NOT NULL DEFAULT 0);INSERT INTO sync_ops_rebuilt SELECT * FROM sync_ops;DROP TABLE sync_ops;ALTER TABLE sync_ops_rebuilt RENAME TO sync_ops;CREATE INDEX sync_ops_order ON sync_ops(lamport,machine_id,machine_seq)")?;
            }
        }
        tx.execute_batch(&statement)?;
    }
    for table in ["beliefs","belief_outcomes"] {
        loop {
            let mut statement=tx.prepare(&format!("SELECT id FROM {table} WHERE uid IS NULL LIMIT 256"))?;
            let ids=statement.query_map([],|r|r.get::<_,i64>(0))?.collect::<std::result::Result<Vec<_>,_>>()?;
            if ids.is_empty(){break;}
            for id in ids {tx.execute(&format!("UPDATE {table} SET uid=? WHERE id=? AND uid IS NULL"),rusqlite::params![uuid::Uuid::new_v4().to_string(),id])?;}
        }
    }
    tx.commit()?;Ok(())
}
pub fn record_project(connection:&Connection,cwd:&Path)->Result<()> {
    let slug=crate::config::project_slug(cwd);let key=crate::config::project_key(cwd);
    connection.execute("INSERT OR IGNORE INTO sync_projects(project_key,slug,origin,created) VALUES(?,?,?,?)",rusqlite::params![key,slug,if key==slug {None}else{Some(key.clone())},crate::utcnow()])?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn schema_is_idempotent_and_old_uid_rows_receive_real_identity() {
        let temp=tempfile::tempdir().unwrap();let config=Config::for_root(temp.path().join("lore"));
        let db=connect(&config).unwrap();
        db.execute("INSERT INTO beliefs(subject,claim,confidence) VALUES('user','fixture',0.8)",[]).unwrap();drop(db);
        let db=connect(&config).unwrap();
        let uid:String=db.query_row("SELECT uid FROM beliefs",[],|r|r.get(0)).unwrap();
        assert!(uuid::Uuid::parse_str(&uid).is_ok());drop(db);
        let db=connect(&config).unwrap();assert_eq!(db.query_row("SELECT uid FROM beliefs",[],|r|r.get::<_,String>(0)).unwrap(),uid);
        assert!(db.query_row("SELECT count(*) FROM msg WHERE msg MATCH 'fixture'",[],|r|r.get::<_,i64>(0)).is_ok());
    }
}
