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
    fixture_bytes(
        replies
            .into_iter()
            .map(|(status, value)| (status, serde_json::to_vec(&value).unwrap()))
            .collect(),
    )
}
fn fixture_bytes(replies: Vec<(u16, Vec<u8>)>) -> (String, thread::JoinHandle<Vec<String>>) {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    let h = thread::spawn(move || {
        let mut requests = Vec::new();
        for (status, response_body) in replies {
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
            write!(
                s,
                "HTTP/1.1 {status} reply\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                response_body.len()
            )
            .unwrap();
            s.write_all(&response_body).unwrap();
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

#[test]
fn oversized_push_splits_and_single_rejection_terminates() {
    let (_t, cfg) = config();
    let mut conn = store::connect(&cfg).unwrap();
    let tx = conn.transaction().unwrap();
    for text in ["first", "second", "third"] {
        store::append_op(&cfg, &tx, "memory", "add", None, &json!({"text":text})).unwrap();
    }
    tx.commit().unwrap();
    let (url, h) = fixture(vec![
        (200, json!({"limits":{"max_ops_per_push":3}})),
        (413, json!({"error":"payload_too_large"})),
        (200, json!({"accepted":1,"duplicate":0})),
        (200, json!({"accepted":1,"duplicate":0})),
        (200, json!({"accepted":1,"duplicate":0})),
    ]);
    let report = net::push(
        &cfg,
        &Transport::new(&url, Some("secret".into()), "hub".into()).unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(report["accepted"], 3);
    assert_eq!(report["pushed_seq"], 3);
    let requests = h.join().unwrap();
    let batches: Vec<Value> = requests[1..]
        .iter()
        .map(|s| serde_json::from_str(s.rsplit("\r\n").next().unwrap()).unwrap())
        .collect();
    assert_eq!(batches[0]["ops"].as_array().unwrap().len(), 3);
    assert!(batches[1..]
        .iter()
        .all(|b| b["ops"].as_array().unwrap().len() == 1));
    let ids: Vec<&Value> = batches[0]["ops"]
        .as_array()
        .unwrap()
        .iter()
        .map(|o| &o["op_id"])
        .collect();
    assert_eq!(batches[1]["ops"][0]["op_id"], *ids[0]);
    assert_eq!(batches[2]["ops"][0]["op_id"], *ids[1]);
    assert_eq!(batches[3]["ops"][0]["op_id"], *ids[2]);
    let (url, h) = fixture(vec![
        (200, json!({})),
        (413, json!({"error":"payload_too_large"})),
    ]);
    let transport = Transport::new(&url, None, "hub".into()).unwrap();
    assert!(net::push(&cfg, &transport, Some(2)).is_err());
    assert_eq!(
        conn.query_row(
            "SELECT pushed_seq FROM sync_peers WHERE peer='hub'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        3
    );
    assert_eq!(h.join().unwrap().len(), 2);
}

#[test]
fn push_respects_small_advertised_body_limit() {
    let (_t, cfg) = config();
    let mut conn = store::connect(&cfg).unwrap();
    let tx = conn.transaction().unwrap();
    for text in ["one", "two"] {
        store::append_op(&cfg, &tx, "memory", "add", None, &json!({"text":text})).unwrap();
    }
    tx.commit().unwrap();
    let (url, h) = fixture(vec![
        (200, json!({"limits":{"max_body_bytes":500}})),
        (200, json!({"accepted":1,"duplicate":0})),
        (200, json!({"accepted":1,"duplicate":0})),
    ]);
    assert_eq!(
        net::push(
            &cfg,
            &Transport::new(&url, None, "hub".into()).unwrap(),
            None
        )
        .unwrap()["accepted"],
        2
    );
    let requests = h.join().unwrap();
    for request in &requests[1..] {
        let body = request.rsplit("\r\n").next().unwrap();
        assert!(body.len() <= 500);
        let body: Value = serde_json::from_str(body).unwrap();
        assert_eq!(body["ops"].as_array().unwrap().len(), 1);
        assert!(body["ops"][0].get("machine_id").is_none());
    }
}

#[test]
fn all_sources_continue_and_hub_push_runs_after_failed_pull() {
    let (_t, cfg) = config();
    let mut conn = store::connect(&cfg).unwrap();
    let tx = conn.transaction().unwrap();
    store::append_op(
        &cfg,
        &tx,
        "memory",
        "add",
        None,
        &json!({"text":"authored here"}),
    )
    .unwrap();
    tx.commit().unwrap();
    let (hub, h) = fixture(vec![
        (
            401,
            json!({"error":"unauthenticated","message":"fixture-secret"}),
        ),
        (200, json!({})),
        (200, json!({"accepted":1,"duplicate":0})),
    ]);
    let mut remote = op("Independent source survives", 1);
    remote["hub_seq"] = json!(1);
    let (peer, p) = fixture(vec![(200, json!({"ops":[remote],"next":null}))]);
    let targets = vec![
        Transport::new(&hub, Some("fixture-secret".into()), "hub".into()).unwrap(),
        Transport::new(&peer, None, "peer:good".into()).unwrap(),
    ];
    let report = net::exchange(&cfg, &targets, true).unwrap();
    assert_eq!(report["failed"], 1);
    assert_eq!(report["peer:good"]["applied"], 1);
    assert_eq!(report["push"]["accepted"], 1);
    assert!(!serde_json::to_string(&report)
        .unwrap()
        .contains("fixture-secret"));
    assert_eq!(
        conn.query_row(
            "SELECT pulled_cursor FROM sync_peers WHERE peer='peer:good'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "1"
    );
    assert_eq!(
        conn.query_row(
            "SELECT pulled_cursor FROM sync_peers WHERE peer='hub'",
            [],
            |r| r.get::<_, Option<String>>(0)
        )
        .unwrap(),
        None
    );
    h.join().unwrap();
    p.join().unwrap();
}

#[test]
fn cursor_and_envelope_errors_never_apply_partial_drain() {
    for case in 0..3 {
        let (_t, cfg) = config();
        let mut first = op("Never apply framing errors", 1);
        first["hub_seq"] = json!(2);
        let mut second = op("Malformed later row", 2);
        second["hub_seq"] = json!(3);
        let reply = match case {
            0 => {
                second["hub_seq"] = json!(2);
                json!({"ops":[first,second],"next":null})
            }
            1 => json!({"ops":[first,second],"next":1}),
            _ => {
                second["machine_seq"] = json!(0);
                json!({"ops":[first,second],"next":null})
            }
        };
        let (url, h) = fixture(vec![(200, reply)]);
        assert_eq!(
            net::pull(
                &cfg,
                &Transport::new(&url, None, "peer:invalid".into()).unwrap()
            ),
            Err(Error::InvalidRequest)
        );
        let conn = store::read_only(&cfg).unwrap();
        assert_eq!(
            conn.query_row("SELECT count(*) FROM sync_ops", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert!(!cfg.root.join("USER.md").exists());
        h.join().unwrap();
    }
}

#[test]
fn large_bundle_uses_receiver_byte_budget_and_empty_report_is_complete() {
    let (t, cfg) = config();
    let path = t.path().join("large.json");
    let (_source_temp, source) = config();
    for n in 1..=18 {
        let mut row = op(&format!("large fixture {n}"), n);
        // Unknown payload members are signed and preserved by the wire contract.
        row["payload"]["padding"] = json!("x".repeat(500_000));
        row["mac"] = json!(store::canonical_mac(
            &json!([
                row["op_id"],
                row["machine_id"],
                row["machine_seq"],
                row["lamport"],
                row["class"],
                row["op"],
                row["project_key"],
                row["payload"]
            ]),
            KEY
        )
        .unwrap());
        assert_eq!(
            lore_core::sync_apply::apply_ops(&source, &[row]).unwrap()["applied"],
            1
        );
    }
    assert_eq!(net::export_bundle(&source, &path).unwrap()["count"], 18);
    assert!(std::fs::metadata(&path).unwrap().len() > 8 * 1024 * 1024);
    let report = net::import_bundle(&cfg, &path).unwrap();
    assert_eq!(report["applied"], 18);
    assert_eq!(report["failed"], 0);
    let empty = t.path().join("empty.json");
    let (_t2, src) = config();
    net::export_bundle(&src, &empty).unwrap();
    let report = net::import_bundle(&cfg, &empty).unwrap();
    assert_eq!(report["unverified"], 0);
    assert_eq!(report.as_object().unwrap().len(), 9);
}

#[test]
fn peer_identity_alone_never_authenticates_embedded_server() {
    let (_t, cfg) = config();
    let server = PeerServer {
        cfg,
        machine_id: "fixture".into(),
        loopback: true,
        auth: "tailscale".into(),
        allow: BTreeSet::new(),
        secret: None,
    };
    let headers = BTreeMap::from([("tailscale-user-login".into(), "owner@example.test".into())]);
    assert_eq!(server.handle("GET", "/v1/ops", &headers).unwrap().0, 401);
    let public = PeerServer {
        auth: "none".into(),
        loopback: false,
        ..server
    };
    assert_eq!(
        public
            .handle("GET", "/v1/whoami", &BTreeMap::new())
            .unwrap()
            .1["trust"],
        "no authentication"
    );
}

#[test]
fn peer_cursor_keys_preserve_ports_paths_and_endpoint_aliases() {
    assert_eq!(
        Transport::peer("host.test").unwrap().peer,
        Transport::peer("http://host.test:8765/v1").unwrap().peer
    );
    assert_eq!(
        Transport::peer("https://host.test/store/").unwrap().peer,
        Transport::peer("https://host.test/store/v1").unwrap().peer
    );
    assert_ne!(
        Transport::peer("https://host.test/store-a").unwrap().peer,
        Transport::peer("https://host.test/store-b").unwrap().peer
    );
    assert_ne!(
        Transport::peer("[::1]:8443").unwrap().peer,
        Transport::peer("[::1:8443]").unwrap().peer
    );
}

#[test]
fn configured_sources_deduplicate_and_bootstrap_requires_one() {
    struct Restore(Vec<(&'static str, Option<std::ffi::OsString>)>);
    impl Drop for Restore {
        fn drop(&mut self) {
            for (name, value) in &self.0 {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
    let vars = [
        "LORE_SYNC_URL",
        "LORE_SYNC_TOKEN",
        "LORE_SYNC_AUTH",
        "LORE_SYNC_PEER",
        "LORE_SYNC_PEER_PORT",
        "LORE_SYNC_PEER_SECRET",
    ];
    let _restore = Restore(vars.map(|name| (name, std::env::var_os(name))).into());
    for name in vars {
        std::env::remove_var(name);
    }
    std::env::set_var(
        "LORE_SYNC_PEER",
        "host.test,http://host.test:8765/v1,https://host.test/store,https://host.test/store/v1",
    );
    let targets = net::configured_targets(None).unwrap();
    assert_eq!(targets.len(), 2);
    assert!(net::bootstrap_target(None).is_err());
    assert_eq!(
        net::bootstrap_target(Some("host.test")).unwrap().peer,
        "peer:host.test"
    );
    std::env::set_var("LORE_SYNC_URL", "https://hub.example.test");
    std::env::set_var("LORE_SYNC_TOKEN", "fixture-only-token");
    std::env::set_var("LORE_SYNC_AUTH", " TOKEN ");
    assert_eq!(net::configured_targets(None).unwrap().len(), 3);
    assert!(net::bootstrap_target(None).is_err());
    std::env::set_var("LORE_SYNC_PEER", "");
    assert_eq!(net::bootstrap_target(None).unwrap().peer, "hub");
    std::env::remove_var("LORE_SYNC_URL");
    assert!(net::configured_targets(None).is_err());
}

#[test]
fn proxy_html_413_splits_without_advancing_rejected_prefix() {
    let (_t, cfg) = config();
    let mut conn = store::connect(&cfg).unwrap();
    let tx = conn.transaction().unwrap();
    for text in ["one", "two"] {
        store::append_op(&cfg, &tx, "memory", "add", None, &json!({"text":text})).unwrap();
    }
    tx.commit().unwrap();
    let (url, h) = fixture_bytes(vec![
        (200, b"{}".to_vec()),
        (413, b"<html>Request entity too large</html>".to_vec()),
        (200, b"{\"accepted\":1,\"duplicate\":0}".to_vec()),
        (200, b"{\"accepted\":1,\"duplicate\":0}".to_vec()),
    ]);
    let report = net::push(
        &cfg,
        &Transport::new(&url, None, "hub".into()).unwrap(),
        None,
    )
    .unwrap();
    assert_eq!(report["accepted"], 2);
    let requests = h.join().unwrap();
    let batch = |index: usize| -> Value {
        serde_json::from_str(requests[index].rsplit("\r\n").next().unwrap()).unwrap()
    };
    assert_eq!(batch(1)["ops"].as_array().unwrap().len(), 2);
    assert_eq!(batch(1)["ops"][0]["op_id"], batch(2)["ops"][0]["op_id"]);
    assert_eq!(batch(2)["ops"].as_array().unwrap().len(), 1);
    assert_eq!(batch(3)["ops"].as_array().unwrap().len(), 1);
}
