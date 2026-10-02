//! Native op-log administration. Dry runs retain identity and every mutation is
//! backed up through SQLite before one bounded, all-or-nothing transaction.
use crate::{
    config::{self, Config},
    files, gate,
    memory::{self, Scope},
    store, sync_apply, Error, Result,
};
use rusqlite::{params, Connection, OpenFlags, TransactionBehavior};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsStr,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
const CAP: usize = 100_000;
fn enabled(cfg: &Config, class: &str) -> bool {
    cfg.sync.enabled
        && cfg.sync.classes.contains(match class {
            "belief" => "beliefs",
            "skill" => "skills",
            s => s,
        })
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
fn ops(conn: &Connection) -> Result<Vec<Value>> {
    let mut stmt=conn.prepare("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created,applied FROM sync_ops ORDER BY lamport,machine_id,machine_seq LIMIT 100001")?;
    let mut rows = stmt.query([])?;
    let mut out = Vec::new();
    let mut bytes = 0;
    while let Some(row) = rows.next()? {
        if out.len() >= CAP {
            return Err(Error::TooLarge);
        }
        let payload: String = row.get(7)?;
        bytes += payload.len();
        if bytes > 128 * 1024 * 1024 {
            return Err(Error::TooLarge);
        }
        out.push(json!({"op_id":row.get::<_,String>(0)?,"machine_id":row.get::<_,String>(1)?,"machine_seq":row.get::<_,i64>(2)?,"lamport":row.get::<_,i64>(3)?,"class":row.get::<_,String>(4)?,"op":row.get::<_,String>(5)?,"project_key":row.get::<_,Option<String>>(6)?,"payload":serde_json::from_str::<Value>(&payload).unwrap_or(Value::Null),"mac":row.get::<_,Option<String>>(8)?,"created":row.get::<_,String>(9)?,"applied":row.get::<_,i64>(10)?}));
    }
    Ok(out)
}
pub fn backup(cfg: &Config, source: &Connection) -> Result<PathBuf> {
    let pages = source.query_row("PRAGMA page_count", [], |r| r.get::<_, i64>(0))?;
    let size = source.query_row("PRAGMA page_size", [], |r| r.get::<_, i64>(0))?;
    if pages
        .checked_mul(size)
        .is_none_or(|n| n > 4 * 1024 * 1024 * 1024)
    {
        return Err(Error::TooLarge);
    }
    let dir = files::open_directory(&cfg.root)?;
    let name = format!("state.db.bak-{}", uuid::Uuid::new_v4());
    let file = files::create_private_file(&dir, OsStr::new(&name), true)?;
    let path = cfg.root.join(&name);
    let mut dest = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    let backup = rusqlite::backup::Backup::new(source, &mut dest)?;
    let start = Instant::now();
    loop {
        match backup.step(128)? {
            rusqlite::backup::StepResult::Done => break,
            rusqlite::backup::StepResult::More => {}
            _ => std::thread::sleep(Duration::from_millis(10)),
        }
        if start.elapsed() > Duration::from_secs(120) {
            return Err(Error::Timeout);
        }
    }
    drop(backup);
    drop(dest);
    file.sync_all()?;
    dir.sync_all()?;
    Ok(path)
}
pub fn resign(cfg: &Config, apply: bool, replace: bool) -> Result<Value> {
    let key = cfg.sync.key.as_deref().ok_or(Error::Untrusted)?;
    let mut conn = store::connect(cfg)?;
    let machine = store::machine_id(&conn)?;
    let rows = ops(&conn)?;
    let (mut unsigned, mut current, mut other, mut malformed, mut foreign, mut foreign_unsigned) =
        (0, 0, 0, 0, 0, 0);
    let mut plan = Vec::new();
    for op in rows {
        if op["machine_id"] != machine {
            foreign += 1;
            if op["mac"].is_null() || op["mac"] == "" {
                foreign_unsigned += 1
            }
            continue;
        }
        if sync_apply::validate_envelope(&op).is_err()
            || !op["machine_seq"].as_i64().is_some_and(|n| n > 0)
            || !op["lamport"].as_i64().is_some_and(|n| n > 0)
        {
            malformed += 1;
            continue;
        }
        if op["mac"].is_null() || op["mac"] == "" {
            unsigned += 1;
            plan.push(op)
        } else if sync_apply::verify_mac(&op, Some(key)) {
            current += 1
        } else {
            other += 1;
            if replace {
                plan.push(op)
            }
        }
    }
    let saved = if apply && !plan.is_empty() {
        Some(backup(cfg, &conn)?)
    } else {
        None
    };
    if apply && !plan.is_empty() {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        for op in &plan {
            let current = tx.query_row("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac FROM sync_ops WHERE op_id=?", [op["op_id"].as_str().ok_or(Error::InvalidRequest)?], |row| {
                let payload: String = row.get(7)?;
                Ok(json!({"op_id":row.get::<_,String>(0)?,"machine_id":row.get::<_,String>(1)?,"machine_seq":row.get::<_,i64>(2)?,"lamport":row.get::<_,i64>(3)?,"class":row.get::<_,String>(4)?,"op":row.get::<_,String>(5)?,"project_key":row.get::<_,Option<String>>(6)?,"payload":serde_json::from_str::<Value>(&payload).unwrap_or(Value::Null),"mac":row.get::<_,Option<String>>(8)?}))
            })?;
            if tuple(&current) != tuple(op) || current["mac"] != op["mac"] {
                return Err(Error::Changed);
            }
            let signature = sync_apply::compute_mac(op, key)?;
            if tx.execute(
                "UPDATE sync_ops SET mac=? WHERE op_id=? AND machine_id=?",
                params![signature, op["op_id"].as_str(), machine],
            )? != 1
            {
                return Err(Error::Changed);
            }
        }
        tx.commit()?;
    }
    Ok(
        json!({"machine_id":machine,"own_total":unsigned+current+other+malformed,"unsigned":unsigned,"signed_current":current,"signed_other":other,"malformed":malformed,"foreign_total":foreign,"foreign_unsigned":foreign_unsigned,"replace_foreign_key":replace,"to_resign":plan.len(),"resigned":if apply{plan.len()}else{0},"applied":apply,"backup":saved}),
    )
}
#[derive(Clone)]
struct Descriptor {
    class: String,
    verb: String,
    key: Option<String>,
    payload: Value,
}
fn append(
    plan: &mut Vec<Descriptor>,
    class: &str,
    verb: &str,
    key: Option<String>,
    payload: Value,
) -> Result<()> {
    if plan.len() >= CAP {
        return Err(Error::TooLarge);
    }
    plan.push(Descriptor {
        class: class.into(),
        verb: verb.into(),
        key,
        payload,
    });
    Ok(())
}
fn replay(log: &[Value], class: &str, key: Option<&str>, bucket: &str) -> Vec<String> {
    let mut entries = Vec::<String>::new();
    for op in log.iter().filter(|o| {
        o["applied"] == 1
            && o["class"] == class
            && o["project_key"].as_str() == key
            && o["payload"].is_object()
    }) {
        let p = &op["payload"];
        let text = p["text"].as_str().unwrap_or("");
        match op["op"].as_str() {
            Some("add") => {
                if !text.is_empty()
                    && !entries
                        .iter()
                        .any(|e| e.to_lowercase() == text.to_lowercase())
                {
                    entries.push(text.into())
                }
            }
            Some("remove") => {
                if let Some(key) = p["key"].as_str()
                    .and_then(|key| sync_apply::portable_key(key, class, bucket).ok())
                {
                    entries.retain(|e| gate::entry_key(class, bucket, e) != key);
                }
            }
            Some("replace") => {
                let old_key = p["old_key"].as_str()
                    .and_then(|key| sync_apply::portable_key(key, class, bucket).ok());
                if let Some(e) = entries
                    .iter_mut()
                    .find(|e| old_key.as_ref().is_some_and(|key| gate::entry_key(class, bucket, e) == key.as_str()))
                {
                    *e = text.into()
                } else if !text.is_empty()
                    && !entries
                        .iter()
                        .any(|e| e.to_lowercase() == text.to_lowercase())
                {
                    entries.push(text.into())
                }
            }
            _ => {}
        }
    }
    entries
}
fn file_plan(
    cfg: &Config,
    conn: &Connection,
    log: &[Value],
    plan: &mut Vec<Descriptor>,
    class: &str,
    key: Option<String>,
    bucket: &str,
    path: &Path,
) -> Result<()> {
    let represented = replay(log, class, key.as_deref(), bucket);
    for entry in memory::read_entries(path)? {
        let scrubbed = crate::scrub::scrub(&entry)?;
        if represented
            .iter()
            .any(|e| e.to_lowercase() == scrubbed.to_lowercase())
        {
            continue;
        }
        let p = gate::provenance(cfg, class, bucket, &entry);
        let mut payload = json!({"text":entry,"via":p["via"].as_str().filter(|s|!s.is_empty()).unwrap_or("sync-seed"),"writer":p["writer"].as_str().filter(|s|!s.is_empty()).unwrap_or("sync-seed")});
        if class == "memory" {
            payload["source_engine"] = json!(p["source_engine"].as_str().unwrap_or("unknown"))
        }
        append(plan, class, "add", key.clone(), payload)?;
    }
    let _ = conn;
    Ok(())
}
fn names(dir: &Path) -> Result<Vec<std::ffi::OsString>> {
    if !dir.try_exists()? {
        return Ok(Vec::new());
    }
    let mut names = files::directory_names(dir, 10000)?;
    names.sort();
    Ok(names)
}
pub fn seed(cfg: &Config, apply: bool) -> Result<Value> {
    if cfg.sync.key.is_none() {
        return Err(Error::Untrusted);
    }
    let mut conn = store::connect(cfg)?;
    let machine = store::machine_id(&conn)?;
    let log = ops(&conn)?;
    let mut plan = Vec::new();
    if enabled(cfg, "memory") {
        file_plan(
            cfg,
            &conn,
            &log,
            &mut plan,
            "memory",
            None,
            "user",
            &Scope::User.path(cfg, "user")?,
        )?;
        let dir = cfg.root.join("projects");
        for name in names(&dir)? {
            let slug = name
                .to_str()
                .filter(|s| config::valid_slug(s))
                .ok_or(Error::UnsafePath)?;
            let path = Scope::Project.path(cfg, slug)?;
            if !path.try_exists()? {
                continue;
            }
            file_plan(
                cfg,
                &conn,
                &log,
                &mut plan,
                "memory",
                Some(store::project_key_for_slug(&conn, slug)?),
                &format!("project:{slug}"),
                &path,
            )?
        }
    }
    if enabled(cfg, "filemap") {
        let dir = cfg.root.join("filemap");
        for name in names(&dir)? {
            let Some(slug) = name
                .to_str()
                .and_then(|s| s.strip_suffix(".md"))
                .filter(|s| config::valid_slug(s))
            else {
                continue;
            };
            file_plan(
                cfg,
                &conn,
                &log,
                &mut plan,
                "filemap",
                Some(store::project_key_for_slug(&conn, slug)?),
                slug,
                &dir.join(&name),
            )?
        }
    }
    if enabled(cfg, "belief") {
        belief_plan(&conn, &log, &mut plan)?
    }
    if enabled(cfg, "skill") {
        let dir = &cfg.skills;
        for name in names(dir)? {
            let Some(name) = name.to_str().filter(|s| config::valid_skill_name(s)) else {
                continue;
            };
            let path = dir.join(name).join("SKILL.md");
            if !path.try_exists()? {
                continue;
            }
            let body = String::from_utf8(files::read_regular(&path, crate::MAX_FRAME_BYTES)?)
                .map_err(|_| Error::InvalidRequest)?;
            let mut represented = None;
            for op in log.iter().filter(|o| {
                o["applied"] == 1 && o["class"] == "skill" && o["payload"]["name"] == name
            }) {
                match op["op"].as_str() {
                    Some("put") => represented = op["payload"]["body"].as_str().map(str::to_owned),
                    Some("remove") => represented = None,
                    _ => {}
                }
            }
            if represented != Some(crate::scrub::scrub(&body)?) {
                append(
                    &mut plan,
                    "skill",
                    "put",
                    None,
                    json!({"name":name,"body":body}),
                )?
            }
        }
    }
    let mut classes = json!({});
    for class in ["memory", "filemap", "belief", "skill"] {
        let n = plan.iter().filter(|p| p.class == class).count();
        classes[class] =
            json!({"enabled":enabled(cfg,class),"candidates":n,"seeded":if apply{n}else{0}})
    }
    let saved = if apply && !plan.is_empty() {
        Some(backup(cfg, &conn)?)
    } else {
        None
    };
    if apply && !plan.is_empty() {
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        for row in &plan {
            store::append_op(
                cfg,
                &tx,
                &row.class,
                &row.verb,
                row.key.as_deref(),
                &row.payload,
            )?
        }
        tx.commit()?;
    }
    Ok(
        json!({"machine_id":machine,"classes":classes,"total_candidates":plan.len(),"total_seeded":if apply{plan.len()}else{0},"applied":apply,"backup":saved}),
    )
}
fn belief_plan(conn: &Connection, log: &[Value], plan: &mut Vec<Descriptor>) -> Result<()> {
    let mut coverage = BTreeSet::<(String, String, String)>::new();
    let mut edges = BTreeSet::<(String, String, String)>::new();
    for op in log
        .iter()
        .filter(|o| o["applied"] == 1 && o["class"] == "belief" && o["payload"].is_object())
    {
        let p = &op["payload"];
        let verb = op["op"].as_str().unwrap_or("");
        if let Some(uid) = p["uid"].as_str() {
            if verb != "supersede" || p["by_uid"].as_str().is_some() {
                coverage.insert((
                    verb.into(),
                    uid.into(),
                    p["status"].as_str().unwrap_or("").into(),
                ));
            }
        }
        if verb == "edge" {
            if let (Some(src), Some(dst), Some(rel)) = (
                p["src_uid"].as_str(),
                p["dst_uid"].as_str(),
                p["rel"].as_str(),
            ) {
                edges.insert((src.into(), dst.into(), rel.into()));
            }
        }
    }
    let mut stmt=conn.prepare("SELECT id,uid,subject,claim,confidence,status,superseded_by,resolution,created,via,writer,source_engine FROM beliefs ORDER BY id LIMIT 100001")?;
    let mut rows = stmt.query([])?;
    let mut beliefs = BTreeMap::new();
    while let Some(r) = rows.next()? {
        if beliefs.len() >= CAP {
            return Err(Error::TooLarge);
        }
        let v = json!({"id":r.get::<_,i64>(0)?,"uid":r.get::<_,Option<String>>(1)?,"subject":r.get::<_,String>(2)?,"claim":r.get::<_,String>(3)?,"confidence":r.get::<_,f64>(4)?,"status":r.get::<_,String>(5)?,"superseded_by":r.get::<_,Option<i64>>(6)?,"resolution":r.get::<_,Option<String>>(7)?,"created":r.get::<_,Option<String>>(8)?,"via":r.get::<_,Option<String>>(9)?,"writer":r.get::<_,Option<String>>(10)?,"source_engine":r.get::<_,Option<String>>(11)?});
        beliefs.insert(v["id"].as_i64().unwrap(), v);
    }
    let key_for = |subject: &str| -> Result<Option<String>> {
        subject
            .strip_prefix("project:")
            .map(|s| store::project_key_for_slug(conn, s))
            .transpose()
    };
    for (bid, row) in &beliefs {
        let Some(uid) = row["uid"].as_str().filter(|s| !s.is_empty()) else {
            continue;
        };
        if !coverage.contains(&("insert".into(), uid.into(), String::new())) {
            let evidence=conn.query_row("SELECT session_id,project,note,source_engine FROM belief_evidence WHERE belief_id=? ORDER BY created LIMIT 1",[bid],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?,r.get::<_,Option<String>>(3)?))).optional()?;
            let ev = if let Some((sid, project, note, engine)) = evidence {
                json!({"session_id":sid,"project_key":project.map(|p|store::project_key_for_slug(conn,&p)).transpose()?,"note":note,"source_engine":engine.unwrap_or_else(||"unknown".into())})
            } else {
                json!({"session_id":null,"project_key":null,"note":null,"source_engine":"unknown"})
            };
            append(
                plan,
                "belief",
                "insert",
                key_for(row["subject"].as_str().ok_or(Error::InvalidRequest)?)?,
                json!({"uid":uid,"subject":row["subject"],"claim":row["claim"],"confidence":row["confidence"],"via":row["via"].as_str().unwrap_or("sync-seed"),"writer":row["writer"].as_str().unwrap_or("sync-seed"),"source_engine":row["source_engine"].as_str().unwrap_or("unknown"),"created":row["created"],"evidence":ev}),
            )?
        }
    }
    for row in beliefs.values() {
        let Some(uid) = row["uid"].as_str() else {
            continue;
        };
        let key = key_for(row["subject"].as_str().ok_or(Error::InvalidRequest)?)?;
        match row["status"].as_str() {
            Some("retracted")
                if !coverage.contains(&("retract".into(), uid.into(), String::new())) =>
            {
                append(plan, "belief", "retract", key, json!({"uid":uid}))?
            }
            Some("dormant")
                if !coverage.contains(&("status".into(), uid.into(), "dormant".into())) =>
            {
                append(
                    plan,
                    "belief",
                    "status",
                    key,
                    json!({"uid":uid,"status":"dormant"}),
                )?
            }
            Some("superseded")
                if !coverage.contains(&("supersede".into(), uid.into(), String::new())) =>
            {
                if let Some(by) = row["superseded_by"]
                    .as_i64()
                    .and_then(|id| beliefs.get(&id))
                    .and_then(|r| r["uid"].as_str())
                {
                    append(
                        plan,
                        "belief",
                        "supersede",
                        key,
                        json!({"uid":uid,"by_uid":by,"reason":row["resolution"].as_str().unwrap_or("")}),
                    )?
                }
            }
            _ => {}
        }
    }
    let mut stmt=conn.prepare("SELECT src,dst,rel,source,session_id,note FROM belief_edges ORDER BY src,dst,rel LIMIT 100001")?;
    let mut rows = stmt.query([])?;
    let mut count = 0;
    while let Some(r) = rows.next()? {
        count += 1;
        if count > CAP {
            return Err(Error::TooLarge);
        }
        let src = r.get::<_, i64>(0)?;
        let dst = r.get::<_, i64>(1)?;
        let rel = r.get::<_, String>(2)?;
        let (Some(a), Some(b)) = (beliefs.get(&src), beliefs.get(&dst)) else {
            continue;
        };
        let (Some(uid_a), Some(uid_b)) = (a["uid"].as_str(), b["uid"].as_str()) else {
            continue;
        };
        if edges.contains(&(uid_a.into(), uid_b.into(), rel.clone())) {
            continue;
        }
        append(
            plan,
            "belief",
            "edge",
            key_for(a["subject"].as_str().ok_or(Error::InvalidRequest)?)?,
            json!({"src_uid":uid_a,"dst_uid":uid_b,"rel":rel,"source":r.get::<_,String>(3)?,"session_id":r.get::<_,Option<String>>(4)?,"note":r.get::<_,Option<String>>(5)?}),
        )?;
    }
    Ok(())
}
use rusqlite::OptionalExtension;

#[cfg(test)]
mod tests {
    use super::replay;
    use crate::gate;
    use serde_json::json;

    #[test]
    fn seed_replay_translates_foreign_project_keys() {
        let author_key = gate::entry_key("memory", "project:author", "Historical fact");
        let log = vec![
            json!({"applied":1,"class":"memory","project_key":"shared","op":"add","payload":{"text":"Historical fact"}}),
            json!({"applied":1,"class":"memory","project_key":"shared","op":"remove","payload":{"key":author_key}}),
        ];
        assert!(replay(&log, "memory", Some("shared"), "project:receiver").is_empty());
    }
}
