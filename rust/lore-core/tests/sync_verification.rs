#[path = "../src/sync_verification.rs"]
mod sync_verification;

use lore_core::{config::Config, store, sync_apply, Error};
use rusqlite::Connection;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

const KEY: &str = "issue112-offline-verifier-fixture-key";

fn config(root: PathBuf) -> Config {
    let mut config = Config::for_root(root);
    config.sync.enabled = true;
    config.sync.key = Some(KEY.into());
    config.sync.classes = ["beliefs"].into_iter().map(str::to_owned).collect();
    config
}

fn op(sequence: i64, verb: &str, payload: Value) -> Value {
    let mut op = json!({"op_id":format!("fixture-op-{sequence}"),"machine_id":"retained-source",
        "machine_seq":sequence,"lamport":sequence,"class":"belief","op":verb,
        "project_key":null,"payload":payload,"created":"2026-09-30T00:00:00Z","mac":null});
    op["mac"] = json!(sync_apply::compute_mac(&op, KEY).unwrap());
    op
}

fn backup(source: &Config, path: &Path) {
    let source = store::read_only(source).unwrap();
    let mut target = Connection::open(path).unwrap();
    let copy = rusqlite::backup::Backup::new(&source, &mut target).unwrap();
    copy.run_to_completion(128, std::time::Duration::ZERO, None)
        .unwrap();
}

fn fixture(ops: &[Value]) -> (tempfile::TempDir, Config, PathBuf) {
    let temp = tempfile::tempdir().unwrap();
    let cfg = config(temp.path().join("source"));
    for chunk in ops.chunks(sync_apply::MAX_PAGE) {
        sync_apply::apply_ops(&cfg, chunk).unwrap();
    }
    let path = temp.path().join("retained-backup.db");
    backup(&cfg, &path);
    (temp, cfg, path)
}

fn historical() -> Vec<Value> {
    vec![
        op(
            1,
            "insert",
            json!({"uid":"original-uid","subject":"user","claim":"private fixture text must not escape","confidence":0.8}),
        ),
        op(2, "reinforce", json!({"uid":null})),
        op(
            3,
            "edge",
            json!({"src_uid":"original-uid","dst_uid":null,"rel":"explains","source":"derived"}),
        ),
        op(4, "supersede", json!({"uid":"original-uid","by_uid":null})),
    ]
}

#[test]
fn native_inspection_never_replays_even_valid_history() {
    let (temp, cfg, path) = fixture(&historical());
    let before = std::fs::read(&path).unwrap();
    let report = sync_verification::inspect_backup(&path, &cfg).unwrap();
    assert_eq!(report["signature_gate"], true);
    assert_eq!(report["source"]["unchanged"], true);
    assert_eq!(report["replay_performed"], false);
    assert_eq!(report["offline_replay_gate"], false);
    assert_eq!(report["issue112_success_gate"], false);
    assert_eq!(std::fs::read_dir(temp.path()).unwrap().count(), 2);
    assert_eq!(before, std::fs::read(&path).unwrap());
}

#[test]
fn offline_historical_replay_preserves_proof_and_reports_unproven_full_gate() {
    let (temp, cfg, path) = fixture(&historical());
    let before = std::fs::read(&path).unwrap();
    let report = sync_verification::verify_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(report["source"]["verified_macs"], 4);
    assert_eq!(report["source"]["unchanged"], true);
    assert_eq!(report["durable_states"]["quarantined"], 3);
    assert_eq!(report["durable_states"]["applied"], 1);
    assert_eq!(report["signed_history_preserved"], true);
    assert_eq!(report["portable_database"]["matches"], true);
    assert_eq!(report["idempotent_database_and_log"], true);
    assert_eq!(report["offline_replay_gate"], true);
    assert_eq!(report["issue112_success_gate"], false);
    assert_eq!(report["transport"]["banked_cursor"], Value::Null);
    let output = report.to_string();
    for private in [KEY, "private fixture text", "original-uid", "fixture-op-"] {
        assert!(!output.contains(private), "report leaked {private}");
    }
    assert_eq!(before, std::fs::read(&path).unwrap());
    assert!(!PathBuf::from(format!("{}-wal", path.display())).exists());
    assert_eq!(
        std::fs::read_dir(temp.path()).unwrap().count(),
        2,
        "temporary destination must be removed"
    );
}

#[test]
fn native_loopback_transport_banks_cursor_repeats_and_resumes_then_cleans_up() {
    let (temp, cfg, path) = fixture(&historical());
    let before = std::fs::read(&path).unwrap();
    let report = sync_verification::verify_transport_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(report["source"]["verified_macs"], 4);
    assert_eq!(report["first_pass"]["fetched"], 4);
    assert_eq!(report["durable_states"]["applied"], 1);
    assert_eq!(report["durable_states"]["quarantined"], 3);
    assert_eq!(report["transport"]["initial_cursor"], 0);
    assert_eq!(report["transport"]["banked_cursor"], 4);
    assert_eq!(report["transport"]["repeat_banked_cursor"], 4);
    assert_eq!(report["transport"]["resumed_banked_cursor"], 4);
    assert_eq!(report["transport"]["resumed"]["fetched"], 0);
    assert_eq!(report["repeat"]["duplicate"], 4);
    assert_eq!(report["transport"]["source_copy_history_preserved"], true);
    assert_eq!(report["transport"]["listener_closed"], true);
    assert_eq!(report["transport"]["delivery_gate"], true);
    assert_eq!(report["transport_replay_gate"], true);
    assert_eq!(report["issue112_success_gate"], false);
    assert_eq!(std::fs::read_dir(temp.path()).unwrap().count(), 2);
    assert_eq!(before, std::fs::read(&path).unwrap());
    for private in [KEY, "private fixture text", "original-uid", "fixture-op-"] {
        assert!(!report.to_string().contains(private));
    }
}

#[test]
fn native_loopback_cursor_is_source_sequence_not_operation_count() {
    let (temp, cfg, path) = fixture(&historical());
    let conn = Connection::open(&path).unwrap();
    conn.execute("UPDATE sync_ops SET seq=seq+100", []).unwrap();
    drop(conn);
    let report = sync_verification::verify_transport_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(report["source"]["operations"], 4);
    assert_eq!(report["transport"]["expected_cursor"], 104);
    assert_eq!(report["transport"]["banked_cursor"], 104);
    assert_eq!(report["transport"]["delivery_gate"], true);
    assert_eq!(report["signed_history_preserved"], true);
}

#[test]
fn native_loopback_receipt_cursor_does_not_hide_unresolved_dependency() {
    let (temp, cfg, path) = fixture(&[op(1, "reinforce", json!({"uid":"absent-uid"}))]);
    let report = sync_verification::verify_transport_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(report["transport"]["banked_cursor"], 1);
    assert_eq!(report["transport"]["cursor_gate"], true);
    assert_eq!(report["durable_states"]["deferred"], 1);
    assert_eq!(report["transport"]["delivery_gate"], false);
    assert_eq!(report["transport_replay_gate"], false);
    assert_eq!(report["transport"]["listener_closed"], true);
}

#[test]
fn native_loopback_banked_cursor_does_not_waive_semantic_parity() {
    let (temp, cfg, path) = fixture(&historical());
    let conn = Connection::open(&path).unwrap();
    conn.execute("UPDATE beliefs SET confidence=0.2", [])
        .unwrap();
    drop(conn);
    let report = sync_verification::verify_transport_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(report["transport"]["delivery_gate"], true);
    assert_eq!(report["transport_replay_gate"], false);
    let fields = &report["portable_database"]["differences"]["beliefs"]["fields"];
    assert_eq!(fields["confidence"]["matches"], false);
    assert_eq!(fields["claim"]["matches"], true);
    assert_eq!(
        fields["confidence"]["source"]["sha256"]
            .as_str()
            .unwrap()
            .len(),
        64
    );
    assert_eq!(
        report["portable_database"]["differences"]["beliefs"]["row_multiset_difference"]
            ["source_only"]["count"],
        1
    );
    assert_eq!(report["issue112_success_gate"], false);
}

#[test]
fn native_loopback_refuses_unauthenticated_source_before_temp_allocation() {
    let (temp, mut cfg, path) = fixture(&historical());
    cfg.sync.key = Some("different-fixture-key".into());
    let absent = temp.path().join("must-not-be-created");
    let report = sync_verification::verify_transport_backup(&path, &cfg, &absent).unwrap();
    assert_eq!(report["signature_gate"], false);
    assert_eq!(report["transport"]["exercised"], false);
    assert!(!absent.exists());
}

#[test]
fn tampered_or_unsigned_history_is_refused_before_destination_allocation() {
    for mac in [Some("0".repeat(64)), None] {
        let (temp, cfg, path) = fixture(&historical());
        let conn = Connection::open(&path).unwrap();
        conn.execute(
            "UPDATE sync_ops SET mac=? WHERE op_id='fixture-op-2'",
            [mac],
        )
        .unwrap();
        drop(conn);
        let absent = temp.path().join("must-not-be-created");
        let report = sync_verification::verify_backup(&path, &cfg, &absent).unwrap();
        assert_eq!(report["source"]["unverified_macs"], 1);
        assert_eq!(report["replay_performed"], false);
        assert_eq!(report["offline_replay_gate"], false);
        assert!(!absent.exists());
    }
}

#[test]
fn valid_log_digest_does_not_hide_semantic_state_divergence() {
    let (temp, cfg, path) = fixture(&historical());
    let conn = Connection::open(&path).unwrap();
    conn.execute("UPDATE beliefs SET confidence=0.2", [])
        .unwrap();
    drop(conn);
    let report = sync_verification::verify_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(report["signed_history_preserved"], true);
    assert_eq!(report["portable_database"]["matches"], false);
    assert_eq!(
        report["portable_database"]["differences"]["beliefs"]["field_multiset_differences"],
        json!(["confidence"])
    );
    assert_eq!(report["offline_replay_gate"], false);
}

#[test]
fn duplicate_row_diagnostics_do_not_weaken_exact_state_gate() {
    let (temp, cfg, path) = fixture(&historical());
    let conn = Connection::open(&path).unwrap();
    conn.execute(
        "INSERT INTO belief_evidence SELECT * FROM belief_evidence",
        [],
    )
    .unwrap();
    drop(conn);
    let report = sync_verification::verify_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(
        report["portable_database"]["source"]["evidence"]["count"],
        2
    );
    assert_eq!(
        report["portable_database"]["destination"]["evidence"]["count"],
        1
    );
    assert_eq!(
        report["portable_database"]["differences"]["evidence"]["distinct_rows"]["matches"],
        true
    );
    assert_eq!(report["portable_database"]["matches"], false);
    assert_eq!(report["offline_replay_gate"], false);
}

#[test]
fn portable_projection_ignores_local_row_ids() {
    let (temp, cfg, path) = fixture(&historical());
    let conn = Connection::open(&path).unwrap();
    conn.execute("UPDATE beliefs SET id=id+1000", []).unwrap();
    conn.execute("UPDATE belief_evidence SET belief_id=belief_id+1000", [])
        .unwrap();
    drop(conn);
    let report = sync_verification::verify_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(report["portable_database"]["matches"], true);
    assert_eq!(report["offline_replay_gate"], true);
}

#[test]
fn unresolved_nonnull_uid_remains_deferred_and_fails_gate() {
    let (temp, cfg, path) = fixture(&[op(1, "reinforce", json!({"uid":"absent-uid"}))]);
    let report = sync_verification::verify_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(report["durable_states"]["deferred"], 1);
    assert_eq!(report["durable_states"]["quarantined"], 0);
    assert_eq!(report["offline_replay_gate"], false);
}

#[test]
fn cross_chunk_dependency_uses_final_durable_states() {
    let mut ops = vec![op(1, "reinforce", json!({"uid":"late-uid"}))];
    for seq in 2..=513 {
        ops.push(op(
            seq,
            "insert",
            json!({"uid":format!("uid-{seq}"),"claim":format!("fixture {seq}"),"confidence":0.8}),
        ));
    }
    ops.push(op(
        514,
        "insert",
        json!({"uid":"late-uid","claim":"late fixture","confidence":0.8}),
    ));
    let (temp, cfg, path) = fixture(&ops);
    let report = sync_verification::verify_backup(&path, &cfg, temp.path()).unwrap();
    assert_eq!(
        report["first_pass"]["deferred"], 1,
        "provisional page report remains distinct from durable states"
    );
    assert_eq!(report["durable_states"]["deferred"], 0);
    assert_eq!(report["durable_states"]["applied"], 514);
    assert_eq!(report["offline_replay_gate"], true);
}

#[test]
fn refuses_live_names_sidecars_symlinks_oversized_inputs_and_missing_keys() {
    let (temp, mut cfg, path) = fixture(&historical());
    cfg.sync.key = None;
    assert_eq!(
        sync_verification::verify_backup(&path, &cfg, temp.path()).unwrap_err(),
        Error::Untrusted
    );
    cfg.sync.key = Some(KEY.into());
    let live = temp.path().join("state.db");
    std::fs::copy(&path, &live).unwrap();
    assert_eq!(
        sync_verification::verify_backup(&live, &cfg, temp.path()).unwrap_err(),
        Error::UnsafePath
    );
    std::fs::write(
        format!("{}-wal", path.display()),
        b"not a standalone snapshot",
    )
    .unwrap();
    assert_eq!(
        sync_verification::verify_backup(&path, &cfg, temp.path()).unwrap_err(),
        Error::UnsafePath
    );
    let huge = temp.path().join("huge.db");
    std::fs::File::create(&huge)
        .unwrap()
        .set_len(256 * 1024 * 1024 + 1)
        .unwrap();
    assert_eq!(
        sync_verification::verify_backup(&huge, &cfg, temp.path()).unwrap_err(),
        Error::TooLarge
    );
    #[cfg(unix)]
    {
        let link = temp.path().join("linked.db");
        std::os::unix::fs::symlink(&live, &link).unwrap();
        assert_eq!(
            sync_verification::verify_backup(&link, &cfg, temp.path()).unwrap_err(),
            Error::UnsafePath
        );
    }
}
