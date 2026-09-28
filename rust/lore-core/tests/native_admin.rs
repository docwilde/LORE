use lore_core::{config::Config, gate::Authority, memory, store, sync_admin, sync_network};
use serde_json::{json, Value};
use std::{path::PathBuf, process::Command};
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
#[test]
fn resign_preserves_foreign_ops_and_backs_up_own_unsigned() {
    let (_t, mut cfg) = config();
    cfg.sync.key = None;
    let mut conn = store::connect(&cfg).unwrap();
    let tx = conn.transaction().unwrap();
    store::append_op(&cfg, &tx, "memory", "add", None, &json!({"text":"one"})).unwrap();
    tx.commit().unwrap();
    conn.execute("INSERT INTO sync_ops(op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created,applied) VALUES('foreign-id','foreign-machine',1,2,'memory','add',NULL,'{\"text\": \"foreign\"}',NULL,'2026-09-28T00:00:00Z',0)",[]).unwrap();
    cfg.sync.key = Some(KEY.into());
    let dry = sync_admin::resign(&cfg, false, false).unwrap();
    assert_eq!(dry["unsigned"], 1);
    assert_eq!(dry["foreign_unsigned"], 1);
    let before: Option<String> = conn
        .query_row(
            "SELECT mac FROM sync_ops WHERE machine_id='foreign-machine'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let result = sync_admin::resign(&cfg, true, false).unwrap();
    assert_eq!(result["resigned"], 1);
    assert!(PathBuf::from(result["backup"].as_str().unwrap()).is_file());
    assert_eq!(
        conn.query_row(
            "SELECT mac FROM sync_ops WHERE machine_id='foreign-machine'",
            [],
            |r| r.get::<_, Option<String>>(0)
        )
        .unwrap(),
        before
    );
    assert_eq!(
        sync_admin::resign(&cfg, false, false).unwrap()["signed_current"],
        1
    );
}
#[test]
fn seed_covers_portable_state_once_and_leaves_overcap_personal_memory() {
    let (_t, cfg) = config();
    lore_core::files::private_dir(&cfg.root).unwrap();
    let personal = "- Existing uncapped personal fact\n";
    std::fs::write(cfg.root.join("USER.md"), personal).unwrap();
    std::fs::create_dir_all(cfg.skills.join("fixture-skill")).unwrap();
    std::fs::write(
        cfg.skills.join("fixture-skill/SKILL.md"),
        "---\nname: fixture-skill\ndescription: fixture\n---\nBody\n",
    )
    .unwrap();
    let mut cfg = cfg;
    cfg.user_cap = 3;
    assert_eq!(
        sync_admin::seed(&cfg, false).unwrap()["total_candidates"],
        2
    );
    let seeded = sync_admin::seed(&cfg, true).unwrap();
    assert_eq!(seeded["total_seeded"], 2);
    assert_eq!(
        std::fs::read_to_string(cfg.root.join("USER.md")).unwrap(),
        personal
    );
    assert_eq!(sync_admin::seed(&cfg, true).unwrap()["total_seeded"], 0);
    let conn = store::read_only(&cfg).unwrap();
    let signed = conn
        .query_row(
            "SELECT count(*) FROM sync_ops WHERE mac IS NOT NULL",
            [],
            |r| r.get::<_, i64>(0),
        )
        .unwrap();
    assert_eq!(signed, 2);
}
#[test]
fn resign_accepts_python_payload_spacing_without_rewriting_bytes() {
    let (_t, cfg) = config();
    let conn = store::connect(&cfg).unwrap();
    let machine = store::machine_id(&conn).unwrap();
    let raw = "{\"text\": \"spacing retained\", \"writer\": \"terminal\"}";
    conn.execute("INSERT INTO sync_ops(op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac,created,applied) VALUES('own-id',?,1,1,'memory','add',NULL,?,NULL,'2026-09-28T00:00:00Z',1)",rusqlite::params![machine,raw]).unwrap();
    assert_eq!(
        sync_admin::resign(&cfg, true, false).unwrap()["resigned"],
        1
    );
    assert_eq!(
        conn.query_row("SELECT payload FROM sync_ops", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        raw
    );
}
#[test]
fn python_and_native_signed_bundle_formats_interoperate() {
    let (t, cfg) = config();
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();
    let path = t.path().join("python-bundle.json");
    let output=Command::new("python3").arg("-c").arg("import json, pathlib, sys; from lore_core.store import db_connect; from lore_core.sync_apply import apply_ops; from lore_core.sync_transfer import export_bundle; fixture=json.loads(pathlib.Path(sys.argv[1]).read_text()); conn=db_connect(); report=apply_ops(conn,[fixture['op'] | {'mac': fixture['expected_mac_hex']}]); assert report['applied']==1,report; export_bundle(pathlib.Path(sys.argv[2]))").arg(repo.join("tests/fixtures/sync_protocol/memory_add.json")).arg(&path).current_dir(&repo).env("LORE_ROOT",t.path().join("python-source")).env("LORE_SYNC_HMAC_KEY",KEY).env("LORE_DISABLE_SYNC","0").output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        sync_network::import_bundle(&cfg, &path).unwrap()["applied"],
        1
    );
    let native = t.path().join("native-bundle.json");
    assert_eq!(
        sync_network::export_bundle(&cfg, &native).unwrap()["count"],
        1
    );
    let output=Command::new("python3").arg("-c").arg("import pathlib,sys; from lore_core.sync_transfer import import_bundle; r=import_bundle(pathlib.Path(sys.argv[1])); assert r['applied']==1,r").arg(&native).current_dir(&repo).env("LORE_ROOT",t.path().join("python-dest")).env("LORE_SYNC_HMAC_KEY",KEY).env("LORE_DISABLE_SYNC","0").output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
#[test]
fn native_cli_respects_detached_write_gate_and_mcp_model_authority() {
    let (t, cfg) = config();
    let bin = env!("CARGO_BIN_EXE_lore-rs");
    let run = Command::new(bin)
        .args(["memory", "add", "--scope", "user", "Detached staged fact"])
        .env("LORE_ROOT", &cfg.root)
        .env_remove("AI_AGENT")
        .env_remove("CLAUDECODE")
        .env_remove("LORE_WRITE_GATE")
        .stdin(std::process::Stdio::null())
        .output()
        .unwrap();
    assert!(
        run.status.success(),
        "{}",
        String::from_utf8_lossy(&run.stderr)
    );
    let output: Value = serde_json::from_slice(&run.stdout).unwrap();
    assert_eq!(output["status"], "staged");
    assert!(memory::read_entries(&cfg.root.join("USER.md"))
        .unwrap()
        .is_empty());
    let mut process = Command::new(bin)
        .args([
            "mcp",
            "--cwd",
            t.path().to_str().unwrap(),
            "--session-id",
            "fixture-mcp",
            "--engine",
            "claude",
        ])
        .env("LORE_ROOT", &cfg.root)
        .env("LORE_WRITE_GATE", "off")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use std::io::Write;
    let input = process.stdin.as_mut().unwrap();
    for request in [
        json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}),
        json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
        json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"lore_remember","arguments":{"text":"MCP model fact","authority":"human"}}}),
    ] {
        writeln!(input, "{request}").unwrap()
    }
    drop(process.stdin.take());
    let result = process.wait_with_output().unwrap();
    assert!(result.status.success());
    let lines = String::from_utf8(result.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str::<Value>(s).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(lines.len(), 3);
    assert!(lines[1]["result"]["tools"].as_array().unwrap().len() > 3);
    assert_eq!(lines[2]["result"]["isError"], true);
    assert!(memory::read_entries(&cfg.root.join("USER.md"))
        .unwrap()
        .is_empty());
    let _ = Authority::Interactive {
        agent: "fixture".into(),
        engine: "claude".into(),
    };
}
#[test]
fn native_hook_injection_never_calls_provider() {
    let (t, cfg) = config();
    lore_core::files::private_dir(&cfg.root).unwrap();
    std::fs::write(cfg.root.join("USER.md"), "- Hook fixture memory\n").unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_lore-rs"))
        .args(["hook", "--engine", "claude", "--event", "session-start"])
        .env("LORE_ROOT", &cfg.root)
        .env("LORE_CLAUDE_BIN", "/this/provider/must/never/be/launched")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use std::io::Write;
    write!(
        child.stdin.as_mut().unwrap(),
        "{}",
        json!({"cwd":t.path(),"session_id":"hook-fixture"})
    )
    .unwrap();
    drop(child.stdin.take());
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["hookSpecificOutput"]["hookEventName"], "SessionStart");
    assert!(value["hookSpecificOutput"]["additionalContext"]
        .as_str()
        .unwrap()
        .contains("Hook fixture memory"));
}
