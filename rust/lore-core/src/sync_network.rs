//! Bounded native sync transports. Networking conveys data, never write authority.
use crate::{config::Config, files, store, sync_apply, Error, Result};
use reqwest::{blocking::Client, Url};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::Path,
    time::{Duration, Instant},
};
const RESPONSE_BYTES: usize = 32 * 1024 * 1024;
const DRAIN_BYTES: usize = 128 * 1024 * 1024;
const MAX_OPS: usize = 100_000;
const BUNDLE_BYTES: usize = 128 * 1024 * 1024;
const PORTABLE: &[&str] = &["memory", "filemap", "belief", "pending", "skill"];

#[derive(Debug)]
pub struct TransportError {
    pub status: u16,
    pub code: String,
    pub body: Value,
}
impl TransportError {
    fn fixed(status: u16, code: &str) -> Self {
        Self {
            status,
            code: code.into(),
            body: json!({"error":code}),
        }
    }
}
// Deliberately excludes URL, credential and arbitrary remote diagnostic text.
impl std::fmt::Display for TransportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "sync {} ({})", self.code, self.status)
    }
}
impl std::error::Error for TransportError {}
pub struct Transport {
    client: Client,
    base: Url,
    credential: Option<String>,
    pub peer: String,
}
fn timeout() -> Duration {
    Duration::from_secs_f64(
        std::env::var("LORE_SYNC_TIMEOUT")
            .ok()
            .and_then(|x| x.parse::<f64>().ok())
            .filter(|n| n.is_finite() && *n > 0.)
            .unwrap_or(15.)
            .min(120.),
    )
}
fn port() -> u16 {
    std::env::var("LORE_SYNC_PEER_PORT")
        .ok()
        .and_then(|x| x.parse().ok())
        .filter(|n| *n > 0)
        .unwrap_or(8765)
}
impl Transport {
    pub fn new(base: &str, credential: Option<String>, peer: String) -> Result<Self> {
        let mut base = Url::parse(base).map_err(|_| Error::InvalidRequest)?;
        if !matches!(base.scheme(), "http" | "https")
            || base.host_str().is_none()
            || !base.username().is_empty()
            || base.password().is_some()
            || base.query().is_some()
            || base.fragment().is_some()
        {
            return Err(Error::InvalidRequest);
        }
        let path = base.path().trim_end_matches('/');
        let path = if path.ends_with("/v1") {
            path.to_owned()
        } else {
            format!("{path}/v1")
        };
        base.set_path(&format!("{path}/"));
        let client = Client::builder()
            .timeout(timeout())
            .connect_timeout(timeout())
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| Error::Unavailable)?;
        Ok(Self {
            client,
            base,
            credential,
            peer,
        })
    }
    pub fn hub() -> Result<Self> {
        let url = std::env::var("LORE_SYNC_URL").map_err(|_| Error::InvalidRequest)?;
        let auth = std::env::var("LORE_SYNC_AUTH").unwrap_or_else(|_| "token".into());
        let token = if auth == "token" {
            Some(
                std::env::var("LORE_SYNC_TOKEN")
                    .ok()
                    .filter(|s| !s.is_empty())
                    .ok_or(Error::Untrusted)?,
            )
        } else if auth == "tailscale" {
            None
        } else {
            return Err(Error::InvalidRequest);
        };
        Self::new(&url, token, "hub".into())
    }
    pub fn peer(spec: &str) -> Result<Self> {
        let raw = if spec.contains("://") {
            spec.to_owned()
        } else if spec.starts_with('[') {
            if spec.contains("]:") {
                format!("http://{spec}")
            } else {
                format!("http://{spec}:{}", port())
            }
        } else if spec.matches(':').count() > 1 {
            format!("http://[{spec}]:{}", port())
        } else if spec
            .rsplit_once(':')
            .is_some_and(|(_, p)| p.parse::<u16>().is_ok())
        {
            format!("http://{spec}")
        } else {
            format!("http://{spec}:{}", port())
        };
        let url = Url::parse(&raw).map_err(|_| Error::InvalidRequest)?;
        let host = url
            .host_str()
            .ok_or(Error::InvalidRequest)?
            .trim_matches(['[', ']'])
            .to_lowercase();
        let host = if host.contains(':') {
            format!("[{host}]")
        } else {
            host
        };
        let p = url.port_or_known_default().ok_or(Error::InvalidRequest)?;
        let key = if url.scheme() == "https" {
            format!(
                "peer:https://{host}{}",
                if p == 443 {
                    String::new()
                } else {
                    format!(":{p}")
                }
            )
        } else if p == port() {
            format!("peer:{host}")
        } else if p == 80 {
            format!("peer:http://{host}")
        } else {
            format!("peer:{host}:{p}")
        };
        Self::new(
            &raw,
            std::env::var("LORE_SYNC_PEER_SECRET")
                .ok()
                .filter(|s| !s.is_empty()),
            key,
        )
    }
    pub fn request(
        &self,
        method: &str,
        path: &str,
        query: &[(&str, String)],
        body: Option<&Value>,
    ) -> std::result::Result<Value, TransportError> {
        if !matches!(
            (method, path),
            ("GET", "ops" | "health" | "whoami") | ("POST", "ops")
        ) || path == "ops" && method == "POST" && self.peer != "hub"
        {
            return Err(TransportError::fixed(0, "unavailable_operation"));
        }
        let url = self
            .base
            .join(path)
            .map_err(|_| TransportError::fixed(0, "bad_url"))?;
        for attempt in 0..4 {
            let mut request = self
                .client
                .request(
                    if method == "POST" {
                        reqwest::Method::POST
                    } else {
                        reqwest::Method::GET
                    },
                    url.clone(),
                )
                .query(query);
            if path != "health" {
                if let Some(secret) = &self.credential {
                    request = request.bearer_auth(secret);
                }
            }
            if let Some(body) = body {
                request = request.json(body);
            }
            let response = request
                .send()
                .map_err(|_| TransportError::fixed(0, "unreachable"))?;
            let status = response.status().as_u16();
            let mut raw = Vec::new();
            response
                .take(RESPONSE_BYTES as u64 + 1)
                .read_to_end(&mut raw)
                .map_err(|_| TransportError::fixed(status, "unreachable"))?;
            if raw.len() > RESPONSE_BYTES {
                return Err(TransportError::fixed(status, "response_too_large"));
            }
            let answer: Value = serde_json::from_slice(&raw)
                .ok()
                .filter(Value::is_object)
                .ok_or_else(|| TransportError::fixed(status, "protocol_error"))?;
            if (200..300).contains(&status) {
                return Ok(answer);
            }
            let code = answer["error"]
                .as_str()
                .filter(|s| s.len() <= 64 && s.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'))
                .unwrap_or("protocol_error")
                .to_owned();
            if status == 503 && code == "busy" && attempt < 3 {
                std::thread::sleep(Duration::from_millis([200, 500, 1000][attempt]));
                continue;
            }
            return Err(TransportError {
                status,
                code,
                body: answer,
            });
        }
        unreachable!()
    }
}
fn wire(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let payload: String = row.get(8)?;
    let payload = serde_json::from_str::<Value>(&payload).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(8, rusqlite::types::Type::Text, Box::new(e))
    })?;
    Ok(
        json!({"seq":row.get::<_,i64>(0)?,"op_id":row.get::<_,String>(1)?,"machine_id":row.get::<_,String>(2)?,"machine_seq":row.get::<_,i64>(3)?,"lamport":row.get::<_,i64>(4)?,"class":row.get::<_,String>(5)?,"op":row.get::<_,String>(6)?,"project_key":row.get::<_,Option<String>>(7)?,"payload":payload,"mac":row.get::<_,Option<String>>(9)?,"created":row.get::<_,String>(10)?}),
    )
}
const SELECT:&str="SELECT seq,op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created FROM sync_ops";
fn peer_state(conn: &Connection, peer: &str) -> Result<(i64, i64)> {
    conn.execute(
        "INSERT OR IGNORE INTO sync_peers(peer,pushed_seq) VALUES(?,0)",
        [peer],
    )?;
    let (push, cursor) = conn.query_row(
        "SELECT pushed_seq,pulled_cursor FROM sync_peers WHERE peer=?",
        [peer],
        |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?)),
    )?;
    let cursor = cursor
        .map(|s| s.parse::<i64>().map_err(|_| Error::InvalidRequest))
        .transpose()?
        .unwrap_or(0);
    if push < 0 || cursor < 0 {
        return Err(Error::InvalidRequest);
    }
    Ok((push, cursor))
}
fn note(
    conn: &Connection,
    peer: &str,
    push: Option<i64>,
    pull: Option<i64>,
    error: Option<&str>,
) -> Result<()> {
    conn.execute("UPDATE sync_peers SET pushed_seq=coalesce(?,pushed_seq),pulled_cursor=coalesce(?,pulled_cursor),last_push=CASE WHEN ? IS NULL THEN last_push ELSE ? END,last_pull=CASE WHEN ? IS NULL THEN last_pull ELSE ? END,last_error=? WHERE peer=?",params![push,pull.map(|x|x.to_string()),push,crate::utcnow(),pull,crate::utcnow(),error,peer])?;
    Ok(())
}
/// Drain completely before application so merge ordering spans pages.
pub fn pull(cfg: &Config, transport: &Transport) -> Result<Value> {
    pull_from(cfg, transport, None, true)
}
pub fn pull_from(
    cfg: &Config,
    transport: &Transport,
    from: Option<i64>,
    exclude_self: bool,
) -> Result<Value> {
    if !cfg.sync.enabled {
        return Err(Error::Untrusted);
    }
    let conn = store::connect(cfg)?;
    let machine = store::machine_id(&conn)?;
    let (_, saved) = peer_state(&conn, &transport.peer)?;
    let initial = from.unwrap_or(saved);
    if initial < 0 {
        return Err(Error::InvalidRequest);
    }
    let mut cursor = initial;
    let mut drained = initial;
    let mut ops = Vec::new();
    let mut bytes = 0;
    let started = Instant::now();
    let mut pages = 0;
    let fetched = (|| -> Result<()> {
        loop {
            if pages >= 10000 || started.elapsed() > Duration::from_secs(600) {
                return Err(Error::TooLarge);
            }
            let response = transport
                .request(
                    "GET",
                    "ops",
                    &[
                        ("since", cursor.to_string()),
                        ("limit", "500".into()),
                        (
                            "exclude",
                            if exclude_self {
                                machine.clone()
                            } else {
                                String::new()
                            },
                        ),
                    ],
                    None,
                )
                .map_err(|_| Error::Unavailable)?;
            let page = response["ops"]
                .as_array()
                .filter(|p| p.len() <= 512)
                .ok_or(Error::InvalidRequest)?;
            bytes += serde_json::to_vec(page)
                .map_err(|_| Error::InvalidRequest)?
                .len();
            if bytes > DRAIN_BYTES || ops.len() + page.len() > MAX_OPS {
                return Err(Error::TooLarge);
            }
            for op in page {
                let position = op["hub_seq"]
                    .as_i64()
                    .filter(|n| *n > cursor)
                    .ok_or(Error::InvalidRequest)?;
                drained = drained.max(position);
                ops.push(op.clone());
            }
            pages += 1;
            match response.get("next") {
                Some(Value::Null) => break,
                Some(n) => {
                    let next = n
                        .as_i64()
                        .filter(|n| *n > cursor)
                        .ok_or(Error::InvalidRequest)?;
                    cursor = next;
                    drained = drained.max(next)
                }
                None => return Err(Error::InvalidRequest),
            }
        }
        Ok(())
    })();
    if let Err(e) = fetched {
        note(&conn, &transport.peer, None, None, Some(e.code()))?;
        return Err(e);
    }
    let mut report = json!({"applied":0,"deferred":0,"unverified":0,"duplicate":0,"unknown":0,"failed":0,"skipped":0,"may_have_applied":0,"staged":0});
    for (index, chunk) in sync_apply::canonical_order(&ops).chunks(512).enumerate() {
        let result = sync_apply::apply_ops(cfg, chunk).map_err(|error| {
            if index > 0 {
                Error::MayHaveApplied
            } else {
                error
            }
        })?;
        for (k, v) in result.as_object().ok_or(Error::Unavailable)? {
            report[k] =
                json!(report[k].as_u64().unwrap_or(0) + v.as_u64().ok_or(Error::Unavailable)?);
        }
    }
    // Failed receipt/staging may not have retained the operation. Keep the
    // cursor until every row is durably received; idempotency handles retries.
    let retry = report["failed"].as_u64().unwrap_or(0) > 0
        || report["may_have_applied"].as_u64().unwrap_or(0) > 0;
    if retry {
        note(
            &conn,
            &transport.peer,
            None,
            None,
            Some("application_incomplete"),
        )?;
    } else {
        note(&conn, &transport.peer, None, Some(drained), None)?;
    }
    report["cursor_advanced"] = json!(!retry);
    report["pages"] = json!(pages);
    report["drained_to"] = json!(drained);
    Ok(report)
}
pub fn push(cfg: &Config, transport: &Transport, from: Option<i64>) -> Result<Value> {
    if !cfg.sync.enabled || transport.peer != "hub" {
        return Err(Error::Untrusted);
    }
    let conn = store::connect(cfg)?;
    let machine = store::machine_id(&conn)?;
    let (mut cursor, _) = peer_state(&conn, &transport.peer)?;
    if let Some(n) = from {
        if n < 0 {
            return Err(Error::InvalidRequest);
        }
        cursor = n
    }
    let limits = transport
        .request("GET", "health", &[], None)
        .unwrap_or(Value::Null);
    let max_count = limits["limits"]["max_ops_per_push"]
        .as_u64()
        .unwrap_or(1000)
        .clamp(1, 1000) as usize;
    let max_bytes = limits["limits"]["max_body_bytes"]
        .as_u64()
        .unwrap_or(4 * 1024 * 1024)
        .clamp(1024, 4 * 1024 * 1024) as usize;
    let mut accepted = 0u64;
    let mut duplicate = 0u64;
    let mut pages = 0;
    let started = Instant::now();
    loop {
        if pages >= 10000 || started.elapsed() > Duration::from_secs(600) {
            return Err(Error::TooLarge);
        }
        let mut stmt = conn.prepare(&format!(
            "{SELECT} WHERE machine_id=? AND seq>? ORDER BY seq LIMIT ?"
        ))?;
        let mut window = Vec::new();
        let mut budget = 0usize;
        for row in stmt.query_map(params![machine, cursor, max_count as i64], wire)? {
            let op = row?;
            budget += serde_json::to_vec(&op)
                .map_err(|_| Error::InvalidRequest)?
                .len();
            if budget > RESPONSE_BYTES {
                return Err(Error::TooLarge);
            }
            window.push(op);
        }
        if window.is_empty() {
            break;
        }
        let mut batch = Vec::new();
        let mut seqs = Vec::new();
        for mut op in window {
            let seq = op["seq"].as_i64().ok_or(Error::InvalidRequest)?;
            op.as_object_mut().unwrap().remove("seq");
            op.as_object_mut().unwrap().remove("machine_id");
            op["created"] = json!(time::OffsetDateTime::parse(
                op["created"].as_str().unwrap_or(""),
                &time::format_description::well_known::Rfc3339
            )
            .ok()
            .and_then(|date| date
                .to_offset(time::UtcOffset::UTC)
                .format(&time::format_description::well_known::Rfc3339)
                .ok())
            .unwrap_or_else(crate::utcnow));
            batch.push(op);
            if serde_json::to_vec(&json!({"machine_id":machine,"ops":batch}))
                .map_err(|_| Error::InvalidRequest)?
                .len()
                > max_bytes
            {
                batch.pop();
                if batch.is_empty() {
                    return Err(Error::TooLarge);
                }
                break;
            }
            seqs.push(seq);
        }
        let answer = transport.request(
            "POST",
            "ops",
            &[],
            Some(&json!({"machine_id":machine,"ops":batch})),
        );
        let (reply, error) = match answer {
            Ok(reply) => (reply, None),
            Err(e) => {
                let advance = e.status == 409
                    && matches!(e.code.as_str(), "machine_seq_gap" | "machine_seq_conflict");
                if !advance {
                    note(&conn, &transport.peer, None, None, Some(&e.code))?;
                    return Err(Error::Unavailable);
                }
                (e.body, Some(e.code))
            }
        };
        let a = reply["accepted"].as_u64().ok_or(Error::InvalidRequest)?;
        let d = reply["duplicate"].as_u64().ok_or(Error::InvalidRequest)?;
        let settled = a
            .checked_add(d)
            .filter(|n| *n <= seqs.len() as u64)
            .ok_or(Error::InvalidRequest)? as usize;
        if settled > 0 {
            cursor = seqs[settled - 1]
        }
        accepted += a;
        duplicate += d;
        pages += 1;
        note(&conn, &transport.peer, Some(cursor), None, error.as_deref())?;
        if let Some(error) = error {
            return Ok(
                json!({"accepted":accepted,"duplicate":duplicate,"pages":pages,"pushed_seq":cursor,"error":error,"partial":true}),
            );
        }
        if settled != seqs.len() {
            return Err(Error::Changed);
        }
    }
    Ok(json!({"accepted":accepted,"duplicate":duplicate,"pages":pages,"pushed_seq":cursor}))
}
pub fn ops_page(cfg: &Config, since: i64, limit: usize, exclude: Option<&str>) -> Result<Value> {
    if since < 0 || limit == 0 {
        return Err(Error::InvalidRequest);
    }
    let conn = store::connect(cfg)?;
    let mut stmt = conn.prepare(&format!("{SELECT} WHERE seq>? ORDER BY seq LIMIT ?"))?;
    let mut last = since;
    let mut ops = Vec::new();
    let mut budget = 0usize;
    for row in stmt.query_map(params![since, limit.min(500) as i64], wire)? {
        let mut op = row?;
        last = op["seq"].as_i64().ok_or(Error::InvalidRequest)?;
        if exclude.is_some_and(|e| op["machine_id"] == e) {
            continue;
        }
        let seq = op.as_object_mut().unwrap().remove("seq").unwrap();
        op["hub_seq"] = seq;
        budget += serde_json::to_vec(&op)
            .map_err(|_| Error::InvalidRequest)?
            .len();
        if budget > RESPONSE_BYTES - 1024 {
            return Err(Error::TooLarge);
        }
        ops.push(op)
    }
    let more = conn
        .query_row("SELECT 1 FROM sync_ops WHERE seq>? LIMIT 1", [last], |r| {
            r.get::<_, i64>(0)
        })
        .optional()?
        .is_some();
    Ok(json!({"ops":ops,"next":if more{Some(last)}else{None}}))
}
fn enabled(cfg: &Config, class: &str) -> bool {
    cfg.sync.enabled
        && cfg.sync.classes.contains(match class {
            "belief" => "beliefs",
            "skill" => "skills",
            other => other,
        })
}
pub fn export_bundle(cfg: &Config, path: &Path) -> Result<Value> {
    let key = cfg.sync.key.as_deref().ok_or(Error::Untrusted)?;
    let conn = store::connect(cfg)?;
    let classes = PORTABLE
        .iter()
        .filter(|c| enabled(cfg, c))
        .map(|s| s.to_string())
        .collect::<BTreeSet<_>>();
    let mut stmt = conn.prepare(&format!(
        "{SELECT} WHERE applied IN (0,1) AND class IN ('memory','filemap','belief','pending','skill') ORDER BY lamport,machine_id,machine_seq LIMIT 100001"
    ))?;
    let mut ops = Vec::new();
    let mut budget = 0;
    for (index, row) in stmt.query_map([], wire)?.enumerate() {
        if index >= MAX_OPS {
            return Err(Error::TooLarge);
        }
        let mut op = row?;
        if !classes.contains(op["class"].as_str().unwrap_or("")) {
            continue;
        }
        op.as_object_mut().unwrap().remove("seq");
        if !sync_apply::verify_mac(&op, Some(key)) {
            return Err(Error::Untrusted);
        }
        budget += serde_json::to_vec(&op)
            .map_err(|_| Error::InvalidRequest)?
            .len();
        if ops.len() >= MAX_OPS || budget > BUNDLE_BYTES {
            return Err(Error::TooLarge);
        }
        ops.push(op)
    }
    let sha = crate::digest(&store::canonical_bytes(&json!(ops))?);
    let bundle = json!({"format":"lore-manual-transfer","version":1,"classes":classes,"count":ops.len(),"sha256":sha,"ops":ops});
    let bytes = serde_json::to_vec_pretty(&bundle).map_err(|_| Error::Unavailable)?;
    if bytes.len() > BUNDLE_BYTES {
        return Err(Error::TooLarge);
    }
    exclusive_write(path, &bytes)?;
    Ok(json!({"count":ops.len(),"classes":classes,"path":path}))
}
fn exclusive_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let directory = files::open_directory(path.parent().ok_or(Error::UnsafePath)?)?;
    let name = path.file_name().ok_or(Error::UnsafePath)?;
    let temp_name = format!(".lore-transfer-{}", uuid::Uuid::new_v4());
    let temp = std::ffi::OsStr::new(&temp_name);
    let result = (|| {
        let mut file = files::create_private_file(&directory, temp, true)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        files::rename_at(&directory, temp, &directory, name, false)?;
        directory.sync_all()?;
        Ok(())
    })();
    let _ = files::unlink_at(&directory, temp);
    result
}
pub fn import_bundle(cfg: &Config, path: &Path) -> Result<Value> {
    if cfg.sync.key.is_none() {
        return Err(Error::Untrusted);
    }
    let raw = files::read_regular(path, BUNDLE_BYTES)?;
    let bundle: Value = serde_json::from_slice(&raw).map_err(|_| Error::InvalidRequest)?;
    if bundle["format"] != "lore-manual-transfer" || bundle["version"] != 1 {
        return Err(Error::InvalidRequest);
    }
    let ops = bundle["ops"]
        .as_array()
        .filter(|o| o.len() <= MAX_OPS)
        .ok_or(Error::InvalidRequest)?;
    if bundle["count"].as_u64() != Some(ops.len() as u64) {
        return Err(Error::InvalidRequest);
    }
    let classes = bundle["classes"].as_array().ok_or(Error::InvalidRequest)?;
    let mut seen = BTreeSet::new();
    for class in classes {
        let class = class
            .as_str()
            .filter(|s| PORTABLE.contains(s))
            .ok_or(Error::InvalidRequest)?;
        if !seen.insert(class) {
            return Err(Error::InvalidRequest);
        }
    }
    let mut ids = BTreeSet::new();
    for op in ops {
        if !seen.contains(op["class"].as_str().unwrap_or(""))
            || !ids.insert(
                op["op_id"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or(Error::InvalidRequest)?,
            )
            || !op["created"].is_string()
            || !op["payload"].is_object()
        {
            return Err(Error::InvalidRequest);
        }
    }
    if bundle["sha256"] != crate::digest(&store::canonical_bytes(&json!(ops))?) {
        return Err(Error::Changed);
    }
    // Validate structural envelopes before touching the receiving store; an invalid
    // MAC intentionally follows the canonical unverified pending review gate.
    for op in ops {
        validate_envelope(op)?;
    }
    let mut report = json!({});
    for (index, chunk) in sync_apply::canonical_order(ops).chunks(512).enumerate() {
        let result = sync_apply::apply_ops(cfg, chunk).map_err(|error| {
            if index > 0 {
                Error::MayHaveApplied
            } else {
                error
            }
        })?;
        for (k, v) in result.as_object().ok_or(Error::Unavailable)? {
            report[k] =
                json!(report[k].as_u64().unwrap_or(0) + v.as_u64().ok_or(Error::Unavailable)?);
        }
    }
    Ok(report)
}
fn validate_envelope(op: &Value) -> Result<()> {
    for key in ["op_id", "machine_id", "class", "op"] {
        if !op[key]
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.len() <= 128 && !s.contains('\0'))
        {
            return Err(Error::InvalidRequest);
        }
    }
    for key in ["machine_seq", "lamport"] {
        if !op[key].as_i64().is_some_and(|n| n >= 1) {
            return Err(Error::InvalidRequest);
        }
    }
    if !op["payload"].is_object()
        || op.get("project_key").is_none()
        || !matches!(op["project_key"], Value::Null | Value::String(_))
        || !matches!(op["mac"], Value::Null | Value::String(_))
        || !op["created"].is_string()
    {
        return Err(Error::InvalidRequest);
    }
    Ok(())
}

#[derive(Clone)]
pub struct PeerServer {
    pub cfg: Config,
    pub machine_id: String,
    pub loopback: bool,
    pub auth: String,
    pub allow: BTreeSet<String>,
    pub secret: Option<String>,
}
impl PeerServer {
    pub fn new(cfg: Config, loopback: bool) -> Result<Self> {
        let auth = std::env::var("LORE_SYNC_PEER_AUTH").unwrap_or_else(|_| "tailscale".into());
        if !matches!(auth.as_str(), "tailscale" | "none") || !loopback && auth != "none" {
            return Err(Error::Untrusted);
        }
        let allow = std::env::var("LORE_SYNC_PEER_ALLOW")
            .unwrap_or_default()
            .split(',')
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty())
            .collect();
        let secret = std::env::var("LORE_SYNC_PEER_SECRET")
            .ok()
            .filter(|s| !s.is_empty());
        if auth != "none" && secret.is_none() {
            return Err(Error::Untrusted);
        }
        let conn = store::connect(&cfg)?;
        let machine_id = store::machine_id(&conn)?;
        Ok(Self {
            cfg,
            machine_id,
            loopback,
            auth,
            allow,
            secret,
        })
    }
    pub fn handle(
        &self,
        method: &str,
        target: &str,
        headers: &BTreeMap<String, String>,
    ) -> Result<(u16, Value)> {
        let url =
            Url::parse(&format!("http://localhost{target}")).map_err(|_| Error::InvalidRequest)?;
        let path = url.path();
        if method == "GET" && path == "/v1/health" {
            let conn = store::connect(&self.cfg)?;
            let max = conn.query_row("SELECT max(seq) FROM sync_ops", [], |r| {
                r.get::<_, Option<i64>>(0)
            })?;
            return Ok((
                200,
                json!({"ok":true,"version":env!("CARGO_PKG_VERSION"),"hub_seq_max":max}),
            ));
        }
        if let Some(secret) = &self.secret {
            use hmac::{Hmac, Mac};
            let mut mac = Hmac::<sha2::Sha256>::new_from_slice(b"lore-peer-secret-compare")
                .map_err(|_| Error::Unavailable)?;
            mac.update(format!("Bearer {secret}").as_bytes());
            let expected = mac.finalize().into_bytes();
            let mut mac = Hmac::<sha2::Sha256>::new_from_slice(b"lore-peer-secret-compare")
                .map_err(|_| Error::Unavailable)?;
            mac.update(
                headers
                    .get("authorization")
                    .map(String::as_str)
                    .unwrap_or("")
                    .trim()
                    .as_bytes(),
            );
            if mac.verify_slice(&expected).is_err() {
                return Ok((401, json!({"error":"unauthenticated"})));
            }
        }
        let login = headers
            .get("tailscale-user-login")
            .map(|s| s.trim())
            .unwrap_or("");
        if !login.is_empty() && !self.loopback || self.auth != "none" && login.is_empty() {
            return Ok((401, json!({"error":"unauthenticated"})));
        }
        if self.auth != "none"
            && !self.allow.is_empty()
            && !self.allow.contains(&login.to_lowercase())
        {
            return Ok((403, json!({"error":"forbidden"})));
        }
        match (method, path) {
            ("GET", "/v1/whoami") => Ok((
                200,
                json!({"account":"peer","machine_id":self.machine_id,"auth":self.auth,"trust":if self.loopback{"same-user (loopback identity headers)"}else{"public listener"},"shared_secret":self.secret.is_some()}),
            )),
            (_, "/v1/snapshot") => Ok((501, json!({"error":"not_implemented"}))),
            ("GET", "/v1/ops") => {
                let mut query = BTreeMap::new();
                for (k, v) in url.query_pairs() {
                    if query.insert(k.into_owned(), v.into_owned()).is_some() {
                        return Ok((400, json!({"error":"bad_request"})));
                    }
                }
                let since = query.get("since").map(|s| s.parse::<i64>()).transpose();
                let limit = query.get("limit").map(|s| s.parse::<usize>()).transpose();
                let (Ok(since), Ok(limit)) = (since, limit) else {
                    return Ok((400, json!({"error":"bad_request"})));
                };
                let since = since.unwrap_or(0);
                let limit = limit.unwrap_or(500);
                if since < 0 || limit == 0 {
                    return Ok((400, json!({"error":"bad_request"})));
                }
                Ok((
                    200,
                    ops_page(
                        &self.cfg,
                        since,
                        limit,
                        query.get("exclude").map(String::as_str),
                    )?,
                ))
            }
            (_, "/v1/ops") => Ok((405, json!({"error":"method_not_allowed"}))),
            _ => Ok((404, json!({"error":"not_found"}))),
        }
    }
    pub fn serve(&self, listener: TcpListener) -> Result<()> {
        listener.set_nonblocking(true)?;
        let server = std::sync::Arc::new(self.clone());
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .max_blocking_threads(8)
            .enable_all()
            .build()?;
        runtime.block_on(async move {
            let listener=tokio::net::TcpListener::from_std(listener)?;
            let permits=std::sync::Arc::new(tokio::sync::Semaphore::new(32));
            loop {
                tokio::select! {
                    signal=tokio::signal::ctrl_c()=>{signal?;break},
                    accepted=listener.accept()=>{
                        let (stream,_)=accepted?;
                        let Ok(permit)=permits.clone().try_acquire_owned() else {drop(stream);continue};
                        let server=server.clone();
                        tokio::spawn(async move {let _permit=permit;let _=server.http_connection(stream).await;});
                    }
                }
            }
            Ok(())
        })
    }
    /// Real-socket fixture and embedding entry point, using the same HTTP
    /// implementation and resource policy as the production listener.
    pub fn connection(&self, stream: TcpStream) -> Result<()> {
        stream.set_nonblocking(true)?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .max_blocking_threads(8)
            .build()?;
        let server = std::sync::Arc::new(self.clone());
        runtime.block_on(async move {
            let stream = tokio::net::TcpStream::from_std(stream)?;
            server.http_connection(stream).await
        })
    }
    async fn http_connection(
        self: std::sync::Arc<Self>,
        stream: tokio::net::TcpStream,
    ) -> Result<()> {
        let service =
            hyper::service::service_fn(move |request: hyper::Request<hyper::body::Incoming>| {
                let server = self.clone();
                async move {
                    let response = server.http_request(request).await;
                    Ok::<_, std::convert::Infallible>(response)
                }
            });
        let mut builder = hyper::server::conn::http1::Builder::new();
        builder
            .timer(hyper_util::rt::TokioTimer::new())
            .header_read_timeout(Duration::from_secs(5))
            .max_headers(100)
            .max_buf_size(16384)
            .keep_alive(false);
        let connection = builder.serve_connection(hyper_util::rt::TokioIo::new(stream), service);
        tokio::time::timeout(Duration::from_secs(15), connection)
            .await
            .map_err(|_| Error::Timeout)?
            .map_err(|_| Error::InvalidRequest)?;
        Ok(())
    }
    async fn http_request(
        self: std::sync::Arc<Self>,
        request: hyper::Request<hyper::body::Incoming>,
    ) -> hyper::Response<http_body_util::Full<hyper::body::Bytes>> {
        let mut headers = BTreeMap::new();
        let mut invalid = false;
        for (key, value) in request.headers() {
            let Ok(value) = value.to_str() else {
                invalid = true;
                break;
            };
            if headers
                .insert(key.as_str().to_ascii_lowercase(), value.to_owned())
                .is_some()
            {
                invalid = true;
                break;
            }
        }
        let method = request.method().as_str().to_owned();
        let target = request
            .uri()
            .path_and_query()
            .map(|p| p.as_str())
            .unwrap_or("/")
            .to_owned();
        // Transport B is pull-only. Refusing request bodies before reading them
        // gives authenticated and anonymous callers the same finite boundary.
        let has_body = request.headers().contains_key("transfer-encoding")
            || request
                .headers()
                .get("content-length")
                .is_some_and(|v| v.to_str().ok().and_then(|s| s.parse::<u64>().ok()) != Some(0));
        let result =
            if invalid || request.uri().authority().is_some() || request.uri().scheme().is_some() {
                Ok((400, json!({"error":"bad_request"})))
            } else if has_body && method == "GET" {
                Ok((413, json!({"error":"payload_too_large"})))
            } else {
                tokio::task::spawn_blocking(move || self.handle(&method, &target, &headers))
                    .await
                    .unwrap_or(Err(Error::Unavailable))
            };
        let (status, value) = result.unwrap_or_else(|error| {
            (
                if error == Error::TooLarge { 413 } else { 500 },
                json!({"error":error.code()}),
            )
        });
        let raw = serde_json::to_vec(&value)
            .unwrap_or_else(|_| b"{\"error\":\"operation_failed\"}".to_vec());
        let (status, raw) = if raw.len() > RESPONSE_BYTES {
            (413, b"{\"error\":\"output_too_large\"}".to_vec())
        } else {
            (status, raw)
        };
        hyper::Response::builder()
            .status(status)
            .header("content-type", "application/json")
            .header("connection", "close")
            .body(http_body_util::Full::new(hyper::body::Bytes::from(raw)))
            .expect("fixed HTTP response headers")
    }
}
