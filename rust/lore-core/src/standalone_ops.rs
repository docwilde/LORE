//! Native administration and human-readable command equivalents. Every mutation
//! uses canonical store/file operations; reports never manufacture trust.
use crate::{
    beliefs,
    config::{self, Config},
    files,
    gate::{self, Authority},
    memory::{self, Scope},
    store, Error, Result,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
fn cap(req: &Value, key: &str, default: u64, max: u64) -> Result<usize> {
    req.get(key)
        .map_or(Some(default), Value::as_u64)
        .filter(|n| *n <= max)
        .map(|n| n as usize)
        .ok_or(Error::InvalidRequest)
}
fn names(path: &Path) -> Result<Vec<std::ffi::OsString>> {
    if !path.try_exists()? {
        return Ok(Vec::new());
    }
    let mut n = files::directory_names(path, 10000)?;
    n.sort();
    Ok(n)
}
fn provider(
    cfg: &Config,
    req: &Value,
    prompt: &str,
    model_env: &str,
    default: &str,
) -> Result<String> {
    let _ = cfg;
    if let Some(path) = req["result_file"].as_str() {
        return String::from_utf8(files::read_regular(
            Path::new(path),
            crate::MAX_FRAME_BYTES,
        )?)
        .map_err(|_| Error::InvalidRequest);
    }
    let program = std::env::var("LORE_CLAUDE_BIN").unwrap_or_else(|_| "claude".into());
    crate::worker::review_provider(
        &program,
        model_env,
        default,
        prompt,
        Instant::now() + Duration::from_secs(150),
    )
}
fn prepare_review(cfg: &Config, req: &Value) -> Result<Option<Value>> {
    let mut req = req.clone();
    let cwd = gate::cwd(&req)?;
    let slug = config::project_slug(cwd);
    if req["latest"] == true && req.get("transcript").is_none() {
        let dir = cfg.projects.join(&slug);
        let mut latest = None;
        for name in names(&dir)? {
            if !name.to_str().is_some_and(|s| s.ends_with(".jsonl")) {
                continue;
            }
            let path = dir.join(name);
            let file = files::open_regular(&path, 256 * 1024 * 1024)?;
            let when = file.metadata()?.modified()?;
            if latest.as_ref().is_none_or(|(old, _)| *old < when) {
                latest = Some((when, path));
            }
        }
        if let Some((_, path)) = latest {
            req["transcript"] = json!(path)
        }
    }
    let Some(path) = req["transcript"].as_str() else {
        return Ok(None);
    };
    let path = Path::new(path);
    let archive = cfg
        .codex_sessions
        .parent()
        .map(|p| p.join("archived_sessions"));
    let codex =
        path.starts_with(&cfg.codex_sessions) || archive.is_some_and(|p| path.starts_with(p));
    let file = files::open_regular(path, 256 * 1024 * 1024)?;
    let (meta, _) = crate::index::parse_transcript_fd(&file, codex)?;
    if meta.internal {
        return Ok(None);
    }
    if req.get("session_id").is_none() {
        req["session_id"] = json!(if codex {
            meta.session_id.ok_or(Error::InvalidRequest)?
        } else {
            path.file_stem()
                .and_then(|s| s.to_str())
                .ok_or(Error::InvalidRequest)?
                .to_owned()
        })
    }
    if req.get("engine").is_none() {
        req["engine"] = json!(if codex { "codex" } else { "claude" })
    }
    Ok(Some(req))
}
fn review_one(cfg: &Config, req: &Value) -> Result<Value> {
    let engine = req["engine"].as_str().unwrap_or("claude");
    let auth = Authority::Derived {
        agent: req["agent"].as_str().unwrap_or("lore-review").into(),
        engine: engine.into(),
    };
    let Some(job) = crate::review::build_review_job(cfg, req, &auth)? else {
        return Ok(json!({"skipped":true}));
    };
    if req["dry_run"] == true {
        return Ok(json!({"prompt":job.prompt(),"session_id":job.session_id()}));
    }
    let response = provider(cfg, req, job.prompt(), "LORE_DERIVER_MODEL", "haiku")?;
    let result = crate::review::process_result(cfg, &job, &response)?;
    let conn = store::connect(cfg).map_err(|_| Error::MayHaveApplied)?;
    conn.execute(
        "INSERT OR REPLACE INTO reviewed(session_id,project,ts) VALUES(?,?,?)",
        params![
            job.session_id(),
            config::project_slug(gate::cwd(req)?),
            crate::utcnow()
        ],
    )
    .map_err(|_| Error::MayHaveApplied)?;
    Ok(result)
}
pub fn review(cfg: &Config, req: &Value) -> Result<Value> {
    let Some(req) = prepare_review(cfg, req)? else {
        return Ok(json!({"skipped":true,"reason":"no_transcript"}));
    };
    if req["full"] != true {
        return review_one(cfg, &req);
    }
    let path = Path::new(req["transcript"].as_str().ok_or(Error::InvalidRequest)?);
    let codex = path.starts_with(&cfg.codex_sessions);
    let file = files::open_regular(path, 256 * 1024 * 1024)?;
    let count = crate::review::parse_review_fd(&file, file.metadata()?.len(), codex)?.len();
    let window = std::env::var("LORE_DIGEST_LAST_N")
        .ok()
        .map_or(Some(500), |s| s.parse::<usize>().ok())
        .filter(|n| *n > 0 && *n <= 20000)
        .ok_or(Error::InvalidRequest)?;
    let mut results = Vec::new();
    let mut ranges = (0..count).step_by(window).enumerate().collect::<Vec<_>>();
    ranges.reverse();
    for (index, lo) in ranges {
        let hi = (lo + window).min(count);
        let mut part = req.clone();
        part["span"] = json!([lo, hi]);
        part["part"] = json!(format!("w{index:03}"));
        part["older"] = json!(hi < count);
        part["agent"] = json!(format!("backfill-w{index}"));
        results.push(review_one(cfg, &part).map_err(|error| {
            if results.is_empty() {
                error
            } else {
                Error::MayHaveApplied
            }
        })?);
    }
    Ok(json!({"messages":count,"windows":results}))
}
pub fn live_index(cfg: &Config, path: &str) -> Result<Value> {
    if std::env::var("LORE_DISABLE_INDEX").is_ok_and(|s| !matches!(s.as_str(), "" | "0")) {
        return Ok(json!({"disabled":true}));
    }
    let path = if path.is_empty() {
        let mut raw = Vec::new();
        std::io::stdin()
            .lock()
            .take(crate::MAX_FRAME_BYTES as u64 + 1)
            .read_to_end(&mut raw)?;
        if raw.len() > crate::MAX_FRAME_BYTES {
            return Err(Error::TooLarge);
        }
        let event: Value = serde_json::from_slice(&raw).map_err(|_| Error::InvalidRequest)?;
        PathBuf::from(
            event["transcript_path"]
                .as_str()
                .ok_or(Error::InvalidRequest)?,
        )
    } else {
        PathBuf::from(path)
    };
    if !path.starts_with(&cfg.projects) && !path.starts_with(&cfg.codex_sessions) {
        return Err(Error::UnsafePath);
    }
    let file = files::open_regular(&path, 256 * 1024 * 1024)?;
    let conn = store::connect(cfg)?;
    let (messages, lines) = crate::index::index_live_fd(cfg, &conn, &file, &path)?;
    Ok(json!({"messages":messages,"lines_consumed":lines}))
}
pub fn backfill(cfg: &Config, req: &Value) -> Result<Value> {
    let mut available = BTreeMap::new();
    for name in names(&cfg.projects)? {
        let slug = name
            .to_str()
            .filter(|s| config::valid_slug(s))
            .ok_or(Error::UnsafePath)?
            .to_owned();
        let dir = cfg.projects.join(&name);
        let mut sessions = Vec::new();
        for name in names(&dir)? {
            if name.to_str().is_some_and(|s| s.ends_with(".jsonl")) {
                sessions.push(dir.join(name));
            }
        }
        if !sessions.is_empty() {
            available.insert(slug, sessions);
        }
    }
    let terms = match req.get("project") {
        None => Vec::new(),
        Some(Value::String(s)) => vec![s.clone()],
        Some(Value::Array(a)) => a
            .iter()
            .map(|s| s.as_str().map(str::to_owned).ok_or(Error::InvalidRequest))
            .collect::<Result<Vec<_>>>()?,
        _ => return Err(Error::InvalidRequest),
    };
    if req["list"] == true || terms.is_empty() {
        return Ok(
            json!({"projects":available.iter().map(|(k,v)|(k.clone(),v.len())).collect::<BTreeMap<_,_>>()}),
        );
    }
    let mut chosen = BTreeSet::new();
    for term in terms {
        if available.contains_key(&term) {
            chosen.insert(term);
            continue;
        }
        let suffix = available
            .keys()
            .filter(|s| s.ends_with(&term))
            .cloned()
            .collect::<Vec<_>>();
        let matched = if suffix.len() == 1 {
            suffix
        } else {
            available
                .keys()
                .filter(|s| s.contains(&term))
                .cloned()
                .collect()
        };
        if matched.len() != 1 {
            return Err(Error::Changed);
        }
        chosen.insert(matched[0].clone());
    }
    let conn = store::connect(cfg)?;
    let mut done = BTreeSet::new();
    if req["force"] != true {
        let mut stmt = conn.prepare("SELECT session_id FROM reviewed LIMIT 100001")?;
        for row in stmt.query_map([], |r| r.get::<_, String>(0))? {
            if done.len() >= 100000 {
                return Err(Error::TooLarge);
            }
            done.insert(row?);
        }
    }
    let mut reviewed = 0;
    let mut skipped = 0;
    let mut failed = 0;
    let mut planned = 0;
    for slug in chosen {
        for path in &available[&slug] {
            let sid = path
                .file_stem()
                .and_then(|s| s.to_str())
                .ok_or(Error::InvalidRequest)?;
            if done.contains(sid) {
                skipped += 1;
                continue;
            }
            planned += 1;
            if req["dry_run"] == true {
                continue;
            }
            let file = files::open_regular(path, 256 * 1024 * 1024)?;
            let (meta, _) = crate::index::parse_transcript_fd(&file, false)?;
            let Some(cwd) = meta.cwd else {
                skipped += 1;
                continue;
            };
            if config::project_slug(Path::new(&cwd)) != slug || meta.internal {
                skipped += 1;
                continue;
            }
            let part = json!({"cwd":cwd,"transcript":path,"session_id":sid,"engine":"claude"});
            match review_one(cfg, &part) {
                Ok(result) if result["skipped"] != true => reviewed += 1,
                Ok(_) => skipped += 1,
                Err(_) => failed += 1,
            }
        }
    }
    Ok(
        json!({"planned":planned,"reviewed":reviewed,"skipped":skipped,"failed":failed,"dry_run":req["dry_run"]==true}),
    )
}
pub fn calibration(cfg: &Config) -> Result<Value> {
    let conn = store::connect(cfg)?;
    let total = conn.query_row("SELECT count(*) FROM belief_outcomes", [], |r| {
        r.get::<_, i64>(0)
    })?;
    let mut stmt=conn.prepare("SELECT round(b.confidence,1),count(DISTINCT b.id),count(o.id),coalesce(sum(o.event='confirmed'),0),coalesce(sum(o.event='contradicted'),0),coalesce(sum(o.event='stale'),0) FROM beliefs b LEFT JOIN belief_outcomes o ON o.belief_id=b.id GROUP BY round(b.confidence,1) ORDER BY round(b.confidence,1)")?;
    let rows=stmt.query_map([],|r|{let c=r.get::<_,i64>(3)?;let x=r.get::<_,i64>(4)?;let s=r.get::<_,i64>(5)?;Ok(json!({"claimed":r.get::<_,f64>(0)?,"beliefs":r.get::<_,i64>(1)?,"outcomes":r.get::<_,i64>(2)?,"confirmed":c,"contradicted":x,"stale":s,"precision":if c+x+s>0{Some(c as f64/(c+x+s) as f64)}else{None}}))})?.collect::<std::result::Result<Vec<_>,_>>()?;
    Ok(json!({"calibrated":total>=100,"ledger_total":total,"display_gate":100,"buckets":rows}))
}
pub fn duplicates(cfg: &Config, req: &Value, cross: bool) -> Result<Value> {
    let threshold = req["threshold"]
        .as_f64()
        .or_else(|| {
            std::env::var("LORE_DUP_CONTAINMENT")
                .ok()
                .and_then(|s| s.parse().ok())
        })
        .unwrap_or(0.85);
    if !threshold.is_finite() || !(0.0..=1.0).contains(&threshold) {
        return Err(Error::InvalidRequest);
    }
    let conn = store::connect(cfg)?;
    let mut stmt=conn.prepare("SELECT id,subject,claim,confidence FROM beliefs WHERE status='active' ORDER BY subject,id LIMIT 4097")?;
    let mut rows = Vec::new();
    let mut budget = 8 * 1024 * 1024;
    let mut cursor = stmt.query([])?;
    while let Some(r) = cursor.next()? {
        if rows.len() >= 4096 {
            return Err(Error::TooLarge);
        }
        let claim = crate::graph::db_text(r, 2, 65536, &mut budget)?;
        let subject = crate::graph::db_text(r, 1, 4096, &mut budget)?;
        let tokens = crate::skills::overlap_tokens(&claim);
        rows.push((
            r.get::<_, i64>(0)?,
            subject,
            claim,
            r.get::<_, f64>(3)?,
            tokens,
        ));
    }
    let mut pairs = Vec::new();
    let mut inspected = 0;
    for (i, a) in rows.iter().enumerate() {
        for b in rows.iter().skip(i + 1) {
            inspected += 1;
            if inspected > 8_000_000 {
                return Err(Error::TooLarge);
            }
            let scope = if cross {
                (a.1 == "user" && b.1 == "user-model") || (a.1 == "user-model" && b.1 == "user")
            } else {
                a.1 == b.1
            };
            if !scope {
                continue;
            }
            let score =
                crate::skills::containment(&a.4, &b.4).max(crate::skills::containment(&b.4, &a.4));
            if score >= threshold {
                if pairs.len() >= 4096 {
                    return Err(Error::TooLarge);
                }
                pairs.push((score,json!({"score":score,"a":{"id":a.0,"subject":a.1,"claim":crate::scrub::scrub(&a.2)?,"confidence":a.3},"b":{"id":b.0,"subject":b.1,"claim":crate::scrub::scrub(&b.2)?,"confidence":b.3}})))
            }
        }
    }
    pairs.sort_by(|a, b| b.0.total_cmp(&a.0));
    Ok(
        json!({"threshold":threshold,"pairs":pairs.into_iter().map(|(_,p)|p).collect::<Vec<_>>(),"changed":false}),
    )
}
pub fn audit(cfg: &Config, req: &Value, auth: &Authority) -> Result<Value> {
    if !auth.may_write() {
        return Err(Error::Untrusted);
    }
    let conn = store::connect(cfg)?;
    let mut stmt=conn.prepare("SELECT id,subject,claim,confidence FROM beliefs WHERE status='active' ORDER BY random() LIMIT ?")?;
    let mut selected = Vec::new();
    let mut budget = crate::MAX_FRAME_BYTES;
    let mut cursor = stmt.query([cap(req, "sample", 10, 100)? as i64])?;
    while let Some(r) = cursor.next()? {
        selected.push((
            r.get::<_, i64>(0)?,
            crate::graph::db_text(r, 1, 4096, &mut budget)?,
            crate::graph::db_text(r, 2, 65536, &mut budget)?,
            r.get::<_, f64>(3)?,
        ))
    }
    drop(cursor);
    drop(stmt);
    let paths = regex::Regex::new(r"/[\w./~-]+/[\w./-]+").map_err(|_| Error::Unavailable)?;
    let tokens = regex::Regex::new(r#"\b[A-Za-z_]\w*=[^\s'\"]+|--[a-z][\w-]+"#)
        .map_err(|_| Error::Unavailable)?;
    let mut rows = Vec::new();
    for (id, subject, claim, confidence) in selected {
        let (verdict, detail) = if let Some(path) = paths.find(&claim) {
            let p = path.as_str().trim_end_matches(['.', ',', ';', ':']);
            (
                if Path::new(p).exists() {
                    "PASS"
                } else {
                    "FAIL"
                },
                format!("path {p}"),
            )
        } else if let Some(token) = tokens.find(&claim) {
            let token = token.as_str().trim_end_matches(['.', ',', ';', ':']);
            let mut child = Command::new("git")
                .args(["grep", "-q", "-F", "-e", token, "--"])
                .current_dir(gate::cwd(req)?)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|_| Error::Unavailable)?;
            let started = Instant::now();
            let status = loop {
                if let Some(status) = child.try_wait()? {
                    break Some(status.code());
                }
                if started.elapsed() > Duration::from_secs(10) {
                    let _ = child.kill();
                    let _ = child.wait();
                    break None;
                }
                std::thread::sleep(Duration::from_millis(10))
            };
            let verdict = match status {
                Some(Some(0)) => "PASS",
                Some(Some(1)) => "FAIL",
                _ => "UNCHECKABLE",
            };
            (verdict, format!("git grep {token}"))
        } else {
            ("UNCHECKABLE", "no machine-checkable fragment".into())
        };
        if matches!(verdict, "PASS" | "FAIL") {
            beliefs::outcome(
                cfg,
                &json!({"belief_id":id,"event":if verdict=="PASS"{"confirmed"}else{"stale"},"source":"audit","note":detail}),
                auth,
            )?;
        }
        rows.push(json!({"id":id,"subject":subject,"claim":crate::scrub::scrub(&claim)?,"confidence":confidence,"verdict":verdict,"detail":crate::scrub::scrub(&detail)?}));
    }
    Ok(json!({"rows":rows}))
}
pub fn derive_graph(cfg: &Config, req: &Value) -> Result<Value> {
    let subjects = beliefs::subjects(req)?;
    let conn = store::connect(cfg)?;
    let mut stmt = conn.prepare(
        "SELECT id,subject,claim FROM beliefs WHERE status='active' ORDER BY id LIMIT 4097",
    )?;
    let mut claims = BTreeMap::new();
    let mut cursor = stmt.query([])?;
    let mut budget = crate::MAX_FRAME_BYTES / 2;
    while let Some(r) = cursor.next()? {
        let subject = crate::graph::db_text(r, 1, 4096, &mut budget)?;
        if req["all"] != true && !subjects.contains(&subject) {
            continue;
        }
        if claims.len() >= 4096 {
            return Err(Error::TooLarge);
        }
        claims.insert(
            r.get::<_, i64>(0)?,
            crate::scrub::scrub(&crate::graph::db_text(r, 2, 65536, &mut budget)?)?,
        );
    }
    let count = claims.len();
    if count < 2 {
        return Ok(json!({"claims":count,"written":0}));
    }
    let limit = cap(req, "max_edges", 60, 1000)?;
    let prompt = include_str!("graph_derive_prompt.txt")
        .replace("{cap}", &limit.to_string())
        .replace("{n}", &count.to_string())
        .replace(
            "{beliefs}",
            &claims
                .iter()
                .map(|(id, claim)| format!("{id} | {}", gate::one_line(claim)))
                .collect::<Vec<_>>()
                .join("\n"),
        );
    if prompt.len() > crate::MAX_FRAME_BYTES {
        return Err(Error::TooLarge);
    }
    if req["dry_run"] == true {
        return Ok(json!({"prompt":prompt,"claims":count}));
    }
    let response = provider(cfg, req, &prompt, "LORE_DREAMER_MODEL", "sonnet")?;
    let data = crate::review::extract_json(&response)?;
    let edges = data["edges"]
        .as_array()
        .filter(|a| a.len() <= 10000)
        .ok_or(Error::InvalidRequest)?;
    let auth = Authority::Derived {
        agent: "graph-derive".into(),
        engine: req["engine"].as_str().unwrap_or("claude").into(),
    };
    let (mut written, mut reasserted, mut bad) = (0, 0, 0);
    for edge in edges.iter().take(limit) {
        let (Some(src), Some(dst), Some(rel)) = (
            edge["from"].as_i64(),
            edge["to"].as_i64(),
            edge["rel"].as_str(),
        ) else {
            bad += 1;
            continue;
        };
        if src == dst
            || !claims.contains_key(&src)
            || !claims.contains_key(&dst)
            || !crate::graph::ASSERTED.contains(&rel)
        {
            bad += 1;
            continue;
        }
        let result=crate::graph::edge_insert(cfg,&json!({"src":src,"dst":dst,"rel":rel,"source":"derived","session_id":"graph-derive","note":edge["why"].as_str().unwrap_or("")}),&auth).map_err(|error|if written>0{Error::MayHaveApplied}else{error})?;
        if result["created"] == true {
            written += 1
        } else {
            reasserted += 1
        }
    }
    Ok(
        json!({"claims":count,"proposed":edges.len().min(limit),"written":written,"reasserted":reasserted,"dropped":bad}),
    )
}
fn known(cfg: &Config, conn: &Connection) -> Result<BTreeSet<String>> {
    let mut out = BTreeSet::new();
    for dir in [cfg.root.join("projects"), cfg.projects.clone()] {
        for name in names(&dir)? {
            if let Some(s) = name.to_str().filter(|s| config::valid_slug(s)) {
                out.insert(s.into());
            }
        }
    }
    let mut stmt=conn.prepare("SELECT substr(subject,9) FROM beliefs WHERE subject LIKE 'project:%' UNION SELECT project FROM sessions WHERE project IS NOT NULL UNION SELECT slug FROM sync_projects LIMIT 10001")?;
    for row in stmt.query_map([], |r| r.get::<_, String>(0))? {
        if out.len() >= 10000 {
            return Err(Error::TooLarge);
        }
        let s = row?;
        if config::valid_slug(&s) {
            out.insert(s);
        }
    }
    Ok(out)
}
fn resolve(raw: &str, known: &BTreeSet<String>) -> Result<String> {
    if Path::new(raw).is_absolute() && Path::new(raw).is_dir() {
        return Ok(config::project_slug(Path::new(raw)));
    }
    let raw = raw.strip_prefix("project:").unwrap_or(raw);
    for value in [raw.to_owned(), format!("-{raw}")] {
        if known.contains(&value) {
            return Ok(value);
        }
    }
    let suffix = known
        .iter()
        .filter(|s| s.ends_with(raw))
        .cloned()
        .collect::<Vec<_>>();
    let matches = if suffix.len() == 1 {
        suffix
    } else {
        known
            .iter()
            .filter(|s| s.contains(raw))
            .cloned()
            .collect::<Vec<_>>()
    };
    if matches.len() == 1 {
        Ok(matches[0].clone())
    } else {
        Err(Error::Changed)
    }
}
pub fn relocate(cfg: &Config, req: &Value, p: &[String], auth: &Authority) -> Result<Value> {
    let old = req["old"]
        .as_str()
        .or_else(|| p.first().map(String::as_str))
        .ok_or(Error::InvalidRequest)?;
    let new = req["new"]
        .as_str()
        .or_else(|| p.get(1).map(String::as_str))
        .ok_or(Error::InvalidRequest)?;
    let dry = req["dry_run"] == true;
    if !dry && !auth.may_write() {
        return Err(Error::Untrusted);
    }
    let mut conn = store::connect(cfg)?;
    let projects = known(cfg, &conn)?;
    let target = resolve(new, &projects)?;
    let bare = !old.starts_with("project:")
        && !matches!(old, "user" | "user-model")
        && conn
            .query_row(
                "SELECT 1 FROM beliefs WHERE subject=? LIMIT 1",
                [old],
                |r| r.get::<_, i64>(0),
            )
            .optional()?
            .is_some();
    let source = if bare {
        old.into()
    } else {
        resolve(old, &projects)?
    };
    if !bare && source == target {
        return Err(Error::InvalidRequest);
    }
    let old_subject = if bare {
        source.clone()
    } else {
        format!("project:{source}")
    };
    let new_subject = format!("project:{target}");
    let mut report = json!({"source":source,"target":target,"dry_run":dry,"left":[]});
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut stmt = tx
        .prepare("SELECT id,claim FROM beliefs WHERE subject=? AND status='active' LIMIT 100001")?;
    let active = stmt
        .query_map([&old_subject], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    drop(stmt);
    if active.len() > 100000 {
        return Err(Error::TooLarge);
    }
    let count = tx.query_row(
        "SELECT count(*) FROM beliefs WHERE subject=?",
        [&old_subject],
        |r| r.get::<_, i64>(0),
    )?;
    let mut duplicates = 0;
    for (id, claim) in active {
        let by=tx.query_row("SELECT id FROM beliefs WHERE subject=? AND lower(claim)=lower(?) AND status='active' LIMIT 1",params![new_subject,claim],|r|r.get::<_,i64>(0)).optional()?;
        if let Some(by) = by {
            duplicates += 1;
            if !dry {
                beliefs::supersede_in_transaction(
                    cfg,
                    &tx,
                    id,
                    Some(by),
                    &format!("project move {source} -> {target}: same claim already held as {by}"),
                    auth,
                )?;
            }
        }
    }
    if !dry {
        tx.execute(
            "UPDATE beliefs SET subject=? WHERE subject=?",
            params![new_subject, old_subject],
        )?;
    }
    report["beliefs"] = json!(count);
    report["duplicates_superseded"] = json!(duplicates);
    if !bare {
        for table in ["belief_evidence", "sessions", "msg", "reviewed"] {
            let n = tx.query_row(
                &format!("SELECT count(*) FROM {table} WHERE project=?"),
                [&source],
                |r| r.get::<_, i64>(0),
            )?;
            if !dry {
                tx.execute(
                    &format!("UPDATE {table} SET project=? WHERE project=?"),
                    params![target, source],
                )?;
            }
            report[table] = json!(n);
        }
        let mappings = tx.query_row(
            "SELECT count(*) FROM sync_projects WHERE slug=?",
            [&source],
            |r| r.get::<_, i64>(0),
        )?;
        if !dry {
            tx.execute(
                "UPDATE sync_projects SET slug=? WHERE slug=?",
                params![target, source],
            )?;
        }
        report["sync_projects"] = json!(mappings);
    }
    if dry {
        tx.rollback()?
    } else {
        tx.commit()?
    }
    if bare {
        return Ok(report);
    }
    let mut left = Vec::new();
    let mut pending = 0;
    let pdir = cfg.root.join("pending");
    for name in names(&pdir)? {
        if !name.to_str().is_some_and(|s| s.ends_with(".json")) {
            continue;
        }
        let path = pdir.join(name);
        let operation = (|| -> Result<bool> {
            let _locks =
                files::Locks::acquire(&cfg.root, std::slice::from_ref(&path), cfg.timeout)?;
            let raw = files::read_regular(&path, crate::MAX_FRAME_BYTES)?;
            let mut item: Value =
                serde_json::from_slice(&raw).map_err(|_| Error::InvalidRequest)?;
            if item["project"] != source && item["origin_project"] != source {
                return Ok(false);
            }
            for key in ["project", "origin_project"] {
                if item[key] == source {
                    item[key] = json!(target)
                }
            }
            if !dry {
                files::atomic_write(
                    &path,
                    &serde_json::to_vec_pretty(&item).map_err(|_| Error::Unavailable)?,
                )?
            }
            Ok(true)
        })();
        match operation {
            Ok(true) => pending += 1,
            Ok(false) => {}
            Err(e) => left.push(
                json!({"kind":"pending","path":path,"error":e.code(),"may_have_applied":!dry}),
            ),
        }
    }
    report["pending"] = json!(pending);
    let src = Scope::Project.path(cfg, &source)?;
    let mut moved = 0;
    for entry in memory::read_entries(&src)? {
        if dry {
            moved += 1;
            continue;
        }
        match memory::move_entry(cfg,Scope::Project,&source,&entry,Scope::Project,&target,auth){Ok(memory::MoveOutcome::Complete)=>moved+=1,Ok(memory::MoveOutcome::DestinationOnly(e)|memory::MoveOutcome::Uncertain(e))=>left.push(json!({"kind":"memory","text":crate::scrub::scrub(&entry)?,"error":e.code(),"may_have_applied":true})),Err(e)=>left.push(json!({"kind":"memory","text":crate::scrub::scrub(&entry)?,"error":e.code(),"may_have_applied":false}))}
    }
    report["memory"] = json!(moved);
    let src = crate::filemap::path(cfg, &source)?;
    let dst = crate::filemap::path(cfg, &target)?;
    let mut moved = 0;
    for entry in memory::read_entries(&src)? {
        if dry {
            moved += 1;
            continue;
        }
        let result = (|| -> Result<()> {
            let _locks =
                files::Locks::acquire(&cfg.root, &[src.clone(), dst.clone()], cfg.timeout)?;
            let (path, purpose) = entry
                .split_once(crate::filemap::SEP)
                .ok_or(Error::InvalidRequest)?;
            let provenance = gate::provenance(cfg, "filemap", &source, &entry);
            crate::filemap::mutate_locked(
                cfg,
                &target,
                "add",
                "",
                path.trim(),
                purpose.trim(),
                None,
                "project-move",
                auth,
            )?;
            gate::record_preserved(
                cfg,
                "filemap",
                &target,
                &entry,
                &provenance,
                &format!("moved from {source}"),
            )
            .map_err(|_| Error::MayHaveApplied)?;
            crate::filemap::mutate_locked(
                cfg,
                &source,
                "remove-exact",
                &entry,
                "",
                "",
                None,
                "project-move",
                auth,
            )
            .map_err(|_| Error::MayHaveApplied)
        })();
        match result{Ok(())=>moved+=1,Err(e)=>left.push(json!({"kind":"filemap","text":crate::scrub::scrub(&entry)?,"error":e.code(),"may_have_applied":e==Error::MayHaveApplied}))}
    }
    report["filemap"] = json!(moved);
    report["left"] = json!(left);
    report["partial"] = json!(!left.is_empty());
    if !dry {
        for path in [
            Scope::Project.path(cfg, &source)?,
            crate::filemap::path(cfg, &source)?,
        ] {
            if path.try_exists()? && memory::read_entries(&path)?.is_empty() {
                let dir = files::open_directory(path.parent().ok_or(Error::UnsafePath)?)?;
                files::unlink_at(&dir, path.file_name().ok_or(Error::UnsafePath)?)
                    .map_err(|_| Error::MayHaveApplied)?
            }
        }
    }
    Ok(report)
}
pub fn provenance(cfg: &Config, req: &Value) -> Result<Value> {
    let slug = config::project_slug(gate::cwd(req)?);
    let host = memory::this_machine();
    let mut out = json!({});
    for (scope, key) in [
        (Scope::User, "user"),
        (Scope::Project, slug.as_str()),
        (Scope::Machine, host.as_str()),
    ] {
        out[scope.name()]=json!(memory::read_entries(&scope.path(cfg,key)?)?.into_iter().map(|entry|json!({"text":crate::scrub::scrub(&entry).unwrap_or_else(|_|"[unavailable]".into()),"provenance":gate::provenance(cfg,"memory",&scope.bucket(key),&entry)})).collect::<Vec<_>>())
    }
    Ok(out)
}
pub fn reset(cfg: &Config, req: &Value, auth: &Authority) -> Result<Value> {
    if !auth.may_write() {
        return Err(Error::Untrusted);
    }
    if req["all"] != true && req["index"] != true && req["beliefs"] != true {
        return Err(Error::InvalidRequest);
    }
    let mut conn = store::connect(cfg)?;
    let backup = crate::sync_admin::backup(cfg, &conn)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let tables = if req["all"] == true {
        let mut stmt=tx.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'msg_%' AND name NOT LIKE 'belief_fts_%' ORDER BY name LIMIT 101")?;
        let names = stmt
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        if names.len() > 100 {
            return Err(Error::TooLarge);
        }
        names
    } else {
        let mut names = Vec::new();
        if req["index"] == true {
            names.extend(["msg", "sessions", "files"].map(String::from));
        }
        if req["beliefs"] == true {
            names.extend(
                [
                    "belief_fts",
                    "belief_edge_assertions",
                    "belief_edges",
                    "belief_outcomes",
                    "belief_evidence",
                    "dream_reviewed",
                    "beliefs",
                ]
                .map(String::from),
            );
        }
        names
    };
    for table in &tables {
        if !table
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
        {
            return Err(Error::InvalidRequest);
        }
        tx.execute_batch(&format!("DROP TABLE IF EXISTS \"{table}\""))?;
    }
    tx.commit()?;
    drop(conn);
    store::connect(cfg).map_err(|_| Error::MayHaveApplied)?;
    Ok(json!({"dropped":tables,"backup":backup,"curated_memory_preserved":true}))
}
pub fn motd(cfg: &Config, req: &Value) -> Result<Value> {
    let conn = store::connect(cfg)?;
    let max = conn.query_row("SELECT coalesce(max(id),0) FROM beliefs", [], |r| {
        r.get::<_, i64>(0)
    })?;
    let path = cfg.root.join("motd_state.json");
    let _locks = files::Locks::acquire(&cfg.root, std::slice::from_ref(&path), cfg.timeout)?;
    let old = if path.try_exists()? {
        serde_json::from_slice::<Value>(&files::read_regular(&path, 4096)?)
            .map_err(|_| Error::InvalidRequest)?["max_belief_id"]
            .as_i64()
            .unwrap_or(0)
    } else {
        0
    };
    let n = conn.query_row("SELECT count(*) FROM beliefs WHERE id>?", [old], |r| {
        r.get::<_, i64>(0)
    })?;
    files::atomic_write(
        &path,
        &serde_json::to_vec(&json!({"max_belief_id":max})).map_err(|_| Error::Unavailable)?,
    )?;
    Ok(
        json!({"new_beliefs":n,"pending":crate::pending::ids(cfg)?.len(),"memory":memory::usage(cfg,req)?}),
    )
}
pub fn statusline(cfg: &Config) -> Result<Value> {
    let pending = crate::pending::ids(cfg)?.len();
    let beliefs = store::read_only(cfg)
        .ok()
        .and_then(|conn| {
            conn.query_row(
                "SELECT count(*) FROM beliefs WHERE status='active'",
                [],
                |r| r.get::<_, i64>(0),
            )
            .ok()
        })
        .unwrap_or(0);
    Ok(json!(format!("lore {beliefs} beliefs · {pending} pending")))
}
pub fn teardown(cfg: &Config, req: &Value) -> Result<Value> {
    let dry = req["dry_run"] == true;
    let slug = config::project_slug(gate::cwd(req)?);
    let mut exports = Vec::new();
    let user = memory::read_entries(&cfg.root.join("USER.md"))?;
    if !user.is_empty() {
        exports.push((
            "user",
            "user".to_owned(),
            cfg.projects.join(&slug).join("memory/lore-export-user.md"),
            user,
        ))
    }
    for name in names(&cfg.root.join("projects"))? {
        let name = name
            .to_str()
            .filter(|s| config::valid_slug(s))
            .ok_or(Error::UnsafePath)?;
        let entries = memory::read_entries(&Scope::Project.path(cfg, name)?)?;
        if !entries.is_empty() {
            exports.push((
                "project",
                name.to_owned(),
                cfg.projects
                    .join(name)
                    .join("memory/lore-export-project.md"),
                entries,
            ))
        }
    }
    for host in memory::known_machines(cfg) {
        let entries = memory::read_entries(&Scope::Machine.path(cfg, &host)?)?;
        if !entries.is_empty() {
            exports.push((
                "machine",
                host.clone(),
                cfg.projects
                    .join(&slug)
                    .join(format!("memory/lore-export-machine-{host}.md")),
                entries,
            ))
        }
    }
    let mut report = Vec::new();
    for (scope, key, path, entries) in exports {
        report.push(json!({"scope":scope,"key":key,"path":path,"entries":entries.len()}));
        if dry {
            continue;
        }
        let label = if scope == "machine" {
            format!("lore-export-machine-{key}")
        } else {
            format!("lore-export-{scope}")
        };
        let text=format!("---\nname: {label}\ndescription: Curated LORE memory ({scope})\nmetadata:\n  type: {scope}\n---\n\n{}",memory::render_entries(&entries));
        files::atomic_write(&path, text.as_bytes())?;
        let pointer = format!(
            "- [lore export ({scope})]({}) — curated lore memory returned by `lore teardown`",
            path.file_name()
                .and_then(|s| s.to_str())
                .ok_or(Error::UnsafePath)?
        );
        let index = path.parent().ok_or(Error::UnsafePath)?.join("MEMORY.md");
        if index.try_exists()? {
            let _locks = files::Locks::acquire(
                index.parent().ok_or(Error::UnsafePath)?,
                std::slice::from_ref(&index),
                cfg.timeout,
            )?;
            let raw = String::from_utf8(files::read_regular(&index, crate::MAX_FRAME_BYTES)?)
                .map_err(|_| Error::InvalidRequest)?;
            if !raw.contains(&pointer) {
                files::atomic_write(&index, format!("{raw}\n{pointer}\n").as_bytes())?
            }
        }
    }
    let home = PathBuf::from(std::env::var_os("HOME").ok_or(Error::Unavailable)?);
    let path = std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".claude"))
        .join("settings.json");
    if path.try_exists()? {
        let _locks = files::Locks::acquire(
            path.parent().ok_or(Error::UnsafePath)?,
            std::slice::from_ref(&path),
            cfg.timeout,
        )?;
        let mut settings: Value =
            serde_json::from_slice(&files::read_regular(&path, crate::MAX_FRAME_BYTES)?)
                .map_err(|_| Error::InvalidRequest)?;
        if !settings.is_object() {
            return Err(Error::InvalidRequest);
        }
        settings["autoMemoryEnabled"] = json!(true);
        if let Some(env) = settings["env"].as_object_mut() {
            env.retain(|k, _| !k.starts_with("LORE_"));
        }
        if !dry {
            files::atomic_write(
                &path,
                &serde_json::to_vec_pretty(&settings).map_err(|_| Error::Unavailable)?,
            )?
        }
    }
    Ok(json!({"exports":report,"dry_run":dry,"root_preserved":cfg.root,"auto_memory_enabled":!dry}))
}
