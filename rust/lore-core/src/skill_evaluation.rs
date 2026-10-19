//! Opt-in, host-executed procedural skill experiments. Results are advisory;
//! proposal bytes and human review remain the installation authority.
use crate::{config::{self, Config}, diagnostics, files, gate::Authority, pending, Error, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{collections::{BTreeMap, BTreeSet}, path::{Path, PathBuf}, process::{Command, Stdio}, time::{Duration, Instant}};

const TREE_CAP: usize = 16 * 1024 * 1024;
const OUTPUT_CAP: u64 = 1024 * 1024;

fn interpreter_bytes(path: &Path) -> Result<Vec<u8>> {
    use std::io::Read;
    let mut options = std::fs::OpenOptions::new(); options.read(true);
    #[cfg(unix)] { use std::os::unix::fs::OpenOptionsExt; options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK); }
    let mut file = options.open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.len()>128*1024*1024 { return Err(Error::UnsafePath); }
    #[cfg(unix)] {
        use std::os::unix::fs::MetadataExt;
        if ![0,unsafe {libc::geteuid()}].contains(&meta.uid()) || meta.mode() & 0o022 != 0 { return Err(Error::UnsafePath); }
    }
    let mut bytes=Vec::new(); file.by_ref().take(128*1024*1024+1).read_to_end(&mut bytes)?;
    if bytes.len()>128*1024*1024 { return Err(Error::TooLarge); }
    Ok(bytes)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Suite {
    schema: String,
    family: String,
    model: String,
    runtime: String,
    interpreter: String,
    runner: String,
    verifier: String,
    timeout_seconds: u64,
    cases: Vec<Case>,
    #[serde(default)] answer_markers: Vec<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Case { id: String, split: String, fixture: String }

fn relative(s: &str) -> bool {
    !s.is_empty() && s.len() <= 240 && Path::new(s).components().all(|c|matches!(c,std::path::Component::Normal(_)))
}
fn collect(root: &Path, path: &Path, out: &mut BTreeMap<String, Vec<u8>>, total: &mut usize) -> Result<()> {
    if path.components().count().saturating_sub(root.components().count()) > 32 { return Err(Error::TooLarge); }
    let mut names = files::directory_names(path, 512)?;
    names.sort();
    for name in names {
        let p = path.join(name);
        let meta = std::fs::symlink_metadata(&p)?;
        if meta.file_type().is_symlink() { return Err(Error::UnsafePath); }
        if meta.is_dir() { collect(root,&p,out,total)?; }
        else if meta.is_file() {
            if out.len() >= 512 { return Err(Error::TooLarge); }
            let bytes = files::read_regular(&p,TREE_CAP.saturating_sub(*total))?;
            *total += bytes.len();
            if *total > TREE_CAP { return Err(Error::TooLarge); }
            let name = p.strip_prefix(root).map_err(|_|Error::UnsafePath)?.to_str().ok_or(Error::UnsafePath)?.to_owned();
            out.insert(name,bytes);
        } else { return Err(Error::UnsafePath); }
    }
    Ok(())
}
fn tree_hash(tree: &BTreeMap<String,Vec<u8>>) -> Result<String> {
    let hashes: BTreeMap<_,_> = tree.iter().map(|(p,b)|(p,crate::digest(b))).collect();
    Ok(crate::digest(&serde_json::to_vec(&hashes).map_err(|_|Error::InvalidRequest)?))
}
fn fixture(tree: &BTreeMap<String,Vec<u8>>, prefix: &str) -> Result<BTreeMap<String,Vec<u8>>> {
    let prefix = format!("{prefix}/");
    let part: BTreeMap<_,_> = tree.iter().filter_map(|(k,v)|k.strip_prefix(&prefix).map(|k|(k.to_owned(),v.clone()))).collect();
    if part.is_empty() { return Err(Error::InvalidRequest); }
    Ok(part)
}
fn materialize(root: &Path, tree: &BTreeMap<String,Vec<u8>>) -> Result<()> {
    for (name,bytes) in tree { files::atomic_write(&root.join(name),bytes)?; }
    Ok(())
}
fn normalize(s: &str) -> String { s.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase() }

fn quality(baseline: &str, candidate: &str, markers: &[String]) -> Value {
    // The absolute cap also bounds newly created skills (empty baseline).
    let oversized = candidate.len() > 16*1024;
    let excessive_growth = !baseline.is_empty() && candidate.len() > baseline.len().saturating_mul(2)
        && candidate.len().saturating_sub(baseline.len()) > 4096;
    let normalized = normalize(candidate);
    let copied_answer = markers.iter().any(|m|normalized.contains(&normalize(m)));
    json!({"passed":!oversized && !excessive_growth && !copied_answer,
        "baseline_bytes":baseline.len(),"candidate_bytes":candidate.len(),
        "max_bytes":16*1024,"growth_allowance_bytes":4096,"growth_ratio":2,
        "oversized":oversized,"excessive_growth":excessive_growth,"answer_marker_match":copied_answer,
        "coverage":"size and operator-supplied exact answer markers; semantic generalization is unverified"})
}

/// No shell, inherited credentials or caller HOME. The selected suite is trusted
/// executable code, not an adversarial sandbox. Rehash the interpreter per run.
fn execute(interpreter: &Path, interpreter_hash: &str, script: &Path, workspace: &Path,
           skill: &Path, home: &Path, case: &Case, suite: &Suite, log: &Path) -> Result<Value> {
    if crate::digest(&interpreter_bytes(interpreter)?) != interpreter_hash { return Err(Error::Changed); }
    let stdout = log.with_extension("stdout");
    let stderr = log.with_extension("stderr");
    let out = std::fs::OpenOptions::new().write(true).create_new(true).open(&stdout)?;
    let err = std::fs::OpenOptions::new().write(true).create_new(true).open(&stderr)?;
    let mut command = Command::new(interpreter);
    command.arg(script).current_dir(workspace).env_clear()
        .env("PATH","/usr/bin:/bin").env("HOME",home).env("TMPDIR",home)
        .env("LORE_EVAL_SKILL",skill).env("LORE_EVAL_TASK_ID",&case.id)
        .env("LORE_EVAL_MODEL",&suite.model).env("LORE_EVAL_RUNTIME",&suite.runtime)
        .stdin(Stdio::null()).stdout(Stdio::from(out)).stderr(Stdio::from(err));
    #[cfg(unix)] {
        use std::os::unix::process::CommandExt;
        unsafe { command.pre_exec(|| { if libc::setsid() < 0 { return Err(std::io::Error::last_os_error()); } Ok(()) }); }
    }
    let start = Instant::now();
    let mut child = command.spawn()?;
    let mut reason = "exit";
    let status = loop {
        if stdout.metadata()?.len() > OUTPUT_CAP || stderr.metadata()?.len() > OUTPUT_CAP {
            reason = "output_limit"; break None;
        }
        if let Some(status) = child.try_wait()? { break Some(status); }
        if start.elapsed() >= Duration::from_secs(suite.timeout_seconds) { reason = "timeout"; break None; }
        std::thread::sleep(Duration::from_millis(20));
    };
    // Kill descendants even when the direct child exits successfully.
    #[cfg(unix)] unsafe { libc::kill(-(child.id() as i32),libc::SIGKILL); }
    if status.is_none() { let _ = child.kill(); }
    let _ = child.wait();
    let read_log = |p:&Path| -> Result<String> {
        use std::io::Read;
        let mut b = Vec::new();
        let mut options = std::fs::OpenOptions::new(); options.read(true);
        #[cfg(unix)] { use std::os::unix::fs::OpenOptionsExt; options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK); }
        let file=options.open(p)?;
        if !file.metadata()?.is_file() { return Err(Error::UnsafePath); }
        file.take(OUTPUT_CAP).read_to_end(&mut b)?;
        crate::scrub::scrub(&String::from_utf8_lossy(&b))
    };
    // Store hashes and short scrubbed excerpts; raw logs die with the workspace.
    let out = read_log(&stdout)?; let err = read_log(&stderr)?;
    Ok(json!({"passed":reason=="exit" && status.is_some_and(|s|s.success()),
        "exit_code":status.and_then(|s|s.code()),"termination":reason,
        "stdout_sha256":crate::digest(out.as_bytes()),"stderr_sha256":crate::digest(err.as_bytes()),
        "stdout_excerpt":out.chars().take(1500).collect::<String>(),
        "stderr_excerpt":err.chars().take(1500).collect::<String>()}))
}

fn run_case(cfg: &Config, base: &Path, suite: &Suite, tree: &BTreeMap<String,Vec<u8>>,
            interpreter: &Path, interpreter_hash: &str, case: &Case, arm: &str, skill_text: &str) -> Result<Value> {
    let dir = base.join(format!("{}-{arm}",case.id));
    let workspace = dir.join("workspace");
    materialize(&workspace,&fixture(tree,&case.fixture)?)?;
    let skill = dir.join("skill.md");
    files::atomic_write(&skill,skill_text.as_bytes())?;
    let home = dir.join("home"); files::private_dir(&home)?;
    let runner = dir.join("runner"); files::atomic_write(&runner,&tree[&suite.runner])?;
    let run = execute(interpreter,interpreter_hash,&runner,&workspace,&skill,&home,case,suite,&dir.join("run"))?;
    // Materialize verifier from frozen bytes after the candidate has executed.
    let verifier = dir.join("verifier"); files::atomic_write(&verifier,&tree[&suite.verifier])?;
    let verify = execute(interpreter,interpreter_hash,&verifier,&workspace,&skill,&home,case,suite,&dir.join("verify"))?;
    let transcript = json!({"schema":"lore.evaluation-trace.v1","case":case.id,"arm":arm,
        "skill_sha256":crate::digest(skill_text.as_bytes()),"fixture_sha256":tree_hash(&fixture(tree,&case.fixture)?)?,
        "runner":run,"verifier":verify,"verification":"host_process_exit","sandboxed":false});
    let hash = diagnostics::put(cfg,"skill-evaluations/traces",&transcript)?;
    Ok(json!({"case":case.id,"split":case.split,"arm":arm,"trace_sha256":hash,
        "passed":run["passed"]==true && verify["passed"]==true}))
}

pub fn evaluate(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    authority.require_review()?;
    let review = pending::review(cfg,req)?;
    let raw = review["raw"].as_str().ok_or(Error::InvalidRequest)?;
    let item: Value = serde_json::from_str(raw).map_err(|_|Error::InvalidRequest)?;
    if item["kind"] != "skill" || item["action"] == "retire" { return Err(Error::Unsupported); }
    let name = item["name"].as_str().filter(|s|config::valid_skill_name(s)).ok_or(Error::InvalidRequest)?;
    let body = crate::scrub::scrub(item["body"].as_str().ok_or(Error::InvalidRequest)?)?;
    let description = item["description"].as_str().map(crate::scrub::scrub).transpose()?;
    let candidate = pending::skill_file_text(name,description.as_deref(),&body);
    let baseline_path = cfg.skills.join(name).join("SKILL.md");
    let baseline = if baseline_path.try_exists()? {
        String::from_utf8(files::read_regular(&baseline_path,crate::MAX_REVIEW_BYTES)?).map_err(|_|Error::InvalidRequest)?
    } else { String::new() };
    if crate::scrub::scrub(&baseline)? != baseline { return Err(Error::Untrusted); }
    let manifest = Path::new(req["suite"].as_str().ok_or(Error::InvalidRequest)?);
    let suite_bytes = files::read_regular(manifest,crate::MAX_REVIEW_BYTES)?;
    let suite: Suite = serde_json::from_slice(&suite_bytes).map_err(|_|Error::InvalidRequest)?;
    if suite.schema != "lore.skill-suite.v1" || suite.family != "coding"
        || suite.model.trim().is_empty() || suite.runtime.trim().is_empty()
        || suite.model.len()>256 || suite.runtime.len()>256 || !Path::new(&suite.interpreter).is_absolute()
        || !(1..=120).contains(&suite.timeout_seconds) || suite.cases.len()>64
        || !relative(&suite.runner) || !relative(&suite.verifier)
        || suite.answer_markers.len()>64
        || suite.answer_markers.iter().any(|m| normalize(m).len()<24 || m.len()>4096) { return Err(Error::InvalidRequest); }
    let mut tree = BTreeMap::new(); let mut total = 0;
    let root = manifest.parent().ok_or(Error::UnsafePath)?;
    collect(root,root,&mut tree,&mut total)?;
    if !tree.contains_key(&suite.runner) || !tree.contains_key(&suite.verifier) { return Err(Error::InvalidRequest); }
    if files::read_regular(manifest,crate::MAX_REVIEW_BYTES)? != suite_bytes { return Err(Error::Changed); }
    let suite_hash = tree_hash(&tree)?;
    let manifest_object=json!({"schema":"lore.skill-suite-snapshot.v1",
        "suite_sha256":suite_hash,"manifest_sha256":crate::digest(&suite_bytes),
        "files":tree.iter().map(|(p,b)|(p.clone(),json!({"sha256":crate::digest(b),"bytes":b.len()}))).collect::<BTreeMap<_,_>>()});
    let suite_object=diagnostics::put(cfg,"skill-evaluations/suites",&manifest_object)?;
    let interpreter = std::fs::canonicalize(PathBuf::from(&suite.interpreter))?;
    let interpreter_hash = crate::digest(&interpreter_bytes(&interpreter)?);
    let mut ids = BTreeSet::new(); let mut splits = BTreeSet::new(); let mut fixtures = BTreeMap::new();
    for case in &suite.cases {
        if !config::valid_id(&case.id) || !ids.insert(&case.id) || !relative(&case.fixture)
            || !matches!(case.split.as_str(),"train"|"replay"|"validation"|"test") { return Err(Error::InvalidRequest); }
        let hash = tree_hash(&fixture(&tree,&case.fixture)?)?;
        if let Some(other) = fixtures.insert(hash,case.split.clone()) {
            if other != case.split { return Err(Error::InvalidRequest); }
        }
        splits.insert(case.split.as_str());
    }
    if splits.len()!=4 { return Err(Error::InvalidRequest); }
    let mut holdout_fixtures=Vec::new();
    for case in suite.cases.iter().filter(|c|c.split=="test") {
        holdout_fixtures.push(tree_hash(&fixture(&tree,&case.fixture)?)?);
    }
    holdout_fixtures.sort();
    // Changing a prompt, model label or runner cannot reopen the same final tests.
    let holdout_hash=crate::digest(&serde_json::to_vec(&json!({"verifier":crate::digest(&tree[&suite.verifier]),
        "fixtures":holdout_fixtures})).map_err(|_|Error::InvalidRequest)?);
    let quality = quality(&baseline,&candidate,&suite.answer_markers);
    let object = json!({"schema":"lore.skill-candidate.v1","name":name,"proposal_sha256":review["sha256"],
        "baseline":baseline,"candidate":candidate});
    let candidate_hash = diagnostics::put(cfg,"skill-evaluations/candidates",&object)?;
    let mut report = json!({"schema":"lore.skill-evaluation.v1","at":crate::utcnow(),"pid":review["pid"],
        "proposal_sha256":review["sha256"],"baseline_sha256":crate::digest(baseline.as_bytes()),
        "candidate_sha256":crate::digest(candidate.as_bytes()),"candidate_object":candidate_hash,
        "suite_sha256":suite_hash,"suite_object":suite_object,"holdout_sha256":holdout_hash,"model":suite.model,"runtime":suite.runtime,
        "interpreter_sha256":interpreter_hash,"quality":quality,"results":[],
        "status":"quality_failed","advisory":true,"final_test":"not_run","sandboxed":false});
    if quality["passed"] == true {
        let temp = cfg.root.join("skill-evaluations/work"); files::private_dir(&temp)?;
        let work = tempfile::Builder::new().prefix("evaluation-").tempdir_in(&temp)?;
        let mut rows = Vec::new();
        for case in suite.cases.iter().filter(|c|c.split!="test") {
            for (arm,text) in [("baseline",baseline.as_str()),("candidate",candidate.as_str())] {
                rows.push(run_case(cfg,work.path(),&suite,&tree,&interpreter,&interpreter_hash,case,arm,text)?);
            }
        }
        let mut passed = true; let mut scores = serde_json::Map::new();
        for split in ["train","replay","validation"] {
            let score = |arm:&str|rows.iter().filter(|r|r["split"]==split && r["arm"]==arm && r["passed"]==true).count();
            let b = score("baseline"); let c = score("candidate");
            passed &= c>=b;
            if split=="replay" { passed &= rows.iter().filter(|r|r["split"]==split && r["arm"]=="baseline" && r["passed"]==true)
                .all(|r|rows.iter().any(|s|s["case"]==r["case"] && s["arm"]=="candidate" && s["passed"]==true)); }
            scores.insert(split.into(),json!({"baseline_passed":b,"candidate_passed":c,
                "total":suite.cases.iter().filter(|c|c.split==split).count()}));
        }
        // At least one training improvement is required; ties alone cannot
        // justify growing a skill. Replay preserves each earlier success.
        passed &= scores["train"]["candidate_passed"].as_u64()>scores["train"]["baseline_passed"].as_u64();
        report["scores"] = json!(scores);
        report["status"] = json!(if passed {"validation_passed"} else {"performance_failed"});
        if passed {
            let holdout = cfg.root.join("skill-evaluations/holdouts").join(format!("{holdout_hash}.json"));
            let _lock = files::Locks::acquire(&cfg.root,std::slice::from_ref(&holdout),cfg.timeout)?;
            if holdout.try_exists()? { report["final_test"] = json!("already_consumed"); }
            else {
                // Consume before running: crashes and interruptions cannot silently
                // reuse the final test to tune a later candidate.
                files::atomic_write(&holdout,&serde_json::to_vec(&json!({"candidate_object":candidate_hash})).map_err(|_|Error::InvalidRequest)?)?;
                let mut final_passed = true;
                for case in suite.cases.iter().filter(|c|c.split=="test") {
                    for (arm,text) in [("baseline",baseline.as_str()),("candidate",candidate.as_str())] {
                        let row = run_case(cfg,work.path(),&suite,&tree,&interpreter,&interpreter_hash,case,arm,text)?;
                        if arm=="candidate" { final_passed &= row["passed"]==true; }
                        rows.push(row);
                    }
                }
                report["final_test"] = json!(if final_passed {"passed"} else {"failed"});
                report["status"] = json!(if final_passed {"passed"} else {"final_test_failed"});
            }
        }
        report["results"] = json!(rows);
    }
    // Recheck optimistic proposal and baseline identity before attaching results.
    let now = pending::review(cfg,req)?;
    if now["sha256"] != review["sha256"] { return Err(Error::Changed); }
    let current = if baseline_path.try_exists()? { files::read_regular(&baseline_path,crate::MAX_REVIEW_BYTES)? } else { vec![] };
    if current != baseline.as_bytes() { return Err(Error::Changed); }
    let hash = diagnostics::put(cfg,"skill-evaluations/reports",&report)?;
    let pid = review["pid"].as_str().ok_or(Error::InvalidRequest)?;
    files::atomic_write(&cfg.root.join("skill-evaluations/proposals").join(format!("{pid}.json")),
        &serde_json::to_vec(&json!({"report_sha256":hash})).map_err(|_|Error::InvalidRequest)?)?;
    Ok(json!({"report_sha256":hash,"status":report["status"],"quality":report["quality"],
        "scores":report["scores"],"final_test":report["final_test"],"advisory":true}))
}

pub fn show(cfg: &Config, req: &Value) -> Result<Value> {
    diagnostics::get(cfg,"skill-evaluations/reports",req["sha256"].as_str().ok_or(Error::InvalidRequest)?)
}

pub fn artifact(cfg: &Config, req: &Value) -> Result<Value> {
    let area=match req["kind"].as_str() {
        Some("candidate")=>"skill-evaluations/candidates",
        Some("trace")=>"skill-evaluations/traces",
        Some("suite")=>"skill-evaluations/suites",
        _=>return Err(Error::InvalidRequest),
    };
    diagnostics::get(cfg,area,req["sha256"].as_str().ok_or(Error::InvalidRequest)?)
}

/// Human views label missing, stale, or corrupted evidence rather than trusting
/// a model-provided `evaluation` field in the proposal itself.
pub fn summary(cfg: &Config, pid: &str, proposal_hash: &str, item: &Value) -> Value {
    let read = || -> Result<Value> {
        let path = cfg.root.join("skill-evaluations/proposals").join(format!("{pid}.json"));
        if !path.try_exists()? { return Ok(json!({"status":"not_evaluated"})); }
        let pointer: Value = serde_json::from_slice(&files::read_regular(&path,4096)?).map_err(|_|Error::InvalidRequest)?;
        let hash = pointer["report_sha256"].as_str().ok_or(Error::InvalidRequest)?;
        let report = diagnostics::get(cfg,"skill-evaluations/reports",hash)?;
        let name = item["name"].as_str().filter(|s|config::valid_skill_name(s)).ok_or(Error::InvalidRequest)?;
        let baseline = cfg.skills.join(name).join("SKILL.md");
        let baseline = if baseline.try_exists()? { files::read_regular(&baseline,crate::MAX_REVIEW_BYTES)? } else {vec![]};
        Ok(json!({"status":report["status"],"report_sha256":hash,
            "current":report["proposal_sha256"]==proposal_hash && report["baseline_sha256"]==crate::digest(&baseline),
            "binding":"proposal_and_installed_baseline",
            "quality":report["quality"],"scores":report["scores"],"final_test":report["final_test"],"advisory":true}))
    };
    read().unwrap_or_else(|e|json!({"status":"evidence_unavailable","error":e.code()}))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn human() -> Authority { Authority::HumanReview {agent:"test".into(),engine:"unknown".into()} }
    fn setup(body: &str) -> (tempfile::TempDir,Config,Value) {
        let dir=tempfile::tempdir().unwrap();
        let cfg=Config::for_root(dir.path().join("store"));
        files::atomic_write(&cfg.skills.join("sort-values/SKILL.md"),b"Sort integers.").unwrap();
        let suite_root=dir.path().join("suite");
        let source=std::fs::canonicalize(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../examples/skill-evaluation")).unwrap();
        let mut tree=BTreeMap::new(); collect(&source,&source,&mut tree,&mut 0).unwrap();
        materialize(&suite_root,&tree).unwrap();
        let cwd=dir.path().join("project");
        let pid=crate::gate::stage(&cfg,&json!({"kind":"skill","name":"sort-values","action":"update",
            "body":body,"project":config::project_slug(&cwd)}),&human()).unwrap();
        let req=json!({"pid":pid,"cwd":cwd,"suite":suite_root.join("suite.json")});
        (dir,cfg,req)
    }
    #[test]
    fn measured_candidate_keeps_human_gate_and_consumes_final_test_once() {
        let (_dir,cfg,req)=setup("Deduplicate before sorting.");
        let result=evaluate(&cfg,&req,&human()).unwrap();
        assert_eq!(result["status"],"passed");
        assert_eq!(result["scores"]["train"]["baseline_passed"],0);
        assert_eq!(result["scores"]["train"]["candidate_passed"],1);
        assert_eq!(files::read_regular(&cfg.skills.join("sort-values/SKILL.md"),1024).unwrap(),b"Sort integers.");
        let review=pending::review(&cfg,&req).unwrap();
        assert_eq!(review["skill_evaluation"]["current"],true);
        let manifest=Path::new(req["suite"].as_str().unwrap());
        let mut suite:Value=serde_json::from_slice(&std::fs::read(manifest).unwrap()).unwrap();
        suite["model"]=json!("another-model-label"); suite["cases"][3]["id"]=json!("renamed-final-test");
        files::atomic_write(manifest,&serde_json::to_vec(&suite).unwrap()).unwrap();
        let result=evaluate(&cfg,&req,&human()).unwrap();
        assert_eq!(result["status"],"validation_passed");
        assert_eq!(result["final_test"],"already_consumed");
        files::atomic_write(&cfg.skills.join("sort-values/SKILL.md"),b"Changed installed baseline.").unwrap();
        assert_eq!(pending::review(&cfg,&req).unwrap()["skill_evaluation"]["current"],false);
    }
    #[test]
    fn individual_replay_regression_fails_and_leaves_final_test_untouched() {
        let (_dir,cfg,req)=setup("Deduplicate before sorting. Break replay.");
        let result=evaluate(&cfg,&req,&human()).unwrap();
        assert_eq!(result["status"],"performance_failed");
        assert_eq!(result["final_test"],"not_run");
        assert!(!cfg.root.join("skill-evaluations/holdouts").exists());
    }
    #[test]
    fn failed_runner_cannot_claim_a_verifier_pass() {
        let (_dir,cfg,req)=setup("Deduplicate before sorting.");
        let runner=Path::new(req["suite"].as_str().unwrap()).parent().unwrap().join("runner.py");
        let mut script=std::fs::read_to_string(&runner).unwrap(); script.push_str("\nraise SystemExit(7)\n");
        files::atomic_write(&runner,script.as_bytes()).unwrap();
        assert_eq!(evaluate(&cfg,&req,&human()).unwrap()["status"],"performance_failed");
    }
    #[test]
    fn copied_answers_and_growth_are_checked_before_executing_the_suite() {
        let (_dir,cfg,req)=setup("memorized task-specific answer must never appear in a reusable skill");
        let result=evaluate(&cfg,&req,&human()).unwrap();
        assert_eq!(result["status"],"quality_failed");
        assert_eq!(result["quality"]["answer_marker_match"],true);
        assert_eq!(quality("old","new ".repeat(3000).as_str(),&[])["excessive_growth"],true);
    }
    #[test]
    fn model_supplied_metadata_cannot_authorize_execution_or_scores() {
        let (_dir,cfg,req)=setup("Deduplicate before sorting.");
        let model=Authority::Model {agent:"fake".into(),engine:"codex".into(),session_id:String::new()};
        assert_eq!(evaluate(&cfg,&req,&model),Err(Error::Untrusted));
        assert!(!cfg.root.join("skill-evaluations").exists());
    }
    #[test]
    fn duplicate_fixtures_across_splits_are_refused() {
        let (_dir,cfg,req)=setup("Deduplicate before sorting.");
        let root=Path::new(req["suite"].as_str().unwrap()).parent().unwrap();
        files::atomic_write(&root.join("tasks/test/case.json"),&std::fs::read(root.join("tasks/train/case.json")).unwrap()).unwrap();
        assert_eq!(evaluate(&cfg,&req,&human()),Err(Error::InvalidRequest));
    }
    #[test]
    fn changed_proposal_and_tampered_report_lose_current_evidence() {
        let (_dir,cfg,req)=setup("Deduplicate before sorting.");
        let result=evaluate(&cfg,&req,&human()).unwrap();
        let path=cfg.root.join("pending").join(format!("{}.json",req["pid"].as_str().unwrap()));
        let mut item:Value=serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        item["body"]=json!("Different candidate.");
        files::atomic_write(&path,&serde_json::to_vec(&item).unwrap()).unwrap();
        assert_eq!(pending::review(&cfg,&req).unwrap()["skill_evaluation"]["current"],false);
        let report=cfg.root.join("skill-evaluations/reports").join(format!("{}.json",result["report_sha256"].as_str().unwrap()));
        files::atomic_write(&report,b"{}").unwrap();
        assert_eq!(pending::review(&cfg,&req).unwrap()["skill_evaluation"]["status"],"evidence_unavailable");
    }
    #[test]
    fn timeout_is_a_failed_host_observation() {
        let (_dir,cfg,req)=setup("Deduplicate before sorting.");
        let root=Path::new(req["suite"].as_str().unwrap()).parent().unwrap();
        let mut suite:Suite=serde_json::from_slice(&std::fs::read(root.join("suite.json")).unwrap()).unwrap();
        suite.timeout_seconds=1;
        let script=root.join("sleep.py"); files::atomic_write(&script,b"import time\ntime.sleep(10)\n").unwrap();
        let interpreter=std::fs::canonicalize(&suite.interpreter).unwrap();
        let hash=crate::digest(&interpreter_bytes(&interpreter).unwrap());
        let row=execute(&interpreter,&hash,&script,root,&root.join("suite.json"),root,&suite.cases[0],&suite,&root.join("timeout")).unwrap();
        assert_eq!(row["passed"],false); assert_eq!(row["termination"],"timeout");
        assert!(!cfg.root.join("skill-evaluations/holdouts").exists());
    }
}
