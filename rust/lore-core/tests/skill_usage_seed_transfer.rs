use lore_core::{config::Config, files, skills, store, sync_admin, sync_network};
use serde_json::{json, Value};

fn fixture(id: &str) -> (tempfile::TempDir, Config) {
    let t = tempfile::tempdir().unwrap();
    let mut cfg = Config::for_root(t.path().join("store"));
    cfg.sync.enabled = true;
    cfg.sync.key = Some("usage-seed-transfer-fixture-key".into());
    cfg.sync.classes = ["skill_usage".into()].into_iter().collect();
    let conn = store::connect(&cfg).unwrap();
    conn.execute(
        "INSERT INTO sync_machine(machine_id,label,lamport) VALUES(?,'fixture',0)",
        [id],
    )
    .unwrap();
    (t, cfg)
}
fn ledger(cfg: &Config, uses: u64) {
    files::atomic_write(
        &cfg.root.join("skill_usage.json"),
        json!({"fixture":{"uses":uses,"ok":0,"fail":0}})
            .to_string()
            .as_bytes(),
    )
    .unwrap();
}
fn prepared() -> (tempfile::TempDir, Config) {
    let (t, cfg) = fixture("usage-author");
    ledger(&cfg, 10);
    assert_eq!(
        sync_admin::seed(&cfg, true).unwrap()["classes"]["skill_usage"]["initialized"],
        1
    );
    ledger(&cfg, 12);
    (t, cfg)
}

#[test]
fn ordinary_sync_seed_reconciles_usage_outside_portable_transaction() {
    let (_t, cfg) = prepared();
    let before = std::fs::read(cfg.root.join("skill_usage.json")).unwrap();
    let dry = sync_admin::seed(&cfg, false).unwrap();
    assert_eq!(dry["total_candidates"], 1);
    assert_eq!(dry["total_seeded"], 0);
    assert_eq!(dry["classes"]["skill_usage"]["candidates"], 1);
    assert_eq!(
        std::fs::read(cfg.root.join("skill_usage.json")).unwrap(),
        before
    );
    let applied = sync_admin::seed(&cfg, true).unwrap();
    assert_eq!(applied["total_seeded"], 1);
    assert!(std::path::Path::new(applied["backup"].as_str().unwrap()).is_file());
    let again = sync_admin::seed(&cfg, true).unwrap();
    assert_eq!(again["total_candidates"], 0);
    assert_eq!(again["total_seeded"], 0);
    let conn = store::read_only(&cfg).unwrap();
    assert_eq!(
        conn.query_row(
            "SELECT count(*) FROM sync_ops WHERE class='skill_usage'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}

#[test]
fn opted_in_offline_usage_transfer_is_signed_idempotent_and_class_gated() {
    let (t, cfg) = prepared();
    sync_admin::seed(&cfg, true).unwrap();
    let bundle = t.path().join("usage.bundle");
    sync_network::export_bundle(&cfg, &bundle).unwrap();
    let data: Value = serde_json::from_slice(&std::fs::read(&bundle).unwrap()).unwrap();
    assert!(data["classes"]
        .as_array()
        .unwrap()
        .contains(&json!("skill_usage")));
    assert_eq!(data["ops"].as_array().unwrap().len(), 1);
    let (_dest, receiver) = fixture("usage-receiver");
    let report = sync_network::import_bundle(&receiver, &bundle).unwrap();
    assert_eq!(report["applied"], 1);
    assert_eq!(report["unverified"], 0);
    assert_eq!(skills::load_usage(&receiver).unwrap()["fixture"]["uses"], 2);
    sync_network::import_bundle(&receiver, &bundle).unwrap();
    assert_eq!(skills::load_usage(&receiver).unwrap()["fixture"]["uses"], 2);

    let (_disabled, mut disabled) = fixture("disabled-receiver");
    disabled.sync.classes.clear();
    sync_network::import_bundle(&disabled, &bundle).unwrap();
    assert_eq!(skills::load_usage(&disabled).unwrap(), json!({}));
    let mut excluded = cfg.clone();
    excluded.sync.classes = ["memory".into()].into_iter().collect();
    let filtered = t.path().join("without-usage.bundle");
    sync_network::export_bundle(&excluded, &filtered).unwrap();
    let data: Value = serde_json::from_slice(&std::fs::read(&filtered).unwrap()).unwrap();
    assert!(!data["classes"]
        .as_array()
        .unwrap()
        .contains(&json!("skill_usage")));
    assert!(data["ops"].as_array().unwrap().is_empty());
}
