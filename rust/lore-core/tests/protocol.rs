use lore_core::{config::Config,store};
use serde_json::{json,Value};

#[test]
fn adopts_every_existing_sync_protocol_golden_without_regenerating_it() {
    let fixtures=std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/sync_protocol");
    let mut count=0;
    for entry in std::fs::read_dir(fixtures).unwrap() {
        let path=entry.unwrap().path();if path.extension().is_none_or(|s|s!="json"){continue;}
        let fixture:Value=serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        let op=&fixture["op"];
        let tuple=json!([op["op_id"],op["machine_id"],op["machine_seq"],op["lamport"],op["class"],op["op"],op["project_key"],op["payload"]]);
        let bytes=store::canonical_bytes(&tuple).unwrap();
        let hex:String=bytes.iter().map(|b|format!("{b:02x}")).collect();
        assert_eq!(hex,fixture["canonical_bytes_hex"].as_str().unwrap(),"{}",path.display());
        assert_eq!(store::canonical_mac(&tuple,"lore-sync-protocol-test-key-DO-NOT-USE-IN-PRODUCTION").unwrap(),fixture["expected_mac_hex"].as_str().unwrap(),"{}",path.display());
        count+=1;
    }
    assert!(count>=9);
}

#[test]
fn floating_point_mac_encoding_matches_python_at_boundaries_and_random_values() {
    use std::io::Write;
    use std::process::{Command,Stdio};
    let mut values=vec![json!(1e-7),json!(1e-5),json!(1e-4),json!(1e15),json!(1e16),json!(1e20),json!(-0.0),json!(0.87)];
    let mut seed=0x9e3779b97f4a7c15u64;
    for _ in 0..2000 {seed^=seed<<13;seed^=seed>>7;seed^=seed<<17;let f=f64::from_bits(seed);if f.is_finite(){values.push(json!(f));}}
    let values=Value::Array(values);
    let mut child=Command::new("python3").args(["-I","-c","import json,sys; print(json.dumps(json.load(sys.stdin),sort_keys=True,separators=(',',':'),ensure_ascii=False))"])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap();
    let mut input=child.stdin.take().unwrap();input.write_all(&serde_json::to_vec(&values).unwrap()).unwrap();drop(input);
    let output=child.wait_with_output().unwrap();assert!(output.status.success());
    let native=store::canonical_bytes(&values).unwrap();
    assert_eq!(String::from_utf8(native).unwrap(),String::from_utf8(output.stdout).unwrap().trim_end());
}

#[test]
fn mutation_and_signed_clock_roll_back_together_and_next_sequence_has_no_gap() {
    let temp=tempfile::tempdir().unwrap();let mut cfg=Config::for_root(temp.path().join("store"));cfg.sync.enabled=true;cfg.sync.key=Some("public-fixture-key".into());
    let mut db=store::connect(&cfg).unwrap();
    {
        let tx=db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).unwrap();
        tx.execute("INSERT INTO beliefs(subject,claim,confidence) VALUES('user','rollback',0.8)",[]).unwrap();
        store::append_op(&cfg,&tx,"belief","insert",None,&json!({"claim":"rollback"})).unwrap();
        // Deliberately abandon the entire caller's mutation transaction.
    }
    assert_eq!(db.query_row("SELECT count(*) FROM beliefs",[],|r|r.get::<_,i64>(0)).unwrap(),0);
    assert_eq!(db.query_row("SELECT count(*) FROM sync_ops",[],|r|r.get::<_,i64>(0)).unwrap(),0);
    let tx=db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate).unwrap();
    store::append_op(&cfg,&tx,"belief","insert",None,&json!({"claim":"fixture","confidence":1e-7})).unwrap();tx.commit().unwrap();
    assert_eq!(db.query_row("SELECT machine_seq FROM sync_ops",[],|r|r.get::<_,i64>(0)).unwrap(),1);
    assert_eq!(db.query_row("SELECT lamport FROM sync_machine",[],|r|r.get::<_,i64>(0)).unwrap(),1);
    assert!(store::append_op(&cfg,&db,"belief","insert",None,&json!({})).is_err());
}

#[cfg(unix)]
#[test]
fn filesystem_refuses_fifo_hardlink_and_directory_link_without_clobbering_target() {
    use std::{os::{unix::{fs::symlink,ffi::OsStrExt}},ffi::CString};
    let temp=tempfile::tempdir().unwrap();let source=temp.path().join("source");std::fs::write(&source,"fixture").unwrap();
    let link=temp.path().join("link");std::fs::hard_link(&source,&link).unwrap();
    assert!(lore_core::files::read_regular(&source,1024).is_err());assert!(lore_core::files::atomic_write(&link,b"replace").is_err());
    let fifo=temp.path().join("fifo");let name=CString::new(fifo.as_os_str().as_bytes()).unwrap();assert_eq!(unsafe{libc::mkfifo(name.as_ptr(),0o600)},0);
    assert!(lore_core::files::read_regular(&fifo,1024).is_err());
    let actual=temp.path().join("actual");std::fs::create_dir(&actual).unwrap();let dir=temp.path().join("dir");symlink(&actual,&dir).unwrap();
    assert!(lore_core::files::atomic_write(&dir.join("secret"),b"fixture").is_err());assert!(!actual.join("secret").exists());
    assert_eq!(std::fs::read_to_string(source).unwrap(),"fixture");
}
