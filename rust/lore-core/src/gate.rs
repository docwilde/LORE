//! Trusted caller policy and Python-compatible informational provenance.
//! Authority is supplied by the carrier; proposal JSON never grants it.
use crate::{
    config::{valid_skill_name, Config},
    files, Error, Result,
};
use serde_json::{json, Value};
#[cfg(test)]
use std::fs;
use std::{io::Write, path::Path};

#[derive(Clone, Debug)]
pub enum Authority {
    HumanReview {
        agent: String,
        engine: String,
    },
    Interactive {
        agent: String,
        engine: String,
    },
    Model {
        agent: String,
        engine: String,
        session_id: String,
    },
    Derived {
        agent: String,
        engine: String,
    },
}
impl Authority {
    pub fn may_write(&self) -> bool {
        matches!(self, Self::HumanReview { .. } | Self::Interactive { .. })
    }
    pub fn may_derive_beliefs(&self) -> bool {
        matches!(self, Self::Derived { .. })
    }
    pub fn require_review(&self) -> Result<()> {
        if matches!(self, Self::HumanReview { .. }) {
            Ok(())
        } else {
            Err(Error::Untrusted)
        }
    }
    pub fn writer(&self) -> &'static str {
        match self {
            Self::HumanReview { .. } => "terminal",
            Self::Interactive { .. } => "interactive",
            Self::Model { .. } => "model",
            Self::Derived { .. } => "derived",
        }
    }
    pub fn agent(&self) -> &str {
        match self {
            Self::HumanReview { agent, .. }
            | Self::Interactive { agent, .. }
            | Self::Model { agent, .. }
            | Self::Derived { agent, .. } => agent,
        }
    }
    pub fn engine(&self) -> &str {
        match self {
            Self::HumanReview { engine, .. }
            | Self::Interactive { engine, .. }
            | Self::Model { engine, .. }
            | Self::Derived { engine, .. } => engine,
        }
    }
}
pub fn one_line(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}
pub fn current_engine(value: &str) -> String {
    let value = value.trim().to_lowercase();
    if !value.is_empty()
        && value.len() <= 32
        && value.as_bytes()[0].is_ascii_lowercase()
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        })
    {
        value
    } else {
        "unknown".into()
    }
}
pub fn entry_key(kind: &str, bucket: &str, text: &str) -> String {
    format!(
        "{kind}:{bucket}:{}",
        &crate::digest(one_line(text).to_lowercase().as_bytes())[..20]
    )
}
fn load(cfg: &Config) -> Result<Value> {
    let path = cfg.root.join("provenance.json");
    let mut data = if !path.try_exists()? {
        json!({"version":1,"entries":{}})
    } else {
        serde_json::from_slice::<Value>(&files::read_regular(&path, crate::MAX_FRAME_BYTES)?)
            .map_err(|_| Error::Unavailable)?
    };
    if !data.is_object() {
        data = json!({"version":1,"entries":{}});
    }
    if !data["entries"].is_object() {
        data["entries"] = json!({});
    }
    if data.get("version").is_none() {
        data["version"] = json!(1);
    }
    Ok(data)
}
fn save(cfg: &Config, data: &Value) -> Result<()> {
    let mut bytes = serde_json::to_vec_pretty(data).map_err(|_| Error::Unavailable)?;
    bytes.push(b'\n');
    if bytes.len() > crate::MAX_FRAME_BYTES {
        return Err(Error::TooLarge);
    }
    files::atomic_write(&cfg.root.join("provenance.json"), &bytes)
}
pub fn provenance(cfg: &Config, kind: &str, bucket: &str, text: &str) -> Value {
    load(cfg)
        .ok()
        .and_then(|data| data["entries"].get(entry_key(kind, bucket, text)).cloned())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}
pub fn record(
    cfg: &Config,
    kind: &str,
    bucket: &str,
    text: &str,
    via: &str,
    origin: Option<&str>,
    authority: &Authority,
    source_engine: Option<&str>,
) -> Result<()> {
    let path = cfg.root.join("provenance.json");
    let _lock = files::Locks::acquire(&cfg.root, &[path], cfg.timeout)?;
    let mut data = load(cfg)?;
    let mut row = json!({"writer":authority.writer(),"via":via,"at":crate::utcnow(),"agent":authority.agent()});
    if let Some(origin) = origin.filter(|value| !value.is_empty()) {
        row["origin"] = json!(origin);
    }
    if kind == "memory" {
        row["source_engine"] = json!(current_engine(source_engine.unwrap_or(authority.engine())));
    }
    data["entries"][entry_key(kind, bucket, text)] = row;
    save(cfg, &data)
}
pub fn record_preserved(
    cfg: &Config,
    kind: &str,
    bucket: &str,
    text: &str,
    record: &Value,
    origin: &str,
) -> Result<()> {
    let path = cfg.root.join("provenance.json");
    let _lock = files::Locks::acquire(&cfg.root, &[path], cfg.timeout)?;
    let mut data = load(cfg)?;
    let mut record = record.clone();
    if !record.is_object() {
        record = json!({});
    }
    // Moving an old unlabelled fact cannot invent an author or engine.
    if record.as_object().is_some_and(|value| !value.is_empty()) {
        record["origin"] = json!(origin);
        data["entries"][entry_key(kind, bucket, text)] = record;
        save(cfg, &data)?;
    }
    Ok(())
}
pub fn forget(cfg: &Config, kind: &str, bucket: &str, text: &str) -> Result<()> {
    let path = cfg.root.join("provenance.json");
    let _lock = files::Locks::acquire(&cfg.root, &[path], cfg.timeout)?;
    let mut data = load(cfg)?;
    if data["entries"]
        .as_object_mut()
        .unwrap()
        .remove(&entry_key(kind, bucket, text))
        .is_some()
    {
        save(cfg, &data)?;
    }
    Ok(())
}
pub fn source_labels(cfg: &Config, bucket: &str, entries: &[String]) -> Vec<Option<String>> {
    let data = load(cfg).unwrap_or_else(|_| json!({"entries":{}}));
    entries
        .iter()
        .map(|text| {
            data["entries"][entry_key("memory", bucket, text)]["source_engine"]
                .as_str()
                .filter(|engine| *engine != "unknown")
                .map(str::to_owned)
        })
        .collect()
}
pub fn provenance_tag(cfg: &Config, kind: &str, bucket: &str, entries: &[String]) -> String {
    let data = load(cfg).unwrap_or_else(|_| json!({"entries":{}}));
    let mut counts = std::collections::BTreeMap::<String, usize>::new();
    for text in entries {
        let row = &data["entries"][entry_key(kind, bucket, text)];
        let label = match row["via"].as_str() {
            Some(value @ ("approved" | "derived" | "dream")) => value,
            _ => row["writer"].as_str().unwrap_or("unknown"),
        };
        *counts.entry(label.to_owned()).or_default() += 1;
    }
    if counts.is_empty() {
        return String::new();
    }
    let mut parts = Vec::new();
    for label in [
        "approved",
        "interactive",
        "terminal",
        "derived",
        "dream",
        "hook",
        "detached",
        "unknown",
    ] {
        if let Some(count) = counts.remove(label) {
            parts.push(format!("{count} {label}"));
        }
    }
    parts.extend(
        counts
            .into_iter()
            .map(|(label, count)| format!("{count} {label}")),
    );
    format!(" — provenance: {}", parts.join(", "))
}
pub fn append_file_op(
    cfg: &Config,
    class: &str,
    op: &str,
    slug: Option<&str>,
    payload: &Value,
) -> Result<()> {
    let configured = match class {
        "belief" => "beliefs",
        "skill" => "skills",
        "session" => "sessions",
        other => other,
    };
    if !cfg.sync.enabled || !cfg.sync.classes.contains(configured) {
        return Ok(());
    }
    let mut conn = crate::store::connect(cfg)?;
    let key = slug
        .map(|slug| crate::store::project_key_for_slug(&conn, slug))
        .transpose()?;
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    crate::store::append_op(cfg, &tx, class, op, key.as_deref(), payload)?;
    tx.commit()?;
    Ok(())
}
pub fn pending_project(item: &Value) -> Option<&str> {
    if item["kind"] == "memory" && matches!(item["scope"].as_str(), Some("user" | "machine")) {
        None
    } else {
        item["project"].as_str().filter(|value| !value.is_empty())
    }
}
pub fn stage(cfg: &Config, item: &Value, authority: &Authority) -> Result<String> {
    if !item.is_object()
        || item["kind"] == "skill" && !item["name"].as_str().is_some_and(valid_skill_name)
    {
        return Err(Error::InvalidRequest);
    }
    let mut payload = item.clone();
    payload["created"] = json!(crate::utcnow());
    payload["derived_by"] = json!(authority.agent());
    payload["writer"] = json!(authority.writer());
    if matches!(payload["kind"].as_str(), Some("memory" | "belief")) {
        payload["source_engine"] = json!(current_engine(authority.engine()));
    }
    payload["uid"] = json!(uuid::Uuid::new_v4().to_string());
    let bytes = serde_json::to_vec_pretty(&payload).map_err(|_| Error::Unavailable)?;
    if bytes.len() > crate::MAX_REVIEW_BYTES {
        return Err(Error::TooLarge);
    }
    let dir = cfg.root.join("pending");
    files::private_dir(&dir)?;
    let _namespace = crate::pending::namespace_lock(cfg)?;
    let directory = files::open_directory(&dir)?;
    let stamp = crate::utcnow().replace(['-', ':', 'T', 'Z'], "");
    for index in 0..10000 {
        let id = format!("{stamp}-{index:02}");
        let name = format!("{id}.json");
        match files::create_private_file(&directory, std::ffi::OsStr::new(&name), true) {
            Ok(mut file) => {
                file.write_all(&bytes).map_err(|_| Error::MayHaveApplied)?;
                file.sync_all().map_err(|_| Error::MayHaveApplied)?;
                directory.sync_all().map_err(|_| Error::MayHaveApplied)?;
                drop(_namespace);
                append_file_op(
                    cfg,
                    "pending",
                    "stage",
                    pending_project(&payload),
                    &json!({"uid":payload["uid"],"item":payload}),
                )
                .map_err(|_| Error::MayHaveApplied)?;
                return Ok(id);
            }
            Err(Error::Changed) => continue,
            Err(error) => return Err(error),
        }
    }
    Err(Error::OverCap)
}
pub fn cwd(req: &Value) -> Result<&Path> {
    req["cwd"]
        .as_str()
        .filter(|cwd| !cwd.is_empty() && cwd.len() <= 4096 && !cwd.contains('\0'))
        .map(Path::new)
        .filter(|path| path.is_absolute())
        .ok_or(Error::InvalidRequest)
}

#[cfg(test)]
mod tests {
    #[test]
    fn landed_proposal_sync_failure_is_explicit_and_disabled_class_never_opens_database() {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("lore"));
        cfg.sync.enabled = true;
        cfg.sync.classes = ["pending".into()].into_iter().collect();
        files::private_dir(&cfg.root).unwrap();
        fs::write(cfg.root.join("state.db"), b"owned corrupt fixture").unwrap();
        let auth = Authority::Model {
            agent: "fixture".into(),
            engine: "codex".into(),
            session_id: "fixture".into(),
        };
        assert_eq!(
            stage(
                &cfg,
                &json!({"kind":"memory","scope":"user","text":"private fixture"}),
                &auth
            ),
            Err(Error::MayHaveApplied)
        );
        let ids = crate::pending::ids(&cfg).unwrap();
        assert_eq!(ids.len(), 1);
        let item: Value = serde_json::from_slice(
            &files::read_regular(
                &cfg.root.join("pending").join(format!("{}.json", ids[0])),
                65536,
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(item["text"], "private fixture");
        assert_eq!(item["source_engine"], "codex");
        cfg.sync.classes.clear();
        assert!(stage(
            &cfg,
            &json!({"kind":"memory","scope":"user","text":"disabled fixture"}),
            &auth
        )
        .is_ok());
        assert_eq!(
            fs::read(cfg.root.join("state.db")).unwrap(),
            b"owned corrupt fixture"
        );
    }

    use super::*;
    #[test]
    fn provenance_keys_match_python_unicode_normalization() {
        assert_eq!(
            entry_key("memory", "user", "  Hello\n WORLD "),
            "memory:user:b94d27b9934d3e08a52e"
        );
        assert_eq!(
            entry_key("memory", "user", "Ä  B"),
            entry_key("memory", "user", "ä b")
        );
        assert_eq!(current_engine("../bad"), "unknown");
    }
    #[test]
    fn disabled_sync_does_not_open_database_or_create_store_during_replay() {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("absent"));
        cfg.sync.enabled = false;
        append_file_op(
            &cfg,
            "memory",
            "add",
            Some("project"),
            &json!({"text":"fixture"}),
        )
        .unwrap();
        assert!(!cfg.root.exists());
    }
    #[test]
    fn model_json_cannot_grant_review_or_choose_provenance() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let auth = Authority::Model {
            agent: "owner".into(),
            engine: "codex".into(),
            session_id: "s".into(),
        };
        assert_eq!(auth.require_review(), Err(Error::Untrusted));
        let id=stage(&cfg,&json!({"kind":"memory","scope":"user","text":"fixture","writer":"terminal","source_engine":"claude"}),&auth).unwrap();
        let row: Value = serde_json::from_slice(
            &files::read_regular(&cfg.root.join("pending").join(format!("{id}.json")), 65536)
                .unwrap(),
        )
        .unwrap();
        assert_eq!(row["writer"], "model");
        assert_eq!(row["source_engine"], "codex");
        assert_eq!(row["derived_by"], "owner");
        assert!(cfg.root.join("USER.md").try_exists().unwrap() == false);
    }
}
