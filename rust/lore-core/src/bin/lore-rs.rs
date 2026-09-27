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
        [command,flag,engine] if command == "review-worker" && flag=="--engine" => review_worker(engine),
        [group, command, options @ ..] if group == "memory" && command == "show" => memory_show(options),
        [group, command] if group == "filemap" && command == "show" => filemap_show(),
        _ => Err(Error::InvalidRequest),
    };
    if let Err(error) = outcome {
        eprintln!("lore-rs: {}", error.code());
        std::process::exit(1);
    }
}

fn review_worker(engine:&str)->lore_core::Result<()> {
    let stdin=io::stdin();let mut input=stdin.lock();let mut raw=Vec::new();
    let length=(&mut input).take(16*1024+1).read_until(b'\n',&mut raw)?;
    if length>16*1024||!raw.ends_with(b"\n"){return Err(Error::TooLarge);}
    let request=serde_json::from_slice::<Value>(&raw).map_err(|_|Error::InvalidRequest)?;
    if !request.is_object(){return Err(Error::InvalidRequest);}
    let config=Config::from_env(Duration::from_secs(3))?;
    lore_core::worker::run(&config,&request,engine)
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

// Readback advertised by native context. These commands open only existing
// curated files/labels; they never initialize a database, lock or store.
const SHOW_BYTES: usize = 64 * 1024;
fn show_output(text: String) -> lore_core::Result<()> {
    if text.len() > SHOW_BYTES { return Err(Error::TooLarge); }
    if text.chars().any(|c| c.is_control() && c != '\n') { return Err(Error::Untrusted); }
    let text = lore_core::scrub::scrub(&text)?;
    if text.len() > SHOW_BYTES { return Err(Error::TooLarge); }
    let mut stdout = io::stdout().lock();
    stdout.write_all(text.as_bytes())?;
    stdout.flush()?;
    Ok(())
}
fn memory_show(options: &[String]) -> lore_core::Result<()> {
    use lore_core::{config, gate, memory::{self, Scope}};
    let mut scope = None;
    let mut host = None;
    let mut at = 0;
    while at < options.len() {
        let value = options.get(at + 1).ok_or(Error::InvalidRequest)?;
        match options[at].as_str() {
            "--scope" if scope.is_none() => scope = Some(Scope::parse(value)?),
            "--host" if host.is_none() && !value.is_empty() && value.chars().count() <= 255
                && !value.chars().any(char::is_control) => host = Some(value.as_str()),
            _ => return Err(Error::InvalidRequest),
        }
        at += 2;
    }
    if host.is_some() && scope.is_some_and(|scope| scope != Scope::Machine) { return Err(Error::InvalidRequest); }
    let cfg = Config::from_env(Duration::from_secs(3))?;
    let cwd = std::env::current_dir()?;
    let slug = config::project_slug(&cwd);
    let mut out = String::new();
    for tier in [Scope::User, Scope::Project, Scope::Machine] {
        if scope.is_some_and(|scope| scope != tier) { continue; }
        let key = if tier == Scope::Machine { memory::resolve_machine(&cfg, host) } else { slug.clone() };
        let entries = memory::read_entries(&tier.path(&cfg, &key)?)?;
        let label = if tier == Scope::Machine { format!("machine — {key}") } else { tier.name().into() };
        out.push_str(&format!("## {label} ({}){}\n", memory::usage_line(&entries, tier.cap(&cfg)), gate::provenance_tag(&cfg, "memory", &tier.bucket(&key), &entries)));
        if entries.is_empty() { out.push_str("(empty)\n"); }
        else {
            for (entry, source) in entries.iter().zip(gate::source_labels(&cfg, &tier.bucket(&key), &entries)) {
                out.push_str(&format!("- {entry}{}\n", source.map(|engine| format!(" [source: {engine}]")).unwrap_or_default()));
                if out.len() > SHOW_BYTES { return Err(Error::TooLarge); }
            }
        }
        if tier == Scope::Machine {
            let others = memory::known_machines(&cfg).into_iter().filter(|name| name != &key).collect::<Vec<_>>();
            if !others.is_empty() { out.push_str(&format!("\nother machines on file: {} — lore-rs memory show --scope machine --host <name>\n", others.join(", "))); }
        }
    }
    show_output(out)
}
fn filemap_show() -> lore_core::Result<()> {
    use lore_core::{config, filemap, gate, memory};
    let cfg = Config::from_env(Duration::from_secs(3))?;
    let cwd = std::env::current_dir()?;
    let slug = config::project_slug(&cwd);
    let entries = memory::read_entries(&filemap::path(&cfg, &slug)?)?;
    let out = format!("## file map ({}) — {slug}{}\n{}\n", memory::usage_line(&entries, cfg.filemap_cap), gate::provenance_tag(&cfg, "filemap", &slug, &entries), if entries.is_empty() { "(empty)".into() } else { memory::render_entries(&entries).trim_end().to_owned() });
    show_output(out)
}
