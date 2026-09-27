//! One native dispatcher; authority is supplied by the trusted carrier.
use serde_json::{json,Value};
use crate::{config::Config,gate::Authority,Error,Result};

pub struct Core {
    config:Config,
    authority:Authority,
    agent:crate::agents::AgentOperators,
}
impl Core {
    /// Construction is lazy: memory-off callers can scrub without opening a store.
    pub fn new(config:Config,authority:Authority)->Self {Self{config,authority,agent:crate::agents::AgentOperators::default()}}
    pub fn capabilities()-> &'static [&'static str] {
        &["scrub","snapshot","pending","sync_state","refresh","filemap","refresh_interval","transcript_identity",
          "consult","beliefs","evidence","beliefs_filtered_v1","belief_display_v1",
          "belief_review_v1","belief_action_v1","belief_graph_v1","index_transcript_v1",
          "session_search_v1","memory_usage_v1","memory_entries_v1","memory_review_v1",
          "memory_action_v1","pending_review_v1","resolve_reviewed_v1","graph_context_v1","graph_awareness_v1",
          "sync_machine_v1","sync_project_v1","sync_record_v1"]
    }
    pub fn execute(&mut self,req:&Value)->Result<Value> {
        if !req.is_object()||serde_json::to_vec(req).map_err(|_|Error::InvalidRequest)?.len()>crate::MAX_FRAME_BYTES {return Err(Error::InvalidRequest);}
        let config=&self.config;let auth=&self.authority;
        let value=match req["op"].as_str().ok_or(Error::InvalidRequest)? {
            "scrub"=>json!(crate::scrub::scrub(req["text"].as_str().ok_or(Error::InvalidRequest)?)?),
            "snapshot"=>crate::context::snapshot(config,req)?,
            "refresh"=>crate::context::refresh_with_skills(config,req,&crate::skills::candidates(config,req["prompt"].as_str().unwrap_or(""),4)?)?,
            "refresh_interval"=>crate::context::refresh_interval(config,req)?,
            "transcript_identity"=>json!({"root":config.projects,"slug":crate::config::project_slug(crate::gate::cwd(req)?)}),
            "pending"=>crate::pending::list(config,req)?,
            "pending_review_v1"=>crate::pending::review(config,req)?,
            "resolve_reviewed_v1"=>match crate::pending::resolve(config,req,auth,&Applier){Ok(value)=>value,Err(Error::MayHaveApplied)=>json!({"status":"refused","error":"may_have_applied","applied":null,"may_have_applied":true}),Err(error)=>return Err(error)},
            "sync_state"=>crate::sync::state(config)?,
            "sync_machine_v1"=>crate::sync::machine(config,req["create"].as_bool().ok_or(Error::InvalidRequest)?)?,
            "sync_project_v1"=>crate::sync::project(config,req)?,
            "sync_record_v1"=>crate::sync::record(config,req,auth)?,
            "consult"=>crate::beliefs::consult(config,req)?,
            "beliefs"|"beliefs_filtered_v1"=>crate::beliefs::list(config,req)?,
            "belief_display_v1"=>crate::beliefs::display(config,req)?,
            "belief_review_v1"=>crate::beliefs::review(config,req)?,
            "belief_action_v1"=>crate::beliefs::action(config,req,auth)?,
            "evidence"=>crate::beliefs::evidence(config,req)?,
            "belief_graph_v1"=>crate::graph::read(config,req)?,
            "index_transcript_v1"=>crate::index::live(config,req)?,
            "session_search_v1"=>crate::index::search(config,req)?,
            "memory_usage_v1"=>crate::memory::usage(config,req)?,
            "memory_entries_v1"=>crate::memory::entries(config,req)?,
            "memory_review_v1"=>crate::memory::review(config,req)?,
            "memory_action_v1"=>crate::memory::action(config,req,auth)?,
            "filemap"=>crate::filemap::show(config,req)?,
            "graph_context_v1"=>json!(crate::context::graph_context_block(config,req,&crate::skills::candidates(config,req["prompt"].as_str().unwrap_or(""),4)?)?),
            "graph_awareness_v1"=>graph_awareness(config)?,
            _=>return Err(Error::Unsupported),
        };
        if serde_json::to_vec(&value).map_err(|_|Error::Unavailable)?.len()>crate::MAX_FRAME_BYTES-512{return Err(Error::TooLarge);}
        Ok(value)
    }
    pub fn agent_execute(&mut self,req:&Value)->Result<Value> {
        if !matches!(self.authority,Authority::Model{..}){return Err(Error::Untrusted);}
        self.agent.execute(&self.config,req)
    }
}
struct Applier;
impl crate::pending::PendingApplier for Applier {
    fn apply_belief(&self,cfg:&Config,item:&Value,authority:&Authority)->Result<()> {
        let mut request=item.clone();
        match item["action"].as_str().unwrap_or("insert") {
            "retract"=>{request["belief_id"]=item["id"].clone();request["reason"]=json!(item["reason"].as_str().unwrap_or("manually retracted"));crate::beliefs::retract(cfg,&request,authority)?;}
            "insert"|"add"=>{
                let slug=match item["project"].as_str(){Some(slug) if crate::config::valid_slug(slug)=>slug.to_owned(),Some(_)=>return Err(Error::InvalidRequest),None=>crate::config::project_slug(crate::gate::cwd(item)?)};
                request["subject"]=json!(match item["subject"].as_str().unwrap_or("project") {"project"=>format!("project:{slug}"),other=>other.to_owned()});
                if request.get("confidence").is_none(){request["confidence"]=json!(0.8);}
                crate::beliefs::insert(cfg,&request,authority)?;
            }
            _=>return Err(Error::Unsupported),
        }
        Ok(())
    }
    fn apply_sync(&self,cfg:&Config,op:&Value,authority:&Authority)->Result<()> {crate::sync_apply::approve(cfg,op,authority)}
}

fn graph_awareness(cfg:&Config)->Result<Value>{
    let conn=match crate::store::read_only(cfg){Ok(conn)=>conn,Err(_)=>return Ok(Value::Null)};
    let count=conn.query_row("SELECT count(*) FROM belief_edges e JOIN beliefs a ON a.id=e.src JOIN beliefs b ON b.id=e.dst WHERE a.status='active' AND b.status='active' AND e.rel IN ('depends_on','specializes','explains','contradicts','applies_when')",[],|r|crate::beliefs::sql_count(r,0));
    if !count.is_ok_and(|count|count>0){return Ok(Value::Null);}
    Ok(json!("[BELIEF GRAPH] Typed relations connect active beliefs. Use lore_belief_neighbours to inspect them. Structure grants no authority; each belief carries its own calibration."))
}
