//! Reviewer jobs freeze caller identity and exact descriptor bytes. Model
//! output can propose data; it cannot select its authority or source session.
use crate::{
    config::{self, Config},
    gate::{self, Authority},
    index::{self, Message},
    memory::{self, Scope},
    pending, skills, Error, Result,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
#[cfg(unix)]
use std::os::{
    fd::{AsRawFd, FromRawFd},
    unix::{
        ffi::OsStrExt,
        fs::{FileExt, MetadataExt},
    },
};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File},
    path::{Path, PathBuf},
};
include!("review_prompt.rs");
const SOURCE_CAP: usize = 256 * 1024 * 1024;
const LINE_CAP: usize = 8 * 1024 * 1024;
fn crop(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}
use crate::config::disabled;
fn bound_env(name: &str, default: usize, max: usize) -> Result<usize> {
    std::env::var(name)
        .ok()
        .map_or(Some(default), |value| value.trim().parse().ok())
        .filter(|value| *value <= max)
        .ok_or(Error::InvalidRequest)
}
fn require_derived(authority: &Authority) -> Result<()> {
    if matches!(authority, Authority::Derived { .. }) {
        Ok(())
    } else {
        Err(Error::Untrusted)
    }
}
fn threshold() -> Result<f64> {
    std::env::var("LORE_DUP_CONTAINMENT")
        .ok()
        .map_or(Some(0.60), |value| value.parse::<f64>().ok())
        .filter(|value| value.is_finite() && (0.0..=1.0).contains(value))
        .ok_or(Error::InvalidRequest)
}
fn scalar(value: &Value) -> String {
    value.as_str().map(str::to_owned).unwrap_or_else(|| {
        if value.is_null() {
            String::new()
        } else {
            value.to_string()
        }
    })
}
fn safe_line(value: &Value, cap: usize) -> Result<String> {
    Ok(crop(
        &gate::one_line(&crate::scrub::scrub(&scalar(value))?),
        cap,
    ))
}
#[derive(Debug)]
struct SourceProof {
    path: PathBuf,
    hash: String,
    inode: u64,
    device: u64,
    size: u64,
    ctime: i64,
    ctime_nsec: i64,
}
#[derive(Debug)]
pub struct ReviewJob {
    prompt: String,
    project: String,
    session_id: String,
    cwd: PathBuf,
    authority: Authority,
    source: SourceProof,
}
impl ReviewJob {
    pub fn prompt(&self) -> &str {
        &self.prompt
    }
    pub fn session_id(&self) -> &str {
        &self.session_id
    }
    pub fn to_value(&self) -> Value {
        json!({"prompt":self.prompt,"project":self.project,"session_id":self.session_id,"cwd":self.cwd,"agent":self.authority.agent(),"source_engine":self.authority.engine(),"source":{"path":self.source.path,"sha256":self.source.hash,"inode":self.source.inode,"device":self.source.device,"size":self.source.size,"ctime":self.source.ctime,"ctime_nsec":self.source.ctime_nsec}})
    }
    pub fn validate_source(&self) -> Result<()> {
        let (file, proof) = open_source(&self.source.path)?;
        let _ = file;
        if proof.hash != self.source.hash
            || proof.inode != self.source.inode
            || proof.device != self.source.device
            || proof.size != self.source.size
            || proof.ctime != self.source.ctime
            || proof.ctime_nsec != self.source.ctime_nsec
        {
            return Err(Error::Changed);
        }
        Ok(())
    }
}
fn open_source(path: &Path) -> Result<(File, SourceProof)> {
    if !path.is_absolute() {
        return Err(Error::UnsafePath);
    }
    #[cfg(not(unix))]
    return Err(Error::Unsupported);
    #[cfg(unix)]
    {
        let mut parent = File::open("/")?;
        let names = path
            .components()
            .filter_map(|component| match component {
                std::path::Component::RootDir | std::path::Component::CurDir => None,
                std::path::Component::Normal(name) => Some(Ok(name)),
                _ => Some(Err(Error::UnsafePath)),
            })
            .collect::<Result<Vec<_>>>()?;
        for (index, name) in names.iter().enumerate() {
            let name = std::ffi::CString::new(name.as_bytes()).map_err(|_| Error::UnsafePath)?;
            let flags = libc::O_RDONLY
                | libc::O_NOFOLLOW
                | libc::O_CLOEXEC
                | if index + 1 == names.len() {
                    libc::O_NONBLOCK
                } else {
                    libc::O_DIRECTORY
                };
            let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
            if fd < 0 {
                return Err(Error::UnsafePath);
            }
            parent = unsafe { File::from_raw_fd(fd) };
        }
        let metadata = parent.metadata()?;
        if !metadata.is_file()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.nlink() != 1
        {
            return Err(Error::UnsafePath);
        }
        if metadata.len() > SOURCE_CAP as u64 {
            return Err(Error::TooLarge);
        }
        let mut hash = sha2::Sha256::new();
        let mut offset = 0;
        let mut buffer = [0u8; 65536];
        use sha2::Digest;
        while offset < metadata.len() {
            let count = (metadata.len() - offset).min(65536) as usize;
            let n = parent.read_at(&mut buffer[..count], offset)?;
            if n == 0 {
                return Err(Error::Changed);
            }
            hash.update(&buffer[..n]);
            offset += n as u64;
        }
        let after = parent.metadata()?;
        if after.ino() != metadata.ino()
            || after.len() != metadata.len()
            || after.ctime() != metadata.ctime()
            || after.ctime_nsec() != metadata.ctime_nsec()
        {
            return Err(Error::Changed);
        }
        Ok((
            parent,
            SourceProof {
                path: path.into(),
                hash: format!("{:x}", hash.finalize()),
                inode: metadata.ino(),
                device: metadata.dev(),
                size: metadata.len(),
                ctime: metadata.ctime(),
                ctime_nsec: metadata.ctime_nsec(),
            },
        ))
    }
}
/// Complete reviewer tool channels; scrub raw inputs before 280/4000-char
/// cuts so credential fragments never survive a truncation boundary.
pub fn parse_review_fd(file: &File, size: u64, codex: bool) -> Result<Vec<Message>> {
    #[cfg(not(unix))]
    {
        let _ = (file, size, codex);
        return Err(Error::Unsupported);
    }
    #[cfg(unix)]
    {
        if size > SOURCE_CAP as u64 {
            return Err(Error::TooLarge);
        }
        let mut rows = Vec::new();
        let mut offset = 0;
        let mut line = Vec::new();
        let mut buffer = [0u8; 65536];
        let mut record = |bytes: &[u8]| -> Result<()> {
            let text = std::str::from_utf8(bytes).map_err(|_| Error::InvalidRequest)?;
            let Ok(value) = serde_json::from_str::<Value>(text) else {
                return if codex && !text.trim().is_empty() {
                    Err(Error::InvalidRequest)
                } else {
                    Ok(())
                };
            };
            if codex && !value.is_object() {
                return Err(Error::InvalidRequest);
            }
            let timestamp = value["timestamp"].as_str().unwrap_or("");
            let ts = crate::scrub::scrub(timestamp)?;
            if codex {
                let payload = &value["payload"];
                if value["type"] != "response_item" {
                    return Ok(());
                }
                match payload["type"].as_str() {
                    Some("message") => {
                        let Some(role) = payload["role"]
                            .as_str()
                            .filter(|role| matches!(*role, "user" | "assistant"))
                        else {
                            return Ok(());
                        };
                        let content = payload["content"].as_array().ok_or(Error::InvalidRequest)?;
                        let text = content
                            .iter()
                            .filter(|block| {
                                matches!(block["type"].as_str(), Some("input_text" | "output_text"))
                            })
                            .map(|block| block["text"].as_str().ok_or(Error::InvalidRequest))
                            .collect::<Result<Vec<_>>>()?
                            .join(" ");
                        if !text.trim().is_empty() {
                            rows.push(Message {
                                ts,
                                role: role.into(),
                                content: crop(&crate::scrub::scrub(text.trim())?, 4000),
                            });
                        }
                    }
                    Some(
                        kind @ ("function_call" | "custom_tool_call" | "local_shell_call"
                        | "tool_search_call" | "web_search_call"),
                    ) => {
                        let name = payload
                            .get("name")
                            .filter(|name| !name.is_null())
                            .map_or(Ok(kind), |name| name.as_str().ok_or(Error::InvalidRequest))?;
                        let raw = payload
                            .get("arguments")
                            .or_else(|| payload.get("input"))
                            .or_else(|| payload.get("action"))
                            .unwrap_or(&Value::Null);
                        let input = if kind == "function_call" {
                            let text = raw.as_str().ok_or(Error::InvalidRequest)?;
                            serde_json::from_str::<Value>(text)
                                .unwrap_or_else(|_| json!({"raw":text}))
                        } else {
                            raw.clone()
                        };
                        let detail = match name {
                            "Bash" => scalar(&input["command"]),
                            "Edit" | "Write" | "Read" | "NotebookEdit" => {
                                scalar(&input["file_path"])
                            }
                            "Skill" => scalar(
                                input
                                    .get("skill")
                                    .filter(|value| !value.is_null() && value.as_str() != Some(""))
                                    .unwrap_or(&input["name"]),
                            ),
                            _ => {
                                serde_json::to_string(&input).map_err(|_| Error::InvalidRequest)?
                            }
                        };
                        // Scrub complete arguments before canonical generic 160
                        // and tool-line 280 character cuts.
                        let detail = crate::scrub::scrub(&detail)?;
                        let detail = if matches!(
                            name,
                            "Bash" | "Edit" | "Write" | "Read" | "NotebookEdit" | "Skill"
                        ) {
                            detail
                        } else {
                            crop(&detail, 160)
                        };
                        let name = gate::one_line(&crate::scrub::scrub(name)?);
                        rows.push(Message {
                            ts,
                            role: "tool".into(),
                            content: format!("{name}: {}", crop(&gate::one_line(&detail), 280)),
                        });
                    }
                    Some(
                        "function_call_output" | "custom_tool_call_output" | "tool_search_output",
                    ) => {
                        // Only provider-declared errors become canonical E
                        // rows. Ordinary outputs do not inflate user turns.
                        if payload["is_error"] == true {
                            let raw = payload
                                .get("output")
                                .or_else(|| payload.get("tools"))
                                .unwrap_or(&Value::Null);
                            let text = if let Some(text) = raw.as_str() {
                                text.to_owned()
                            } else if let Some(blocks) = raw.as_array() {
                                blocks
                                    .iter()
                                    .filter(|part| {
                                        matches!(
                                            part["type"].as_str(),
                                            Some("text" | "input_text" | "output_text")
                                        )
                                    })
                                    .map(|part| part["text"].as_str().ok_or(Error::InvalidRequest))
                                    .collect::<Result<Vec<_>>>()?
                                    .join(" ")
                            } else {
                                return Err(Error::InvalidRequest);
                            };
                            if !text.trim().is_empty() {
                                rows.push(Message {
                                    ts,
                                    role: "toolerr".into(),
                                    content: crop(
                                        &gate::one_line(&crate::scrub::scrub(&text)?),
                                        280,
                                    ),
                                });
                            }
                        }
                    }
                    _ => {}
                }
            } else {
                let Some(role) = value["type"]
                    .as_str()
                    .filter(|role| matches!(*role, "user" | "assistant"))
                else {
                    return Ok(());
                };
                if value["isMeta"] == true {
                    return Ok(());
                }
                let content = &value["message"]["content"];
                let text = index::extract_text(content);
                if !text.is_empty() {
                    rows.push(Message {
                        ts: ts.clone(),
                        role: role.into(),
                        content: crop(&crate::scrub::scrub(&text)?, 4000),
                    });
                }
                for block in content.as_array().into_iter().flatten() {
                    if role == "assistant" && block["type"] == "tool_use" {
                        let name = block["name"].as_str().unwrap_or("?");
                        let input = &block["input"];
                        let detail = match name {
                            "Bash" => scalar(&input["command"]),
                            "Edit" | "Write" | "Read" | "NotebookEdit" => {
                                scalar(&input["file_path"])
                            }
                            "Skill" => scalar(
                                input
                                    .get("skill")
                                    .filter(|value| !value.is_null() && value.as_str() != Some(""))
                                    .unwrap_or(&input["name"]),
                            ),
                            _ => crop(
                                &serde_json::to_string(input).map_err(|_| Error::InvalidRequest)?,
                                160,
                            ),
                        };
                        let detail = crop(&gate::one_line(&crate::scrub::scrub(&detail)?), 280);
                        let name = gate::one_line(&crate::scrub::scrub(name)?);
                        rows.push(Message {
                            ts: ts.clone(),
                            role: "tool".into(),
                            content: format!("{name}: {detail}"),
                        });
                    }
                    if role == "user" && block["type"] == "tool_result" && block["is_error"] == true
                    {
                        let inner = &block["content"];
                        let text = if let Some(text) = inner.as_str() {
                            text.to_owned()
                        } else {
                            inner
                                .as_array()
                                .into_iter()
                                .flatten()
                                .filter(|part| part["type"] == "text")
                                .filter_map(|part| part["text"].as_str())
                                .collect::<Vec<_>>()
                                .join(" ")
                        };
                        if !text.trim().is_empty() {
                            rows.push(Message {
                                ts: ts.clone(),
                                role: "toolerr".into(),
                                content: crop(&gate::one_line(&crate::scrub::scrub(&text)?), 280),
                            });
                        }
                    }
                }
            }
            if rows.len() > 20000 {
                return Err(Error::TooLarge);
            }
            Ok(())
        };
        while offset < size {
            let n = file.read_at(&mut buffer[..(size - offset).min(65536) as usize], offset)?;
            if n == 0 {
                return Err(Error::Changed);
            }
            offset += n as u64;
            for byte in &buffer[..n] {
                line.push(*byte);
                if line.len() > LINE_CAP {
                    return Err(Error::TooLarge);
                }
                if *byte == b'\n' {
                    record(&line)?;
                    line.clear();
                }
            }
        }
        if !line.is_empty() {
            record(&line)?;
        }
        Ok(rows)
    }
}
pub fn build_digest(messages: &[Message]) -> Result<String> {
    let last = bound_env("LORE_DIGEST_LAST_N", 500, 20000)?;
    let cap = bound_env("LORE_DIGEST_TOTAL_CAP", 250000, 1000000)?;
    let mut lines = Vec::new();
    for message in messages.iter().skip(messages.len().saturating_sub(last)) {
        let tag = match message.role.as_str() {
            "user" => "U",
            "assistant" => "A",
            "tool" => "T",
            "toolerr" => "E",
            _ => "?",
        };
        lines.push(format!(
            "{tag}: {}",
            crop(
                &gate::one_line(&crate::scrub::scrub(&message.content)?),
                700
            )
        ));
    }
    let text = lines.join("\n");
    Ok(text
        .chars()
        .skip(text.chars().count().saturating_sub(cap))
        .collect())
}
pub fn pending_items(cfg: &Config, slug: &str) -> Result<Vec<Value>> {
    let mut rows = Vec::new();
    for id in pending::ids(cfg)? {
        let path = cfg.root.join("pending").join(format!("{id}.json"));
        let Ok(snapshot) = pending::snapshot(&path) else {
            continue;
        };
        if snapshot.item["scope"] == "project" && snapshot.item["project"] != slug {
            continue;
        }
        rows.push(snapshot.item);
    }
    Ok(rows)
}
fn resolve_project(cfg: &Config, raw: &Value, slug: &str) -> Result<(String, Value)> {
    let raw = scalar(raw).trim().to_owned();
    if raw.is_empty() {
        return Ok((slug.into(), json!({})));
    }
    let mut known = BTreeSet::new();
    for root in [cfg.root.join("projects"), cfg.projects.clone()] {
        if !root.try_exists()? {
            continue;
        }
        for (index, entry) in fs::read_dir(root)?.take(4097).enumerate() {
            if index >= 4096 {
                return Err(Error::TooLarge);
            }
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                if let Some(name) = entry
                    .file_name()
                    .to_str()
                    .filter(|name| config::valid_slug(name))
                {
                    known.insert(name.to_owned());
                }
            }
        }
    }
    let looks_path =
        raw.contains('/') || matches!(raw.as_str(), "." | "..") || raw.starts_with('~');
    let target = if looks_path {
        let expanded = if let Some(rest) = raw.strip_prefix("~/") {
            std::env::var_os("HOME")
                .map(PathBuf::from)
                .map(|home| home.join(rest))
        } else {
            Some(PathBuf::from(&raw))
        };
        expanded
            .filter(|path| path.is_dir())
            .map(|path| config::project_slug(&path))
    } else if known.contains(&raw) {
        Some(raw.clone())
    } else {
        let suffix = known
            .iter()
            .filter(|name| name.ends_with(&raw))
            .collect::<Vec<_>>();
        if suffix.len() == 1 {
            Some(suffix[0].clone())
        } else {
            let matches = known
                .iter()
                .filter(|name| name.contains(&raw))
                .collect::<Vec<_>>();
            if matches.len() == 1 {
                Some(matches[0].clone())
            } else {
                None
            }
        }
    };
    Ok(match target {
        Some(target) if target != slug => (target, json!({"origin_project":slug})),
        Some(target) => (target, json!({})),
        None => (
            slug.into(),
            json!({"subject_unresolved":crop(&crate::scrub::scrub(&raw)?,4096)}),
        ),
    })
}
fn template(memcap: usize, skills_on: bool, beliefs_on: bool) -> String {
    let mut parts = vec![_REVIEW_INTRO
        .replace("{memcap}", &memcap.to_string())
        .replace(
            "{quota}",
            if skills_on {
                " and at most 1 reusable skill"
            } else {
                ""
            },
        )];
    if skills_on {
        parts.push(_REVIEW_SKILLS_SIGNAL.into());
    }
    parts.extend([_REVIEW_MEMORY_RULES.into(), _REVIEW_FILEMAP.into()]);
    if skills_on {
        parts.push(_REVIEW_SKILLS_RECIPE.into());
    }
    if beliefs_on {
        parts.extend([_REVIEW_CONCLUSIONS.into(), _REVIEW_RELATES.into()]);
    }
    parts.push(_REVIEW_CONTEXT.into());
    if skills_on {
        parts.push(_REVIEW_CONTEXT_SKILLS.into());
    }
    let mut fields = vec![_SCHEMA_MEMORY, _SCHEMA_FILEMAP];
    let mut empty = vec!["\"memory\":[]"];
    if skills_on {
        fields.push(_SCHEMA_SKILLS);
        empty.push("\"skills\":[],\"skill_outcomes\":[]");
    }
    if beliefs_on {
        fields.push(_SCHEMA_CONCLUSIONS);
        empty.push("\"conclusions\":[]");
    }
    parts.push(format!("Output ONLY minified JSON, no prose, no code fences:\n{{{{{}}}}}\nIf nothing qualifies output {{{{{}}}}}\n\nSESSION DIGEST (project {{slug}}):\n{{digest}}\n",fields.join(","),empty.join(",")));
    parts.join("")
}
fn format_template(template: &str, values: &BTreeMap<&str, String>) -> Result<String> {
    let mut result = String::new();
    let chars = template.chars().collect::<Vec<_>>();
    let mut at = 0;
    while at < chars.len() {
        match chars[at] {
            '{' if chars.get(at + 1) == Some(&'{') => {
                result.push('{');
                at += 2;
            }
            '}' if chars.get(at + 1) == Some(&'}') => {
                result.push('}');
                at += 2;
            }
            '{' => {
                let end = (at + 1..chars.len())
                    .find(|index| chars[*index] == '}')
                    .ok_or(Error::InvalidRequest)?;
                let key = chars[at + 1..end].iter().collect::<String>();
                result.push_str(values.get(key.as_str()).ok_or(Error::InvalidRequest)?);
                at = end + 1;
            }
            other => {
                result.push(other);
                at += 1;
            }
        }
    }
    Ok(result)
}
fn neighbourhood(cfg: &Config, slug: &str, messages: &[Message]) -> Result<String> {
    let tokens = skills::overlap_tokens(&build_digest(messages)?);
    let conn = crate::store::connect(cfg)?;
    let subjects = [
        "user".to_owned(),
        format!("project:{slug}"),
        "user-model".to_owned(),
    ];
    let mut found = BTreeMap::new();
    let mut budget = crate::MAX_FRAME_BYTES;
    for theme in tokens.into_iter().take(5) {
        let expr = index::fts_expr(&theme, " OR ");
        if expr.is_empty() {
            continue;
        }
        let mut query=conn.prepare("SELECT b.id,b.claim FROM beliefs b JOIN belief_fts f ON b.id=f.belief_id WHERE belief_fts MATCH ? AND b.status='active' AND b.subject IN (?,?,?) ORDER BY bm25(belief_fts) LIMIT 6")?;
        let mut cursor = query.query(params![expr, subjects[0], subjects[1], subjects[2]])?;
        while let Some(row) = cursor.next()? {
            let id = row.get::<_, i64>(0)?;
            let claim = crate::graph::db_text(row, 1, 65536, &mut budget)?;
            found.entry(id).or_insert(claim);
        }
    }
    let mut rows = Vec::new();
    for (id, claim) in found {
        rows.push(format!("- [{id}] {}", crate::scrub::scrub(&claim)?));
    }
    Ok(rows.join("\n"))
}
/// Pin and hash owned source bytes for standalone incremental review.
pub(crate) fn snapshot_source(path: &Path) -> Result<(File, Value)> {
    let (file, source) = open_source(path)?;
    let proof = json!({"sha256":source.hash,"inode":source.inode,"device":source.device,"size":source.size,"ctime":source.ctime,"ctime_nsec":source.ctime_nsec});
    Ok((file, proof))
}
pub fn build_review_job(
    cfg: &Config,
    req: &Value,
    authority: &Authority,
) -> Result<Option<ReviewJob>> {
    require_derived(authority)?;
    if disabled("LORE_DISABLE_REVIEW")
        || std::env::var("LORE_SKIP").is_ok_and(|value| !value.is_empty())
    {
        return Ok(None);
    }
    let cwd = gate::cwd(req)?;
    let slug = config::project_slug(cwd);
    let sid = req["session_id"]
        .as_str()
        .filter(|id| config::valid_id(id))
        .ok_or(Error::InvalidRequest)?;
    let path = Path::new(
        req["transcript"]
            .as_str()
            .filter(|path| path.len() <= 4096)
            .ok_or(Error::InvalidRequest)?,
    );
    // Only a configured sessions root may admit its canonical archive sibling.
    let archive = cfg
        .codex_sessions
        .file_name()
        .filter(|name| *name == "sessions")
        .and_then(|_| cfg.codex_sessions.parent())
        .map(|root| root.join("archived_sessions"));
    let codex = path.starts_with(&cfg.codex_sessions)
        || archive.as_ref().is_some_and(|root| path.starts_with(root));
    let is_doxa = path == cfg.projects.join(&slug).join(format!("{sid}.jsonl"));
    if !is_doxa && !codex {
        return Err(Error::UnsafePath);
    }
    if codex
        && !path
            .components()
            .all(|component| !matches!(component, std::path::Component::ParentDir))
    {
        return Err(Error::UnsafePath);
    }
    let provider_thread = req
        .get("provider_thread")
        .map(|value| {
            value
                .as_str()
                .filter(|id| config::valid_id(id))
                .ok_or(Error::InvalidRequest)
        })
        .transpose()?;
    if provider_thread.is_some() && (!codex || authority.engine() != "codex") {
        return Err(Error::Untrusted);
    }
    let (file, source) = open_source(path)?;
    if source.size == 0 {
        return Err(Error::Changed);
    }
    // PreCompact carries a previously verified source proof. It may not cause
    // provider work for bytes which changed between the hook and this process.
    match req.get("expected_source") {
        Some(proof) if proof.is_object() => {
            if proof["sha256"].as_str() != Some(source.hash.as_str())
                || proof["inode"].as_u64() != Some(source.inode)
                || proof["device"].as_u64() != Some(source.device)
                || proof["size"].as_u64() != Some(source.size)
                || proof["ctime"].as_i64() != Some(source.ctime)
                || proof["ctime_nsec"].as_i64() != Some(source.ctime_nsec)
            {
                return Err(Error::Changed);
            }
        }
        None if provider_thread.is_none() => {}
        _ => return Err(Error::InvalidRequest),
    }
    let messages = parse_review_fd(&file, source.size, codex)?;
    let (meta, _) = index::parse_transcript_fd(&file, codex)?;
    if meta.internal {
        return Ok(None);
    }
    if codex
        && (meta.session_id.as_deref() != Some(provider_thread.unwrap_or(sid))
            || !meta
                .cwd
                .as_deref()
                .is_some_and(|actual| config::project_slug(Path::new(actual)) == slug))
    {
        return Err(Error::Untrusted);
    }

    if messages.iter().take(50).any(|message| {
        [
            "You are the background memory reviewer",
            "You are the belief reconciler",
        ]
        .iter()
        .any(|marker| message.content.contains(marker))
    }) {
        return Ok(None);
    }
    if messages
        .iter()
        .filter(|message| message.role == "user")
        .count()
        < bound_env("LORE_REVIEW_MIN_MESSAGES", 3, 20000)?
    {
        return Ok(None);
    }
    let span = match req.get("span") {
        None | Some(Value::Null) => None,
        Some(Value::Array(bounds)) if bounds.len() == 2 => {
            let lo = bounds[0].as_u64().ok_or(Error::InvalidRequest)? as usize;
            let hi = bounds[1].as_u64().ok_or(Error::InvalidRequest)? as usize;
            if lo > hi || hi > messages.len() {
                return Err(Error::InvalidRequest);
            }
            Some(lo..hi)
        }
        _ => return Err(Error::InvalidRequest),
    };
    if span.as_ref().is_some_and(|span| span.is_empty()) {
        return Ok(None);
    }
    let record_usage = span.is_none() && req["dry_run"] != true;
    let chosen = span.map_or(messages.as_slice(), |span| &messages[span]);
    let pending = pending_items(cfg, &slug)?;
    let pending_text = pending
        .iter()
        .filter_map(|item| {
            item["text"]
                .as_str()
                .filter(|text| !text.is_empty())
                .or_else(|| item["name"].as_str())
        })
        .map(|text| crate::scrub::scrub(text).map(|text| format!("- {text}")))
        .collect::<Result<Vec<_>>>()?
        .join("\n");
    let learned = skills::learned(cfg)?;
    let usage = skills::load_usage(cfg)?;
    let learned = learned
        .into_iter()
        .map(|(name, desc)| format!("- {name} ({}): {desc}", skills::record_line(&usage[&name])))
        .collect::<Vec<_>>()
        .join("\n");
    let render = |scope: Scope| -> Result<String> {
        let text = memory::render_entries(&memory::read_entries(&scope.path(cfg, &slug)?)?);
        Ok(if text.is_empty() {
            "(empty)".into()
        } else {
            crate::scrub::scrub(&text)?
        })
    };
    let mut values = BTreeMap::new();
    values.insert(
        "learned",
        if learned.is_empty() {
            "(none)".into()
        } else {
            learned
        },
    );
    values.insert("user_entries", render(Scope::User)?);
    values.insert("proj_entries", render(Scope::Project)?);
    values.insert(
        "pending",
        if pending_text.is_empty() {
            "(none)".into()
        } else {
            pending_text
        },
    );
    let installed = skills::installed(cfg)?.join(", ");
    values.insert(
        "skills",
        if installed.is_empty() {
            "(none)".into()
        } else {
            installed
        },
    );
    values.insert("slug", slug.clone());
    values.insert("digest", build_digest(chosen)?);
    let mut prompt = format_template(
        &template(
            bound_env("LORE_MEMORY_PROPOSAL_CAP", 3, 50)?,
            !disabled("LORE_DISABLE_SKILLS"),
            !disabled("LORE_DISABLE_BELIEFS"),
        ),
        &values,
    )?;
    let maps = crate::filemap::entries_for(cfg, &slug)?;
    if !maps.is_empty() {
        prompt.push_str("\nCurrent file map (do not re-propose these paths):\n");
        for (path, purpose) in maps {
            prompt.push_str(&format!(
                "- {} — {}\n",
                crate::scrub::scrub(&path)?,
                crate::scrub::scrub(&purpose)?
            ));
        }
    }
    if !disabled("LORE_DISABLE_BELIEFS") {
        let beliefs = neighbourhood(cfg, &slug, chosen)?;
        if !beliefs.is_empty() {
            prompt.push_str(&format!("\nExisting beliefs that may already state your conclusion below -- cite the id in \"evidence_for\" instead of restating:\n{beliefs}\n"));
        }
    }
    if req
        .get("older")
        .map_or(Some(false), Value::as_bool)
        .ok_or(Error::InvalidRequest)?
    {
        prompt.push_str(RECENCY_NOTE);
    }
    if prompt.len() > crate::MAX_FRAME_BYTES - 512 {
        return Err(Error::TooLarge);
    }
    let part = req
        .get("part")
        .filter(|part| !part.is_null())
        .map(|part| {
            part.as_str()
                .filter(|part| config::valid_id(part))
                .ok_or(Error::InvalidRequest)
        })
        .transpose()?;
    let session_id = part.map_or(sid.to_owned(), |part| format!("{sid}-{part}"));
    if session_id.len() > 128 {
        return Err(Error::InvalidRequest);
    }
    let job = ReviewJob {
        prompt,
        project: slug,
        session_id,
        cwd: cwd.into(),
        authority: authority.clone(),
        source,
    };
    job.validate_source()?;
    if record_usage {
        skills::record_usage(cfg, &messages, authority)?;
    }
    Ok(Some(job))
}
fn validate_channels(data: &Value) -> Result<()> {
    if !data.is_object() {
        return Err(Error::InvalidRequest);
    }
    for key in [
        "memory",
        "filemap",
        "skills",
        "skill_outcomes",
        "conclusions",
    ] {
        match data.get(key) {
            None | Some(Value::Null) => {}
            Some(Value::Array(rows)) if rows.len() <= 1000 => {}
            _ => return Err(Error::InvalidRequest),
        }
    }
    Ok(())
}
pub fn stage_proposals(
    cfg: &Config,
    data: &Value,
    slug: &str,
    sid: &str,
    authority: &Authority,
) -> Result<Value> {
    require_derived(authority)?;
    validate_channels(data)?;
    if !config::valid_slug(slug) || !config::valid_id(sid) || !data.is_object() {
        return Err(Error::InvalidRequest);
    }
    let threshold = threshold()?;
    let mut pending = pending_items(cfg, slug)?;
    let mut exact = BTreeSet::new();
    for item in &pending {
        if let Some(text) = item["text"]
            .as_str()
            .filter(|text| !text.is_empty())
            .or_else(|| item["name"].as_str())
        {
            exact.insert(text.to_lowercase());
        }
    }
    for (scope, key) in [
        (Scope::User, slug.to_owned()),
        (Scope::Project, slug.to_owned()),
        (Scope::Machine, memory::this_machine()),
    ] {
        exact.extend(
            memory::read_entries(&scope.path(cfg, &key)?)?
                .into_iter()
                .map(|text| text.to_lowercase()),
        );
    }
    let cap = bound_env("LORE_MEMORY_PROPOSAL_CAP", 3, 50)?;
    let proposals = data["memory"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    let mut acct = json!({"extracted":proposals.len(),"staged":0,"over_cap":proposals.len().saturating_sub(cap),"duplicate_exact":0,"already_covered":0,"malformed":0});
    let mut staged = 0usize;
    let increment = |stats: &mut Value, key: &str| {
        stats[key] = json!(stats[key].as_u64().unwrap_or(0) + 1);
    };
    let mut put = |mut item: Value| -> Result<()> {
        item["project"] = item.get("project").cloned().unwrap_or_else(|| json!(slug));
        item["session_id"] = json!(sid);
        let id = gate::stage(cfg, &item, authority)?;
        let actual = pending::snapshot(&cfg.root.join("pending").join(format!("{id}.json")))?.item;
        pending.push(actual);
        staged += 1;
        Ok(())
    };
    for proposal in proposals.iter().take(cap) {
        let scope = proposal["scope"]
            .as_str()
            .and_then(|scope| Scope::parse(scope).ok());
        let action = proposal["action"].as_str().unwrap_or("add");
        let text = safe_line(&proposal["text"], 300)?;
        let Some(scope) = scope.filter(|_| matches!(action, "add" | "replace") && !text.is_empty())
        else {
            increment(&mut acct, "malformed");
            continue;
        };
        if exact.contains(&text.to_lowercase()) {
            increment(&mut acct, "duplicate_exact");
            continue;
        }
        let (target, extra) = if scope == Scope::Project {
            resolve_project(cfg, &proposal["project"], slug)?
        } else {
            (slug.into(), json!({}))
        };
        let key = if scope == Scope::Machine {
            memory::this_machine()
        } else {
            target.clone()
        };
        let pool = memory::read_entries(&scope.path(cfg, &key)?)?;
        let needle = crate::scrub::scrub(&scalar(&proposal["match"]))?;
        let superseding = action == "replace"
            && !needle.is_empty()
            && !memory::match_entries(&pool, &needle).is_empty();
        if !superseding {
            let tokens = skills::overlap_tokens(&text);
            if pool.iter().any(|entry| {
                skills::containment(&tokens, &skills::overlap_tokens(entry)) >= threshold
            }) {
                increment(&mut acct, "already_covered");
                continue;
            }
        }
        let mut item = json!({"kind":"memory","scope":scope.name(),"action":action,"match":needle,"text":text,"project":target});
        if scope == Scope::Machine {
            item["host"] = json!(key);
        }
        for (key, value) in extra.as_object().unwrap() {
            item[key] = value.clone();
        }
        exact.insert(text.to_lowercase());
        put(item)?;
        increment(&mut acct, "staged");
    }
    // Release the capturing writer before deriving dedupe maps from its updated pending pile.
    drop(put);
    let mut mapped = crate::filemap::entries_for(cfg, slug)?
        .into_iter()
        .map(|(path, _)| path.to_lowercase())
        .collect::<BTreeSet<_>>();
    for item in &pending {
        if item["kind"] == "filemap" && item["project"] == slug {
            mapped.insert(scalar(&item["path"]).to_lowercase());
        }
    }
    for proposal in data["filemap"].as_array().into_iter().flatten().take(5) {
        let path = safe_line(&proposal["path"], 200)?;
        let purpose = safe_line(&proposal["purpose"], 200)?;
        if path.is_empty() || purpose.is_empty() || !mapped.insert(path.to_lowercase()) {
            continue;
        }
        gate::stage(
            cfg,
            &json!({"kind":"filemap","project":slug,"session_id":sid,"path":path,"purpose":purpose}),
            authority,
        )?;
        staged += 1;
    }
    if !disabled("LORE_DISABLE_SKILLS") {
        let learned = skills::learned(cfg)?;
        let usage = skills::load_usage(cfg)?;
        for proposal in data["skills"].as_array().into_iter().flatten().take(1) {
            let name = scalar(&proposal["name"]).trim().to_lowercase();
            if !config::valid_skill_name(&name) {
                continue;
            }
            let action = proposal["action"]
                .as_str()
                .filter(|action| {
                    matches!(*action, "update" | "retire") && learned.contains_key(&name)
                })
                .unwrap_or("add");
            if action != "add" && !skills::update_admitted(action, &usage[&name]) {
                continue;
            }
            let body = crate::scrub::scrub(&scalar(&proposal["body"]))?
                .trim()
                .to_owned();
            if body.is_empty() && action != "retire" {
                continue;
            }
            gate::stage(
                cfg,
                &json!({"kind":"skill","project":slug,"session_id":sid,"name":name,"action":action,"description":safe_line(&proposal["description"],300)?,"body":body}),
                authority,
            )?;
            staged += 1;
        }
    }
    acct["suppressed"] = json!(proposals
        .len()
        .saturating_sub(acct["staged"].as_u64().unwrap_or(0) as usize));
    Ok(json!({"staged":staged,"memory":acct}))
}
fn cover(conn: &Connection, subject: &str, claim: &str, threshold: f64) -> Result<Option<i64>> {
    let tokens = skills::overlap_tokens(claim);
    let mut query = conn.prepare(
        "SELECT id,claim FROM beliefs WHERE subject=? AND status='active' ORDER BY id LIMIT 10001",
    )?;
    let mut cursor = query.query([subject])?;
    let mut best = None;
    let mut score = threshold;
    let mut budget = crate::MAX_FRAME_BYTES;
    let mut count = 0;
    while let Some(row) = cursor.next()? {
        if count >= 10000 {
            return Err(Error::TooLarge);
        }
        count += 1;
        let id = row.get::<_, i64>(0)?;
        let claim = crate::graph::db_text(row, 1, 65536, &mut budget)?;
        let overlap = skills::containment(&tokens, &skills::overlap_tokens(&claim));
        if overlap >= score && (best.is_none() || overlap > score) {
            best = Some(id);
            score = overlap;
        }
    }
    Ok(best)
}
fn cited_subject(conn: &Connection, id: i64) -> Result<Option<(String, String)>> {
    let mut query = conn.prepare("SELECT subject,status FROM beliefs WHERE id=?")?;
    let mut cursor = query.query([id])?;
    let mut budget = 2048;
    let Some(row) = cursor.next()? else {
        return Ok(None);
    };
    Ok(Some((
        crate::graph::db_text(row, 0, 1024, &mut budget)?,
        crate::graph::db_text(row, 1, 32, &mut budget)?,
    )))
}
pub fn derive_conclusions(
    cfg: &Config,
    data: &Value,
    slug: &str,
    sid: &str,
    authority: &Authority,
) -> Result<Value> {
    require_derived(authority)?;
    validate_channels(data)?;
    if !config::valid_slug(slug) || !config::valid_id(sid) || !data.is_object() {
        return Err(Error::InvalidRequest);
    }
    let mut acct = json!({"extracted":0,"derived":0,"cross_subject":0,"folded":0,"retracted_cited":0,"malformed":0,"relates":0,"relates_dropped":0});
    if disabled("LORE_DISABLE_BELIEFS") {
        return Ok(acct);
    }
    let threshold = threshold()?;
    let mut conn = crate::store::connect(cfg)?;
    // Resolve cross-project subjects before starting the write transaction: resolution reads a separate connection.
    let conclusions = data["conclusions"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut resolved = Vec::new();
    for proposal in conclusions.iter().take(10) {
        resolved.push(if proposal["scope"] == "project" {
            resolve_project(cfg, &proposal["project"], slug)?.0
        } else {
            slug.to_owned()
        });
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let increment = |stats: &mut Value, key: &str| {
        stats[key] = json!(stats[key].as_u64().unwrap_or(0) + 1);
    };
    acct["extracted"] = json!(conclusions.len().min(10));
    for (proposal, target) in conclusions.iter().take(10).zip(resolved) {
        let claim = safe_line(&proposal["claim"], 300)?;
        let Some(scope) = proposal["scope"].as_str().filter(|scope| {
            matches!(*scope, "user" | "project" | "user-model") && !claim.is_empty()
        }) else {
            increment(&mut acct, "malformed");
            continue;
        };
        let subject = if scope == "project" {
            format!("project:{target}")
        } else {
            scope.into()
        };
        if scope == "user-model" && cover(&tx, "user", &claim, threshold)?.is_some() {
            increment(&mut acct, "cross_subject");
            continue;
        }
        let confidence = proposal["confidence"]
            .as_f64()
            .filter(|value| value.is_finite() && *value != 0.0)
            .unwrap_or(0.6)
            .clamp(0.0, 1.0);
        let evidence = crate::scrub::scrub(&scalar(&proposal["evidence"]))?;
        let mut fold = None;
        if let Some(id) = proposal["evidence_for"].as_i64().filter(|id| *id > 0) {
            if let Some((other, status)) = cited_subject(&tx, id)? {
                if other == subject {
                    if status == "active" {
                        fold = Some(id);
                    } else {
                        increment(&mut acct, "retracted_cited");
                    }
                }
            }
        }
        if fold.is_none() {
            fold = cover(&tx, &subject, &claim, threshold)?;
        }
        let req = json!({"subject":subject,"claim":claim,"confidence":confidence,"session_id":sid,"project":target,"note":if evidence.is_empty(){claim.as_str()}else{evidence.as_str()}});
        let bid = if let Some(id) = fold {
            crate::beliefs::reinforce_in_transaction(cfg, &tx, id, confidence, &req, authority)?;
            increment(&mut acct, "folded");
            id
        } else {
            let (id, _) = crate::beliefs::insert_in_transaction(cfg, &tx, &req, authority)?;
            increment(&mut acct, "derived");
            id
        };
        for relation in proposal["relates"].as_array().into_iter().flatten().take(2) {
            let rel = relation["rel"]
                .as_str()
                .filter(|rel| crate::graph::ASSERTED.contains(rel));
            let dst = relation["to"].as_i64().filter(|id| *id > 0);
            let Some((rel, dst)) = rel.zip(dst) else {
                increment(&mut acct, "relates_dropped");
                continue;
            };
            let active = tx
                .query_row(
                    "SELECT status='active' FROM beliefs WHERE id=?",
                    [dst],
                    |row| row.get::<_, bool>(0),
                )
                .optional()?
                .unwrap_or(false);
            if !active {
                increment(&mut acct, "relates_dropped");
                continue;
            }
            if crate::graph::edge_insert_in_transaction(
                cfg,
                &tx,
                bid,
                dst,
                rel,
                "derived",
                Some(sid),
                Some(&format!(
                    "{rel} asserted with the conclusion that became [{bid}]"
                )),
                authority,
            )? {
                increment(&mut acct, "relates");
            }
        }
    }
    tx.commit().map_err(|_| Error::MayHaveApplied)?;
    Ok(acct)
}
fn parse_model_json(text: &str) -> Result<Value> {
    let mut escaped = String::new();
    let (mut quoted, mut slash) = (false, false);
    for c in text.chars() {
        if quoted && !slash {
            match c {
                '\n' => {
                    escaped.push_str("\\n");
                    continue;
                }
                '\r' => {
                    escaped.push_str("\\r");
                    continue;
                }
                '\t' => {
                    escaped.push_str("\\t");
                    continue;
                }
                c if c.is_control() => return Err(Error::InvalidRequest),
                _ => {}
            }
        }
        escaped.push(c);
        if slash {
            slash = false;
        } else if quoted && c == '\\' {
            slash = true;
        } else if c == '"' {
            quoted = !quoted;
        }
    }
    serde_json::from_str(&escaped).map_err(|_| Error::InvalidRequest)
}
pub fn extract_json(output: &str) -> Result<Value> {
    if output.len() > crate::MAX_FRAME_BYTES {
        return Err(Error::TooLarge);
    }
    let clean = output.trim();
    if let Ok(value) = parse_model_json(clean) {
        if value.is_object() {
            return Ok(value);
        }
    }
    // Parse balanced candidate objects, respecting JSON escapes and quoted braces.
    let mut start = None;
    let mut depth = 0usize;
    let mut quoted = false;
    let mut escaped = false;
    for (index, byte) in clean.bytes().enumerate() {
        if let Some(begin) = start {
            if quoted {
                if escaped {
                    escaped = false;
                } else if byte == b'\\' {
                    escaped = true;
                } else if byte == b'"' {
                    quoted = false;
                }
                continue;
            }
            match byte {
                b'"' => quoted = true,
                b'{' => {
                    depth += 1;
                    if depth > 128 {
                        return Err(Error::TooLarge);
                    }
                }
                b'}' => {
                    depth -= 1;
                    if depth == 0 {
                        if let Ok(value) = parse_model_json(&clean[begin..=index]) {
                            if value.is_object() {
                                return Ok(value);
                            }
                        }
                        start = None;
                    }
                }
                _ => {}
            }
        } else if byte == b'{' {
            start = Some(index);
            depth = 1;
        }
    }
    Err(Error::InvalidRequest)
}
pub fn process_result(cfg: &Config, job: &ReviewJob, output: &str) -> Result<Value> {
    require_derived(&job.authority)?;
    job.validate_source()?;
    let data = extract_json(output)?;
    validate_channels(&data)?;
    let staged = stage_proposals(cfg, &data, &job.project, &job.session_id, &job.authority)
        .map_err(|_| Error::MayHaveApplied)?;
    let derived = derive_conclusions(cfg, &data, &job.project, &job.session_id, &job.authority)
        .map_err(|_| Error::MayHaveApplied)?;
    let outcomes = skills::record_outcomes(cfg, &data, &job.cwd, &job.authority)
        .map_err(|_| Error::MayHaveApplied)?;
    Ok(
        json!({"staged":staged["staged"],"memory":staged["memory"],"beliefs":derived,"skill_outcomes":outcomes}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::files;
    fn auth() -> Authority {
        Authority::Derived {
            agent: "fixture-reviewer".into(),
            engine: "codex".into(),
        }
    }
    #[test]
    fn static_segmented_prompt_matches_python_all_channel_combinations() {
        let golden: Value =
            serde_json::from_str(include_str!("review_template_golden.json")).unwrap();
        let values = BTreeMap::from([
            ("learned", "(none)".into()),
            ("user_entries", "(empty)".into()),
            ("proj_entries", "(empty)".into()),
            ("pending", "(none)".into()),
            ("skills", "(none)".into()),
            ("slug", "fixture".into()),
            ("digest", "U: fixture".into()),
        ]);
        for skills in [false, true] {
            for beliefs in [false, true] {
                let key = format!("{}{}", usize::from(skills), usize::from(beliefs));
                assert_eq!(
                    format_template(&template(3, skills, beliefs), &values).unwrap(),
                    golden[&key].as_str().unwrap()
                );
            }
        }
    }
    #[test]
    fn reviewer_parser_preserves_tool_recipe_errors_and_scrubs_before_cut() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("fixture.jsonl");
        let rows = [
            json!({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"fixture"}},{"type":"tool_use","name":"Bash","input":{"command":"echo working"}}]}}),
            json!({"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"exit code 1"}]}}),
        ];
        let bytes = rows
            .iter()
            .map(|row| format!("{row}\n"))
            .collect::<String>();
        files::atomic_write(&path, bytes.as_bytes()).unwrap();
        let (file, proof) = open_source(&path).unwrap();
        let messages = parse_review_fd(&file, proof.size, false).unwrap();
        assert_eq!(
            messages
                .iter()
                .map(|message| message.role.as_str())
                .collect::<Vec<_>>(),
            ["tool", "tool", "toolerr"]
        );
        assert_eq!(messages[0].content, "Skill: fixture");
        let digest = build_digest(&messages).unwrap();
        assert!(digest.contains("T: Bash: echo working") && digest.contains("E: exit code 1"));
    }
    #[test]
    fn changed_transcript_cannot_stage_and_model_cannot_build_reviewer_job() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let cwd = temp.path().join("repo");
        let slug = config::project_slug(&cwd);
        let path = cfg.projects.join(slug).join("fixture.jsonl");
        let rows = (0..3)
            .map(|i| {
                format!(
                    "{}\n",
                    json!({"type":"user","message":{"content":format!("fixture {i}")}})
                )
            })
            .collect::<String>();
        files::atomic_write(&path, rows.as_bytes()).unwrap();
        let req = json!({"cwd":cwd,"session_id":"fixture","transcript":path});
        let model = Authority::Model {
            agent: "m".into(),
            engine: "claude".into(),
            session_id: "s".into(),
        };
        assert!(matches!(
            build_review_job(&cfg, &req, &model),
            Err(Error::Untrusted)
        ));
        let job = build_review_job(&cfg, &req, &auth()).unwrap().unwrap();
        assert_eq!(job.to_value()["source_engine"], "codex");
        assert!(job.prompt().contains("U: fixture 2"));
        files::atomic_write(&path, b"changed source").unwrap();
        assert_eq!(
            process_result(
                &cfg,
                &job,
                "{\"memory\":[{\"scope\":\"user\",\"text\":\"new fact\"}]}"
            ),
            Err(Error::Changed)
        );
        assert!(pending::ids(&cfg).unwrap().is_empty());
    }
    #[test]
    fn codex_review_keeps_calls_errors_and_excludes_reasoning_and_binary_content() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("rollout.jsonl");
        let items = [
            json!({"type":"message","role":"user","content":[{"type":"input_text","text":"request"},{"type":"input_image","image_url":"BINARY"}]}),
            json!({"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"echo working\"}"}),
            json!({"type":"custom_tool_call","name":"apply_patch","input":"patch detail"}),
            json!({"type":"function_call_output","is_error":true,"output":[{"type":"output_text","text":"exit code 1"},{"type":"input_image","image_url":"BINARY"}]}),
            json!({"type":"function_call_output","output":"ordinary result"}),
            json!({"type":"reasoning","encrypted_content":"ENCRYPTED"}),
        ];
        let bytes = items
            .iter()
            .map(|item| format!("{}\n", json!({"type":"response_item","payload":item})))
            .collect::<String>();
        files::atomic_write(&path, bytes.as_bytes()).unwrap();
        let (file, proof) = open_source(&path).unwrap();
        let rows = parse_review_fd(&file, proof.size, true).unwrap();
        assert_eq!(
            rows.iter().map(|row| row.role.as_str()).collect::<Vec<_>>(),
            ["user", "tool", "tool", "toolerr"]
        );
        let digest = build_digest(&rows).unwrap();
        assert!(
            digest.contains("exec_command:")
                && digest.contains("echo working")
                && digest.contains("apply_patch:")
                && digest.contains("patch detail")
                && digest.contains("E: exit code 1")
        );
        for hidden in ["BINARY", "ENCRYPTED", "ordinary result"] {
            assert!(!digest.contains(hidden));
        }
    }
    #[test]
    fn codex_review_refuses_malformed_records_and_scrubs_before_generic_argument_cut() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("rollout.jsonl");
        let secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789";
        let prefix = "x".repeat(140);
        let row = json!({"type":"response_item","payload":{"type":"custom_tool_call","name":"apply_patch","input":format!("{prefix} {secret}")}});
        files::atomic_write(&path, format!("{row}\n").as_bytes()).unwrap();
        let (file, proof) = open_source(&path).unwrap();
        let rows = parse_review_fd(&file, proof.size, true).unwrap();
        assert!(!rows[0].content.contains("ghp_"));
        for invalid in ["{broken JSON\n", "[]\n"] {
            files::atomic_write(&path, invalid.as_bytes()).unwrap();
            let (file, proof) = open_source(&path).unwrap();
            assert!(matches!(
                parse_review_fd(&file, proof.size, true),
                Err(Error::InvalidRequest)
            ));
        }
    }
    #[test]
    fn codex_compact_requires_exact_original_proof_and_provider_identity_before_job() {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("lore"));
        cfg.codex_sessions = temp.path().join("codex/sessions");
        let cwd = temp.path().join("repo");
        for folder in ["sessions", "archived_sessions"] {
            let path = temp.path().join("codex").join(folder).join("rollout.jsonl");
            let mut bytes = format!(
                "{}\n",
                json!({"type":"session_meta","payload":{"id":"provider-thread","cwd":cwd}})
            );
            for i in 0..3 {
                bytes.push_str(&format!("{}\n",json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":format!("fixture {i}")}]}})));
            }
            files::atomic_write(&path, bytes.as_bytes()).unwrap();
            let (_, proof) = open_source(&path).unwrap();
            let mut req = json!({"cwd":cwd,"session_id":"doxa-session","provider_thread":"provider-thread","transcript":path,"older":true,"expected_source":{"sha256":proof.hash,"inode":proof.inode,"device":proof.device,"size":proof.size,"ctime":proof.ctime,"ctime_nsec":proof.ctime_nsec}});
            let mut bad = req.clone();
            bad["expected_source"]["sha256"] = json!("0".repeat(64));
            assert!(matches!(
                build_review_job(&cfg, &bad, &auth()),
                Err(Error::Changed)
            ));
            bad = req.clone();
            bad.as_object_mut().unwrap().remove("expected_source");
            assert!(matches!(
                build_review_job(&cfg, &bad, &auth()),
                Err(Error::InvalidRequest)
            ));
            bad = req.clone();
            bad["provider_thread"] = json!("different");
            assert!(matches!(
                build_review_job(&cfg, &bad, &auth()),
                Err(Error::Untrusted)
            ));
            bad = req.clone();
            bad["cwd"] = json!(temp.path().join("other"));
            assert!(matches!(
                build_review_job(&cfg, &bad, &auth()),
                Err(Error::Untrusted)
            ));
            let job = build_review_job(&cfg, &req, &auth()).unwrap().unwrap();
            assert_eq!(job.session_id(), "doxa-session");
            assert!(job.prompt().contains(RECENCY_NOTE));
            files::atomic_write(&path, b"changed transcript").unwrap();
            assert_eq!(
                process_result(&cfg, &job, "{\"memory\":[]}"),
                Err(Error::Changed)
            );
            assert!(matches!(
                build_review_job(&cfg, &req, &auth()),
                Err(Error::Changed)
            ));
            req["transcript"] = json!(temp.path().join("arbitrary.jsonl"));
            assert!(matches!(
                build_review_job(&cfg, &req, &auth()),
                Err(Error::UnsafePath)
            ));
        }
        assert!(pending::ids(&cfg).unwrap().is_empty());
    }
    #[test]
    fn staging_keeps_superseding_proposals_and_rejects_skill_traversal() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        memory::write_entries(
            &cfg.root.join("USER.md"),
            &["reviewer requires complete verified evidence".into()],
            1000,
        )
        .unwrap();
        let data = json!({"memory":[{"scope":"user","action":"replace","match":"verified evidence","text":"reviewer requires complete verified evidence and code"},{"scope":"user","text":"requires complete verified evidence"}],"skills":[{"name":"../escape","body":"unsafe"}]});
        let result = stage_proposals(&cfg, &data, "fixture", "session", &auth()).unwrap();
        assert_eq!(result["staged"], 1);
        assert_eq!(result["memory"]["already_covered"], 1);
        assert_eq!(pending::ids(&cfg).unwrap().len(), 1);
        assert_eq!(
            memory::read_entries(&cfg.root.join("USER.md")).unwrap(),
            ["reviewer requires complete verified evidence"]
        );
    }
    #[test]
    fn derivation_folds_only_active_same_subject_and_user_facts_override_inference() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let first = json!({"conclusions":[{"scope":"user","claim":"reviewer requires complete verified evidence","confidence":0.8}]});
        assert_eq!(
            derive_conclusions(&cfg, &first, "fixture", "s1", &auth()).unwrap()["derived"],
            1
        );
        let conn = crate::store::connect(&cfg).unwrap();
        let bid: i64 = conn
            .query_row("SELECT id FROM beliefs LIMIT 1", [], |row| row.get(0))
            .unwrap();
        drop(conn);
        let data = json!({"conclusions":[{"scope":"user-model","claim":"reviewer requires complete verified evidence"},{"scope":"user","claim":"reviewer demands complete verified evidence","evidence_for":bid}]});
        let result = derive_conclusions(&cfg, &data, "fixture", "s2", &auth()).unwrap();
        assert_eq!(result["cross_subject"], 1);
        assert_eq!(result["folded"], 1);
        assert_eq!(result["derived"], 0);
    }
    #[test]
    fn model_json_accepts_literal_multiline_body_and_quoted_braces_without_eval() {
        let output = "prose ```json\n{\"skills\":[{\"body\":\"line one\nline {two}\"}]}\n```";
        let parsed = extract_json(output).unwrap();
        assert_eq!(parsed["skills"][0]["body"], "line one\nline {two}");
        assert_eq!(extract_json("{\"memory\":[]}"), Ok(json!({"memory":[]})));
        assert!(extract_json("not JSON").is_err());
    }
}
