use lore_core::{
    config::Config,
    files,
    gate::Authority,
    index::Message,
    skills::{self, sync_seed, usage_sync},
    store, sync_apply, Error,
};
use serde_json::{json, Value};

const KEY: &str = "usage-sync-fixture-key";
fn config(id: &str) -> (tempfile::TempDir, Config) {
    let temp = tempfile::tempdir().unwrap();
    let mut cfg = Config::for_root(temp.path().join("root"));
    cfg.sync.enabled = true;
    cfg.sync.classes = ["skill_usage".into()].into_iter().collect();
    cfg.sync.key = Some(KEY.into());
    let conn = store::connect(&cfg).unwrap();
    conn.execute(
        "INSERT INTO sync_machine(machine_id,label,lamport) VALUES(?, 'fixture', 0)",
        [id],
    )
    .unwrap();
    install(&cfg, "fixture");
    (temp, cfg)
}
fn install(cfg: &Config, name: &str) {
    files::atomic_write(
        &cfg.skills.join(name).join("SKILL.md"),
        b"---\ndescription: fixture recipe\n---\nlore-learned\n",
    )
    .unwrap();
}
fn reviewer() -> Authority {
    Authority::Derived {
        agent: "fixture-reviewer".into(),
        engine: "test".into(),
    }
}
fn use_n(cfg: &Config, n: usize) {
    skills::record_usage(
        cfg,
        &(0..n)
            .map(|_| Message {
                ts: "".into(),
                role: "tool".into(),
                content: "Skill: fixture".into(),
            })
            .collect::<Vec<_>>(),
        &reviewer(),
    )
    .unwrap();
}
fn outcome(cfg: &Config, result: &str) {
    skills::record_outcomes(
        cfg,
        &json!({"skill_outcomes":[{"name":"fixture","outcome":result,"reason":"fixture only"}]}),
        &cfg.root,
        &reviewer(),
    )
    .unwrap();
}
fn own_ops(cfg: &Config) -> Vec<Value> {
    let conn = store::read_only(cfg).unwrap();
    let mut stmt = conn.prepare("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac FROM sync_ops WHERE machine_id=(SELECT machine_id FROM sync_machine LIMIT 1) AND class='skill_usage' ORDER BY machine_seq").unwrap();
    stmt.query_map([], |row| Ok(json!({"op_id":row.get::<_,String>(0)?,"machine_id":row.get::<_,String>(1)?,"machine_seq":row.get::<_,i64>(2)?,"lamport":row.get::<_,i64>(3)?,"class":row.get::<_,String>(4)?,"op":row.get::<_,String>(5)?,"project_key":row.get::<_,Option<String>>(6)?,"payload":serde_json::from_str::<Value>(&row.get::<_,String>(7)?).unwrap(),"mac":row.get::<_,Option<String>>(8)?}))).unwrap().collect::<rusqlite::Result<_>>().unwrap()
}
fn sign(mut op: Value) -> Value {
    op["mac"] = json!(sync_apply::compute_mac(&op, KEY).unwrap());
    op
}
fn snapshot(author: &str, seq: u64, generation: u64, uses: u64, ok: u64, fail: u64) -> Value {
    sign(
        json!({"op_id":format!("{author}-{seq}"),"machine_id":author,"machine_seq":seq,"lamport":seq,"class":"skill_usage","op":"snapshot","project_key":null,"payload":{"v":1,"name":"fixture","origin":author,"generation":generation,"uses":uses,"ok":ok,"fail":fail}}),
    )
}
fn total(cfg: &Config) -> Value {
    let row = skills::load_usage(cfg).unwrap()["fixture"].clone();
    json!([
        row["uses"].as_u64().unwrap_or(0),
        row["ok"].as_u64().unwrap_or(0),
        row["fail"].as_u64().unwrap_or(0)
    ])
}
fn table_exists(cfg: &Config) -> bool {
    store::read_only(cfg)
        .unwrap()
        .query_row(
            "SELECT count(*) FROM sqlite_master WHERE name='skill_usage_components'",
            [],
            |r| r.get::<_, i64>(0),
        )
        .unwrap()
        != 0
}

#[test]
fn two_machines_converge_without_importing_review_proof_or_reauthoring() {
    let (_a, a) = config("machine-a");
    let (_b, b) = config("machine-b");
    use_n(&a, 2);
    outcome(&a, "success");
    use_n(&b, 3);
    outcome(&b, "failure");
    let ao = own_ops(&a);
    let bo = own_ops(&b);
    assert_eq!(sync_apply::apply_ops(&a, &bo).unwrap()["applied"], bo.len());
    assert_eq!(sync_apply::apply_ops(&b, &ao).unwrap()["applied"], ao.len());
    assert_eq!(total(&a), json!([5, 1, 1]));
    assert_eq!(total(&b), total(&a));
    assert_eq!(skills::load_local_usage(&a).unwrap()["fixture"]["uses"], 2);
    assert_eq!(skills::load_local_usage(&b).unwrap()["fixture"]["uses"], 3);
    assert!(!skills::update_admitted(
        "update",
        &skills::load_usage(&a).unwrap()["fixture"]
    ));
    assert!(!skills::update_admitted(
        "retire",
        &skills::load_usage(&b).unwrap()["fixture"]
    ));
    assert_eq!(own_ops(&a), ao);
    assert_eq!(own_ops(&b), bo);
    for op in ao.iter().chain(&bo) {
        assert!(sync_apply::verify_mac(op, Some(KEY)));
        assert_eq!(op["payload"].as_object().unwrap().len(), 7);
        assert!(op["payload"].get("trail").is_none());
    }
    // Relaying an original snapshot back to its author must not add it twice.
    sync_apply::apply_ops(&a, &ao).unwrap();
    assert_eq!(total(&a), json!([5, 1, 1]));
    outcome(&a, "success");
    assert!(skills::update_admitted(
        "update",
        &skills::load_usage(&a).unwrap()["fixture"]
    ));
}

#[test]
fn duplicate_reorder_and_distinct_relay_frames_merge_components_by_max() {
    let (_a, a) = config("receiver-a");
    let (_b, b) = config("receiver-b");
    let small = snapshot("origin", 1, 1, 2, 1, 0);
    let large = snapshot("origin", 2, 1, 7, 0, 1);
    // Distinct signed op IDs carry overlapping snapshots. Oplog dedupe alone
    // cannot prevent double counting these.
    let repeated = snapshot("origin", 3, 1, 7, 0, 1);
    sync_apply::apply_ops(&a, &[large.clone(), repeated.clone()]).unwrap();
    sync_apply::apply_ops(&a, std::slice::from_ref(&small)).unwrap();
    sync_apply::apply_ops(&b, std::slice::from_ref(&small)).unwrap();
    sync_apply::apply_ops(&b, &[repeated.clone(), large.clone()]).unwrap();
    assert_eq!(total(&a), json!([7, 1, 1]));
    assert_eq!(total(&b), total(&a));
    assert_eq!(
        sync_apply::apply_ops(&a, &[small, large, repeated]).unwrap()["duplicate"],
        3
    );
    assert_eq!(total(&a), json!([7, 1, 1]));
}

#[test]
fn default_off_and_class_disabled_never_touch_counter_state() {
    assert!(!Config::for_root("/tmp/unused-usage-default".into())
        .sync
        .classes
        .contains("skill_usage"));
    let (_t, mut cfg) = config("local");
    cfg.sync.classes.clear();
    use_n(&cfg, 2);
    let bytes = std::fs::read(cfg.root.join("skill_usage.json")).unwrap();
    assert_eq!(
        sync_apply::apply_ops(&cfg, &[snapshot("foreign", 1, 1, 99, 3, 3)]).unwrap()["skipped"],
        1
    );
    assert_eq!(sync_seed::skill_usage(&cfg, true).unwrap()["seeded"], 0);
    assert!(!table_exists(&cfg));
    assert!(own_ops(&cfg).is_empty());
    assert_eq!(
        std::fs::read(cfg.root.join("skill_usage.json")).unwrap(),
        bytes
    );
    assert_eq!(total(&cfg), json!([2, 0, 0]));
    cfg.sync.classes.insert("skill_usage".into());
    cfg.sync.enabled = false;
    assert_eq!(
        sync_apply::apply_ops(&cfg, &[snapshot("foreign", 1, 1, 99, 3, 3)]).unwrap()["skipped"],
        1
    );
    assert!(!table_exists(&cfg));
}

#[test]
fn malformed_signed_inputs_refuse_atomically_while_a_good_neighbor_applies() {
    let (_t, cfg) = config("local");
    let mut bad = Vec::new();
    for (field, value) in [
        ("uses", json!(-1)),
        ("uses", json!(true)),
        ("uses", json!(1.5)),
        ("uses", json!("2")),
        ("uses", json!(usage_sync::MAX_COUNT + 1)),
        ("uses", json!(u64::MAX)),
        ("generation", json!(0)),
        ("generation", json!(usage_sync::MAX_GENERATION + 1)),
        ("origin", json!("victim")),
        ("name", json!("../escape")),
        ("trail", json!([{"o":"success"}])),
        ("v", json!(2)),
    ] {
        let mut op = snapshot("foreign", bad.len() as u64 + 1, 1, 5, 2, 1);
        op["payload"][field] = value;
        bad.push(sign(op));
    }
    let mut scoped = snapshot("foreign", 20, 1, 5, 2, 1);
    scoped["project_key"] = json!("wrong");
    bad.push(sign(scoped));
    let mut verb = snapshot("foreign", 21, 1, 5, 2, 1);
    verb["op"] = json!("add");
    bad.push(sign(verb));
    let count = bad.len();
    bad.push(snapshot("honest", 1, 1, 1, 0, 0));
    let report = sync_apply::apply_ops(&cfg, &bad).unwrap();
    assert_eq!(report["failed"], count);
    assert_eq!(report["applied"], 1);
    assert_eq!(total(&cfg), json!([1, 0, 0]));
    let conn = store::read_only(&cfg).unwrap();
    assert_eq!(
        conn.query_row("SELECT count(*) FROM skill_usage_components", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn unsigned_or_tampered_counts_cannot_gain_authority_through_approval() {
    let (_t, cfg) = config("local");
    let mut unsigned = snapshot("foreign", 1, 1, 50, 3, 0);
    unsigned["mac"] = Value::Null;
    let mut tampered = snapshot("foreign", 2, 1, 50, 3, 0);
    tampered["payload"]["uses"] = json!(51);
    let report = sync_apply::apply_ops(&cfg, &[unsigned.clone(), tampered]).unwrap();
    assert_eq!(report["unverified"], 2);
    assert!(!table_exists(&cfg));
    let auth = Authority::HumanReview {
        agent: "fixture".into(),
        engine: "test".into(),
    };
    assert_eq!(
        sync_apply::approve(&cfg, &unsigned, &auth),
        Err(Error::Untrusted)
    );
    assert!(!table_exists(&cfg));
    assert_eq!(lore_core::pending::ids(&cfg).unwrap().len(), 2);
    // A valid signed tuple with a foreign payload origin still fails.
    let mut impersonation = snapshot("foreign", 3, 1, 50, 3, 0);
    impersonation["payload"]["origin"] = json!("victim");
    assert_eq!(
        sync_apply::apply_ops(&cfg, &[sign(impersonation)]).unwrap()["failed"],
        1
    );
    assert!(!table_exists(&cfg));
}

#[test]
fn reset_replaces_only_the_resetting_author_and_old_generations_stay_dead() {
    let (_a, a) = config("machine-a");
    let (_b, b) = config("machine-b");
    use_n(&a, 2);
    use_n(&b, 3);
    let old = own_ops(&a);
    sync_apply::apply_ops(&b, &old).unwrap();
    sync_apply::apply_ops(&a, &own_ops(&b)).unwrap();
    assert_eq!(total(&a), json!([5, 0, 0]));
    std::fs::remove_file(a.root.join("skill_usage.json")).unwrap();
    assert_eq!(sync_seed::skill_usage(&a, false).unwrap()["candidates"], 1);
    assert_eq!(sync_seed::skill_usage(&a, true).unwrap()["seeded"], 1);
    assert_eq!(sync_seed::skill_usage(&a, true).unwrap()["seeded"], 0);
    use_n(&a, 1);
    let latest = own_ops(&a).last().unwrap().clone();
    assert_eq!(latest["payload"]["generation"], 2);
    assert_eq!(latest["payload"]["uses"], 1);
    sync_apply::apply_ops(&b, &[latest]).unwrap();
    // New IDs for old snapshots exercise the component generation fence.
    let stale = snapshot("machine-a", 100, 1, 99, 9, 9);
    sync_apply::apply_ops(&b, &[stale]).unwrap();
    assert_eq!(total(&a), json!([4, 0, 0]));
    assert_eq!(total(&b), total(&a));
}

#[test]
fn legacy_is_preserved_locally_and_never_claimed_as_a_new_observation() {
    let (_a, a) = config("machine-a");
    let (_b, b) = config("machine-b");
    files::atomic_write(
        &a.root.join("skill_usage.json"),
        br#"{"fixture":{"uses":9,"ok":3,"trail":[{"o":"success","h":"old"}]}}"#,
    )
    .unwrap();
    let before = std::fs::read(a.root.join("skill_usage.json")).unwrap();
    assert_eq!(sync_seed::skill_usage(&a, false).unwrap()["initialized"], 1);
    assert!(!table_exists(&a));
    assert_eq!(sync_seed::skill_usage(&a, true).unwrap()["seeded"], 0);
    assert_eq!(
        std::fs::read(a.root.join("skill_usage.json")).unwrap(),
        before
    );
    use_n(&a, 1);
    let ops = own_ops(&a);
    assert_eq!(ops.len(), 1);
    assert_eq!(ops[0]["payload"]["uses"], 1);
    sync_apply::apply_ops(&b, &ops).unwrap();
    assert_eq!(total(&a), json!([10, 3, 0]));
    assert_eq!(total(&b), json!([1, 0, 0]));
    assert!(skills::update_admitted(
        "retire",
        &skills::load_usage(&a).unwrap()["fixture"]
    ));
    assert!(!skills::update_admitted(
        "update",
        &skills::load_usage(&b).unwrap()["fixture"]
    ));
    // A decrease establishes a new local baseline, not a subtraction from a
    // foreign author, and an unknown post-reset count is not exported.
    files::atomic_write(
        &a.root.join("skill_usage.json"),
        br#"{"fixture":{"uses":4}}"#,
    )
    .unwrap();
    assert_eq!(sync_seed::skill_usage(&a, true).unwrap()["seeded"], 1);
    let reset = own_ops(&a).last().unwrap().clone();
    assert_eq!(reset["payload"]["uses"], 0);
    sync_apply::apply_ops(&b, &[reset]).unwrap();
    assert_eq!(total(&b), json!([0, 0, 0]));
    use_n(&a, 1);
    assert_eq!(own_ops(&a).last().unwrap()["payload"]["uses"], 1);
}

#[test]
fn local_file_commit_with_failed_log_recovers_once_and_never_exports_legacy() {
    let (_t, cfg) = config("local");
    let conn = store::connect(&cfg).unwrap();
    conn.execute_batch("CREATE TRIGGER refuse_usage BEFORE INSERT ON sync_ops WHEN NEW.class='skill_usage' BEGIN SELECT RAISE(ABORT,'fixture'); END;").unwrap();
    let messages = [Message {
        ts: "".into(),
        role: "tool".into(),
        content: "Skill: fixture".into(),
    }];
    assert_eq!(
        skills::record_usage(&cfg, &messages, &reviewer()),
        Err(Error::MayHaveApplied)
    );
    assert_eq!(total(&cfg), json!([1, 0, 0]));
    assert!(own_ops(&cfg).is_empty());
    conn.execute_batch("DROP TRIGGER refuse_usage").unwrap();
    assert_eq!(sync_seed::skill_usage(&cfg, true).unwrap()["seeded"], 1);
    assert_eq!(sync_seed::skill_usage(&cfg, true).unwrap()["seeded"], 0);
    assert_eq!(own_ops(&cfg)[0]["payload"]["uses"], 1);
}

#[test]
fn storage_origins_counts_and_local_rows_are_bounded() {
    let (_t, cfg) = config("local");
    let ops = (0..usage_sync::MAX_ORIGINS)
        .map(|i| snapshot(&format!("origin-{i}"), 1, 1, usage_sync::MAX_COUNT, 0, 0))
        .collect::<Vec<_>>();
    assert_eq!(
        sync_apply::apply_ops(&cfg, &ops).unwrap()["applied"],
        usage_sync::MAX_ORIGINS
    );
    assert_eq!(
        total(&cfg),
        json!([usage_sync::MAX_COUNT * usage_sync::MAX_ORIGINS as u64, 0, 0])
    );
    assert_eq!(
        sync_apply::apply_ops(&cfg, &[snapshot("overflow-origin", 1, 1, 1, 0, 0)]).unwrap()
            ["failed"],
        1
    );
    let rows = (0..=usage_sync::MAX_SKILLS)
        .map(|i| (format!("skill-{i}"), json!({"uses":1})))
        .collect::<serde_json::Map<_, _>>();
    files::atomic_write(
        &cfg.root.join("skill_usage.json"),
        json!(rows).to_string().as_bytes(),
    )
    .unwrap();
    assert_eq!(sync_seed::skill_usage(&cfg, true), Err(Error::TooLarge));
}

#[test]
fn dry_run_and_no_key_have_no_sync_side_effects() {
    let temp = tempfile::tempdir().unwrap();
    let mut cfg = Config::for_root(temp.path().join("absent"));
    cfg.sync.classes = ["skill_usage".into()].into_iter().collect();
    cfg.sync.enabled = true;
    cfg.sync.key = Some(KEY.into());
    assert_eq!(
        sync_seed::skill_usage(&cfg, false).unwrap()["candidates"],
        0
    );
    assert!(!cfg.root.exists());
    let (_t, mut cfg) = config("local");
    cfg.sync.key = None;
    use_n(&cfg, 1);
    assert!(!table_exists(&cfg));
    assert!(own_ops(&cfg).is_empty());
    assert_eq!(sync_seed::skill_usage(&cfg, true), Err(Error::Untrusted));
}

#[test]
fn renamed_names_do_not_transfer_observations_and_generation_exhaustion_refuses() {
    let (_t, cfg) = config("local");
    use_n(&cfg, 2);
    files::atomic_write(
        &cfg.root.join("skill_usage.json"),
        br#"{"renamed":{"uses":2}}"#,
    )
    .unwrap();
    let result = sync_seed::skill_usage(&cfg, true).unwrap();
    assert_eq!(result["initialized"], 1);
    assert_eq!(result["seeded"], 1);
    let ops = own_ops(&cfg);
    assert_eq!(ops.last().unwrap()["payload"]["name"], "fixture");
    assert_eq!(ops.last().unwrap()["payload"]["generation"], 2);
    assert_eq!(ops.last().unwrap()["payload"]["uses"], 0);
    assert!(ops.iter().all(|op| op["payload"]["name"] != "renamed"));
    let conn = store::connect(&cfg).unwrap();
    conn.execute(
        "UPDATE skill_usage_local SET generation=?,uses=1 WHERE name='fixture'",
        [usage_sync::MAX_GENERATION as i64],
    )
    .unwrap();
    conn.execute(
        "UPDATE skill_usage_components SET generation=? WHERE name='fixture' AND origin='local'",
        [usage_sync::MAX_GENERATION as i64],
    )
    .unwrap();
    let before = own_ops(&cfg);
    assert_eq!(sync_seed::skill_usage(&cfg, true), Err(Error::TooLarge));
    assert_eq!(own_ops(&cfg), before);
}

#[test]
fn missing_baseline_or_invalid_local_counts_refuse_instead_of_recounting() {
    let (_t, cfg) = config("local");
    use_n(&cfg, 1);
    let conn = store::connect(&cfg).unwrap();
    conn.execute("DELETE FROM skill_usage_local", []).unwrap();
    assert_eq!(sync_seed::skill_usage(&cfg, true), Err(Error::Changed));
    files::atomic_write(
        &cfg.root.join("skill_usage.json"),
        br#"{"fixture":{"uses":true}}"#,
    )
    .unwrap();
    assert_eq!(
        sync_seed::skill_usage(&cfg, true),
        Err(Error::InvalidRequest)
    );
    assert_eq!(own_ops(&cfg).len(), 1);
}

#[test]
fn native_python_receiver_and_original_envelope_relay_interoperate() {
    let (_a, a) = config("machine-a");
    let (t, b) = config("machine-b");
    let (_c, c) = config("machine-c");
    use_n(&a, 2);
    outcome(&a, "success");
    use_n(&b, 1);
    let ops = own_ops(&a);
    let path = t.path().join("fixture-ops.json");
    std::fs::write(&path, json!(ops).to_string()).unwrap();
    let repo = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_owned();
    let script = r#"
import json, os, pathlib, sys
from lore_core.store import db_connect
from lore_core.sync_apply import apply_ops, apply_op_after_approval
from lore_core.sync_oplog import compute_mac
from lore_core import skill_usage_sync
ops = json.loads(pathlib.Path(sys.argv[1]).read_text())
conn = db_connect()
assert all(op['mac'] == compute_mac(op, 'usage-sync-fixture-key') for op in ops)
report = apply_ops(conn, ops, key='usage-sync-fixture-key')
assert report['applied'] == len(ops), report
assert apply_ops(conn, ops)['duplicate'] == len(ops)
bad = dict(ops[-1], op_id='python-foreign-origin', machine_seq=50, lamport=50,
           payload=dict(ops[-1]['payload'], origin='victim'))
bad['mac'] = compute_mac(bad, 'usage-sync-fixture-key')
assert apply_ops(conn,[bad])['failed'] == 1
unsigned = dict(ops[-1], op_id='python-unsigned', machine_seq=51, lamport=51, mac=None)
assert apply_ops(conn,[unsigned])['unverified'] == 1
assert apply_op_after_approval(unsigned) is not None
for i, value in enumerate([True, -1, 1.5, '2', 10**12+1]):
    malformed = dict(ops[-1], op_id='python-malformed-'+str(i), machine_seq=60+i, lamport=60+i,
                     payload=dict(ops[-1]['payload'], uses=value))
    malformed['mac'] = compute_mac(malformed, 'usage-sync-fixture-key')
    assert apply_ops(conn,[malformed])['failed'] == 1
os.environ['LORE_SYNC_CLASSES'] = 'memory'
disabled = dict(ops[-1], op_id='python-disabled', machine_seq=70, lamport=70)
disabled['mac'] = compute_mac(disabled, 'usage-sync-fixture-key')
assert apply_ops(conn,[disabled])['skipped'] == 1
os.environ['LORE_SYNC_CLASSES'] = 'skill_usage'
assert conn.execute("SELECT uses,ok,fail FROM skill_usage_components WHERE origin='machine-a'").fetchall() == [(2,1,0)]
# The compatibility writer still sees local evidence, not aggregated counters.
from lore_core.deriver import load_skill_usage, record_skill_usage
assert load_skill_usage()['fixture']['uses'] == 1
record_skill_usage([('', 'tool', 'Skill: fixture')])
assert load_skill_usage()['fixture']['uses'] == 2
rows = []
for r in conn.execute("SELECT op_id,machine_id,machine_seq,lamport,class,op,project_key,payload,mac FROM sync_ops WHERE applied=1"):
    rows.append(dict(zip(['op_id','machine_id','machine_seq','lamport','class','op','project_key','payload','mac'], r)))
for row in rows: row['payload'] = json.loads(row['payload'])
print(json.dumps(rows))
"#;
    let output = std::process::Command::new("python3")
        .args(["-c", script])
        .arg(&path)
        .current_dir(repo)
        .env("LORE_ROOT", &b.root)
        .env("LORE_SKILLS_DIR", &b.skills)
        .env("LORE_SYNC_CLASSES", "skill_usage")
        .env("LORE_SYNC_HMAC_KEY", KEY)
        .env("LORE_DISABLE_SYNC", "0")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let relayed: Vec<Value> = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(relayed.len(), ops.len() + 1);
    assert_eq!(total(&b), json!([4, 1, 0]));
    assert_eq!(
        sync_apply::apply_ops(&c, &relayed).unwrap()["applied"],
        ops.len() + 1
    );
    // Native reconcile observes the compatibility writer's increment without
    // attributing imported counts to this machine or exporting local legacy.
    assert_eq!(sync_seed::skill_usage(&b, true).unwrap()["seeded"], 1);
    sync_apply::apply_ops(&c, &own_ops(&b)).unwrap();
    assert_eq!(total(&c), total(&b));
    assert_eq!(skills::load_local_usage(&b).unwrap()["fixture"]["uses"], 2);
}

#[test]
fn full_store_reset_refuses_recounting_but_partial_resets_preserve_continuity() {
    let (_t, cfg) = config("local");
    use_n(&cfg, 2);
    let auth = Authority::Interactive {
        agent: "fixture".into(),
        engine: "test".into(),
    };
    lore_core::standalone_ops::reset(&cfg, &json!({"index":true,"beliefs":true}), &auth).unwrap();
    assert_eq!(total(&cfg), json!([2, 0, 0]));
    use_n(&cfg, 1);
    assert_eq!(own_ops(&cfg).last().unwrap()["payload"]["uses"], 3);
    let local = std::fs::read(cfg.root.join("skill_usage.json")).unwrap();
    lore_core::standalone_ops::reset(&cfg, &json!({"all":true}), &auth).unwrap();
    assert_eq!(skills::load_usage(&cfg), Err(Error::Changed));
    assert_eq!(sync_seed::skill_usage(&cfg, false), Err(Error::Changed));
    assert_eq!(sync_seed::skill_usage(&cfg, true), Err(Error::Changed));
    assert_eq!(
        sync_apply::apply_ops(&cfg, &[snapshot("old-author", 1, 1, 3, 0, 0)]).unwrap()["failed"],
        1
    );
    assert!(!table_exists(&cfg));
    assert_eq!(
        std::fs::read(cfg.root.join("skill_usage.json")).unwrap(),
        local
    );
}

#[test]
fn skill_name_storage_and_page_workload_caps_refuse_before_an_extra_effect() {
    let (_t, cfg) = config("local");
    let ops = (0..usage_sync::MAX_SKILLS)
        .map(|i| {
            let mut op = snapshot("origin", i as u64 + 1, 1, 1, 0, 0);
            op["payload"]["name"] = json!(format!("skill-{i}"));
            sign(op)
        })
        .collect::<Vec<_>>();
    assert_eq!(
        sync_apply::apply_ops(&cfg, &ops).unwrap()["applied"],
        usage_sync::MAX_SKILLS
    );
    assert_eq!(
        skills::load_usage(&cfg).unwrap().as_object().unwrap().len(),
        usage_sync::MAX_SKILLS
    );
    assert_eq!(
        sync_apply::apply_ops(&cfg, &[snapshot("origin", 401, 1, 1, 0, 0)]).unwrap()["failed"],
        1
    );
    let too_many = vec![snapshot("origin", 402, 1, 1, 0, 0); sync_apply::MAX_PAGE + 1];
    assert_eq!(sync_apply::apply_ops(&cfg, &too_many), Err(Error::TooLarge));
    let conn = store::read_only(&cfg).unwrap();
    assert_eq!(
        conn.query_row("SELECT count(*) FROM skill_usage_components", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        usage_sync::MAX_SKILLS as i64
    );
}
