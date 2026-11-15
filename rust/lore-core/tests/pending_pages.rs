use lore_core::{config::Config, files, pending, pending_page, Error};
use serde_json::json;
use std::collections::BTreeSet;

fn fixture(count: usize) -> (tempfile::TempDir, Config) {
    let temp = tempfile::tempdir().unwrap();
    files::private_dir(&temp.path().join(".git")).unwrap();
    let cfg = Config::for_root(temp.path().join("store"));
    files::private_dir(&cfg.root.join("pending")).unwrap();
    for n in 0..count {
        let item = json!({"kind":"memory","scope":"user","action":"add","text":format!("Fixture proposal {n}")});
        std::fs::write(
            cfg.root
                .join("pending")
                .join(format!("fixture-{n:05}.json")),
            item.to_string(),
        )
        .unwrap();
    }
    (temp, cfg)
}

#[test]
fn large_queue_is_read_in_bounded_pages_without_duplicates_or_mutations() {
    let (temp, cfg) = fixture(4496);
    let before: Vec<_> = (0..4496)
        .map(|n| {
            std::fs::read(
                cfg.root
                    .join("pending")
                    .join(format!("fixture-{n:05}.json")),
            )
            .unwrap()
        })
        .collect();
    assert_eq!(pending::ids(&cfg), Err(Error::TooLarge));
    let mut req = json!({"cwd":temp.path(),"limit":37});
    let mut seen = BTreeSet::new();
    let mut pages = 0;
    loop {
        let page = pending_page::list(&cfg, &req).unwrap_or_else(|error| {
            panic!(
                "page {pages}: {error:?}, scope={}, dir={:?}",
                lore_core::config::project_slug(temp.path()),
                std::fs::metadata(cfg.root.join("pending"))
            )
        });
        pages += 1;
        assert!(page["items"].as_array().unwrap().len() <= 37);
        assert!(page["scanned"].as_u64().unwrap() <= 256);
        assert!(serde_json::to_vec(&page).unwrap().len() < 64 * 1024);
        assert_eq!(page["page_complete"], true);
        for row in page["items"].as_array().unwrap() {
            assert!(seen.insert(row["pid"].as_str().unwrap().to_owned()));
        }
        if page["has_more"] == false {
            assert!(page["next_cursor"].is_null());
            break;
        }
        req["cursor"] = page["next_cursor"].clone();
        assert!(pages < 150);
    }
    assert_eq!(seen.len(), 4496);
    assert!(pages > 100);
    for (n, bytes) in before.iter().enumerate() {
        assert_eq!(
            *bytes,
            std::fs::read(
                cfg.root
                    .join("pending")
                    .join(format!("fixture-{n:05}.json"))
            )
            .unwrap()
        );
    }
    assert_eq!(pending::ids(&cfg), Err(Error::TooLarge));
    assert!(!cfg.root.join("USER.md").exists());
}

#[test]
fn continuation_rejects_tampering_scope_changes_and_namespace_changes() {
    let (temp, cfg) = fixture(3);
    let mut req = json!({"cwd":temp.path(),"limit":1});
    let page = pending_page::list(&cfg, &req).unwrap();
    let cursor = page["next_cursor"].as_str().unwrap();
    let mut tampered = cursor.to_owned();
    let last = tampered.pop().unwrap();
    tampered.push(if last == '0' { '1' } else { '0' });
    req["cursor"] = json!(tampered);
    assert_eq!(pending_page::list(&cfg, &req), Err(Error::InvalidRequest));
    req["cursor"] = json!(cursor);
    let foreign = tempfile::tempdir().unwrap();
    files::private_dir(&foreign.path().join(".git")).unwrap();
    req["cwd"] = json!(foreign.path());
    assert_eq!(pending_page::list(&cfg, &req), Err(Error::Changed));
    req["cwd"] = json!(temp.path());
    std::fs::write(cfg.root.join("pending/new.json"), b"{}").unwrap();
    assert_eq!(pending_page::list(&cfg, &req), Err(Error::Changed));
}

#[test]
fn page_filters_project_proposals_and_reports_unavailable_entries() {
    let (temp, cfg) = fixture(1);
    let own = temp.path().join("own");
    let foreign = temp.path().join("foreign");
    files::private_dir(&own.join(".git")).unwrap();
    files::private_dir(&foreign.join(".git")).unwrap();
    for (id, cwd) in [("own", &own), ("foreign", &foreign)] {
        let item = json!({"kind":"memory","scope":"project","project":lore_core::config::project_slug(cwd),"text":format!("{id} proposal")});
        std::fs::write(
            cfg.root.join("pending").join(format!("{id}.json")),
            item.to_string(),
        )
        .unwrap();
    }
    std::fs::write(cfg.root.join("pending/broken.json"), b"not json").unwrap();
    std::fs::write(cfg.root.join("pending/bad stem.json"), b"{}").unwrap();
    let page = pending_page::list(&cfg, &json!({"cwd":own})).unwrap();
    let ids: BTreeSet<_> = page["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["pid"].as_str().unwrap())
        .collect();
    assert_eq!(ids, BTreeSet::from(["fixture-00000", "own"]));
    assert_eq!(page["page_complete"], false);
    assert_eq!(page["unavailable_count"], 2);
    assert_eq!(page["has_more"], false);
}

#[test]
fn oversized_preview_is_explicit_and_cannot_block_later_items() {
    let (temp, cfg) = fixture(2);
    std::fs::write(
        cfg.root.join("pending/oversized.json"),
        json!({"kind":"memory","scope":"user","text":"ordinary fixture words ".repeat(2500)})
            .to_string(),
    )
    .unwrap();
    let page = pending_page::list(&cfg, &json!({"cwd":temp.path()})).unwrap();
    assert_eq!(page["items"].as_array().unwrap().len(), 2);
    assert_eq!(page["unavailable_count"], 1);
    assert_eq!(page["unavailable"][0]["pid"], "oversized");
    assert_eq!(page["unavailable"][0]["error"], "output_too_large");
    assert!(serde_json::to_vec(&page).unwrap().len() < 64 * 1024);
    assert!(pending::snapshot(&cfg.root.join("pending/oversized.json")).is_ok());
}

#[test]
fn missing_queue_stays_lazy_and_invalid_limits_refuse() {
    let temp = tempfile::tempdir().unwrap();
    let cfg = Config::for_root(temp.path().join("missing"));
    let page = pending_page::list(&cfg, &json!({"cwd":temp.path()})).unwrap();
    assert_eq!(page["items"], json!([]));
    assert_eq!(page["has_more"], false);
    assert!(!cfg.root.exists());
    for limit in [json!(0), json!(51), json!("1"), json!(-1)] {
        assert_eq!(
            pending_page::list(&cfg, &json!({"cwd":temp.path(),"limit":limit})),
            Err(Error::InvalidRequest)
        );
    }
}

#[test]
fn cli_falls_back_to_pages_while_full_namespace_consumers_stay_strict() {
    let (temp, cfg) = fixture(4100);
    let req = json!({"cwd":temp.path(),"limit":10});
    let page = lore_core::standalone_ops::pending_list(&cfg, &req).unwrap();
    assert_eq!(page["items"].as_array().unwrap().len(), 10);
    assert_eq!(page["has_more"], true);
    assert_eq!(
        lore_core::standalone_ops::pending_list(&cfg, &json!({"cwd":temp.path(),"all":true})),
        Err(Error::TooLarge)
    );
    let summary =
        pending::for_sessions(&cfg, &json!({"cwd":temp.path(),"session_ids":["fixture"]})).unwrap();
    assert_eq!(summary["complete"], false);
    assert_eq!(summary["sessions"][0]["complete"], false);
}
