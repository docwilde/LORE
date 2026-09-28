//! Standalone carrier. Command arguments contain data; runtime binds authority.
use crate::{
    config::{self, Config},
    gate::Authority,
    Core, Error, Result,
};
use serde_json::{json, Value};
use std::{
    io::{self, BufRead, IsTerminal, Read, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    time::Duration,
};
const HELP:&str="LORE native\nUsage: lore-rs COMMAND [SUBCOMMAND] [options]\nCommands: snapshot inject refresh memory filemap belief evidence graph search session index history pending approve reject review dream skills consult ask outcome status stats doctor setup config sync mcp\nUse --json '{...}' for exact structured arguments; --cwd defaults to this directory.\nSync: status health whoami push pull serve export import classes login resign\nPending: list show PID; approve/reject PID --expected '{\"sha256\":...,\"inode\":...}'\n";
pub fn authority() -> Authority {
    let engine = std::env::var("LORE_ENGINE").unwrap_or_else(|_| "unknown".into());
    let marker = std::env::var("AI_AGENT").unwrap_or_default();
    let claude = std::env::var("CLAUDECODE").is_ok_and(|s| !s.is_empty())
        || marker.starts_with("claude-code");
    let project = std::env::var("CLAUDE_PROJECT_DIR").is_ok_and(|s| !s.is_empty());
    let agent = "lore-cli".to_string();
    if marker.ends_with("_harness") || claude && project && !marker.ends_with("_agent") {
        return Authority::Model {
            agent,
            engine,
            session_id: String::new(),
        };
    }
    if marker.ends_with("_agent")
        || claude
        || std::env::var("LORE_WRITE_GATE")
            .is_ok_and(|s| matches!(s.as_str(), "off" | "0" | "false"))
    {
        return Authority::Interactive { agent, engine };
    }
    if io::stdin().is_terminal() {
        Authority::HumanReview { agent, engine }
    } else {
        Authority::Model {
            agent,
            engine,
            session_id: String::new(),
        }
    }
}
fn output(value: Value) -> Result<()> {
    let mut bytes = if let Some(s) = value.as_str() {
        s.as_bytes().to_vec()
    } else {
        serde_json::to_vec_pretty(&value).map_err(|_| Error::Unavailable)?
    };
    if bytes.len() > crate::MAX_FRAME_BYTES {
        return Err(Error::TooLarge);
    }
    if !bytes.ends_with(b"\n") {
        bytes.push(b'\n')
    }
    io::stdout().lock().write_all(&bytes)?;
    Ok(())
}
fn parse(args: &[String]) -> Result<(Value, Vec<String>)> {
    let mut req = json!({"cwd":std::env::current_dir()?});
    let mut positional = Vec::new();
    let mut index = 0;
    while index < args.len() {
        let arg = &args[index];
        if arg == "--" {
            positional.extend_from_slice(&args[index + 1..]);
            break;
        }
        if let Some(key) = arg.strip_prefix("--") {
            if key == "json" && !args.get(index + 1).is_some_and(|s| s.starts_with('{')) {
                req["output_json"] = json!(true);
                index += 1;
                continue;
            }
            if key == "json" {
                let raw = args.get(index + 1).ok_or(Error::InvalidRequest)?;
                if raw.len() > crate::MAX_FRAME_BYTES {
                    return Err(Error::TooLarge);
                }
                let object: Value = serde_json::from_str(raw).map_err(|_| Error::InvalidRequest)?;
                for (k, v) in object.as_object().ok_or(Error::InvalidRequest)? {
                    req[k] = v.clone()
                }
                index += 2;
                continue;
            }
            let key = match key {
                "from" => "from_seq",
                "depth" => "hops",
                other => other,
            }
            .replace('-', "_");
            let value = if matches!(
                key.as_str(),
                "force"
                    | "all"
                    | "dry_run"
                    | "browser"
                    | "merge"
                    | "index"
                    | "beliefs"
                    | "apply"
                    | "replace_foreign_key"
                    | "latest"
                    | "foreground"
                    | "full"
                    | "list"
                    | "cluster"
                    | "include_dormant"
                    | "history"
                    | "asserted"
                    | "mermaid"
                    | "no_open"
            ) {
                json!(true)
            } else {
                index += 1;
                let raw = args.get(index).ok_or(Error::InvalidRequest)?;
                if matches!(key.as_str(), "expected" | "ids" | "exclude_ids" | "payload") {
                    serde_json::from_str(raw).map_err(|_| Error::InvalidRequest)?
                } else if matches!(
                    key.as_str(),
                    "limit"
                        | "offset"
                        | "belief_id"
                        | "src"
                        | "dst"
                        | "hops"
                        | "cap"
                        | "confidence"
                        | "spawn_depth"
                        | "port"
                        | "from_seq"
                        | "sample"
                        | "threshold"
                        | "max_edges"
                        | "workers"
                        | "jobs"
                        | "context"
                        | "trunc"
                        | "max_hops"
                ) {
                    serde_json::from_str(raw).map_err(|_| Error::InvalidRequest)?
                } else {
                    json!(raw)
                }
            };
            if req.get(&key).is_some() && key != "cwd" {
                if key == "project" || key == "subject" {
                    let old = req[&key].clone();
                    let mut values = if let Some(a) = old.as_array() {
                        a.clone()
                    } else {
                        vec![old]
                    };
                    values.push(value);
                    req[&key] = json!(values);
                    index += 1;
                    continue;
                }
                return Err(Error::InvalidRequest);
            }
            req[key] = value;
        } else {
            positional.push(arg.clone())
        }
        index += 1
    }
    Ok((req, positional))
}
fn pos(req: &mut Value, name: &str, p: &[String], join: bool) -> Result<()> {
    if req.get(name).is_none() {
        let value = if join {
            p.join(" ")
        } else {
            p.first().cloned().ok_or(Error::InvalidRequest)?
        };
        if value.is_empty() {
            return Err(Error::InvalidRequest);
        }
        req[name] = json!(value)
    }
    Ok(())
}
fn id(req: &mut Value, p: &[String]) -> Result<()> {
    if req.get("belief_id").is_none() {
        req["belief_id"] = json!(p
            .first()
            .ok_or(Error::InvalidRequest)?
            .parse::<i64>()
            .map_err(|_| Error::InvalidRequest)?)
    }
    Ok(())
}
pub fn run(args: &[String]) -> Result<()> {
    if args.is_empty()
        || args
            .iter()
            .any(|s| matches!(s.as_str(), "--help" | "help" | "-h"))
    {
        return output(json!(HELP));
    }
    if matches!(args[0].as_str(), "--version" | "version") {
        return output(json!(env!("CARGO_PKG_VERSION")));
    }
    let cfg = Config::from_env(Duration::from_secs(3))?;
    if args[0] == "hook" {
        return hook(&cfg, &args[1..]);
    }
    if args[0] == "mcp" {
        return mcp(&cfg, &args[1..]);
    }
    let group = args[0].as_str();
    let grouped = matches!(
        group,
        "memory"
            | "filemap"
            | "belief"
            | "graph"
            | "history"
            | "skills"
            | "sync"
            | "config"
            | "pending"
    );
    let (sub, tail) = if grouped && args.get(1).is_some_and(|s| !s.starts_with('-')) {
        (args[1].as_str(), &args[2..])
    } else {
        ("", &args[1..])
    };
    let (mut req, p) = parse(tail)?;
    let auth = authority();
    let mut core = Core::new(cfg.clone(), auth.clone());
    let value = match (group, sub) {
        ("snapshot" | "inject", _) => crate::context::snapshot(&cfg, &req)?,
        ("refresh", _) => crate::context::refresh(&cfg, &req)?,
        ("memory", "show" | "entries" | "list") => {
            if req.get("scope").is_some() {
                crate::memory::entries(&cfg, &req)?
            } else {
                let mut out = json!({});
                for scope in ["user", "project", "machine"] {
                    req["scope"] = json!(scope);
                    out[scope] = crate::memory::entries(&cfg, &req)?
                }
                out
            }
        }
        ("memory", "review") => crate::memory::review(&cfg, &req)?,
        ("memory", "usage") => crate::memory::usage(&cfg, &req)?,
        ("memory", action @ ("add" | "replace" | "remove" | "move")) => {
            req["action"] = json!(action);
            if matches!(action, "add" | "replace") {
                pos(&mut req, "text", &p, true)?
            }
            if let Some(to) = req.get("to_machine").cloned() {
                req["to_scope"] = json!("machine");
                req["to"] = to
            }
            crate::memory::direct_action(&cfg, &req, &auth)?
        }
        ("filemap", "show") => crate::filemap::show(&cfg, &req)?,
        ("filemap", action @ ("add" | "replace" | "remove")) => {
            req["action"] = json!(action);
            if action != "remove" {
                pos(&mut req, "path", &p, false)?;
                pos(&mut req, "purpose", p.get(1..).unwrap_or(&[]), true)?
            }
            crate::filemap::action(&cfg, &req, &auth)?
        }
        ("belief", "list") => crate::beliefs::list(&cfg, &req)?,
        ("belief", "search") => {
            pos(&mut req, "query", &p, true)?;
            crate::beliefs::list(&cfg, &req)?
        }
        ("belief", "show" | "review") => {
            id(&mut req, &p)?;
            if sub == "review" {
                crate::beliefs::review(&cfg, &req)?
            } else {
                crate::beliefs::display(&cfg, &req)?
            }
        }
        ("belief", "add" | "insert") => {
            pos(&mut req, "claim", &p, true)?;
            if req.get("subject").is_none() {
                req["subject"] = json!(format!(
                    "project:{}",
                    config::project_slug(crate::gate::cwd(&req)?)
                ))
            } else if req["subject"] == "project" {
                req["subject"] = json!(format!(
                    "project:{}",
                    config::project_slug(crate::gate::cwd(&req)?)
                ))
            }
            if req.get("confidence").is_none() {
                req["confidence"] = json!(0.8)
            }
            if auth.may_write() {
                crate::beliefs::insert(&cfg, &req, &auth)?
            } else {
                let pid = crate::gate::stage(
                    &cfg,
                    &json!({"kind":"belief","claim":req["claim"],"subject":req["subject"],"confidence":req["confidence"],"cwd":req["cwd"]}),
                    &auth,
                )?;
                json!({"status":"staged","pid":pid})
            }
        }
        ("belief", "retract") => {
            id(&mut req, &p)?;
            if req.get("reason").is_none() {
                req["reason"] = json!("manually retracted")
            }
            if auth.may_write() {
                crate::beliefs::retract(&cfg, &req, &auth)?
            } else {
                let pid = crate::gate::stage(
                    &cfg,
                    &json!({"kind":"belief","action":"retract","id":req["belief_id"],"reason":req["reason"],"cwd":req["cwd"]}),
                    &auth,
                )?;
                json!({"status":"staged","pid":pid})
            }
        }
        ("belief", "edges") => {
            id(&mut req, &p)?;
            req["browser"] = json!(false);
            crate::graph::read(&cfg, &req)?
        }
        ("evidence", _) => {
            id(&mut req, &p)?;
            crate::beliefs::evidence(&cfg, &req)?
        }
        ("graph", "stats") => {
            req["mode"] = json!("degree");
            let degree = crate::graph::query(&cfg, &req)?;
            req["mode"] = json!("components");
            let components = crate::graph::query(&cfg, &req)?;
            req["mode"] = json!("communities");
            json!({"degree":degree,"components":components,"communities":crate::graph::query(&cfg,&req)?})
        }
        ("graph", "neighbours") => {
            id(&mut req, &p)?;
            req["mode"] = json!("khop");
            if req.get("hops").is_none() {
                req["hops"] = json!(2)
            }
            crate::graph::query(&cfg, &req)?
        }
        ("graph", mode @ ("path" | "paths" | "communities" | "components" | "degree")) => {
            req["mode"] = json!(mode);
            if mode == "path" || mode == "paths" {
                for (field, position) in [("src", 0), ("dst", 1)] {
                    if req.get(field).is_none() {
                        req[field] = json!(p
                            .get(position)
                            .ok_or(Error::InvalidRequest)?
                            .parse::<i64>()
                            .map_err(|_| Error::InvalidRequest)?)
                    }
                }
            }
            crate::graph::query(&cfg, &req)?
        }
        ("graph", "html") => {
            id(&mut req, &p)?;
            req["browser"] = json!(true);
            crate::graph::read(&cfg, &req)?
        }
        ("graph", "context") => crate::context::graph_context_op(&cfg, &req)?,
        ("graph", "backfill") => crate::graph::backfill(&cfg, &req, &auth)?,
        ("graph", "edge") => crate::graph::edge_insert(&cfg, &req, &auth)?,
        ("search", _) => {
            pos(&mut req, "query", &p, true)?;
            crate::index::search(&cfg, &req)?
        }
        ("session", _) => {
            pos(&mut req, "session_id", &p, false)?;
            crate::index::session(&cfg, &req)?
        }
        ("index", _) => {
            if let Some(path) = req["live"].as_str() {
                crate::standalone_ops::live_index(&cfg, path)?
            } else {
                crate::index::index(&cfg, &req)?
            }
        }
        ("history", "recent" | "") => crate::history::recent(&cfg, &req)?,
        ("history", "prefix") => {
            pos(&mut req, "prefix", &p, false)?;
            crate::history::prefix(&cfg, &req)?
        }
        ("history", "metadata") => crate::history::metadata(&cfg, &req)?,
        ("pending", "" | "list") => crate::pending::list(&cfg, &req)?,
        ("pending", "show") => {
            pos(&mut req, "pid", &p, false)?;
            crate::pending::review(&cfg, &req)?
        }
        (decision @ ("approve" | "reject"), _) => {
            pos(&mut req, "pid", &p, false)?;
            req["decision"] = json!(decision);
            req["op"] = json!("resolve_reviewed_v1");
            core.execute(&req)?
        }
        ("consult" | "ask", _) => {
            pos(&mut req, "query", &p, true)?;
            crate::beliefs::consult(&cfg, &req)?
        }
        ("outcome", _) => {
            id(&mut req, &p)?;
            if req.get("source").is_none() {
                req["source"] = json!("user")
            }
            crate::beliefs::outcome(&cfg, &req, &auth)?
        }
        ("skills", "list" | "") => {
            json!({"installed":crate::skills::installed(&cfg)?,"learned":crate::skills::learned(&cfg)?})
        }
        ("skills", "candidates") => {
            pos(&mut req, "prompt", &p, true)?;
            crate::skills::candidates_op(&cfg, &req)?
        }
        ("skills", "usage") => crate::skills::load_usage(&cfg)?,
        ("review", _) => {
            own_process_group()?;
            crate::standalone_ops::review(&cfg, &req)?
        }
        ("backfill", _) => {
            own_process_group()?;
            crate::standalone_ops::backfill(&cfg, &req)?
        }
        ("dream", _) => dream(&cfg, &req)?,
        ("status", _) => status(&cfg, &req)?,
        ("stats", _) => crate::standalone_ops::calibration(&cfg)?,
        ("audit", _) => crate::standalone_ops::audit(&cfg, &req, &auth)?,
        ("crosscheck", _) => crate::standalone_ops::duplicates(&cfg, &req, true)?,
        ("belief", "dedup-report") => crate::standalone_ops::duplicates(&cfg, &req, false)?,
        ("graph", "derive") => {
            own_process_group()?;
            crate::standalone_ops::derive_graph(&cfg, &req)?
        }
        ("project", "move") => crate::standalone_ops::relocate(&cfg, &req, &p, &auth)?,
        ("provenance", _) => crate::standalone_ops::provenance(&cfg, &req)?,
        ("reset", _) => crate::standalone_ops::reset(&cfg, &req, &auth)?,
        ("motd", _) => crate::standalone_ops::motd(&cfg, &req)?,
        ("statusline", _) => crate::standalone_ops::statusline(&cfg)?,
        ("teardown", _) => crate::standalone_ops::teardown(&cfg, &req)?,
        ("doctor", _) => doctor(&cfg)?,
        ("setup", _) => {
            crate::files::private_dir(&cfg.root)?;
            let conn = crate::store::connect(&cfg)?;
            crate::store::record_project(&conn, crate::gate::cwd(&req)?)?;
            json!({"root":cfg.root,"native":true,"schema":true})
        }
        ("config", _) => configuration(&cfg, sub, &req, &p)?,
        ("sync", _) => sync(&cfg, sub, &req, &p)?,
        ("request", _) => core.execute(&req)?,
        _ => return Err(Error::InvalidRequest),
    };
    let failed = value["error"].is_string()
        || value["failed"].as_u64().unwrap_or(0) > 0
        || value["may_have_applied"].as_u64().unwrap_or(0) > 0;
    output(value)?;
    if failed {
        return Err(Error::MayHaveApplied);
    }
    Ok(())
}
fn own_process_group() -> Result<()> {
    #[cfg(unix)]
    {
        if unsafe { libc::getpgrp() } != unsafe { libc::getpid() }
            && unsafe { libc::setpgid(0, 0) } != 0
        {
            return Err(Error::Untrusted);
        }
        Ok(())
    }
    #[cfg(not(unix))]
    {
        Err(Error::Unsupported)
    }
}
fn dream(cfg: &Config, req: &Value) -> Result<Value> {
    let authority = Authority::Derived {
        agent: "lore-dream".into(),
        engine: req["engine"].as_str().unwrap_or("claude").into(),
    };
    let Some(job) = crate::dream::build(cfg, crate::gate::cwd(req)?, &authority)? else {
        return Ok(json!({"skipped":true}));
    };
    if req["dry_run"] == true {
        return Ok(json!(job.prompt()));
    }
    let output = if let Some(path) = req["result_file"].as_str() {
        String::from_utf8(crate::files::read_regular(
            Path::new(path),
            crate::MAX_FRAME_BYTES,
        )?)
        .map_err(|_| Error::InvalidRequest)?
    } else {
        own_process_group()?;
        let program = std::env::var("LORE_CLAUDE_BIN").unwrap_or_else(|_| "claude".into());
        crate::worker::review_provider(
            &program,
            "LORE_DREAMER_MODEL",
            "sonnet",
            job.prompt(),
            std::time::Instant::now() + Duration::from_secs(150),
        )?
    };
    crate::dream::process(cfg, &job, &output)
}
fn status(cfg: &Config, req: &Value) -> Result<Value> {
    let conn = crate::store::connect(cfg)?;
    let mut counts = json!({});
    for (table, name) in [
        ("beliefs", "beliefs"),
        ("belief_evidence", "evidence"),
        ("sessions", "sessions"),
        ("msg", "messages"),
        ("sync_ops", "ops"),
    ] {
        let n = conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| {
            r.get::<_, i64>(0)
        })?;
        counts[name] = json!(n)
    }
    Ok(
        json!({"root":cfg.root,"version":env!("CARGO_PKG_VERSION"),"counts":counts,"memory":crate::memory::usage(cfg,req)?,"pending":crate::pending::ids(cfg)?.len(),"sync":crate::sync::state(cfg)?}),
    )
}
fn doctor(cfg: &Config) -> Result<Value> {
    let mut checks = json!({"native":true,"root_absolute":cfg.root.is_absolute(),"sync_key_configured":cfg.sync.key.is_some(),"version":env!("CARGO_PKG_VERSION")});
    checks["store"] = json!(match crate::store::read_only(cfg) {
        Ok(conn) => conn
            .query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0))
            .unwrap_or_else(|_| "unavailable".into()),
        Err(e) => e.code().into(),
    });
    Ok(checks)
}
fn settings() -> Result<PathBuf> {
    let home = std::env::var_os("HOME").ok_or(Error::Unavailable)?;
    Ok(std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(home).join(".claude"))
        .join("settings.json"))
}
fn configuration(cfg: &Config, sub: &str, req: &Value, p: &[String]) -> Result<Value> {
    if matches!(sub, "" | "show") {
        return Ok(crate::config::runtime(cfg));
    }
    let var = req["var"]
        .as_str()
        .or_else(|| p.first().map(String::as_str))
        .filter(|s| {
            s.starts_with("LORE_")
                && s.bytes()
                    .all(|b| b.is_ascii_uppercase() || b == b'_' || b.is_ascii_digit())
        })
        .ok_or(Error::InvalidRequest)?;
    let path = settings()?;
    let _lock = crate::files::Locks::acquire(
        path.parent().ok_or(Error::UnsafePath)?,
        std::slice::from_ref(&path),
        cfg.timeout,
    )?;
    let mut settings = if path.try_exists()? {
        serde_json::from_slice::<Value>(&crate::files::read_regular(&path, crate::MAX_FRAME_BYTES)?)
            .map_err(|_| Error::InvalidRequest)?
    } else {
        json!({})
    };
    if !settings.is_object() {
        return Err(Error::InvalidRequest);
    }
    if settings.get("env").is_none() {
        settings["env"] = json!({})
    }
    let env = settings["env"]
        .as_object_mut()
        .ok_or(Error::InvalidRequest)?;
    match sub {
        "set" => {
            let value = req["value"]
                .as_str()
                .or_else(|| p.get(1).map(String::as_str))
                .ok_or(Error::InvalidRequest)?;
            if value.len() > 16384 || value.contains('\0') {
                return Err(Error::TooLarge);
            }
            env.insert(var.into(), json!(value));
        }
        "unset" => {
            env.remove(var);
        }
        _ => return Err(Error::InvalidRequest),
    }
    crate::files::atomic_write(
        &path,
        &serde_json::to_vec_pretty(&settings).map_err(|_| Error::Unavailable)?,
    )?;
    Ok(json!({"updated":var,"path":path}))
}
fn absolute(raw: &str) -> Result<PathBuf> {
    let p = PathBuf::from(raw);
    Ok(if p.is_absolute() {
        p
    } else {
        std::env::current_dir()?.join(p)
    })
}
fn sync(cfg: &Config, sub: &str, req: &Value, p: &[String]) -> Result<Value> {
    use crate::sync_network as n;
    match sub {
        "resign" => crate::sync_admin::resign(
            cfg,
            req["apply"] == true,
            req["replace_foreign_key"] == true,
        ),
        "seed" => crate::sync_admin::seed(cfg, req["apply"] == true),
        "status" => crate::sync::state(cfg),
        "bootstrap" => {
            let transport = if let Some(peer) = req["peer"].as_str() {
                n::Transport::peer(peer)?
            } else if std::env::var("LORE_SYNC_URL").is_ok_and(|s| !s.is_empty()) {
                n::Transport::hub()?
            } else {
                let peers = std::env::var("LORE_SYNC_PEER").unwrap_or_default();
                let peers = peers
                    .split(',')
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .collect::<Vec<_>>();
                if peers.len() != 1 {
                    return Err(Error::InvalidRequest);
                }
                n::Transport::peer(peers[0])?
            };
            if req["merge"] == true {
                return n::pull(cfg, &transport);
            }
            let conn = crate::store::connect(cfg)?;
            let machine = crate::store::machine_id(&conn)?;
            let beliefs =
                conn.query_row("SELECT count(*) FROM beliefs", [], |r| r.get::<_, i64>(0))?;
            let own = conn.query_row(
                "SELECT count(*) FROM sync_ops WHERE machine_id=?",
                [machine],
                |r| r.get::<_, i64>(0),
            )?;
            if beliefs > 0
                || own > 0
                || !crate::pending::ids(cfg)?.is_empty()
                || !crate::memory::read_entries(&cfg.root.join("USER.md"))?.is_empty()
            {
                return Err(Error::Changed);
            }
            let projects = cfg.root.join("projects");
            if projects.try_exists()? {
                for name in crate::files::directory_names(&projects, 10000)? {
                    if !crate::memory::read_entries(&projects.join(name).join("MEMORY.md"))?
                        .is_empty()
                    {
                        return Err(Error::Changed);
                    }
                }
            }
            n::pull_from(cfg, &transport, Some(0), false)
        }
        "classes" => {
            let mut classes = cfg.sync.classes.iter().collect::<Vec<_>>();
            classes.sort();
            Ok(json!({"classes":classes,"enabled":cfg.sync.enabled}))
        }
        "login" => {
            let token = p.first().ok_or(Error::InvalidRequest)?;
            configuration(
                cfg,
                "set",
                &json!({"var":"LORE_SYNC_TOKEN","value":token}),
                &[],
            )
        }
        "health" | "whoami" => {
            let t = if let Some(peer) = req["peer"].as_str() {
                n::Transport::peer(peer)?
            } else {
                n::Transport::hub()?
            };
            t.request("GET", sub, &[], None).map_err(|e| {
                eprintln!("{e}");
                Error::Unavailable
            })
        }
        "push" => n::push(cfg, &n::Transport::hub()?, req["from_seq"].as_i64()),
        "pull" | "" => {
            let mut targets = Vec::new();
            if let Some(peer) = req["peer"].as_str() {
                targets.push(n::Transport::peer(peer)?)
            } else {
                if std::env::var("LORE_SYNC_URL").is_ok_and(|s| !s.is_empty()) {
                    targets.push(n::Transport::hub()?)
                }
                for peer in std::env::var("LORE_SYNC_PEER")
                    .unwrap_or_default()
                    .split(',')
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                {
                    targets.push(n::Transport::peer(peer)?);
                }
            }
            if targets.is_empty() {
                return Err(Error::InvalidRequest);
            }
            let mut reports = json!({});
            for target in targets {
                reports[&target.peer] = n::pull(cfg, &target)?;
            }
            if sub.is_empty() && std::env::var("LORE_SYNC_URL").is_ok_and(|s| !s.is_empty()) {
                reports["push"] = n::push(cfg, &n::Transport::hub()?, None)?
            }
            Ok(reports)
        }
        "export" | "import" => {
            let path = absolute(
                req["path"]
                    .as_str()
                    .or_else(|| p.first().map(String::as_str))
                    .ok_or(Error::InvalidRequest)?,
            )?;
            if sub == "export" {
                n::export_bundle(cfg, &path)
            } else {
                n::import_bundle(cfg, &path)
            }
        }
        "serve" => {
            let bind = req["bind"].as_str().unwrap_or("127.0.0.1");
            let port = req["port"].as_u64().unwrap_or(8765);
            if port > 65535 {
                return Err(Error::InvalidRequest);
            }
            let listener = TcpListener::bind((bind, port as u16))?;
            let address = listener.local_addr()?;
            let server = n::PeerServer::new(cfg.clone(), address.ip().is_loopback())?;
            eprintln!(
                "lore peer listening {address} auth={} secret={}",
                server.auth,
                server.secret.is_some()
            );
            server.serve(listener)?;
            Ok(json!({"stopped":true}))
        }
        _ => Err(Error::Unsupported),
    }
}
/// MCP stdio uses the same catalog, identity and model proposal gate as DOXA.
pub fn mcp(cfg: &Config, args: &[String]) -> Result<()> {
    let (req, p) = parse(args)?;
    if !p.is_empty() {
        return Err(Error::InvalidRequest);
    }
    let engine = req["engine"].as_str().unwrap_or("claude");
    let session = req["session_id"]
        .as_str()
        .map(str::to_owned)
        .or_else(|| std::env::var("CODEX_THREAD_ID").ok())
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let identity = json!({"cwd":req["cwd"],"session_id":session,"source_engine":engine,"spawn_depth":req["spawn_depth"].as_u64().unwrap_or(0),"lore":true});
    let mut tools = crate::agents::AgentOperators::default();
    tools.execute(cfg, &json!({"op":"agent_catalog_v1","identity":identity}))?;
    let mut input = io::stdin().lock();
    let mut stdout = io::stdout().lock();
    loop {
        let mut raw = Vec::new();
        let len = input.by_ref().take(65537).read_until(b'\n', &mut raw)?;
        if len == 0 {
            return Ok(());
        }
        if len > 65536 || !raw.ends_with(b"\n") {
            return Err(Error::TooLarge);
        }
        let request: Value = serde_json::from_slice(&raw).map_err(|_| Error::InvalidRequest)?;
        if request["jsonrpc"] != "2.0" || !request.is_object() {
            return Err(Error::InvalidRequest);
        }
        if request.get("id").is_none() {
            continue;
        }
        let id = request["id"].clone();
        if !id.is_string() && !id.is_number() && !id.is_null() {
            return Err(Error::InvalidRequest);
        }
        let result = match request["method"].as_str() {
            Some("initialize") => Ok(
                json!({"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"lore","version":env!("CARGO_PKG_VERSION")}}),
            ),
            Some("ping") => Ok(json!({})),
            Some("tools/list") => tools
                .execute(cfg, &json!({"op":"agent_catalog_v1","identity":identity}))
                .map(|catalog| json!({"tools":catalog})),
            Some("tools/call") => {
                let value=tools.execute(cfg,&json!({"op":"agent_tool_v1","identity":identity,"name":request["params"]["name"],"arguments":request["params"]["arguments"]})).unwrap_or_else(|e|json!({"error":e.code()}));
                let is_error = value["error"].is_string();
                Ok(
                    json!({"content":[{"type":"text","text":serde_json::to_string(&value).map_err(|_|Error::Unavailable)?}],"isError":is_error}),
                )
            }
            _ => Err(Error::Unsupported),
        };
        let reply = match result {
            Ok(value) => json!({"jsonrpc":"2.0","id":id,"result":value}),
            Err(e) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":e.code()}}),
        };
        let mut bytes = serde_json::to_vec(&reply).map_err(|_| Error::Unavailable)?;
        if bytes.len() > 65536 {
            return Err(Error::TooLarge);
        }
        bytes.push(b'\n');
        stdout.write_all(&bytes)?;
        stdout.flush()?;
    }
}

/// Provider hook adapter: bounded untrusted event data, no provider work on
/// injection or prompt paths. Review admits only canonical owned transcripts.
pub fn hook(cfg: &Config, args: &[String]) -> Result<()> {
    let (options, positional) = parse(args)?;
    if !positional.is_empty() {
        return Err(Error::InvalidRequest);
    }
    let engine = options["engine"]
        .as_str()
        .filter(|s| matches!(*s, "claude" | "codex"))
        .ok_or(Error::InvalidRequest)?;
    let event = options["event"]
        .as_str()
        .filter(|s| {
            matches!(
                *s,
                "session-start" | "prompt" | "pre-compact" | "session-end"
            )
        })
        .ok_or(Error::InvalidRequest)?;
    let mut raw = Vec::new();
    io::stdin()
        .lock()
        .take(crate::MAX_FRAME_BYTES as u64 + 1)
        .read_to_end(&mut raw)?;
    if raw.len() > crate::MAX_FRAME_BYTES {
        return Err(Error::TooLarge);
    }
    let input: Value = serde_json::from_slice(&raw).map_err(|_| Error::InvalidRequest)?;
    if !input.is_object() {
        return Err(Error::InvalidRequest);
    }
    if std::env::var("LORE_SKIP").is_ok_and(|s| !s.is_empty()) {
        return Ok(());
    }
    let cwd = input["cwd"]
        .as_str()
        .or_else(|| options["cwd"].as_str())
        .ok_or(Error::InvalidRequest)?;
    if !Path::new(cwd).is_absolute() || cwd.len() > 4096 {
        return Err(Error::InvalidRequest);
    }
    let session = input["session_id"]
        .as_str()
        .or_else(|| input["thread_id"].as_str())
        .or_else(|| options["session_id"].as_str());
    let mut req = json!({"cwd":cwd,"prompt":input["prompt"].as_str().unwrap_or("")});
    if let Some(session) = session {
        if !config::valid_id(session) {
            return Err(Error::InvalidRequest);
        }
        req["session_id"] = json!(session);
    }
    match event {
        "session-start" => {
            if std::env::var("LORE_DISABLE_INJECT").is_ok_and(|s| !matches!(s.as_str(), "" | "0")) {
                return Ok(());
            }
            let snapshot = crate::context::snapshot(cfg, &req)?;
            output(
                json!({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":snapshot}}),
            )
        }
        "prompt" => {
            if engine == "claude"
                && ["LORE_LIVE_INDEX", "LORE_STREAM_INDEX"]
                    .iter()
                    .any(|name| std::env::var(name).is_ok_and(|s| s == "1"))
            {
                let _ = crate::index::live(cfg, &req);
            }
            let frame = crate::context::refresh(cfg, &req)?;
            if frame.is_null() {
                Ok(())
            } else {
                output(frame)
            }
        }
        "pre-compact" | "session-end" => {
            let sid = session.ok_or(Error::InvalidRequest)?;
            let transcript = input["transcript_path"]
                .as_str()
                .or_else(|| input["transcript"].as_str())
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    cfg.projects
                        .join(config::project_slug(Path::new(cwd)))
                        .join(format!("{sid}.jsonl"))
                });
            req["transcript"] = json!(transcript);
            own_process_group()?;
            crate::worker::run(cfg, &req, engine)
        }
        _ => Err(Error::InvalidRequest),
    }
}
