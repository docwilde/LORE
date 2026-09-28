//! Standalone graph views share canonical loading, weights and bounded traversal.
use crate::{config::Config, files, graph, store, Error, Result};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    process::{Command, Stdio},
};
fn cap(req: &Value, key: &str, default: u64, max: u64) -> Result<usize> {
    req.get(key)
        .map_or(Some(default), Value::as_u64)
        .filter(|n| *n <= max)
        .map(|n| n as usize)
        .ok_or(Error::InvalidRequest)
}
fn relations(req: &Value) -> Result<Option<BTreeSet<String>>> {
    let values = match req.get("rel") {
        None => return Ok(None),
        Some(Value::String(s)) => vec![s.clone()],
        Some(Value::Array(a)) => a
            .iter()
            .map(|v| v.as_str().map(str::to_owned).ok_or(Error::InvalidRequest))
            .collect::<Result<Vec<_>>>()?,
        _ => return Err(Error::InvalidRequest),
    };
    if values.len() > 16
        || values.iter().any(|v| {
            !graph::ASSERTED.contains(&v.as_str())
                && !matches!(v.as_str(), "supersedes" | "co_derived")
        })
    {
        return Err(Error::InvalidRequest);
    }
    Ok(Some(values.into_iter().collect()))
}
pub fn view(cfg: &Config, req: &Value, mode: &str) -> Result<Value> {
    let conn = store::connect(cfg)?;
    let rels = relations(req)?;
    let statuses = if req["history"] == true {
        vec!["active", "superseded", "retracted", "dormant"]
    } else {
        vec!["active"]
    };
    let (adj, claims) = graph::adjacency(&conn, None, &statuses, rels.as_ref(), true)?;
    let nodes: BTreeSet<_> = claims.keys().copied().collect();
    let id = |field: &str| -> Result<i64> {
        req[field]
            .as_i64()
            .filter(|n| nodes.contains(n))
            .ok_or(Error::Changed)
    };
    match mode {
        "stats" => {
            let mut relations = BTreeMap::<String, usize>::new();
            let mut seen = BTreeSet::new();
            for (src, edges) in &adj {
                for e in edges {
                    let key = if graph::symmetric(&e.rel) {
                        ((*src).min(e.dst), (*src).max(e.dst), e.rel.clone())
                    } else {
                        (*src, e.dst, e.rel.clone())
                    };
                    if seen.insert(key) {
                        *relations.entry(e.rel.clone()).or_default() += 1;
                    }
                }
            }
            Ok(
                json!({"nodes":nodes.len(),"relations":relations,"components":graph::components(&adj,&nodes),"communities":graph::communities(&adj,&nodes,12),"degree":graph::degree(&adj)}),
            )
        }
        "neighbours" => Ok(
            json!({"nodes":graph::khop(&adj,id("belief_id")?,cap(req,"hops",2,8)?,rels.as_ref()),"claims":claims}),
        ),
        "components" => Ok(json!(graph::components(&adj, &nodes))),
        "degree" => Ok(json!(graph::degree(&adj))),
        "communities" => Ok(json!(graph::communities(&adj, &nodes, 12)
            .into_iter()
            .take(cap(req, "limit", 10, 1000)?)
            .collect::<Vec<_>>())),
        "paths" => Ok(json!(graph::simple_paths(
            &adj,
            id("src")?,
            id("dst")?,
            cap(req, "max_hops", 4, 8)?,
            rels.as_ref(),
            256
        )?)),
        "path" => {
            let src = id("src")?;
            let dst = id("dst")?;
            let max = cap(req, "max_hops", 4, 8)?;
            let mut layer = BTreeMap::from([(src, (1.0, Vec::<(i64, String, i64)>::new()))]);
            let mut best = (if src == dst { 1.0 } else { 0.0 }, Vec::new());
            let mut work = 0;
            for _ in 0..max {
                let mut next = BTreeMap::new();
                for (node, (weight, path)) in layer {
                    for edge in adj.get(&node).into_iter().flatten() {
                        work += 1;
                        if work > 100000 {
                            return Err(Error::TooLarge);
                        }
                        let score = weight * edge.weight;
                        let mut hops = path.clone();
                        hops.push((node, edge.rel.clone(), edge.dst));
                        if edge.dst == dst && score > best.0 {
                            best = (score, hops.clone());
                        }
                        if score
                            > next
                                .get(&edge.dst)
                                .map_or(-1.0, |v: &(f64, Vec<(i64, String, i64)>)| v.0)
                        {
                            next.insert(edge.dst, (score, hops));
                        }
                    }
                }
                layer = next;
            }
            Ok(json!({"hops":best.1,"confidence":best.0}))
        }
        "html" => {
            let max = cap(req, "max_nodes", 200, 1000)?;
            let clusters = cap(req, "max_clusters", 12, 1000)?;
            let chosen: BTreeSet<i64> = if req.get("belief_id").is_some() {
                graph::khop(
                    &adj,
                    id("belief_id")?,
                    cap(req, "hops", 2, 8)?,
                    rels.as_ref(),
                )
                .into_keys()
                .take(max)
                .collect()
            } else {
                graph::components(&adj, &nodes)
                    .into_iter()
                    .take(clusters)
                    .flatten()
                    .take(max)
                    .collect()
            };
            let source = graph::mermaid_source(&adj, &claims, &chosen);
            let note = format!("{} of {} beliefs · native LORE", chosen.len(), nodes.len());
            let html = include_str!("graph_html.html")
                .replace("@TITLE@", "LORE graph")
                .replace("@NOTE@", &note)
                .replace("@GRAPH@", &source);
            if html.len() > crate::MAX_FRAME_BYTES {
                return Err(Error::TooLarge);
            }
            let path = match req["out"].as_str() {
                Some(raw) => {
                    let p = Path::new(raw);
                    if p.is_absolute() {
                        p.to_path_buf()
                    } else {
                        std::env::current_dir()?.join(p)
                    }
                }
                None => cfg.root.join("graph.html"),
            };
            if req["out"].is_string() {
                // An explicit output never replaces existing state or files.
                let parent = path.parent().ok_or(Error::UnsafePath)?;
                let temporary = parent.join(format!(
                    ".lore-graph-{}.html",
                    uuid::Uuid::new_v4().simple()
                ));
                files::atomic_write(&temporary, html.as_bytes())?;
                let directory = files::open_directory(parent)?;
                let rename = files::rename_at(
                    &directory,
                    temporary.file_name().ok_or(Error::UnsafePath)?,
                    &directory,
                    path.file_name().ok_or(Error::UnsafePath)?,
                    false,
                );
                if rename.is_err() {
                    let _ = files::unlink_at(
                        &directory,
                        temporary.file_name().ok_or(Error::UnsafePath)?,
                    );
                }
                rename?;
            } else {
                files::atomic_write(&path, html.as_bytes())?;
            }
            if req["no_open"] != true {
                let _ = Command::new("xdg-open")
                    .arg(&path)
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn();
            }
            Ok(
                json!({"path":path,"nodes":chosen.len(),"total":nodes.len(),"mermaid":if req["mermaid"]==true{Some(source)}else{None}}),
            )
        }
        _ => Err(Error::InvalidRequest),
    }
}
/// CLI relation readback admits history and global subjects. Mutation and host
/// review APIs keep their separate canonical identity and scope checks.
pub fn edges(cfg: &Config, req: &Value) -> Result<Value> {
    let id = req["belief_id"].as_i64().ok_or(Error::InvalidRequest)?;
    let conn = store::connect(cfg)?;
    let exists = conn.query_row("SELECT count(*) FROM beliefs WHERE id=?", [id], |r| {
        r.get::<_, i64>(0)
    })?;
    if exists != 1 {
        return Err(Error::Changed);
    }
    let mut rows = Vec::new();
    let mut budget = 8 * 1024 * 1024;
    for (direction,sql) in [("out","SELECT e.src,e.dst,e.rel,e.source,e.session_id,e.note,b.subject,b.claim,b.status FROM belief_edges e JOIN beliefs b ON b.id=e.dst WHERE e.src=? ORDER BY e.rel,e.dst LIMIT 1001"),("in","SELECT e.src,e.dst,e.rel,e.source,e.session_id,e.note,b.subject,b.claim,b.status FROM belief_edges e JOIN beliefs b ON b.id=e.src WHERE e.dst=? ORDER BY e.rel,e.src LIMIT 1001")] {
        let mut stmt=conn.prepare(sql)?;let mut cursor=stmt.query([id])?;
        while let Some(row)=cursor.next()?{if rows.len()>=1000{return Err(Error::TooLarge)}let src=row.get::<_,i64>(0)?;let dst=row.get::<_,i64>(1)?;let rel=graph::db_text(row,2,32,&mut budget)?;rows.push(json!({"direction":direction,"src":src,"dst":dst,"rel":crate::scrub::scrub(&rel)?,"source":crate::scrub::scrub(&graph::db_text(row,3,64,&mut budget)?)?,"session_id":graph::db_optional_text(row,4,134,&mut budget)?.map(|s|crate::scrub::scrub(&s)).transpose()?,"note":graph::db_optional_text(row,5,65536,&mut budget)?.map(|s|crate::scrub::scrub(&s)).transpose()?,"subject":crate::scrub::scrub(&graph::db_text(row,6,4096,&mut budget)?)?,"claim":crate::scrub::scrub(&graph::db_text(row,7,65536,&mut budget)?)?,"status":graph::db_text(row,8,32,&mut budget)?,"independent_assertions":graph::edge_support(&conn,src,dst,&rel)?}));}
    }
    Ok(json!(rows))
}
