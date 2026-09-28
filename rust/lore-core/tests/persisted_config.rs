use serde_json::{json, Value};
use std::process::Command;
fn show(home: &std::path::Path, overrides: &[(&str, &str)]) -> Value {
    let mut command = Command::new(env!("CARGO_BIN_EXE_lore-rs"));
    command
        .env_clear()
        .env("HOME", home)
        .env("TMPDIR", home)
        .args(["config", "show"]);
    for (name, value) in overrides {
        command.env(name, value);
    }
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}
#[test]
fn persisted_cli_precedence_and_isolation() {
    let home = tempfile::tempdir().unwrap();
    let path = home.path().join(".claude/settings.json");
    lore_core::files::atomic_write(&path,json!({"env":{"LORE_MEMORY_CAP":"17600","LORE_FILEMAP_CAP":"8800","LORE_SYNC_HMAC_KEY":"fixture","LORE_DISABLE_REVIEW":"1","LORE_SYNC_URL":"https://example.invalid"}}).to_string().as_bytes()).unwrap();
    let saved = show(home.path(), &[]);
    assert_eq!(saved["caps"]["project"], 17600);
    assert_eq!(saved["caps"]["filemap"], 8800);
    assert_eq!(saved["sync"]["key_configured"], true);
    assert_eq!(saved["sync"]["hub_configured"], true);
    assert!(saved["disabled_stages"]
        .as_array()
        .unwrap()
        .contains(&json!("review")));
    let overridden = show(
        home.path(),
        &[
            ("LORE_MEMORY_CAP", "9900"),
            ("LORE_SYNC_HMAC_KEY", ""),
            ("LORE_DISABLE_REVIEW", "0"),
        ],
    );
    assert_eq!(overridden["caps"]["project"], 9900);
    assert_eq!(overridden["sync"]["key_configured"], false);
    assert!(!overridden["disabled_stages"]
        .as_array()
        .unwrap()
        .contains(&json!("review")));
    let root = home.path().join("isolated");
    let isolated = show(home.path(), &[("LORE_ROOT", root.to_str().unwrap())]);
    assert_eq!(isolated["caps"]["project"], 8800);
    assert_eq!(isolated["sync"]["key_configured"], false);
    assert_eq!(isolated["sync"]["hub_configured"], false);
    assert!(isolated["disabled_stages"].as_array().unwrap().is_empty());
}
