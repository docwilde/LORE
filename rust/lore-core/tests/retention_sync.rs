use lore_core::{
    config::Config,
    gate::{self, Authority},
    pending, store, sync_apply,
};
use serde_json::json;

#[test]
fn unsigned_foreign_trace_reference_is_staged_without_claiming_local_evidence() {
    let t = tempfile::tempdir().unwrap();
    let mut cfg = Config::for_root(t.path().join("store"));
    cfg.sync.enabled = true;
    cfg.sync.key = Some("foreign-trace-reference-fixture-key".into());
    cfg.sync.classes = ["pending".into()].into_iter().collect();
    let foreign = json!({"op_id":"foreign-trace-proposal","machine_id":"foreign-author",
        "machine_seq":1,"lamport":1,"class":"pending","op":"stage","project_key":null,
        "payload":{"uid":"foreign-proposal","item":{"kind":"skill","action":"add",
            "name":"fixture","body":"---\ndescription: fixture\n---\nlore-learned\n",
            "diagnostic_trace_sha256":"f".repeat(64)}},"mac":null});
    let report = sync_apply::apply_ops(&cfg, &[foreign.clone()]).unwrap();
    assert_eq!(report["unverified"], 1);
    assert_eq!(report["failed"], 0);
    let ids = pending::ids(&cfg).unwrap();
    assert_eq!(ids.len(), 1);
    let snapshot =
        pending::snapshot(&cfg.root.join("pending").join(format!("{}.json", ids[0]))).unwrap();
    assert_eq!(snapshot.item["kind"], "sync");
    assert_eq!(snapshot.item["op"], foreign);
    assert!(!cfg.root.join("diagnostic-traces").exists());
    let conn = store::read_only(&cfg).unwrap();
    assert_eq!(
        conn.query_row(
            "SELECT count(*) FROM sync_ops WHERE op_id='foreign-trace-proposal' AND applied=1",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
    assert!(!cfg.skills.join("fixture/SKILL.md").exists());
}

#[test]
fn local_skill_proposal_cannot_publish_a_missing_trace_reference() {
    let t = tempfile::tempdir().unwrap();
    let mut cfg = Config::for_root(t.path().join("store"));
    cfg.sync.enabled = false;
    let item = json!({"kind":"skill","action":"add","name":"fixture",
        "body":"fixture","diagnostic_trace_sha256":"f".repeat(64)});
    let authority = Authority::Derived {
        agent: "fixture-reviewer".into(),
        engine: "test".into(),
    };
    assert!(gate::stage(&cfg, &item, &authority).is_err());
    assert!(pending::ids(&cfg).unwrap().is_empty());
    assert!(!cfg.root.join("diagnostic-traces").exists());
}
