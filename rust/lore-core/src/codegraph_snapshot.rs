//! Owner-reviewed, worktree-scoped storage for DOXA's read-only syntax answers.
//! The stored graph is producer data, never a semantic binding or memory fact.
use crate::{config::{project_slug, Config}, files, gate::{self, Authority}, Error, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{io::Read, path::{Component, Path, PathBuf}};
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;

const MAX_GRAPH_BYTES: usize = 96 * 1024;
const MAX_RECORD_BYTES: usize = 128 * 1024;
const MAX_SOURCE_BYTES: usize = 1024 * 1024;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    schema_version: u8,
    project_key: String,
    worktree_root: String,
    worktree_identity: String,
    query: String,
    path: String,
    revision: u64,
    source_sha256: String,
    graph_sha256: String,
    graph: Value,
}

struct Scope {
    root: PathBuf,
    project_key: String,
    worktree_identity: String,
}

fn scope(req: &Value) -> Result<Scope> {
    let root = gate::cwd(req)?;
    if root != root.canonicalize().map_err(|_| Error::Changed)? {
        return Err(Error::Changed);
    }
    let directory = files::open_directory(root)?;
    let dot_git = root.join(".git");
    let marker = match files::open_directory(&dot_git) {
        // The directory's ctime changes during ordinary Git operations. Its
        // config file is the stable checkout marker; replacing it invalidates
        // the snapshot conservatively.
        Ok(_) => files::open_regular(&dot_git.join("config"), 64 * 1024)?,
        Err(_) => {
            let mut file = files::open_regular(&dot_git, 4096)?;
            let mut content = String::new();
            file.read_to_string(&mut content).map_err(|_| Error::InvalidRequest)?;
            let target = content.trim().strip_prefix("gitdir: ").ok_or(Error::Changed)?;
            let target = Path::new(target);
            if !target.is_absolute() || !target.components().all(|part|
                matches!(part, Component::RootDir | Component::Normal(_))) {
                return Err(Error::Changed);
            }
            files::open_directory(&target)?;
            file
        }
    };
    #[cfg(unix)]
    let identity = {
        let root_meta = directory.metadata()?;
        let git_meta = marker.metadata()?;
        crate::digest(format!("{}:{}:{}:{}:{}:{}:{}:{}", root.display(),
            root_meta.dev(), root_meta.ino(), git_meta.dev(), git_meta.ino(),
            git_meta.ctime(), git_meta.ctime_nsec(), git_meta.len()).as_bytes())
    };
    #[cfg(not(unix))]
    let identity = return Err(Error::Unsupported);
    Ok(Scope { root: root.to_path_buf(), project_key: project_slug(root), worktree_identity: identity })
}

fn query(req: &Value) -> Result<(&str, &str)> {
    let query = req["query"].as_str()
        .filter(|kind| matches!(*kind, "file" | "imports" | "calls" | "modules"))
        .ok_or(Error::InvalidRequest)?;
    let path = req["path"].as_str().ok_or(Error::InvalidRequest)?;
    if path.is_empty() || path.len() > 4096 || !path.ends_with(".rs")
        || !Path::new(path).components().all(|part| matches!(part, Component::Normal(_)))
        || path.chars().any(char::is_control) {
        return Err(Error::InvalidRequest);
    }
    Ok((query, path))
}

fn location(cfg: &Config, scope: &Scope, query: &str, path: &str) -> PathBuf {
    let key = crate::digest(format!("{}\0{}\0{}\0{}", scope.root.display(),
        scope.worktree_identity, query, path).as_bytes());
    cfg.root.join("codegraph-v1").join(format!("{key}.json"))
}

fn source_hash(root: &Path, path: &str) -> Result<String> {
    let path = root.join(path);
    // open_regular walks every component from / with O_NOFOLLOW, so a
    // repo-relative path cannot traverse an intermediate directory link.
    let mut file = files::open_regular(&path, MAX_SOURCE_BYTES)?;
    let before = file.metadata()?;
    let mut bytes = Vec::new();
    (&mut file).take(MAX_SOURCE_BYTES as u64 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > MAX_SOURCE_BYTES { return Err(Error::TooLarge); }
    let after = file.metadata()?;
    #[cfg(unix)]
    if before.dev() != after.dev() || before.ino() != after.ino()
        || before.len() != after.len() || before.mtime_nsec() != after.mtime_nsec()
        || before.mtime() != after.mtime() { return Err(Error::Changed); }
    Ok(crate::digest(&bytes))
}

fn hex64(value: &str) -> bool { value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()) }

fn graph_bytes(graph: &Value) -> Result<Vec<u8>> {
    let bytes = serde_json::to_vec(graph).map_err(|_| Error::InvalidRequest)?;
    if bytes.len() > MAX_GRAPH_BYTES { return Err(Error::TooLarge); }
    Ok(bytes)
}

fn graph_basis(graph: &Value, scope: &Scope, query: &str, path: &str) -> Result<String> {
    let object = graph.as_object().ok_or(Error::InvalidRequest)?;
    if object.get("scope").and_then(Value::as_str) != scope.root.to_str()
        || graph["query"] != query || graph["value"] != path || graph["status"] != "ok"
        || graph["requested_source_read_unix_ms"].as_u64().is_none()
        || !graph["coverage"].is_object() || !graph["rows"].is_array()
        || !graph["edges"].is_array() || !graph["module_edges"].is_array() {
        return Err(Error::Changed);
    }
    let sha = graph["requested_source_sha256"].as_str()
        .filter(|sha| hex64(sha)).ok_or(Error::InvalidRequest)?;
    Ok(sha.to_ascii_lowercase())
}

fn load(path: &Path) -> Result<Option<Record>> {
    if !path.try_exists()? { return Ok(None); }
    let bytes = files::read_regular(path, MAX_RECORD_BYTES)?;
    let record: Record = serde_json::from_slice(&bytes).map_err(|_| Error::Changed)?;
    if record.schema_version != 1 || record.revision == 0 || !hex64(&record.source_sha256)
        || !hex64(&record.graph_sha256)
        || crate::digest(&graph_bytes(&record.graph)?) != record.graph_sha256 {
        return Err(Error::Changed);
    }
    Ok(Some(record))
}

fn verify(record: &Record, scope: &Scope, query: &str, path: &str, check_source: bool) -> Result<()> {
    if record.project_key != scope.project_key || record.worktree_root != scope.root.to_string_lossy()
        || record.worktree_identity != scope.worktree_identity
        || record.query != query || record.path != path
        || graph_basis(&record.graph, scope, query, path)? != record.source_sha256 {
        return Err(Error::Changed);
    }
    if check_source && source_hash(&scope.root, path)? != record.source_sha256 {
        return Err(Error::Changed);
    }
    Ok(())
}

/// Read one current snapshot. Missing is explicit; stale bytes and ambiguous
/// scope fail without returning graph data. This never creates store state.
pub fn read(cfg: &Config, req: &Value) -> Result<Value> {
    let scope = scope(req)?;
    let (query, path) = query(req)?;
    let Some(record) = load(&location(cfg, &scope, query, path))? else {
        return Ok(json!({"status":"missing"}));
    };
    verify(&record, &scope, query, path, true)?;
    Ok(json!({"status":"current","schema_version":1,"project_key":record.project_key,
        "worktree_root":record.worktree_root,"query":query,"path":path,"revision":record.revision,
        "source_sha256":record.source_sha256,"graph_sha256":record.graph_sha256,
        "freshness":"requested_source_verified_only","binding":"unknown","graph":record.graph}))
}

/// Only a trusted, explicit human review can persist one exact current answer.
/// Revision zero creates; later writes compare and swap under the file lock.
pub fn store(cfg: &Config, req: &Value, authority: &Authority) -> Result<Value> {
    authority.require_review()?;
    let scope = scope(req)?;
    let (query, path) = query(req)?;
    let expected = req["expected_revision"].as_u64().ok_or(Error::InvalidRequest)?;
    let snapshot = req["snapshot"].as_object().ok_or(Error::InvalidRequest)?;
    // DOXA's optional query_sha256 covers its original struct serialization.
    // JSON parsing loses that field order, so LORE discards the producer claim
    // and computes/verifies its own graph_sha256 over the stored graph value.
    if snapshot.keys().any(|key| !matches!(key.as_str(),
        "schema_version" | "storage" | "graph_binding" | "graph" |
        "curated_purpose" | "query_sha256"))
        || snapshot.get("schema_version") != Some(&json!(1))
        || snapshot.get("storage") != Some(&json!("export_only_not_persisted"))
        || snapshot.get("graph_binding") != Some(&json!("unknown"))
        || snapshot.get("curated_purpose").and_then(|purpose| purpose["project_key"].as_str())
            != Some(scope.project_key.as_str()) {
        return Err(Error::InvalidRequest);
    }
    let graph = snapshot.get("graph").ok_or(Error::InvalidRequest)?;
    let graph_sha256 = crate::digest(&graph_bytes(graph)?);
    let source_sha256 = graph_basis(graph, &scope, query, path)?;
    if source_hash(&scope.root, path)? != source_sha256 { return Err(Error::Changed); }
    let file = location(cfg, &scope, query, path);
    let _lock = files::Locks::acquire(&cfg.root, &[file.clone()], cfg.timeout)?;
    let current = load(&file)?;
    let revision = match current {
        Some(record) => {
            verify(&record, &scope, query, path, false)?;
            if record.revision != expected { return Err(Error::Changed); }
            expected.checked_add(1).ok_or(Error::OverCap)?
        }
        None if expected == 0 => 1,
        None => return Err(Error::Changed),
    };
    // Recheck after acquiring the lock; source edits never publish stale data.
    if source_hash(&scope.root, path)? != source_sha256 { return Err(Error::Changed); }
    let record = Record { schema_version: 1, project_key: scope.project_key,
        worktree_root: scope.root.to_string_lossy().into_owned(),
        worktree_identity: scope.worktree_identity, query: query.into(), path: path.into(),
        revision, source_sha256: source_sha256.clone(), graph_sha256: graph_sha256.clone(),
        graph: graph.clone() };
    let bytes = serde_json::to_vec(&record).map_err(|_| Error::InvalidRequest)?;
    if bytes.len() > MAX_RECORD_BYTES { return Err(Error::TooLarge); }
    files::atomic_write(&file, &bytes)?;
    Ok(json!({"status":"stored","revision":revision,"source_sha256":source_sha256,
        "graph_sha256":graph_sha256,"binding":"unknown"}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn fixture() -> (tempfile::TempDir, Config, PathBuf, Value) {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("repo");
        fs::create_dir(&root).unwrap();
        fs::create_dir(root.join(".git")).unwrap();
        fs::write(root.join(".git/config"), "[core]\n").unwrap();
        fs::write(root.join("lib.rs"), "fn one() {}\n").unwrap();
        let cfg = Config::for_root(temp.path().join("store"));
        let sha = source_hash(&root, "lib.rs").unwrap();
        let graph = json!({"scope":root,"query":"file","value":"lib.rs","status":"ok","coverage":{},
            "requested_source_sha256":sha,"requested_source_read_unix_ms":1,
            "rows":[],"edges":[],"module_edges":[]});
        let snapshot = json!({"schema_version":1,"storage":"export_only_not_persisted",
            "query_sha256":"a".repeat(64),"graph_binding":"unknown","graph":graph,
            "curated_purpose":{"project_key":project_slug(&root),"resolution":"unknown",
                "candidates":[],"freshness":"unverified_no_lore_source_hash"}});
        (temp, cfg, root, snapshot)
    }
    fn req(root: &Path, snapshot: &Value, expected: u64) -> Value {
        json!({"cwd":root,"query":"file","path":"lib.rs","expected_revision":expected,"snapshot":snapshot})
    }
    fn owner() -> Authority { Authority::HumanReview { agent: "fixture".into(), engine: "codex".into() } }

    #[test]
    fn read_is_lazy_and_owner_store_requires_exact_revision_and_current_source() {
        let (_temp, cfg, root, snapshot) = fixture();
        let request = req(&root, &snapshot, 0);
        assert_eq!(read(&cfg, &request).unwrap()["status"], "missing");
        assert!(!cfg.root.exists());
        assert_eq!(store(&cfg, &request, &Authority::Model { agent:"fixture".into(), engine:"codex".into(), session_id:"s".into() }), Err(Error::Untrusted));
        assert_eq!(store(&cfg, &request, &Authority::Interactive { agent:"fixture".into(), engine:"codex".into() }), Err(Error::Untrusted));
        assert!(!cfg.root.exists());
        assert_eq!(store(&cfg, &request, &owner()).unwrap()["revision"], 1);
        assert_eq!(read(&cfg, &request).unwrap()["binding"], "unknown");
        assert_eq!(store(&cfg, &request, &owner()), Err(Error::Changed));
        let next = req(&root, &snapshot, 1);
        assert_eq!(store(&cfg, &next, &owner()).unwrap()["revision"], 2);
        fs::write(root.join("lib.rs"), "fn changed() {}\n").unwrap();
        assert_eq!(read(&cfg, &next), Err(Error::Changed));
        assert_eq!(store(&cfg, &next, &owner()), Err(Error::Changed));
    }

    #[test]
    fn different_or_recreated_worktree_cannot_reuse_a_snapshot() {
        let (_temp, cfg, root, snapshot) = fixture();
        let request = req(&root, &snapshot, 0);
        store(&cfg, &request, &owner()).unwrap();
        let other = root.parent().unwrap().join("other");
        fs::create_dir(&other).unwrap();
        let linked_git = root.join(".git/worktrees/other");
        fs::create_dir_all(&linked_git).unwrap();
        fs::write(other.join(".git"), format!("gitdir: {}\n", linked_git.display())).unwrap();
        fs::write(other.join("lib.rs"), "fn one() {}\n").unwrap();
        assert_eq!(project_slug(&other), project_slug(&root));
        assert_eq!(read(&cfg, &req(&other, &snapshot, 0)).unwrap()["status"], "missing");
        fs::remove_dir_all(root.join(".git")).unwrap();
        assert_eq!(read(&cfg, &request), Err(Error::Unavailable));
        fs::create_dir(root.join(".git")).unwrap();
        fs::write(root.join(".git/config"), "[core]\n").unwrap();
        assert_eq!(read(&cfg, &request).unwrap()["status"], "missing");
    }

    #[test]
    fn malformed_scope_and_claims_fail_before_storage() {
        let (_temp, cfg, root, mut snapshot) = fixture();
        let mut request = req(&root, &snapshot, 0);
        request["path"] = json!("../lib.rs");
        assert_eq!(store(&cfg, &request, &owner()), Err(Error::InvalidRequest));
        request = req(&root.join("..").join("repo"), &snapshot, 0);
        assert_eq!(read(&cfg, &request), Err(Error::Changed));
        request = req(&root, &snapshot, 0);
        snapshot["graph_binding"] = json!("resolved");
        request["snapshot"] = snapshot;
        assert_eq!(store(&cfg, &request, &owner()), Err(Error::InvalidRequest));
        assert!(!cfg.root.exists());
    }

    #[test]
    fn stored_ambiguity_is_preserved_but_never_upgraded_to_a_binding() {
        let (_temp, cfg, root, mut snapshot) = fixture();
        // The producer's byte-order-specific digest is intentionally not a
        // LORE integrity claim; only LORE's graph_sha256 is returned.
        snapshot["query_sha256"] = json!("not-recomputed-by-lore");
        snapshot["graph"]["module_edges"] = json!([{
            "source":"lib.rs","target":null,"reason":"ambiguous_layout",
            "candidates":["child.rs","child/mod.rs"]
        }]);
        let request = req(&root, &snapshot, 0);
        store(&cfg, &request, &owner()).unwrap();
        let found = read(&cfg, &request).unwrap();
        assert_eq!(found["binding"], "unknown");
        assert!(found.get("query_sha256").is_none());
        assert_eq!(found["graph_sha256"], crate::digest(&graph_bytes(&snapshot["graph"]).unwrap()));
        assert_eq!(found["graph"]["module_edges"], snapshot["graph"]["module_edges"]);
        assert_eq!(found["freshness"], "requested_source_verified_only");
        let file = location(&cfg, &scope(&request).unwrap(), "file", "lib.rs");
        let mut corrupt = fs::read(&file).unwrap();
        corrupt[0] = b'!';
        fs::write(file, corrupt).unwrap();
        assert_eq!(read(&cfg, &request), Err(Error::Changed));
    }

    #[cfg(unix)]
    #[test]
    fn source_read_refuses_intermediate_symlink_even_when_outside_bytes_match() {
        use std::os::unix::fs::symlink;
        let (temp, cfg, root, mut snapshot) = fixture();
        let inside = root.join("src");
        fs::create_dir(&inside).unwrap();
        fs::write(inside.join("lib.rs"), "fn one() {}\n").unwrap();
        snapshot["graph"]["value"] = json!("src/lib.rs");
        let request = json!({"cwd":root,"query":"file","path":"src/lib.rs",
            "expected_revision":0,"snapshot":snapshot});
        store(&cfg, &request, &owner()).unwrap();
        let outside = temp.path().join("outside");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("lib.rs"), "fn one() {}\n").unwrap();
        fs::remove_file(inside.join("lib.rs")).unwrap();
        fs::remove_dir(&inside).unwrap();
        symlink(&outside, &inside).unwrap();
        assert_eq!(source_hash(&root, "src/lib.rs"), Err(Error::UnsafePath));
        assert_eq!(read(&cfg, &request), Err(Error::UnsafePath));
        assert_eq!(store(&cfg, &request, &owner()), Err(Error::UnsafePath));
    }
}
