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

#[test]
fn custom_settings_directory_preserves_default_root_consistency() {
    if std::env::var_os("OWNED_CUSTOM_SETTINGS_CHILD").is_some() {
        let cfg = lore_core::config::Config::from_env(std::time::Duration::from_secs(3)).unwrap();
        assert_eq!(
            cfg.root,
            std::path::PathBuf::from(std::env::var_os("HOME").unwrap()).join(".claude/lore")
        );
        assert_eq!(cfg.project_cap, 17600);
        let saved = cfg.settings().unwrap();
        assert_eq!(
            saved.get("LORE_SYNC_URL").unwrap(),
            "https://example.invalid"
        );
        assert_eq!(saved.get("LORE_DISABLE_REVIEW").unwrap(), "1");
        assert!(cfg.disabled("LORE_DISABLE_REVIEW"));
        let refresh = lore_core::context::RefreshPolicy::from_config(&cfg);
        assert_eq!(refresh.interval_secs, Some(45));
        assert!(!refresh.on_change);
        assert_eq!(
            lore_core::context::refresh_interval(&cfg, &json!({})).unwrap(),
            json!(45)
        );
        let authority = lore_core::gate::Authority::Derived {
            agent: "fixture".into(),
            engine: "claude".into(),
        };
        assert!(lore_core::dream::build(&cfg, &cfg.root, &authority)
            .unwrap()
            .is_none());
        assert!(!cfg.root.exists());

        assert_eq!(
            lore_core::sync_network::configured_targets_with_settings(&saved, None)
                .unwrap()
                .len(),
            1
        );
        return;
    }
    let home = tempfile::tempdir().unwrap();
    let directory = home.path().join("custom-claude");
    lore_core::files::atomic_write(&directory.join("settings.json"),json!({"env":{
        "LORE_MEMORY_CAP":"17600","LORE_DISABLE_REVIEW":"1","LORE_SYNC_URL":"https://example.invalid","LORE_SYNC_AUTH":"tailscale","LORE_DISABLE_BELIEFS":"1","LORE_REFRESH_SECS":"45","LORE_REFRESH_ON_CHANGE":"0"
    }}).to_string().as_bytes()).unwrap();
    let shown = show(
        home.path(),
        &[("CLAUDE_CONFIG_DIR", directory.to_str().unwrap())],
    );
    assert_eq!(shown["caps"]["project"], 17600);
    assert_eq!(
        shown["root"],
        home.path().join(".claude/lore").to_str().unwrap()
    );
    assert_eq!(shown["sync"]["hub_configured"], true);
    assert!(shown["disabled_stages"]
        .as_array()
        .unwrap()
        .contains(&json!("review")));
    let child = Command::new(std::env::current_exe().unwrap())
        .env_clear()
        .env("HOME", home.path())
        .env("TMPDIR", home.path())
        .env("CLAUDE_CONFIG_DIR", &directory)
        .env("OWNED_CUSTOM_SETTINGS_CHILD", "1")
        .args([
            "--exact",
            "custom_settings_directory_preserves_default_root_consistency",
            "--test-threads=1",
        ])
        .output()
        .unwrap();
    assert!(
        child.status.success(),
        "{}",
        String::from_utf8_lossy(&child.stdout)
    );
}
