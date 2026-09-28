//! Context-advertised read-only CLI fixtures, isolated from host state.
#![cfg(target_os = "linux")]
use lore_core::{config, files, gate};
use serde_json::json;
use std::{
    fs,
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
            .stdin(Stdio::null())
            .stdout(Stdio::from(fs::File::create(&out).unwrap()))
            .stderr(Stdio::from(fs::File::create(&err).unwrap()))
            .spawn()
            .unwrap();
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
