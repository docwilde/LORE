use lore_core::{config::Config, files};
use serde_json::{json, Value};
use std::{
    io::Write,
    path::Path,
    process::{Command, Output, Stdio},
};

fn hook(home: &Path, cfg: &Config, event: &str) -> Output {
    let input = json!({"cwd":home,"session_id":"omp-fixture","prompt":"fixture"});
    let mut child = Command::new(env!("CARGO_BIN_EXE_lore-rs"))
        .args(["hook", "--engine", "omp", "--event", event])
        .env_clear()
        .env("HOME", home)
        .env("LORE_ROOT", &cfg.root)
        .env("LORE_PROJECTS_DIR", &cfg.projects)
        .env("LORE_DISABLE_SYNC", "1")
        .env("LORE_CLAUDE_BIN", "/provider/must/never/be/launched")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    write!(child.stdin.as_mut().unwrap(), "{input}").unwrap();
    drop(child.stdin.take());
    child.wait_with_output().unwrap()
}

#[test]
fn omp_startup_injects_memory_and_refuses_transcript_review() {
    let home = tempfile::tempdir().unwrap();
    let cfg = Config::for_root(home.path().join("store"));
    files::private_dir(&cfg.root).unwrap();
    std::fs::write(cfg.root.join("USER.md"), "- Curated startup fixture\n").unwrap();

    let output = hook(home.path(), &cfg, "session-start");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let frame: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(frame["hookSpecificOutput"]["hookEventName"], "SessionStart");
    let text = frame["hookSpecificOutput"]["additionalContext"]
        .as_str()
        .unwrap();
    assert!(text.contains("Curated startup fixture"));
    assert!(text.contains("In oh-my-pi, this snapshot is injected"));

    assert!(!hook(home.path(), &cfg, "session-end").status.success());
}
