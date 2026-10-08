//! Canonical sync receiver. Transport never grants mutation authority: verify
//! the original signed tuple, or enter through an exact HumanReview approval.
use crate::{
    beliefs,
    config::{self, Config},
    filemap, files,
    gate::{self, Authority},
    memory::{self, Scope},
    pending, scrub, store, Error, Result,
};
use hmac::{Hmac, Mac};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::Write,
    path::{Path, PathBuf},
};
pub const APPLIED_NO: i64 = 0;
pub const APPLIED_YES: i64 = 1;
pub const APPLIED_UNVERIFIED: i64 = 2;
pub const APPLIED_UNKNOWN: i64 = 3;
pub const APPLIED_FAILED: i64 = 4;
/// Authenticated historical belief data with a missing required UID reference.
/// Keep the original operation for audit/export, but never replay it.
pub const APPLIED_QUARANTINED: i64 = 5;
const CLASSES: &[&str] = &[
    "memory",
    "filemap",
    "belief",
    "pending",
    "skill",
    "session",
    "transcript",
    "tabset",
    "worktree",
];
pub const MAX_PAGE: usize = 512;
pub const MAX_PAGE_BYTES: usize = 8 * 1024 * 1024;
// Older compatible clients authored whole session message snapshots larger
// than an ordinary frame. Only this exact data-only operation gets a larger
// envelope; row count/content limits and signed authority remain unchanged.
pub const MAX_SESSION_PAYLOAD_BYTES: usize = 4 * 1024 * 1024;
fn payload_limit(op: &Value) -> usize {
    if op["class"] == "session" && op["op"] == "msgs" {
        MAX_SESSION_PAYLOAD_BYTES
    } else {
        crate::MAX_FRAME_BYTES
    }
}
const MAX_DIRECTORY: usize = 10000;
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Applied {
    Complete,
    Deferred,
    Unknown,
    Staged,
}
#[derive(Default)]
struct Report {
    applied: usize,
    deferred: usize,
    quarantined: usize,
    unverified: usize,
    duplicate: usize,
    unknown: usize,
    failed: usize,
    skipped: usize,
    partial: usize,
    staged: usize,
}
impl Report {
    fn value(&self) -> Value {
        json!({"applied":self.applied,"deferred":self.deferred,"quarantined":self.quarantined,"unverified":self.unverified,"duplicate":self.duplicate,"unknown":self.unknown,"failed":self.failed,"skipped":self.skipped,"may_have_applied":self.partial,"staged":self.staged})
    }
}
fn text<'a>(v: &'a Value, key: &str, cap: usize) -> Result<&'a str> {
    v[key]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= cap && !s.contains('\0'))
        .ok_or(Error::InvalidRequest)
}
fn optional(v: &Value, key: &str, cap: usize) -> Result<Option<String>> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.len() <= cap && !s.contains('\0') => Ok(Some(s.clone())),
        _ => Err(Error::InvalidRequest),
    }
}
fn fallback(v: &Value, key: &str, default: &str, cap: usize) -> Result<String> {
    Ok(optional(v, key, cap)?
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| default.to_owned()))
}
fn integer(v: &Value, key: &str, default: i64) -> Result<i64> {
    v.get(key)
        .map_or(Some(default), Value::as_i64)
        .filter(|n| *n >= 0)
        .ok_or(Error::InvalidRequest)
}
pub fn validate_envelope(op: &Value) -> Result<()> {
    if !op.is_object() {
        return Err(Error::InvalidRequest);
    }
    for (key, cap) in [
        ("op_id", 128),
        ("machine_id", 128),
        ("class", 64),
        ("op", 64),
    ] {
        text(op, key, cap)?;
    }
    integer(op, "machine_seq", -1)?;
    integer(op, "lamport", -1)?;
    if op.get("project_key").is_none() {
        return Err(Error::InvalidRequest);
    }
    optional(op, "project_key", 2048)?;
    if !op["payload"].is_object() {
        return Err(Error::InvalidRequest);
    }
    if serde_json::to_vec(&op["payload"])
        .map_err(|_| Error::InvalidRequest)?
        .len()
        > payload_limit(op)
    {
        return Err(Error::TooLarge);
    }
    optional(op, "mac", 64)?;
    optional(op, "created", 128)?;
    Ok(())
}
fn tuple(op: &Value) -> Value {
    json!([
        op["op_id"],
        op["machine_id"],
        op["machine_seq"],
        op["lamport"],
        op["class"],
        op["op"],
        op["project_key"],
        op["payload"]
    ])
}
fn mac_bytes(op: &Value) -> Result<Vec<u8>> {
    let canonical_limit = if payload_limit(op) > crate::MAX_FRAME_BYTES {
        payload_limit(op) + 8192
    } else {
        crate::MAX_FRAME_BYTES
    };
    store::canonical_bytes_bounded(&tuple(op), canonical_limit)
}
/// Sign an authored envelope under the same exact framing the receiver verifies.
pub fn compute_mac(op: &Value, key: &str) -> Result<String> {
    validate_envelope(op)?;
    let raw = mac_bytes(op)?;
    let mut signer = Hmac::<sha2::Sha256>::new_from_slice(key.as_bytes())
        .map_err(|_| Error::InvalidRequest)?;
    signer.update(&raw);
    Ok(signer.finalize().into_bytes().iter().map(|b| format!("{b:02x}")).collect())
}
pub fn verify_mac(op: &Value, key: Option<&str>) -> bool {
    let Some(key) = key.filter(|s| !s.is_empty()) else {
        return false;
    };
    if validate_envelope(op).is_err() {
        return false;
    }
    let Some(mac) = op["mac"].as_str().filter(|s| {
        s.len() == 64
            && s.bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    }) else {
        return false;
    };
    let mut bytes = [0u8; 32];
    for (index, pair) in mac.as_bytes().chunks_exact(2).enumerate() {
        let Ok(hex) = std::str::from_utf8(pair) else {
            return false;
        };
        let Ok(byte) = u8::from_str_radix(hex, 16) else {
            return false;
        };
        bytes[index] = byte;
    }
    let Ok(raw) = mac_bytes(op) else {
        return false;
    };
    let Ok(mut verifier) = Hmac::<sha2::Sha256>::new_from_slice(key.as_bytes()) else {
        return false;
    };
    verifier.update(&raw);
    verifier.verify_slice(&bytes).is_ok()
}
pub fn deterministic_uid(op_id: &str) -> String {
    crate::digest(op_id.as_bytes())
}
fn order(op: &Value) -> (i64, String, i64) {
    match (
        op["lamport"].as_i64(),
        op["machine_id"].as_str(),
        op["machine_seq"].as_i64(),
    ) {
        (Some(l), Some(m), Some(s)) => (l, m.to_owned(), s),
        _ => (-1, String::new(), 0),
    }
}
pub fn canonical_order(ops: &[Value]) -> Vec<Value> {
    let mut sorted = ops.to_vec();
    sorted.sort_by_key(order);
    sorted
}
fn disabled(cfg: &Config, class: &str) -> bool {
    let name = match class {
        "belief" => Some("beliefs"),
        "skill" => Some("skills"),
        "session" => Some("sessions"),
        "memory" | "filemap" | "pending" => Some(class),
        _ => None,
    };
    !cfg.sync.enabled || name.is_some_and(|name| !cfg.sync.classes.contains(name))
}
fn suppressed(cfg: &Config) -> Config {
    let mut cfg = cfg.clone();
    cfg.sync.enabled = false;
    cfg
}
fn one_text(
    conn: &Connection,
    sql: &str,
    params: impl rusqlite::Params,
    cap: usize,
) -> Result<Option<String>> {
    let mut stmt = conn.prepare(sql)?;
    let mut rows = stmt.query(params)?;
    let mut budget = cap;
    rows.next()?
        .map(|row| crate::graph::db_text(row, 0, cap, &mut budget))
        .transpose()
}
fn local_slug(conn: &Connection, key: Option<&str>) -> Result<Option<String>> {
    let Some(key) = key.filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    if let Some(slug) = one_text(
        conn,
        "SELECT slug FROM sync_projects WHERE project_key=?",
        [key],
        1024,
    )? {
        if !config::valid_slug(&slug) {
            return Err(Error::UnsafePath);
        }
        return Ok(Some(slug));
    }
    let base = format!(
        "sync-{}",
        key.chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
            .collect::<String>()
    );
    let digest = crate::digest(key.as_bytes());
    for length in std::iter::once(0).chain((12..=64).step_by(4)) {
        let slug = if length == 0 && base.chars().count() <= 255 {
            base.clone()
        } else {
            format!(
                "{}-{}",
                beliefs::crop(&base, 190),
                &digest[..if length == 0 { 64 } else { length }]
            )
        };
        let n=conn.execute("INSERT OR IGNORE INTO sync_projects(project_key,slug,origin,created) VALUES(?,?,NULL,?)",params![key,slug,crate::utcnow()])?;
        if n > 0 {
            return Ok(Some(slug));
        }
        if let Some(slug) = one_text(
            conn,
            "SELECT slug FROM sync_projects WHERE project_key=?",
            [key],
            1024,
        )? {
            return Ok(Some(slug));
        }
    }
    Err(Error::Changed)
}
fn resolve_uid(conn: &Connection, uid: &str) -> Result<Option<i64>> {
    Ok(conn.query_row("SELECT id FROM beliefs WHERE uid=? UNION ALL SELECT belief_id FROM sync_belief_aliases WHERE uid=? LIMIT 1",params![uid,uid],|r|r.get(0)).optional()?)
}
fn record(conn: &Connection, op: &Value, state: i64) -> Result<bool> {
    let payload = serde_json::to_string(&op["payload"]).map_err(|_| Error::InvalidRequest)?;
    Ok(conn.execute("INSERT OR IGNORE INTO sync_ops(op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created,applied) VALUES(?,?,?,?,?,?,?,?,?,?,?)",params![text(op,"op_id",128)?,text(op,"machine_id",128)?,integer(op,"machine_seq",-1)?,integer(op,"lamport",-1)?,text(op,"class",64)?,text(op,"op",64)?,optional(op,"project_key",2048)?,payload,optional(op,"mac",64)?,fallback(op,"created",&crate::utcnow(),128)?,state])?>0)
}
fn mark(conn: &Connection, op: &Value, state: i64) -> Result<()> {
    conn.execute(
        "UPDATE sync_ops SET applied=? WHERE op_id=?",
        params![state, text(op, "op_id", 128)?],
    )?;
    Ok(())
}
fn observe(conn: &Connection, op: &Value) -> Result<()> {
    let mid = store::machine_id(conn)?;
    let old: i64 = conn.query_row(
        "SELECT lamport FROM sync_machine WHERE machine_id=?",
        [&mid],
        |r| r.get(0),
    )?;
    let next = old
        .max(integer(op, "lamport", -1)?)
        .checked_add(1)
        .ok_or(Error::TooLarge)?;
    conn.execute(
        "UPDATE sync_machine SET lamport=? WHERE machine_id=?",
        params![next, mid],
    )?;
    Ok(())
}
fn proposal_files(cfg: &Config, archive: bool) -> Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    for dir in [
        cfg.root.join("pending"),
        cfg.root.join("pending/archive"),
        cfg.root.join("pending/.claimed"),
    ]
    .into_iter()
    .take(if archive { 3 } else { 1 })
    {
        if !dir.try_exists()? {
            continue;
        }
        let metadata = fs::symlink_metadata(&dir)?;
        if !metadata.is_dir() {
            return Err(Error::UnsafePath);
        }
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            if entry.file_name().to_string_lossy().ends_with(".json") {
                out.push(entry.path());
                if out.len() > MAX_DIRECTORY {
                    return Err(Error::TooLarge);
                }
            }
        }
    }
    out.sort();
    Ok(out)
}
fn stage(cfg: &Config, item: &Value, uid: &str) -> Result<bool> {
    if !item.is_object() || !pending::valid_portable_uid(uid) {
        return Err(Error::InvalidRequest);
    }
    let dir = cfg.root.join("pending");
    files::private_dir(&dir)?;
    let _lock = files::Locks::acquire(&cfg.root, &[dir.join(".sync-stage")], cfg.timeout)?;
    let _namespace = pending::namespace_lock(cfg)?;
    for path in proposal_files(cfg, true)? {
        let value: Value =
            serde_json::from_slice(&files::read_regular(&path, crate::MAX_FRAME_BYTES)?)
                .map_err(|_| Error::InvalidRequest)?;
        if value["uid"] == uid {
            return Ok(false);
        }
    }
    let mut payload = item.clone();
    payload["uid"] = json!(uid);
    if payload.get("created").is_none() {
        payload["created"] = json!(crate::utcnow())
    }
    let bytes = serde_json::to_vec_pretty(&payload).map_err(|_| Error::InvalidRequest)?;
    if bytes.len() > crate::MAX_FRAME_BYTES {
        return Err(Error::TooLarge);
    }
    let stamp = crate::utcnow().replace(['-', ':', 'T', 'Z'], "");
    let mut temporary = tempfile::NamedTempFile::new_in(&dir)?;
    temporary.write_all(&bytes)?;
    temporary.as_file().sync_all()?;
    for index in 0..MAX_DIRECTORY {
        let path = dir.join(format!("{stamp}-{index:02}.json"));
        match temporary.persist_noclobber(path) {
            Ok(_file) => {
                File::open(&dir)
                    .and_then(|dir| dir.sync_all())
                    .map_err(|_| Error::MayHaveApplied)?;
                return Ok(true);
            }
            Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => {
                temporary = error.file;
            }
            Err(_) => return Err(Error::Unavailable),
        }
    }
    Err(Error::OverCap)
}
fn unverified(cfg: &Config, op: &Value) -> Result<()> {
    stage(
        cfg,
        &json!({"kind":"sync","action":"apply","unverified":true,"reason":if cfg.sync.key.is_none(){"no LORE_SYNC_HMAC_KEY configured on this machine"}else{"mac missing or does not verify"},"op":op,"origin":"sync-unverified"}),
        &deterministic_uid(text(op, "op_id", 128)?),
    )?;
    Ok(())
}

fn confidence(v: &Value) -> Result<f64> {
    v.as_f64()
        .filter(|n| n.is_finite())
        .map(|n| n.clamp(0., 1.))
        .ok_or(Error::InvalidRequest)
}
fn belief(cfg: &Config, conn: &Connection, op: &Value) -> Result<Applied> {
    let p = &op["payload"];
    let verb = text(op, "op", 64)?;
    let engine = fallback(p, "source_engine", "unknown", 32)?;
    let authority = Authority::Interactive {
        agent: "sync-replay".into(),
        engine,
    };
    let cfg = suppressed(cfg);
    match verb {
        "insert" => {
            let uid = text(p, "uid", 128)?;
            if resolve_uid(conn, uid)?.is_some() {
                return Ok(Applied::Complete);
            }
            let subject = fallback(p, "subject", "user", 4096)?;
            let subject = if subject.starts_with("project:") {
                local_slug(conn, optional(op, "project_key", 2048)?.as_deref())?
                    .map_or(subject, |slug| format!("project:{slug}"))
            } else {
                subject
            };
            let e = p
                .get("evidence")
                .filter(|v| v.is_object())
                .cloned()
                .unwrap_or_else(|| json!({}));
            let project = local_slug(conn, optional(&e, "project_key", 2048)?.as_deref())?;
            let req = json!({"uid":uid,"subject":subject,"claim":text(p,"claim",65536)?,"confidence":confidence(p.get("confidence").unwrap_or(&json!(0.)))?,"session_id":optional(&e,"session_id",128)?,"project":project,"note":optional(&e,"note",4096)?});
            let (id, created) = beliefs::insert_in_transaction(&cfg, conn, &req, &authority)?;
            if !created {
                conn.execute(
                    "INSERT OR IGNORE INTO sync_belief_aliases(uid,belief_id) VALUES(?,?)",
                    params![uid, id],
                )?;
            } else {
                conn.execute("UPDATE beliefs SET writer=?,via=?,source_engine=?,created=coalesce(?,created) WHERE id=?",params![optional(p,"writer",32)?,fallback(p,"via","direct",32)?,gate::current_engine(authority.engine()),optional(p,"created",128)?,id])?;
            }
            Ok(Applied::Complete)
        }
        "reinforce" => {
            let Some(id) = resolve_uid(conn, text(p, "uid", 128)?)? else {
                return Ok(Applied::Deferred);
            };
            let e = p
                .get("evidence")
                .filter(|v| v.is_object())
                .cloned()
                .unwrap_or_else(|| json!({}));
            let auth = Authority::Interactive {
                agent: "sync-replay".into(),
                engine: fallback(&e, "source_engine", "unknown", 32)?,
            };
            let req = json!({"session_id":optional(&e,"session_id",128)?,"project":local_slug(conn,optional(&e,"project_key",2048)?.as_deref())?,"note":optional(&e,"note",4096)?});
            beliefs::reinforce_in_transaction(
                &cfg,
                conn,
                id,
                confidence(p.get("confidence").unwrap_or(&json!(0.)))?,
                &req,
                &auth,
            )?;
            Ok(Applied::Complete)
        }
        "supersede" => {
            let (Some(id), Some(by)) = (
                resolve_uid(conn, text(p, "uid", 128)?)?,
                resolve_uid(conn, text(p, "by_uid", 128)?)?,
            ) else {
                return Ok(Applied::Deferred);
            };
            beliefs::supersede_in_transaction(
                &cfg,
                conn,
                id,
                Some(by),
                &fallback(p, "reason", "", 4096)?,
                &authority,
            )?;
            Ok(Applied::Complete)
        }
        "retract" => {
            let Some(id) = resolve_uid(conn, text(p, "uid", 128)?)? else {
                return Ok(Applied::Deferred);
            };
            beliefs::retract_in_transaction(
                &cfg,
                conn,
                id,
                &fallback(p, "reason", "retracted elsewhere", 4096)?,
                &authority,
            )?;
            Ok(Applied::Complete)
        }
        "status" => {
            let Some(id) = resolve_uid(conn, text(p, "uid", 128)?)? else {
                return Ok(Applied::Deferred);
            };
            let status = fallback(p, "status", "active", 32)?;
            if !matches!(status.as_str(), "active" | "dormant") {
                return Err(Error::InvalidRequest);
            }
            conn.execute("UPDATE beliefs SET status=?,updated=? WHERE id=? AND status NOT IN ('superseded','retracted')",params![status,crate::utcnow(),id])?;
            Ok(Applied::Complete)
        }
        "edge" => {
            let (Some(src), Some(dst)) = (
                resolve_uid(conn, text(p, "src_uid", 128)?)?,
                resolve_uid(conn, text(p, "dst_uid", 128)?)?,
            ) else {
                return Ok(Applied::Deferred);
            };
            let rel = text(p, "rel", 32)?;
            if !crate::graph::ASSERTED.contains(&rel) && rel != "supersedes" {
                return Err(Error::InvalidRequest);
            }
            crate::graph::edge_insert_in_transaction(
                &cfg,
                conn,
                src,
                dst,
                rel,
                text(p, "source", 32)?,
                optional(p, "session_id", 128)?.as_deref(),
                optional(p, "note", 4096)?.as_deref(),
                &authority,
            )?;
            Ok(Applied::Complete)
        }
        "outcome" => {
            let Some(id) = resolve_uid(conn, text(p, "belief_uid", 128)?)? else {
                return Ok(Applied::Deferred);
            };
            let uid = optional(p, "uid", 128)?;
            if let Some(uid) = &uid {
                if conn
                    .query_row("SELECT 1 FROM belief_outcomes WHERE uid=?", [uid], |r| {
                        r.get::<_, i64>(0)
                    })
                    .optional()?
                    .is_some()
                {
                    return Ok(Applied::Complete);
                }
            }
            beliefs::outcome_in_transaction(
                &cfg,
                conn,
                id,
                &fallback(p, "event", "confirmed", 32)?,
                &fallback(p, "source", "sync", 32)?,
                optional(p, "session_id", 128)?.as_deref(),
                optional(p, "agent", 128)?.as_deref(),
                optional(p, "note", 4096)?.as_deref(),
                uid.as_deref(),
                &authority,
            )?;
            Ok(Applied::Complete)
        }
        "dream_reviewed" => {
            let (Some(a), Some(b)) = (
                resolve_uid(conn, text(p, "a_uid", 128)?)?,
                resolve_uid(conn, text(p, "b_uid", 128)?)?,
            ) else {
                return Ok(Applied::Deferred);
            };
            conn.execute(
                "INSERT OR IGNORE INTO dream_reviewed(a,b) VALUES(?,?)",
                params![a.min(b), a.max(b)],
            )?;
            Ok(Applied::Complete)
        }
        _ => Ok(Applied::Unknown),
    }
}
/// Only absent/null *required references* are quarantined. An unknown UID may
/// arrive in a later page and remains deferred; malformed non-null values keep
/// their normal validation failure path. The optional outcome UID is not a ref.
fn missing_required_belief_uid(op: &Value) -> bool {
    if op["class"] != "belief" {
        return false;
    }
    let required: &[&str] = match op["op"].as_str() {
        Some("reinforce" | "retract" | "status") => &["uid"],
        Some("supersede") => &["uid", "by_uid"],
        Some("edge") => &["src_uid", "dst_uid"],
        Some("outcome") => &["belief_uid"],
        Some("dream_reviewed") => &["a_uid", "b_uid"],
        _ => return false,
    };
    required.iter().any(|field| op["payload"].get(*field).is_none_or(Value::is_null))
}
pub(crate) fn portable_key(key: &str, kind: &str, bucket: &str) -> Result<String> {
    let Some((prefix, digest)) = key.rsplit_once(':') else {
        return Err(Error::InvalidRequest);
    };
    if !prefix.starts_with(&format!("{kind}:"))
        || digest.len() != 20
        || !digest
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return Err(Error::InvalidRequest);
    }
    if kind == "filemap" && prefix.len() == "filemap:".len() {
        return Err(Error::InvalidRequest);
    }
    if kind == "memory"
        && !(bucket == "user" && prefix == "memory:user"
            || bucket.starts_with("project:")
                && prefix.starts_with("memory:project:")
                && prefix.len() > "memory:project:".len())
    {
        return Err(Error::InvalidRequest);
    }
    Ok(format!("{kind}:{bucket}:{digest}"))
}
fn keyed_entry(entries: &[String], kind: &str, bucket: &str, key: &str) -> Result<Option<String>> {
    let key = portable_key(key, kind, bucket)?;
    Ok(entries
        .iter()
        .find(|entry| gate::entry_key(kind, bucket, entry) == key)
        .cloned())
}
fn overflow(
    cfg: &Config,
    op: &Value,
    kind: &str,
    scope: &str,
    slug: &str,
    text: &str,
) -> Result<Applied> {
    let mut item = json!({"kind":kind,"action":"add","origin":"sync-overflow","project":slug,"text":text,"writer":op["payload"]["writer"],"via":op["payload"]["via"],"created":op["created"]});
    if kind == "memory" {
        item["scope"] = json!(scope);
        item["source_engine"] = op["payload"]["source_engine"].clone();
    } else {
        let (path, purpose) = text.split_once(filemap::SEP).unwrap_or((text, ""));
        item["path"] = json!(path.trim());
        item["purpose"] = json!(purpose.trim())
    }
    stage(cfg, &item, &deterministic_uid(text_field(op, "op_id")?))?;
    Ok(Applied::Staged)
}
fn text_field<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    text(v, key, 128)
}
fn conflict(
    conn: &Connection,
    op: &Value,
    kind: &str,
    bucket: &str,
    key: &str,
    new: &str,
) -> Result<bool> {
    let normalized = portable_key(key, kind, bucket)?;
    let mut stmt=conn.prepare("SELECT machine_id,payload FROM sync_ops WHERE class=? AND op='replace' AND applied=1 AND op_id!=? AND project_key IS ? LIMIT 10001")?;
    let mut budget = MAX_PAGE_BYTES;
    let mut query = stmt.query(params![
        op["class"].as_str(),
        op["op_id"].as_str(),
        optional(op, "project_key", 2048)?
    ])?;
    let mut rows = Vec::new();
    while let Some(row) = query.next()? {
        if rows.len() == 10000 {
            return Err(Error::TooLarge);
        }
        rows.push((
            crate::graph::db_text(row, 0, 128, &mut budget)?,
            crate::graph::db_text(row, 1, crate::MAX_FRAME_BYTES, &mut budget)?,
        ));
    }
    drop(query);
    drop(stmt);
    for (machine, raw) in rows {
        if machine == op["machine_id"].as_str().unwrap_or("") {
            continue;
        }
        let p: Value = serde_json::from_str(&raw).map_err(|_| Error::InvalidRequest)?;
        if p["old_key"]
            .as_str()
            .and_then(|k| portable_key(k, kind, bucket).ok())
            .as_ref()
            == Some(&normalized)
            && p["text"].as_str() != Some(new)
        {
            conn.execute("INSERT OR IGNORE INTO sync_conflicts(kind,bucket,old_key,a_text,b_text,op_id,created) VALUES(?,?,?,?,?,?,?)",params![kind,bucket,normalized,fallback(&p,"text","",65536)?,new,text_field(op,"op_id")?,crate::utcnow()])?;
            return Ok(true);
        }
    }
    Ok(false)
}
fn file_entries(cfg: &Config, conn: &Connection, op: &Value, kind: &str) -> Result<Applied> {
    let p = &op["payload"];
    let pk = optional(op, "project_key", 2048)?;
    let slug = local_slug(conn, pk.as_deref())?.unwrap_or_default();
    if kind == "filemap" && slug.is_empty() {
        return Err(Error::InvalidRequest);
    }
    let scope = if pk.is_none() {
        Scope::User
    } else {
        Scope::Project
    };
    let path = if kind == "memory" {
        scope.path(cfg, &slug)?
    } else {
        filemap::path(cfg, &slug)?
    };
    let bucket = if kind == "memory" {
        scope.bucket(&slug)
    } else {
        slug.clone()
    };
    let _lock = files::Locks::acquire(&cfg.root, &[path.clone()], cfg.timeout)?;
    let entries = memory::read_entries(&path)?;
    let verb = text(op, "op", 64)?;
    let mut action = verb;
    let mut old = String::new();
    let value = if verb == "remove" {
        String::new()
    } else {
        fallback(p, "text", "", 65536)?
    };
    match verb {
        "add" => {}
        "remove" => {
            let Some(entry) = keyed_entry(&entries, kind, &bucket, text(p, "key", 4096)?)? else {
                return Ok(Applied::Complete);
            };
            old = entry;
        }
        "replace" => {
            let key = text(p, "old_key", 4096)?;
            if let Some(entry) = keyed_entry(&entries, kind, &bucket, key)? {
                old = entry
            } else {
                if entries
                    .iter()
                    .any(|entry| entry.to_lowercase() == gate::one_line(&value).to_lowercase())
                {
                    return Ok(Applied::Complete);
                }
                let competing = conflict(conn, op, kind, &bucket, key, &value)?;
                action = if kind == "filemap" && competing {
                    "add-conflict"
                } else {
                    "add"
                };
            }
        }
        _ => return Ok(Applied::Unknown),
    }
    if action != "remove" && value.is_empty() {
        return Err(Error::InvalidRequest);
    }
    if matches!(action, "add" | "add-conflict")
        && entries
            .iter()
            .any(|entry| entry.to_lowercase() == gate::one_line(&value).to_lowercase())
    {
        return Ok(Applied::Complete);
    }
    let action = match action {
        "replace" => "replace-exact",
        "remove" => "remove-exact",
        other => other,
    };
    let cfg = suppressed(cfg);
    let authority = Authority::Interactive {
        agent: "sync-replay".into(),
        engine: fallback(p, "source_engine", "unknown", 32)?,
    };
    let via = fallback(p, "via", "direct", 32)?;
    let result =
        memory::observe_file_write(&[path.clone(), cfg.root.join("provenance.json")], || {
            if kind == "memory" {
                memory::mutate_locked(
                    &cfg,
                    scope,
                    &slug,
                    action,
                    &old,
                    &value,
                    &via,
                    Some("sync"),
                    Some(authority.engine()),
                    &authority,
                )
            } else {
                let (map_path, purpose) = value.split_once(filemap::SEP).unwrap_or((&value, ""));
                filemap::mutate_locked(
                    &cfg,
                    &slug,
                    action,
                    &old,
                    map_path.trim(),
                    purpose.trim(),
                    None,
                    &via,
                    &authority,
                )
            }
        });
    match result {
        Err(Error::OverCap) => return overflow(&cfg, op, kind, scope.name(), &slug, &value),
        Err(Error::MayHaveApplied) => return Err(Error::MayHaveApplied),
        Err(error) => return Err(error),
        Ok(memory::FileOutcome::Complete) => {}
        Ok(memory::FileOutcome::Partial(_) | memory::FileOutcome::Uncertain(_)) => {
            return Err(Error::MayHaveApplied)
        }
    }
    if action != "remove-exact" {
        let safe = gate::one_line(&scrub::scrub(&value)?);
        let mut provenance = json!({"via":via,"writer":p.get("writer").cloned().unwrap_or_else(||json!("unknown")),"at":fallback(op,"created",&crate::utcnow(),128)?});
        if kind == "memory" {
            provenance["source_engine"] = json!(gate::current_engine(authority.engine()));
        }
        if let Err(_) = gate::record_preserved(&cfg, kind, &bucket, &safe, &provenance, "sync") {
            return Err(Error::MayHaveApplied);
        }
    }
    mark(conn, op, APPLIED_YES).map_err(|_| Error::MayHaveApplied)?;
    Ok(Applied::Complete)
}

fn stored_wire_rows(
    stmt: &mut rusqlite::Statement<'_>,
    params: impl rusqlite::Params,
) -> Result<Vec<Value>> {
    let mut query = stmt.query(params)?;
    let mut rows = Vec::new();
    let mut budget = MAX_PAGE_BYTES;
    while let Some(row) = query.next()? {
        if rows.len() == 10000 {
            return Err(Error::TooLarge);
        }
        let id = crate::graph::db_text(row, 0, 128, &mut budget)?;
        let machine = crate::graph::db_text(row, 1, 128, &mut budget)?;
        let seq = row.get::<_, i64>(2)?;
        let lamport = row.get::<_, i64>(3)?;
        if seq < 0 || lamport < 0 {
            return Err(Error::InvalidRequest);
        }
        let class = crate::graph::db_text(row, 4, 64, &mut budget)?;
        let op = crate::graph::db_text(row, 5, 64, &mut budget)?;
        let pk = crate::graph::db_optional_text(row, 6, 2048, &mut budget)?;
        let payload_cap = if class == "session" && op == "msgs" {
            MAX_SESSION_PAYLOAD_BYTES + 8192
        } else {
            crate::MAX_FRAME_BYTES
        };
        let payload = crate::graph::db_text(row, 7, payload_cap, &mut budget)?;
        let mac = crate::graph::db_optional_text(row, 8, 64, &mut budget)?;
        let created = crate::graph::db_optional_text(row, 9, 128, &mut budget)?;
        rows.push(json!({"op_id":id,"machine_id":machine,"machine_seq":seq,"lamport":lamport,"class":class,"op":op,"project_key":pk,"payload":serde_json::from_str::<Value>(&payload).unwrap_or(Value::Null),"mac":mac,"created":created}));
    }
    Ok(rows)
}
fn prior_ops(conn: &Connection, class: &str, verb: &str) -> Result<Vec<Value>> {
    let mut stmt=conn.prepare("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created FROM sync_ops WHERE class=? AND op=? AND applied=1 LIMIT 10001")?;
    stored_wire_rows(&mut stmt, params![class, verb])
}
fn newer(conn: &Connection, op: &Value, verb: &str, field: &str, identity: &str) -> Result<bool> {
    let path = match field {
        "session_id" => "$.session_id",
        "name" => "$.name",
        _ => return Err(Error::InvalidRequest),
    };
    let (lamport, machine, sequence) = order(op);
    let present = conn.query_row(
        "SELECT 1 FROM sync_ops WHERE applied=? AND class=? AND op=? AND op_id<>? \
         AND CASE WHEN json_valid(payload) THEN json_extract(payload, ?) END=? \
         AND (lamport,machine_id,machine_seq)>(?,?,?) LIMIT 1",
        params![APPLIED_YES, text(op, "class", 64)?, verb, text(op, "op_id", 128)?,
            path, identity, lamport, machine, sequence],
        |row| row.get::<_, i64>(0),
    ).optional()?;
    Ok(present.is_some())
}
fn session(conn: &Connection, op: &Value) -> Result<Applied> {
    let p = &op["payload"];
    let sid = text(p, "session_id", 134)?;
    if !config::valid_id(sid) && !sid.strip_prefix("codex:").is_some_and(config::valid_id) {
        return Err(Error::InvalidRequest);
    }
    let verb = text(op, "op", 64)?;
    if newer(conn, op, verb, "session_id", sid)? {
        return Ok(Applied::Complete);
    }
    let slug = local_slug(conn, optional(op, "project_key", 2048)?.as_deref())?;
    match verb {
        "upsert" => {
            conn.execute("INSERT OR REPLACE INTO sessions(session_id,project,cwd,title,first_ts,last_ts,messages,engine) VALUES(?,?,?,?,?,?,?,?)",params![sid,slug,optional(p,"cwd",4096)?,optional(p,"title",4096)?,optional(p,"first_ts",128)?,optional(p,"last_ts",128)?,integer(p,"messages",0)?,fallback(p,"engine","claude",32)?])?;
            Ok(Applied::Complete)
        }
        "msgs" => {
            let rows = p["rows"]
                .as_array()
                .filter(|r| r.len() <= 20000)
                .ok_or(Error::InvalidRequest)?;
            let mut clean = Vec::new();
            for row in rows {
                if !row.is_object() {
                    return Err(Error::InvalidRequest);
                }
                clean.push((
                    fallback(row, "ts", "", 128)?,
                    fallback(row, "role", "", 32)?,
                    fallback(row, "content", "", 65536)?,
                ));
            }
            conn.execute("DELETE FROM msg WHERE session_id=?", [sid])?;
            for (ts, role, content) in clean {
                conn.execute(
                    "INSERT INTO msg(session_id,project,ts,role,content) VALUES(?,?,?,?,?)",
                    params![sid, slug, ts, role, scrub::scrub(&content)?],
                )?;
            }
            Ok(Applied::Complete)
        }
        _ => Ok(Applied::Unknown),
    }
}
fn remote_record(conn: &Connection, op: &Value) -> Result<Applied> {
    let p = &op["payload"];
    let machine = text(p, "machine_id", 128)?;
    let pk = optional(p, "project_key", 2048)?.or(optional(op, "project_key", 2048)?);
    let class = text(op, "class", 64)?;
    if op["op"] == "remove" {
        conn.execute(
            "DELETE FROM sync_remote_records WHERE class=? AND project_key IS ? AND machine_id=?",
            params![class, pk, machine],
        )?;
    } else {
        let record = p.get("record").cloned().unwrap_or_else(|| json!({}));
        conn.execute(
            "DELETE FROM sync_remote_records WHERE class=? AND project_key IS ? AND machine_id=?",
            params![class, pk, machine],
        )?;
        conn.execute("INSERT OR REPLACE INTO sync_remote_records(class,project_key,machine_id,record,updated) VALUES(?,?,?,?,?)",params![class,pk,machine,serde_json::to_string(&scrub::scrub_json(&record)?).map_err(|_|Error::InvalidRequest)?,crate::utcnow()])?;
    }
    Ok(Applied::Complete)
}
fn child_dir(path: &Path) -> Result<()> {
    if let Ok(metadata) = fs::symlink_metadata(path) {
        if !metadata.is_dir() {
            return Err(Error::UnsafePath);
        }
    }
    files::private_dir(path)
}
fn bounded_tree(path: &Path, depth: usize, count: &mut usize) -> Result<()> {
    if depth > 16 {
        return Err(Error::TooLarge);
    }
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        return Err(Error::UnsafePath);
    }
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? {
            *count += 1;
            if *count > MAX_DIRECTORY {
                return Err(Error::TooLarge);
            }
            bounded_tree(&entry?.path(), depth + 1, count)?
        }
    } else {
        files::read_regular(path, crate::MAX_FRAME_BYTES)?;
    }
    Ok(())
}
fn skill(cfg: &Config, conn: &Connection, op: &Value) -> Result<Applied> {
    let p = &op["payload"];
    let name = text(p, "name", 128)?;
    if !config::valid_skill_name(name) {
        return Err(Error::UnsafePath);
    }
    child_dir(&cfg.skills)?;
    let directory = cfg.skills.join(name);
    let target = directory.join("SKILL.md");
    if directory.try_exists()? && !fs::symlink_metadata(&directory)?.is_dir() {
        return Err(Error::UnsafePath);
    }
    let _lock = files::Locks::acquire(&cfg.root, &[target.clone()], cfg.timeout)?;
    let prior = prior_ops(conn, "skill", "put")?
        .into_iter()
        .filter(|old| old["op_id"] != op["op_id"] && old["payload"]["name"] == name)
        .max_by_key(order);
    let verb = text(op, "op", 64)?;
    if directory.try_exists()? {
        // A valid MAC does not grant ownership of a locally installed skill.
        // The current file must still match a previously applied LORE put.
        let owned = if let Some(old) = &prior {
            if target.try_exists()? {
                let current = files::read_regular(&target, crate::MAX_FRAME_BYTES)?;
                current == fallback(&old["payload"], "body", "", crate::MAX_FRAME_BYTES)?.as_bytes()
            } else {
                false
            }
        } else {
            false
        };
        let removable = if verb == "remove" && owned {
            let mut entries = fs::read_dir(&directory)?;
            entries.next().transpose()?.is_some_and(|e| e.file_name() == "SKILL.md")
                && entries.next().transpose()?.is_none()
        } else {
            owned
        };
        if !removable {
            stage(cfg, &json!({"kind":"skill","action":if verb == "remove" { "retire" } else { "update" },
                "name":name,"body":if verb == "put" { fallback(p,"body","",crate::MAX_FRAME_BYTES)? } else { String::new() },
                "description":format!("sync request conflicts with local skill {name}"),
                "origin":"sync-skill-local-conflict"}),
                &deterministic_uid(text_field(op, "op_id")?))?;
            mark(conn, op, APPLIED_YES)?;
            return Ok(Applied::Complete);
        }
    }
    match verb {
        "remove" => {
            if prior.as_ref().is_some_and(|old| order(old) > order(op)) {
                return Ok(Applied::Complete);
            }
            if directory.try_exists()? {
                bounded_tree(&directory, 0, &mut 0)?;
                fs::remove_dir_all(&directory).map_err(|_| Error::MayHaveApplied)?;
                File::open(&cfg.skills)
                    .and_then(|dir| dir.sync_all())
                    .map_err(|_| Error::MayHaveApplied)?;
            }
            mark(conn, op, APPLIED_YES)?;
            Ok(Applied::Complete)
        }
        "put" => {
            if newer(conn, op, "remove", "name", name)? {
                return Ok(Applied::Complete);
            }
            let body = fallback(p, "body", "", crate::MAX_FRAME_BYTES)?;
            if let Some(prior) = prior {
                let old = fallback(&prior["payload"], "body", "", crate::MAX_FRAME_BYTES)?;
                if old == body {
                    return Ok(Applied::Complete);
                }
                let losing = if order(&prior) > order(op) {
                    op
                } else {
                    &prior
                };
                let losing_body = if order(&prior) > order(op) {
                    &body
                } else {
                    &old
                };
                stage(
                    cfg,
                    &json!({"kind":"skill","action":"update","name":name,"body":losing_body,"description":format!("conflicting body for {name} from another machine"),"origin":"sync-skill-conflict"}),
                    &deterministic_uid(text_field(losing, "op_id")?),
                )?;
                if order(&prior) > order(op) {
                    return Ok(Applied::Complete);
                }
            }
            child_dir(&directory)?;
            if target.try_exists()? {
                files::read_regular(&target, crate::MAX_FRAME_BYTES)?;
            }
            files::atomic_write(&target, body.as_bytes()).map_err(|_| Error::MayHaveApplied)?;
            mark(conn, op, APPLIED_YES).map_err(|_| Error::MayHaveApplied)?;
            Ok(Applied::Complete)
        }
        _ => Ok(Applied::Unknown),
    }
}
fn path_component(raw: &str) -> String {
    let text = raw
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || matches!(c, '-' | '_') {
                c
            } else {
                '-'
            }
        })
        .collect::<String>();
    if text.is_empty() {
        "-".into()
    } else {
        text
    }
}
fn transcript(cfg: &Config, conn: &Connection, op: &Value) -> Result<Applied> {
    if op["op"] != "chunk" {
        return Ok(Applied::Unknown);
    }
    let p = &op["payload"];
    let sid = text(p, "session_id", 128)?;
    if path_component(sid) != sid {
        return Err(Error::UnsafePath);
    }
    let key = optional(op, "project_key", 2048)?.unwrap_or_else(|| "user".into());
    let key = path_component(&key);
    if key.len() > 255 {
        return Err(Error::TooLarge);
    }
    let base = cfg.root.join("transcripts");
    child_dir(&base)?;
    let dir = base.join(key);
    child_dir(&dir)?;
    let path = dir.join(format!("{sid}.jsonl"));
    let _lock = files::Locks::acquire(&cfg.root, &[path.clone()], cfg.timeout)?;
    let lines = p["lines"]
        .as_array()
        .filter(|r| r.len() <= 10000)
        .ok_or(Error::InvalidRequest)?;
    let mut new = Vec::new();
    for line in lines {
        let line = line.as_str().ok_or(Error::InvalidRequest)?;
        if line.len() > crate::MAX_FRAME_BYTES || line.contains('\0') {
            return Err(Error::InvalidRequest);
        }
        new.push(scrub::scrub(line.trim_end_matches('\n'))?);
    }
    let mut held = if path.try_exists()? {
        files::read_regular(&path, 64 * 1024 * 1024)?
    } else {
        Vec::new()
    };
    let raw = std::str::from_utf8(&held).map_err(|_| Error::InvalidRequest)?;
    if !raw.is_empty() && !raw.ends_with('\n') {
        return Err(Error::MayHaveApplied);
    }
    let count = raw.lines().count();
    let to = usize::try_from(integer(p, "to_line", 0)?).map_err(|_| Error::TooLarge)?;
    let from = usize::try_from(integer(p, "from_line", 1)?).map_err(|_| Error::TooLarge)?;
    if to > 0 && to <= count {
        return Ok(Applied::Complete);
    }
    if from == 0
        || to > 0
            && to
                != from
                    .checked_add(new.len())
                    .ok_or(Error::TooLarge)?
                    .saturating_sub(1)
    {
        return Err(Error::InvalidRequest);
    }
    if from > count + 1 {
        return Ok(Applied::Deferred);
    }
    for line in new.into_iter().skip(count.saturating_sub(from - 1)) {
        held.extend_from_slice(line.as_bytes());
        held.push(b'\n');
    }
    if held.len() > 64 * 1024 * 1024 {
        return Err(Error::TooLarge);
    }
    files::atomic_write(&path, &held).map_err(|_| Error::MayHaveApplied)?;
    mark(conn, op, APPLIED_YES).map_err(|_| Error::MayHaveApplied)?;
    Ok(Applied::Complete)
}
fn pending_op(cfg: &Config, op: &Value) -> Result<Applied> {
    let p = &op["payload"];
    let uid = text(p, "uid", 128)?;
    if !pending::valid_portable_uid(uid) {
        return Err(Error::InvalidRequest);
    }
    match text(op, "op", 64)? {
        "stage" => {
            if !p["item"].is_object() {
                return Err(Error::InvalidRequest);
            }
            stage(cfg, &p["item"], uid)?;
            Ok(Applied::Complete)
        }
        "resolve" => {
            pending::archive_uid(
                &suppressed(cfg),
                uid,
                &fallback(p, "status", "approved", 32)?,
            )?;
            Ok(Applied::Complete)
        }
        _ => Ok(Applied::Unknown),
    }
}
fn dispatch(cfg: &Config, conn: &Connection, op: &Value) -> Result<Applied> {
    match text(op, "class", 64)? {
        "memory" => file_entries(cfg, conn, op, "memory"),
        "filemap" => file_entries(cfg, conn, op, "filemap"),
        "belief" => belief(cfg, conn, op),
        "session" => session(conn, op),
        "pending" => pending_op(cfg, op),
        "skill" => skill(cfg, conn, op),
        "transcript" => transcript(cfg, conn, op),
        "tabset" | "worktree" => remote_record(conn, op),
        _ => Ok(Applied::Unknown),
    }
}

fn database_class(op: &Value) -> bool {
    matches!(
        op["class"].as_str(),
        Some("belief" | "session" | "tabset" | "worktree")
    )
}
fn state(conn: &Connection, op: &Value) -> Result<Option<i64>> {
    Ok(conn
        .query_row(
            "SELECT applied FROM sync_ops WHERE op_id=?",
            [text_field(op, "op_id")?],
            |r| r.get(0),
        )
        .optional()?)
}
fn same_recorded_proof(conn: &Connection, op: &Value) -> Result<bool> {
    let mut stmt = conn.prepare("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created FROM sync_ops WHERE op_id=?")?;
    let prior = stored_wire_rows(&mut stmt, [text_field(op, "op_id")?])?;
    Ok(prior.len() == 1 && tuple(&prior[0]) == tuple(op) && prior[0]["mac"] == op["mac"])
}
fn receive_lock(cfg: &Config, op: &Value) -> Result<files::Locks> {
    files::Locks::acquire(
        &cfg.root,
        &[cfg.root.join(format!(
            ".sync-op-{}",
            deterministic_uid(text_field(op, "op_id")?)
        ))],
        cfg.timeout,
    )
}
fn effect(cfg: &Config, conn: &mut Connection, op: &Value) -> Result<Applied> {
    if database_class(op) {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if state(&tx, op)? == Some(APPLIED_YES) {
            return Ok(Applied::Complete);
        }
        let result = dispatch(cfg, &tx, op)?;
        if result == Applied::Complete {
            mark(&tx, op, APPLIED_YES)?;
        } else if result == Applied::Unknown {
            mark(&tx, op, APPLIED_UNKNOWN)?;
        }
        tx.commit()?;
        Ok(result)
    } else {
        let result = dispatch(cfg, conn, op)?;
        match result {
            Applied::Complete | Applied::Staged => {
                mark(conn, op, APPLIED_YES).map_err(|_| Error::MayHaveApplied)?
            }
            Applied::Unknown => mark(conn, op, APPLIED_UNKNOWN)?,
            Applied::Deferred => {}
        }
        Ok(result)
    }
}
fn note_failure(conn: &Connection, op: &Value, error: Error, report: &mut Report) -> Result<()> {
    mark(conn, op, APPLIED_FAILED)?;
    report.failed += 1;
    if error == Error::MayHaveApplied {
        report.partial += 1
    }
    eprintln!("sync apply: operation refused ({})", error.code());
    Ok(())
}
pub fn apply_ops(cfg: &Config, ops: &[Value]) -> Result<Value> {
    if ops.len() > MAX_PAGE {
        return Err(Error::TooLarge);
    }
    let size = ops.iter().try_fold(0usize, |size, op| {
        size.checked_add(
            serde_json::to_vec(op)
                .map_err(|_| Error::InvalidRequest)?
                .len(),
        )
        .ok_or(Error::TooLarge)
    })?;
    if size > MAX_PAGE_BYTES {
        return Err(Error::TooLarge);
    }
    let mut conn = store::connect(cfg)?;
    let mut report = Report::default();
    let mut deferred = BTreeSet::new();
    for op in canonical_order(ops) {
        if validate_envelope(&op).is_err() {
            report.unknown += 1;
            continue;
        }
        if disabled(cfg, text(&op, "class", 64)?) {
            report.skipped += 1;
            continue;
        }
        let _lock = match receive_lock(cfg, &op) {
            Ok(lock) => lock,
            Err(_) => {
                report.failed += 1;
                continue;
            }
        };
        if !verify_mac(&op, cfg.sync.key.as_deref()) {
            // Stage before reserving even an unverified ID. A failed disk write
            // must remain retryable, while never taking a verified machine slot.
            match unverified(cfg, &op) {
                Ok(()) => {
                    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
                    if record(&tx, &op, APPLIED_UNVERIFIED)? {
                        report.unverified += 1
                    } else if state(&tx, &op)? == Some(APPLIED_FAILED) {
                        // An unauthenticated reuse of a failed ID cannot
                        // settle the delivery and release the pull cursor.
                        report.failed += 1
                    } else {
                        report.duplicate += 1
                    }
                    tx.commit()?
                }
                Err(error) => {
                    report.failed += 1;
                    if error == Error::MayHaveApplied {
                        report.partial += 1
                    }
                }
            }
            continue;
        }
        let unknown = !CLASSES.contains(&text(&op, "class", 64)?);
        let quarantine = missing_required_belief_uid(&op);
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let initial = if unknown { APPLIED_UNKNOWN } else if quarantine { APPLIED_QUARANTINED } else { APPLIED_NO };
        if !record(&tx, &op, initial)? {
            // A failed effect retains the authenticated operation so the next
            // delivery can retry it. Never let a different signed envelope
            // borrow that op_id, even when its MAC is otherwise valid.
            match state(&tx, &op)? {
                Some(APPLIED_FAILED) if same_recorded_proof(&tx, &op)? => {}
                Some(_) if !same_recorded_proof(&tx, &op)? => {
                    report.failed += 1;
                    tx.commit()?;
                    continue;
                }
                _ => {
                    report.duplicate += 1;
                    tx.commit()?;
                    continue;
                }
            }
        }
        if let Err(error) = observe(&tx, &op) {
            mark(&tx, &op, APPLIED_FAILED)?;
            tx.commit()?;
            report.failed += 1;
            eprintln!("sync apply: clock refused ({})", error.code());
            continue;
        }
        tx.commit()?;
        if unknown {
            report.unknown += 1;
            continue;
        }
        if quarantine {
            report.quarantined += 1;
            continue;
        }
        match effect(cfg, &mut conn, &op) {
            Ok(Applied::Complete) => report.applied += 1,
            Ok(Applied::Staged) => {
                report.applied += 1;
                report.staged += 1
            }
            Ok(Applied::Deferred) => {
                report.deferred += 1;
                deferred.insert(text_field(&op, "op_id")?.to_owned());
            }
            Ok(Applied::Unknown) => report.unknown += 1,
            Err(error) => note_failure(&conn, &op, error, &mut report)?,
        }
    }
    retry_with_connection(cfg, &mut conn)?;
    for id in deferred {
        let result = conn.query_row("SELECT applied FROM sync_ops WHERE op_id=?", [id], |r| {
            r.get::<_, i64>(0)
        })?;
        if result == APPLIED_YES {
            report.applied += 1;
            report.deferred -= 1
        } else if result == APPLIED_FAILED {
            report.failed += 1;
            report.deferred -= 1
        }
    }
    Ok(report.value())
}
fn stored_ops(conn: &Connection) -> Result<Vec<Value>> {
    let mut stmt=conn.prepare("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created FROM sync_ops WHERE applied=0 LIMIT 10001")?;
    stored_wire_rows(&mut stmt, [])
}
fn retry_with_connection(cfg: &Config, conn: &mut Connection) -> Result<usize> {
    let mut landed = 0;
    for _ in 0..10000 {
        let ops = canonical_order(&stored_ops(conn)?);
        let mut progress = false;
        for op in ops {
            if disabled(cfg, text(&op, "class", 64)?) {
                continue;
            }
            let _lock = receive_lock(cfg, &op)?;
            if state(conn, &op)? != Some(APPLIED_NO) {
                continue;
            }
            if validate_envelope(&op).is_err() || !CLASSES.contains(&text(&op, "class", 64)?) {
                mark(conn, &op, APPLIED_FAILED)?;
                progress = true;
                continue;
            }
            if missing_required_belief_uid(&op) {
                mark(conn, &op, APPLIED_QUARANTINED)?;
                progress = true;
                continue;
            }
            // Only previously authenticated/approved state zero is eligible.
            // A rotated or removed key cannot turn it into an unverified op.
            match effect(cfg, conn, &op) {
                Ok(Applied::Complete | Applied::Staged) => {
                    landed += 1;
                    progress = true
                }
                Ok(Applied::Deferred) => {}
                Ok(Applied::Unknown) => {
                    mark(conn, &op, APPLIED_UNKNOWN)?;
                    progress = true
                }
                Err(error) => {
                    mark(conn, &op, APPLIED_FAILED)?;
                    progress = true;
                    eprintln!("sync apply: deferred operation refused ({})", error.code());
                }
            }
        }
        if !progress {
            return Ok(landed);
        }
    }
    Err(Error::TooLarge)
}
pub fn retry_deferred(cfg: &Config) -> Result<usize> {
    let mut conn = store::connect(cfg)?;
    retry_with_connection(cfg, &mut conn)
}
pub fn approve(cfg: &Config, op: &Value, authority: &Authority) -> Result<()> {
    authority.require_review()?;
    validate_envelope(op)?;
    if missing_required_belief_uid(op) {
        return Err(Error::InvalidRequest);
    }
    if disabled(cfg, text(op, "class", 64)?) {
        return Err(Error::Untrusted);
    }
    if !CLASSES.contains(&text(op, "class", 64)?) {
        return Err(Error::Unsupported);
    }
    let _lock = receive_lock(cfg, op)?;
    let mut conn = store::connect(cfg)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let held = one_text(
        &tx,
        "SELECT op_id FROM sync_ops WHERE machine_id=? AND machine_seq=? AND applied!=2",
        params![
            text(op, "machine_id", 128)?,
            integer(op, "machine_seq", -1)?
        ],
        128,
    )?;
    if held
        .as_deref()
        .is_some_and(|id| id != text_field(op, "op_id").unwrap_or(""))
    {
        return Err(Error::Changed);
    }
    let mut proof_budget = MAX_PAGE_BYTES;
    let mut prior_query = tx.prepare("SELECT payload FROM sync_ops WHERE op_id=?")?;
    let mut prior_rows = prior_query.query([text_field(op, "op_id")?])?;
    let prior = if let Some(row) = prior_rows.next()? {
        Some(crate::graph::db_text(
            row,
            0,
            crate::MAX_FRAME_BYTES,
            &mut proof_budget,
        )?)
    } else {
        None
    };
    drop(prior_rows);
    drop(prior_query);
    if let Some(raw) = prior {
        let prior: Value = serde_json::from_str(&raw).map_err(|_| Error::InvalidRequest)?;
        if prior != op["payload"] {
            return Err(Error::Changed);
        }
        let mut proof_stmt=tx.prepare("SELECT machine_id,machine_seq,lamport,class,op,project_key,mac FROM sync_ops WHERE op_id=?")?;
        let mut proof_rows = proof_stmt.query([text_field(op, "op_id")?])?;
        let row = proof_rows.next()?.ok_or(Error::Changed)?;
        let original = json!([
            op["op_id"],
            crate::graph::db_text(row, 0, 128, &mut proof_budget)?,
            row.get::<_, i64>(1)?,
            row.get::<_, i64>(2)?,
            crate::graph::db_text(row, 3, 64, &mut proof_budget)?,
            crate::graph::db_text(row, 4, 64, &mut proof_budget)?,
            crate::graph::db_optional_text(row, 5, 2048, &mut proof_budget)?,
            prior
        ]);
        let mac = crate::graph::db_optional_text(row, 6, 64, &mut proof_budget)?;
        drop(proof_rows);
        drop(proof_stmt);
        if original != tuple(op) || json!(mac) != op.get("mac").cloned().unwrap_or(Value::Null) {
            return Err(Error::Changed);
        }
        if state(&tx, op)? == Some(APPLIED_YES) {
            return Ok(());
        }
        mark(&tx, op, APPLIED_NO)?;
    } else if !record(&tx, op, APPLIED_NO)? {
        return Err(Error::Changed);
    }
    observe(&tx, op)?;
    if database_class(op) {
        let outcome = dispatch(cfg, &tx, op)?;
        if outcome != Applied::Complete {
            return Err(if outcome == Applied::Unknown {
                Error::Unsupported
            } else {
                Error::Unavailable
            });
        }
        mark(&tx, op, APPLIED_YES)?;
        tx.commit().map_err(|_| Error::MayHaveApplied)?;
        return Ok(());
    }
    // Files cannot be rolled back by SQLite. After this acceptance point every
    // error is explicitly uncertain, and the pending carrier retains its claim.
    tx.commit().map_err(|_| Error::MayHaveApplied)?;
    match effect(cfg, &mut conn, op) {
        Ok(Applied::Complete) => Ok(()),
        Ok(Applied::Staged) => {
            mark(&conn, op, APPLIED_FAILED).map_err(|_| Error::MayHaveApplied)?;
            Err(Error::MayHaveApplied)
        }
        Ok(Applied::Deferred) => Err(Error::MayHaveApplied),
        Ok(Applied::Unknown) => {
            mark(&conn, op, APPLIED_UNKNOWN)?;
            Err(Error::MayHaveApplied)
        }
        Err(error) => {
            mark(&conn, op, APPLIED_FAILED).map_err(|_| Error::MayHaveApplied)?;
            eprintln!("sync approval: operation refused ({})", error.code());
            Err(Error::MayHaveApplied)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn oversized_and_aggregate_stored_payloads_refuse_without_retry_effects() {
        let (_temp, cfg) = fixture();
        let conn = store::connect(&cfg).unwrap();
        let insert = |id: &str, seq: i64, payload: &str, state: i64| {
            conn.execute("INSERT INTO sync_ops(op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,created,applied) VALUES(?,'fixture-machine',?,?,'skill','put',NULL,?,'2026-01-01',?)",params![id,seq,seq,payload,state]).unwrap();
        };
        insert(
            "oversized",
            1,
            &"x".repeat(crate::MAX_FRAME_BYTES + 1),
            APPLIED_NO,
        );
        assert!(matches!(stored_ops(&conn), Err(Error::TooLarge)));
        assert_eq!(
            conn.query_row("SELECT applied FROM sync_ops", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            APPLIED_NO
        );
        conn.execute("DELETE FROM sync_ops", []).unwrap();
        let payload =
            serde_json::to_string(&json!({"name":"fixture","body":"x".repeat(700000)})).unwrap();
        for seq in 1..=13 {
            insert(&format!("aggregate-{seq}"), seq, &payload, APPLIED_YES);
        }
        assert!(matches!(
            prior_ops(&conn, "skill", "put"),
            Err(Error::TooLarge)
        ));
        conn.execute("DELETE FROM sync_ops", []).unwrap();
        insert(
            "ordinary",
            1,
            r#"{"name":"fixture","body":"ordinary"}"#,
            APPLIED_YES,
        );
        assert_eq!(
            prior_ops(&conn, "skill", "put").unwrap()[0]["payload"]["body"],
            "ordinary"
        );
    }
    fn fixture() -> (tempfile::TempDir, Config) {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("lore"));
        cfg.sync.enabled = true;
        cfg.sync.classes = [
            "memory", "filemap", "beliefs", "pending", "skills", "sessions",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect();
        cfg.sync.key = Some("owned-fixture-key".into());
        (temp, cfg)
    }
    fn op(
        cfg: &Config,
        id: &str,
        seq: i64,
        class: &str,
        verb: &str,
        pk: Option<&str>,
        payload: Value,
    ) -> Value {
        let mut op = json!({"op_id":id,"machine_id":"fixture-author","machine_seq":seq,"lamport":seq,"class":class,"op":verb,"project_key":pk,"payload":payload,"created":"2026-09-27T00:00:00Z","mac":null});
        op["mac"] =
            json!(store::canonical_mac(&tuple(&op), cfg.sync.key.as_deref().unwrap()).unwrap());
        op
    }
    fn imported(conn: &Connection) -> Vec<Value> {
        let mut stmt=conn.prepare("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created FROM sync_ops ORDER BY seq").unwrap();
        stmt.query_map([],|r|Ok(json!({"op_id":r.get::<_,String>(0)?,"machine_id":r.get::<_,String>(1)?,"machine_seq":r.get::<_,i64>(2)?,"lamport":r.get::<_,i64>(3)?,"class":r.get::<_,String>(4)?,"op":r.get::<_,String>(5)?,"project_key":r.get::<_,Option<String>>(6)?,"payload":serde_json::from_str::<Value>(&r.get::<_,String>(7)?).unwrap(),"mac":r.get::<_,Option<String>>(8)?,"created":r.get::<_,String>(9)?}))).unwrap().collect::<std::result::Result<_,_>>().unwrap()
    }
    #[test]
    fn forged_slot_and_disabled_classes_cannot_move_clock_or_block_real_author() {
        let (_temp, cfg) = fixture();
        let real = op(
            &cfg,
            "real",
            1,
            "memory",
            "add",
            None,
            json!({"text":"verified fixture","writer":"terminal","via":"direct","source_engine":"codex"}),
        );
        let mut forged = real.clone();
        forged["op_id"] = json!("forged");
        forged["lamport"] = json!(i64::MAX);
        forged["mac"] = Value::Null;
        assert_eq!(apply_ops(&cfg, &[forged.clone()]).unwrap()["unverified"], 1);
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(
            conn.query_row("SELECT count(*) FROM sync_machine", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert!(!cfg.root.join("USER.md").exists());
        drop(conn);
        let report = apply_ops(&cfg, &[real.clone()]).unwrap();
        assert_eq!(report["applied"], 1);
        assert_eq!(
            memory::read_entries(&cfg.root.join("USER.md")).unwrap(),
            ["verified fixture"]
        );
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(state(&conn, &forged).unwrap(), Some(APPLIED_UNVERIFIED));
        assert_eq!(state(&conn, &real).unwrap(), Some(APPLIED_YES));
        drop(conn);
        assert_eq!(apply_ops(&cfg, &[real]).unwrap()["duplicate"], 1);
        let mut disabled_cfg = cfg.clone();
        disabled_cfg.sync.classes.remove("memory");
        let blocked = op(
            &cfg,
            "disabled",
            2,
            "memory",
            "add",
            None,
            json!({"text":"must not land"}),
        );
        assert_eq!(apply_ops(&disabled_cfg, &[blocked]).unwrap()["skipped"], 1);
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(
            conn.query_row("SELECT count(*) FROM sync_ops", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            2
        );
    }
    #[test]
    fn source_and_receiver_project_keys_replay_without_echo_and_keep_provenance() {
        let (_source_dir, source) = fixture();
        let (_receiver_dir, receiver) = fixture();
        let pk = "github.com/owned/fixture";
        let source_db = store::connect(&source).unwrap();
        source_db.execute("INSERT INTO sync_projects(project_key,slug,created) VALUES(?,'source-checkout','now')",[pk]).unwrap();
        source_db.execute("INSERT INTO sync_machine(machine_id,label,lamport) VALUES('source-owner','fixture',0)",[]).unwrap();
        drop(source_db);
        let receiver_db = store::connect(&receiver).unwrap();
        receiver_db.execute("INSERT INTO sync_projects(project_key,slug,created) VALUES(?,'receiver-checkout','now')",[pk]).unwrap();
        drop(receiver_db);
        let authority = Authority::Interactive {
            agent: "source-operator".into(),
            engine: "codex".into(),
        };
        memory::mutate(
            &source,
            Scope::Project,
            "source-checkout",
            "add",
            "",
            "original portable entry",
            "direct",
            None,
            Some("codex"),
            &authority,
        )
        .unwrap();
        let db = store::connect(&source).unwrap();
        let first = imported(&db);
        drop(db);
        assert_eq!(apply_ops(&receiver, &first).unwrap()["applied"], 1);
        let path = Scope::Project.path(&receiver, "receiver-checkout").unwrap();
        assert_eq!(
            memory::read_entries(&path).unwrap(),
            ["original portable entry"]
        );
        let provenance = gate::provenance(
            &receiver,
            "memory",
            "project:receiver-checkout",
            "original portable entry",
        );
        assert_eq!(provenance["source_engine"], "codex");
        assert_eq!(provenance["writer"], "interactive");
        memory::mutate(
            &source,
            Scope::Project,
            "source-checkout",
            "replace",
            "original portable entry",
            "replacement portable entry",
            "direct",
            None,
            Some("codex"),
            &authority,
        )
        .unwrap();
        let db = store::connect(&source).unwrap();
        let ops = imported(&db);
        drop(db);
        assert_eq!(apply_ops(&receiver, &ops).unwrap()["applied"], 1);
        assert_eq!(
            memory::read_entries(&path).unwrap(),
            ["replacement portable entry"]
        );
        memory::mutate(
            &source,
            Scope::Project,
            "source-checkout",
            "remove",
            "replacement portable entry",
            "",
            "direct",
            None,
            None,
            &authority,
        )
        .unwrap();
        let db = store::connect(&source).unwrap();
        let ops = imported(&db);
        drop(db);
        apply_ops(&receiver, &ops).unwrap();
        assert!(memory::read_entries(&path).unwrap().is_empty());
        let db = store::connect(&receiver).unwrap();
        assert_eq!(
            db.query_row("SELECT count(*) FROM sync_ops", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            3
        );
        let local = store::machine_id(&db).unwrap();
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM sync_ops WHERE machine_id=?",
                [local],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
    }
    #[test]
    fn missing_belief_dependencies_retry_aliases_and_bad_batch_member_are_isolated() {
        let (_temp, cfg) = fixture();
        let human = Authority::Interactive {
            agent: "fixture".into(),
            engine: "claude".into(),
        };
        let existing = beliefs::insert(
            &cfg,
            &json!({"subject":"user","claim":"folded fixture","confidence":0.7}),
            &human,
        )
        .unwrap()["id"]
            .as_i64()
            .unwrap();
        let edge = op(
            &cfg,
            "edge",
            1,
            "belief",
            "edge",
            None,
            json!({"src_uid":"remote-a","dst_uid":"remote-b","rel":"depends_on","source":"derived","session_id":"source-session"}),
        );
        assert_eq!(apply_ops(&cfg, &[edge.clone()]).unwrap()["deferred"], 1);
        let a = op(
            &cfg,
            "a",
            2,
            "belief",
            "insert",
            None,
            json!({"uid":"remote-a","subject":"user","claim":"folded fixture","confidence":0.8,"writer":"derived","via":"dream","source_engine":"codex"}),
        );
        let bad = op(
            &cfg,
            "bad",
            3,
            "belief",
            "insert",
            None,
            json!({"uid":"bad-uid","subject":"user","claim":"bad fixture","confidence":"not-number"}),
        );
        let b = op(
            &cfg,
            "b",
            4,
            "belief",
            "insert",
            None,
            json!({"uid":"remote-b","subject":"user","claim":"independent fixture","confidence":0.9,"writer":"derived","via":"derived","source_engine":"codex"}),
        );
        let report = apply_ops(&cfg, &[b.clone(), bad.clone(), a]).unwrap();
        assert_eq!(report["applied"], 2);
        assert_eq!(report["failed"], 1);
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(state(&conn, &edge).unwrap(), Some(APPLIED_YES));
        assert_eq!(resolve_uid(&conn, "remote-a").unwrap(), Some(existing));
        assert_eq!(state(&conn, &bad).unwrap(), Some(APPLIED_FAILED));
        assert_eq!(
            conn.query_row(
                "SELECT count(*) FROM beliefs WHERE uid='bad-uid'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
        assert_eq!(
            conn.query_row(
                "SELECT writer,via,source_engine FROM beliefs WHERE uid='remote-b'",
                [],
                |r| Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?
                ))
            )
            .unwrap(),
            ("derived".into(), "derived".into(), "codex".into())
        );
    }
    #[test]
    fn session_replacement_transcript_gaps_and_remote_null_keys_are_real_effects() {
        let (_temp, cfg) = fixture();
        let second = op(
            &cfg,
            "tail",
            1,
            "transcript",
            "chunk",
            None,
            json!({"session_id":"owned-session","from_line":2,"to_line":2,"lines":["second"]}),
        );
        assert_eq!(apply_ops(&cfg, &[second.clone()]).unwrap()["deferred"], 1);
        let first = op(
            &cfg,
            "head",
            2,
            "transcript",
            "chunk",
            None,
            json!({"session_id":"owned-session","from_line":1,"to_line":1,"lines":["first"]}),
        );
        apply_ops(&cfg, &[first]).unwrap();
        assert_eq!(
            fs::read_to_string(cfg.root.join("transcripts/user/owned-session.jsonl")).unwrap(),
            "first\nsecond\n"
        );
        let upsert = op(
            &cfg,
            "upsert",
            3,
            "session",
            "upsert",
            None,
            json!({"session_id":"codex:owned","messages":1,"engine":"codex"}),
        );
        let messages = op(
            &cfg,
            "msgs",
            4,
            "session",
            "msgs",
            None,
            json!({"session_id":"codex:owned","rows":[{"ts":"now","role":"assistant","content":"owned history"}]}),
        );
        let tab = op(
            &cfg,
            "tab",
            5,
            "tabset",
            "upsert",
            None,
            json!({"machine_id":"fixture-author","record":{"tabs":["remote advisory"]}}),
        );
        let tab2 = op(
            &cfg,
            "tab2",
            6,
            "tabset",
            "upsert",
            None,
            json!({"machine_id":"fixture-author","record":{"tabs":["new advisory"]}}),
        );
        let worktree = op(
            &cfg,
            "worktree",
            7,
            "worktree",
            "upsert",
            None,
            json!({"machine_id":"fixture-author","record":{"path":"/display-only/never-open"}}),
        );
        let report = apply_ops(&cfg, &[upsert, messages, tab, tab2, worktree]).unwrap();
        assert_eq!(report["applied"], 5);
        let db = store::connect(&cfg).unwrap();
        assert_eq!(
            db.query_row(
                "SELECT content FROM msg WHERE session_id='codex:owned'",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
            "owned history"
        );
        assert_eq!(db.query_row("SELECT count(*) FROM sync_remote_records WHERE class='tabset' AND project_key IS NULL",[],|r|r.get::<_,i64>(0)).unwrap(),1);
        assert_eq!(state(&db, &second).unwrap(), Some(APPLIED_YES));
    }
    #[test]
    fn pending_original_uid_skill_conflict_and_filemap_ops_are_supported() {
        let (_temp, cfg) = fixture();
        let pending = op(
            &cfg,
            "pending-stage",
            1,
            "pending",
            "stage",
            None,
            json!({"uid":"shared-proposal","item":{"kind":"memory","scope":"user","text":"original proposal","source_engine":"codex","writer":"model"}}),
        );
        let put = op(
            &cfg,
            "skill-old",
            2,
            "skill",
            "put",
            None,
            json!({"name":"owned-fixture","body":"old safe recipe"}),
        );
        let newer = op(
            &cfg,
            "skill-new",
            3,
            "skill",
            "put",
            None,
            json!({"name":"owned-fixture","body":"new safe recipe"}),
        );
        let map = op(
            &cfg,
            "filemap",
            4,
            "filemap",
            "add",
            Some("owned/project"),
            json!({"text":"src/main.rs — entry point","writer":"terminal","via":"direct"}),
        );
        assert_eq!(
            apply_ops(&cfg, &[pending, put, newer, map]).unwrap()["applied"],
            4
        );
        assert_eq!(
            fs::read_to_string(cfg.skills.join("owned-fixture/SKILL.md")).unwrap(),
            "new safe recipe"
        );
        let files = proposal_files(&cfg, false).unwrap();
        let rows = files
            .iter()
            .map(|p| {
                serde_json::from_slice::<Value>(
                    &files::read_regular(p, crate::MAX_FRAME_BYTES).unwrap(),
                )
                .unwrap()
            })
            .collect::<Vec<_>>();
        assert!(rows.iter().any(|r| r["uid"] == "shared-proposal"
            && r["source_engine"] == "codex"
            && r["writer"] == "model"));
        assert!(rows
            .iter()
            .any(|r| r["uid"] == deterministic_uid("skill-old") && r["body"] == "old safe recipe"));
        let resolve = op(
            &cfg,
            "resolve",
            5,
            "pending",
            "resolve",
            None,
            json!({"uid":"shared-proposal","status":"rejected"}),
        );
        let remove = op(
            &cfg,
            "remove-skill",
            6,
            "skill",
            "remove",
            None,
            json!({"name":"owned-fixture"}),
        );
        assert_eq!(apply_ops(&cfg, &[resolve, remove]).unwrap()["applied"], 2);
        assert!(!cfg.skills.join("owned-fixture").exists());
        assert!(proposal_files(&cfg, true).unwrap().iter().any(
            |p| serde_json::from_slice::<Value>(
                &files::read_regular(p, crate::MAX_FRAME_BYTES).unwrap()
            )
            .unwrap()["uid"]
                == "shared-proposal"
        ));
    }
    #[test]
    fn signed_skill_ops_preserve_manual_installations_and_local_edits() {
        let (_temp, cfg) = fixture();
        let manual = cfg.skills.join("manual-skill");
        fs::create_dir_all(&manual).unwrap();
        fs::write(manual.join("SKILL.md"), "manual instructions").unwrap();
        let put = op(&cfg, "manual-put", 1, "skill", "put", None,
            json!({"name":"manual-skill","body":"remote instructions"}));
        let remove = op(&cfg, "manual-remove", 2, "skill", "remove", None,
            json!({"name":"manual-skill"}));
        assert_eq!(apply_ops(&cfg, &[put, remove]).unwrap()["applied"], 2);
        assert_eq!(fs::read_to_string(manual.join("SKILL.md")).unwrap(), "manual instructions");
        let proposals = proposal_files(&cfg, false).unwrap();
        assert_eq!(proposals.len(), 2);

        let initial = op(&cfg, "owned-put", 3, "skill", "put", None,
            json!({"name":"edited-skill","body":"synced body"}));
        assert_eq!(apply_ops(&cfg, &[initial]).unwrap()["applied"], 1);
        let edited = cfg.skills.join("edited-skill");
        fs::write(edited.join("SKILL.md"), "local edit").unwrap();
        let update = op(&cfg, "owned-update", 4, "skill", "put", None,
            json!({"name":"edited-skill","body":"new remote body"}));
        assert_eq!(apply_ops(&cfg, &[update]).unwrap()["applied"], 1);
        assert_eq!(fs::read_to_string(edited.join("SKILL.md")).unwrap(), "local edit");
        fs::write(edited.join("SKILL.md"), "synced body").unwrap();
        fs::write(edited.join("notes.txt"), "manual asset").unwrap();
        let remove = op(&cfg, "owned-remove", 5, "skill", "remove", None,
            json!({"name":"edited-skill"}));
        assert_eq!(apply_ops(&cfg, &[remove]).unwrap()["applied"], 1);
        assert!(edited.join("SKILL.md").exists());
        assert!(edited.join("notes.txt").exists());
    }
    #[test]
    fn portable_pending_uids_are_exact_data_keys_not_filenames() {
        let (temp, cfg) = fixture();
        let outside = temp.path().join("outside.json");
        fs::write(&outside, "owned outside sentinel").unwrap();
        for (index, uid) in ["sync:legacy:portable", "sync:雪", "../../outside.json"]
            .into_iter()
            .enumerate()
        {
            let base = (index as i64) * 4 + 1;
            let staged = op(
                &cfg,
                &format!("stage-{index}"),
                base,
                "pending",
                "stage",
                None,
                json!({"uid":uid,"item":{"kind":"memory","scope":"user","text":"owned fixture","writer":"model","source_engine":"codex"}}),
            );
            assert_eq!(apply_ops(&cfg, &[staged]).unwrap()["applied"], 1);
            let resolve_wrong = op(
                &cfg,
                &format!("wrong-{index}"),
                base + 1,
                "pending",
                "resolve",
                None,
                json!({"uid":format!("{uid}-other"),"status":"rejected"}),
            );
            assert_eq!(apply_ops(&cfg, &[resolve_wrong]).unwrap()["applied"], 1);
            assert_eq!(proposal_files(&cfg, false).unwrap().len(), 1);
            let resolved = op(
                &cfg,
                &format!("resolve-{index}"),
                base + 2,
                "pending",
                "resolve",
                None,
                json!({"uid":uid,"status":"rejected"}),
            );
            assert_eq!(apply_ops(&cfg, &[resolved.clone()]).unwrap()["applied"], 1);
            assert_eq!(apply_ops(&cfg, &[resolved]).unwrap()["duplicate"], 1);
            let repeated = op(
                &cfg,
                &format!("again-{index}"),
                base + 3,
                "pending",
                "resolve",
                None,
                json!({"uid":uid,"status":"rejected"}),
            );
            assert_eq!(apply_ops(&cfg, &[repeated]).unwrap()["applied"], 1);
            assert!(proposal_files(&cfg, false).unwrap().is_empty());
        }
        assert_eq!(fs::read_to_string(outside).unwrap(), "owned outside sentinel");
        assert!(!cfg.root.join("USER.md").exists());
        let archived = proposal_files(&cfg, true).unwrap();
        assert_eq!(archived.len(), 3);
        for file in archived {
            assert!(file.starts_with(cfg.root.join("pending/archive")));
            assert!(config::valid_id(file.file_stem().unwrap().to_str().unwrap()));
        }
    }
    #[test]
    fn portable_pending_uid_bounds_and_controls_fail_closed() {
        for (index, uid) in [
            "".to_owned(),
            "x".repeat(129),
            "x\0y".to_owned(),
            "x\ny".to_owned(),
            "x\u{0085}y".to_owned(),
        ]
        .into_iter()
        .enumerate()
        {
            let (_temp, cfg) = fixture();
            for (offset, verb) in ["stage", "resolve"].into_iter().enumerate() {
                let incoming = op(
                    &cfg,
                    &format!("bad-{index}-{offset}"),
                    offset as i64 + 1,
                    "pending",
                    verb,
                    None,
                    json!({"uid":uid,"status":"rejected","item":{"kind":"memory","scope":"user","text":"owned fixture"}}),
                );
                assert_eq!(apply_ops(&cfg, &[incoming]).unwrap()["failed"], 1);
                assert!(proposal_files(&cfg, false).unwrap().is_empty());
            }
        }
    }


    #[test]
    fn human_approval_refuses_changed_identity_model_and_missing_dependency() {
        let (_temp, cfg) = fixture();
        let mut incoming = op(
            &cfg,
            "unverified-edge",
            1,
            "belief",
            "edge",
            None,
            json!({"src_uid":"missing-a","dst_uid":"missing-b","rel":"explains","source":"derived"}),
        );
        incoming["mac"] = Value::Null;
        apply_ops(&cfg, &[incoming.clone()]).unwrap();
        let human = Authority::HumanReview {
            agent: "fixture".into(),
            engine: "codex".into(),
        };
        let model = Authority::Model {
            agent: "fixture".into(),
            engine: "codex".into(),
            session_id: "owned".into(),
        };
        assert_eq!(approve(&cfg, &incoming, &model), Err(Error::Untrusted));
        assert!(approve(&cfg, &incoming, &human).is_err());
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(state(&conn, &incoming).unwrap(), Some(APPLIED_UNVERIFIED));
        assert_eq!(
            conn.query_row("SELECT count(*) FROM sync_machine", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        drop(conn);
        let mut changed = incoming.clone();
        changed["machine_id"] = json!("wrong-author");
        assert_eq!(approve(&cfg, &changed, &human), Err(Error::Changed));
    }
    #[test]
    fn python_mac_golden_tampering_and_bounds_match_signed_protocol() {
        let (_temp, cfg) = fixture();
        let mut golden = json!({"op_id":"golden","machine_id":"fixture-author","machine_seq":1,"lamport":1,"class":"belief","op":"insert","project_key":null,"payload":{"uid":"golden-uid","subject":"user","claim":"Straße Σ","confidence":0.1},"mac":"d8af060153d58c05a88788b536ef40c8453c66dbf099b5fa3cfc902cc212ff0b"});
        assert!(verify_mac(&golden, cfg.sync.key.as_deref()));
        golden["mac"] = json!(golden["mac"].as_str().unwrap().to_uppercase());
        assert!(!verify_mac(&golden, cfg.sync.key.as_deref()));
        golden["machine_seq"] = json!(true);
        assert!(validate_envelope(&golden).is_err());
        golden["machine_seq"] = json!(1);
        golden["payload"] = json!({"text":"x".repeat(crate::MAX_FRAME_BYTES)});
        assert!(validate_envelope(&golden).is_err());
    }
    #[test]
    fn overcap_is_staged_and_partial_file_failure_never_claims_approval_success() {
        let (_temp, mut cfg) = fixture();
        cfg.user_cap = 2;
        let incoming = op(
            &cfg,
            "overcap",
            1,
            "memory",
            "add",
            None,
            json!({"text":"long owned fixture","writer":"derived","via":"derived","source_engine":"codex"}),
        );
        let report = apply_ops(&cfg, &[incoming]).unwrap();
        assert_eq!(report["staged"], 1);
        assert!(!cfg.root.join("USER.md").exists());
        let row: Value = serde_json::from_slice(
            &files::read_regular(
                &proposal_files(&cfg, false).unwrap()[0],
                crate::MAX_FRAME_BYTES,
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(row["origin"], "sync-overflow");
        assert_eq!(row["source_engine"], "codex");
        let (_other, broken) = fixture();
        files::private_dir(&broken.root).unwrap();
        files::atomic_write(&broken.root.join("provenance.json"), b"{broken fixture").unwrap();
        let signed = op(
            &broken,
            "partial",
            1,
            "memory",
            "add",
            None,
            json!({"text":"landed owned fixture","source_engine":"codex"}),
        );
        let report = apply_ops(&broken, &[signed.clone()]).unwrap();
        assert_eq!(report["failed"], 1);
        assert_eq!(report["may_have_applied"], 1);
        assert_eq!(
            memory::read_entries(&broken.root.join("USER.md")).unwrap(),
            ["landed owned fixture"]
        );
        let conn = store::connect(&broken).unwrap();
        assert_eq!(state(&conn, &signed).unwrap(), Some(APPLIED_FAILED));
        drop(conn);
        let (_third, approval) = fixture();
        files::private_dir(&approval.root).unwrap();
        files::atomic_write(&approval.root.join("provenance.json"), b"{broken fixture").unwrap();
        let mut unsigned = op(
            &approval,
            "approved-partial",
            1,
            "memory",
            "add",
            None,
            json!({"text":"approved uncertain fixture","source_engine":"codex"}),
        );
        unsigned["mac"] = Value::Null;
        apply_ops(&approval, &[unsigned.clone()]).unwrap();
        assert_eq!(
            approve(
                &approval,
                &unsigned,
                &Authority::HumanReview {
                    agent: "fixture".into(),
                    engine: "codex".into()
                }
            ),
            Err(Error::MayHaveApplied)
        );
        assert_eq!(
            memory::read_entries(&approval.root.join("USER.md")).unwrap(),
            ["approved uncertain fixture"]
        );
    }

    #[test]
    fn uncertain_signed_effect_retries_only_its_original_proof() {
        let (_temp, cfg) = fixture();
        files::private_dir(&cfg.root).unwrap();
        files::atomic_write(&cfg.root.join("provenance.json"), b"{broken fixture").unwrap();
        let original = op(
            &cfg,
            "retry-partial",
            1,
            "memory",
            "add",
            None,
            json!({"text":"partially landed fixture","source_engine":"codex"}),
        );
        let first = apply_ops(&cfg, &[original.clone()]).unwrap();
        assert_eq!(first["failed"], 1);
        assert_eq!(first["may_have_applied"], 1);
        let mut changed = original.clone();
        changed["payload"]["text"] = json!("replacement fixture");
        changed["mac"] = json!(compute_mac(&changed, cfg.sync.key.as_deref().unwrap()).unwrap());
        let rejected = apply_ops(&cfg, &[changed]).unwrap();
        assert_eq!(rejected["failed"], 1);
        assert_eq!(rejected["duplicate"], 0);
        let mut unsigned = original.clone();
        unsigned["mac"] = Value::Null;
        let unverified = apply_ops(&cfg, &[unsigned]).unwrap();
        assert_eq!(unverified["failed"], 1);
        assert_eq!(unverified["duplicate"], 0);
        assert_eq!(
            memory::read_entries(&cfg.root.join("USER.md")).unwrap(),
            ["partially landed fixture"]
        );
        files::atomic_write(&cfg.root.join("provenance.json"), b"{}").unwrap();
        let retried = apply_ops(&cfg, &[original.clone()]).unwrap();
        assert_eq!(retried["applied"], 1);
        assert_eq!(retried["duplicate"], 0);
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(state(&conn, &original).unwrap(), Some(APPLIED_YES));
        assert_eq!(
            memory::read_entries(&cfg.root.join("USER.md")).unwrap(),
            ["partially landed fixture"]
        );
    }

    #[test]
    fn project_keys_translate_and_conflicts_keep_same_path_alternatives() {
        let (_temp, cfg) = fixture();
        let conn = store::connect(&cfg).unwrap();
        conn.execute("INSERT INTO sync_projects(project_key,slug,created) VALUES('repo-a','receiver-a','fixture'),('repo-b','receiver-b','fixture')", []).unwrap();
        drop(conn);
        let original = "src.rs — shared";
        let key = gate::entry_key("filemap", "sender-checkout", original);
        let seed = op(
            &cfg,
            "seed-map",
            1,
            "filemap",
            "add",
            Some("repo-a"),
            json!({"text":original}),
        );
        let first = op(
            &cfg,
            "first-map",
            2,
            "filemap",
            "replace",
            Some("repo-a"),
            json!({"old_key":key,"text":"src.rs — first"}),
        );
        let mut second = op(
            &cfg,
            "second-map",
            3,
            "filemap",
            "replace",
            Some("repo-a"),
            json!({"old_key":key,"text":"src.rs — second"}),
        );
        second["machine_id"] = json!("other-author");
        let raw = store::canonical_bytes(&tuple(&second)).unwrap();
        let mut mac =
            Hmac::<sha2::Sha256>::new_from_slice(cfg.sync.key.as_ref().unwrap().as_bytes())
                .unwrap();
        mac.update(&raw);
        second["mac"] = json!(mac
            .finalize()
            .into_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>());
        assert_eq!(
            apply_ops(&cfg, &[seed, first, second]).unwrap()["applied"],
            3
        );
        let entries = memory::read_entries(&filemap::path(&cfg, "receiver-a").unwrap()).unwrap();
        assert!(
            entries.contains(&"src.rs — first".into())
                && entries.contains(&"src.rs — second".into())
        );
        let other = op(
            &cfg,
            "other-project",
            4,
            "filemap",
            "replace",
            Some("repo-b"),
            json!({"old_key":key,"text":"src.rs — isolated"}),
        );
        assert_eq!(apply_ops(&cfg, &[other]).unwrap()["applied"], 1);
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(
            conn.query_row("SELECT count(*) FROM sync_conflicts", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            1
        );
        drop(conn);
        let mut identical = op(
            &cfg,
            "identical-map",
            5,
            "filemap",
            "replace",
            Some("repo-a"),
            json!({"old_key":key,"text":"src.rs — first"}),
        );
        identical["machine_id"] = json!("third-author");
        identical["mac"] =
            json!(
                store::canonical_mac(&tuple(&identical), cfg.sync.key.as_deref().unwrap()).unwrap()
            );
        assert_eq!(apply_ops(&cfg, &[identical]).unwrap()["applied"], 1);
        assert_eq!(
            memory::read_entries(&filemap::path(&cfg, "receiver-a").unwrap()).unwrap(),
            ["src.rs — first", "src.rs — second"]
        );
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(
            conn.query_row("SELECT count(*) FROM sync_conflicts", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            1
        );
        drop(conn);
        let remove = op(
            &cfg,
            "remove-alternative",
            6,
            "filemap",
            "remove",
            Some("repo-a"),
            json!({"key":gate::entry_key("filemap","sender-checkout","src.rs — first")}),
        );
        assert_eq!(apply_ops(&cfg, &[remove]).unwrap()["applied"], 1);
        assert_eq!(
            memory::read_entries(&filemap::path(&cfg, "receiver-a").unwrap()).unwrap(),
            ["src.rs — second"]
        );
    }

    #[test]
    fn portable_entry_key_translation_preserves_scope_boundaries() {
        for bad in [
            "filemap::0123456789abcdefabcd",
            "memory:user:0123456789abcdefabcD",
            "memory:user:0123456789abcdefabc",
            "belief:user:0123456789abcdefabcd",
        ] {
            assert!(portable_key(bad, "filemap", "receiver").is_err());
        }
        let (_temp, cfg) = fixture();
        let seed = op(
            &cfg,
            "seed-user",
            1,
            "memory",
            "add",
            None,
            json!({"text":"same visible fact"}),
        );
        apply_ops(&cfg, &[seed]).unwrap();
        let wrong = op(
            &cfg,
            "wrong-scope",
            2,
            "memory",
            "remove",
            None,
            json!({"key":gate::entry_key("memory","machine:other-host","same visible fact")}),
        );
        assert_eq!(apply_ops(&cfg, &[wrong]).unwrap()["failed"], 1);
        assert_eq!(
            memory::read_entries(&cfg.root.join("USER.md")).unwrap(),
            ["same visible fact"]
        );
    }

    #[test]
    fn signed_missing_belief_references_are_quarantined_without_rewriting_or_effects() {
        let (_temp, cfg) = fixture();
        let cases = [
            ("reinforce", json!({"uid":null,"confidence":0.9})),
            ("supersede", json!({"uid":"unknown","by_uid":null})),
            ("retract", json!({"uid":null})),
            ("status", json!({"status":"active"})),
            ("edge", json!({"src_uid":null,"dst_uid":"unknown","rel":"depends_on"})),
            ("outcome", json!({"belief_uid":null,"uid":"optional-outcome-id"})),
            ("dream_reviewed", json!({"a_uid":"unknown","b_uid":null})),
        ];
        let ops = cases.iter().enumerate().map(|(i, (verb, payload))| {
            op(&cfg, &format!("quarantine-{i}"), i as i64 + 1, "belief", verb, None, payload.clone())
        }).collect::<Vec<_>>();
        let report = apply_ops(&cfg, &ops).unwrap();
        assert_eq!(report["quarantined"], cases.len());
        assert_eq!(report["applied"], 0);
        assert_eq!(report["deferred"], 0);
        assert_eq!(retry_deferred(&cfg).unwrap(), 0);
        let conn = store::connect(&cfg).unwrap();
        for original in &ops {
            assert_eq!(state(&conn, original).unwrap(), Some(APPLIED_QUARANTINED));
            let stored = imported(&conn).into_iter().find(|row| row["op_id"] == original["op_id"]).unwrap();
            assert_eq!(stored["payload"], original["payload"]);
            assert_eq!(stored["mac"], original["mac"]);
        }
        assert_eq!(conn.query_row("SELECT count(*) FROM beliefs", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
        drop(conn);
        assert_eq!(apply_ops(&cfg, &ops).unwrap()["duplicate"], cases.len());
        let path = _temp.path().join("quarantined-export.json");
        assert_eq!(crate::sync_network::export_bundle(&cfg, &path).unwrap()["count"], cases.len());
        let bundle: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        for original in &ops {
            assert!(bundle["ops"].as_array().unwrap().iter().any(|row| row["op_id"] == original["op_id"] && row["mac"] == original["mac"]));
        }
    }

    #[test]
    fn retry_migrates_legacy_null_refs_but_unknown_dependencies_stay_deferred() {
        let (_temp, cfg) = fixture();
        let legacy = op(&cfg, "legacy-null", 1, "belief", "edge", None,
            json!({"src_uid":"known-later","dst_uid":null,"rel":"depends_on"}));
        let unknown = op(&cfg, "unknown-ref", 2, "belief", "reinforce", None,
            json!({"uid":"known-later","confidence":0.8}));
        let conn = store::connect(&cfg).unwrap();
        assert!(record(&conn, &legacy, APPLIED_NO).unwrap());
        drop(conn);
        assert_eq!(retry_deferred(&cfg).unwrap(), 0);
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(state(&conn, &legacy).unwrap(), Some(APPLIED_QUARANTINED));
        drop(conn);
        assert_eq!(apply_ops(&cfg, &[unknown.clone()]).unwrap()["deferred"], 1);
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(state(&conn, &unknown).unwrap(), Some(APPLIED_NO));
        drop(conn);
        let mut forged = op(&cfg, "forged-null", 3, "belief", "retract", None, json!({"uid":null}));
        forged["mac"] = json!("0".repeat(64));
        assert_eq!(apply_ops(&cfg, &[forged.clone()]).unwrap()["unverified"], 1);
        let conn = store::connect(&cfg).unwrap();
        assert_eq!(state(&conn, &forged).unwrap(), Some(APPLIED_UNVERIFIED));
    }
}
