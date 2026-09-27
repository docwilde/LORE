//! Bounded read-only session history. These probes never create or migrate a store.
use std::{collections::BTreeSet, path::Path};
use rusqlite::{params, Connection, Row};
use serde_json::{json, Value};
use crate::{config::{self, Config}, scrub, store, Error, Result};

const MAX_RECENT: usize = 20;
const MAX_PREFIX: usize = 9;
const MAX_META: usize = 50;
fn limit(req: &Value, default: usize, cap: usize) -> Result<usize> {
    match req.get("limit") { None => Ok(default), Some(value) => value.as_u64()
        .filter(|n| *n <= cap as u64).map(|n| n as usize).ok_or(Error::InvalidRequest) }
}
fn valid_session(id: &str) -> bool {
    config::valid_id(id) || id.strip_prefix("codex:").is_some_and(config::valid_id)
}
fn bounded(raw: Option<String>, cap: usize) -> Result<String> {
    let raw = raw.unwrap_or_default();
    if raw.len() > cap || raw.contains('\0') { return Err(Error::TooLarge); }
    Ok(scrub::scrub(raw.trim())?)
}
// SQLite bounds the values before allocating Rust Strings. The extra character
// detects oversized columns rather than silently publishing truncated metadata.
const COLUMNS: &str = "substr(session_id,1,135),substr(project,1,1021),substr(cwd,1,4097),substr(title,1,4097),substr(last_ts,1,129),messages";
fn hit(row: &Row<'_>, recent: bool) -> Result<Value> {
    let id: String = row.get(0)?;
    if !valid_session(&id) { return Err(Error::InvalidRequest); }
    let project = bounded(row.get(1)?, 1020)?;
    if !project.is_empty() && !config::valid_slug(&project) { return Err(Error::InvalidRequest); }
    let cwd = bounded(row.get(2)?, 4096)?;
    let title = bounded(row.get(3)?, 4096)?;
    let ts = bounded(row.get(4)?, 128)?;
    let messages: i64 = row.get::<_, Option<i64>>(5)?.unwrap_or(0);
    if messages < 0 { return Err(Error::InvalidRequest); }
    Ok(json!({"session_id":id,"project":project,"cwd":cwd,"title":title,"ts":ts,
        "role":"","messages":messages,"snippet":if recent {format!("{messages} message{}",if messages==1 {""} else {"s"})} else {String::new()}}))
}
fn finish(rows: Vec<Value>) -> Result<Value> {
    let value = json!(rows);
    if serde_json::to_vec(&value).map_err(|_| Error::Unavailable)?.len() > crate::MAX_FRAME_BYTES - 512 {
        return Err(Error::TooLarge);
    }
    Ok(value)
}
pub fn recent(cfg: &Config, req: &Value) -> Result<Value> {
    let cwd = req["cwd"].as_str().filter(|s| !s.is_empty() && s.len()<=4096 && !s.contains('\0'))
        .ok_or(Error::InvalidRequest)?;
    let cap = limit(req, MAX_RECENT, MAX_RECENT)?;
    if cap==0 { return finish(vec![]); }
    let conn = store::read_only(cfg)?;
    let slug = config::project_slug(Path::new(cwd));
    let mut stmt = conn.prepare(&format!("SELECT {COLUMNS} FROM sessions ORDER BY CASE WHEN project=? THEN 0 ELSE 1 END,last_ts DESC,session_id LIMIT ?"))?;
    let mut cursor = stmt.query(params![slug, cap as i64])?;
    let mut rows = Vec::new();
    while let Some(row) = cursor.next()? { rows.push(hit(row,true)?); }
    finish(rows)
}
pub fn prefix(cfg: &Config, req: &Value) -> Result<Value> {
    let term = req["prefix"].as_str().filter(|s| !s.is_empty() && s.len()<=134 &&
        s.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c,b'-'|b'_'|b':')))
        .ok_or(Error::InvalidRequest)?;
    let cap = limit(req, MAX_PREFIX, MAX_PREFIX)?;
    if cap==0 { return finish(vec![]); }
    let conn = store::read_only(cfg)?;
    // instr is literal: SQL LIKE's '%' and '_' never become wildcards.
    let mut stmt = conn.prepare(&format!("SELECT {COLUMNS} FROM sessions WHERE instr(session_id,?)=1 ORDER BY last_ts DESC,session_id LIMIT ?"))?;
    let mut cursor = stmt.query(params![term, cap as i64])?;
    let mut rows = Vec::new();
    while let Some(row) = cursor.next()? { rows.push(hit(row,false)?); }
    finish(rows)
}
fn has_engine(conn: &Connection) -> Result<bool> {
    let mut stmt = conn.prepare("PRAGMA table_info(sessions)")?;
    let mut rows = stmt.query([])?;
    let mut found = false;
    let mut count = 0;
    while let Some(row) = rows.next()? {
        count+=1; if count>64 {return Err(Error::TooLarge);}
        found |= row.get::<_,String>(1)? == "engine";
    }
    Ok(found)
}
pub fn metadata(cfg: &Config, req: &Value) -> Result<Value> {
    let ids = req["ids"].as_array().filter(|rows| rows.len()<=MAX_META).ok_or(Error::InvalidRequest)?;
    let mut unique = BTreeSet::new();
    for id in ids {
        let id = id.as_str().filter(|s| valid_session(s)).ok_or(Error::InvalidRequest)?;
        unique.insert(id);
    }
    if unique.is_empty() {return finish(vec![]);}
    let conn = store::read_only(cfg)?;
    let engine = if has_engine(&conn)? {"substr(engine,1,33)"} else {"NULL"};
    let mut stmt = conn.prepare(&format!("SELECT substr(title,1,4097),substr(cwd,1,4097),{engine} FROM sessions WHERE session_id=?"))?;
    let mut result = Vec::new();
    for id in unique {
        let mut cursor = stmt.query([id])?;
        if let Some(row) = cursor.next()? {
            result.push(json!({"session_id":id,"title":bounded(row.get(0)?,4096)?,
                "cwd":bounded(row.get(1)?,4096)?,"engine":bounded(row.get(2)?,32)?}));
        }
    }
    finish(result)
}

#[cfg(test)] mod tests {
    use super::*;
    fn fixture() -> (tempfile::TempDir, Config, Connection) {
        let temp = tempfile::tempdir().unwrap(); let cfg=Config::for_root(temp.path().join("lore"));
        let conn=store::connect(&cfg).unwrap(); (temp,cfg,conn)
    }
    #[test] fn missing_store_stays_missing_and_invalid_requests_are_refused() {
        let temp=tempfile::tempdir().unwrap();let cfg=Config::for_root(temp.path().join("missing"));
        assert!(recent(&cfg,&json!({"cwd":"/owned"})).is_err()); assert!(!cfg.root.exists());
        assert!(prefix(&cfg,&json!({"prefix":"%"})).is_err());
        assert!(metadata(&cfg,&json!({"ids":["../escape"]})).is_err());
        assert!(metadata(&cfg,&json!({"ids":vec!["safe";51]})).is_err());
    }
    #[test] fn recent_is_project_first_and_prefix_is_literal_bounded_and_read_only() {
        let (_temp,cfg,conn)=fixture();let slug=config::project_slug(Path::new("/owned"));
        for n in 0..30 {conn.execute("INSERT INTO sessions(session_id,project,cwd,title,last_ts,messages,engine) VALUES(?,?,?,?,?,1,'claude')",
            params![format!("owned_{n:02}"),if n<2 {slug.as_str()} else {"other"},"/owned",format!("title{n}"),format!("2026-09-{n:02}")]).unwrap();}
        let rows=recent(&cfg,&json!({"cwd":"/owned","limit":3})).unwrap();
        assert_eq!(rows[0]["session_id"],"owned_01");assert_eq!(rows[1]["session_id"],"owned_00");assert_eq!(rows[2]["session_id"],"owned_29");
        assert_eq!(rows[0]["snippet"],"1 message");
        assert_eq!(prefix(&cfg,&json!({"prefix":"owned_","limit":9})).unwrap().as_array().unwrap().len(),9);
        assert!(prefix(&cfg,&json!({"prefix":"owned_","limit":10})).is_err());
        assert!(prefix(&cfg,&json!({"prefix":"owned_0%"})).is_err());
        assert_eq!(conn.query_row("SELECT count(*) FROM sessions",[],|r|r.get::<_,i64>(0)).unwrap(),30);
    }
    #[test] fn metadata_deduplicates_scrubs_and_rejects_oversized_or_negative_columns() {
        let (_temp,cfg,conn)=fixture();conn.execute("INSERT INTO sessions(session_id,project,title,messages,engine) VALUES('codex:owned','project',?,1,'codex')",["sk-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ012345"]).unwrap();
        let rows=metadata(&cfg,&json!({"ids":["codex:owned","codex:owned","absent"]})).unwrap();
        assert_eq!(rows.as_array().unwrap().len(),1);assert_eq!(rows[0]["engine"],"codex");assert!(!rows[0]["title"].as_str().unwrap().contains("sk-"));
        conn.execute("UPDATE sessions SET title=?",["x".repeat(100000)]).unwrap();assert!(metadata(&cfg,&json!({"ids":["codex:owned"]})).is_err());
        conn.execute("UPDATE sessions SET title='',messages=-1",[]).unwrap();assert!(recent(&cfg,&json!({"cwd":"/owned"})).is_err());
    }
}
