//! Canonical belief reads and transactional mutation bodies. Display data is
//! never a review proof; callers must separately authorize direct operators.
use std::{collections::{BTreeMap,BTreeSet},path::Path,sync::OnceLock};
use rusqlite::{params,Connection,OptionalExtension,TransactionBehavior,functions::FunctionFlags};
use serde_json::{json,Value};
use crate::{config::{self,Config},gate::Authority,scrub,store,Error,Result};

pub fn casefold(text:&str)->String {
    static TABLE:OnceLock<BTreeMap<u32,String>>=OnceLock::new();
    let table=TABLE.get_or_init(|| {
        let value:Value=serde_json::from_str(include_str!("casefold.json")).expect("pinned Unicode casefold data");
        value["mapping"].as_object().expect("casefold map").iter()
            .map(|(key,value)|(key.parse().expect("Unicode scalar"),value.as_str().expect("casefold text").to_owned())).collect()
    });
    let mut out=String::new();
    for c in text.chars(){if let Some(value)=table.get(&(c as u32)){out.push_str(value)}else{out.push(c)}}
    out
}
pub(crate) fn one_line(text:&str)->String{text.split_whitespace().collect::<Vec<_>>().join(" ")}
pub(crate) fn crop(text:&str,count:usize)->String{text.chars().take(count).collect()}
pub(crate) fn id(req:&Value,key:&str)->Result<i64>{req[key].as_i64().filter(|n|*n>0).ok_or(Error::InvalidRequest)}
pub(crate) fn text<'a>(req:&'a Value,key:&str,cap:usize)->Result<&'a str>{
    req[key].as_str().filter(|s|!s.is_empty()&&s.len()<=cap&&!s.contains('\0')).ok_or(Error::InvalidRequest)
}
pub(crate) fn page(req:&Value,cap:u64)->Result<(i64,i64)>{
    let offset=req.get("offset").map_or(Some(0),Value::as_u64).filter(|n|*n<=10000).ok_or(Error::InvalidRequest)?;
    let limit=req.get("limit").map_or(Some(cap),Value::as_u64).filter(|n|*n<=cap).ok_or(Error::InvalidRequest)?;
    Ok((i64::try_from(offset).map_err(|_|Error::TooLarge)?,i64::try_from(limit).map_err(|_|Error::TooLarge)?))
}
/// SQLite integers are signed. Reject malformed negative counters instead of
/// wrapping them into large unsigned values at protocol boundaries.
pub(crate) fn sql_count(row:&rusqlite::Row<'_>,index:usize)->rusqlite::Result<u64>{let value=row.get::<_,i64>(index)?;u64::try_from(value).map_err(|_|rusqlite::Error::IntegralValueOutOfRange(index,value))}
pub(crate) fn sql_integer(value:usize)->Result<i64>{i64::try_from(value).map_err(|_|Error::TooLarge)}
pub(crate) fn subjects(req:&Value)->Result<[String;3]>{
    let cwd=text(req,"cwd",4096)?;
    Ok(["user".into(),"user-model".into(),format!("project:{}",config::project_slug(Path::new(cwd)))])
}
fn optional(req:&Value,key:&str,cap:usize)->Result<Option<String>>{
    match req.get(key){None|Some(Value::Null)=>Ok(None),Some(Value::String(s)) if s.len()<=cap&&!s.contains('\0')=>Ok(Some(s.clone())),_=>Err(Error::InvalidRequest)}
}
pub(crate) fn key(conn:&Connection,subject:&str)->Result<Option<String>>{
    let Some(slug)=subject.strip_prefix("project:") else{return Ok(None)};
    Ok(Some(store::project_key_for_slug(conn,slug)?))
}
fn uid_subject(conn:&Connection,bid:i64)->Result<(String,String)>{
    conn.query_row("SELECT uid,subject FROM beliefs WHERE id=?",[bid],|r|Ok((r.get(0)?,r.get(1)?))).optional()?.ok_or(Error::Changed)
}
pub(crate) fn require_write(authority:&Authority)->Result<()>{if !authority.may_write()&&!authority.may_derive_beliefs(){return Err(Error::Untrusted)}Ok(())}
fn require_operator(authority:&Authority)->Result<()>{if !authority.may_write(){return Err(Error::Untrusted)}Ok(())}
fn attributed(req:&Value,authority:&Authority)->Result<Value>{if !req.is_object(){return Err(Error::InvalidRequest)}let mut req=req.clone();req["writer"]=json!(authority.writer());req["source_engine"]=json!(crate::gate::current_engine(authority.engine()));req["via"]=json!(if matches!(authority,Authority::HumanReview{..}){"approved"}else if authority.may_derive_beliefs(){"derived"}else{"direct"});Ok(req)}
fn confidence(req:&Value)->Result<f64>{req["confidence"].as_f64().filter(|n|n.is_finite()).map(|n|n.clamp(0.,1.)).ok_or(Error::InvalidRequest)}
fn evidence_fields(req:&Value)->Result<(Option<String>,Option<String>,Option<String>,Option<String>)>{
    Ok((optional(req,"session_id",128)?,optional(req,"project",4096)?,optional(req,"note",4096)?.map(|s|scrub::scrub(&s).map(|s|crop(&one_line(&s),300))).transpose()?,optional(req,"source_engine",32)?))
}
pub fn list(cfg:&Config,req:&Value)->Result<Value>{
    let (offset,limit)=page(req,50)?;
    let query=req.get("query").map_or(Some(""),Value::as_str).filter(|s|s.chars().count()<=200&&s.len()<=1024&&!s.chars().any(char::is_control)).ok_or(Error::InvalidRequest)?;
    let conn=store::connect(cfg)?;
    conn.create_scalar_function("visible_casefold",1,FunctionFlags::SQLITE_UTF8|FunctionFlags::SQLITE_DETERMINISTIC,|ctx| {
        let text=ctx.get::<String>(0)?;
        scrub::scrub(&text).map(|s|casefold(&s)).map_err(|e|rusqlite::Error::UserFunctionError(Box::new(e)))
    })?;
    let mut stmt=conn.prepare("SELECT b.id,b.subject,b.claim,b.confidence,(SELECT count(*) FROM belief_evidence e WHERE e.belief_id=b.id),b.updated,b.created,coalesce(CASE WHEN length(CAST(b.updated AS BLOB))<=64 AND b.updated GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' AND julianday(b.updated) IS NOT NULL THEN b.updated END,CASE WHEN length(CAST(b.created AS BLOB))<=64 AND b.created GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' AND julianday(b.created) IS NOT NULL THEN b.created END) AS recency FROM beliefs b WHERE status='active' AND (?='' OR instr(visible_casefold(subject),?)>0 OR instr(visible_casefold(claim),?)>0) ORDER BY julianday(recency) DESC,b.id LIMIT ? OFFSET ?")?;
    let q=casefold(query);
    let rows=stmt.query_map(params![q,q,q,limit,offset],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,f64>(3)?,r.get::<_,i64>(4)?,r.get::<_,Option<String>>(5)?,r.get::<_,Option<String>>(6)?,r.get::<_,Option<String>>(7)?)))?;
    let mut result=Vec::new();
    for row in rows {let (id,subject,claim,confidence,evidence_count,updated,created,recency)=row?;let claim=scrub::scrub(&claim)?;
        result.push(json!({"id":id,"subject":scrub::scrub(&subject)?,"claim":crop(&claim,4096),"claim_truncated":claim.chars().count()>4096,"confidence":confidence,"evidence_count":evidence_count,"updated":updated.filter(|s|s.len()<=64&&!s.chars().any(char::is_control)),"created":created.filter(|s|s.len()<=64&&!s.chars().any(char::is_control)),"recency":recency.filter(|s|!s.chars().any(char::is_control))}));}
    bounded(json!(result))
}
fn bounded(value:Value)->Result<Value>{if serde_json::to_vec(&value).map_err(|_|Error::Unavailable)?.len()>crate::MAX_FRAME_BYTES-512{return Err(Error::TooLarge)}Ok(value)}
pub fn display(cfg:&Config,req:&Value)->Result<Value>{
    text(req,"cwd",4096)?;let bid=id(req,"belief_id")?;let conn=store::connect(cfg)?;
    let row=conn.query_row("SELECT CASE WHEN length(CAST(subject AS BLOB))<=4096 THEN subject END,CASE WHEN length(CAST(claim AS BLOB))<=65536 THEN claim END FROM beliefs WHERE id=? AND status='active'",[bid],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?))).optional()?.ok_or(Error::Changed)?;
    let subject=row.0.ok_or(Error::TooLarge)?;let safe_subject=scrub::scrub(&subject)?;
    if safe_subject.len()>4096||safe_subject.chars().any(char::is_control){return Err(Error::TooLarge)}
    let mut claim=scrub::scrub(row.1.as_deref().unwrap_or(""))?;let mut redacted=safe_subject!=subject||row.1.as_ref().is_some_and(|s|s!=&claim);
    let mut complete=row.1.is_some()&&!redacted;
    if claim.chars().any(|c|c.is_control()&&c!='\n'){claim.clear();complete=false;redacted=true}
    if safe_subject.len()+claim.len()>65536{claim.clear();complete=false}
    Ok(json!({"id":bid,"subject":safe_subject,"claim":claim,"complete":complete,"redacted":redacted}))
}
fn checked(conn:&Connection,bid:i64,scope:&[String;3])->Result<(String,String,String)>{
    let (uid,subject,claim,status):(String,String,String,String)=conn.query_row("SELECT uid,subject,claim,status FROM beliefs WHERE id=?",[bid],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?.ok_or(Error::Changed)?;
    if !scope.contains(&subject)||status!="active"{return Err(Error::Changed)}
    if uid.is_empty()||uid.len()>128{return Err(Error::Changed)}
    Ok((uid,subject,claim))
}
pub fn review(cfg:&Config,req:&Value)->Result<Value>{
    let scope=subjects(req)?;let bid=id(req,"belief_id")?;let conn=store::connect(cfg)?;
    let (uid,subject,claim)=checked(&conn,bid,&scope)?;
    if claim.len()>16384||scrub::scrub(&claim)?!=claim||claim.chars().any(|c|c.is_control()&&c!='\n'){return Err(Error::TooLarge)}
    Ok(json!({"id":bid,"uid":uid,"subject":subject,"claim":claim,"claim_sha256":crate::digest(claim.as_bytes())}))
}
pub fn action(cfg:&Config,req:&Value,authority:&Authority)->Result<Value>{
    authority.require_review()?;
    let bid=id(req,"belief_id")?;let scope=subjects(req)?;let action=text(req,"action",32)?;let note=text(req,"note",300)?;
    if note.trim().is_empty()||scrub::scrub(note)?!=note||!matches!(action,"confirmed"|"contradicted"|"stale"|"retract"){return Err(Error::InvalidRequest)}
    let expected=req["expected"].as_object().filter(|o|o.len()==3).ok_or(Error::InvalidRequest)?;
    let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (uid,subject,claim)=checked(&tx,bid,&scope)?;
    if expected.get("uid")!=Some(&json!(uid))||expected.get("subject")!=Some(&json!(subject))||expected.get("claim_sha256")!=Some(&json!(crate::digest(claim.as_bytes()))){return Err(Error::Changed)}
    // Recheck review completeness inside the same write lock as the identity.
    if claim.len()>16384||scrub::scrub(&claim)?!=claim||claim.chars().any(|c|c.is_control()&&c!='\n'){return Err(Error::TooLarge)}
    if action=="retract"{retract_in_transaction(cfg,&tx,bid,note,authority)?;}else{outcome_in_transaction(cfg,&tx,bid,action,"user",None,Some(authority.agent()),Some(note),None,authority)?;}
    let (c,x,s)=outcome_counts(&tx,bid)?;let status:String=tx.query_row("SELECT status FROM beliefs WHERE id=?",[bid],|r|r.get(0))?;tx.commit()?;
    Ok(json!({"status":status,"retired":status!="active","confirmed":c,"contradicted":x,"stale":s}))
}
pub fn evidence(cfg:&Config,req:&Value)->Result<Value>{
    let bid=id(req,"belief_id")?;let (offset,limit)=page(req,50)?;let conn=store::connect(cfg)?;
    let mut stmt=conn.prepare("SELECT session_id,project,note,created,source_engine FROM belief_evidence WHERE belief_id=? ORDER BY created,rowid LIMIT ? OFFSET ?")?;
    let rows=stmt.query_map(params![bid,limit+1,offset],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,Option<String>>(2)?,r.get::<_,Option<String>>(3)?,r.get::<_,Option<String>>(4)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;
    let more=rows.len()>limit as usize;let mut out=Vec::new();
    for (sid,project,note,created,engine) in rows.into_iter().take(limit as usize){let note=scrub::scrub(&note.unwrap_or_default())?;let mut row=json!({"session_id":scrub::scrub(&sid.unwrap_or_default())?,"project":scrub::scrub(&project.unwrap_or_default())?,"note":crop(&note,4096),"note_truncated":note.chars().count()>4096,"created":scrub::scrub(&created.unwrap_or_default())?});if let Some(engine)=engine{row["source_engine"]=json!(scrub::scrub(&engine)?)}out.push(row)}
    if more{if let Some(last)=out.last_mut(){last["trail_truncated"]=json!(true)}}bounded(json!(out))
}
pub fn consult(cfg:&Config,req:&Value)->Result<Value>{
    let prompt=text(req,"prompt",32768)?;if prompt.chars().count()>8192{return Err(Error::InvalidRequest)}let expression=crate::index::fts_expr(prompt," OR ");if expression.is_empty(){return Ok(Value::Null)}let conn=store::connect(cfg)?;
    let row=conn.query_row("SELECT b.id,b.claim,b.confidence,bm25(belief_fts) FROM beliefs b JOIN belief_fts f ON b.id=f.belief_id WHERE belief_fts MATCH ? AND status='active' ORDER BY bm25(belief_fts) LIMIT 1",[expression],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,f64>(2)?,r.get::<_,f64>(3)?))).optional()?;
    match row{None=>Ok(Value::Null),Some((id,claim,confidence,score))=>{let claim=scrub::scrub(&claim)?;Ok(json!({"id":id,"claim":crop(&claim,240),"claim_truncated":claim.chars().count()>240,"confidence":confidence,"score":score,"citation_status":"cite_only"}))}}
}
pub fn calibrated_confidence(prior:f64,confirms:u64,contradicts:u64)->f64{(prior*2.+confirms as f64)/(2.+confirms as f64+contradicts as f64)}
pub fn outcome_counts(conn:&Connection,bid:i64)->Result<(u64,u64,u64)>{Ok(conn.query_row("SELECT coalesce(sum(event='confirmed'),0),coalesce(sum(event='contradicted'),0),coalesce(sum(event='stale'),0) FROM belief_outcomes WHERE belief_id=?",[bid],|r|Ok((sql_count(r,0)?,sql_count(r,1)?,sql_count(r,2)?)))?)}
pub(crate) fn reinforce_in_transaction(cfg:&Config,conn:&Connection,bid:i64,confidence:f64,req:&Value,authority:&Authority)->Result<()> {
    require_write(authority)?;let attributed=attributed(req,authority)?;let req=&attributed;
    let (uid,subject)=uid_subject(conn,bid)?;let old:f64=conn.query_row("SELECT confidence FROM beliefs WHERE id=?",[bid],|r|r.get(0))?;let confidence=old.max(confidence);let now=crate::utcnow();let (sid,project,note,engine)=evidence_fields(req)?;
    conn.execute("UPDATE beliefs SET confidence=?,updated=? WHERE id=?",params![confidence,now,bid])?;
    conn.execute("INSERT INTO belief_evidence(belief_id,session_id,project,note,created,source_engine) VALUES(?,?,?,?,?,?)",params![bid,sid,project,note,now,engine])?;
    let pk=key(conn,&subject)?;store::append_op(cfg,conn,"belief","reinforce",pk.as_deref(),&json!({"uid":uid,"confidence":confidence,"evidence":{"session_id":sid,"project_key":pk,"note":note,"source_engine":engine}}))
}
pub(crate) fn insert_in_transaction(cfg:&Config,conn:&Connection,req:&Value,authority:&Authority)->Result<(i64,bool)>{
    require_write(authority)?;let attributed=attributed(req,authority)?;let req=&attributed;
    let subject=text(req,"subject",4096)?;let claim=one_line(text(req,"claim",65536)?);if claim.is_empty()||scrub::scrub(&claim)?!=claim{return Err(Error::InvalidRequest)}let confidence=confidence(req)?;
    let row:Option<i64>=conn.query_row("SELECT id FROM beliefs WHERE subject=? AND lower(claim)=lower(?) AND status='active'",params![subject,claim],|r|r.get(0)).optional()?;
    let excluded:BTreeSet<i64>=match req.get("exclude_ids"){None|Some(Value::Null)=>BTreeSet::new(),Some(Value::Array(rows)) if rows.len()<=256=>rows.iter().map(|v|v.as_i64().filter(|n|*n>0).ok_or(Error::InvalidRequest)).collect::<Result<_>>()?,_=>return Err(Error::InvalidRequest)};
    if let Some(bid)=row.filter(|id|!excluded.contains(id)){reinforce_in_transaction(cfg,conn,bid,confidence,req,authority)?;return Ok((bid,false))}
    let uid=optional(req,"uid",128)?.unwrap_or_else(||uuid::Uuid::new_v4().to_string());let now=crate::utcnow();let via=optional(req,"via",32)?.unwrap_or_else(||"direct".into());let writer=optional(req,"writer",32)?;let (sid,project,note,engine)=evidence_fields(req)?;
    conn.execute("INSERT INTO beliefs(subject,claim,confidence,status,created,updated,writer,via,uid,source_engine) VALUES(?,?,?,'active',?,?,?,?,?,?)",params![subject,claim,confidence,now,now,writer,via,uid,engine])?;let bid=conn.last_insert_rowid();
    conn.execute("INSERT INTO belief_fts(belief_id,claim) VALUES(?,?)",params![bid,claim])?;
    conn.execute("INSERT INTO belief_evidence(belief_id,session_id,project,note,created,source_engine) VALUES(?,?,?,?,?,?)",params![bid,sid,project,note,now,engine])?;
    let pk=key(conn,subject)?;store::append_op(cfg,conn,"belief","insert",pk.as_deref(),&json!({"uid":uid,"subject":subject,"claim":claim,"confidence":confidence,"via":via,"writer":writer,"created":now,"source_engine":engine,"evidence":{"session_id":sid,"project_key":pk,"note":note,"source_engine":engine}}))?;Ok((bid,true))
}
pub(crate) fn outcome_in_transaction(cfg:&Config,conn:&Connection,bid:i64,event:&str,source:&str,sid:Option<&str>,agent:Option<&str>,note:Option<&str>,uid:Option<&str>,authority:&Authority)->Result<()> {
    require_operator(authority)?;
    if !matches!(event,"confirmed"|"contradicted"|"stale"){return Err(Error::InvalidRequest)}let (belief_uid,subject)=uid_subject(conn,bid)?;let uid=uid.map(str::to_owned).unwrap_or_else(||uuid::Uuid::new_v4().to_string());let note=note.map(|s|scrub::scrub(s).map(|s|crop(&one_line(&s),300))).transpose()?;let pk=key(conn,&subject)?;
    conn.execute("INSERT INTO belief_outcomes(belief_id,event,source,session_id,agent,note,created,uid) VALUES(?,?,?,?,?,?,?,?)",params![bid,event,source,sid,agent,note,crate::utcnow(),uid])?;
    store::append_op(cfg,conn,"belief","outcome",pk.as_deref(),&json!({"uid":uid,"belief_uid":belief_uid,"event":event,"source":source,"session_id":sid,"agent":agent,"note":note}))?;
    if event=="contradicted"&&outcome_counts(conn,bid)?.1>=2&&conn.execute("UPDATE beliefs SET status='dormant',updated=? WHERE id=? AND status='active'",params![crate::utcnow(),bid])?>0{store::append_op(cfg,conn,"belief","status",pk.as_deref(),&json!({"uid":belief_uid,"status":"dormant"}))?}Ok(())
}
pub(crate) fn supersede_in_transaction(cfg:&Config,conn:&Connection,bid:i64,by:Option<i64>,reason:&str,authority:&Authority)->Result<bool>{
    require_write(authority)?;
    if by==Some(bid){return Ok(false)}let (uid,subject)=uid_subject(conn,bid)?;let reason=crop(&one_line(reason),300);
    if conn.execute("UPDATE beliefs SET status='superseded',superseded_by=?,resolution=?,updated=? WHERE id=? AND status='active'",params![by,reason,crate::utcnow(),bid])?==0{return Ok(false)}
    if let Some(by)=by {let (by_uid,_)=uid_subject(conn,by)?;conn.execute("UPDATE belief_evidence SET belief_id=? WHERE belief_id=?",params![by,bid])?;crate::graph::repoint(conn,bid,by)?;let pk=key(conn,&subject)?;store::append_op(cfg,conn,"belief","supersede",pk.as_deref(),&json!({"uid":uid,"by_uid":by_uid,"reason":reason}))?;}Ok(true)
}
pub(crate) fn retract_in_transaction(cfg:&Config,conn:&Connection,bid:i64,reason:&str,authority:&Authority)->Result<bool>{
    require_write(authority)?;
    let Some((uid,subject))=conn.query_row("SELECT uid,subject FROM beliefs WHERE id=?",[bid],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional()? else{return Ok(false)};
    supersede_in_transaction(cfg,conn,bid,None,reason,authority)?;conn.execute("UPDATE beliefs SET status='retracted' WHERE id=?",[bid])?;let pk=key(conn,&subject)?;store::append_op(cfg,conn,"belief","retract",pk.as_deref(),&json!({"uid":uid}))?;Ok(true)
}
pub fn insert(cfg:&Config,req:&Value,authority:&Authority)->Result<Value>{require_write(authority)?;let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;let (id,created)=insert_in_transaction(cfg,&tx,req,authority)?;tx.commit()?;Ok(json!({"id":id,"created":created}))}
pub fn reinforce(cfg:&Config,req:&Value,authority:&Authority)->Result<Value>{require_write(authority)?;let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;let bid=id(req,"belief_id")?;reinforce_in_transaction(cfg,&tx,bid,confidence(req)?,req,authority)?;tx.commit()?;Ok(json!({"id":bid,"reinforced":true}))}
pub fn retract(cfg:&Config,req:&Value,authority:&Authority)->Result<Value>{require_write(authority)?;let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;let changed=retract_in_transaction(cfg,&tx,id(req,"belief_id")?,text(req,"reason",4096)?,authority)?;tx.commit()?;Ok(json!({"changed":changed}))}
pub fn supersede(cfg:&Config,req:&Value,authority:&Authority)->Result<Value>{require_write(authority)?;let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;let by=match req.get("by"){None|Some(Value::Null)=>None,_=>Some(id(req,"by")?)};let changed=supersede_in_transaction(cfg,&tx,id(req,"belief_id")?,by,text(req,"reason",4096)?,authority)?;tx.commit()?;Ok(json!({"changed":changed}))}
pub fn outcome(cfg:&Config,req:&Value,authority:&Authority)->Result<Value>{require_operator(authority)?;let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;let sid=optional(req,"session_id",128)?;let agent=Some(authority.agent().to_owned());let note=optional(req,"note",4096)?;let uid=optional(req,"uid",128)?;let bid=id(req,"belief_id")?;outcome_in_transaction(cfg,&tx,bid,text(req,"event",32)?,text(req,"source",32)?,sid.as_deref(),agent.as_deref(),note.as_deref(),uid.as_deref(),authority)?;let counts=outcome_counts(&tx,bid)?;tx.commit()?;Ok(json!({"confirmed":counts.0,"contradicted":counts.1,"stale":counts.2}))}
pub fn dormant(cfg:&Config,req:&Value,authority:&Authority)->Result<Value>{require_write(authority)?;
    let days=req.get("days").map_or(Some(45),Value::as_u64).filter(|n|*n<=36500).ok_or(Error::InvalidRequest)?;let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut stmt=tx.prepare("SELECT id,subject,uid FROM beliefs WHERE status='active' AND confidence<0.95 AND coalesce(last_referenced,updated)<datetime('now',?)")?;let rows=stmt.query_map([format!("-{days} day")],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;drop(stmt);
    for (id,subject,uid) in &rows{tx.execute("UPDATE beliefs SET status='dormant',updated=? WHERE id=?",params![crate::utcnow(),id])?;let pk=key(&tx,subject)?;store::append_op(cfg,&tx,"belief","status",pk.as_deref(),&json!({"uid":uid,"status":"dormant"}))?}tx.commit()?;Ok(json!({"moved":rows.len()}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]fn unicode_casefold_is_literal_and_covers_expansion_and_sigma(){assert_eq!(casefold("Straße Σςİ"),"strasse σσi\u{307}");assert_eq!(casefold("100%_literal"),"100%_literal")}
    #[test]fn insert_reinforce_outcomes_and_review_identity_share_one_transaction(){
        let authority=Authority::HumanReview{agent:"fixture".into(),engine:"unknown".into()};let temp=tempfile::tempdir_in("/home/docwilde/.cache/t").unwrap();let cfg=Config::for_root(temp.path().join("lore"));let req=json!({"subject":"user","claim":"private fixture fact","confidence":0.9,"session_id":"one"});let id=insert(&cfg,&req,&authority).unwrap()["id"].as_i64().unwrap();assert!(!insert(&cfg,&req,&authority).unwrap()["created"].as_bool().unwrap());
        let review=review(&cfg,&json!({"cwd":temp.path(),"belief_id":id})).unwrap();let expected=json!({"uid":review["uid"],"subject":review["subject"],"claim_sha256":review["claim_sha256"]});let action_req=json!({"cwd":temp.path(),"belief_id":id,"action":"contradicted","note":"fixture evidence","expected":expected});for _ in 0..2{action(&cfg,&action_req,&authority).unwrap();}
        assert!(action(&cfg,&action_req,&authority).is_err());let conn=store::connect(&cfg).unwrap();assert_eq!(outcome_counts(&conn,id).unwrap(),(0,2,0));assert!((calibrated_confidence(0.9,0,3)-0.36).abs()<1e-12);
    }
    #[test]fn recency_filter_precedes_pagination_and_stale_review_cannot_mutate(){
        let authority=Authority::HumanReview{agent:"fixture".into(),engine:"unknown".into()};let temp=tempfile::tempdir_in("/home/docwilde/.cache/t").unwrap();let cfg=Config::for_root(temp.path().join("lore"));let a=insert(&cfg,&json!({"subject":"user","claim":"Straße first","confidence":0.8}),&authority).unwrap()["id"].as_i64().unwrap();let b=insert(&cfg,&json!({"subject":"user","claim":"STRASSE second","confidence":0.8}),&authority).unwrap()["id"].as_i64().unwrap();let conn=store::connect(&cfg).unwrap();conn.execute("UPDATE beliefs SET updated='2020-01-01' WHERE id=?",[b]).unwrap();conn.execute("UPDATE beliefs SET updated='2021-01-01' WHERE id=?",[a]).unwrap();drop(conn);
        assert_eq!(list(&cfg,&json!({"query":"strasse","offset":1,"limit":1})).unwrap()[0]["id"],b);
        let reviewed=review(&cfg,&json!({"cwd":temp.path(),"belief_id":a})).unwrap();let conn=store::connect(&cfg).unwrap();conn.execute("UPDATE beliefs SET claim='changed' WHERE id=?",[a]).unwrap();drop(conn);assert!(action(&cfg,&json!({"cwd":temp.path(),"belief_id":a,"action":"retract","note":"test","expected":{"uid":reviewed["uid"],"subject":reviewed["subject"],"claim_sha256":reviewed["claim_sha256"]}}),&authority).is_err());
    }
    #[test]fn display_is_not_review_authority_and_models_cannot_write(){
        let temp=tempfile::tempdir_in("/home/docwilde/.cache/t").unwrap();let cfg=Config::for_root(temp.path().join("lore"));let human=Authority::HumanReview{agent:"fixture".into(),engine:"claude".into()};let model=Authority::Model{agent:"fixture".into(),engine:"claude".into(),session_id:"fixture".into()};let derived=Authority::Derived{agent:"owned-worker".into(),engine:"codex".into()};let request=json!({"subject":"global-read-only","claim":"full\nvisible claim","confidence":0.8,"writer":"spoof","source_engine":"spoof"});assert!(insert(&cfg,&request,&model).is_err());let bid=insert(&cfg,&request,&derived).unwrap()["id"].as_i64().unwrap();let conn=store::connect(&cfg).unwrap();assert_eq!(conn.query_row("SELECT writer,via,source_engine FROM beliefs WHERE id=?",[bid],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?))).unwrap(),("derived".into(),"derived".into(),"codex".into()));drop(conn);let request=json!({"cwd":temp.path(),"belief_id":bid});assert_eq!(display(&cfg,&request).unwrap()["complete"],true);assert!(review(&cfg,&request).is_err());assert!(outcome(&cfg,&json!({"belief_id":bid,"event":"confirmed","source":"user"}),&derived).is_err());
        let other=insert(&cfg,&json!({"subject":"user","claim":"rollback survivor","confidence":0.8}),&human).unwrap()["id"].as_i64().unwrap();assert!(supersede(&cfg,&json!({"belief_id":other,"by":999999,"reason":"must roll back"}),&human).is_err());let conn=store::connect(&cfg).unwrap();assert_eq!(conn.query_row("SELECT status FROM beliefs WHERE id=?",[other],|r|r.get::<_,String>(0)).unwrap(),"active");
    }

}
