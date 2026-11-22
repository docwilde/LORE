use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::Path,
    process::{Command, Stdio},
};

fn cli(root: &Path, recording: Option<&str>) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_lore-rs"));
    command
        .current_dir(root)
        .env_clear()
        .env("HOME", root)
        .env("LORE_ROOT", root.join("lore"))
        .env("LORE_DISABLE_SYNC", "1");
    if let Some(value) = recording {
        command.env("LORE_TOOL_EVIDENCE", value);
    }
    command
}

fn hook(
    root: &Path,
    recording: Option<&str>,
    engine: &str,
    event: &str,
    payload: &Value,
) -> std::process::Output {
    let mut child = cli(root, recording)
        .args([
            "tool-evidence",
            "hook",
            "--engine",
            engine,
            "--event",
            event,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    writeln!(child.stdin.take().unwrap(), "{payload}").unwrap();
    child.wait_with_output().unwrap()
}

#[test]
fn records_structured_completions_once_and_discards_payloads() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    let claude = |id: &str, event: &str| {
        json!({
            "hook_event_name": event, "cwd": root, "session_id": "claude-session",
            "tool_use_id": id, "tool_name": "Bash", "tool_input": {"command":"evidence-test-private-input"},
            "tool_response":"evidence-test-private-output", "error":"evidence-test-private-error",
        })
    };
    let first = claude("call-1", "PostToolUse");
    assert!(hook(root, Some("0"), "claude", "PostToolUse", &first)
        .status
        .success());
    assert!(!root.join("lore/state.db").exists());
    for _ in 0..2 {
        assert!(hook(root, None, "claude", "PostToolUse", &first)
            .status
            .success());
    }
    let failure = claude("call-2", "PostToolUseFailure");
    assert!(hook(root, None, "claude", "PostToolUseFailure", &failure)
        .status
        .success());
    let conflict = claude("call-1", "PostToolUseFailure");
    assert!(!hook(root, None, "claude", "PostToolUseFailure", &conflict)
        .status
        .success());

    let codex = |id: &str, tool: &str, response: Value| {
        json!({
            "hook_event_name": "PostToolUse", "cwd":root,"session_id":"codex-session",
            "tool_use_id":id,"tool_name":tool,"tool_response":response,
        })
    };
    for (id, response) in [
        ("mcp-success", json!({"content":[]})),
        ("mcp-failure", json!({"content":[],"isError":true})),
    ] {
        assert!(hook(
            root,
            None,
            "codex",
            "PostToolUse",
            &codex(id, "mcp__fixture__read", response)
        )
        .status
        .success());
    }
    let opaque = codex("opaque", "Bash", json!("Process exited with code 0"));
    assert!(hook(root, None, "codex", "PostToolUse", &opaque)
        .status
        .success());

    for (step, error) in [(0, ""), (1, "evidence-test-private-error")] {
        let payload = json!({"workspacePaths":[root], "conversationId":"agy-session",
            "stepIdx":step,"toolCall":{"name":"run_command","args":{"CommandLine":"evidence-test-private-input"}},
            "error":error});
        let result = hook(root, None, "antigravity", "PostToolUse", &payload);
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        assert_eq!(
            serde_json::from_slice::<Value>(&result.stdout).unwrap(),
            json!({})
        );
    }

    let listing = cli(root, None)
        .args(["tool-evidence", "list"])
        .output()
        .unwrap();
    assert!(
        listing.status.success(),
        "{}",
        String::from_utf8_lossy(&listing.stderr)
    );
    let rows: Vec<Value> = serde_json::from_slice(&listing.stdout).unwrap();
    assert_eq!(
        rows.iter()
            .map(|row| (
                row["engine"].as_str().unwrap(),
                row["call_id"].as_str().unwrap(),
                row["outcome"].as_str().unwrap()
            ))
            .collect::<Vec<_>>(),
        [
            ("antigravity", "step-1", "failure"),
            ("antigravity", "step-0", "success"),
            ("codex", "mcp-failure", "failure"),
            ("codex", "mcp-success", "success"),
            ("claude", "call-2", "failure"),
            ("claude", "call-1", "success"),
        ]
    );
    let db = fs::read(root.join("lore/state.db")).unwrap();
    assert!(!db
        .windows(b"evidence-test-private".len())
        .any(|candidate| candidate == b"evidence-test-private"));
    let rejected = cli(root, None)
        .args(["tool-evidence", "link", "1", "--belief", "1"])
        .output()
        .unwrap();
    assert!(!rejected.status.success());
    assert!(String::from_utf8_lossy(&rejected.stderr).contains("untrusted_write"));
}
