use lore_core::{
    config::Config,
    store,
    sync_network::{self as net, PeerServer, Transport},
    Error,
};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    thread,
};
const KEY: &str = "lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION";
fn config() -> (tempfile::TempDir, Config) {
    let t = tempfile::tempdir().unwrap();
    let mut c = Config::for_root(t.path().join("root"));
    c.sync.enabled = true;
    c.sync.key = Some(KEY.into());
    c.sync.classes = ["memory", "filemap", "beliefs", "pending", "skills"]
        .map(String::from)
        .into_iter()
        .collect();
    (t, c)
}
fn op(text: &str, lamport: i64) -> Value {
    let mut v = json!({"op_id":uuid::Uuid::new_v4().to_string(),"machine_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","machine_seq":lamport,"lamport":lamport,"class":"memory","op":"add","project_key":null,"payload":{"text":text,"writer":"approved","via":"test"},"created":"2026-09-28T00:00:00Z","mac":null});
    v["mac"] = json!(store::canonical_mac(
        &json!([
            v["op_id"],
            v["machine_id"],
            v["machine_seq"],
            v["lamport"],
            v["class"],
            v["op"],
            v["project_key"],
            v["payload"]
        ]),
        KEY
    )
    .unwrap());
    v
}
fn fixture(replies: Vec<(u16, Value)>) -> (String, thread::JoinHandle<Vec<String>>) {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    let h = thread::spawn(move || {
        let mut requests = Vec::new();
        for (status, value) in replies {
            let (mut s, _) = l.accept().unwrap();
            s.set_read_timeout(Some(std::time::Duration::from_secs(5)))
                .unwrap();
            let mut r = BufReader::new(s.try_clone().unwrap());
            let mut lines = String::new();
            let mut length = 0;
            loop {
                let mut line = String::new();
                r.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
                if line.to_lowercase().starts_with("content-length:") {
                    length = line.split_once(':').unwrap().1.trim().parse().unwrap()
                }
                lines.push_str(&line)
            }
            let mut body = vec![0; length];
            r.read_exact(&mut body).unwrap();
            lines.push_str(&String::from_utf8(body).unwrap());
            requests.push(lines);
            let body = serde_json::to_vec(&value).unwrap();
            write!(
                s,
                "HTTP/1.1 {status} reply\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            )
            .unwrap();
            s.write_all(&body).unwrap();
        }
        requests
    });
    (url, h)
}
#[test]
fn peer_auth_requires_secret_and_refuses_public_identity() {
    let (_t, cfg) = config();
    let server = PeerServer {
        machine_id: "own".into(),
        cfg,
        loopback: true,
        auth: "tailscale".into(),
        allow: ["owner@example.test".into()].into_iter().collect(),
        secret: Some("fixture-secret".into()),
    };
    let mut h = BTreeMap::from([("tailscale-user-login".into(), "owner@example.test".into())]);
    assert_eq!(server.handle("GET", "/v1/ops", &h).unwrap().0, 401);
    h.insert("authorization".into(), "Bearer fixture-secret".into());
    assert_eq!(server.handle("GET", "/v1/ops", &h).unwrap().0, 200);
    assert_eq!(server.handle("POST", "/v1/ops", &h).unwrap().0, 405);
    let public = PeerServer {
        loopback: false,
        ..server
    };
    assert_eq!(public.handle("GET", "/v1/ops", &h).unwrap().0, 401);
    assert_eq!(
        public
            .handle("GET", "/v1/health", &BTreeMap::new())
            .unwrap()
            .0,
        200
    );
}
#[test]
fn signed_bundle_roundtrip_preserves_gate_and_refuses_overwrite() {
    let (t, cfg) = config();
    let conn = store::connect(&cfg).unwrap();
    let op = op("Portable native fact", 1);
    let report = lore_core::sync_apply::apply_ops(&cfg, &[op]).unwrap();
    assert_eq!(report["applied"], 1);
    let path = t.path().join("bundle.json");
    assert_eq!(net::export_bundle(&cfg, &path).unwrap()["count"], 1);
    assert!(net::export_bundle(&cfg, &path).is_err());
    let (_t2, dest) = config();
    let imported = net::import_bundle(&dest, &path).unwrap();
    assert_eq!(imported["applied"], 1);
    assert_eq!(net::import_bundle(&dest, &path).unwrap()["duplicate"], 1);
    let mut bundle: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    bundle["ops"][0]["mac"] = Value::Null;
    bundle["sha256"] = json!(lore_core::digest(
        &store::canonical_bytes(&bundle["ops"]).unwrap()
    ));
    std::fs::write(&path, serde_json::to_vec(&bundle).unwrap()).unwrap();
    let (_t3, unverified) = config();
    assert_eq!(
        net::import_bundle(&unverified, &path).unwrap()["unverified"],
        1
    );
    assert!(!unverified.root.join("USER.md").exists());
    drop(conn);
}
#[test]
fn pull_drains_before_canonical_application_and_banks_cursor() {
    let (_t, cfg) = config();
    let mut a = op("Older fact", 1);
    let mut b = op("Later fact", 2);
    a["hub_seq"] = json!(2);
    b["hub_seq"] = json!(1);
    let (url, h) = fixture(vec![
        (200, json!({"ops":[b],"next":1})),
        (200, json!({"ops":[a],"next":null})),
    ]);
    let transport = Transport::new(&url, None, "peer:test".into()).unwrap();
    let report = net::pull(&cfg, &transport).unwrap();
    assert_eq!(report["applied"], 2);
    assert_eq!(report["drained_to"], 2);
    let conn = store::read_only(&cfg).unwrap();
    let text: String = conn
        .query_row(
            "SELECT pulled_cursor FROM sync_peers WHERE peer='peer:test'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(text, "2");
    let requests = h.join().unwrap();
    assert!(requests[1].contains("since=1"));
    let rows = lore_core::memory::read_entries(&cfg.root.join("USER.md")).unwrap();
    assert_eq!(rows, ["Later fact", "Older fact"]);
    let stored: Vec<String> = conn
        .prepare("SELECT json_extract(payload, '$.text') FROM sync_ops ORDER BY seq")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<std::result::Result<_, _>>()
        .unwrap();
    assert_eq!(stored, ["Older fact", "Later fact"]);
}
#[test]
fn partial_push_conflict_banks_only_settled_prefix() {
    let (_t, cfg) = config();
    let mut conn = store::connect(&cfg).unwrap();
    let tx = conn.transaction().unwrap();
    for text in ["one", "two"] {
        store::append_op(&cfg, &tx, "memory", "add", None, &json!({"text":text})).unwrap()
    }
    tx.commit().unwrap();
    let (url, h) = fixture(vec![
        (200, json!({"ok":true})),
        (
            409,
            json!({"error":"machine_seq_gap","accepted":1,"duplicate":0,"expected":3,"got":4}),
        ),
    ]);
    let transport = Transport::new(&url, Some("fixture-token".into()), "hub".into()).unwrap();
    let report = net::push(&cfg, &transport, None).unwrap();
    assert_eq!(report["accepted"], 1);
    assert_eq!(report["pushed_seq"], 1);
    assert_eq!(report["error"], "machine_seq_gap");
    assert_eq!(
        conn.query_row(
            "SELECT pushed_seq FROM sync_peers WHERE peer='hub'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    let requests = h.join().unwrap();
    assert!(!requests[0].to_lowercase().contains("authorization:"));
    assert!(requests[1]
        .to_lowercase()
        .contains("authorization: bearer fixture-token"));
}
#[test]
fn op_id_conflict_never_advances_push_cursor() {
    let (_t, cfg) = config();
    let mut conn = store::connect(&cfg).unwrap();
    let tx = conn.transaction().unwrap();
    store::append_op(&cfg, &tx, "memory", "add", None, &json!({"text":"one"})).unwrap();
    tx.commit().unwrap();
    let (url, h) = fixture(vec![
        (200, json!({"ok":true})),
        (
            409,
            json!({"error":"op_id_conflict","accepted":0,"duplicate":0}),
        ),
    ]);
    assert!(net::push(
        &cfg,
        &Transport::new(&url, None, "hub".into()).unwrap(),
        None
    )
    .is_err());
    assert_eq!(
        conn.query_row(
            "SELECT pushed_seq FROM sync_peers WHERE peer='hub'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
    h.join().unwrap();
}
#[test]
fn failed_drain_never_applies_or_advances() {
    let (_t, cfg) = config();
    let mut a = op("Do not apply partial drain", 1);
    a["hub_seq"] = json!(1);
    let (url, h) = fixture(vec![
        (200, json!({"ops":[a],"next":1})),
        (200, json!({"ops":[],"next":1})),
    ]);
    assert_eq!(
        net::pull(
            &cfg,
            &Transport::new(&url, None, "peer:test".into()).unwrap()
        ),
        Err(Error::InvalidRequest)
    );
    let conn = store::read_only(&cfg).unwrap();
    assert_eq!(
        conn.query_row("SELECT count(*) FROM sync_ops", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        conn.query_row(
            "SELECT pulled_cursor FROM sync_peers WHERE peer='peer:test'",
            [],
            |r| r.get::<_, Option<String>>(0)
        )
        .unwrap(),
        None
    );
    h.join().unwrap();
}
#[test]
fn native_peer_real_socket_serves_signed_wire() {
    let (_t, cfg) = config();
    lore_core::sync_apply::apply_ops(&cfg, &[op("socket fact", 1)]).unwrap();
    let server = PeerServer {
        machine_id: "own".into(),
        cfg,
        loopback: true,
        auth: "none".into(),
        allow: BTreeSet::new(),
        secret: Some("secret".into()),
    };
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let task = thread::spawn(move || server.connection(listener.accept().unwrap().0).unwrap());
    let transport = Transport::new(&url, Some("secret".into()), "peer:fixture".into()).unwrap();
    let response = transport.request("GET", "ops", &[], None).unwrap();
    assert_eq!(response["ops"].as_array().unwrap().len(), 1);
    assert!(lore_core::sync_apply::verify_mac(
        &response["ops"][0],
        Some(KEY)
    ));
    task.join().unwrap();
}
#[test]
fn failed_receipt_retains_cursor_and_retry_receives_missing_operation() {
    let (_t, mut cfg) = config();
    cfg.timeout = std::time::Duration::from_millis(20);
    let mut row = op("Retryable owned transport fact", 1);
    row["hub_seq"] = json!(1);
    let lock_path = cfg.root.join(format!(
        ".sync-op-{}",
        lore_core::sync_apply::deterministic_uid(row["op_id"].as_str().unwrap())
    ));
    let lock = lore_core::files::Locks::acquire(&cfg.root, &[lock_path], cfg.timeout).unwrap();
    let (url, h) = fixture(vec![
        (200, json!({"ops":[row.clone()],"next":null})),
        (200, json!({"ops":[row],"next":null})),
    ]);
    let transport = Transport::new(&url, None, "peer:retry".into()).unwrap();
    let first = net::pull(&cfg, &transport).unwrap();
    assert_eq!(first["failed"], 1);
    assert_eq!(first["cursor_advanced"], false);
    let conn = store::read_only(&cfg).unwrap();
    let cursor: String = conn
        .query_row(
            "SELECT coalesce(pulled_cursor,'0') FROM sync_peers WHERE peer='peer:retry'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(cursor, "0");
    assert_eq!(
        conn.query_row(
            "SELECT count(*) FROM sync_ops WHERE machine_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
    drop(lock);
    let second = net::pull(&cfg, &transport).unwrap();
    assert_eq!(second["applied"], 1);
    assert_eq!(second["cursor_advanced"], true);
    let requests = h.join().unwrap();
    assert!(requests.iter().all(|r| r.contains("since=0")));
    let cursor: String = conn
        .query_row(
            "SELECT coalesce(pulled_cursor,'0') FROM sync_peers WHERE peer='peer:retry'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(cursor, "1");
}
#[test]
fn session_start_schedules_only_native_pull_and_rate_limits_following_starts() {
    let (t, cfg) = config();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let server = thread::spawn(move || {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        let mut socket = loop {
            match listener.accept() {
                Ok((s, _)) => break s,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "native hook pull did not connect"
                    );
                    thread::sleep(std::time::Duration::from_millis(10));
                }
                Err(e) => panic!("owned fixture accept: {e}"),
            }
        };
        socket
            .set_read_timeout(Some(std::time::Duration::from_secs(2)))
            .unwrap();
        let mut reader = BufReader::new(socket.try_clone().unwrap());
        let mut request = String::new();
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if line == "\r\n" {
                break;
            }
            assert!(request.len() + line.len() < 16384);
            request.push_str(&line);
        }
        let payload = b"{\"ops\":[],\"next\":null}";
        write!(socket,"HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\nContent-Type: application/json\r\n\r\n",payload.len()).unwrap();
        socket.write_all(payload).unwrap();
        request
    });
    let hook = || {
        let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_lore-rs"))
            .args(["hook", "--engine", "claude", "--event", "session-start"])
            .env_clear()
            .env("HOME", t.path())
            .env("LORE_ROOT", &cfg.root)
            .env("LORE_PROJECTS_DIR", &cfg.projects)
            .env("LORE_CODEX_SESSIONS_DIR", &cfg.codex_sessions)
            .env("LORE_SYNC_PEER", &url)
            .env("LORE_CLAUDE_BIN", "/a/model/must/never/run/from/inject")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        write!(
            child.stdin.as_mut().unwrap(),
            "{}",
            json!({"cwd":t.path(),"session_id":"owned-pull-hook"})
        )
        .unwrap();
        drop(child.stdin.take());
        child.wait_with_output().unwrap()
    };
    let first = hook();
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    let request = server.join().unwrap();
    assert!(request.contains("GET /v1/ops?since=0"));
    let stamp = std::fs::read(cfg.root.join(".sync/pull")).unwrap();
    assert!(hook().status.success());
    assert_eq!(std::fs::read(cfg.root.join(".sync/pull")).unwrap(), stamp);
}
