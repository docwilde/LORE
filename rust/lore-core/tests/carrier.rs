//! Native carrier fixtures: private HOME/root, no provider or existing store.
use serde_json::{json, Value};
use std::{fs, os::unix::process::CommandExt, process::{Command, Stdio}, time::{Duration, Instant}};

fn invoke(command: &str, input: &[u8]) -> (bool, String, String, bool) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("lore");
    let source = dir.path().join("input");
    fs::write(&source, input).unwrap();
    let output = dir.path().join("output");
    let error = dir.path().join("error");
    let mut child = Command::new(env!("CARGO_BIN_EXE_lore-rs"))
        .arg(command).env_clear().env("HOME",dir.path()).env("LORE_ROOT",&root)
        .env("LORE_PROJECTS_DIR",dir.path().join("projects"))
        .env("LORE_CODEX_SESSIONS_DIR",dir.path().join("codex-sessions"))
        .env("LORE_DISABLE_SYNC","1").process_group(0)
        .stdin(Stdio::from(fs::File::open(source).unwrap()))
        .stdout(Stdio::from(fs::File::create(&output).unwrap()))
        .stderr(Stdio::from(fs::File::create(&error).unwrap())).spawn().unwrap();
    let deadline = Instant::now()+Duration::from_secs(3);
    loop {
        let mut info: libc::siginfo_t = unsafe {std::mem::zeroed()};
        let ready = unsafe {libc::waitid(libc::P_PID,child.id(),&mut info,libc::WEXITED|libc::WNOHANG|libc::WNOWAIT)};
        assert_eq!(ready,0);
        if unsafe {info.si_pid()}!=0 {break;}
        if Instant::now()>=deadline {
            unsafe {libc::kill(-(child.id() as i32),libc::SIGKILL)};
            let _=child.wait();
            panic!("native carrier exceeded owned fixture deadline");
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    // Kill any owned descendants before releasing/reaping the leader identity.
    unsafe {libc::kill(-(child.id() as i32),libc::SIGKILL)};
    let success=child.wait().unwrap().success();
    let out=fs::read_to_string(output).unwrap();let err=fs::read_to_string(error).unwrap();
    (success,out,err,root.exists())
}

#[test]
fn ui_carrier_scrubs_without_opening_store_and_returns_only_fixed_errors() {
    let requests=[json!({"id":1,"op":"scrub","text":"ordinary owned fixture"}),
        json!({"id":2,"op":"unknown_secret_payload","payload":"never echoed"})];
    let input=requests.iter().map(|v|format!("{v}\n")).collect::<String>();
    let (success,out,err,store)=invoke("bridge",input.as_bytes());
    assert!(success && err.is_empty() && !store);
    let rows=out.lines().map(|s|serde_json::from_str::<Value>(s).unwrap()).collect::<Vec<_>>();
    assert_eq!(rows.len(),3);assert_eq!(rows[0]["proto"],1);
    assert!(rows[0]["capabilities"].as_array().unwrap().iter().any(|v|v=="resolve_reviewed_v1"));
    assert_eq!(rows[1],json!({"type":"reply","id":1,"ok":true,"text":"ordinary owned fixture"}));
    assert_eq!(rows[2],json!({"type":"reply","id":2,"ok":false,"error":"unavailable_operation"}));
    assert!(!out.contains("never echoed")&&!out.contains("unknown_secret_payload"));
}

#[test]
fn agent_cannot_operate_before_frozen_host_catalog_binding() {
    let input=format!("{}\n",json!({"id":1,"op":"agent_tool_v1","name":"lore_memory_list","arguments":{"scope":"user"}}));
    let (success,out,err,store)=invoke("agent-bridge",input.as_bytes());
    assert!(success && err.is_empty() && !store);
    let rows=out.lines().map(|s|serde_json::from_str::<Value>(s).unwrap()).collect::<Vec<_>>();
    assert_eq!(rows[0]["capabilities"].as_array().unwrap().len(),3);
    assert_eq!(rows[1]["ok"],false);
}

#[test]
fn unterminated_oversize_jsonl_is_refused_at_finite_frame_limit() {
    let (success,out,err,store)=invoke("agent-bridge",&vec![b'x';64*1024+1]);
    assert!(!success&&!store);
    assert_eq!(out.lines().count(),1);
    assert_eq!(err,"lore-rs: output_too_large\n");
}
