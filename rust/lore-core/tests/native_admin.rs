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
        json!({"jsonrpc":"2.0","id":4,"method":"ping"}),
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
    assert_eq!(lines.len(), 4);
    assert_eq!(lines[3]["result"], json!({}));
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
        .env_clear()
        .env("HOME", t.path())
        .env("LORE_ROOT", &cfg.root)
        .env("LORE_PROJECTS_DIR", &cfg.projects)
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
fn interactive() -> Authority {
    Authority::Interactive {
        agent: "fixture-reviewer".into(),
        engine: "claude".into(),
    }
}
#[test]
fn project_relocation_transaction_rolls_back_before_file_mutations() {
    let (t, cfg) = config();
    let old = t.path().join("old");
    let new = t.path().join("new");
    std::fs::create_dir(&old).unwrap();
    std::fs::create_dir(&new).unwrap();
    let source = lore_core::config::project_slug(&old);
    let target = lore_core::config::project_slug(&new);
    memory::direct_action(&cfg,&json!({"cwd":old,"scope":"project","action":"add","text":"Curated fact survives rollback"}),&interactive()).unwrap();
    let result=lore_core::beliefs::insert(&cfg,&json!({"cwd":old,"subject":format!("project:{source}"),"claim":"Relocation fixture claim","confidence":0.8}),&interactive()).unwrap();
    let conn = store::connect(&cfg).unwrap();
    conn.execute_batch("CREATE TRIGGER refuse_session_relocation BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT,'fixture rollback'); END;").unwrap();
    conn.execute("INSERT INTO sessions(session_id,project,cwd,messages) VALUES('fixture-session',?,'fixture',1)",[&source]).unwrap();
    let req = json!({"cwd":old,"old":source,"new":new});
    assert!(lore_core::standalone_ops::relocate(&cfg, &req, &[], &interactive()).is_err());
    let subject: String = conn
        .query_row(
            "SELECT subject FROM beliefs WHERE id=?",
            [result["id"].as_i64().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(subject, format!("project:{source}"));
    assert_eq!(
        memory::read_entries(&memory::Scope::Project.path(&cfg, &source).unwrap()).unwrap(),
        ["Curated fact survives rollback"]
    );
    assert!(
        memory::read_entries(&memory::Scope::Project.path(&cfg, &target).unwrap())
            .unwrap()
            .is_empty()
    );
}
#[test]
fn project_relocation_reports_cap_failure_and_preserves_provenance_on_retry() {
    let (t, mut cfg) = config();
    let old = t.path().join("old");
    let new = t.path().join("new");
    std::fs::create_dir(&old).unwrap();
    std::fs::create_dir(&new).unwrap();
    let source = lore_core::config::project_slug(&old);
    let target = lore_core::config::project_slug(&new);
    let text = "Provenance follows the curated entry";
    memory::direct_action(
        &cfg,
        &json!({"cwd":old,"scope":"project","action":"add","text":text}),
        &interactive(),
    )
    .unwrap();
    let before = lore_core::gate::provenance(&cfg, "memory", &source, text);
    cfg.project_cap = 4;
    let req = json!({"cwd":old,"old":source,"new":new});
    let partial = lore_core::standalone_ops::relocate(&cfg, &req, &[], &interactive()).unwrap();
    assert_eq!(partial["partial"], true);
    assert_eq!(partial["left"][0]["kind"], "memory");
    assert_eq!(partial["left"][0]["may_have_applied"], false);
    assert_eq!(
        memory::read_entries(&memory::Scope::Project.path(&cfg, &source).unwrap()).unwrap(),
        [text]
    );
    assert!(
        memory::read_entries(&memory::Scope::Project.path(&cfg, &target).unwrap())
            .unwrap()
            .is_empty()
    );
    let cli = Command::new(env!("CARGO_BIN_EXE_lore-rs"))
        .args(["project", "move", source.as_str(), new.to_str().unwrap()])
        .env_clear()
        .env("HOME", t.path())
        .env("LORE_ROOT", &cfg.root)
        .env("LORE_PROJECTS_DIR", &cfg.projects)
        .env("LORE_MEMORY_CAP", "4")
        .env("LORE_WRITE_GATE", "off")
        .output()
        .unwrap();
    assert!(!cli.status.success());
    assert_eq!(
        serde_json::from_slice::<Value>(&cli.stdout).unwrap()["partial"],
        true
    );
    cfg.project_cap = 8800;
    let moved = lore_core::standalone_ops::relocate(&cfg, &req, &[], &interactive()).unwrap();
    assert_eq!(moved["partial"], false);
    assert!(
        memory::read_entries(&memory::Scope::Project.path(&cfg, &source).unwrap())
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        memory::read_entries(&memory::Scope::Project.path(&cfg, &target).unwrap()).unwrap(),
        [text]
    );
    let after = lore_core::gate::provenance(&cfg, "memory", &target, text);
    assert_eq!(after["writer"], before["writer"]);
    assert_eq!(after["source_engine"], before["source_engine"]);
    assert_eq!(after["agent"], before["agent"]);
    let model = Authority::Model {
        agent: "fixture-model".into(),
        engine: "claude".into(),
        session_id: "fixture".into(),
    };
    assert_eq!(
        lore_core::standalone_ops::relocate(&cfg, &req, &[], &model).unwrap_err(),
        lore_core::Error::Untrusted
    );
}
fn native(t: &tempfile::TempDir, cfg: &Config, args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_lore-rs"))
        .args(args)
        .env_clear()
        .env("HOME", t.path())
        .env("PATH", "/usr/bin:/bin")
        .env("LORE_ROOT", &cfg.root)
        .env("LORE_PROJECTS_DIR", &cfg.projects)
        .env("LORE_CODEX_SESSIONS_DIR", &cfg.codex_sessions)
        .env("CLAUDE_CONFIG_DIR", t.path().join("claude"))
        .env("LORE_USER_CAP", "5")
        .env("LORE_CLAUDE_BIN", t.path().join("must-never-launch"))
        .env("LORE_WRITE_GATE", "off")
        .env("TMPDIR", t.path())
        .output()
        .unwrap()
}
#[test]
fn setup_stages_auto_memory_and_teardown_preserves_existing_preferences_and_overcap() {
    let (t, cfg) = config();
    let cwd = t.path().join("project");
    std::fs::create_dir(&cwd).unwrap();
    let slug = lore_core::config::project_slug(&cwd);
    let auto = cfg.projects.join(&slug).join("memory");
    lore_core::files::atomic_write(&auto.join("MEMORY.md"), b"Existing auto-memory index\n")
        .unwrap();
    lore_core::files::atomic_write(
        &auto.join("fixture.md"),
        b"---\nname: fixture\n---\n\nA project memory candidate requiring review\n",
    )
    .unwrap();
    let settings = t.path().join("claude/settings.json");
    let baseline = json!({"autoMemoryEnabled":true,"theme":"fixture","permissions":{"allow":["Read"],"deny":["Bash(rm *)"]},"env":{"LORE_OLD":"1","OTHER_PREF":"preserve"}});
    lore_core::files::atomic_write(&settings, baseline.to_string().as_bytes()).unwrap();
    lore_core::files::atomic_write(
        &cfg.root.join("USER.md"),
        b"- Existing personal memory beyond configured cap\n",
    )
    .unwrap();
    let before = std::fs::read(cfg.root.join("USER.md")).unwrap();
    let cwd_s = cwd.to_str().unwrap();
    let dry = native(&t, &cfg, &["setup", "--cwd", cwd_s, "--dry-run"]);
    assert!(
        dry.status.success(),
        "{}",
        String::from_utf8_lossy(&dry.stderr)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&std::fs::read(&settings).unwrap()).unwrap(),
        baseline
    );
    assert!(lore_core::pending::ids(&cfg).unwrap().is_empty());
    let setup = native(&t, &cfg, &["setup", "--cwd", cwd_s]);
    assert!(
        setup.status.success(),
        "{}",
        String::from_utf8_lossy(&setup.stderr)
    );
    let result: Value = serde_json::from_slice(&setup.stdout).unwrap();
    assert_eq!(result["staged"].as_array().unwrap().len(), 2);
    assert_eq!(std::fs::read(cfg.root.join("USER.md")).unwrap(), before);
    assert!(
        memory::read_entries(&memory::Scope::Project.path(&cfg, &slug).unwrap())
            .unwrap()
            .is_empty()
    );
    let changed: Value = serde_json::from_slice(&std::fs::read(&settings).unwrap()).unwrap();
    assert_eq!(changed["autoMemoryEnabled"], false);
    assert_eq!(
        changed["permissions"]["deny"],
        baseline["permissions"]["deny"]
    );
    assert_eq!(changed["theme"], "fixture");
    let pid = lore_core::pending::ids(&cfg).unwrap().pop().unwrap();
    let pending =
        lore_core::pending::snapshot(&cfg.root.join("pending").join(format!("{pid}.json")))
            .unwrap();
    assert!(pending.item["source_file"]
        .as_str()
        .unwrap()
        .starts_with(auto.to_str().unwrap()));
    assert_eq!(pending.item["writer"], "model");
    let teardown = native(&t, &cfg, &["teardown", "--cwd", cwd_s]);
    assert!(
        teardown.status.success(),
        "{}",
        String::from_utf8_lossy(&teardown.stderr)
    );
    let final_settings: Value = serde_json::from_slice(&std::fs::read(&settings).unwrap()).unwrap();
    assert_eq!(final_settings["autoMemoryEnabled"], true);
    assert_eq!(final_settings["env"], json!({"OTHER_PREF":"preserve"}));
    assert_eq!(std::fs::read(cfg.root.join("USER.md")).unwrap(), before);
    assert!(auto.join("lore-export-user.md").exists());
    let pointer = std::fs::read_to_string(auto.join("MEMORY.md")).unwrap();
    assert!(pointer.starts_with("Existing auto-memory index"));
    assert_eq!(pointer.matches("lore-export-user.md").count(), 1);
    assert!(native(&t, &cfg, &["teardown", "--cwd", cwd_s])
        .status
        .success());
    assert_eq!(
        std::fs::read_to_string(auto.join("MEMORY.md"))
            .unwrap()
            .matches("lore-export-user.md")
            .count(),
        1
    );
}
#[test]
fn full_review_backfill_and_graph_dry_runs_never_launch_provider() {
    let (t, cfg) = config();
    let cwd = t.path().join("project");
    std::fs::create_dir(&cwd).unwrap();
    let slug = lore_core::config::project_slug(&cwd);
    let source = cfg.projects.join(&slug).join("dry-session.jsonl");
    let rows=(0..4).map(|i|format!("{}\n",json!({"type":"user","cwd":cwd,"sessionId":"dry-session","message":{"content":format!("Meaningful fixture user message for a native dry run {i}")}}))).collect::<String>();
    lore_core::files::atomic_write(&source, rows.as_bytes()).unwrap();
    let cwd_s = cwd.to_str().unwrap();
    let result = native(
        &t,
        &cfg,
        &[
            "review",
            "--latest",
            "--full",
            "--workers",
            "2",
            "--dry-run",
            "--cwd",
            cwd_s,
        ],
    );
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let value: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert!(value["windows"][0]["prompt"]
        .as_str()
        .unwrap()
        .contains("fixture user message"));
    let result = native(
        &t,
        &cfg,
        &[
            "backfill",
            "--project",
            &slug,
            "--jobs",
            "2",
            "--dry-run",
            "--cwd",
            cwd_s,
        ],
    );
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let value: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(value["planned"], 1);
    assert_eq!(value["reviewed"], 0);
    for claim in [
        "A native graph fixture depends on strict parsing",
        "Private ownership checks protect stored memory",
    ] {
        lore_core::beliefs::insert(
            &cfg,
            &json!({"cwd":cwd,"subject":"user","claim":claim,"confidence":0.8}),
            &interactive(),
        )
        .unwrap();
    }
    let result = native(&t, &cfg, &["graph", "derive", "--dry-run", "--cwd", cwd_s]);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let value: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(value["claims"], 2);
    assert!(value["estimated_tokens"].as_u64().unwrap() > 0);
    assert!(lore_core::pending::ids(&cfg).unwrap().is_empty());
    let conn = store::read_only(&cfg).unwrap();
    assert_eq!(
        conn.query_row("SELECT count(*) FROM reviewed", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert!(!native(
        &t,
        &cfg,
        &[
            "memory",
            "add",
            "--scope",
            "user",
            "--unknown-native-option",
            "fixture",
            "fact"
        ]
    )
    .status
    .success());
}
#[cfg(target_os = "linux")]
#[test]
fn standalone_cancellation_reaps_provider_and_escaped_descendants() {
    use std::os::unix::fs::PermissionsExt;
    let (t, cfg) = config();
    let cwd = t.path().join("project");
    std::fs::create_dir(&cwd).unwrap();
    let slug = lore_core::config::project_slug(&cwd);
    let source = cfg.projects.join(&slug).join("cancel-session.jsonl");
    let rows=(0..3).map(|i|format!("{}\n",json!({"type":"user","cwd":cwd,"message":{"content":format!("Cancellation fixture canonical input {i}")}}))).collect::<String>();
    lore_core::files::atomic_write(&source, rows.as_bytes()).unwrap();
    let provider = t.path().join("owned-provider");
    let ready = t.path().join("ready");
    lore_core::files::atomic_write(&provider,b"#!/usr/bin/python3\nimport os,sys,json,time,subprocess,pathlib\nchild=subprocess.Popen(['/bin/sleep','30'],start_new_session=True)\npathlib.Path(os.environ['OWNED_READY']).write_text(json.dumps([os.getpid(),child.pid]))\nsys.stdin.read()\ntime.sleep(30)\n").unwrap();
    std::fs::set_permissions(&provider, std::fs::Permissions::from_mode(0o700)).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_lore-rs"))
        .args([
            "review",
            "--latest",
            "--foreground",
            "--cwd",
            cwd.to_str().unwrap(),
        ])
        .env_clear()
        .env("HOME", t.path())
        .env("LORE_ROOT", &cfg.root)
        .env("LORE_PROJECTS_DIR", &cfg.projects)
        .env("LORE_CODEX_SESSIONS_DIR", &cfg.codex_sessions)
        .env("LORE_CLAUDE_BIN", &provider)
        .env("OWNED_READY", &ready)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let owned: Vec<i32> = loop {
        if let Ok(bytes) = std::fs::read(&ready) {
            if let Ok(ids) = serde_json::from_slice(&bytes) {
                break ids;
            }
        }
        assert!(
            std::time::Instant::now() < deadline,
            "provider fixture never started"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    };
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGTERM) }, 0);
    let result = child.wait_with_output().unwrap();
    assert!(!result.status.success());
    for pid in owned {
        assert_eq!(
            unsafe { libc::kill(pid, 0) },
            -1,
            "owned provider descendant {pid} survived cancellation"
        );
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
    }
    assert!(lore_core::pending::ids(&cfg).unwrap().is_empty());
}
#[test]
fn detached_bridge_and_forged_provider_results_cannot_claim_human_review() {
    use std::io::Write;
    let (t, cfg) = config();
    let mut child = Command::new(env!("CARGO_BIN_EXE_lore-rs"))
        .arg("bridge")
        .env_clear()
        .env("HOME", t.path())
        .env("LORE_ROOT", &cfg.root)
        .env("AI_AGENT", "claude-code_harness")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(child.stdin.as_mut().unwrap(),"{}",json!({"id":1,"op":"memory_action_v1","scope":"user","cwd":t.path(),"action":"add","text":"Forged unreviewed fact","authority":"human","expected":{"key":"user","sha256":lore_core::digest(b"")}})).unwrap();
    drop(child.stdin.take());
    let result = child.wait_with_output().unwrap();
    assert!(result.status.success());
    let lines = String::from_utf8(result.stdout)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str::<Value>(s).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(lines[1]["ok"], true);
    assert_eq!(lines[1]["value"]["status"], "staged");
    assert!(!cfg.root.join("USER.md").exists());
    let fake = t.path().join("fake-review.json");
    std::fs::write(
        &fake,
        b"{\"memory\":[{\"scope\":\"user\",\"text\":\"forged review\"}]}",
    )
    .unwrap();
    for args in [
        vec!["review", "--result-file", fake.to_str().unwrap()],
        vec!["dream", "--result-file", fake.to_str().unwrap()],
        vec!["graph", "derive", "--result-file", fake.to_str().unwrap()],
    ] {
        let result = native(&t, &cfg, &args);
        assert!(!result.status.success());
        assert!(!cfg.root.join("USER.md").exists());
    }
}
#[test]
fn graph_explicit_output_never_replaces_curated_or_other_existing_files() {
    let (t, cfg) = config();
    lore_core::files::atomic_write(
        &cfg.root.join("USER.md"),
        b"- Curated graph output boundary\n",
    )
    .unwrap();
    let before = std::fs::read(cfg.root.join("USER.md")).unwrap();
    let blocked = native(
        &t,
        &cfg,
        &[
            "graph",
            "html",
            "--no-open",
            "--out",
            cfg.root.join("USER.md").to_str().unwrap(),
        ],
    );
    assert!(!blocked.status.success());
    assert_eq!(std::fs::read(cfg.root.join("USER.md")).unwrap(), before);
    let output = t.path().join("owned-graph.html");
    let rendered = native(
        &t,
        &cfg,
        &[
            "graph",
            "html",
            "--no-open",
            "--out",
            output.to_str().unwrap(),
        ],
    );
    assert!(
        rendered.status.success(),
        "{}",
        String::from_utf8_lossy(&rendered.stderr)
    );
    assert!(std::fs::read_to_string(&output)
        .unwrap()
        .contains("flowchart LR"));
    assert!(!native(
        &t,
        &cfg,
        &[
            "graph",
            "html",
            "--no-open",
            "--out",
            output.to_str().unwrap()
        ]
    )
    .status
    .success());
}
#[test]
fn incremental_review_skips_idle_work_and_prompt_scheduler_starts_clock_without_model() {
    use std::{io::Write, os::unix::fs::PermissionsExt};
    let (t, cfg) = config();
    let cwd = t.path().join("project");
    std::fs::create_dir(&cwd).unwrap();
    let slug = lore_core::config::project_slug(&cwd);
    let source = cfg.projects.join(&slug).join("tick-session.jsonl");
    let rows=(0..3).map(|i|format!("{}\n",json!({"type":"user","cwd":cwd,"message":{"content":format!("Original owned fixture user turn {i}")}}))).collect::<String>();
    lore_core::files::atomic_write(&source, rows.as_bytes()).unwrap();
    let provider = t.path().join("owned-provider");
    let calls = t.path().join("provider-calls");
    let prompt = t.path().join("provider-prompt");
    lore_core::files::atomic_write(&provider,b"#!/usr/bin/python3\nimport os,sys,pathlib,json\nprompt=sys.stdin.read()\npathlib.Path(os.environ['OWNED_PROMPT']).write_text(prompt)\nwith pathlib.Path(os.environ['OWNED_CALLS']).open('a') as file:file.write('call\\n')\nprint(json.dumps({'memory':[],'filemap':[],'skills':[],'conclusions':[]}))\n").unwrap();
    std::fs::set_permissions(&provider, std::fs::Permissions::from_mode(0o700)).unwrap();
    let command = |args: &[&str]| {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_lore-rs"));
        cmd.args(args)
            .env_clear()
            .env("HOME", t.path())
            .env("LORE_ROOT", &cfg.root)
            .env("LORE_PROJECTS_DIR", &cfg.projects)
            .env("LORE_CODEX_SESSIONS_DIR", &cfg.codex_sessions)
            .env("LORE_CLAUDE_BIN", &provider)
            .env("LORE_DEFER_DREAM", "1")
            .env("OWNED_PROMPT", &prompt)
            .env("OWNED_CALLS", &calls);
        cmd
    };
    let args = [
        "review",
        "--latest",
        "--incremental",
        "--cwd",
        cwd.to_str().unwrap(),
    ];
    let first = command(&args).output().unwrap();
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    assert_eq!(std::fs::read_to_string(&calls).unwrap().lines().count(), 1);
    let idle = command(&args).output().unwrap();
    assert!(idle.status.success());
    assert_eq!(
        serde_json::from_slice::<Value>(&idle.stdout).unwrap()["reason"],
        "no_new_messages"
    );
    assert_eq!(std::fs::read_to_string(&calls).unwrap().lines().count(), 1);
    let append = |text: &str| {
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&source)
            .unwrap();
        writeln!(
            file,
            "{}",
            json!({"type":"user","cwd":cwd,"message":{"content":text}})
        )
        .unwrap();
    };
    append("Fresh appended native fixture turn");
    let second = command(&args).output().unwrap();
    assert!(
        second.status.success(),
        "{}",
        String::from_utf8_lossy(&second.stderr)
    );
    let latest = std::fs::read_to_string(&prompt).unwrap();
    assert!(latest.contains("Fresh appended native fixture turn"));
    assert!(!latest.contains("Original owned fixture user turn"));
    let event = json!({"cwd":cwd,"session_id":"tick-session","transcript_path":source,"prompt":"fixture prompt"});
    let hook = || {
        let mut child = command(&["hook", "--engine", "claude", "--event", "prompt"])
            .env("LORE_REVIEW_SECS", "1")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        write!(child.stdin.as_mut().unwrap(), "{event}").unwrap();
        drop(child.stdin.take());
        child.wait_with_output().unwrap()
    };
    let first_prompt = hook();
    assert!(
        first_prompt.status.success(),
        "{}",
        String::from_utf8_lossy(&first_prompt.stderr)
    );
    assert_eq!(std::fs::read_to_string(&calls).unwrap().lines().count(), 2);
    assert!(cfg.root.join(".midreview/tick-session").exists());
    append("Second fresh delta scheduled natively");
    lore_core::files::atomic_write(&cfg.root.join(".midreview/tick-session"), b"0").unwrap();
    assert!(hook().status.success());
    let watermark = cfg
        .root
        .join(".derived")
        .join(&slug)
        .join("tick-session.json");
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        let value: Value = serde_json::from_slice(&std::fs::read(&watermark).unwrap()).unwrap();
        if value["count"] == 5 {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "native scheduled review did not settle"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert_eq!(std::fs::read_to_string(&calls).unwrap().lines().count(), 3);
    assert!(std::fs::read_to_string(&prompt)
        .unwrap()
        .contains("Second fresh delta scheduled natively"));
}
