//! End-to-end native reviewers with an owned fake provider and private stores.
//! The fixture retains the process-group leader with WNOWAIT until cleanup;
//! neither provider calls nor credentials from the surrounding host are used.
#![cfg(target_os = "linux")]
use lore_core::{config::{self, Config}, files, pending};
use serde_json::{json, Value};
use std::{fs, io::Write, os::{fd::{FromRawFd, OwnedFd, AsRawFd}, unix::{fs::{MetadataExt, PermissionsExt}, process::CommandExt}}, path::{Path, PathBuf}, process::{Child, Command, ExitStatus, Stdio}, time::{Duration, Instant}};

const PROVIDER: &str = r#"#!/usr/bin/python3
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['OWNED_FIXTURE'])
assert sys.argv[1:] == ['--bare', '-p', '--model', 'owned-model', '--allowedTools', '']
assert os.environ['LORE_SKIP'] == '1' and os.environ['LORE_DISABLE_REVIEW'] == '1'
child = subprocess.Popen(['/bin/sleep', '30'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(root / 'ready').write_text(json.dumps({'provider':os.getpid(), 'descendant':child.pid}))
mode = os.environ['OWNED_MODE']
if mode == 'blocked_stdin':
    time.sleep(30)
    sys.exit(7)
prompt = sys.stdin.read()
(root / 'prompt').write_text(prompt)
if mode == 'oversize':
    sys.stdout.write('x' * (1024 * 1024 + 1))
    sys.exit(0)
if mode == 'error':
    sys.stderr.write('owned provider failed')
    sys.exit(7)
if mode == 'changed_source':
    Path(os.environ['OWNED_SOURCE']).write_text('changed owned source\n')
print(json.dumps({'memory':[{'scope':'user','action':'add','text':'Native reviewer preserves owned evidence','source_engine':'forged'}], 'filemap':[], 'skills':[], 'skill_outcomes':[], 'conclusions':[]}))
"#;

struct Fixture { _dir:tempfile::TempDir, home:PathBuf, root:PathBuf, cwd:PathBuf, projects:PathBuf, codex:PathBuf, source:PathBuf, request:Value }
impl Fixture {
    fn new(engine:&str, large:bool)->Self {
        let dir=tempfile::tempdir().unwrap();let home=dir.path().to_path_buf();let root=home.join("lore");let cwd=home.join("repo");let projects=home.join("projects");let codex=home.join("codex/sessions");fs::create_dir(&cwd).unwrap();
        let source=if engine=="codex"{codex.join("rollout.jsonl")}else{projects.join(config::project_slug(&cwd)).join("owned-session.jsonl")};
        let text=if large{"owned source text ".repeat(400)}else{"owned source text".into()};let mut rows=Vec::new();
        if engine=="codex" {rows.push(json!({"type":"session_meta","payload":{"id":"owned-provider-thread","cwd":cwd}}));}
        for i in 0..3 {
            rows.push(if engine=="codex"{json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":format!("{text} {i}")}]}})}else{json!({"type":"user","message":{"content":format!("{text} {i}")}})});
        }
        if engine=="codex" {
            rows.push(json!({"type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"owned codex tool\"}"}}));
            rows.push(json!({"type":"response_item","payload":{"type":"custom_tool_call_output","is_error":true,"output":"owned tool error"}}));
        }
        let raw=rows.iter().map(|row|format!("{row}\n")).collect::<String>();files::atomic_write(&source,raw.as_bytes()).unwrap();let st=source.metadata().unwrap();
        let mut request=json!({"cwd":cwd,"session_id":"owned-session","transcript":source,"older":true,"agent":"forged-agent","source_engine":"forged"});
        if engine=="codex" {request["provider_thread"]=json!("owned-provider-thread");request["expected_source"]=json!({"sha256":lore_core::digest(raw.as_bytes()),"device":st.dev(),"inode":st.ino(),"size":st.len(),"ctime":st.ctime(),"ctime_nsec":st.ctime_nsec()});}
        let provider=home.join("provider");files::atomic_write(&provider,PROVIDER.as_bytes()).unwrap();fs::set_permissions(&provider,fs::Permissions::from_mode(0o700)).unwrap();
        Self{_dir:dir,home,root,cwd,projects,codex,source,request}
    }
    fn spawn(&self,engine:&str,mode:&str)->OwnedWorker {
        let output=self.home.join("stdout");let error=self.home.join("stderr");
        let mut command=Command::new(env!("CARGO_BIN_EXE_lore-rs"));
        command.args(["review-worker","--engine",engine]).env_clear()
            .env("HOME",&self.home).env("PATH","/usr/bin:/bin").env("LORE_ROOT",&self.root)
            .env("LORE_PROJECTS_DIR",&self.projects).env("LORE_CODEX_SESSIONS_DIR",&self.codex)
            .env("LORE_SKILLS_DIR",self.home.join("skills")).env("LORE_DISABLE_SYNC","1")
            .env("LORE_MACHINE_HOST","owned-machine").env("LORE_MACHINE_ID","owned-machine")
            .env("LORE_CLAUDE_BIN",self.home.join("provider")).env("LORE_DERIVER_MODEL","owned-model")
            .env("LORE_DEFER_DREAM","1").env("OWNED_FIXTURE",&self.home).env("OWNED_SOURCE",&self.source).env("OWNED_MODE",mode)
            .current_dir(&self.cwd).stdin(Stdio::piped()).stdout(Stdio::from(fs::File::create(&output).unwrap())).stderr(Stdio::from(fs::File::create(&error).unwrap()));
        // The production supervisor creates a fresh session/process group.
        unsafe{command.pre_exec(||{if libc::setsid()<0{return Err(std::io::Error::last_os_error());}Ok(())});}
        let mut child=command.spawn().unwrap();let leader=pidfd(child.id());let mut input=child.stdin.take().unwrap();writeln!(input,"{}",self.request).unwrap();drop(input);
        OwnedWorker{child:Some(child),leader,descendants:Vec::new(),output,error}
    }
    fn pending(&self)->Vec<Value> {let mut cfg=Config::for_root(self.root.clone());cfg.sync.enabled=false;pending::ids(&cfg).unwrap().iter().map(|id|pending::snapshot(&self.root.join("pending").join(format!("{id}.json"))).unwrap().item).collect()}
    fn assert_not_curated(&self){assert!(!self.root.join("USER.md").exists());assert!(!self.root.join("projects").join(config::project_slug(&self.cwd)).join("MEMORY.md").exists());}
}
fn pidfd(pid:u32)->OwnedFd {let fd=unsafe{libc::syscall(libc::SYS_pidfd_open,pid,0)} as i32;assert!(fd>=0,"owned process handle unavailable: {}",std::io::Error::last_os_error());unsafe{OwnedFd::from_raw_fd(fd)}}
fn exited(fd:&OwnedFd,timeout:Duration)->bool {let mut poll=libc::pollfd{fd:fd.as_raw_fd(),events:libc::POLLIN,revents:0};(unsafe{libc::poll(&mut poll,1,timeout.as_millis().min(i32::MAX as u128) as i32)})>0}
struct OwnedWorker {child:Option<Child>,leader:OwnedFd,descendants:Vec<OwnedFd>,output:PathBuf,error:PathBuf}
impl OwnedWorker {
    fn observe_provider(&mut self,home:&Path){let deadline=Instant::now()+Duration::from_secs(3);loop{if let Ok(raw)=fs::read(home.join("ready")){if let Ok(value)=serde_json::from_slice::<Value>(&raw){self.descendants.push(pidfd(value["descendant"].as_u64().unwrap() as u32));return;}}assert!(Instant::now()<deadline,"owned fake provider did not start");std::thread::sleep(Duration::from_millis(5));}}
    fn finish(&mut self,timeout:Duration)->Option<ExitStatus> {
        let deadline=Instant::now()+timeout;let child=self.child.as_mut().unwrap();let completed=loop{
            let mut info:libc::siginfo_t=unsafe{std::mem::zeroed()};assert_eq!(unsafe{libc::waitid(libc::P_PID,child.id(),&mut info,libc::WEXITED|libc::WNOHANG|libc::WNOWAIT)},0);
            if unsafe{info.si_pid()}!=0{break true;}if Instant::now()>=deadline{break false;}std::thread::sleep(Duration::from_millis(5));
        };
        // The unreaped leader reserves this group identity while descendants
        // are killed, including success, fixed-error and outer deadline paths.
        unsafe{libc::kill(-(child.id() as i32),libc::SIGKILL)};let status=child.wait().unwrap();self.child=None;
        assert!(exited(&self.leader,Duration::from_secs(1)));for fd in &self.descendants{assert!(exited(fd,Duration::from_secs(2)),"owned provider descendant survived");}
        completed.then_some(status)
    }
    fn stderr(&self)->String{fs::read_to_string(&self.error).unwrap()}
    fn stdout(&self)->String{fs::read_to_string(&self.output).unwrap()}
}
impl Drop for OwnedWorker {fn drop(&mut self){if let Some(child)=self.child.as_mut(){unsafe{libc::kill(-(child.id() as i32),libc::SIGKILL)};let _=child.wait();}}}

#[test]
fn claude_native_worker_stages_derived_memory_without_curated_write(){
    let fixture=Fixture::new("claude",false);let mut worker=fixture.spawn("claude","success");worker.observe_provider(&fixture.home);assert!(worker.finish(Duration::from_secs(5)).unwrap().success());assert!(worker.stderr().is_empty()&&worker.stdout().is_empty());
    let items=fixture.pending();assert_eq!(items.len(),1);assert_eq!(items[0]["source_engine"],"claude");assert_eq!(items[0]["writer"],"derived");assert_eq!(items[0]["derived_by"],"doxa-deriver");assert_eq!(items[0]["session_id"],"owned-session");fixture.assert_not_curated();assert!(fs::read_to_string(fixture.home.join("prompt")).unwrap().contains("U: owned source text 2"));
}
#[test]
fn codex_native_worker_proves_provider_source_and_keeps_raw_tools(){
    let fixture=Fixture::new("codex",false);let mut worker=fixture.spawn("codex","success");worker.observe_provider(&fixture.home);assert!(worker.finish(Duration::from_secs(5)).unwrap().success());assert!(worker.stderr().is_empty());let items=fixture.pending();assert_eq!(items.len(),1);assert_eq!(items[0]["source_engine"],"codex");assert_eq!(items[0]["writer"],"derived");assert_eq!(items[0]["session_id"],"owned-session");fixture.assert_not_curated();let prompt=fs::read_to_string(fixture.home.join("prompt")).unwrap();assert!(prompt.contains("T: exec_command:")&&prompt.contains("owned codex tool")&&prompt.contains("E: owned tool error"));
}
#[test]
fn codex_invalid_source_proof_or_identity_never_launches_provider(){
    for invalid in ["proof","thread","cwd"]{let mut fixture=Fixture::new("codex",false);match invalid{"proof"=>fixture.request["expected_source"]["sha256"]=json!("0".repeat(64)),"thread"=>fixture.request["provider_thread"]=json!("other-thread"),_=>fixture.request["cwd"]=json!(fixture.home.join("other-repo"))};let mut worker=fixture.spawn("codex","success");assert!(!worker.finish(Duration::from_secs(3)).unwrap().success());assert!(!fixture.home.join("ready").exists());assert!(fixture.pending().is_empty());fixture.assert_not_curated();assert_eq!(worker.stderr(),if invalid=="proof"{"lore-rs: review_changed\n"}else{"lore-rs: untrusted_write\n"});}
}
#[test]
fn changed_source_after_provider_cannot_stage_any_review_result(){
    let fixture=Fixture::new("codex",false);let mut worker=fixture.spawn("codex","changed_source");worker.observe_provider(&fixture.home);assert!(!worker.finish(Duration::from_secs(3)).unwrap().success());assert_eq!(worker.stderr(),"lore-rs: review_changed\n");assert!(fixture.pending().is_empty());fixture.assert_not_curated();
}
#[test]
fn provider_output_overflow_and_failure_return_fixed_errors_without_effects(){
    for mode in ["oversize","error"]{let fixture=Fixture::new("claude",false);let mut worker=fixture.spawn("claude",mode);worker.observe_provider(&fixture.home);assert!(!worker.finish(Duration::from_secs(3)).unwrap().success());assert_eq!(worker.stderr(),if mode=="oversize"{"lore-rs: output_too_large\n"}else{"lore-rs: operation_failed\n"});assert!(worker.stdout().is_empty());assert!(fixture.pending().is_empty());fixture.assert_not_curated();}
}
#[test]
fn outer_owned_deadline_cancels_provider_blocked_on_prompt_stdin(){
    let fixture=Fixture::new("claude",true);let mut worker=fixture.spawn("claude","blocked_stdin");worker.observe_provider(&fixture.home);let started=Instant::now();assert!(worker.finish(Duration::from_millis(200)).is_none());assert!(started.elapsed()<Duration::from_secs(3));assert!(fixture.pending().is_empty());fixture.assert_not_curated();
}
