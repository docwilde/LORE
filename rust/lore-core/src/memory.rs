//! Canonical bullet memory and exact, optimistic human review operations.
use crate::{
    config::{project_slug, valid_slug, Config},
    files,
    gate::{self, Authority},
    Error, Result,
};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
};
// Config caps count Unicode characters; four UTF-8 bytes per character.
const SOURCE_CAP: usize = 4 * 1024 * 1024;
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Scope {
    User,
    Project,
    Machine,
}
impl Scope {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "user" => Ok(Self::User),
            "project" => Ok(Self::Project),
            "machine" => Ok(Self::Machine),
            _ => Err(Error::InvalidRequest),
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::User => "user",
            Self::Project => "project",
            Self::Machine => "machine",
        }
    }
    pub fn cap(self, cfg: &Config) -> usize {
        match self {
            Self::User => cfg.user_cap,
            Self::Project => cfg.project_cap,
            Self::Machine => cfg.machine_cap,
        }
    }
    pub fn bucket(self, key: &str) -> String {
        match self {
            Self::User => "user".into(),
            Self::Project => format!("project:{key}"),
            Self::Machine => format!("machine:{key}"),
        }
    }
    pub fn path(self, cfg: &Config, key: &str) -> Result<PathBuf> {
        if self != Self::User && !valid_slug(key) {
            return Err(Error::UnsafePath);
        }
        Ok(match self {
            Self::User => cfg.root.join("USER.md"),
            Self::Project => cfg.root.join("projects").join(key).join("MEMORY.md"),
            Self::Machine => cfg.root.join("machines").join(format!("{key}.md")),
        })
    }
}
pub fn read_entries(path: &Path) -> Result<Vec<String>> {
    if !path.try_exists()? {
        return Ok(Vec::new());
    }
    let bytes = files::read_regular(path, SOURCE_CAP)?;
    let text = std::str::from_utf8(&bytes).map_err(|_| Error::Unavailable)?;
    Ok(text
        .lines()
        .filter_map(|line| {
            line.trim()
                .strip_prefix("- ")
                .map(|entry| entry.trim().to_owned())
        })
        .collect())
}
pub fn render_entries(entries: &[String]) -> String {
    entries.iter().map(|entry| format!("- {entry}\n")).collect()
}
pub fn match_entries(entries: &[String], needle: &str) -> Vec<usize> {
    let needle = needle.to_lowercase();
    entries
        .iter()
        .enumerate()
        .filter_map(|(index, entry)| entry.to_lowercase().contains(&needle).then_some(index))
        .collect()
}
pub fn write_entries(path: &Path, entries: &[String], cap: usize) -> Result<()> {
    // Persist a text-ordered set irrespective of local authorship/receipt order.
    // UTF-8 byte order matches Python's Unicode code point order. Keep reads
    // and review snapshots faithful to existing files until a successful write.
    let mut ordered = entries.to_vec();
    ordered.sort();
    let body = render_entries(&ordered);
    if body.len() > SOURCE_CAP {
        return Err(Error::TooLarge);
    }
    if body.chars().count() > cap {
        return Err(Error::OverCap);
    }
    files::atomic_write(path, body.as_bytes())
}
/// A file may have landed before directory fsync or ledger recording failed.
/// Preserve that distinction so callers never retry a partially applied review.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FileOutcome {
    Complete,
    Partial(Error),
    Uncertain(Error),
}
fn file_bytes(path: &Path) -> Result<Option<Vec<u8>>> {
    match fs::symlink_metadata(path) {
        Ok(_) => files::read_regular(path, SOURCE_CAP).map(Some),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(Error::Unavailable),
    }
}
pub(crate) fn observe_file_write(
    paths: &[PathBuf],
    apply: impl FnOnce() -> Result<()>,
) -> Result<FileOutcome> {
    let before = paths
        .iter()
        .map(|path| file_bytes(path))
        .collect::<Result<Vec<_>>>()?;
    match apply() {
        Ok(()) => Ok(FileOutcome::Complete),
        Err(error) => {
            let mut uncertain = false;
            for (path, previous) in paths.iter().zip(before) {
                match file_bytes(path) {
                    Ok(current) if current != previous => return Ok(FileOutcome::Partial(error)),
                    Err(_) => uncertain = true,
                    _ => {}
                }
            }
            if uncertain {
                Ok(FileOutcome::Uncertain(error))
            } else {
                Err(error)
            }
        }
    }
}
pub(crate) fn report_file_write(outcome: FileOutcome) -> Value {
    match outcome {
        FileOutcome::Complete => json!({"status":"applied"}),
        FileOutcome::Partial(error) => {
            json!({"status":"refused","applied":true,"error":error.code()})
        }
        FileOutcome::Uncertain(error) => {
            json!({"status":"refused","applied":null,"may_have_applied":true,"error":error.code()})
        }
    }
}
pub fn usage_line(entries: &[String], cap: usize) -> String {
    let chars = render_entries(entries).chars().count();
    let pct = if cap == 0 {
        0
    } else {
        (100.0 * chars as f64 / cap as f64).round_ties_even() as usize
    };
    format!("{chars}/{cap} chars ({pct}%)")
}
pub fn machine_slug(raw: &str) -> String {
    let mut value = String::new();
    let mut separator = false;
    for byte in raw.trim().bytes() {
        if byte.is_ascii_alphanumeric() {
            if separator && !value.is_empty() {
                value.push('-');
            }
            value.push((byte as char).to_ascii_lowercase());
            separator = false;
        } else {
            separator = true;
        }
    }
    value
}
pub fn this_machine() -> String {
    let configured = std::env::var("LORE_MACHINE_HOST").unwrap_or_default();
    let raw = if !configured.trim().is_empty() {
        configured
    } else {
        #[cfg(unix)]
        {
            let mut bytes = [0u8; 256];
            if unsafe { libc::gethostname(bytes.as_mut_ptr().cast(), bytes.len()) } == 0 {
                String::from_utf8_lossy(bytes.split(|byte| *byte == 0).next().unwrap_or(&[]))
                    .into_owned()
            } else {
                "unknown".into()
            }
        }
        #[cfg(not(unix))]
        {
            "unknown".into()
        }
    };
    let slug = machine_slug(&raw);
    if slug.is_empty() {
        "unknown".into()
    } else {
        slug
    }
}
pub fn known_machines(cfg: &Config) -> Vec<String> {
    let mut names = fs::read_dir(cfg.root.join("machines"))
        .into_iter()
        .flatten()
        .take(4096)
        .filter_map(|entry| {
            let entry = entry.ok()?;
            if !entry.file_type().ok()?.is_file() {
                return None;
            }
            let path = entry.path();
            (path.extension()? == "md")
                .then(|| path.file_stem()?.to_str().map(str::to_owned))
                .flatten()
        })
        .collect::<Vec<_>>();
    names.sort();
    names
}
pub fn resolve_machine(cfg: &Config, raw: Option<&str>) -> String {
    let raw = raw.unwrap_or("").trim();
    if raw.is_empty() {
        return this_machine();
    }
    let slug = machine_slug(raw);
    let names = known_machines(cfg);
    if names.contains(&slug) {
        return slug;
    }
    for matches in [
        names
            .iter()
            .filter(|name| name.ends_with(&slug))
            .collect::<Vec<_>>(),
        names.iter().filter(|name| name.contains(&slug)).collect(),
    ] {
        if matches.len() == 1 {
            return matches[0].clone();
        }
    }
    if slug.is_empty() {
        this_machine()
    } else {
        slug
    }
}
pub fn identity(cfg: &Config, req: &Value) -> Result<(Scope, String, PathBuf)> {
    let cwd = gate::cwd(req)?;
    let scope = Scope::parse(req["scope"].as_str().ok_or(Error::InvalidRequest)?)?;
    let key = if scope == Scope::Machine {
        resolve_machine(cfg, req["host"].as_str())
    } else {
        project_slug(cwd)
    };
    let path = scope.path(cfg, &key)?;
    Ok((scope, key, path))
}
fn exact_review(cfg: &Config, scope: Scope, key: &str, path: &Path) -> Result<Value> {
    let entries = read_entries(path)?;
    let body = render_entries(&entries);
    if entries.len() > 400
        || body.len() > 65536
        || entries
            .iter()
            .any(|entry| entry.chars().any(char::is_control))
    {
        return Err(Error::TooLarge);
    }
    for entry in &entries {
        if crate::scrub::scrub(entry)? != *entry {
            return Err(Error::Untrusted);
        }
    }
    Ok(
        json!({"scope":scope.name(),"key":if scope==Scope::User{"user"}else{key},"entries":entries,"chars":body.chars().count(),"cap_chars":scope.cap(cfg),"sha256":crate::digest(body.as_bytes())}),
    )
}
pub fn review(cfg: &Config, req: &Value) -> Result<Value> {
    let (scope, key, path) = identity(cfg, req)?;
    let _lock = files::Locks::acquire(&cfg.root, &[path.clone()], cfg.timeout)?;
    exact_review(cfg, scope, &key, &path)
}
pub fn entries(cfg: &Config, req: &Value) -> Result<Value> {
    let (scope, key, path) = identity(cfg, req)?;
    let _lock = files::Locks::acquire(&cfg.root, &[path.clone()], cfg.timeout)?;
    let entries = read_entries(&path)?;
    if entries.len() > 400 || render_entries(&entries).len() > 65536 {
        return Err(Error::TooLarge);
    }
    let labels = gate::source_labels(cfg, &scope.bucket(&key), &entries);
    let mut rows = Vec::new();
    let mut bytes = 0;
    for (entry, label) in entries.into_iter().zip(labels) {
        let text = crate::scrub::scrub(&entry)?;
        if text.len() > 16384 || text.chars().any(char::is_control) {
            return Err(Error::TooLarge);
        }
        bytes += text.len() + 3;
        if bytes > 65536 {
            return Err(Error::TooLarge);
        }
        let label = label.map(|label| crate::scrub::scrub(&label)).transpose()?;
        if label
            .as_ref()
            .is_some_and(|label| label.len() > 64 || label.chars().any(char::is_control))
        {
            return Err(Error::TooLarge);
        }
        rows.push(json!({"redacted":text!=entry,"text":text,"source":label}));
    }
    Ok(json!(rows))
}
pub fn usage(cfg: &Config, req: &Value) -> Result<Value> {
    let cwd = gate::cwd(req)?;
    let key = project_slug(cwd);
    let mut result = json!({});
    for scope in [Scope::User, Scope::Project] {
        let entries = read_entries(&scope.path(cfg, &key)?)?;
        let chars = render_entries(&entries).chars().count();
        result[format!("{}_chars", scope.name())] = json!(chars);
        result[format!("{}_cap_chars", scope.name())] = json!(scope.cap(cfg));
    }
    Ok(result)
}
fn append_memory_op(
    cfg: &Config,
    scope: Scope,
    key: &str,
    op: &str,
    payload: &Value,
) -> Result<()> {
    if scope != Scope::Machine {
        gate::append_file_op(
            cfg,
            "memory",
            op,
            (scope == Scope::Project).then_some(key),
            payload,
        )
        .map_err(|_| Error::MayHaveApplied)?;
    }
    Ok(())
}
pub fn mutate_locked(
    cfg: &Config,
    scope: Scope,
    key: &str,
    action: &str,
    needle: &str,
    text: &str,
    via: &str,
    origin: Option<&str>,
    source_engine: Option<&str>,
    authority: &Authority,
) -> Result<()> {
    if !authority.may_write() {
        return Err(Error::Untrusted);
    }
    // Reserved internal actions are used only after sync resolved a text key.
    // Public action/direct_action still accept add/replace/remove exclusively.
    let exact = matches!(action, "replace-exact" | "remove-exact");
    let action = match action {
        "replace-exact" => "replace",
        "remove-exact" => "remove",
        other => other,
    };
    let path = scope.path(cfg, key)?;
    let mut entries = read_entries(&path)?;
    let bucket = scope.bucket(key);
    let text = gate::one_line(&crate::scrub::scrub(text)?);
    let old = match action {
        "add" => {
            if text.is_empty() {
                return Err(Error::InvalidRequest);
            }
            if entries
                .iter()
                .any(|entry| entry.to_lowercase() == text.to_lowercase())
            {
                return Ok(());
            }
            entries.push(text.clone());
            None
        }
        "replace" | "remove" => {
            let hits = if exact {
                entries
                    .iter()
                    .enumerate()
                    .filter_map(|(index, entry)| (entry == needle).then_some(index))
                    .collect::<Vec<_>>()
            } else {
                match_entries(&entries, needle)
            };
            if hits.len() != 1 {
                return Err(Error::Changed);
            }
            let index = hits[0];
            let old = entries[index].clone();
            if action == "remove" {
                entries.remove(index);
            } else {
                entries[index] = text.clone();
            }
            Some(old)
        }
        _ => return Err(Error::InvalidRequest),
    };
    write_entries(&path, &entries, scope.cap(cfg))?;
    if let Some(old) = &old {
        gate::forget(cfg, "memory", &bucket, old)?;
    }
    if action != "remove" {
        gate::record(
            cfg,
            "memory",
            &bucket,
            &text,
            via,
            origin,
            authority,
            source_engine,
        )?;
    }
    let payload = match action {
        "add" => {
            json!({"text":text,"via":via,"writer":authority.writer(),"source_engine":gate::current_engine(source_engine.unwrap_or(authority.engine()))})
        }
        "replace" => {
            json!({"old_key":gate::entry_key("memory",&bucket,old.as_deref().unwrap()),"text":text,"via":via,"writer":authority.writer(),"source_engine":gate::current_engine(source_engine.unwrap_or(authority.engine()))})
        }
        _ => json!({"key":gate::entry_key("memory",&bucket,old.as_deref().unwrap())}),
    };
    append_memory_op(cfg, scope, key, action, &payload)
}
pub fn mutate(
    cfg: &Config,
    scope: Scope,
    key: &str,
    action: &str,
    needle: &str,
    text: &str,
    via: &str,
    origin: Option<&str>,
    source_engine: Option<&str>,
    authority: &Authority,
) -> Result<()> {
    let path = scope.path(cfg, key)?;
    let _lock = files::Locks::acquire(&cfg.root, &[path], cfg.timeout)?;
    mutate_locked(
        cfg,
        scope,
        key,
        action,
        needle,
        text,
        via,
        origin,
        source_engine,
        authority,
    )
}
pub fn action(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    let (scope, key, path) = identity(cfg, req)?;
    let action = req["action"].as_str().ok_or(Error::InvalidRequest)?;
    let text = req
        .get("text")
        .map_or(Some(""), Value::as_str)
        .ok_or(Error::InvalidRequest)?;
    let entry = req
        .get("entry")
        .map_or(Some(""), Value::as_str)
        .ok_or(Error::InvalidRequest)?;
    let expected = req["expected"]
        .as_object()
        .filter(|value| {
            value.len() == 2 && value.contains_key("key") && value.contains_key("sha256")
        })
        .ok_or(Error::InvalidRequest)?;
    if !expected["sha256"].as_str().is_some_and(|sha| {
        sha.len() == 64
            && sha
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }) {
        return Err(Error::InvalidRequest);
    }
    if !matches!(action, "add" | "replace" | "remove")
        || text.len() > 16384
        || entry.len() > 16384
        || text.chars().chain(entry.chars()).any(char::is_control)
        || action != "remove" && (text.trim().is_empty() || gate::one_line(text) != text)
        || crate::scrub::scrub(text)? != text
    {
        return Err(Error::InvalidRequest);
    }
    let _lock = files::Locks::acquire(&cfg.root, &[path.clone()], cfg.timeout)?;
    let reviewed = exact_review(cfg, scope, &key, &path)?;
    if expected.get("key") != reviewed.get("key")
        || expected.get("sha256") != reviewed.get("sha256")
    {
        return Err(Error::Changed);
    }
    if action != "add" {
        let entries = read_entries(&path)?;
        let hits = match_entries(&entries, entry);
        if hits.len() != 1 || entries[hits[0]] != entry {
            return Err(Error::Changed);
        }
    }
    if !authority.may_write() {
        let id = gate::stage(
            cfg,
            &json!({"kind":"memory","action":action,"scope":scope.name(),"project":key,"host":if scope==Scope::Machine{Some(key.as_str())}else{None},"match":entry,"text":text}),
            authority,
        )?;
        return Ok(json!({"status":"staged","pid":id}));
    }
    observe_file_write(&[path], || {
        mutate_locked(
            cfg, scope, &key, action, entry, text, "direct", None, None, authority,
        )
    })
    .map(report_file_write)
}
/// Native CLI/tool write path. Human optimistic UI editing uses `action`
/// instead; model/derived writes here stage and never bypass full review.
pub fn direct_action(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    let (scope, key, _) = identity(cfg, req)?;
    let action = req["action"].as_str().ok_or(Error::InvalidRequest)?;
    if action == "move" {
        return move_action(cfg, req, authority);
    }
    if !matches!(action, "add" | "replace" | "remove") {
        return Err(Error::InvalidRequest);
    }
    let text = req
        .get("text")
        .map_or(Some(""), Value::as_str)
        .ok_or(Error::InvalidRequest)?;
    let needle = req
        .get("match")
        .map_or(Some(""), Value::as_str)
        .ok_or(Error::InvalidRequest)?;
    let text = gate::one_line(&crate::scrub::scrub(text)?);
    if action == "add" && text.is_empty() {
        return Err(Error::InvalidRequest);
    }
    if !authority.may_write() {
        let id = gate::stage(
            cfg,
            &json!({"kind":"memory","action":action,"scope":scope.name(),"project":key,"host":if scope==Scope::Machine{Some(key.as_str())}else{None},"match":needle,"text":text}),
            authority,
        )?;
        return Ok(json!({"status":"staged","pid":id}));
    }
    let path = scope.path(cfg, &key)?;
    let _lock = files::Locks::acquire(&cfg.root, &[path.clone()], cfg.timeout)?;
    observe_file_write(&[path], || {
        mutate_locked(
            cfg, scope, &key, action, needle, &text, "direct", None, None, authority,
        )
    })
    .map(report_file_write)
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MoveOutcome {
    Complete,
    DestinationOnly(Error),
    Uncertain(Error),
}
pub fn move_entry(
    cfg: &Config,
    scope: Scope,
    from: &str,
    needle: &str,
    to_scope: Scope,
    to: &str,
    authority: &Authority,
) -> Result<MoveOutcome> {
    move_with_writer(
        cfg,
        scope,
        from,
        needle,
        to_scope,
        to,
        authority,
        write_entries,
    )
}
fn move_with_writer(
    cfg: &Config,
    scope: Scope,
    from: &str,
    needle: &str,
    to_scope: Scope,
    to: &str,
    authority: &Authority,
    write: impl Fn(&Path, &[String], usize) -> Result<()>,
) -> Result<MoveOutcome> {
    if !authority.may_write() {
        return Err(Error::Untrusted);
    }
    if scope == to_scope && (scope == Scope::User || from == to) {
        return Err(Error::InvalidRequest);
    }
    let source = scope.path(cfg, from)?;
    let destination = to_scope.path(cfg, to)?;
    let _lock = files::Locks::acquire(
        &cfg.root,
        &[source.clone(), destination.clone()],
        cfg.timeout,
    )?;
    let mut src = read_entries(&source)?;
    let hits = match_entries(&src, needle);
    if hits.len() != 1 {
        return Err(Error::Changed);
    }
    let text = src[hits[0]].clone();
    let mut dst = read_entries(&destination)?;
    let src_bucket = scope.bucket(from);
    let provenance = gate::provenance(cfg, "memory", &src_bucket, &text);
    if !dst
        .iter()
        .any(|entry| entry.to_lowercase() == text.to_lowercase())
    {
        dst.push(text.clone());
        match observe_file_write(&[destination.clone()], || {
            write(&destination, &dst, to_scope.cap(cfg))
        })? {
            FileOutcome::Complete => {}
            FileOutcome::Partial(error) => return Ok(MoveOutcome::DestinationOnly(error)),
            FileOutcome::Uncertain(error) => return Ok(MoveOutcome::Uncertain(error)),
        };
        if let Err(error) = gate::record_preserved(
            cfg,
            "memory",
            &to_scope.bucket(to),
            &text,
            &provenance,
            &format!(
                "moved from {}",
                if scope == Scope::User { "user" } else { from }
            ),
        ) {
            return Ok(MoveOutcome::DestinationOnly(error));
        }
    }
    src.remove(hits[0]);
    if let Err(error) = write(&source, &src, scope.cap(cfg)) {
        return Ok(MoveOutcome::DestinationOnly(error));
    }
    if let Err(error) = gate::forget(cfg, "memory", &src_bucket, &text) {
        return Ok(MoveOutcome::DestinationOnly(error));
    }
    if to_scope == Scope::Machine && scope != Scope::Machine {
        if let Err(error) = append_memory_op(
            cfg,
            scope,
            from,
            "remove",
            &json!({"key":gate::entry_key("memory",&src_bucket,&text)}),
        ) {
            return Ok(MoveOutcome::DestinationOnly(error));
        }
    }
    Ok(MoveOutcome::Complete)
}
pub fn move_action(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    let (scope, key, _) = identity(cfg, req)?;
    let to_scope = Scope::parse(req["to_scope"].as_str().unwrap_or(scope.name()))?;
    let to = req["to"].as_str().ok_or(Error::InvalidRequest)?;
    let needle = req["match"].as_str().ok_or(Error::InvalidRequest)?;
    if !authority.may_write() {
        let id = gate::stage(
            cfg,
            &json!({"kind":"memory","action":"move","scope":scope.name(),"project":key,"match":needle,"to_scope":to_scope.name(),"to":to,"host":if scope==Scope::Machine{Some(key.as_str())}else{None}}),
            authority,
        )?;
        return Ok(json!({"status":"staged","pid":id}));
    }
    match move_entry(cfg, scope, &key, needle, to_scope, to, authority)? {
        MoveOutcome::Complete => Ok(json!({"status":"applied"})),
        MoveOutcome::DestinationOnly(error) => {
            Ok(json!({"status":"refused","applied":true,"error":error.code()}))
        }
        MoveOutcome::Uncertain(error) => Ok(
            json!({"status":"refused","applied":null,"may_have_applied":true,"error":error.code()}),
        ),
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn landed_memory_with_failed_enabled_log_reports_partial_effect() {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("lore"));
        cfg.sync.enabled = true;
        cfg.sync.classes = ["memory".into()].into_iter().collect();
        files::private_dir(&cfg.root).unwrap();
        fs::write(cfg.root.join("state.db"), b"owned corrupt fixture").unwrap();
        let reviewed = review(&cfg, &json!({"cwd":temp.path(),"scope":"user"})).unwrap();
        let result=action(&cfg,&json!({"cwd":temp.path(),"scope":"user","action":"add","text":"landed fixture","expected":{"key":"user","sha256":reviewed["sha256"]}}),&auth()).unwrap();
        assert_eq!(result["status"], "refused");
        assert_eq!(result["applied"], true);
        assert_eq!(result["error"], "may_have_applied");
        assert_eq!(
            read_entries(&cfg.root.join("USER.md")).unwrap(),
            ["landed fixture"]
        );
        assert_eq!(
            fs::read(cfg.root.join("state.db")).unwrap(),
            b"owned corrupt fixture"
        );
    }

    use super::*;
    fn auth() -> Authority {
        Authority::HumanReview {
            agent: "fixture".into(),
            engine: "codex".into(),
        }
    }
    #[test]
    fn internal_exact_mutation_selects_only_keyed_text_and_reorders_bytes() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let path = cfg.root.join("USER.md");
        files::atomic_write(&path, b"- shared fact extended\n- shared fact\n").unwrap();
        mutate(
            &cfg,
            Scope::User,
            "",
            "replace-exact",
            "shared fact",
            "Alpha replacement",
            "direct",
            None,
            Some("codex"),
            &auth(),
        )
        .unwrap();
        assert_eq!(
            fs::read(&path).unwrap(),
            b"- Alpha replacement\n- shared fact extended\n"
        );
        mutate(
            &cfg,
            Scope::User,
            "",
            "add",
            "",
            "shared fact",
            "direct",
            None,
            Some("codex"),
            &auth(),
        )
        .unwrap();
        mutate(
            &cfg,
            Scope::User,
            "",
            "remove-exact",
            "shared fact",
            "",
            "direct",
            None,
            None,
            &auth(),
        )
        .unwrap();
        assert_eq!(
            fs::read(&path).unwrap(),
            b"- Alpha replacement\n- shared fact extended\n"
        );
    }

    #[test]
    fn unicode_cap_duplicate_and_exact_snapshot_are_canonical() {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("lore"));
        cfg.user_cap = 5;
        mutate(
            &cfg,
            Scope::User,
            "ignored",
            "add",
            "",
            "éé",
            "direct",
            None,
            None,
            &auth(),
        )
        .unwrap();
        mutate(
            &cfg,
            Scope::User,
            "ignored",
            "add",
            "",
            "ÉÉ",
            "direct",
            None,
            None,
            &auth(),
        )
        .unwrap();
        assert_eq!(read_entries(&cfg.root.join("USER.md")).unwrap(), ["éé"]);
        assert_eq!(
            mutate(
                &cfg,
                Scope::User,
                "ignored",
                "add",
                "",
                "x",
                "direct",
                None,
                None,
                &auth()
            ),
            Err(Error::OverCap)
        );
        let req = json!({"cwd":temp.path(),"scope":"user"});
        let first = review(&cfg, &req).unwrap();
        assert_eq!(first["chars"], 5);
        let mut action_req = req;
        action_req["action"] = json!("remove");
        action_req["entry"] = json!("éé");
        action_req["expected"] = json!({"key":"user","sha256":"0".repeat(64)});
        assert_eq!(action(&cfg, &action_req, &auth()), Err(Error::Changed));
        assert!(cfg.root.join("USER.md").exists());
    }
    #[test]
    fn destination_cap_failure_preserves_source_and_unknown_provenance() {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("lore"));
        cfg.project_cap = 5;
        write_entries(
            &Scope::Project.path(&cfg, "a").unwrap(),
            &["first".into()],
            100,
        )
        .unwrap();
        assert_eq!(
            move_entry(
                &cfg,
                Scope::Project,
                "a",
                "first",
                Scope::Project,
                "b",
                &auth()
            ),
            Err(Error::OverCap)
        );
        assert_eq!(
            read_entries(&Scope::Project.path(&cfg, "a").unwrap()).unwrap(),
            ["first"]
        );
        cfg.project_cap = 100;
        move_entry(
            &cfg,
            Scope::Project,
            "a",
            "first",
            Scope::Project,
            "b",
            &auth(),
        )
        .unwrap();
        assert_eq!(
            gate::source_labels(&cfg, "project:b", &["first".into()]),
            [None]
        );
        assert!(Scope::Project.path(&cfg, "../escape").is_err());
    }
    #[test]
    fn failed_source_removal_reports_landed_destination_without_fabricating_success() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        let source = Scope::Project.path(&cfg, "a").unwrap();
        write_entries(&source, &["fact".into()], 100).unwrap();
        let result = move_with_writer(
            &cfg,
            Scope::Project,
            "a",
            "fact",
            Scope::Project,
            "b",
            &auth(),
            |path, entries, cap| {
                if path == source {
                    Err(Error::Unavailable)
                } else {
                    write_entries(path, entries, cap)
                }
            },
        )
        .unwrap();
        assert_eq!(result, MoveOutcome::DestinationOnly(Error::Unavailable));
        assert_eq!(read_entries(&source).unwrap(), ["fact"]);
        assert_eq!(
            read_entries(&Scope::Project.path(&cfg, "b").unwrap()).unwrap(),
            ["fact"]
        );
    }
    #[test]
    fn landed_write_then_failure_is_reported_without_claiming_success() {
        let temp = tempfile::tempdir().unwrap();
        let file = temp.path().join("state");
        let outcome = observe_file_write(&[file.clone()], || {
            files::atomic_write(&file, b"landed")?;
            Err(Error::Unavailable)
        })
        .unwrap();
        assert_eq!(outcome, FileOutcome::Partial(Error::Unavailable));
        assert_eq!(report_file_write(outcome)["applied"], true);
        assert_eq!(
            observe_file_write(&[file], || Err(Error::OverCap)),
            Err(Error::OverCap)
        );
    }
    #[test]
    fn model_and_derived_cli_writes_stage_without_touching_curated_files() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        for authority in [
            Authority::Model {
                agent: "m".into(),
                engine: "codex".into(),
                session_id: "s".into(),
            },
            Authority::Derived {
                agent: "r".into(),
                engine: "claude".into(),
            },
        ] {
            let req = json!({"cwd":temp.path(),"scope":"user","action":"add","text":"fixture"});
            assert_eq!(
                direct_action(&cfg, &req, &authority).unwrap()["status"],
                "staged"
            );
        }
        assert!(!cfg.root.join("USER.md").exists());
    }
    #[test]
    fn exact_entry_cannot_ambiguously_match_other_fact() {
        let temp = tempfile::tempdir().unwrap();
        let cfg = Config::for_root(temp.path().join("lore"));
        write_entries(
            &cfg.root.join("USER.md"),
            &["fact".into(), "fact plus".into()],
            100,
        )
        .unwrap();
        let req = json!({"cwd":temp.path(),"scope":"user"});
        let review = review(&cfg, &req).unwrap();
        let action_req = json!({"cwd":temp.path(),"scope":"user","action":"remove","entry":"fact","expected":{"key":review["key"],"sha256":review["sha256"]}});
        assert_eq!(action(&cfg, &action_req, &auth()), Err(Error::Changed));
    }
}
