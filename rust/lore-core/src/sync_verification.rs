//! Isolated recovery evidence. This module is used by the verifier example and
//! its integration tests without adding a production command or network route.
use lore_core::{config::Config, files, store, sync_apply, sync_network, Error, Result};
use rusqlite::{types::ValueRef, Connection, OpenFlags, OptionalExtension};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    io::Read,
    net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};

const MAX_OPS: usize = 100_000;
const MAX_BYTES: usize = 128 * 1024 * 1024;
const MAX_DATABASE_BYTES: usize = 256 * 1024 * 1024;

fn file_digest(path: &Path) -> Result<String> {
    let mut file = files::open_regular(path, MAX_DATABASE_BYTES)?;
    let mut hash = Sha256::new();
    let mut bytes = [0; 65536];
    let mut size = 0;
    loop {
        let n = file.read(&mut bytes)?;
        if n == 0 {
            break;
        }
        size += n;
        if size > MAX_DATABASE_BYTES {
            return Err(Error::TooLarge);
        }
        hash.update(&bytes[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn snapshot(path: &Path) -> Result<Connection> {
    // Immutable is appropriate only for a retained standalone backup. Refuse
    // the live store name and journal sidecars rather than ignoring its WAL.
    if path.file_name().is_none_or(|s| s == "state.db") {
        return Err(Error::UnsafePath);
    }
    for suffix in ["-wal", "-shm", "-journal"] {
        if Path::new(&format!("{}{suffix}", path.display())).try_exists()? {
            return Err(Error::UnsafePath);
        }
    }
    let raw = path.to_str().ok_or(Error::UnsafePath)?;
    let escaped = raw
        .replace('%', "%25")
        .replace('?', "%3F")
        .replace('#', "%23");
    let conn = Connection::open_with_flags(
        format!("file:{escaped}?immutable=1"),
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    conn.set_limit(
        rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH,
        8 * 1024 * 1024,
    )?;
    conn.pragma_update(None, "query_only", true)?;
    Ok(conn)
}

fn operations(conn: &Connection) -> Result<Vec<Value>> {
    let mut statement = conn.prepare("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created FROM sync_ops ORDER BY lamport,machine_id,machine_seq LIMIT 100001")?;
    let mut rows = statement.query([])?;
    let mut ops = Vec::new();
    let mut bytes = 0;
    let mut ids = BTreeSet::new();
    let mut slots = BTreeSet::new();
    while let Some(row) = rows.next()? {
        if ops.len() == MAX_OPS {
            return Err(Error::TooLarge);
        }
        let payload: String = row.get(7)?;
        let op = json!({"op_id":row.get::<_,String>(0)?,"machine_id":row.get::<_,String>(1)?,
            "machine_seq":row.get::<_,i64>(2)?,"lamport":row.get::<_,i64>(3)?,
            "class":row.get::<_,String>(4)?,"op":row.get::<_,String>(5)?,
            "project_key":row.get::<_,Option<String>>(6)?,
            "payload":serde_json::from_str::<Value>(&payload).map_err(|_|Error::InvalidRequest)?,
            "mac":row.get::<_,Option<String>>(8)?,"created":row.get::<_,String>(9)?});
        sync_apply::validate_envelope(&op)?;
        if !op["machine_seq"].as_i64().is_some_and(|n| n > 0)
            || !op["lamport"].as_i64().is_some_and(|n| n > 0)
            || !ids.insert(op["op_id"].as_str().unwrap().to_owned())
            || !slots.insert((
                op["machine_id"].as_str().unwrap().to_owned(),
                op["machine_seq"].as_i64().unwrap(),
            ))
        {
            return Err(Error::InvalidRequest);
        }
        bytes += serde_json::to_vec(&op)
            .map_err(|_| Error::InvalidRequest)?
            .len();
        if bytes > MAX_BYTES {
            return Err(Error::TooLarge);
        }
        ops.push(op);
    }
    if ops.is_empty() {
        return Err(Error::InvalidRequest);
    }
    Ok(ops)
}

fn log_digest(ops: &[Value]) -> Result<String> {
    Ok(lore_core::digest(&store::canonical_bytes_bounded(
        &json!(ops),
        MAX_BYTES,
    )?))
}

fn canonical_rows(conn: &Connection, sql: &str) -> Result<Vec<Vec<u8>>> {
    let mut statement = conn.prepare(sql)?;
    let columns = statement.column_count();
    let mut rows = statement.query([])?;
    let mut canonical = Vec::new();
    let mut bytes = 0;
    while let Some(row) = rows.next()? {
        if canonical.len() == MAX_OPS {
            return Err(Error::TooLarge);
        }
        let mut values = Vec::new();
        for column in 0..columns {
            values.push(match row.get_ref(column)? {
                ValueRef::Null => Value::Null,
                ValueRef::Integer(n) => json!(n),
                ValueRef::Real(n) => json!(n),
                ValueRef::Text(s) => {
                    json!(std::str::from_utf8(s).map_err(|_| Error::InvalidRequest)?)
                }
                ValueRef::Blob(_) => return Err(Error::InvalidRequest),
            });
        }
        let encoded = store::canonical_bytes_bounded(&json!(values), MAX_BYTES)?;
        bytes += encoded.len();
        if bytes > MAX_BYTES {
            return Err(Error::TooLarge);
        }
        canonical.push(encoded);
    }
    canonical.sort();
    Ok(canonical)
}

fn row_hash(rows: &[Vec<u8>]) -> Value {
    let mut hash = Sha256::new();
    for row in rows {
        hash.update((row.len() as u64).to_be_bytes());
        hash.update(row);
    }
    json!({"count":rows.len(),"sha256":format!("{:x}",hash.finalize())})
}

fn rows_digest(conn: &Connection, sql: &str) -> Result<Value> {
    Ok(row_hash(&canonical_rows(conn, sql)?))
}

fn row_differences(source: &Connection, destination: &Connection, sql: &str) -> Result<Value> {
    let source = canonical_rows(source, sql)?;
    let destination = canonical_rows(destination, sql)?;
    let (mut a, mut b, mut common) = (0, 0, 0);
    let mut source_only = Vec::new();
    let mut destination_only = Vec::new();
    while a < source.len() || b < destination.len() {
        if a == source.len() || b < destination.len() && destination[b] < source[a] {
            destination_only.push(destination[b].clone());
            b += 1;
        } else if b == destination.len() || source[a] < destination[b] {
            source_only.push(source[a].clone());
            a += 1;
        } else {
            common += 1;
            a += 1;
            b += 1;
        }
    }
    Ok(
        json!({"common_count":common,"source_only":row_hash(&source_only),"destination_only":row_hash(&destination_only)}),
    )
}

fn portable_queries() -> [(&'static str, String); 8] {
    let project = |column: &str| {
        format!("coalesce((SELECT project_key FROM sync_projects WHERE slug={column}),{column})")
    };
    let subject = format!(
        "CASE WHEN b.subject LIKE 'project:%' THEN 'project:'||{} ELSE b.subject END",
        project("substr(b.subject,9)")
    );
    [
        ("beliefs", format!("SELECT b.uid,{subject},b.claim,b.confidence,b.status,s.uid,b.resolution,b.writer,b.via,b.source_engine FROM beliefs b LEFT JOIN beliefs s ON s.id=b.superseded_by")),
        ("evidence",format!("SELECT b.uid,e.session_id,{},e.note,e.source_engine FROM belief_evidence e LEFT JOIN beliefs b ON b.id=e.belief_id",project("e.project"))),
        ("edges","SELECT s.uid,d.uid,e.rel,e.source,e.session_id,e.note FROM belief_edges e LEFT JOIN beliefs s ON s.id=e.src LEFT JOIN beliefs d ON d.id=e.dst".into()),
        ("assertions","SELECT s.uid,d.uid,e.rel,e.session_id FROM belief_edge_assertions e LEFT JOIN beliefs s ON s.id=e.src LEFT JOIN beliefs d ON d.id=e.dst".into()),
        ("outcomes","SELECT o.uid,b.uid,o.event,o.source,o.session_id,o.agent,o.note FROM belief_outcomes o LEFT JOIN beliefs b ON b.id=o.belief_id".into()),
        ("dream_reviewed","SELECT min(a.uid,b.uid),max(a.uid,b.uid) FROM dream_reviewed d LEFT JOIN beliefs a ON a.id=d.a LEFT JOIN beliefs b ON b.id=d.b".into()),
        ("sessions",format!("SELECT session_id,{},title,first_ts,last_ts,messages,engine FROM sessions",project("project"))),
        ("messages",format!("SELECT session_id,{},ts,role,content FROM msg",project("project"))),
    ]
}

/// Portable semantic fields, including UID joins rather than local row IDs.
/// Receiver-generated timestamps and local filesystem/session locations are
/// excluded. This is a named projection, not a digest of all possible state.
pub fn portable_database(conn: &Connection) -> Result<Value> {
    let mut report = json!({});
    for (name, sql) in portable_queries() {
        report[name] = rows_digest(conn, &sql)?;
    }
    Ok(report)
}

// Diagnose actual divergence without exposing rows or changing the exact gate.
// Anchoring field projections to the first column retains UID/session identity;
// these are diagnostics, while the full row projection remains authoritative.
fn differences(
    source: &Connection,
    destination: &Connection,
    expected: &Value,
    actual: &Value,
) -> Result<Value> {
    let mut report = json!({});
    for (name, sql) in portable_queries() {
        if expected[name] == actual[name] {
            continue;
        }
        let distinct = format!("SELECT DISTINCT * FROM ({sql})");
        let source_rows = rows_digest(source, &distinct)?;
        let destination_rows = rows_digest(destination, &distinct)?;
        report[name] = json!({"distinct_rows":{"source":source_rows,"destination":destination_rows,"matches":source_rows==destination_rows}});
        report[name]["row_multiset_difference"] = row_differences(source, destination, &sql)?;
        let labels: &[&str] = match name {
            "beliefs" => &[
                "uid",
                "subject",
                "claim",
                "confidence",
                "status",
                "superseded_by_uid",
                "resolution",
                "writer",
                "via",
                "source_engine",
            ],
            "evidence" => &[
                "belief_uid",
                "session_id",
                "project_key",
                "note",
                "source_engine",
            ],
            "messages" => &["session_id", "project_key", "timestamp", "role", "content"],
            _ => continue,
        };
        let columns = (0..labels.len())
            .map(|i| format!("c{i}"))
            .collect::<Vec<_>>()
            .join(",");
        let mut changed = Vec::new();
        for (index, label) in labels.iter().enumerate() {
            let field = format!("WITH p({columns}) AS ({sql}) SELECT c0,c{index} FROM p");
            let source_field = rows_digest(source, &field)?;
            let destination_field = rows_digest(destination, &field)?;
            if source_field != destination_field {
                changed.push(*label);
            }
            report[name]["fields"][*label] = json!({"source":source_field,"destination":destination_field,"matches":source_field==destination_field});
        }
        report[name]["field_multiset_differences"] = json!(changed);
    }
    Ok(report)
}

/// Owns a real native peer on an ephemeral IPv4 loopback socket. The bearer is
/// generated for this process only; no saved transport secret is consulted.
/// Drop stops admission, joins the bounded connection handler, then closes the
/// listener before temporary databases are removed, including on error paths.
struct LoopbackDelivery {
    transport: sync_network::Transport,
    source: Config,
    expected_cursor: i64,
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    task: Option<thread::JoinHandle<Result<usize>>>,
}

impl LoopbackDelivery {
    fn new(original: &Connection, mut source: Config) -> Result<Self> {
        // Native Transport honors proxy environment variables. Refuse them
        // rather than risk sending private loopback traffic to a proxy. The
        // CLI wrapper can unset these names for this process without reading
        // their values or changing saved signing configuration.
        let presence = lore_core::config::ResolvedSettings::default();
        for name in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ] {
            if presence.source(name).is_some() {
                return Err(Error::Untrusted);
            }
        }
        files::private_dir(&source.root)?;
        let directory = files::open_directory(&source.root)?;
        let mut create = std::fs::OpenOptions::new();
        create.read(true).write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            create.mode(0o600).custom_flags(libc::O_NOFOLLOW);
        }
        let file = create.open(source.root.join("state.db"))?;
        let mut target = Connection::open_with_flags(
            source.root.join("state.db"),
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )?;
        let backup = rusqlite::backup::Backup::new(original, &mut target)?;
        let started = Instant::now();
        loop {
            match backup.step(128)? {
                rusqlite::backup::StepResult::Done => break,
                rusqlite::backup::StepResult::More => {}
                _ => thread::sleep(Duration::from_millis(10)),
            }
            if started.elapsed() > Duration::from_secs(120) {
                return Err(Error::Timeout);
            }
        }
        drop(backup);
        drop(target);
        file.sync_all()?;
        directory.sync_all()?;
        source.sync.enabled = true;
        let conn = store::connect(&source)?;
        let machine_id = store::machine_id(&conn)?;
        let expected_cursor =
            conn.query_row("SELECT coalesce(max(seq),0) FROM sync_ops", [], |r| {
                r.get::<_, i64>(0)
            })?;
        drop(conn);
        let secret = uuid::Uuid::new_v4().to_string();
        let server = sync_network::PeerServer {
            cfg: source.clone(),
            machine_id,
            loopback: true,
            auth: "none".into(),
            allow: BTreeSet::new(),
            secret: Some(secret.clone()),
        };
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
        listener.set_nonblocking(true)?;
        let address = listener.local_addr()?;
        let transport = sync_network::Transport::new(
            &format!("http://{address}"),
            Some(secret),
            "peer:retained-replay-verifier".into(),
        )?;
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let task = thread::spawn(move || {
            let started = Instant::now();
            let mut requests = 0;
            while !stopped.load(Ordering::Acquire)
                && started.elapsed() < Duration::from_secs(600)
                && requests < 1024
            {
                match listener.accept() {
                    Ok((stream, _)) => {
                        server.connection(stream)?;
                        requests += 1;
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5))
                    }
                    Err(_) => return Err(Error::Unavailable),
                }
            }
            Ok(requests)
        });
        Ok(Self {
            transport,
            source,
            expected_cursor,
            address,
            stop,
            task: Some(task),
        })
    }

    fn cursor(&self, destination: &Config) -> Result<i64> {
        let conn = store::read_only(destination)?;
        let cursor = conn
            .query_row(
                "SELECT pulled_cursor FROM sync_peers WHERE peer=?",
                [self.transport.cursor_key()],
                |r| r.get::<_, Option<String>>(0),
            )
            .optional()?
            .flatten();
        cursor
            .as_deref()
            .unwrap_or("0")
            .parse()
            .map_err(|_| Error::InvalidRequest)
    }

    fn close(&mut self) -> Result<usize> {
        self.stop.store(true, Ordering::Release);
        self.task
            .take()
            .map(|task| task.join().map_err(|_| Error::Unavailable)?)
            .unwrap_or(Ok(0))
    }
}

impl Drop for LoopbackDelivery {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

fn states(conn: &Connection) -> Result<Value> {
    let mut output =
        json!({"deferred":0,"applied":0,"unverified":0,"unknown":0,"failed":0,"quarantined":0});
    let mut query = conn.prepare("SELECT applied,count(*) FROM sync_ops GROUP BY applied")?;
    for row in query.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))? {
        let (state, count) = row?;
        let key = match state {
            0 => "deferred",
            1 => "applied",
            2 => "unverified",
            3 => "unknown",
            4 => "failed",
            5 => "quarantined",
            _ => return Err(Error::InvalidRequest),
        };
        output[key] = json!(count);
    }
    Ok(output)
}

fn replay(cfg: &Config, ops: &[Value], started: Instant) -> Result<Value> {
    let mut totals = json!({});
    let mut start = 0;
    while start < ops.len() {
        if started.elapsed().as_secs() >= 600 {
            return Err(Error::Timeout);
        }
        let mut end = start;
        let mut bytes = 0;
        while end < ops.len() && end - start < sync_apply::MAX_PAGE {
            let size = serde_json::to_vec(&ops[end])
                .map_err(|_| Error::InvalidRequest)?
                .len();
            if size > sync_apply::MAX_PAGE_BYTES {
                return Err(Error::TooLarge);
            }
            if bytes + size > sync_apply::MAX_PAGE_BYTES {
                break;
            }
            bytes += size;
            end += 1;
        }
        let report = sync_apply::apply_ops(cfg, &ops[start..end])?;
        for (key, value) in report.as_object().ok_or(Error::Unavailable)? {
            totals[key] = json!(
                totals[key].as_u64().unwrap_or(0) + value.as_u64().ok_or(Error::Unavailable)?
            );
        }
        start = end;
    }
    sync_apply::retry_deferred(cfg)?;
    Ok(totals)
}

/// Verify all signatures before any replay, then run the canonical native
/// receiver twice inside a newly allocated private temporary store. Credentials
/// arrive in the native Config; no key parsing, signing, transport or approvals.
/// Only counts, hashes and fixed labels escape in the returned report.
pub fn verify_backup(
    source: &Path,
    credentials: &Config,
    temporary_parent: &Path,
) -> Result<Value> {
    verify_backup_inner(source, credentials, Some(temporary_parent), false)
}

/// Canonical native peer -> pull replay over IPv4 loopback, with an isolated
/// SQLite backup copy as the only served source. Saved signing configuration is
/// resolved by the caller; no operation is authored or re-signed.
pub fn verify_transport_backup(
    source: &Path,
    credentials: &Config,
    temporary_parent: &Path,
) -> Result<Value> {
    verify_backup_inner(source, credentials, Some(temporary_parent), true)
}

/// Read-only signature preflight for the host holding the original native
/// configuration. No temporary store or receiver is entered, even on success.
pub fn inspect_backup(source: &Path, credentials: &Config) -> Result<Value> {
    verify_backup_inner(source, credentials, None, false)
}

fn verify_backup_inner(
    source: &Path,
    credentials: &Config,
    temporary_parent: Option<&Path>,
    transport_mode: bool,
) -> Result<Value> {
    if credentials.sync.key.is_none() {
        return Err(Error::Untrusted);
    }
    let source_sha = file_digest(source)?;
    let conn = snapshot(source)?;
    let ops = operations(&conn)?;
    let verified = ops
        .iter()
        .filter(|op| sync_apply::verify_mac(op, credentials.sync.key.as_deref()))
        .count();
    let mut report = json!({"format":"lore-sync-replay-evidence","version":1,
        "engine_version":env!("CARGO_PKG_VERSION"),"mode":"offline-retained-database",
        "source":{"sha256":source_sha,"operations":ops.len(),"verified_macs":verified,"unverified_macs":ops.len()-verified,"log_sha256":log_digest(&ops)?},
        "replay_performed":false,"signature_gate":verified==ops.len(),"offline_replay_gate":false,"transport_replay_gate":false,"issue112_success_gate":false,
        "transport":{"exercised":false,"banked_cursor":null},
        "evidence_gaps":["No transport was contacted; a banked fresh-stream cursor is unproven.",
            "The database backup contains no contemporaneous memory, file-map, pending or skill file snapshot; full portable state parity is unproven."]});
    if temporary_parent.is_none() {
        report["mode"] = json!("inspect-retained-database");
    } else if transport_mode {
        report["mode"] = json!("loopback-peer-retained-database");
    }
    if verified != ops.len() || temporary_parent.is_none() {
        report["source"]["unchanged"] = json!(source_sha == file_digest(source)?);
        return Ok(report);
    }
    let expected = portable_database(&conn)?;
    let workspace = tempfile::Builder::new()
        .prefix("lore-replay-")
        .tempdir_in(temporary_parent.ok_or(Error::InvalidRequest)?)?;
    let mut destination = Config::for_root(workspace.path().join("destination"));
    destination.sync = credentials.sync.clone();
    destination.sync.enabled = true;
    destination.sync.classes = [
        "memory", "filemap", "beliefs", "pending", "skills", "sessions",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    // Config::for_root pins every writable path to the isolated workspace;
    // process overrides for skills, project paths and roots are not consulted.
    let started = Instant::now();
    let mut delivery = if transport_mode {
        let mut source_copy = Config::for_root(workspace.path().join("source"));
        source_copy.sync = destination.sync.clone();
        Some(LoopbackDelivery::new(&conn, source_copy)?)
    } else {
        None
    };
    let first = match delivery.as_ref().map_or_else(
        || replay(&destination, &ops, started),
        |peer| sync_network::pull(&destination, &peer.transport),
    ) {
        Ok(first) => first,
        Err(error) => {
            if let Some(peer) = &mut delivery {
                let requests = peer.close()?;
                let closed =
                    TcpStream::connect_timeout(&peer.address, Duration::from_millis(100)).is_err();
                report["transport"] = json!({"exercised":true,"listener_scope":"ipv4-loopback","error":error.code(),"delivery_gate":false,"banked_cursor":peer.cursor(&destination)?,"listener_closed":closed,"requests":requests});
                report["source"]["unchanged"] = json!(source_sha == file_digest(source)?);
                return Ok(report);
            }
            return Err(error);
        }
    };
    let first_cursor = delivery
        .as_ref()
        .map(|peer| peer.cursor(&destination))
        .transpose()?;
    let received = store::read_only(&destination)?;
    let final_states = states(&received)?;
    let retained = operations(&received)?;
    let actual = portable_database(&received)?;
    let diagnostic = differences(&conn, &received, &expected, &actual)?;
    let log_matches = log_digest(&ops)? == log_digest(&retained)?;
    drop(received);
    let repeated = delivery.as_ref().map_or_else(
        || replay(&destination, &ops, started),
        |peer| sync_network::pull_from(&destination, &peer.transport, Some(0), false),
    )?;
    let repeat_cursor = delivery
        .as_ref()
        .map(|peer| peer.cursor(&destination))
        .transpose()?;
    let mut transport_gate = false;
    if let Some(peer) = &mut delivery {
        let resumed = sync_network::pull(&destination, &peer.transport)?;
        let resumed_cursor = peer.cursor(&destination)?;
        let copied = store::read_only(&peer.source)?;
        let copy_preserved = log_digest(&ops)? == log_digest(&operations(&copied)?)?;
        drop(copied);
        let requests = peer.close()?;
        let closed = TcpStream::connect_timeout(&peer.address, Duration::from_millis(100)).is_err();
        let cursor_gate = first_cursor == Some(peer.expected_cursor)
            && repeat_cursor == first_cursor
            && resumed_cursor == peer.expected_cursor
            && first["cursor_advanced"] == true
            && repeated["cursor_advanced"] == true
            && resumed["cursor_advanced"] == true
            && resumed["fetched"] == 0
            && first["fetched"].as_u64() == Some(ops.len() as u64)
            && repeated["fetched"].as_u64() == Some(ops.len() as u64);
        transport_gate = cursor_gate && copy_preserved && closed;
        report["transport"] = json!({"exercised":true,"listener_scope":"ipv4-loopback",
            "authentication":"ephemeral-bearer","initial_cursor":0,"expected_cursor":peer.expected_cursor,
            "banked_cursor":first_cursor,"repeat_banked_cursor":repeat_cursor,"resumed_banked_cursor":resumed_cursor,
            "cursor_gate":cursor_gate,"source_copy_history_preserved":copy_preserved,
            "resumed":resumed,"requests":requests,"max_requests":1024,"max_listener_seconds":600,
            "max_connection_seconds":15,"listener_closed":closed});
    }
    let received = store::read_only(&destination)?;
    let idempotent = actual == portable_database(&received)?
        && log_digest(&retained)? == log_digest(&operations(&received)?)?
        && final_states == states(&received)?
        && repeated["duplicate"].as_u64() == Some(ops.len() as u64)
        && repeated["applied"] == 0;
    let unchanged = source_sha == file_digest(source)?;
    let settled = ["deferred", "unverified", "unknown", "failed"]
        .into_iter()
        .all(|key| final_states[key] == 0)
        && ["failed", "unknown", "skipped", "may_have_applied"]
            .into_iter()
            .all(|key| first[key] == 0);
    report["source"]["unchanged"] = json!(unchanged);
    report["replay_performed"] = json!(true);
    report["first_pass"] = first;
    report["durable_states"] = final_states;
    report["signed_history_preserved"] = json!(log_matches);
    report["portable_database"] = json!({"projection":"uid-semantic-v1","source":expected,"destination":actual,"matches":expected==actual,"differences":diagnostic});
    report["repeat"] = repeated;
    report["idempotent_database_and_log"] = json!(idempotent);
    let exact = settled && log_matches && expected == actual && idempotent && unchanged;
    if transport_mode {
        report["transport"]["delivery_gate"] =
            json!(transport_gate && settled && log_matches && idempotent && unchanged);
        report["transport_replay_gate"] = json!(exact && transport_gate);
        if report["transport"]["delivery_gate"] == true {
            report["evidence_gaps"] = json!(["The database backup contains no contemporaneous memory, file-map, pending or skill file snapshot; full portable state parity is unproven."]);
        }
    } else {
        report["offline_replay_gate"] = json!(exact);
    }
    // Drop the closed native peer before deleting either isolated database.
    drop(delivery);
    Ok(report)
}
