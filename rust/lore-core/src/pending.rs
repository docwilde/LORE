//! Exact reviewed proposal claims. Failed application preserves a recoverable
//! proposal; archive failure explicitly reports whether application landed.
use crate::{
    config::{project_slug, valid_id, valid_skill_name, valid_slug, Config},
    files,
    gate::{self, Authority},
    memory::{self, Scope},
    Error, Result,
};
use serde_json::{json, Value};
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
use std::{
    fs::{self, File, Metadata},
    io::{Read, Write},
    path::{Path, PathBuf},
};

pub trait PendingApplier {
    fn apply_belief(&self, cfg: &Config, item: &Value, authority: &Authority) -> Result<()>;
    fn apply_sync(&self, cfg: &Config, op: &Value, authority: &Authority) -> Result<()>;
}
#[derive(Debug)]
pub struct Snapshot {
    pub raw: String,
    pub item: Value,
    pub sha256: String,
    pub inode: u64,
}
fn proposal_path(cfg: &Config, id: &str) -> Result<PathBuf> {
    if !valid_id(id) {
        return Err(Error::InvalidRequest);
    }
    Ok(cfg.root.join("pending").join(format!("{id}.json")))
}
fn same_file(before: &Metadata, after: &Metadata) -> bool {
    #[cfg(unix)]
    {
        before.ino() == after.ino()
            && before.len() == after.len()
            && before.ctime() == after.ctime()
            && before.ctime_nsec() == after.ctime_nsec()
    }
    #[cfg(not(unix))]
    {
        before.len() == after.len() && before.modified().ok() == after.modified().ok()
    }
}
pub fn snapshot(path: &Path) -> Result<Snapshot> {
    let directory = files::open_directory(path.parent().ok_or(Error::UnsafePath)?)?;
    snapshot_at(&directory, path.file_name().ok_or(Error::UnsafePath)?)
}
fn snapshot_at(directory: &File, name: &std::ffi::OsStr) -> Result<Snapshot> {
    let mut file = files::open_regular_at(directory, name, crate::MAX_REVIEW_BYTES)?;
    let before = file.metadata()?;
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.len() > crate::MAX_REVIEW_BYTES as u64
        || !same_file(&before, &metadata)
    {
        return Err(Error::Changed);
    }
    #[cfg(unix)]
    if metadata.uid() != unsafe { libc::geteuid() } || metadata.nlink() != 1 {
        return Err(Error::UnsafePath);
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(crate::MAX_REVIEW_BYTES as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > crate::MAX_REVIEW_BYTES {
        return Err(Error::TooLarge);
    }
    if !same_file(&metadata, &file.metadata()?)
        || !same_file(
            &metadata,
            &files::open_regular_at(directory, name, crate::MAX_REVIEW_BYTES)?.metadata()?,
        )
    {
        return Err(Error::Changed);
    }
    let sha256 = crate::digest(&bytes);
    let raw = String::from_utf8(bytes).map_err(|_| Error::InvalidRequest)?;
    let item: Value = serde_json::from_str(&raw).map_err(|_| Error::InvalidRequest)?;
    if !item.is_object() {
        return Err(Error::InvalidRequest);
    }
    #[cfg(unix)]
    let inode = metadata.ino();
    #[cfg(not(unix))]
    let inode = 0;
    if inode == 0 {
        return Err(Error::Unsupported);
    }
    Ok(Snapshot {
        raw,
        item,
        sha256,
        inode,
    })
}
fn visible(item: &Value, slug: &str) -> bool {
    // File maps and reviewed skills carry their project in `project`, without
    // a `scope` field. Use the same project identity as agent context so their
    // proposals cannot be listed or approved from an unrelated checkout.
    if item["kind"] == "belief" {
        if let Some(project) = item["subject"].as_str().and_then(|subject| subject.strip_prefix("project:")) {
            // Older staged beliefs omitted `project`; their subject still
            // carries the boundary. A conflicting explicit project refuses
            // review from either checkout.
            return !project.is_empty() && project == slug
                && item["project"].as_str().is_none_or(|staged| staged == project);
        }
    }
    if item["scope"] == "project" || item["kind"] == "filemap" {
        return item["project"].as_str().is_some_and(|project| !project.is_empty() && project == slug);
    }
    gate::pending_project(item).is_none_or(|project| project == slug)
}
pub fn ids(cfg: &Config) -> Result<Vec<String>> {
    let dir = cfg.root.join("pending");
    if fs::symlink_metadata(&dir).is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound) {
        return Ok(Vec::new());
    }
    let mut ids = Vec::new();
    for name in files::directory_names(&dir, 4096)? {
        let path = dir.join(name);
        if path
            .extension()
            .is_some_and(|extension| extension == "json")
        {
            if let Some(id) = path
                .file_stem()
                .and_then(|value| value.to_str())
                .filter(|id| valid_id(id))
            {
                ids.push(id.to_owned());
            }
        }
    }
    ids.sort();
    Ok(ids)
}
fn scrub_display(value: &Value, depth: usize) -> Result<Value> {
    if depth > 32 {
        return Err(Error::TooLarge);
    }
    Ok(match value {
        Value::String(text) => json!(crate::scrub::scrub(text)?),
        Value::Array(values) => Value::Array(
            values
                .iter()
                .map(|value| scrub_display(value, depth + 1))
                .collect::<Result<Vec<_>>>()?,
        ),
        Value::Object(values) => {
            let mut output = serde_json::Map::new();
            for (key, value) in values {
                let key = crate::scrub::scrub(key)?;
                if output.contains_key(&key) {
                    return Err(Error::InvalidRequest);
                }
                output.insert(key, scrub_display(value, depth + 1)?);
            }
            Value::Object(output)
        }
        _ => value.clone(),
    })
}
fn public_project_key(value: &mut Value) -> Result<()> {
    match value {
        Value::Null => {}
        Value::String(text)
            if !text.is_empty() && text.len() <= 2048 && !text.chars().any(char::is_control) =>
        {
            if crate::scrub::scrub(text)? != *text {
                return Err(Error::Untrusted);
            }
        }
        _ => return Err(Error::InvalidRequest),
    }
    // This is a typed public project identity, not a credential named *_key.
    *value = Value::Null;
    Ok(())
}
fn content_pairs(value: &Value, depth: usize) -> Result<()> {
    if depth > 32 {
        return Err(Error::TooLarge);
    }
    match value {
        Value::Object(fields) => {
            for (key, value) in fields {
                if value.is_string() || value.is_number() {
                    let pair = json!({key:value});
                    let text = serde_json::to_string(&pair).map_err(|_| Error::Unavailable)?;
                    if crate::scrub::scrub(&text)? != text {
                        return Err(Error::Untrusted);
                    }
                }
                content_pairs(value, depth + 1)?;
            }
        }
        Value::Array(values) => {
            for value in values {
                content_pairs(value, depth + 1)?;
            }
        }
        _ => {}
    }
    Ok(())
}
fn reviewable(snapshot: &Snapshot) -> Result<()> {
    let mut checked = snapshot.item.clone();
    if checked["kind"] == "sync" {
        let op = checked
            .get_mut("op")
            .filter(|value| value.is_object())
            .ok_or(Error::InvalidRequest)?;
        for (key, cap) in [
            ("op_id", 128),
            ("machine_id", 128),
            ("class", 64),
            ("op", 64),
        ] {
            if !op[key].as_str().is_some_and(|text| {
                !text.is_empty() && text.len() <= cap && !text.chars().any(char::is_control)
            }) {
                return Err(Error::InvalidRequest);
            }
        }
        for key in ["machine_seq", "lamport"] {
            if !op[key].as_i64().is_some_and(|value| value >= 0) {
                return Err(Error::InvalidRequest);
            }
        }
        if !op["payload"].is_object() {
            return Err(Error::InvalidRequest);
        }
        if let Some(mac) = op.get("mac").filter(|value| !value.is_null()) {
            if !mac.as_str().is_some_and(|mac| {
                mac.len() == 64
                    && mac
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            }) {
                return Err(Error::InvalidRequest);
            }
        }
        let id = op["op_id"].as_str().unwrap().to_owned();
        let project_key = op.get_mut("project_key").ok_or(Error::InvalidRequest)?;
        public_project_key(project_key)?;
        if op["class"] == "session" && op["op"] == "upsert" {
            if let Some(value) = op["payload"].get_mut("project_key") {
                public_project_key(value)?;
            }
        }
        if op["class"] == "belief" && matches!(op["op"].as_str(), Some("insert" | "reinforce")) {
            if let Some(value) = op["payload"]["evidence"].get_mut("project_key") {
                public_project_key(value)?;
            }
        }
        if matches!(op["class"].as_str(), Some("memory" | "filemap"))
            && matches!(op["op"].as_str(), Some("remove" | "replace"))
        {
            let field = if op["op"] == "remove" {
                "key"
            } else {
                "old_key"
            };
            let value = op["payload"].get_mut(field).ok_or(Error::InvalidRequest)?;
            let key = value
                .as_str()
                .filter(|key| key.len() <= 4096 && !key.chars().any(char::is_control))
                .ok_or(Error::InvalidRequest)?;
            let (prefix, digest) = key.rsplit_once(':').ok_or(Error::InvalidRequest)?;
            if !prefix.starts_with("memory:") && !prefix.starts_with("filemap:")
                || digest.len() != 20
                || !digest
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            {
                return Err(Error::InvalidRequest);
            }
            *value = Value::Null;
        }
        // MAC and deterministic proposal UID are public typed identifiers.
        // This clone is ONLY a secret check; raw review bytes and exact proof
        // are unchanged, and neither metadata field grants write authority.
        op["mac"] = Value::Null;
        if checked["uid"] == crate::digest(id.as_bytes()) {
            checked["uid"] = Value::Null;
        }
    }
    if scrub_display(&checked, 0)? != checked {
        return Err(Error::Untrusted);
    }
    // Checking individual scalar pairs catches {api_key: <secret>} while
    // avoiding a regex crossing JSON null/array/object structural delimiters.
    content_pairs(&checked, 0)
}
pub fn list(cfg: &Config, req: &Value) -> Result<Value> {
    let slug = project_slug(gate::cwd(req)?);
    let offset = req
        .get("offset")
        .map_or(Some(0), Value::as_u64)
        .filter(|value| *value <= 10000)
        .ok_or(Error::InvalidRequest)? as usize;
    let limit = req
        .get("limit")
        .map_or(Some(50), Value::as_u64)
        .filter(|value| *value <= 50)
        .ok_or(Error::InvalidRequest)? as usize;
    let mut rows = Vec::new();
    let mut visible_count = 0;
    for id in ids(cfg)? {
        let Ok(snap) = snapshot(&proposal_path(cfg, &id)?) else {
            continue;
        };
        if !visible(&snap.item, &slug) {
            continue;
        }
        if visible_count < offset {
            visible_count += 1;
            continue;
        }
        if rows.len() >= limit {
            break;
        }
        let mut row = json!({"pid":id});
        for field in [
            "kind",
            "action",
            "scope",
            "project",
            "subject",
            "id",
            "confidence",
            "session_id",
            "derived_by",
            "created",
            "writer",
            "origin_project",
            "subject_unresolved",
            "to",
        ] {
            if let Some(value) = snap.item.get(field).filter(|value| !value.is_null()) {
                row[field] = scrub_display(value, 0)?;
            }
        }
        for field in [
            "text",
            "claim",
            "match",
            "path",
            "purpose",
            "name",
            "description",
            "evidence",
            "reason",
            "writer_evidence",
        ] {
            if let Some(value) = snap.item.get(field).filter(|value| !value.is_null()) {
                let text = value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string());
                row[field] = json!(crate::scrub::scrub(&text)?);
            }
        }
        rows.push(row);
    }
    Ok(json!(rows))
}
fn check_expected(req: &Value, snapshot: &Snapshot) -> Result<()> {
    let expected = req["expected"]
        .as_object()
        .filter(|value| {
            value.len() == 2 && value.contains_key("sha256") && value.contains_key("inode")
        })
        .ok_or(Error::InvalidRequest)?;
    let sha = expected["sha256"]
        .as_str()
        .filter(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .ok_or(Error::InvalidRequest)?;
    let inode = expected["inode"]
        .as_u64()
        .filter(|value| *value > 0)
        .ok_or(Error::InvalidRequest)?;
    if sha != snapshot.sha256 || inode != snapshot.inode {
        return Err(Error::Changed);
    }
    Ok(())
}
pub fn review(cfg: &Config, req: &Value) -> Result<Value> {
    let slug = project_slug(gate::cwd(req)?);
    let id = req["pid"].as_str().ok_or(Error::InvalidRequest)?;
    let snap = snapshot(&proposal_path(cfg, id)?)?;
    if !visible(&snap.item, &slug) {
        return Err(Error::Untrusted);
    }
    reviewable(&snap)?;
    if req.get("expected").is_some() {
        check_expected(req, &snap)?;
    }
    Ok(json!({"pid":id,"raw":snap.raw,"sha256":snap.sha256,"inode":snap.inode,"complete":true}))
}
fn listings(cfg: &Config) -> Result<Value> {
    let path = cfg.root.join("pending/.listed");
    if !path.try_exists()? {
        return Ok(json!({}));
    }
    let value: Value = serde_json::from_slice(&files::read_regular(&path, crate::MAX_FRAME_BYTES)?)
        .map_err(|_| Error::Unavailable)?;
    if !value.is_object() {
        return Err(Error::Unavailable);
    }
    Ok(value)
}
fn save_listings(cfg: &Config, data: &Value) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(data).map_err(|_| Error::Unavailable)?;
    if bytes.len() > crate::MAX_FRAME_BYTES {
        return Err(Error::TooLarge);
    }
    files::atomic_write(&cfg.root.join("pending/.listed"), &bytes)
}
fn record_full_locked(cfg: &Config, id: &str, snap: &Snapshot) -> Result<()> {
    let mut data = listings(cfg)?;
    data[id] = json!({"sha256":snap.sha256,"ino":snap.inode,"reviewed":true});
    save_listings(cfg, &data)
}
pub fn mark_full_review(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    authority.require_review()?;
    let id = req["pid"].as_str().ok_or(Error::InvalidRequest)?;
    let path = proposal_path(cfg, id)?;
    let _locks = files::Locks::acquire(
        &cfg.root,
        &[path.clone(), cfg.root.join("pending/.listed")],
        cfg.timeout,
    )?;
    let snap = snapshot(&path)?;
    check_expected(req, &snap)?;
    reviewable(&snap)?;
    if !visible(&snap.item, &project_slug(gate::cwd(req)?)) {
        return Err(Error::Untrusted);
    }
    record_full_locked(cfg, id, &snap)?;
    Ok(json!({"status":"reviewed"}))
}
fn listed_exact(data: &Value, id: &str, snap: &Snapshot) -> bool {
    let row = &data[id];
    row["reviewed"] == true
        && row["sha256"] == snap.sha256
        && row["ino"].as_u64().is_none_or(|inode| inode == snap.inode)
}
fn changed_listing(data: &Value, id: &str, snap: &Snapshot) -> bool {
    let row = &data[id];
    let digest = row.as_str().or_else(|| row["sha256"].as_str());
    digest.is_some_and(|digest| {
        digest != snap.sha256 || row["ino"].as_u64().is_some_and(|inode| inode != snap.inode)
    })
}
fn private_claim_dir(cfg: &Config) -> Result<PathBuf> {
    let dir = cfg.root.join("pending/.claimed");
    if let Ok(metadata) = fs::symlink_metadata(&dir) {
        if !metadata.is_dir() {
            return Err(Error::UnsafePath);
        }
        #[cfg(unix)]
        if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o077 != 0 {
            return Err(Error::UnsafePath);
        }
    }
    files::private_dir(&dir)?;
    Ok(dir)
}
#[cfg(test)]
fn restore_claim(source: &Path, claimed: &Path, id: &str) -> Result<()> {
    let source_dir = files::open_directory(source.parent().ok_or(Error::UnsafePath)?)?;
    let claim_dir = files::open_directory(claimed.parent().ok_or(Error::UnsafePath)?)?;
    restore_claim_at(
        &source_dir,
        source.file_name().ok_or(Error::UnsafePath)?,
        &claim_dir,
        claimed.file_name().ok_or(Error::UnsafePath)?,
        id,
    )
}
fn restore_claim_at(
    source_dir: &File,
    source_name: &std::ffi::OsStr,
    claim_dir: &File,
    claim_name: &std::ffi::OsStr,
    id: &str,
) -> Result<()> {
    match files::link_move_at(claim_dir, claim_name, source_dir, source_name) {
        Ok(()) => Ok(()),
        Err(Error::Changed) => {
            let recovery = format!("{id}-recovered-{}.json", uuid::Uuid::new_v4().simple());
            files::rename_at(
                claim_dir,
                claim_name,
                source_dir,
                std::ffi::OsStr::new(&recovery),
                false,
            )?;
            Err(Error::Changed)
        }
        Err(error) => Err(error),
    }
}
fn archive(cfg: &Config, id: &str, claimed: &Path, snap: &Snapshot, status: &str) -> Result<()> {
    archive_inner(cfg, id, claimed, snap, status, true)
}
fn archive_inner(
    cfg: &Config,
    id: &str,
    claimed: &Path,
    snap: &Snapshot,
    status: &str,
    tidy_listing: bool,
) -> Result<()> {
    let mut item = snap.item.clone();
    item["status"] = json!(status);
    item["resolved"] = json!(crate::utcnow());
    let claim_dir = files::open_directory(claimed.parent().ok_or(Error::UnsafePath)?)?;
    let claim_name = claimed.file_name().ok_or(Error::UnsafePath)?;
    let current = snapshot_at(&claim_dir, claim_name)?;
    if current.inode != snap.inode || current.sha256 != snap.sha256 {
        return Err(Error::Changed);
    }
    let dir = cfg.root.join("pending/archive");
    files::private_dir(&dir)?;
    let archive_dir = files::open_directory(&dir)?;
    let name = format!("{id}-{status}-{}.json", uuid::Uuid::new_v4().simple());
    let bytes = serde_json::to_vec_pretty(&item).map_err(|_| Error::Unavailable)?;
    let mut archived = files::create_private_file(&archive_dir, std::ffi::OsStr::new(&name), true)?;
    archived
        .write_all(&bytes)
        .map_err(|_| Error::MayHaveApplied)?;
    archived.sync_all().map_err(|_| Error::MayHaveApplied)?;
    archive_dir.sync_all().map_err(|_| Error::MayHaveApplied)?;
    let current = snapshot_at(&claim_dir, claim_name)?;
    if current.inode != snap.inode || current.sha256 != snap.sha256 {
        return Err(Error::MayHaveApplied);
    }
    files::unlink_at(&claim_dir, claim_name).map_err(|_| Error::MayHaveApplied)?;
    if tidy_listing
        && fs::symlink_metadata(proposal_path(cfg, id)?)
            .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound)
    {
        let mut data = listings(cfg)?;
        if data.as_object_mut().unwrap().remove(id).is_some() {
            save_listings(cfg, &data)?;
        }
    }
    if let Some(uid) = item["uid"].as_str() {
        gate::append_file_op(
            cfg,
            "pending",
            "resolve",
            gate::pending_project(&item),
            &json!({"uid":uid,"status":status}),
        )
        .map_err(|_| Error::MayHaveApplied)?;
    }
    Ok(())
}
/// Portable identities are equality keys, never local filenames. Preserve the
/// wire byte bound while refusing control characters in a human review identity.
pub(crate) fn valid_portable_uid(uid: &str) -> bool {
    !uid.is_empty() && uid.len() <= 128 && !uid.chars().any(char::is_control)
}
/// Internal MAC-verified sync replay only; does not apply proposal content or
/// infer human authority from its stored fields. The dispatcher owns admission.
pub(crate) fn archive_uid(cfg: &Config, uid: &str, status: &str) -> Result<bool> {
    if !valid_portable_uid(uid) || !matches!(status, "approved" | "rejected") {
        return Err(Error::InvalidRequest);
    }
    let matches = ids(cfg)?
        .into_iter()
        .filter_map(|id| {
            snapshot(&proposal_path(cfg, &id).ok()?)
                .ok()
                .filter(|snap| snap.item["uid"] == uid)
                .map(|snap| (id, snap))
        })
        .collect::<Vec<_>>();
    if matches.len() > 1 {
        return Err(Error::Changed);
    }
    let Some((id, before)) = matches.into_iter().next() else {
        return Ok(false);
    };
    let source = proposal_path(cfg, &id)?;
    let _locks = files::Locks::acquire(&cfg.root, &[source.clone()], cfg.timeout)?;
    let source_dir = files::open_directory(source.parent().ok_or(Error::UnsafePath)?)?;
    let snap = snapshot_at(&source_dir, source.file_name().ok_or(Error::UnsafePath)?)?;
    if snap.sha256 != before.sha256 || snap.inode != before.inode {
        return Err(Error::Changed);
    }
    let claim_path = private_claim_dir(cfg)?;
    let claim_dir = files::open_directory(&claim_path)?;
    let claimed = claim_path.join(format!("{id}-{}.json", uuid::Uuid::new_v4().simple()));
    files::rename_at(
        &source_dir,
        source.file_name().ok_or(Error::UnsafePath)?,
        &claim_dir,
        claimed.file_name().ok_or(Error::UnsafePath)?,
        false,
    )?;
    let mut local = cfg.clone();
    local.sync.enabled = false;
    archive_inner(&local, &id, &claimed, &snap, status, false)
        .map_err(|_| Error::MayHaveApplied)?;
    Ok(true)
}
pub fn skill_file_text(name: &str, description: Option<&str>, body: &str) -> String {
    let frontmatter = body
        .strip_prefix("---\n")
        .and_then(|value| value.split_once("\n---\n"))
        .is_some_and(|(head, _)| {
            head.lines().any(|line| {
                line.strip_prefix("name:")
                    .is_some_and(|value| !value.trim().is_empty())
            })
        });
    if frontmatter {
        return body.into();
    }
    format!(
        "---\nname: {name}\ndescription: \"{} (lore-learned)\"\n---\n\n{body}\n",
        description.unwrap_or(name).replace('"', "'")
    )
}
fn apply_skill(cfg: &Config, item: &Value, authority: &Authority) -> Result<()> {
    authority.require_review()?;
    let name = item["name"]
        .as_str()
        .filter(|name| valid_skill_name(name))
        .ok_or(Error::UnsafePath)?;
    // private_dir checks every ancestor for symlinks before touching either
    // installation or retirement destination. No user-installed skill is
    // overwritten/retired unless its canonical lore-learned marker permits it.
    files::private_dir(&cfg.skills)?;
    let directory = cfg.skills.join(name);
    let target = directory.join("SKILL.md");
    let _locks = files::Locks::acquire(&cfg.root, &[target.clone()], cfg.timeout)?;
    if let Ok(metadata) = fs::symlink_metadata(&directory) {
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(Error::UnsafePath);
        }
    }
    let existing = if target.try_exists()? {
        Some(
            String::from_utf8(files::read_regular(&target, crate::MAX_REVIEW_BYTES)?)
                .map_err(|_| Error::InvalidRequest)?,
        )
    } else {
        None
    };
    let learned = existing.as_ref().is_some_and(|text| {
        text.chars()
            .take(600)
            .collect::<String>()
            .contains("lore-learned")
    });
    if item["action"] == "retire" {
        if !learned {
            return Err(Error::Untrusted);
        }
        let graveyard = cfg.root.join("skills-retired");
        files::private_dir(&graveyard)?;
        let skills_dir = files::open_directory(&cfg.skills)?;
        let retired_dir = files::open_directory(&graveyard)?;
        let destination = format!("{name}-{}", crate::utcnow().replace(':', ""));
        files::rename_at(
            &skills_dir,
            std::ffi::OsStr::new(name),
            &retired_dir,
            std::ffi::OsStr::new(&destination),
            true,
        )?;
        gate::append_file_op(cfg, "skill", "remove", None, &json!({"name":name}))
            .map_err(|_| Error::MayHaveApplied)?;
        return Ok(());
    }
    if existing.is_some() && !(item["action"] == "update" && learned) {
        return Err(Error::Untrusted);
    }
    let body = item["body"].as_str().ok_or(Error::InvalidRequest)?;
    let body = crate::scrub::scrub(body)?;
    let description = item["description"]
        .as_str()
        .map(crate::scrub::scrub)
        .transpose()?;
    let text = skill_file_text(name, description.as_deref(), &body);
    if text.len() > crate::MAX_REVIEW_BYTES {
        return Err(Error::TooLarge);
    }
    files::atomic_write(&target, text.as_bytes())?;
    gate::append_file_op(cfg, "skill", "put", None, &json!({"name":name,"body":text}))
        .map_err(|_| Error::MayHaveApplied)
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ApplyOutcome {
    Complete,
    Partial(Error),
    Uncertain(Error),
}
impl From<memory::FileOutcome> for ApplyOutcome {
    fn from(outcome: memory::FileOutcome) -> Self {
        match outcome {
            memory::FileOutcome::Complete => Self::Complete,
            memory::FileOutcome::Partial(error) => Self::Partial(error),
            memory::FileOutcome::Uncertain(error) => Self::Uncertain(error),
        }
    }
}
pub fn apply_item(
    cfg: &Config,
    item: &Value,
    authority: &Authority,
    applier: &impl PendingApplier,
) -> Result<ApplyOutcome> {
    authority.require_review()?;
    for field in ["project", "host"] {
        if let Some(value) = item.get(field).filter(|value| !value.is_null()) {
            if !value.as_str().is_some_and(valid_slug) {
                return Err(Error::UnsafePath);
            }
        }
    }
    let cwd = gate::cwd(item)?;
    let project = item["project"]
        .as_str()
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| project_slug(cwd));
    match item["kind"].as_str() {
        Some("memory") => {
            let scope = Scope::parse(item["scope"].as_str().ok_or(Error::InvalidRequest)?)?;
            let key = if scope == Scope::Machine {
                memory::resolve_machine(cfg, item["host"].as_str())
            } else {
                project
            };
            let action = item["action"].as_str().unwrap_or("add");
            let needle = item["match"].as_str().unwrap_or("");
            if action == "move" {
                let destination = Scope::parse(item["to_scope"].as_str().unwrap_or(scope.name()))?;
                return memory::move_entry(
                    cfg,
                    scope,
                    &key,
                    needle,
                    destination,
                    item["to"].as_str().ok_or(Error::InvalidRequest)?,
                    authority,
                )
                .map(|outcome| match outcome {
                    memory::MoveOutcome::Complete => ApplyOutcome::Complete,
                    memory::MoveOutcome::DestinationOnly(error) => ApplyOutcome::Partial(error),
                    memory::MoveOutcome::Uncertain(error) => ApplyOutcome::Uncertain(error),
                });
            }
            let action = if action == "replace" && !needle.is_empty() {
                "replace"
            } else if action == "remove" && !needle.is_empty() {
                "remove"
            } else {
                "add"
            };
            let path = scope.path(cfg, &key)?;
            let _lock = files::Locks::acquire(&cfg.root, &[path.clone()], cfg.timeout)?;
            let action = if action == "replace"
                && memory::match_entries(&memory::read_entries(&path)?, needle).is_empty()
            {
                "add"
            } else {
                action
            };
            memory::observe_file_write(&[path], || {
                memory::mutate_locked(
                    cfg,
                    scope,
                    &key,
                    action,
                    needle,
                    item["text"].as_str().unwrap_or(""),
                    "approved",
                    None,
                    Some(item["source_engine"].as_str().unwrap_or("unknown")),
                    authority,
                )
            })
            .map(ApplyOutcome::from)
        }
        Some("filemap") => {
            let path = crate::filemap::path(cfg, &project)?;
            let _lock = files::Locks::acquire(&cfg.root, &[path.clone()], cfg.timeout)?;
            memory::observe_file_write(&[path], || {
                crate::filemap::mutate_locked(
                    cfg,
                    &project,
                    item["action"].as_str().unwrap_or("add"),
                    item["match"].as_str().unwrap_or(""),
                    item["path"].as_str().unwrap_or(""),
                    item["purpose"].as_str().unwrap_or(""),
                    None,
                    "approved",
                    authority,
                )
            })
            .map(ApplyOutcome::from)
        }
        Some("belief") => {
            let approved = Authority::HumanReview {
                agent: authority.agent().to_owned(),
                engine: gate::current_engine(item["source_engine"].as_str().unwrap_or("unknown")),
            };
            applier
                .apply_belief(cfg, item, &approved)
                .map(|_| ApplyOutcome::Complete)
        }
        Some("sync") => applier
            .apply_sync(
                cfg,
                item.get("op")
                    .filter(|value| value.is_object())
                    .ok_or(Error::InvalidRequest)?,
                authority,
            )
            .map(|_| ApplyOutcome::Complete),
        Some("skill") | None => {
            let name = item["name"]
                .as_str()
                .filter(|name| valid_skill_name(name))
                .ok_or(Error::UnsafePath)?;
            memory::observe_file_write(&[cfg.skills.join(name).join("SKILL.md")], || {
                apply_skill(cfg, item, authority)
            })
            .map(ApplyOutcome::from)
        }
        _ => Err(Error::Unsupported),
    }
}
pub fn resolve(
    cfg: &Config,
    req: &Value,
    authority: &Authority,
    applier: &impl PendingApplier,
) -> Result<Value> {
    authority.require_review()?;
    let id = req["pid"].as_str().ok_or(Error::InvalidRequest)?;
    let decision = req["decision"]
        .as_str()
        .filter(|decision| matches!(*decision, "approve" | "reject"))
        .ok_or(Error::InvalidRequest)?;
    let source = proposal_path(cfg, id)?;
    let _locks = files::Locks::acquire(
        &cfg.root,
        &[source.clone(), cfg.root.join("pending/.listed")],
        cfg.timeout,
    )?;
    let source_dir = files::open_directory(source.parent().ok_or(Error::UnsafePath)?)?;
    let source_name = source.file_name().ok_or(Error::UnsafePath)?;
    let before = snapshot_at(&source_dir, source_name)?;
    check_expected(req, &before)?;
    reviewable(&before)?;
    if !visible(&before.item, &project_slug(gate::cwd(req)?)) {
        return Err(Error::Untrusted);
    }
    // The carrier's explicit HumanReview plus expected exact snapshot is the
    // completed UI review signal. Model JSON cannot manufacture this marker.
    if decision == "approve" {
        record_full_locked(cfg, id, &before)?;
    }
    let claim_path = private_claim_dir(cfg)?;
    let claim_dir = files::open_directory(&claim_path)?;
    let claimed = claim_path.join(format!("{id}-{}.json", uuid::Uuid::new_v4().simple()));
    let claim_name = claimed.file_name().ok_or(Error::UnsafePath)?;
    files::rename_at(&source_dir, source_name, &claim_dir, claim_name, false)?;
    let checked = (|| {
        let snap = snapshot_at(&claim_dir, claim_name)?;
        check_expected(req, &snap)?;
        let listed = listings(cfg)?;
        if changed_listing(&listed, id, &snap)
            || snap.item["kind"] == "sync" && !listed_exact(&listed, id, &snap)
        {
            return Err(Error::Changed);
        }
        Ok(snap)
    })();
    let mut snap = match checked {
        Ok(snap) => snap,
        Err(error) => {
            restore_claim_at(&source_dir, source_name, &claim_dir, claim_name, id)?;
            return Err(error);
        }
    };
    let status = if decision == "approve" {
        "approved"
    } else {
        "rejected"
    };
    if decision == "approve" {
        let original_cwd = snap.item.get("cwd").cloned();
        snap.item["cwd"] = req["cwd"].clone();
        match apply_item(cfg, &snap.item, authority, applier) {
            Ok(ApplyOutcome::Complete) => {}
            Ok(ApplyOutcome::Partial(error)) => {
                return Ok(json!({"status":"refused","error":error.code(),"applied":true}))
            }
            Ok(ApplyOutcome::Uncertain(error)) => {
                return Ok(
                    json!({"status":"refused","error":error.code(),"applied":null,"may_have_applied":true}),
                )
            }
            Err(Error::MayHaveApplied) => {
                return Ok(
                    json!({"status":"refused","error":"may_have_applied","applied":null,"may_have_applied":true}),
                )
            }
            Err(error) => {
                restore_claim_at(&source_dir, source_name, &claim_dir, claim_name, id)?;
                return Ok(json!({"status":"refused","error":error.code(),"applied":false}));
            }
        }
        if let Some(cwd) = original_cwd {
            snap.item["cwd"] = cwd;
        } else {
            snap.item.as_object_mut().unwrap().remove("cwd");
        }
    }
    if let Err(error) = archive(cfg, id, &claimed, &snap, status) {
        return Ok(json!({"status":"refused","error":error.code(),"applied":decision=="approve"}));
    }
    Ok(json!({"status":status}))
}

#[cfg(test)]
mod tests {
    #[test]
    fn project_bound_proposals_are_hidden_outside_their_project() {
        let own = "own-project";
        let foreign = "another-project";
        for item in [
            json!({"kind":"memory","scope":"project","project":own}),
            json!({"kind":"filemap","project":own}),
            json!({"kind":"skill","project":own}),
            json!({"kind":"belief","project":own}),
        ] {
            assert!(visible(&item, own));
            assert!(!visible(&item, foreign));
        }
        assert!(!visible(&json!({"kind":"filemap"}), own));
        assert!(!visible(&json!({"kind":"memory","scope":"project"}), own));
        // User memory can record an origin project without becoming project-bound.
        assert!(visible(&json!({"kind":"memory","scope":"user","project":own}), foreign));
        // A synced skill conflict without a project is a global review item.
        assert!(visible(&json!({"kind":"skill","origin":"sync-skill-conflict"}), foreign));
    }

    #[test]
    fn foreign_filemap_cannot_be_listed_or_reviewed() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let own = temp.path().join("own");
        let foreign = temp.path().join("foreign");
        let pid = gate::stage(&cfg, &json!({
            "kind":"filemap", "project":project_slug(&own),
            "path":"src/lib.rs", "purpose":"fixture"
        }), &auth()).unwrap();
        let own_rows = list(&cfg, &json!({"cwd":own})).unwrap();
        assert_eq!(own_rows.as_array().unwrap().len(), 1);
        let foreign_rows = list(&cfg, &json!({"cwd":foreign.clone()})).unwrap();
        assert!(foreign_rows.as_array().unwrap().is_empty());
        assert_eq!(review(&cfg, &json!({"cwd":foreign,"pid":pid})), Err(Error::Untrusted));
    }

    #[test]
    fn legacy_project_belief_without_project_field_stays_in_its_checkout() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let own = temp.path().join("own");
        let foreign = temp.path().join("foreign");
        let slug = project_slug(&own);
        let pid = gate::stage(&cfg, &json!({
            "kind":"belief", "subject":format!("project:{slug}"),
            "claim":"isolated fixture", "cwd":own
        }), &auth()).unwrap();
        assert_eq!(list(&cfg, &json!({"cwd":foreign.clone()})).unwrap().as_array().unwrap().len(), 0);
        assert_eq!(review(&cfg, &json!({"cwd":foreign,"pid":pid})), Err(Error::Untrusted));
        assert_eq!(list(&cfg, &json!({"cwd":own})).unwrap().as_array().unwrap().len(), 1);
    }

    #[cfg(unix)]
    #[test]
    fn linked_pending_directory_never_yields_outside_review_proof() {
        use std::os::unix::fs::symlink;
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        fs::create_dir(&cfg.root).unwrap();
        let outside = temp.path().join("outside");
        fs::create_dir(&outside).unwrap();
        fs::write(
            outside.join("fixture.json"),
            br#"{"kind":"belief","scope":"user","claim":"outside fixture"}"#,
        )
        .unwrap();
        symlink(&outside, cfg.root.join("pending")).unwrap();
        assert_eq!(ids(&cfg), Err(Error::UnsafePath));
        assert!(matches!(
            review(&cfg, &json!({"cwd":temp.path(),"pid":"fixture"})),
            Err(Error::UnsafePath)
        ));
        assert!(outside.join("fixture.json").exists());
        assert!(!outside.join(".listed").exists());
    }

    use super::*;
    struct Applier;
    impl PendingApplier for Applier {
        fn apply_belief(&self, _: &Config, _: &Value, _: &Authority) -> Result<()> {
            Err(Error::Unavailable)
        }
        fn apply_sync(&self, _: &Config, _: &Value, _: &Authority) -> Result<()> {
            Err(Error::Unavailable)
        }
    }
    fn auth() -> Authority {
        Authority::HumanReview {
            agent: "fixture".into(),
            engine: "claude".into(),
        }
    }
    fn request(cfg: &Config, id: &str, cwd: &Path) -> Value {
        let snap = snapshot(&proposal_path(cfg, id).unwrap()).unwrap();
        json!({"cwd":cwd,"pid":id,"decision":"approve","expected":{"sha256":snap.sha256,"inode":snap.inode}})
    }
    #[test]
    fn enabled_skills_only_log_failure_preserves_landed_claim_without_retry() {
        for action in ["add", "update", "retire"] {
            let temp = tempfile::tempdir().unwrap();
            let mut cfg = Config::for_root(temp.path().join("lore"));
            cfg.sync.enabled = true;
            cfg.sync.classes = ["skills".to_owned()].into_iter().collect();
            let target = cfg.skills.join("fixture-skill/SKILL.md");
            if action != "add" {
                files::atomic_write(
                    &target,
                    skill_file_text("fixture-skill", None, "old owned fixture").as_bytes(),
                )
                .unwrap();
            }
            let id=gate::stage(&cfg,&json!({"kind":"skill","action":action,"name":"fixture-skill","body":"new owned fixture"}),&auth()).unwrap();
            let req = request(&cfg, &id, temp.path());
            let original = snapshot(&proposal_path(&cfg, &id).unwrap()).unwrap();
            fs::write(cfg.root.join("state.db"), b"owned corrupt fixture").unwrap();
            let reply = resolve(&cfg, &req, &auth(), &Applier).unwrap();
            assert_eq!(reply["status"], "refused");
            assert_eq!(reply["error"], "may_have_applied");
            assert_eq!(reply["applied"], true);
            assert!(!proposal_path(&cfg, &id).unwrap().exists());
            assert!(!cfg.root.join("pending/archive").exists());
            let claims = files::directory_names(&cfg.root.join("pending/.claimed"), 10).unwrap();
            assert_eq!(claims.len(), 1);
            let claimed = snapshot(&cfg.root.join("pending/.claimed").join(&claims[0])).unwrap();
            assert_eq!(claimed.sha256, original.sha256);
            assert_eq!(claimed.inode, original.inode);
            assert_eq!(
                fs::read(cfg.root.join("state.db")).unwrap(),
                b"owned corrupt fixture"
            );
            if action == "retire" {
                assert!(!target.exists());
                let retired = files::directory_names(&cfg.root.join("skills-retired"), 10).unwrap();
                assert_eq!(retired.len(), 1);
            } else {
                assert!(
                    String::from_utf8(files::read_regular(&target, 65536).unwrap())
                        .unwrap()
                        .contains("new owned fixture")
                );
            }
        }
    }
    #[test]
    fn exact_changed_inode_and_model_review_are_refused() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let id = gate::stage(
            &cfg,
            &json!({"kind":"memory","scope":"user","text":"fixture"}),
            &auth(),
        )
        .unwrap();
        let req = request(&cfg, &id, temp.path());
        let file = proposal_path(&cfg, &id).unwrap();
        let bytes = files::read_regular(&file, 65536).unwrap();
        files::atomic_write(&file, &bytes).unwrap();
        assert_eq!(resolve(&cfg, &req, &auth(), &Applier), Err(Error::Changed));
        assert!(file.exists());
        assert!(!cfg.root.join("USER.md").exists());
        let model = Authority::Model {
            agent: "m".into(),
            engine: "codex".into(),
            session_id: "s".into(),
        };
        assert_eq!(
            mark_full_review(&cfg, &request(&cfg, &id, temp.path()), &model),
            Err(Error::Untrusted)
        );
    }
    #[test]
    fn apply_failure_recovers_and_reject_archives_without_curated_write() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let id = gate::stage(&cfg, &json!({"kind":"belief","claim":"fixture"}), &auth()).unwrap();
        let mut req = request(&cfg, &id, temp.path());
        assert_eq!(
            resolve(&cfg, &req, &auth(), &Applier).unwrap()["applied"],
            false
        );
        assert!(proposal_path(&cfg, &id).unwrap().exists());
        req["decision"] = json!("reject");
        assert_eq!(
            resolve(&cfg, &req, &auth(), &Applier).unwrap()["status"],
            "rejected"
        );
        assert!(!proposal_path(&cfg, &id).unwrap().exists());
        assert_eq!(
            fs::read_dir(cfg.root.join("pending/archive"))
                .unwrap()
                .count(),
            1
        );
    }
    #[test]
    fn approved_memory_keeps_original_engine_and_creates_exact_archive() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let writer = Authority::Model {
            agent: "source".into(),
            engine: "codex".into(),
            session_id: "s".into(),
        };
        let id=gate::stage(&cfg,&json!({"kind":"memory","scope":"user","text":"approved fixture","source_engine":"claude"}),&writer).unwrap();
        assert!(!cfg.root.join("USER.md").exists());
        let req = request(&cfg, &id, temp.path());
        assert_eq!(
            resolve(&cfg, &req, &auth(), &Applier).unwrap()["status"],
            "approved"
        );
        assert_eq!(
            memory::read_entries(&cfg.root.join("USER.md")).unwrap(),
            ["approved fixture"]
        );
        let record = gate::provenance(&cfg, "memory", "user", "approved fixture");
        assert_eq!(record["via"], "approved");
        assert_eq!(record["source_engine"], "codex");
        assert!(!proposal_path(&cfg, &id).unwrap().exists());
        assert_eq!(
            fs::read_dir(cfg.root.join("pending/archive"))
                .unwrap()
                .count(),
            1
        );
    }
    #[test]
    fn sync_review_exempts_only_typed_mac_and_refuses_nested_secret_or_secret_key() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        for payload in [
            json!({"text":"safe fixture"}),
            json!({"api_key":"abcdefghijklmnop"}),
            json!({"token=abcdefghijklmnop":"safe"}),
        ] {
            let id=gate::stage(&cfg,&json!({"kind":"sync","op":{"op_id":"fixture-op","machine_id":"fixture-machine","machine_seq":1,"lamport":1,"project_key":null,"class":"memory","op":"add","mac":"a".repeat(64),"payload":payload}}),&auth()).unwrap();
            let req = json!({"cwd":temp.path(),"pid":id});
            let result = review(&cfg, &req);
            if payload.get("text").is_some() {
                let raw = result.unwrap()["raw"].as_str().unwrap().to_owned();
                assert!(raw.contains(&"a".repeat(64)));
            } else {
                assert_eq!(result, Err(Error::Untrusted));
            }
        }
        let id = gate::stage(
            &cfg,
            &json!({"kind":"sync","op":{"mac":"b".repeat(63),"payload":{"text":"safe"}}}),
            &auth(),
        )
        .unwrap();
        assert_eq!(
            review(&cfg, &json!({"cwd":temp.path(),"pid":id})),
            Err(Error::InvalidRequest)
        );
    }
    #[test]
    fn typed_sync_null_project_and_deterministic_uid_are_reviewable_without_exempting_payload_hashes(
    ) {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let op = json!({"op_id":"fixture-op","machine_id":"fixture-machine","machine_seq":1,"lamport":1,"class":"belief","op":"insert","project_key":null,"mac":"a".repeat(64),"payload":{"uid":"fixture-belief","subject":"user","claim":"owned fixture","confidence":0.8,"evidence":{"project_key":null}}});
        let path = cfg.root.join("pending/fixture.json");
        let item = json!({"kind":"sync","uid":crate::digest(b"fixture-op"),"op":op});
        files::atomic_write(&path, &serde_json::to_vec(&item).unwrap()).unwrap();
        let req = json!({"cwd":temp.path(),"pid":"fixture"});
        let result = review(&cfg, &req).unwrap();
        assert_eq!(result["complete"], true);
        assert_eq!(
            serde_json::from_str::<Value>(result["raw"].as_str().unwrap()).unwrap(),
            item
        );
        let mut unsafe_item = item;
        unsafe_item["op"]["payload"]["claim"] = json!("b".repeat(64));
        files::atomic_write(&path, &serde_json::to_vec(&unsafe_item).unwrap()).unwrap();
        assert_eq!(review(&cfg, &req), Err(Error::Untrusted));
        unsafe_item["op"]["payload"]["claim"] = json!("owned fixture");
        unsafe_item["op"]["payload"]["api_key"] = json!("not-real-fixture-credential");
        files::atomic_write(&path, &serde_json::to_vec(&unsafe_item).unwrap()).unwrap();
        assert_eq!(review(&cfg, &req), Err(Error::Untrusted));
    }
    #[test]
    fn indeterminate_adapter_failure_retains_private_claim_without_retryable_original() {
        struct Uncertain;
        impl PendingApplier for Uncertain {
            fn apply_belief(&self, _: &Config, _: &Value, _: &Authority) -> Result<()> {
                Err(Error::MayHaveApplied)
            }
            fn apply_sync(&self, _: &Config, _: &Value, _: &Authority) -> Result<()> {
                Err(Error::MayHaveApplied)
            }
        }
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let id = gate::stage(&cfg, &json!({"kind":"belief","claim":"fixture"}), &auth()).unwrap();
        let reply = resolve(&cfg, &request(&cfg, &id, temp.path()), &auth(), &Uncertain).unwrap();
        assert_eq!(reply["may_have_applied"], true);
        assert!(reply["applied"].is_null());
        assert!(!proposal_path(&cfg, &id).unwrap().exists());
        let claimed = fs::read_dir(cfg.root.join("pending/.claimed"))
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        assert_eq!(snapshot(&claimed).unwrap().item["claim"], "fixture");
    }
    #[test]
    fn recovery_does_not_overwrite_reused_proposal_id() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let source = proposal_path(&cfg, "fixture").unwrap();
        files::atomic_write(&source, b"new proposal").unwrap();
        let claimed = private_claim_dir(&cfg).unwrap().join("claimed.json");
        files::atomic_write(&claimed, b"old proposal").unwrap();
        assert_eq!(
            restore_claim(&source, &claimed, "fixture"),
            Err(Error::Changed)
        );
        assert_eq!(files::read_regular(&source, 100).unwrap(), b"new proposal");
        let recovered = fs::read_dir(source.parent().unwrap())
            .unwrap()
            .filter_map(|entry| entry.ok())
            .find(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("fixture-recovered-")
            })
            .unwrap();
        assert_eq!(
            files::read_regular(&recovered.path(), 100).unwrap(),
            b"old proposal"
        );
    }
    #[test]
    fn sync_archive_uses_exact_uid_and_never_takes_review_listing_lock() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let id = gate::stage(
            &cfg,
            &json!({"kind":"memory","scope":"user","text":"fixture"}),
            &auth(),
        )
        .unwrap();
        let snap = snapshot(&proposal_path(&cfg, &id).unwrap()).unwrap();
        let uid = snap.item["uid"].as_str().unwrap();
        let _listed =
            files::Locks::acquire(&cfg.root, &[cfg.root.join("pending/.listed")], cfg.timeout)
                .unwrap();
        assert_eq!(archive_uid(&cfg, uid, "rejected"), Ok(true));
        assert_eq!(archive_uid(&cfg, uid, "rejected"), Ok(false));
        assert!(!cfg.root.join("USER.md").exists());
    }
    #[test]
    fn enumeration_reports_overflow_including_nonproposal_entries() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let dir = cfg.root.join("pending");
        files::private_dir(&dir).unwrap();
        for index in 0..4097 {
            File::create(dir.join(format!("ignored-{index}.txt"))).unwrap();
        }
        assert_eq!(ids(&cfg), Err(Error::TooLarge));
    }
    #[test]
    fn skill_wrapper_is_idempotent_and_foreign_install_is_not_overwritten() {
        let text = skill_file_text("fixture", None, "body");
        assert_eq!(skill_file_text("fixture", None, &text), text);
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        files::atomic_write(&cfg.skills.join("fixture/SKILL.md"), b"human skill").unwrap();
        let item = json!({"kind":"skill","name":"fixture","action":"update","body":"new","cwd":temp.path()});
        assert_eq!(
            apply_item(&cfg, &item, &auth(), &Applier),
            Err(Error::Untrusted)
        );
        let item = json!({"kind":"skill","name":"../escape","body":"new","cwd":temp.path()});
        assert_eq!(
            apply_item(&cfg, &item, &auth(), &Applier),
            Err(Error::UnsafePath)
        );
    }
}
