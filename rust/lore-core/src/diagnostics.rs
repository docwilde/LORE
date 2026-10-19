//! Local diagnostic evidence, deliberately separate from the lossy review digest.
//! Provider flags describe observations; only a host-run verifier can check a task.
use crate::{config, files, Error, Result};
use serde_json::{json, Value};
use std::{fs::File, io::{BufRead, BufReader, Seek}, path::Path};

// Leave room for the standalone carrier's pretty JSON within its 1 MiB frame.
pub const ARTIFACT_CAP: usize = 512 * 1024;
const EVENT_CAP: usize = 32 * 1024;
const LINE_CAP: u64 = 8 * 1024 * 1024;

pub fn valid_hash(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Content-addressed local objects are never replaced by a different byte sequence.
pub(crate) fn put(cfg: &config::Config, area: &str, value: &Value) -> Result<String> {
    let bytes = serde_json::to_vec(value).map_err(|_| Error::InvalidRequest)?;
    if bytes.len() > ARTIFACT_CAP { return Err(Error::TooLarge); }
    let hash = crate::digest(&bytes);
    let path = cfg.root.join(area).join(format!("{hash}.json"));
    let _lock = files::Locks::acquire(&cfg.root, std::slice::from_ref(&path), cfg.timeout)?;
    if path.try_exists()? {
        if files::read_regular(&path, ARTIFACT_CAP)? != bytes { return Err(Error::Changed); }
    } else {
        files::atomic_write(&path, &bytes)?;
    }
    Ok(hash)
}

pub(crate) fn get(cfg: &config::Config, area: &str, hash: &str) -> Result<Value> {
    if !valid_hash(hash) { return Err(Error::InvalidRequest); }
    let bytes = files::read_regular(&cfg.root.join(area).join(format!("{hash}.json")), ARTIFACT_CAP)?;
    if crate::digest(&bytes) != hash { return Err(Error::Changed); }
    serde_json::from_slice(&bytes).map_err(|_| Error::InvalidRequest)
}

struct Trace {
    events: Vec<Value>, bytes: usize, clipped: usize, dropped: usize, unsupported: usize,
}
impl Trace {
    fn event(&mut self, line: usize, kind: &str, id: &Value, name: &Value,
             content: &Value, error: &Value) -> Result<()> {
        // Scrub the entire field before clipping: credentials spanning the boundary
        // must not leave a usable prefix. JSON preserves multiline observations.
        let text = content.as_str().map(str::to_owned).unwrap_or_else(|| content.to_string());
        let text = crate::scrub::scrub(&text)?;
        let clipped = text.len() > EVENT_CAP;
        let end = if clipped {
            let mut end = EVENT_CAP;
            while !text.is_char_boundary(end) { end -= 1; }
            end
        } else { text.len() };
        let event = json!({"sequence":self.events.len(),"source_line":line,"kind":kind,
            "call_id":crate::scrub::scrub(id.as_str().unwrap_or(""))?,
            "tool":crate::scrub::scrub(name.as_str().unwrap_or(""))?,
            "content":&text[..end],"content_sha256":crate::digest(text.as_bytes()),
            "content_bytes":text.len(),"truncated":clipped,
            "provider_is_error":error.as_bool(),"verification":"unverified"});
        let n = serde_json::to_vec(&event).map_err(|_| Error::InvalidRequest)?.len();
        if self.bytes + n > ARTIFACT_CAP - 8192 { self.dropped += 1; }
        else { self.bytes += n; self.clipped += usize::from(clipped); self.events.push(event); }
        Ok(())
    }
}

pub fn parse(file: &File, codex: bool) -> Result<Value> {
    let mut copy = file.try_clone()?;
    copy.rewind()?;
    let mut reader = BufReader::new(copy);
    let mut trace = Trace { events:vec![], bytes:0, clipped:0, dropped:0, unsupported:0 };
    let mut line = Vec::new();
    let mut lines = 0;
    loop {
        use std::io::Read;
        line.clear();
        let n = reader.by_ref().take(LINE_CAP + 1).read_until(b'\n', &mut line)?;
        if n == 0 { break; }
        if n as u64 > LINE_CAP { return Err(Error::TooLarge); }
        lines += 1;
        let v: Value = serde_json::from_slice(&line).map_err(|_| Error::InvalidRequest)?;
        if !v.is_object() { return Err(Error::InvalidRequest); }
        if codex {
            if v["type"] != "response_item" { continue; }
            let p = &v["payload"];
            match p["type"].as_str().unwrap_or("") {
                "message" => {
                    let role = p["role"].as_str().unwrap_or("");
                    if !matches!(role, "user" | "assistant") { continue; }
                    for b in p["content"].as_array().ok_or(Error::InvalidRequest)? {
                        if matches!(b["type"].as_str(), Some("input_text" | "output_text")) {
                            trace.event(lines, role, &Value::Null, &Value::Null, &b["text"], &Value::Null)?;
                        } else { trace.unsupported += 1; }
                    }
                }
                "function_call" | "custom_tool_call" | "local_shell_call" | "tool_search_call" | "web_search_call" => {
                    let content = p.get("arguments").or_else(||p.get("input")).or_else(||p.get("action")).unwrap_or(&Value::Null);
                    trace.event(lines, "action", p.get("call_id").unwrap_or(&p["id"]), p.get("name").unwrap_or(&p["type"]), content, &Value::Null)?;
                }
                "function_call_output" | "custom_tool_call_output" | "tool_search_output" => {
                    trace.event(lines, "observation", &p["call_id"], &Value::Null,
                        p.get("output").or_else(||p.get("tools")).unwrap_or(&Value::Null), &p["is_error"])?;
                }
                // Reasoning and encrypted/image payloads are not text evidence.
                _ => trace.unsupported += 1,
            }
        } else {
            let role = v["type"].as_str().unwrap_or("");
            if !matches!(role, "user" | "assistant") || v["isMeta"] == true { continue; }
            let content = &v["message"]["content"];
            if content.is_string() {
                trace.event(lines, role, &Value::Null, &Value::Null, content, &Value::Null)?;
            } else {
                for b in content.as_array().ok_or(Error::InvalidRequest)? {
                    match b["type"].as_str().unwrap_or("") {
                        "text" => trace.event(lines, role, &Value::Null, &Value::Null, &b["text"], &Value::Null)?,
                        "tool_use" => trace.event(lines, "action", &b["id"], &b["name"], &b["input"], &Value::Null)?,
                        "tool_result" => trace.event(lines, "observation", &b["tool_use_id"], &Value::Null, &b["content"], &b["is_error"])?,
                        _ => trace.unsupported += 1,
                    }
                }
            }
        }
    }
    Ok(json!({"events":trace.events,"source_lines":lines,"truncated_events":trace.clipped,
        "dropped_events":trace.dropped,"unsupported_blocks":trace.unsupported,
        "complete":trace.clipped == 0 && trace.dropped == 0 && trace.unsupported == 0}))
}

pub fn capture(cfg: &config::Config, req: &Value) -> Result<Value> {
    let path = Path::new(req["transcript"].as_str().ok_or(Error::InvalidRequest)?);
    let sid = req["session_id"].as_str().filter(|s|config::valid_id(s)).ok_or(Error::InvalidRequest)?;
    let engine = req["engine"].as_str().filter(|s|matches!(*s,"claude"|"codex")).ok_or(Error::InvalidRequest)?;
    let (file, proof) = crate::review::snapshot_source(path)?;
    let mut trace = parse(&file, engine == "codex")?;
    let (_, after) = crate::review::snapshot_source(path)?;
    if proof != after { return Err(Error::Changed); }
    trace["schema"] = json!("lore.diagnostic-trace.v1");
    trace["session_id"] = json!(sid);
    trace["engine"] = json!(engine);
    trace["source_sha256"] = proof["sha256"].clone();
    trace["source_bytes"] = proof["size"].clone();
    trace["task_outcome"] = json!("unverified");
    let hash = put(cfg, "diagnostic-traces", &trace)?;
    Ok(json!({"sha256":hash,"source_sha256":proof["sha256"],"events":trace["events"].as_array().map(Vec::len),
        "complete":trace["complete"],"task_outcome":"unverified"}))
}

pub fn show(cfg: &config::Config, req: &Value) -> Result<Value> {
    get(cfg, "diagnostic-traces", req["sha256"].as_str().ok_or(Error::InvalidRequest)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    fn parsed(rows: &[Value], codex: bool) -> Value {
        let mut f = tempfile::tempfile().unwrap();
        for row in rows { writeln!(f,"{row}").unwrap(); }
        parse(&f,codex).unwrap()
    }
    #[test]
    fn successful_observations_are_linked_and_never_task_verification() {
        for codex in [false,true] {
            let rows = if codex { vec![
                json!({"type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"a","arguments":"{\"cmd\":\"cargo test\"}"}}),
                json!({"type":"response_item","payload":{"type":"function_call_output","call_id":"a","output":"all tests passed\n42 cases"}})
            ] } else { vec![
                json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":"a","name":"Bash","input":{"command":"cargo test"}}]}}),
                json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"a","content":"all tests passed\n42 cases","is_error":false}]}})
            ] };
            let trace = parsed(&rows,codex);
            assert_eq!(trace["events"][1]["call_id"],"a");
            assert_eq!(trace["events"][1]["kind"],"observation");
            assert_eq!(trace["events"][1]["content"],"all tests passed\n42 cases");
            assert_eq!(trace["events"][1]["verification"],"unverified");
            assert_eq!(trace["complete"],true);
        }
    }
    #[test]
    fn clipping_and_unsupported_content_are_explicit() {
        let t = parsed(&[json!({"type":"assistant","message":{"content":[
            {"type":"text","text":"trace output ".repeat(EVENT_CAP/10)}, {"type":"image"}]}})],false);
        assert_eq!(t["complete"],false);
        assert_eq!(t["truncated_events"],1);
        assert_eq!(t["unsupported_blocks"],1);
    }
    #[test]
    fn source_is_scrubbed_before_any_cut_and_tampered_objects_refuse() {
        let secret=format!("sk-{}","a".repeat(80));
        let text=format!("{}{}", "ordinary output ".repeat(EVENT_CAP/16),secret);
        let t=parsed(&[json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"a","content":text,"is_error":true}]}})],false);
        assert!(!t.to_string().contains("sk-"));
        assert_eq!(t["events"][0]["provider_is_error"],true);
        let dir=tempfile::tempdir().unwrap(); let cfg=config::Config::for_root(dir.path().join("store"));
        let hash=put(&cfg,"diagnostic-traces",&t).unwrap();
        files::atomic_write(&cfg.root.join("diagnostic-traces").join(format!("{hash}.json")),b"{}").unwrap();
        assert_eq!(get(&cfg,"diagnostic-traces",&hash),Err(Error::Changed));
    }
}
