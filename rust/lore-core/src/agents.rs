//! Model tools share native storage and graph algorithms with the human UI.
//! The carrier binds identity once; model arguments cannot change attribution.
use std::{collections::BTreeMap, path::Path, sync::OnceLock};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use crate::{beliefs, config::{self, Config}, gate, graph, memory::{self, Scope}, pending, scrub, store, Error, Result};

const FRAME_CAP: usize = 64 * 1024 - 512;

#[derive(Default)]
pub struct AgentOperators {
    identity: Option<Value>,
    failures: BTreeMap<String, u8>,
}

fn catalog() -> &'static Vec<Value> {
    static TOOLS: OnceLock<Vec<Value>> = OnceLock::new();
    TOOLS.get_or_init(|| serde_json::from_str(include_str!("agent_catalog.json")).expect("bundled operator schemas"))
}

impl AgentOperators {
    fn bind(&mut self, identity: &Value) -> Result<()> {
        let object = identity.as_object().filter(|o| o.len() == 5).ok_or(Error::InvalidRequest)?;
        if !["session_id", "cwd", "source_engine", "spawn_depth", "lore"].iter().all(|k| object.contains_key(*k)) {
            return Err(Error::InvalidRequest);
        }
        let cwd = identity["cwd"].as_str().ok_or(Error::InvalidRequest)?;
        if cwd.len() > 4096 || cwd.chars().any(char::is_control) || !Path::new(cwd).is_absolute()
            || !Path::new(cwd).is_dir()
            || !identity["session_id"].as_str().is_some_and(config::valid_id)
            || !matches!(identity["source_engine"].as_str(), Some("codex" | "claude" | "deepseek" | "glm"))
            || !identity["spawn_depth"].as_u64().is_some_and(|n| n <= 128)
            || identity["lore"] != true {
            return Err(Error::InvalidRequest);
        }
        if let Some(bound) = &self.identity {
            if bound != identity { return Err(Error::Untrusted); }
        } else { self.identity = Some(identity.clone()); }
        Ok(())
    }

    fn disabled(&self) -> Vec<String> {
        self.failures.iter().filter(|(_, n)| **n >= 2).map(|(name, _)| name.clone()).collect()
    }

    pub fn execute(&mut self, cfg: &Config, req: &Value) -> Result<Value> {
        if serde_json::to_vec(req).map_err(|_| Error::InvalidRequest)?.len() > FRAME_CAP { return Err(Error::TooLarge); }
        if self.identity.is_none() && req["op"] != "agent_catalog_v1" { return Err(Error::Untrusted); }
        self.bind(&req["identity"])?;
        match req["op"].as_str() {
            Some("agent_catalog_v1") => Ok(json!(catalog().iter().filter(|t| !self.disabled().contains(&t["name"].as_str().unwrap().to_owned())).collect::<Vec<_>>())),
            Some("agent_status_v1") => {
                let count = store::connect(cfg).ok().and_then(|conn| conn.query_row("SELECT count(*) FROM beliefs WHERE status='active'", [], |r| beliefs::sql_count(r, 0)).ok());
                Ok(json!({"belief_count": count, "disabled_tools": self.disabled()}))
            }
            Some("agent_tool_v1") => {
                let name = req["name"].as_str().ok_or(Error::InvalidRequest)?;
                let tool = catalog().iter().find(|tool| tool["name"] == name).ok_or(Error::Unsupported)?;
                if self.failures.get(name).copied().unwrap_or(0) >= 2 {
                    return Ok(json!({"error": "LORE operator is unavailable in this session"}));
                }
                if !valid_arguments(&tool["inputSchema"], &req["arguments"]) {
                    return Ok(json!({"error": format!("{name}: invalid arguments")}));
                }
                let identity = self.identity.as_ref().ok_or(Error::Untrusted)?;
                let result = dispatch(cfg, identity, name, &req["arguments"]);
                let value = match result {
                    Ok(value) => value,
                    Err(Error::InvalidRequest | Error::Changed | Error::Untrusted | Error::OverCap) => json!({"error": format!("{name}: request refused")}),
                    Err(error) => {
                        let count = self.failures.entry(name.to_owned()).or_default();
                        *count = count.saturating_add(1).min(2);
                        json!({"error": format!("{name} failed: {}", error.code())})
                    }
                };
                let clean = scrub::scrub_json(&value)?;
                if serde_json::to_vec(&clean).map_err(|_| Error::Unavailable)?.len() > FRAME_CAP { return Err(Error::TooLarge); }
                Ok(clean)
            }
            _ => Err(Error::Unsupported),
        }
    }
}

fn valid_arguments(schema: &Value, args: &Value) -> bool {
    let Some(args) = args.as_object() else { return false; };
    let Some(properties) = schema["properties"].as_object() else { return false; };
    if schema["required"].as_array().is_some_and(|required| required.iter().any(|key| !key.as_str().is_some_and(|key| args.contains_key(key)))) { return false; }
    args.iter().all(|(key, value)| {
        let Some(spec) = properties.get(key) else { return false; };
        let valid = match spec["type"].as_str() {
            Some("string") => value.as_str().is_some_and(|s| s.len() <= 32768 && !s.contains('\0')),
            Some("integer") => value.as_i64().is_some_and(|n| spec["minimum"].as_i64().is_none_or(|min| n >= min) && spec["maximum"].as_i64().is_none_or(|max| n <= max)),
            _ => false,
        };
        valid && spec["enum"].as_array().is_none_or(|choices| choices.contains(value))
    })
}

fn dispatch(cfg: &Config, identity: &Value, name: &str, args: &Value) -> Result<Value> {
    match name {
        "lore_belief_search" => belief_search(cfg, args),
        "lore_belief_show" => belief_show(cfg, args),
        "lore_belief_neighbours" => neighbours(cfg, args),
        "lore_memory_list" => memory_list(cfg, identity, args),
        "lore_session_search" => session_search(cfg, identity, args),
        "lore_remember" => remember(cfg, identity, args),
        _ => Err(Error::Unsupported),
    }
}

fn rounded(value: f64, digits: u32) -> f64 {
    let factor = 10f64.powi(digits as i32);
    (value * factor).round_ties_even() / factor
}

fn belief_search(cfg:&Config,args:&Value)->Result<Value>{
    let query=args["query"].as_str().ok_or(Error::InvalidRequest)?;let limit=args["limit"].as_i64().unwrap_or(8);let conn=store::connect(cfg)?;
    for op in [" "," OR "]{let expr=crate::index::fts_expr(query,op);if expr.is_empty(){return Err(Error::InvalidRequest);}
        let mut stmt=conn.prepare("SELECT b.id,b.subject,b.claim,b.confidence,b.status,b.source_engine,(SELECT count(*) FROM belief_evidence e WHERE e.belief_id=b.id) FROM beliefs b JOIN belief_fts f ON b.id=f.belief_id WHERE belief_fts MATCH ? AND b.status='active' ORDER BY bm25(belief_fts) LIMIT ?")?;
        let mut query=stmt.query(params![expr,limit])?;let mut rows=Vec::new();let mut budget=FRAME_CAP;
        while let Some(row)=query.next()?{let id=row.get::<_,i64>(0)?;let subject=graph::db_text(row,1,4096,&mut budget)?;let claim=graph::db_text(row,2,FRAME_CAP,&mut budget)?;
            let confidence=row.get::<_,f64>(3)?;let status=graph::db_text(row,4,32,&mut budget)?;let engine=graph::db_optional_text(row,5,32,&mut budget)?;let count=beliefs::sql_count(row,6)?;
            let mut row=json!({"id":id,"subject":subject,"claim":claim,"confidence":rounded(confidence,2),"status":status,"evidence_count":count});if let Some(engine)=engine.filter(|s|!s.is_empty()){row["source_engine"]=json!(engine);}rows.push(row);
        }if !rows.is_empty(){return Ok(json!({"count":rows.len(),"beliefs":rows}));}
    }Ok(json!({"beliefs":[],"count":0,"note":"no matching active beliefs"}))
}

fn citation(conn: &Connection, id: i64, claim: &str) -> Result<Value> {
    let prior = conn.query_row("SELECT confidence FROM beliefs WHERE id=?", [id], |r| r.get::<_, f64>(0))?;
    let (confirmed, contradicted, stale) = beliefs::outcome_counts(conn, id)?;
    let count = confirmed.saturating_add(contradicted).saturating_add(stale);
    Ok(if count >= 3 {
        json!({"id": id,"claim": claim,"citation_status": "steer","calibrated_confidence": rounded(beliefs::calibrated_confidence(prior,confirmed,contradicted),2),"outcome_count": count})
    } else {
        json!({"id": id,"claim": claim,"citation_status": "cite_only","confidence": rounded(prior,2),"outcome_count": count})
    })
}

fn belief_show(cfg:&Config,args:&Value)->Result<Value>{
    let id=beliefs::id(args,"belief_id")?;let conn=store::connect(cfg)?;let mut budget=FRAME_CAP;
    let mut stmt=conn.prepare("SELECT subject,claim,confidence,status,source_engine FROM beliefs WHERE id=?")?;let mut rows=stmt.query([id])?;let row=rows.next()?.ok_or(Error::Changed)?;
    let subject=graph::db_text(row,0,4096,&mut budget)?;let claim=graph::db_text(row,1,FRAME_CAP,&mut budget)?;let prior=row.get::<_,f64>(2)?;
    let status=graph::db_text(row,3,32,&mut budget)?;let engine=graph::db_optional_text(row,4,32,&mut budget)?;drop(rows);drop(stmt);
    let mut evidence=Vec::new();let mut stmt=conn.prepare("SELECT session_id,project,note,created,source_engine FROM belief_evidence WHERE belief_id=? ORDER BY created,rowid LIMIT 4097")?;let mut rows=stmt.query([id])?;
    while let Some(row)=rows.next()?{if evidence.len()==4096{return Err(Error::TooLarge);}let sid=graph::db_optional_text(row,0,134,&mut budget)?;let project=graph::db_optional_text(row,1,2048,&mut budget)?;
        let note=graph::db_optional_text(row,2,4096,&mut budget)?;let created=graph::db_optional_text(row,3,128,&mut budget)?;let engine=graph::db_optional_text(row,4,32,&mut budget)?;
        let mut row=json!({"session_id":sid,"project":project,"note":note,"created":created});if let Some(engine)=engine{row["source_engine"]=json!(engine);}evidence.push(row);
    }drop(rows);drop(stmt);
    let mut edges=Vec::new();let mut stmt=conn.prepare("SELECT e.src,e.dst,e.rel,e.source,b.id,b.claim,b.status FROM belief_edges e JOIN beliefs b ON b.id=CASE WHEN e.src=? THEN e.dst ELSE e.src END WHERE e.src=? OR e.dst=? ORDER BY e.rel,b.id LIMIT 4097")?;let mut rows=stmt.query(params![id,id,id])?;
    while let Some(row)=rows.next()?{if edges.len()==4096{return Err(Error::TooLarge);}let src=row.get::<_,i64>(0)?;let dst=row.get::<_,i64>(1)?;let rel=graph::db_text(row,2,32,&mut budget)?;let source=graph::db_text(row,3,32,&mut budget)?;
        let other=row.get::<_,i64>(4)?;let claim=graph::db_text(row,5,FRAME_CAP,&mut budget)?;let status=graph::db_text(row,6,32,&mut budget)?;
        edges.push(json!({"direction":if src==id{"out"}else{"in"},"verb":rel,"belief_id":other,"claim":claim,"status":status,"source":source,"support":graph::edge_support(&conn,src,dst,&rel)?}));
    }
    let(c,d,s)=beliefs::outcome_counts(&conn,id)?;let mut belief=json!({"id":id,"subject":subject,"claim":claim,"confidence":rounded(prior,2),"calibrated_confidence":rounded(beliefs::calibrated_confidence(prior,c,d),2),"status":status});if let Some(engine)=engine{belief["source_engine"]=json!(engine);}
    Ok(json!({"belief":belief,"evidence":evidence,"outcomes":{"confirmed":c,"contradicted":d,"stale":s},"edges":edges}))
}

fn neighbours(cfg:&Config,args:&Value)->Result<Value> {
    let id=beliefs::id(args,"belief_id")?;
    let conn=store::connect(cfg)?;
    let(adj,claims)=graph::adjacency(&conn,None,&["active"],None,true)?;
    let claim=claims.get(&id).ok_or(Error::Changed)?;
    let seed=citation(&conn,id,claim)?;
    if args.get("to_id").is_some() {
        let target=beliefs::id(args,"to_id")?;
        let target_claim=claims.get(&target).ok_or(Error::Changed)?;
        let(path,confidence)=graph::best_path(&adj,id,target,None);
        if path.is_empty() {return Ok(json!({"mode":"path","seed":seed,"target":citation(&conn,target,target_claim)?,"path":[],"confidence":0.0,"hop_count":0,"note":"no path between these beliefs in the active graph"}));}
        let mut ids=vec![path[0].0];let mut hops=Vec::new();
        for(src,rel,dst)in &path {ids.push(*dst);let projected=rel=="co_derived";let mut hop=json!({"src":src,"verb":rel,"dst":dst,"projected":projected});if !projected{hop["support"]=json!(graph::edge_support(&conn,*src,*dst,rel)?);}hops.push(hop);}
        let others=graph::simple_paths(&adj,id,target,path.len()+1,None,64)?;
        let tags=ids.iter().map(|id|citation(&conn,*id,&claims[id])).collect::<Result<Vec<_>>>()?;
        return Ok(json!({"mode":"path","path":hops,"confidence":rounded(confidence,4),"hop_count":path.len(),"beliefs":tags,"other_paths_exist":others.len()>1,"note":"Path confidence is the product over hops. Structure earns no citation authority; each belief has its own citation_status."}));
    }
    let hops=args["hops"].as_u64().unwrap_or(1) as usize;
    let limit=args["limit"].as_u64().unwrap_or(12) as usize;
    let reached=graph::khop(&adj,id,hops,None);
    let mut others=reached.iter().filter(|(other,_)|**other!=id).map(|(id,distance)|(*id,*distance)).collect::<Vec<_>>();
    others.sort_by_key(|(id,distance)|(*distance,*id));
    let mut neighbours=Vec::new();
    for(other,distance)in others.iter().take(limit) {
        let(path,confidence)=graph::best_path(&adj,id,*other,None);
        let rel=path.last().map(|hop|hop.1.as_str()).unwrap_or("?");
        let mut tag=citation(&conn,*other,&claims[other])?;
        tag["hop_distance"]=json!(distance);tag["path_confidence"]=json!(rounded(confidence,4));tag["via_relation"]=json!(rel);tag["relation_projected"]=json!(rel=="co_derived");neighbours.push(tag);
    }
    Ok(json!({"mode":"neighbourhood","seed":seed,"hops":hops,"count":neighbours.len(),"neighbours":neighbours,"reachable_total":others.len(),"limit":limit,"truncated":others.len()>limit,"note":"Structure earns no citation authority. Every citation_status belongs to its own belief; path confidence is the product over hops."}))
}

fn memory_list(cfg:&Config,identity:&Value,args:&Value)->Result<Value> {
    let slug=config::project_slug(gate::cwd(identity)?);
    let scope=args["scope"].as_str().unwrap_or("all");
    let mut out=json!({"project_slug":slug});
    for sc in [Scope::User,Scope::Project] {
        if scope=="all"||scope==sc.name() {let entries=memory::read_entries(&sc.path(cfg,&slug)?)?;out[sc.name()]=json!({"usage":memory::usage_line(&entries,sc.cap(cfg)),"entries":entries});}
    }
    Ok(out)
}

fn session_search(cfg:&Config,identity:&Value,args:&Value)->Result<Value>{
    let query=args["query"].as_str().ok_or(Error::InvalidRequest)?;let limit=args["limit"].as_i64().unwrap_or(6);let slug=config::project_slug(gate::cwd(identity)?);let conn=store::connect(cfg)?;
    for scope in [Some(slug.as_str()),None]{for op in [" "," OR "]{let expr=crate::index::fts_expr(query,op);if expr.is_empty(){return Err(Error::InvalidRequest);}
        let mut stmt=conn.prepare("SELECT m.session_id,m.project,m.ts,m.role,snippet(msg,4,'[',']','…',16),(SELECT engine FROM sessions s WHERE s.session_id=m.session_id) FROM msg m WHERE msg MATCH ? AND m.project IS NOT NULL AND (? IS NULL OR m.project=?) ORDER BY bm25(msg) LIMIT ?")?;
        let mut query=stmt.query(params![expr,scope,scope,limit])?;let mut hits=Vec::new();let mut budget=FRAME_CAP;
        while let Some(row)=query.next()?{let Some(project)=graph::db_optional_text(row,1,2048,&mut budget)?else{continue};
            let sid=graph::db_text(row,0,134,&mut budget)?;let ts=graph::db_text(row,2,128,&mut budget)?;let role=graph::db_text(row,3,32,&mut budget)?;
            let snippet=graph::db_text(row,4,16384,&mut budget)?;let engine=graph::db_optional_text(row,5,32,&mut budget)?;
            let mut hit=json!({"session_id":sid,"project":project,"ts":ts,"role":role,"snippet":beliefs::crop(&gate::one_line(&snippet),280)});if let Some(engine)=engine{hit["engine"]=json!(engine);}hits.push(hit);
        }if !hits.is_empty(){return Ok(json!({"scope":if scope.is_some(){"project"}else{"all"},"count":hits.len(),"hits":hits}));}
    }}Ok(json!({"scope":"all","hits":[],"count":0,"note":"no hits in the session index"}))
}

fn remember(cfg:&Config,identity:&Value,args:&Value)->Result<Value> {
    let text=beliefs::crop(&gate::one_line(&scrub::scrub(args["text"].as_str().ok_or(Error::InvalidRequest)?)?),300);
    if text.is_empty(){return Err(Error::InvalidRequest);}
    let slug=config::project_slug(gate::cwd(identity)?);let folded=text.to_lowercase();
    for scope in [Scope::User,Scope::Project] {if memory::read_entries(&scope.path(cfg,&slug)?)?.iter().any(|entry|entry.to_lowercase()==folded){return Ok(json!({"staged":null,"note":"already in curated memory or pending review -- nothing staged"}));}}
    for id in pending::ids(cfg)? {
        let snapshot=pending::snapshot(&cfg.root.join("pending").join(format!("{id}.json")))?;
        let item:Value=serde_json::from_str(&snapshot.raw).map_err(|_|Error::Unavailable)?;
        if gate::pending_project(&item).is_none_or(|p|p==slug)&&item["text"].as_str().is_some_and(|s|s.to_lowercase()==folded){return Ok(json!({"staged":null,"note":"already in curated memory or pending review -- nothing staged"}));}
    }
    let scope=args["scope"].as_str().unwrap_or("project");
    let authority=gate::Authority::Model{agent:"doxa-tool".into(),engine:identity["source_engine"].as_str().ok_or(Error::Untrusted)?.into(),session_id:identity["session_id"].as_str().ok_or(Error::Untrusted)?.into()};
    let id=gate::stage(cfg,&json!({"kind":"memory","scope":scope,"action":"add","match":"","text":text,"project":slug,"session_id":identity["session_id"]}),&authority)?;
    Ok(json!({"staged":id,"scope":scope,"text":text,"note":"staged as a pending proposal -- nothing enters curated memory until a human approves it"}))
}

#[cfg(test)]
mod tests {
    #[test] fn oversized_database_claim_and_evidence_refuse_while_normal_show_remains() {
        let temp=tempfile::tempdir().unwrap();let cfg=Config::for_root(temp.path().join("lore"));let conn=store::connect(&cfg).unwrap();
        conn.execute("INSERT INTO beliefs(subject,claim,confidence,status,created,updated,uid) VALUES('user',?,0.8,'active','2026-01-01','2026-01-01','fixture')",["x".repeat(FRAME_CAP+1)]).unwrap();
        assert!(matches!(belief_show(&cfg,&json!({"belief_id":1})),Err(Error::TooLarge)));
        conn.execute("UPDATE beliefs SET claim='ordinary café'",[]).unwrap();conn.execute("INSERT INTO belief_evidence(belief_id,note,created) VALUES(1,?,'2026-01-01')",["x".repeat(4097)]).unwrap();
        assert!(matches!(belief_show(&cfg,&json!({"belief_id":1})),Err(Error::TooLarge)));
        conn.execute("UPDATE belief_evidence SET note='owned evidence'",[]).unwrap();assert_eq!(belief_show(&cfg,&json!({"belief_id":1})).unwrap()["belief"]["claim"],"ordinary café");
    }
    #[test] fn null_remote_project_does_not_poison_global_agent_search_or_displace_known_hit() {
        let temp=tempfile::tempdir().unwrap();let cfg=Config::for_root(temp.path().join("lore"));let conn=store::connect(&cfg).unwrap();
        conn.execute("INSERT INTO sessions(session_id,project,engine) VALUES('remote',NULL,'codex')",[]).unwrap();conn.execute("INSERT INTO msg(session_id,project,ts,role,content) VALUES('remote',NULL,'','user','fixture unicorn')",[]).unwrap();
        conn.execute("INSERT INTO sessions(session_id,project,engine) VALUES('known','known-project','claude')",[]).unwrap();conn.execute("INSERT INTO msg(session_id,project,ts,role,content) VALUES('known','known-project','','user','fixture unicorn')",[]).unwrap();
        let result=session_search(&cfg,&json!({"cwd":temp.path()}),&json!({"query":"unicorn","limit":1})).unwrap();assert_eq!(result["hits"][0]["session_id"],"known");assert_eq!(result["count"],1);
    }

    use super::*;
    fn fixture()->(tempfile::TempDir,Config,Value) {
        let dir=tempfile::tempdir().unwrap();let cfg=Config::for_root(dir.path().join("lore"));
        let identity=json!({"session_id":"fixture","cwd":dir.path(),"source_engine":"claude","spawn_depth":0,"lore":true});
        (dir,cfg,identity)
    }
    #[test]
    fn identity_and_arguments_cannot_escalate_model_authority() {
        let(_dir,cfg,identity)=fixture();let mut tools=AgentOperators::default();
        tools.execute(&cfg,&json!({"op":"agent_catalog_v1","identity":identity})).unwrap();
        let call=json!({"op":"agent_tool_v1","identity":identity,"name":"lore_remember","arguments":{"text":"Owned fact","authority":"human"}});
        assert!(tools.execute(&cfg,&call).unwrap()["error"].as_str().unwrap().contains("invalid arguments"));
        let mut changed=identity.clone();changed["session_id"]=json!("other");
        assert_eq!(tools.execute(&cfg,&json!({"op":"agent_catalog_v1","identity":changed})),Err(Error::Untrusted));
        assert!(!cfg.root.exists());
    }
    #[test]
    fn remember_stages_scrubbed_text_and_deduplicates_without_curating() {
        let(_dir,cfg,identity)=fixture();let mut tools=AgentOperators::default();
        tools.execute(&cfg,&json!({"op":"agent_catalog_v1","identity":identity})).unwrap();
        let call=json!({"op":"agent_tool_v1","identity":identity,"name":"lore_remember","arguments":{"text":"Owned fact"}});
        let value=tools.execute(&cfg,&call).unwrap();assert!(value["staged"].is_string());
        assert!(tools.execute(&cfg,&call).unwrap()["staged"].is_null());
        assert!(!cfg.root.join("USER.md").exists());
        let row=pending::snapshot(&cfg.root.join("pending").join(format!("{}.json",value["staged"].as_str().unwrap()))).unwrap();
        let item:Value=serde_json::from_str(&row.raw).unwrap();assert_eq!(item["writer"],"model");assert_eq!(item["source_engine"],"claude");
    }
    #[test]
    fn graph_projection_never_inherits_citation_authority() {
        let(_dir,cfg,_)=fixture();let human=gate::Authority::HumanReview{agent:"fixture".into(),engine:"human".into()};
        let mut ids=Vec::new();for claim in ["First", "Second"]{ids.push(beliefs::insert(&cfg,&json!({"subject":"user","claim":claim,"confidence":0.9,"session_id":"same"}),&human).unwrap()["id"].as_i64().unwrap());}
        for _ in 0..3 {beliefs::outcome(&cfg,&json!({"belief_id":ids[0],"event":"confirmed","source":"user"}),&human).unwrap();}
        let out=neighbours(&cfg,&json!({"belief_id":ids[0],"hops":1,"limit":12})).unwrap();
        assert_eq!(out["seed"]["citation_status"],"steer");assert_eq!(out["neighbours"][0]["citation_status"],"cite_only");assert_eq!(out["neighbours"][0]["relation_projected"],true);
    }
}
