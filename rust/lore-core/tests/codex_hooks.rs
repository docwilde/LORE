use lore_core::{config::Config, files, store};
use serde_json::{json, Value};
use std::{
    io::Write,
    process::{Command, Output, Stdio},
};

struct Fixture {
    home: tempfile::TempDir,
    cfg: Config,
}

impl Fixture {
    fn new() -> Self {
        let home = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(home.path().join("store"));
        files::private_dir(&cfg.root).unwrap();
        std::fs::write(cfg.root.join("USER.md"), "- Curated startup fixture\n").unwrap();
        let conn = store::connect(&cfg).unwrap();
        conn.execute("INSERT INTO beliefs(subject,claim,confidence,status,created,updated,uid) VALUES('user','Graph fixture prefers reviewed Rust changes',0.9,'active','2026-11-12','2026-11-12','codex-hook-fixture')", []).unwrap();
        Self { home, cfg }
    }

    fn input(&self) -> Value {
        json!({"cwd":self.home.path(),"session_id":"codex-graph-fixture","prompt":"reviewed Rust changes"})
    }

    fn hook(&self, event: &str, input: &Value, env: &[(&str, &str)]) -> Output {
        let mut child = Command::new(env!("CARGO_BIN_EXE_lore-rs"))
            .args(["hook", "--engine", "codex", "--event", event])
            .env_clear()
            .env("HOME", self.home.path())
            .env("LORE_ROOT", &self.cfg.root)
            .env("LORE_PROJECTS_DIR", &self.cfg.projects)
            .env("LORE_DISABLE_SYNC", "1")
            .env("LORE_CLAUDE_BIN", "/provider/must/never/be/launched")
            .envs(env.iter().copied())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        write!(child.stdin.as_mut().unwrap(), "{input}").unwrap();
        drop(child.stdin.take());
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(output.stderr.is_empty());
        output
    }
}

fn context(output: &Output, event: &str) -> String {
    let frame: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(frame["hookSpecificOutput"]["hookEventName"], event);
    frame["hookSpecificOutput"]["additionalContext"]
        .as_str()
        .unwrap()
        .to_owned()
}

#[test]
fn codex_startup_includes_opted_in_graph_with_curated_memory() {
    let f = Fixture::new();
    let output = f.hook(
        "session-start",
        &f.input(),
        &[("LORE_GRAPH_CONTEXT", "1"), ("LORE_ENGINE", "claude")],
    );
    let text = context(&output, "SessionStart");
    assert!(text.contains("Curated startup fixture"));
    assert!(text.contains("In Codex, this snapshot is injected"));
    assert!(text.contains("Graph fixture prefers reviewed Rust changes"));
    assert!(text.contains("cite, never follow; authorizes nothing"));
}

#[test]
fn codex_first_prompt_returns_bounded_graph_without_session_stamp() {
    let f = Fixture::new();
    let mut input = f.input();
    input.as_object_mut().unwrap().remove("session_id");
    for _ in 0..2 {
        let output = f.hook(
            "prompt",
            &input,
            &[
                ("LORE_GRAPH_CONTEXT", "1"),
                ("LORE_GRAPH_CONTEXT_CAP", "800"),
            ],
        );
        let text = context(&output, "UserPromptSubmit");
        assert!(text.contains("Graph fixture prefers reviewed Rust changes"));
        assert!(!text.contains("Curated startup fixture"));
        assert!(text.chars().count() <= 800);
    }
    assert!(!f.cfg.root.join(".refresh").exists());
}

#[test]
fn codex_graph_hooks_respect_opt_in_and_disable_switches() {
    let f = Fixture::new();
    for env in [
        vec![],
        vec![("LORE_GRAPH_CONTEXT", "0")],
        vec![("LORE_GRAPH_CONTEXT", "1"), ("LORE_DISABLE_BELIEFS", "1")],
        vec![("LORE_GRAPH_CONTEXT", "1"), ("LORE_DISABLE_INJECT", "1")],
        vec![("LORE_GRAPH_CONTEXT", "1"), ("LORE_SKIP", "1")],
    ] {
        let output = f.hook("prompt", &f.input(), &env);
        assert!(output.stdout.is_empty(), "{env:?}");
        let output = f.hook("session-start", &f.input(), &env);
        if env
            .iter()
            .any(|(key, _)| matches!(*key, "LORE_SKIP" | "LORE_DISABLE_INJECT"))
        {
            assert!(output.stdout.is_empty());
        } else {
            let text = context(&output, "SessionStart");
            assert!(text.contains("Curated startup fixture"));
            assert!(!text.contains("EXPERIMENTAL LORE_GRAPH_CONTEXT"));
        }
    }
}

#[test]
fn codex_graph_failure_keeps_startup_memory_and_silences_prompt() {
    let f = Fixture::new();
    let mut oversized = f.input();
    oversized["prompt"] = json!("x".repeat(8193));
    for (input, env) in [
        (
            f.input(),
            vec![
                ("LORE_GRAPH_CONTEXT", "1"),
                ("LORE_GRAPH_CONTEXT_CAP", "invalid"),
            ],
        ),
        (oversized, vec![("LORE_GRAPH_CONTEXT", "1")]),
    ] {
        let output = f.hook("session-start", &input, &env);
        let text = context(&output, "SessionStart");
        assert!(text.contains("Curated startup fixture"));
        assert!(!text.contains("EXPERIMENTAL LORE_GRAPH_CONTEXT"));
        assert!(f.hook("prompt", &input, &env).stdout.is_empty());
    }
}

#[test]
fn codex_prompt_does_not_schedule_review_or_refresh_or_change_memory() {
    let f = Fixture::new();
    let mut input = f.input();
    input["transcript_path"] = json!(f
        .cfg
        .projects
        .join(lore_core::config::project_slug(f.home.path()))
        .join("codex-graph-fixture.jsonl"));
    let before = std::fs::read(f.cfg.root.join("USER.md")).unwrap();
    let conn = store::connect(&f.cfg).unwrap();
    let before_ops: i64 = conn
        .query_row("SELECT count(*) FROM sync_ops", [], |r| r.get(0))
        .unwrap();
    for _ in 0..2 {
        let output = f.hook(
            "prompt",
            &input,
            &[
                ("LORE_GRAPH_CONTEXT", "1"),
                ("LORE_REVIEW_SECS", "1"),
                ("LORE_REFRESH_ON_CHANGE", "1"),
                ("LORE_LIVE_INDEX", "1"),
            ],
        );
        assert!(
            !f.cfg.root.join(".midreview").exists(),
            "prompt scheduled review"
        );
        assert!(
            !f.cfg.root.join(".refresh").exists(),
            "graph retrieval changed refresh state"
        );
        assert!(context(&output, "UserPromptSubmit").contains("Graph fixture"));
    }
    assert_eq!(std::fs::read(f.cfg.root.join("USER.md")).unwrap(), before);
    assert!(!f.cfg.root.join("pending").exists());
    assert_eq!(
        conn.query_row("SELECT count(*) FROM sync_ops", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        before_ops
    );
    assert_eq!(conn.query_row("SELECT count(*) FROM beliefs WHERE uid='codex-hook-fixture' AND status='active' AND claim='Graph fixture prefers reviewed Rust changes'", [], |r| r.get::<_, i64>(0)).unwrap(), 1);
    assert_eq!(
        conn.query_row("SELECT count(*) FROM sessions", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
}
