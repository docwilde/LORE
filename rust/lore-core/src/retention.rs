//! Bounded retention of disposable diagnostic traces only. Curated state and
//! evaluation evidence are reference roots, never deletion targets.
//!
//! Writers publish pending references under pending::namespace_lock; object
//! writers use evidence_lock. Cleanup takes those locks in that order. A
//! publisher must verify_trace_references while holding its publishing lock.
use crate::{config::Config, diagnostics, files, gate::Authority, Error, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
#[cfg(unix)]
use std::os::{fd::AsRawFd, unix::fs::MetadataExt};
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::{OsStr, OsString},
    fs::{self, File},
    io::Read,
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

const ENTRY_CAP: usize = 4096;
const SCAN_BYTES: usize = 16 * 1024 * 1024;
const TRACE_SCAN_BYTES: usize = 64 * 1024 * 1024;
const DEPTH_CAP: usize = 12;
/// Exceeds the supervised review's 150s provider deadline and 1h outer job
/// deadline. Count pressure cannot collect captures still awaiting publication.
pub const MIN_TRACE_AGE_SECONDS: u64 = 24 * 60 * 60;
const ROOTS: &[&str] = &[
    "pending",
    "skill-evaluations",
    "reports",
    "review-reports",
    "review-proofs",
];

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub max_age_seconds: u64,
    pub max_count: usize,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            max_age_seconds: 30 * 86400,
            max_count: 1000,
        }
    }
}
impl Policy {
    fn validate(&self) -> Result<()> {
        if self.max_age_seconds == 0
            || self.max_age_seconds > 10 * 366 * 86400
            || self.max_count > ENTRY_CAP
        {
            return Err(Error::InvalidRequest);
        }
        Ok(())
    }
    pub fn configured(cfg: &Config) -> Result<Option<Self>> {
        Self::configured_with(|name| cfg.var(name))
    }
    fn configured_with(
        get: impl Fn(&str) -> std::result::Result<String, std::env::VarError>,
    ) -> Result<Option<Self>> {
        // Disabled means no scans, settings validation, locks, or store creation.
        match get("LORE_DIAGNOSTIC_RETENTION") {
            Err(std::env::VarError::NotPresent) => return Ok(None),
            Ok(v) if v == "0" || v.is_empty() => return Ok(None),
            Ok(v) if v == "1" => {}
            _ => return Err(Error::InvalidRequest),
        }
        let mut policy = Self::default();
        policy.max_age_seconds = match get("LORE_DIAGNOSTIC_RETENTION_MAX_AGE_SECONDS") {
            Ok(v) => v.parse().map_err(|_| Error::InvalidRequest)?,
            Err(std::env::VarError::NotPresent) => policy.max_age_seconds,
            Err(_) => return Err(Error::InvalidRequest),
        };
        policy.max_count = match get("LORE_DIAGNOSTIC_RETENTION_MAX_COUNT") {
            Ok(v) => v.parse().map_err(|_| Error::InvalidRequest)?,
            Err(std::env::VarError::NotPresent) => policy.max_count,
            Err(_) => return Err(Error::InvalidRequest),
        };
        policy.validate()?;
        Ok(Some(policy))
    }
}

pub(crate) fn evidence_lock(cfg: &Config) -> Result<files::Locks> {
    files::Locks::acquire(
        &cfg.root,
        &[cfg.root.join("diagnostic-traces/.evidence")],
        cfg.timeout,
    )
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub(crate) struct Stamp {
    dev: u64,
    ino: u64,
    size: u64,
    mtime: i64,
    mtime_ns: i64,
    ctime: i64,
    ctime_ns: i64,
}
#[cfg(unix)]
fn stamp(file: &File) -> Result<Stamp> {
    let m = file.metadata()?;
    if !m.is_file() || m.uid() != unsafe { libc::geteuid() } || m.nlink() != 1 {
        return Err(Error::UnsafePath);
    }
    Ok(Stamp {
        dev: m.dev(),
        ino: m.ino(),
        size: m.len(),
        mtime: m.mtime(),
        mtime_ns: m.mtime_nsec(),
        ctime: m.ctime(),
        ctime_ns: m.ctime_nsec(),
    })
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub(crate) struct Proof {
    stamp: Stamp,
    pub sha256: String,
}
pub(crate) struct Object {
    pub bytes: Vec<u8>,
    pub proof: Proof,
}

pub(crate) fn object_at(directory: &File, name: &OsStr, cap: usize) -> Result<Object> {
    let mut file = files::open_regular_at(directory, name, cap)?;
    let before = stamp(&file)?;
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(cap as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > cap {
        return Err(Error::TooLarge);
    }
    if before != stamp(&file)? || before != stamp(&files::open_regular_at(directory, name, cap)?)? {
        return Err(Error::Changed);
    }
    Ok(Object {
        proof: Proof {
            stamp: before,
            sha256: crate::digest(&bytes),
        },
        bytes,
    })
}

pub(crate) fn directory_identity(directory: &File) -> Result<Value> {
    let m = directory.metadata()?;
    if !m.is_dir() || m.uid() != unsafe { libc::geteuid() } {
        return Err(Error::UnsafePath);
    }
    // ctime is intentionally excluded: acquiring locks creates .locks.
    Ok(json!({"dev":m.dev(),"ino":m.ino()}))
}

pub(crate) fn names_at(directory: &File, cap: usize) -> Result<Vec<OsString>> {
    let mut stream = files::DirectoryStream::at(directory)?;
    let mut names = Vec::new();
    while let Some(name) = stream.next()? {
        if names.len() >= cap {
            return Err(Error::TooLarge);
        }
        names.push(name);
    }
    names.sort();
    Ok(names)
}

pub(crate) fn optional_directory(path: &Path) -> Result<Option<File>> {
    match fs::symlink_metadata(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
        Ok(_) => files::open_directory(path).map(Some),
    }
}
fn optional_child(directory: &File, name: &str) -> Result<Option<File>> {
    let name_c = std::ffi::CString::new(name).map_err(|_| Error::UnsafePath)?;
    let mut meta = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe {
        libc::fstatat(
            directory.as_raw_fd(),
            name_c.as_ptr(),
            meta.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } != 0
    {
        if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
            return Ok(None);
        }
        return Err(Error::Unavailable);
    }
    files::open_entry_at(directory, OsStr::new(name), true).map(Some)
}

// Collect all 64-hex strings, including hashes in prose, JSON keys and escaped
// JSON strings. Extra protection is preferable to guessing a reference schema.
fn hashes(text: &str, found: &mut BTreeSet<String>) {
    for chunk in text.split(|c: char| !c.is_ascii_hexdigit()) {
        if chunk.len() == 64 {
            found.insert(chunk.to_ascii_lowercase());
        }
    }
}
fn json_hashes(v: &Value, found: &mut BTreeSet<String>) {
    match v {
        Value::String(s) => hashes(s, found),
        Value::Array(a) => {
            for v in a {
                json_hashes(v, found);
            }
        }
        Value::Object(o) => {
            for (k, v) in o {
                hashes(k, found);
                json_hashes(v, found);
            }
        }
        _ => {}
    }
}

/// Check new named diagnostic references before publishing. Call while holding
/// pending's namespace lock or evidence_lock. This never creates anything.
pub fn verify_trace_references(cfg: &Config, value: &Value) -> Result<()> {
    let mut found = BTreeSet::new();
    named_references(value, &mut found)?;
    if !found.is_empty() {
        let dir = files::open_directory(&cfg.root.join("diagnostic-traces"))?;
        for hash in found {
            let object = object_at(
                &dir,
                OsStr::new(&format!("{hash}.json")),
                diagnostics::ARTIFACT_CAP,
            )?;
            diagnostics::validate_trace(&object.bytes, &hash)?;
        }
    }
    Ok(())
}
fn named_references(value: &Value, found: &mut BTreeSet<String>) -> Result<()> {
    match value {
        Value::Object(o) => {
            for (key, value) in o {
                if key == "diagnostic_trace_sha256" && !value.is_null() {
                    let hash = value
                        .as_str()
                        .filter(|s| diagnostics::valid_hash(s))
                        .ok_or(Error::InvalidRequest)?;
                    found.insert(hash.to_owned());
                } else {
                    named_references(value, found)?;
                }
            }
        }
        Value::Array(a) => {
            for v in a {
                named_references(v, found)?;
            }
        }
        _ => {}
    }
    Ok(())
}

struct References {
    entries: BTreeMap<String, Value>,
    hashes: BTreeSet<String>,
    count: usize,
    bytes: usize,
}
impl References {
    fn walk(&mut self, directory: &File, prefix: &str, depth: usize) -> Result<()> {
        if depth > DEPTH_CAP {
            return Err(Error::TooLarge);
        }
        let before = directory.metadata()?;
        let names = names_at(directory, ENTRY_CAP)?;
        self.entries
            .insert(prefix.into(), directory_identity(directory)?);
        for name in &names {
            self.count += 1;
            if self.count > ENTRY_CAP {
                return Err(Error::TooLarge);
            }
            let text = name.to_str().ok_or(Error::UnsafePath)?;
            let path = format!("{prefix}/{text}");
            // Try a directory first. Any other type must pass the regular-file
            // opener; symlinks, devices, hardlinks and unreadable entries refuse.
            if let Ok(child) = files::open_entry_at(directory, name, true) {
                self.walk(&child, &path, depth + 1)?;
            } else {
                let object = object_at(directory, name, diagnostics::ARTIFACT_CAP)?;
                self.bytes += object.bytes.len();
                if self.bytes > SCAN_BYTES {
                    return Err(Error::TooLarge);
                }
                let text_bytes =
                    std::str::from_utf8(&object.bytes).map_err(|_| Error::InvalidRequest)?;
                hashes(text_bytes, &mut self.hashes);
                if text.ends_with(".json") {
                    let value: Value =
                        serde_json::from_slice(&object.bytes).map_err(|_| Error::InvalidRequest)?;
                    if !value.is_object() && !value.is_array() {
                        return Err(Error::InvalidRequest);
                    }
                    json_hashes(&value, &mut self.hashes);
                    named_references(&value, &mut self.hashes)?;
                }
                self.entries.insert(
                    path,
                    serde_json::to_value(&object.proof).map_err(|_| Error::InvalidRequest)?,
                );
            }
        }
        let after = directory.metadata()?;
        if before.mtime() != after.mtime()
            || before.mtime_nsec() != after.mtime_nsec()
            || before.ctime() != after.ctime()
            || before.ctime_nsec() != after.ctime_nsec()
            || names != names_at(directory, ENTRY_CAP)?
        {
            return Err(Error::Changed);
        }
        Ok(())
    }
}

fn now() -> Result<u64> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| Error::Unavailable)?
        .as_secs())
}
fn policy(req: &Value) -> Result<Policy> {
    let p = match req.get("policy") {
        Some(p) => serde_json::from_value(p.clone()).map_err(|_| Error::InvalidRequest)?,
        None => Policy::default(),
    };
    p.validate()?;
    Ok(p)
}

fn inspect(cfg: &Config, policy: &Policy, at: u64) -> Result<Value> {
    let Some(root) = optional_directory(&cfg.root)? else {
        return Ok(
            json!({"schema":"lore.trace-retention-plan.v1","root":cfg.root,"root_identity":null,
            "trace_directory_identity":null,"policy":policy,"at_unix_seconds":at,"references":{},"traces":{},"remove":[]}),
        );
    };
    let root_identity = directory_identity(&root)?;
    let mut references = References {
        entries: BTreeMap::new(),
        hashes: BTreeSet::new(),
        count: 0,
        bytes: 0,
    };
    for name in ROOTS {
        if let Some(dir) = optional_child(&root, name)? {
            references.walk(&dir, name, 0)?;
        } else {
            references.entries.insert((*name).into(), Value::Null);
        }
    }
    let mut traces = BTreeMap::new();
    let mut unprotected = Vec::new();
    let trace_dir = optional_child(&root, "diagnostic-traces")?;
    let trace_directory_identity = trace_dir.as_ref().map(directory_identity).transpose()?;
    let mut trace_bytes = 0;
    if let Some(dir) = trace_dir {
        for name in names_at(&dir, ENTRY_CAP)? {
            let name = name.to_str().ok_or(Error::UnsafePath)?;
            let hash = name
                .strip_suffix(".json")
                .filter(|s| diagnostics::valid_hash(s))
                .ok_or(Error::InvalidRequest)?;
            let object = object_at(&dir, OsStr::new(name), diagnostics::ARTIFACT_CAP)?;
            trace_bytes += object.bytes.len();
            if trace_bytes > TRACE_SCAN_BYTES {
                return Err(Error::TooLarge);
            }
            let value = diagnostics::validate_trace(&object.bytes, hash)?;
            if !references.hashes.contains(hash) {
                let modified = u64::try_from(object.proof.stamp.mtime).ok();
                // An optional embedded capture time can only extend protection.
                let modified = modified
                    .map(|m| m.max(value["captured_at_unix_seconds"].as_u64().unwrap_or(0)));
                if let Some(modified) = modified {
                    unprotected.push((modified, object.proof.stamp.mtime_ns, hash.to_owned()));
                }
            }
            traces.insert(hash.to_owned(), object.proof);
        }
    }
    // Protected evidence is exempt from the cap. Keep the newest max_count
    // unprotected traces; remove older excess OR age-expired ones. Future-dated
    // entries are retained regardless of count to fail closed on clock skew.
    unprotected.sort_by(|a, b| b.cmp(a));
    let remove: Vec<_> = unprotected
        .iter()
        .enumerate()
        .filter_map(|(rank, (mtime, _, hash))| {
            let modified = *mtime;
            if modified > at {
                return None;
            }
            let age = at.saturating_sub(modified);
            (age >= MIN_TRACE_AGE_SECONDS
                && (age > policy.max_age_seconds || rank >= policy.max_count))
                .then_some(hash.clone())
        })
        .collect();
    if directory_identity(&files::open_directory(&cfg.root)?)? != root_identity {
        return Err(Error::Changed);
    }
    Ok(
        json!({"schema":"lore.trace-retention-plan.v1","root":cfg.root,"root_identity":root_identity,
        "trace_directory_identity":trace_directory_identity,"policy":policy,"at_unix_seconds":at,
        "references":references.entries,"traces":traces,"remove":remove}),
    )
}

pub(crate) fn envelope(plan: Value) -> Result<Value> {
    let bytes = serde_json::to_vec(&plan).map_err(|_| Error::InvalidRequest)?;
    if bytes.len() > crate::MAX_FRAME_BYTES - 1024 {
        return Err(Error::TooLarge);
    }
    Ok(json!({"dry_run":true,"plan_sha256":crate::digest(&bytes),"plan":plan}))
}
pub(crate) fn checked_plan(req: &Value, schema: &str) -> Result<Value> {
    let plan = req
        .get("plan")
        .filter(|p| p["schema"] == schema)
        .ok_or(Error::InvalidRequest)?;
    let bytes = serde_json::to_vec(plan).map_err(|_| Error::InvalidRequest)?;
    if bytes.len() > crate::MAX_FRAME_BYTES - 1024 {
        return Err(Error::TooLarge);
    }
    if req["plan_sha256"].as_str() != Some(crate::digest(&bytes).as_str()) {
        return Err(Error::Changed);
    }
    Ok(plan.clone())
}

/// No locks, temporary files, directories, or persisted plans are created.
pub fn plan(cfg: &Config, req: &Value) -> Result<Value> {
    let policy = policy(req)?;
    let at = now()?;
    let first = inspect(cfg, &policy, at)?;
    if first != inspect(cfg, &policy, at)? {
        return Err(Error::Changed);
    }
    envelope(first)
}

fn apply_plan(cfg: &Config, req: &Value) -> Result<Value> {
    let planned = checked_plan(req, "lore.trace-retention-plan.v1")?;
    let policy = policy(&planned)?;
    let at = planned["at_unix_seconds"]
        .as_u64()
        .ok_or(Error::InvalidRequest)?;
    if at > now()? {
        return Err(Error::Changed);
    }
    // Missing stores remain missing, including on apply of an empty dry-run.
    if optional_directory(&cfg.root)?.is_none() {
        if inspect(cfg, &policy, at)? != planned {
            return Err(Error::Changed);
        }
        return Ok(json!({"removed":[],"dry_run":false}));
    }
    let _pending = crate::pending::namespace_lock(cfg)?;
    let _evidence = evidence_lock(cfg)?;
    if inspect(cfg, &policy, at)? != planned {
        return Err(Error::Changed);
    }
    let remove = planned["remove"].as_array().ok_or(Error::InvalidRequest)?;
    if remove.is_empty() {
        return Ok(json!({"removed":[],"dry_run":false}));
    }
    let dir = files::open_directory(&cfg.root.join("diagnostic-traces"))?;
    if directory_identity(&dir)? != planned["trace_directory_identity"] {
        return Err(Error::Changed);
    }
    let mut removed = Vec::new();
    for hash in remove {
        let hash = hash
            .as_str()
            .filter(|h| diagnostics::valid_hash(h))
            .ok_or(Error::InvalidRequest)?;
        let proof: Proof = serde_json::from_value(planned["traces"][hash].clone())
            .map_err(|_| Error::InvalidRequest)?;
        let result = (|| {
            let path = cfg
                .root
                .join("diagnostic-traces")
                .join(format!("{hash}.json"));
            let _file = files::Locks::acquire(&cfg.root, &[path], cfg.timeout)?;
            remove_exact(
                &dir,
                OsStr::new(&format!("{hash}.json")),
                &proof,
                diagnostics::ARTIFACT_CAP,
            )
        })();
        if let Err(e) = result {
            return Err(if removed.is_empty() {
                e
            } else {
                Error::MayHaveApplied
            });
        }
        removed.push(hash);
    }
    Ok(json!({"removed":removed,"dry_run":false}))
}

pub fn apply(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    authority.require_review()?;
    apply_plan(cfg, req)
}

/// Host-only opt-in maintenance; never expose this as a model-callable tool.
pub fn automatic(cfg: &Config) -> Result<Value> {
    automatic_with_policy(cfg, Policy::configured(cfg)?)
}
fn automatic_with_policy(cfg: &Config, policy: Option<Policy>) -> Result<Value> {
    let Some(policy) = policy else {
        return Ok(json!({"status":"disabled","removed":[]}));
    };
    let planned = plan(cfg, &json!({"policy":policy}))?;
    apply_plan(cfg, &planned)
}

/// Claim into a new private directory before deletion. Replacements in the
/// source namespace are never unlinked: a post-claim mismatch is preserved for
/// recovery and reported as may_have_applied. Cooperating writers hold Locks;
/// private claims and descriptor-relative operations close parent-path races.
pub(crate) fn remove_exact(
    directory: &File,
    name: &OsStr,
    expected: &Proof,
    cap: usize,
) -> Result<()> {
    remove_exact_with(directory, name, expected, cap, || {})
}
fn remove_exact_with(
    directory: &File,
    name: &OsStr,
    expected: &Proof,
    cap: usize,
    before_claim: impl FnOnce(),
) -> Result<()> {
    if &object_at(directory, name, cap)?.proof != expected {
        return Err(Error::Changed);
    }
    let claim_name = format!(".retention-{}", uuid::Uuid::new_v4().simple());
    let c = std::ffi::CString::new(claim_name.as_bytes()).map_err(|_| Error::UnsafePath)?;
    if unsafe { libc::mkdirat(directory.as_raw_fd(), c.as_ptr(), 0o700) } != 0 {
        return Err(Error::Unavailable);
    }
    let claim = files::open_entry_at(directory, OsStr::new(&claim_name), true)?;
    before_claim();
    files::rename_at(directory, name, &claim, OsStr::new("object"), false)?;
    let moved = object_at(&claim, OsStr::new("object"), cap).map_err(|_| Error::MayHaveApplied)?;
    // rename changes ctime; inode, content, size and mtime must still match.
    let a = &expected.stamp;
    let b = &moved.proof.stamp;
    if expected.sha256 != moved.proof.sha256
        || a.dev != b.dev
        || a.ino != b.ino
        || a.size != b.size
        || a.mtime != b.mtime
        || a.mtime_ns != b.mtime_ns
    {
        return Err(Error::MayHaveApplied);
    }
    files::unlink_at(&claim, OsStr::new("object")).map_err(|_| Error::MayHaveApplied)?;
    if unsafe { libc::unlinkat(directory.as_raw_fd(), c.as_ptr(), libc::AT_REMOVEDIR) } != 0 {
        return Err(Error::MayHaveApplied);
    }
    directory.sync_all().map_err(|_| Error::MayHaveApplied)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs::{FileTimes, Permissions},
        os::unix::fs::{symlink, PermissionsExt},
        time::Duration,
    };

    fn fixture() -> (tempfile::TempDir, Config) {
        let temp = tempfile::tempdir().unwrap();
        let mut cfg = Config::for_root(temp.path().join("store"));
        cfg.sync.enabled = false;
        (temp, cfg)
    }
    fn owner() -> Authority {
        Authority::HumanReview {
            agent: "test".into(),
            engine: "codex".into(),
        }
    }
    fn write(path: &Path, bytes: &[u8]) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }
    fn trace(cfg: &Config, sid: &str, age: u64) -> String {
        let value = json!({"schema":"lore.diagnostic-trace.v1","session_id":sid,"engine":"codex",
            "source_sha256":"a".repeat(64),"source_bytes":1,"task_outcome":"unverified",
            "events":[],"complete":true,"source_lines":1,"truncated_events":0,
            "dropped_events":0,"unsupported_blocks":0});
        let bytes = serde_json::to_vec(&value).unwrap();
        let hash = crate::digest(&bytes);
        let path = cfg
            .root
            .join("diagnostic-traces")
            .join(format!("{hash}.json"));
        write(&path, &bytes);
        File::open(path)
            .unwrap()
            .set_times(FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(age)))
            .unwrap();
        hash
    }
    fn path(cfg: &Config, hash: &str) -> std::path::PathBuf {
        cfg.root
            .join("diagnostic-traces")
            .join(format!("{hash}.json"))
    }
    fn request() -> Value {
        json!({"policy":{"max_age_seconds":86400,"max_count":1000}})
    }
    // Includes modes, identity and times, excludes atime (reads may update it).
    fn tree(root: &Path) -> BTreeMap<String, Value> {
        fn walk(root: &Path, path: &Path, out: &mut BTreeMap<String, Value>) {
            let m = fs::symlink_metadata(path).unwrap();
            let bytes = if m.is_file() {
                fs::read(path).unwrap()
            } else {
                vec![]
            };
            out.insert(
                path.strip_prefix(root)
                    .unwrap()
                    .to_string_lossy()
                    .into_owned(),
                json!([
                    m.mode(),
                    m.ino(),
                    m.len(),
                    m.mtime(),
                    m.mtime_nsec(),
                    m.ctime(),
                    m.ctime_nsec(),
                    bytes
                ]),
            );
            if m.is_dir() {
                for entry in fs::read_dir(path).unwrap() {
                    walk(root, &entry.unwrap().path(), out);
                }
            }
        }
        let mut out = BTreeMap::new();
        if root.exists() {
            walk(root, root, &mut out);
        }
        out
    }

    #[test]
    fn dry_run_is_nonmutating_even_for_a_missing_store_and_apply_requires_review() {
        let (_temp, cfg) = fixture();
        let empty = plan(&cfg, &request()).unwrap();
        assert!(!cfg.root.exists());
        assert_eq!(apply(&cfg, &empty, &owner()).unwrap()["removed"], json!([]));
        assert!(!cfg.root.exists());
        let expired = trace(&cfg, "expired", 3 * 86400);
        let before = tree(&cfg.root);
        let planned = plan(&cfg, &request()).unwrap();
        assert_eq!(tree(&cfg.root), before);
        assert_eq!(planned["plan"]["remove"], json!([expired]));
        let model = Authority::Model {
            agent: "test".into(),
            engine: "codex".into(),
            session_id: "s".into(),
        };
        assert_eq!(apply(&cfg, &planned, &model), Err(Error::Untrusted));
        assert_eq!(tree(&cfg.root), before);
        assert_eq!(
            apply(&cfg, &planned, &owner()).unwrap()["removed"],
            json!([expired])
        );
        assert!(!path(&cfg, &expired).exists());
    }

    #[test]
    fn every_reference_root_protects_traces_and_curated_and_evaluation_evidence_survive() {
        let (_temp, cfg) = fixture();
        let mut protected = Vec::new();
        let roots = [
            "pending/p.json",
            "pending/archive/a.json",
            "pending/.claimed/c.json",
            "skill-evaluations/candidates/c.json",
            "skill-evaluations/traces/t.json",
            "skill-evaluations/reports/r.json",
            "reports/report.md",
            "review-proofs/proof.json",
        ];
        let mut evidence = BTreeMap::new();
        for (i, root) in roots.iter().enumerate() {
            let hash = trace(&cfg, &format!("protected-{i}"), 4 * 86400);
            let bytes = if root.ends_with(".json") {
                serde_json::to_vec(&json!({"nested":[{"diagnostic_trace_sha256":hash}]})).unwrap()
            } else {
                format!("Evidence trace: {hash}\n").into_bytes()
            };
            write(&cfg.root.join(root), &bytes);
            evidence.insert(root.to_string(), bytes);
            protected.push(hash);
        }
        // Negative controls are never deletion candidates, even when old.
        for (name, bytes) in [
            ("USER.md", b"curated user".as_slice()),
            ("beliefs.json", b"curated beliefs"),
            ("skill-evaluations/holdouts/h.json", b"{\"consumed\":true}"),
            ("pending/.review-listings.json", b"{}"),
            ("skills/kept/SKILL.md", b"skill proof"),
        ] {
            write(&cfg.root.join(name), bytes);
            evidence.insert(name.into(), bytes.to_vec());
        }
        let expired = trace(&cfg, "expired", 3 * 86400);
        let recent = trace(&cfg, "recent", 1);
        let planned = plan(&cfg, &request()).unwrap();
        assert_eq!(planned["plan"]["remove"], json!([expired]));
        assert_eq!(
            apply(&cfg, &planned, &owner()).unwrap()["removed"],
            json!([expired])
        );
        for hash in protected.into_iter().chain([recent]) {
            assert!(path(&cfg, &hash).exists());
        }
        for (name, bytes) in evidence {
            assert_eq!(fs::read(cfg.root.join(name)).unwrap(), bytes);
        }
    }

    #[test]
    fn decoded_escaped_hashes_and_hashes_inside_reports_are_protected() {
        let (_temp, cfg) = fixture();
        let hash = trace(&cfg, "escaped", 3 * 86400);
        let escaped: String = hash
            .chars()
            .map(|c| format!("\\u{:04x}", c as u32))
            .collect();
        write(
            &cfg.root.join("reports/escaped.json"),
            format!("{{\"proof\":\"{escaped}\"}}").as_bytes(),
        );
        assert_eq!(plan(&cfg, &request()).unwrap()["plan"]["remove"], json!([]));
    }

    #[test]
    fn age_and_count_have_a_mandatory_inflight_floor_and_protected_entries_are_exempt() {
        let (_temp, cfg) = fixture();
        let older = trace(&cfg, "older", 3 * 86400);
        let newest_old = trace(&cfg, "newest-old", 2 * 86400);
        let fresh = trace(&cfg, "fresh", 1);
        let planned = plan(
            &cfg,
            &json!({"policy":{"max_age_seconds":10 * 86400,"max_count":2}}),
        )
        .unwrap();
        assert_eq!(planned["plan"]["remove"], json!([older]));
        let result = automatic_with_policy(
            &cfg,
            Some(Policy {
                max_age_seconds: 1,
                max_count: 0,
            }),
        )
        .unwrap();
        assert_eq!(result["removed"].as_array().unwrap().len(), 2);
        assert!(path(&cfg, &fresh).exists());
        assert!(!path(&cfg, &newest_old).exists());
    }

    #[test]
    fn capture_time_and_clock_skew_can_only_extend_inflight_protection() {
        let (_temp, cfg) = fixture();
        let aged = trace(&cfg, "capture-time", 40 * 86400);
        let mut value: Value =
            serde_json::from_slice(&fs::read(path(&cfg, &aged)).unwrap()).unwrap();
        value["captured_at_unix_seconds"] = json!(now().unwrap());
        let bytes = serde_json::to_vec(&value).unwrap();
        let fresh = crate::digest(&bytes);
        write(&path(&cfg, &fresh), &bytes);
        fs::remove_file(path(&cfg, &aged)).unwrap();
        File::open(path(&cfg, &fresh))
            .unwrap()
            .set_times(
                FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(40 * 86400)),
            )
            .unwrap();
        let future = trace(&cfg, "future", 0);
        File::open(path(&cfg, &future))
            .unwrap()
            .set_times(
                FileTimes::new().set_modified(SystemTime::now() + Duration::from_secs(86400)),
            )
            .unwrap();
        let result = automatic_with_policy(
            &cfg,
            Some(Policy {
                max_age_seconds: 1,
                max_count: 0,
            }),
        )
        .unwrap();
        assert_eq!(result["removed"], json!([]));
        assert!(path(&cfg, &fresh).exists());
        assert!(path(&cfg, &future).exists());
    }

    #[test]
    fn disabled_automatic_policy_is_opt_in_and_invalid_enabled_policy_refuses() {
        use std::env::VarError::NotPresent;
        assert_eq!(Policy::configured_with(|_| Err(NotPresent)), Ok(None));
        assert_eq!(
            Policy::configured_with(|name| if name == "LORE_DIAGNOSTIC_RETENTION" {
                Ok("0".into())
            } else {
                Ok("invalid ignored while disabled".into())
            }),
            Ok(None)
        );
        assert_eq!(
            Policy::configured_with(|name| if name == "LORE_DIAGNOSTIC_RETENTION" {
                Ok("1".into())
            } else {
                Ok("bogus".into())
            }),
            Err(Error::InvalidRequest)
        );
        let (_temp, cfg) = fixture();
        assert_eq!(
            automatic_with_policy(&cfg, None).unwrap()["status"],
            "disabled"
        );
        assert!(!cfg.root.exists());
        let expired = trace(&cfg, "expired", 3 * 86400);
        let before = tree(&cfg.root);
        automatic_with_policy(&cfg, None).unwrap();
        assert_eq!(tree(&cfg.root), before);
        assert!(path(&cfg, &expired).exists());
    }

    #[test]
    fn changed_references_inventory_bytes_and_plan_hash_refuse_before_removal() {
        for change in 0..5 {
            let (_temp, cfg) = fixture();
            let expired = trace(&cfg, "expired", 3 * 86400);
            let mut planned = plan(&cfg, &request()).unwrap();
            match change {
                0 => write(
                    &cfg.root.join("pending/new.json"),
                    &serde_json::to_vec(&json!({"diagnostic_trace_sha256":expired})).unwrap(),
                ),
                1 => {
                    trace(&cfg, "new", 3 * 86400);
                }
                2 => {
                    let bytes = fs::read(path(&cfg, &expired)).unwrap();
                    fs::write(path(&cfg, &expired), bytes).unwrap();
                }
                3 => planned["plan_sha256"] = json!("0".repeat(64)),
                _ => planned["plan"]["remove"] = json!([]),
            }
            assert!(apply(&cfg, &planned, &owner()).is_err());
            assert!(path(&cfg, &expired).exists());
        }
    }

    #[test]
    fn incomplete_oversized_unreadable_or_nonregular_reference_scans_fail_closed() {
        for defect in 0..7 {
            let (temp, cfg) = fixture();
            let expired = trace(&cfg, "expired", 3 * 86400);
            let reference = cfg.root.join("pending/bad.json");
            write(&reference, b"{}");
            match defect {
                0 => fs::write(&reference, b"{truncated").unwrap(),
                1 => fs::write(&reference, vec![b'x'; diagnostics::ARTIFACT_CAP + 1]).unwrap(),
                2 => {
                    fs::remove_file(&reference).unwrap();
                    symlink(temp.path().join("missing"), &reference).unwrap();
                }
                3 => {
                    fs::hard_link(&reference, cfg.root.join("copy")).unwrap();
                }
                4 => {
                    fs::remove_file(&reference).unwrap();
                    let mut deep = cfg.root.join("pending");
                    for _ in 0..DEPTH_CAP + 2 {
                        deep.push("d");
                    }
                    write(&deep.join("proof.json"), b"{}");
                }
                5 => {
                    if unsafe { libc::geteuid() } == 0 {
                        continue;
                    }
                    fs::set_permissions(&reference, Permissions::from_mode(0)).unwrap();
                }
                _ => {
                    fs::remove_file(&reference).unwrap();
                    write(&cfg.root.join("reports/binary"), &[255]);
                }
            }
            assert!(plan(&cfg, &request()).is_err(), "defect {defect}");
            assert!(automatic_with_policy(&cfg, Some(Policy::default())).is_err());
            assert!(path(&cfg, &expired).exists());
        }
    }

    #[test]
    fn reference_entry_overflow_never_becomes_a_partial_scan() {
        let (_temp, cfg) = fixture();
        let expired = trace(&cfg, "expired", 40 * 86400);
        for i in 0..=ENTRY_CAP {
            write(&cfg.root.join(format!("pending/{i}.json")), b"{}");
        }
        assert_eq!(plan(&cfg, &request()), Err(Error::TooLarge));
        assert_eq!(
            automatic_with_policy(&cfg, Some(Policy::default())),
            Err(Error::TooLarge)
        );
        assert!(path(&cfg, &expired).exists());
    }

    #[test]
    fn malformed_or_tampered_trace_state_is_never_automatically_deleted() {
        for defect in 0..4 {
            let (temp, cfg) = fixture();
            let hash = trace(&cfg, "expired", 40 * 86400);
            match defect {
                0 => fs::write(path(&cfg, &hash), b"{}").unwrap(),
                1 => {
                    let bytes = b"{\"schema\":\"lore.evaluation-trace.v1\"}";
                    fs::remove_file(path(&cfg, &hash)).unwrap();
                    write(&path(&cfg, &crate::digest(bytes)), bytes);
                }
                2 => {
                    fs::hard_link(path(&cfg, &hash), temp.path().join("hardlink")).unwrap();
                }
                _ => {
                    fs::rename(path(&cfg, &hash), temp.path().join("outside")).unwrap();
                    symlink(temp.path().join("outside"), path(&cfg, &hash)).unwrap();
                }
            }
            assert!(automatic_with_policy(&cfg, Some(Policy::default())).is_err());
            assert_eq!(
                fs::read_dir(cfg.root.join("diagnostic-traces"))
                    .unwrap()
                    .count(),
                1
            );
        }
    }

    #[test]
    fn publication_checks_are_lazy_and_refuse_dangling_diagnostic_references() {
        let (_temp, cfg) = fixture();
        verify_trace_references(
            &cfg,
            &json!({"text":"memory","diagnostic_trace_sha256":null}),
        )
        .unwrap();
        assert!(!cfg.root.exists());
        let reference = json!({"nested":[{"diagnostic_trace_sha256":"b".repeat(64)}]});
        assert!(verify_trace_references(&cfg, &reference).is_err());
        assert!(!cfg.root.exists());
        let hash = trace(&cfg, "evidence", 3 * 86400);
        let value = json!({"diagnostic_trace_sha256":hash});
        verify_trace_references(&cfg, &value).unwrap();
        fs::remove_file(path(&cfg, &hash)).unwrap();
        assert!(diagnostics::put(&cfg, "skill-evaluations/reports", &value).is_err());
        assert!(!cfg.root.join("skill-evaluations/reports").exists());
    }

    #[test]
    fn pending_and_evidence_locks_prevent_cleanup_while_publishers_hold_them() {
        for pending in [true, false] {
            let (_temp, mut cfg) = fixture();
            cfg.timeout = Duration::from_millis(10);
            let expired = trace(&cfg, "expired", 3 * 86400);
            let planned = plan(&cfg, &request()).unwrap();
            let _guard = if pending {
                crate::pending::namespace_lock(&cfg).unwrap()
            } else {
                evidence_lock(&cfg).unwrap()
            };
            assert_eq!(apply(&cfg, &planned, &owner()), Err(Error::Timeout));
            assert!(path(&cfg, &expired).exists());
        }
    }

    #[test]
    fn a_source_replacement_during_claim_is_preserved_and_never_unlinked() {
        let (temp, cfg) = fixture();
        let hash = trace(&cfg, "expired", 3 * 86400);
        let directory = files::open_directory(&cfg.root.join("diagnostic-traces")).unwrap();
        let name = format!("{hash}.json");
        let expected = object_at(&directory, OsStr::new(&name), diagnostics::ARTIFACT_CAP)
            .unwrap()
            .proof;
        let saved = temp.path().join("saved");
        assert_eq!(
            remove_exact_with(
                &directory,
                OsStr::new(&name),
                &expected,
                diagnostics::ARTIFACT_CAP,
                || {
                    fs::rename(path(&cfg, &hash), &saved).unwrap();
                    fs::write(path(&cfg, &hash), b"unreviewed replacement").unwrap();
                }
            ),
            Err(Error::MayHaveApplied)
        );
        assert!(saved.exists());
        let names = names_at(&directory, 10).unwrap();
        assert_eq!(names.len(), 1);
        assert_eq!(
            fs::read(
                cfg.root
                    .join("diagnostic-traces")
                    .join(&names[0])
                    .join("object")
            )
            .unwrap(),
            b"unreviewed replacement"
        );
        assert!(plan(&cfg, &request()).is_err());
    }

    #[test]
    fn a_replaced_parent_cannot_redirect_deletion_outside_the_pinned_directory() {
        let (temp, cfg) = fixture();
        let hash = trace(&cfg, "expired", 3 * 86400);
        let active = cfg.root.join("diagnostic-traces");
        let pinned = files::open_directory(&active).unwrap();
        let name = format!("{hash}.json");
        let expected = object_at(&pinned, OsStr::new(&name), diagnostics::ARTIFACT_CAP)
            .unwrap()
            .proof;
        let outside = temp.path().join("outside");
        write(&outside.join(&name), b"outside negative control");
        let saved = temp.path().join("moved-traces");
        fs::rename(&active, &saved).unwrap();
        symlink(&outside, &active).unwrap();
        remove_exact(
            &pinned,
            OsStr::new(&name),
            &expected,
            diagnostics::ARTIFACT_CAP,
        )
        .unwrap();
        assert_eq!(
            fs::read(outside.join(&name)).unwrap(),
            b"outside negative control"
        );
        assert!(!saved.join(&name).exists());
    }
}
