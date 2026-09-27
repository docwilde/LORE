//! Native JSONL carrier. No Python interpreter, fallback or JSON authority.
use lore_core::{config::Config, gate::Authority, Core, Error};
use serde_json::{json, Value};
use std::io::{self, BufRead, Read, Write};
use std::time::Duration;

const AGENT_FRAME_BYTES: usize = 64 * 1024;
const AGENT_CAPABILITIES: &[&str] = &["agent_catalog_v1", "agent_tool_v1", "agent_status_v1"];

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    // Root's native review/worker commands extend this dispatch boundary.
    let outcome = match args.as_slice() {
        [command] if command == "bridge" => bridge(false),
        [command] if command == "agent-bridge" => bridge(true),
        _ => Err(Error::InvalidRequest),
    };
    if let Err(error) = outcome {
        eprintln!("lore-rs: {}", error.code());
        std::process::exit(1);
    }
}

fn bridge(agent: bool) -> lore_core::Result<()> {
    let config = Config::from_env(Duration::from_secs(3))?;
    let authority = if agent {
        Authority::Model { agent: "doxa".into(), engine: "unbound".into(), session_id: String::new() }
    } else {
        Authority::HumanReview { agent: "doxa-ui".into(), engine: "human".into() }
    };
    let mut core = Core::new(config, authority);
    let limit = if agent { AGENT_FRAME_BYTES } else { lore_core::MAX_FRAME_BYTES };
    let capabilities = if agent { AGENT_CAPABILITIES } else { Core::capabilities() };
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    write_frame(&mut output, &json!({"type":"hello","proto":1,"capabilities":capabilities}), limit)?;
    loop {
        let mut raw = Vec::new();
        let length = (&mut input).take(limit as u64 + 1).read_until(b'\n', &mut raw)?;
        if length == 0 { return Ok(()); }
        if length > limit || !raw.ends_with(b"\n") { return Err(Error::TooLarge); }
        let request = serde_json::from_slice::<Value>(&raw).ok().filter(Value::is_object);
        let Some(request) = request else { continue; };
        let Some(id) = request["id"].as_u64() else { continue; };
        let result = if agent { core.agent_execute(&request) } else { core.execute(&request) };
        let reply = match result {
            Ok(value) if matches!(request["op"].as_str(), Some("scrub" | "snapshot")) =>
                json!({"type":"reply","id":id,"ok":true,"text":value}),
            Ok(value) => json!({"type":"reply","id":id,"ok":true,"value":value}),
            Err(error) => json!({"type":"reply","id":id,"ok":false,"error":error.code()}),
        };
        write_frame(&mut output, &reply, limit)?;
    }
}

fn write_frame(output: &mut impl Write, reply: &Value, limit: usize) -> lore_core::Result<()> {
    let mut raw = serde_json::to_vec(reply).map_err(|_| Error::InvalidRequest)?;
    if raw.len() + 1 > limit {
        raw = serde_json::to_vec(&json!({"type":"reply","id":reply["id"],"ok":false,"error":"output_too_large"}))
            .map_err(|_| Error::InvalidRequest)?;
    }
    raw.push(b'\n');
    output.write_all(&raw)?;
    output.flush()?;
    Ok(())
}
