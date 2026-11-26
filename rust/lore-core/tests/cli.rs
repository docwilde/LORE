//! Context-advertised read-only CLI fixtures, isolated from host state.
#![cfg(target_os = "linux")]
use lore_core::{config, files, gate};
use serde_json::json;
use std::{
    fs,
    io::Write,
    os::unix::{fs::symlink, process::CommandExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

struct Fixture {
    _dir: tempfile::TempDir,
    home: PathBuf,
    root: PathBuf,
    cwd: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().to_path_buf();
        let root = home.join("lore");
        let cwd = home.join("repo");
        fs::create_dir(&cwd).unwrap();
        Self {
            _dir: dir,
            home,
            root,
            cwd,
        }
    }
    fn invoke(&self, args: &[&str]) -> (bool, String, String) {
        self.invoke_env(args, &[])
    }
    fn invoke_env(&self, args: &[&str], env: &[(&str, &str)]) -> (bool, String, String) {
        self.invoke_with_input(args, env, None)
    }
    fn invoke_with_input(&self, args: &[&str], env: &[(&str, &str)], input: Option<&str>) -> (bool, String, String) {
        let out = self.home.join("stdout");
        let err = self.home.join("stderr");
        let mut child = Command::new(env!("CARGO_BIN_EXE_lore-rs"))
            .args(args)
            .env_clear()
            .env("HOME", &self.home)
            .env("LORE_ROOT", &self.root)
            .env("LORE_MACHINE_HOST", "owned-host")
            .env("LORE_SKILLS_DIR", self.home.join("skills"))
            .env("LORE_PROJECTS_DIR", self.home.join("sessions"))
            .env("LORE_CODEX_SESSIONS_DIR", self.home.join("codex-sessions"))
            .envs(env.iter().copied())
            .current_dir(&self.cwd)
            .process_group(0)
            .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
            .stdout(Stdio::from(fs::File::create(&out).unwrap()))
            .stderr(Stdio::from(fs::File::create(&err).unwrap()))
            .spawn()
            .unwrap();
        if let Some(input) = input {
            child.stdin.take().unwrap().write_all(input.as_bytes()).unwrap();
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        let ready = loop {
            let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
            assert_eq!(
                unsafe {
                    libc::waitid(
                        libc::P_PID,
                        child.id(),
                        &mut info,
                        libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                    )
                },
                0
            );
            if unsafe { info.si_pid() } != 0 {
                break true;
            }
            if Instant::now() >= deadline {
                break false;
            }
            std::thread::sleep(Duration::from_millis(5));
        };
        unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL) };
        let status = child.wait().unwrap();
        assert!(ready, "owned read-only CLI exceeded deadline");
        (
            status.success(),
            fs::read_to_string(out).unwrap(),
            fs::read_to_string(err).unwrap(),
        )
    }
    fn write(&self, path: &Path, text: &str) {
        files::atomic_write(path, text.as_bytes()).unwrap();
    }
    fn project(&self) -> PathBuf {
        self.root
            .join("projects")
            .join(config::project_slug(&self.cwd))
            .join("MEMORY.md")
    }
    fn map(&self) -> PathBuf {
        self.root
            .join("filemap")
            .join(format!("{}.md", config::project_slug(&self.cwd)))
    }
}

#[test]
fn antigravity_start_injects_only_the_declared_workspace() {
    let fixture = Fixture::new();
    let workspace = fixture.home.join("workspace");
    fs::create_dir(&workspace).unwrap();
    fixture.write(&fixture.project(), "- wrong workspace fact\n");
    fixture.write(&fixture.root.join("projects").join(config::project_slug(&workspace)).join("MEMORY.md"),
        "- selected workspace fact\n");
    let args = ["hook", "--engine", "antigravity", "--event", "session-start"];
    let input = json!({"workspacePaths":[workspace]}).to_string();
    let (ok, out, error) = fixture.invoke_with_input(&args, &[], Some(&input));
    assert!(ok && error.is_empty(), "{error}");
    let response: serde_json::Value = serde_json::from_str(&out).unwrap();
    let text = response["injectSteps"][0]["systemMessage"]["systemMessage"].as_str().unwrap();
    assert!(text.contains("selected workspace fact"));
    assert!(!text.contains("wrong workspace fact"));
    let ambiguous = json!({"workspacePaths":[workspace, fixture.cwd]}).to_string();
    for (input, env) in [(&ambiguous, &[][..]), (&input, &[("LORE_DISABLE_INJECT", "1")][..])] {
        let (ok, out, error) = fixture.invoke_with_input(&args, env, Some(input));
        assert!(ok && error.is_empty(), "{error}");
        assert_eq!(out.trim(), "{}");
    }
}

#[test]
fn antigravity_stop_indexes_owned_chat_steps_and_is_safe_to_repeat() {
    let fixture = Fixture::new();
    let sid = "5b200830-3bc3-4e5b-aa56-4e0649feaddb";
    let transcript = fixture.home.join(".gemini/antigravity-cli/brain").join(sid)
        .join(".system_generated/logs/transcript.jsonl");
    fixture.write(&transcript, concat!(
        "{\"type\":\"SYSTEM_MESSAGE\",\"source\":\"SYSTEM\",\"status\":\"DONE\",\"content\":\"injected memory\"}\n",
        "{\"type\":\"USER_INPUT\",\"source\":\"USER_EXPLICIT\",\"status\":\"DONE\",\"created_at\":\"2026-11-25T00:00:00Z\",\"content\":\"<USER_REQUEST>\\nlorehookneedle question\\n</USER_REQUEST>\\n<ADDITIONAL_METADATA>private clock metadata</ADDITIONAL_METADATA>\"}\n",
        "{\"type\":\"GENERIC\",\"source\":\"MODEL\",\"status\":\"DONE\",\"content\":\"tool output\"}\n",
        "{\"type\":\"PLANNER_RESPONSE\",\"source\":\"MODEL\",\"status\":\"DONE\",\"created_at\":\"2026-11-25T00:00:01Z\",\"content\":\"answer sk-abcdefghijklmnopqrstuvwxyz123456\",\"thinking\":\"private reasoning\"}\n",
    ));
    let args = ["hook", "--engine", "antigravity", "--event", "stop"];
    let event = json!({"workspacePaths":[fixture.cwd],"conversationId":sid,
                       "transcriptPath":transcript,"fullyIdle":true});
    let mut busy = event.clone();
    busy["fullyIdle"] = json!(false);
    let (ok, _, _) = fixture.invoke_with_input(&args, &[], Some(&busy.to_string()));
    assert!(ok && !fixture.root.exists());
    for _ in 0..2 {
        let (ok, out, error) = fixture.invoke_with_input(&args, &[], Some(&event.to_string()));
        assert!(ok && error.is_empty(), "{error}");
        assert_eq!(serde_json::from_str::<serde_json::Value>(&out).unwrap(), json!({"decision":"stop"}));
    }
    let (ok, out, error) = fixture.invoke(&["session", sid]);
    assert!(ok, "{error}");
    let session: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(session["session"][0]["engine"], "antigravity");
    assert_eq!(session["rows"].as_array().unwrap().len(), 2);
    assert_eq!(session["rows"][0]["content"], "lorehookneedle question");
    assert!(!out.contains("injected memory") && !out.contains("tool output") && !out.contains("private reasoning"));
    assert!(!out.contains("sk-abcdefghijklmnopqrstuvwxyz123456"));
    let (ok, out, error) = fixture.invoke(&["search", "lorehookneedle"]);
    assert!(ok && out.contains(sid), "{error}");

    let other_file = fixture.home.join("other-transcript.jsonl");
    fs::rename(&transcript, &other_file).unwrap();
    symlink(&other_file, &transcript).unwrap();
    let (ok, out, error) = fixture.invoke_with_input(&args, &[], Some(&event.to_string()));
    assert!(ok && out.contains("stop") && error.contains("unsafe_path"), "{error}");
}

#[test]
fn standalone_codegraph_queries_rust_and_python_without_a_memory_store() {
    let fixture = Fixture::new();
    assert!(Command::new("git").args(["init", "-q"])
        .current_dir(&fixture.cwd).status().unwrap().success());
    let rust = "use std::fmt;\npub fn root() { helper(); }\nfn helper() {}\nmod child;\n";
    fs::write(fixture.cwd.join("lib.rs"), rust).unwrap();
    fs::write(fixture.cwd.join("child.rs"), "pub fn child() {}\n").unwrap();
    fs::write(fixture.cwd.join("app.py"), "from pathlib import Path\ndef action():\n    return Path('.')\n").unwrap();
    let query = |kind: &str, input: &str| -> serde_json::Value {
        let (ok, reply, error) = fixture.invoke_env(
            &["codegraph", kind, input],
            &[("PATH", "/usr/bin:/bin")],
        );
        assert!(ok, "{kind}: {error}");
        serde_json::from_str(&reply).unwrap()
    };
    assert_eq!(query("file", "lib.rs")["requested_source_sha256"], lore_core::digest(rust.as_bytes()));
    assert_eq!(query("symbol", "action")["rows"][0]["file"], "app.py");
    assert!(query("imports", "app.py")["rows"].as_array().is_some_and(|rows| !rows.is_empty()));
    assert_eq!(query("calls", "lib.rs")["edges"][0]["binding"], "candidate_only");
    assert_eq!(query("modules", "lib.rs")["module_edges"][0]["target"], "child.rs");
    let (ok, _, _) = fixture.invoke_env(&["codegraph", "file", "../outside.rs"], &[("PATH", "/usr/bin:/bin")]);
    assert!(!ok);
    assert!(!fixture.root.exists());
}
fn tree(root: &Path) -> Vec<(PathBuf, Vec<u8>)> {
    let mut rows = Vec::new();
    if let Ok(entries) = fs::read_dir(root) {
        for entry in entries {
            let path = entry.unwrap().path();
            if path.is_dir() {
                rows.extend(tree(&path));
            } else {
                rows.push((path.clone(), fs::read(path).unwrap()));
            }
        }
    }
    rows.sort();
    rows
}

#[test]
fn audit_reports_fragments_across_checkouts_without_recording_outcomes() {
    let fixture = Fixture::new();
    let mut args = ["audit", "--cwd", fixture.cwd.to_str().unwrap()];
    let (ok, empty, error) = fixture.invoke(&args);
    assert!(ok, "{error}");
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&empty).unwrap(),
        json!({"rows":[],"legacy_audit_outcomes":0})
    );
    assert!(!fixture.root.exists());

    let other = fixture.home.join("other");
    fs::create_dir(&other).unwrap();
    for repo in [&fixture.cwd, &other] {
        assert!(Command::new("git")
            .args(["init", "-q"])
            .current_dir(repo)
            .status()
            .unwrap()
            .success());
    }
    let flags = fixture.cwd.join("flags.txt");
    fs::write(&flags, "run --fixture-feature\n").unwrap();
    assert!(Command::new("git")
        .args(["add", "flags.txt"])
        .current_dir(&fixture.cwd)
        .status()
        .unwrap()
        .success());

    let cfg = config::Config::for_root(fixture.root.clone());
    let conn = lore_core::store::connect(&cfg).unwrap();
    conn.execute(
        "INSERT INTO beliefs(id,subject,claim,confidence) VALUES(1,?1,?2,0.7),(2,?1,?3,0.7)",
        [
            format!("project:{}", config::project_slug(&fixture.cwd)),
            "Run with --fixture-feature".into(),
            format!("The config at {} sets replicas to 1000", flags.display()),
        ],
    )
    .unwrap();
    conn.execute(
        "INSERT INTO belief_outcomes(belief_id,event,source) VALUES(1,'stale','audit')",
        [],
    )
    .unwrap();

    for (repo, expected) in [(&fixture.cwd, "MATCH"), (&other, "NO_MATCH")] {
        args[2] = repo.to_str().unwrap();
        let (ok, report, error) = fixture.invoke_env(&args, &[("PATH", "/usr/bin:/bin")]);
        assert!(ok, "{error}");
        let report: serde_json::Value = serde_json::from_str(&report).unwrap();
        assert_eq!(report["legacy_audit_outcomes"], 1);
        let rows = report["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 2);
        let verdict = |id: i64| &rows.iter().find(|row| row["id"] == id).unwrap()["verdict"];
        assert_eq!(verdict(1), expected);
        assert_eq!(verdict(2), "MATCH");
    }
    let outcomes: (i64, String, String) = conn
        .query_row("SELECT count(*),event,source FROM belief_outcomes", [], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })
        .unwrap();
    assert_eq!(outcomes, (1, "stale".into(), "audit".into()));
}

#[test]
fn legacy_audit_outcomes_remain_in_the_ledger_without_admitting_steer() {
    let fixture = Fixture::new();
    let cfg = config::Config::for_root(fixture.root.clone());
    let conn = lore_core::store::connect(&cfg).unwrap();
    conn.execute(
        "INSERT INTO beliefs(id,subject,claim,confidence) VALUES(1,'user','calibrationfixture',0.7)",
        [],
    )
    .unwrap();
    conn.execute_batch(
        "INSERT INTO belief_fts(belief_id,claim) VALUES(1,'calibrationfixture');
         INSERT INTO belief_outcomes(belief_id,event,source) VALUES
            (1,'confirmed','audit'),(1,'confirmed','audit'),(1,'stale','audit'),
            (1,'confirmed','user'),(1,'confirmed','user');",
    )
    .unwrap();
    let invoke = |args: &[&str]| -> serde_json::Value {
        let (ok, report, error) = fixture.invoke(args);
        assert!(ok, "{error}");
        serde_json::from_str(&report).unwrap()
    };
    let consult = || invoke(&["consult", "calibrationfixture"]);
    let report = consult();
    assert_eq!(report["steer"], json!([]));
    assert_eq!(report["cite_only"][0]["outcomes"], 2, "{report}");
    let stats = invoke(&["stats"]);
    assert_eq!(stats["ledger_total"], 5);
    assert_eq!(stats["calibration_total"], 2);
    assert_eq!(stats["legacy_audit_outcomes"], 3);
    assert_eq!(stats["buckets"][0]["precision"], 1.0);

    conn.execute(
        "INSERT INTO belief_outcomes(belief_id,event,source) VALUES(1,'contradicted','user')",
        [],
    )
    .unwrap();
    let report = consult();
    assert_eq!(report["steer"][0]["outcomes"], 3);
    assert_eq!(report["steer"][0]["stale"], 0);
    assert!((report["steer"][0]["calibrated_confidence"].as_f64().unwrap() - 0.68).abs() < 1e-9);
}

fn sync_reply(status: u16, body: serde_json::Value) -> (String, std::thread::JoinHandle<()>) {
    use std::io::{BufRead, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let handle = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(
                        Instant::now() < deadline,
                        "native CLI never contacted source"
                    );
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => panic!("owned fixture accept: {error}"),
            }
        };
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut reader = std::io::BufReader::new(stream.try_clone().unwrap());
        let mut bytes = 0;
        loop {
            let mut line = String::new();
            let read = reader.read_line(&mut line).unwrap();
            assert!(read > 0);
            bytes += read;
            assert!(bytes < 16384);
            if line == "\r\n" {
                break;
            }
        }
        let body = serde_json::to_vec(&body).unwrap();
        write!(
            stream,
            "HTTP/1.1 {status} fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        )
        .unwrap();
        stream.write_all(&body).unwrap();
    });
    (url, handle)
}

#[test]
fn native_sync_cli_preserves_successful_source_and_reports_failure_exit() {
    let fixture = Fixture::new();
    let (failed_url, failed) = sync_reply(500, json!({"error":"fixture_failure"}));
    let (good_url, good) = sync_reply(200, json!({"ops":[],"next":null}));
    let peers = format!("{failed_url},{good_url},{good_url}/v1");
    let (success, stdout, stderr) = fixture.invoke_env(
        &["sync", "pull"],
        &[
            ("LORE_SYNC_PEER", &peers),
            ("LORE_SYNC_TIMEOUT", "1"),
            ("LORE_SYNC_PEER_SECRET", "fixture-token-never-print"),
        ],
    );
    failed.join().unwrap();
    good.join().unwrap();
    assert!(!success);
    let report: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(report["failed"], 1);
    assert_eq!(
        report.as_object().unwrap().len(),
        3,
        "one report per distinct source"
    );
    assert_eq!(
        report
            .as_object()
            .unwrap()
            .values()
            .filter(|value| value["cursor_advanced"] == true)
            .count(),
        1
    );
    assert!(stderr.contains("may_have_applied"));
    assert!(!format!("{stdout}{stderr}").contains("fixture-token-never-print"));
}

#[test]
fn native_bootstrap_refuses_ambiguous_source_before_network_or_store_changes() {
    let fixture = Fixture::new();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (success, _, stderr) = fixture.invoke_env(
        &["sync", "bootstrap"],
        &[
            ("LORE_SYNC_URL", &url),
            ("LORE_SYNC_TOKEN", "fixture-token-never-print"),
            ("LORE_SYNC_PEER", &url),
        ],
    );
    assert!(!success);
    assert!(stderr.contains("invalid_request"));
    assert_eq!(
        listener.accept().unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    assert!(!fixture.root.exists());
}

#[test]
fn native_import_reports_unverified_operations_as_failed_without_curated_writes() {
    let fixture = Fixture::new();
    let ops = json!([{
        "op_id":uuid::Uuid::new_v4().to_string(),
        "machine_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "machine_seq":1, "lamport":1,
        "class":"memory", "op":"add", "project_key":null,
        "payload":{"text":"Unverified fixture fact", "writer":"approved", "via":"test"},
        "created":"2026-09-28T00:00:00Z", "mac":null
    }]);
    let digest = lore_core::digest(&lore_core::store::canonical_bytes(&ops).unwrap());
    let path = fixture.home.join("unverified-bundle.json");
    let bundle = json!({"format":"lore-manual-transfer", "version":1,
        "classes":["memory"], "count":1, "sha256":digest, "ops":ops});
    fixture.write(&path, &serde_json::to_string(&bundle).unwrap());
    let (success, stdout, _) = fixture.invoke_env(
        &["sync", "import", path.to_str().unwrap()],
        &[("LORE_SYNC_HMAC_KEY", "fixture-shared-key")],
    );
    assert!(!success);
    let report: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(report["unverified"], 1);
    assert!(!fixture.root.join("USER.md").exists());
}

#[test]
fn fresh_context_readback_is_lazy_and_does_not_create_a_store() {
    let fixture = Fixture::new();
    for args in [
        vec!["memory", "show"],
        vec!["filemap", "show"],
        vec![
            "memory",
            "show",
            "--scope",
            "machine",
            "--host",
            "remote-host",
        ],
    ] {
        let (success, out, err) = fixture.invoke(&args);
        assert!(success && err.is_empty());
        assert!(out.contains("(empty)"));
        assert!(!fixture.root.exists());
    }
}
#[test]
fn memory_readback_preserves_scopes_unknown_sources_and_other_host_pull_policy() {
    let fixture = Fixture::new();
    fixture.write(
        &fixture.root.join("USER.md"),
        "- known user fact\n- unlabelled older fact\n",
    );
    fixture.write(&fixture.project(), "- current project fact\n");
    fixture.write(
        &fixture.root.join("machines/owned-host.md"),
        "- current machine fact\n",
    );
    fixture.write(
        &fixture.root.join("machines/remote-host.md"),
        "- remote private fact\n",
    );
    let key = gate::entry_key("memory", "user", "known user fact");
    fixture.write(&fixture.root.join("provenance.json"),&json!({"version":1,"entries":{(key):{"writer":"terminal","via":"direct","source_engine":"claude"}}}).to_string());
    let before = tree(&fixture.root);
    let (success, out, err) = fixture.invoke(&["memory", "show"]);
    assert!(success && err.is_empty());
    assert!(
        out.contains("known user fact [source: claude]") && out.contains("unlabelled older fact\n")
    );
    assert!(!out.contains("unlabelled older fact [source:"));
    assert!(
        out.contains("current project fact")
            && out.contains("current machine fact")
            && out.contains("other machines on file: remote-host")
    );
    assert!(!out.contains("remote private fact"));
    let (success, out, err) = fixture.invoke(&[
        "memory",
        "show",
        "--scope",
        "machine",
        "--host",
        "remote-host",
    ]);
    assert!(success && err.is_empty());
    assert!(
        out.contains("remote private fact")
            && !out.contains("current machine fact")
            && !out.contains("known user fact")
    );
    assert_eq!(tree(&fixture.root), before);
    assert!(!fixture.root.join("state.db").exists() && !fixture.root.join(".locks").exists());
}
#[test]
fn codex_session_writes_apply_with_interactive_provenance() {
    for marker in ["CODEX_THREAD_ID", "CODEX_SESSION_ID"] {
        let fixture = Fixture::new();
        let env = [(marker, "fixture-codex-session")];
        let (success, out, err) = fixture.invoke_env(
            &["memory", "add", "--scope", "user", "Codex session fact"],
            &env,
        );
        assert!(success && err.is_empty(), "{marker}: {out} {err}");
        assert_eq!(
            fs::read_to_string(fixture.root.join("USER.md")).unwrap(),
            "- Codex session fact\n"
        );
        let cfg = config::Config::for_root(fixture.root.clone());
        let provenance = gate::provenance(&cfg, "memory", "user", "Codex session fact");
        assert_eq!(provenance["writer"], "interactive", "{marker}");
        assert_eq!(provenance["source_engine"], "codex", "{marker}");
        assert!(lore_core::pending::ids(&cfg).unwrap().is_empty());

        let (success, out, err) = fixture.invoke_env(
            &[
                "memory", "replace", "--scope", "user", "--match",
                "Codex session fact", "Updated Codex fact",
            ],
            &env,
        );
        assert!(success && err.is_empty(), "{marker}: {out} {err}");
        assert_eq!(
            fs::read_to_string(fixture.root.join("USER.md")).unwrap(),
            "- Updated Codex fact\n"
        );
        let (success, out, err) = fixture.invoke_env(
            &[
                "memory", "remove", "--scope", "user", "--match", "Updated Codex fact",
            ],
            &env,
        );
        assert!(success && err.is_empty(), "{marker}: {out} {err}");
        assert_eq!(
            fs::read_to_string(fixture.root.join("USER.md")).unwrap(),
            ""
        );
        assert!(lore_core::pending::ids(&cfg).unwrap().is_empty());
    }
}

#[test]
fn memory_and_filemap_mutations_refuse_a_missing_or_misplaced_match() {
    // The reported case was `memory replace --scope project "<old>" "<new>"`:
    // both positionals became the text, the match stayed empty and the write
    // was refused as review_changed. The match is a flag; say so.
    let fixture = Fixture::new();
    let env = [("CODEX_THREAD_ID", "fixture-codex-session")];
    fixture.write(&fixture.project(), "- old project fact\n- other project fact\n");
    fixture.write(&fixture.map(), "- src/a.rs \u{2014} first\n");
    let memory = fs::read(fixture.project()).unwrap();
    let map = fs::read(fixture.map()).unwrap();
    for args in [
        vec!["memory", "replace", "--scope", "project", "old project fact", "new project fact"],
        vec!["memory", "replace", "--scope", "project", "new project fact"],
        vec!["memory", "replace", "--scope", "project", "--match", "", "new project fact"],
        vec!["memory", "replace", "--scope", "project", "--match", "old", "--text", "new", "stray"],
        vec!["memory", "remove", "--scope", "project", "old project fact"],
        vec!["memory", "remove", "--scope", "project", "--match", "old", "stray"],
        vec!["memory", "move", "--scope", "project", "--match", "old", "--to-machine", "owned-host", "stray"],
        vec!["memory", "move", "--scope", "project", "--match", " ", "--to-machine", "owned-host"],
        vec!["filemap", "remove", "src/a.rs"],
        vec!["filemap", "replace", "src/b.rs", "second"],
        vec!["filemap", "remove", "--match", "a.rs", "stray"],
    ] {
        let (success, out, err) = fixture.invoke_env(&args, &env);
        assert!(!success && out.is_empty(), "{args:?}: {out}");
        assert_eq!(err, "lore-rs: invalid_request\n", "{args:?}");
        assert_eq!(fs::read(fixture.project()).unwrap(), memory, "{args:?}");
        assert_eq!(fs::read(fixture.map()).unwrap(), map, "{args:?}");
    }
    for (needle, code) in [("absent", "match_not_found"), ("project fact", "match_ambiguous")] {
        let (success, out, err) = fixture.invoke_env(
            &["memory", "replace", "--scope", "project", "--match", needle, "new project fact"],
            &env,
        );
        assert!(!success && out.is_empty(), "{needle}: {out}");
        assert_eq!(err, format!("lore-rs: {code}\n"));
        assert_eq!(fs::read(fixture.project()).unwrap(), memory);
    }
    // A destination project name that resolves to nothing is the same kind
    // of miss, reported the same way.
    let (success, out, err) = fixture.invoke_env(
        &["memory", "move", "--scope", "project", "--match", "old", "--to", "no-such-project"],
        &env,
    );
    assert!(!success && out.is_empty(), "{out}");
    assert_eq!(err, "lore-rs: match_not_found\n");
    assert_eq!(fs::read(fixture.project()).unwrap(), memory);
    let (success, out, err) = fixture.invoke_env(
        &["memory", "replace", "--scope", "project", "--match", "old", "new project fact"],
        &env,
    );
    assert!(success && err.is_empty(), "{out} {err}");
    assert_eq!(
        fs::read_to_string(fixture.project()).unwrap(),
        "- new project fact\n- other project fact\n"
    );
}
#[test]
fn codex_session_can_write_config_and_preserves_explicit_source_engine() {
    let fixture = Fixture::new();
    let env = [
        ("CODEX_THREAD_ID", "fixture-codex-session"),
        ("LORE_ENGINE", "custom"),
    ];
    let (success, out, err) =
        fixture.invoke_env(&["config", "set", "LORE_REFRESH_SECS", "60"], &env);
    assert!(success && err.is_empty(), "{out} {err}");
    let settings: serde_json::Value = serde_json::from_slice(
        &fs::read(fixture.home.join(".claude/settings.json")).unwrap(),
    ).unwrap();
    assert_eq!(settings["env"]["LORE_REFRESH_SECS"], "60");
    let (success, out, err) = fixture.invoke_env(
        &["memory", "add", "--scope", "user", "Explicit engine fact"],
        &env,
    );
    assert!(success && err.is_empty(), "{out} {err}");
    let cfg = config::Config::for_root(fixture.root.clone());
    assert_eq!(
        gate::provenance(&cfg, "memory", "user", "Explicit engine fact")["source_engine"],
        "custom"
    );
}

#[test]
fn codex_markers_do_not_override_hooks_or_grant_human_review() {
    for extra in [
        vec![("AI_AGENT", "codex_fixture_harness")],
        vec![
            ("AI_AGENT", "claude-code_fixture_harness"),
            ("CLAUDECODE", "1"),
        ],
        vec![("CLAUDECODE", "1"), ("CLAUDE_PROJECT_DIR", "/fixture")],
    ] {
        let fixture = Fixture::new();
        let mut env = vec![
            ("CODEX_THREAD_ID", "fixture-codex-session"),
            ("CODEX_SESSION_ID", "fixture-codex-session"),
        ];
        env.extend(extra);
        let (success, out, err) = fixture.invoke_env(
            &["memory", "add", "--scope", "user", "Hook proposal"],
            &env,
        );
        assert!(success && err.is_empty(), "{out} {err}");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&out).unwrap()["status"],
            "staged"
        );
        assert!(!fixture.root.join("USER.md").exists());
        let (success, out, err) =
            fixture.invoke_env(&["config", "set", "LORE_REFRESH_SECS", "60"], &env);
        assert!(!success && out.is_empty());
        assert_eq!(err, "lore-rs: untrusted_write\n");
    }
    let fixture = Fixture::new();
    let (success, out, err) = fixture.invoke_env(
        &["codegraph", "store"],
        &[("CODEX_THREAD_ID", "fixture-codex-session")],
    );
    assert!(!success && out.is_empty());
    assert_eq!(err, "lore-rs: untrusted_write\n");
}

#[test]
fn empty_codex_markers_and_engine_labels_do_not_grant_write_authority() {
    let fixture = Fixture::new();
    let (success, out, err) = fixture.invoke_env(
        &["memory", "add", "--scope", "user", "Detached proposal"],
        &[
            ("CODEX_THREAD_ID", ""),
            ("CODEX_SESSION_ID", ""),
            ("LORE_ENGINE", "codex"),
        ],
    );
    assert!(success && err.is_empty(), "{out} {err}");
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&out).unwrap()["status"],
        "staged"
    );
    assert!(!fixture.root.join("USER.md").exists());
}

#[test]
fn project_and_filemap_use_current_repository_identity_without_mutating_files() {
    let mut fixture = Fixture::new();
    fs::create_dir(fixture.cwd.join(".git")).unwrap();
    fixture.write(&fixture.project(), "- repository scoped fact\n");
    fixture.write(
        &fixture.map(),
        "- src/lib.rs — canonical parser\n- legacy-path-only\n",
    );
    let before = tree(&fixture.root);
    let subdir = fixture.cwd.join("subdir");
    fs::create_dir(&subdir).unwrap();
    fixture.cwd = subdir;
    let (success, out, err) = fixture.invoke(&["memory", "show", "--scope", "project"]);
    assert!(success && err.is_empty());
    assert!(out.contains("repository scoped fact") && !out.contains("## user"));
    let (success, out, err) = fixture.invoke(&["filemap", "show"]);
    assert!(success && err.is_empty());
    assert!(out.contains("src/lib.rs — canonical parser") && out.contains("- legacy-path-only\n"));
    assert_eq!(tree(&fixture.root), before);
}
#[test]
fn readback_scrubs_credentials_and_refuses_terminal_controls_or_oversized_output() {
    let fixture = Fixture::new();
    let source = fixture.root.join("USER.md");
    fixture.write(
        &source,
        "- token ghp_abcdefghijklmnopqrstuvwxyz0123456789\n",
    );
    let (success, out, err) = fixture.invoke(&["memory", "show", "--scope", "user"]);
    assert!(success && err.is_empty());
    assert!(!out.contains("ghp_"));
    fixture.write(&source, "- owned escape \u{1b}[31m\n");
    let (success, out, err) = fixture.invoke(&["memory", "show", "--scope", "user"]);
    assert!(!success && out.is_empty());
    assert_eq!(err, "lore-rs: untrusted_write\n");
    fixture.write(&source, &format!("- {}\n", "x".repeat(65536)));
    let before = fs::read(&source).unwrap();
    let (success, out, err) = fixture.invoke(&["memory", "show", "--scope", "user"]);
    assert!(!success && out.is_empty());
    assert_eq!(err, "lore-rs: output_too_large\n");
    assert_eq!(fs::read(&source).unwrap(), before);
    fixture.write(&fixture.map(), "- owned-path — \u{1b}[31m\n");
    let (success, out, err) = fixture.invoke(&["filemap", "show"]);
    assert!(!success && out.is_empty());
    assert_eq!(err, "lore-rs: untrusted_write\n");
}
#[test]
fn unsafe_sources_and_unrecognized_cli_forms_are_refused() {
    let fixture = Fixture::new();
    for args in [
        vec!["memory", "add", "fact"],
        vec!["memory", "show", "--scope", "unknown"],
        vec!["memory", "show", "--scope", "user", "--host", "remote"],
        vec!["memory", "show", "--scope", "user", "--scope", "project"],
        vec!["memory", "show", "--host"],
    ] {
        let (success, out, err) = fixture.invoke(&args);
        assert!(!success && out.is_empty());
        assert_eq!(err, "lore-rs: invalid_request\n");
        assert!(!fixture.root.exists());
    }
    fs::create_dir(&fixture.root).unwrap();
    let outside = fixture.home.join("outside");
    fs::write(&outside, "- private outside fact\n").unwrap();
    symlink(&outside, fixture.root.join("USER.md")).unwrap();
    let (success, out, err) = fixture.invoke(&["memory", "show", "--scope", "user"]);
    assert!(!success && out.is_empty());
    assert_eq!(err, "lore-rs: unsafe_path\n");
    assert_eq!(
        fs::read_to_string(outside).unwrap(),
        "- private outside fact\n"
    );
    assert!(!fixture.root.join("state.db").exists());
}
