//! Reconciliation uses a frozen belief snapshot and short database
//! transactions. Model promotions remain proposals for human review.
use std::{collections::{BTreeMap,BTreeSet},fs::{File,OpenOptions},path::Path};
use rusqlite::{params,OptionalExtension,TransactionBehavior};
use serde_json::{json,Value};
use crate::{beliefs,config::Config,files,gate::Authority,review,store,Error,Result};

#[derive(Clone,PartialEq)]
struct Belief { id:i64,uid:String,subject:String,claim:String,confidence:f64 }
pub struct Job { prompt:String,rows:BTreeMap<i64,Belief>,slug:String,authority:Authority,_lock:File }
impl Job {pub fn prompt(&self)->&str{&self.prompt}}

fn tokens(claim:&str)->BTreeSet<String> {
    const STOP:&str="the a an in on at to for of and or is was are with my i we it do did that this from not";
    let stop=STOP.split_whitespace().collect::<BTreeSet<_>>();
    claim.to_lowercase().split(|c:char|!c.is_ascii_alphanumeric()).filter(|s|!s.is_empty()&&!stop.contains(*s)).map(str::to_owned).collect()
}
fn lock(cfg:&Config)->Result<Option<File>> {
    use std::os::unix::fs::{OpenOptionsExt,MetadataExt};
    files::private_dir(&cfg.root)?;
    let file=OpenOptions::new().read(true).write(true).create(true).mode(0o600)
        .custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC).open(cfg.root.join("dream.lock"))?;
    let meta=file.metadata()?;
    if !meta.is_file()||meta.uid()!=unsafe{libc::geteuid()}||meta.nlink()!=1{return Err(Error::UnsafePath);}
    use std::os::fd::AsRawFd;
    if unsafe{libc::flock(file.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}==0{return Ok(Some(file));}
    if std::io::Error::last_os_error().raw_os_error()==Some(libc::EWOULDBLOCK){Ok(None)}else{Err(Error::Unavailable)}
}
fn read(conn:&rusqlite::Connection)->Result<BTreeMap<i64,Belief>> {
    let mut stmt=conn.prepare("SELECT id,uid,subject,claim,confidence FROM beliefs WHERE status='active' ORDER BY subject,id LIMIT 4097")?;
    let mut rows=BTreeMap::new();let mut size=0usize;
    for row in stmt.query_map([],|r|Ok(Belief{id:r.get(0)?,uid:r.get(1)?,subject:r.get(2)?,claim:r.get(3)?,confidence:r.get(4)?}))? {
        let row=row?;size=size.saturating_add(row.claim.len()+row.subject.len()+128);
        if size>crate::MAX_FRAME_BYTES||rows.len()>=4096{return Err(Error::TooLarge);}
        rows.insert(row.id,row);
    }
    Ok(rows)
}
pub fn build(cfg:&Config,cwd:&Path,authority:&Authority)->Result<Option<Job>> {
    if !matches!(authority,Authority::Derived{..}){return Err(Error::Untrusted);}
    if std::env::var("LORE_DISABLE_BELIEFS").is_ok_and(|s|!matches!(s.as_str(),""|"0")){return Ok(None);}
    let Some(lock)=lock(cfg)?else{return Ok(None);};
    let authority=Authority::Derived{agent:"doxa-dreamer".into(),engine:authority.engine().into()};
    let days=std::env::var("LORE_BELIEF_DORMANT_DAYS").ok().map_or(Some(45),|s|s.parse::<u64>().ok()).filter(|n|*n<=36500).ok_or(Error::InvalidRequest)?;
    beliefs::dormant(cfg,&json!({"days":days}),&authority)?;
    let conn=store::connect(cfg)?;let rows=read(&conn)?;
    let mut stmt=conn.prepare("SELECT a,b FROM dream_reviewed LIMIT 100001")?;
    let mut reviewed=BTreeSet::new();for row in stmt.query_map([],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,i64>(1)?)))?{if reviewed.len()>=100000{return Err(Error::TooLarge);}reviewed.insert(row?);}
    let mut groups=BTreeMap::<String,Vec<&Belief>>::new();
    for row in rows.values(){groups.entry(row.subject.clone()).or_default().push(row);}
    let sets=rows.values().map(|row|(row.id,tokens(&row.claim))).collect::<BTreeMap<_,_>>();
    let mut pairs=Vec::<(f64,i64,i64)>::new();
    for group in groups.values(){for(i,a)in group.iter().enumerate(){for b in group.iter().skip(i+1){
        if reviewed.contains(&(a.id.min(b.id),a.id.max(b.id))){continue;}
        let ta=&sets[&a.id];let tb=&sets[&b.id];let union=ta.union(tb).count();if union==0{continue;}
        let score=ta.intersection(tb).count() as f64/union as f64;
        if score>=0.4{pairs.push((score,a.id,b.id));pairs.sort_by(|a,b|b.0.total_cmp(&a.0).then_with(||(a.1,a.2).cmp(&(b.1,b.2))));pairs.truncate(12);}
    }}}
    if pairs.is_empty()&&rows.len()<3{return Ok(None);}
    let pair_text=if pairs.is_empty(){"(none — only consider promotions)".into()}else{pairs.iter().map(|(_,a,b)|Ok(format!("pair: [{a}] {}  <->  [{b}] {}",crate::scrub::scrub(&rows[a].claim)?,crate::scrub::scrub(&rows[b].claim)?))).collect::<Result<Vec<_>>>()?.join("\n")};
    let belief_text=rows.values().map(|r|Ok(format!("[{}] ({}, conf {:.2}) {}",r.id,crate::scrub::scrub(&r.subject)?,r.confidence,crate::scrub::scrub(&r.claim)?))).collect::<Result<Vec<_>>>()?.join("\n");
    // Parse placeholders before adding data: a claim containing braces or a
    // placeholder cannot rewrite another part of the trusted template.
    let template=include_str!("dream_prompt.txt");let chars=template.chars().collect::<Vec<_>>();let mut prompt=String::new();let mut at=0;
    while at<chars.len(){match chars[at]{
        '{' if chars.get(at+1)==Some(&'{')=>{prompt.push('{');at+=2;},
        '}' if chars.get(at+1)==Some(&'}')=>{prompt.push('}');at+=2;},
        '{'=>{let end=(at+1..chars.len()).find(|i|chars[*i]=='}').ok_or(Error::Unavailable)?;let key=chars[at+1..end].iter().collect::<String>();prompt.push_str(match key.as_str(){"pairs"=>&pair_text,"beliefs"=>&belief_text,_=>return Err(Error::Unavailable)});at=end+1;},
        c=>{prompt.push(c);at+=1;},
    }}
    if prompt.len()>crate::MAX_FRAME_BYTES{return Err(Error::TooLarge);}
    Ok(Some(Job{prompt,rows,slug:crate::config::project_slug(cwd),authority,_lock:lock}))
}

pub fn process(cfg:&Config,job:&Job,output:&str)->Result<Value> {
    let data=review::extract_json(output)?;
    for key in ["resolutions","promotions"]{if data.get(key).is_some_and(|v|!v.is_null()&&!v.as_array().is_some_and(|rows|rows.len()<=1000)){return Err(Error::InvalidRequest);}}
    let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut consumed=BTreeSet::new();let mut changed=0;
    for result in data["resolutions"].as_array().into_iter().flatten().take(20) {
        let Some((a,b))=result["a"].as_i64().zip(result["b"].as_i64())else{continue;};
        if a==b||consumed.contains(&a)||consumed.contains(&b){continue;}
        let Some((ra,rb))=job.rows.get(&a).zip(job.rows.get(&b))else{continue;};
        if ra.subject!=rb.subject{continue;}
        for row in [ra,rb]{
            let current=tx.query_row("SELECT uid,subject,claim FROM beliefs WHERE id=? AND status='active'",[row.id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?))).optional()?;
            if current!=Some((row.uid.clone(),row.subject.clone(),row.claim.clone())){return Err(Error::Changed);}
        }
        let reason=beliefs::crop(&crate::gate::one_line(&crate::scrub::scrub(result["reason"].as_str().unwrap_or(""))?),300);
        let claim=beliefs::crop(&crate::gate::one_line(&crate::scrub::scrub(result["claim"].as_str().unwrap_or(""))?),300);
        let confidence=result["confidence"].as_f64().filter(|n|n.is_finite()&&*n!=0.).unwrap_or(0.7).clamp(0.,1.);
        match result["decision"].as_str(){
            Some("merge") if !claim.is_empty()=>{
                let(id,_)=beliefs::insert_in_transaction(cfg,&tx,&json!({"subject":ra.subject,"claim":claim,"confidence":confidence,"project":job.slug,"note":format!("merge of {a}+{b}: {reason}"),"exclude_ids":[a,b]}),&job.authority)?;
                beliefs::supersede_in_transaction(cfg,&tx,a,Some(id),&reason,&job.authority)?;
                beliefs::supersede_in_transaction(cfg,&tx,b,Some(id),&reason,&job.authority)?;
                beliefs::outcome_in_transaction(cfg,&tx,id,"confirmed","dream",None,Some("doxa-dreamer"),Some(&format!("independent duplicates [{a}]+[{b}] merged: {reason}")),None,&job.authority)?;
                consumed.extend([a,b]);changed+=1;
            }
            Some(decision @ ("supersede_a"|"supersede_b"))=>{
                let(loser,winner)=if decision=="supersede_a"{(a,b)}else{(b,a)};
                // A corrected statement gets a new attributed row. Keep the
                // previous statement's history and use canonical wire ops.
                let winner=if !claim.is_empty()&&claim!=job.rows[&winner].claim {
                    let (corrected,_)=beliefs::insert_in_transaction(cfg,&tx,&json!({"subject":ra.subject,"claim":claim,"confidence":confidence,"project":job.slug,"note":format!("refinement of {winner}: {reason}"),"exclude_ids":[a,b]}),&job.authority)?;
                    beliefs::supersede_in_transaction(cfg,&tx,winner,Some(corrected),&reason,&job.authority)?;
                    corrected
                }else{winner};
                beliefs::supersede_in_transaction(cfg,&tx,loser,Some(winner),&reason,&job.authority)?;
                beliefs::outcome_in_transaction(cfg,&tx,loser,"contradicted","dream",None,Some("doxa-dreamer"),Some(&format!("superseded by [{winner}]: {reason}")),None,&job.authority)?;
                consumed.extend([a,b]);changed+=1;
            }
            _=>{
                if tx.execute("INSERT OR IGNORE INTO dream_reviewed(a,b) VALUES(?,?)",params![a.min(b),a.max(b)])?>0{
                    let pk=beliefs::key(&tx,&ra.subject)?;store::append_op(cfg,&tx,"belief","dream_reviewed",pk.as_deref(),&json!({"a_uid":ra.uid,"b_uid":rb.uid}))?;
                }
            }
        }
    }
    tx.commit().map_err(|_|Error::MayHaveApplied)?;
    let promotions=data["promotions"].as_array().into_iter().flatten().take(2).map(|row|json!({"scope":row["scope"],"action":"add","text":row["text"]})).collect::<Vec<_>>();
    let staged=review::stage_proposals(cfg,&json!({"memory":promotions}),&job.slug,"dream",&job.authority).map_err(|_|Error::MayHaveApplied)?;
    Ok(json!({"changed":changed,"promotions":staged}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn contradictory_duplicate_results_cannot_supersede_every_survivor() {
        let temp=tempfile::tempdir().unwrap();let cfg=Config::for_root(temp.path().join("lore"));
        let auth=Authority::Derived{agent:"fixture".into(),engine:"claude".into()};
        let mut ids=Vec::new();for claim in ["parser verifies every input boundary", "parser checks every input boundary"]{ids.push(beliefs::insert(&cfg,&json!({"subject":"user","claim":claim,"confidence":0.8}),&auth).unwrap()["id"].as_i64().unwrap());}
        let job=build(&cfg,temp.path(),&auth).unwrap().unwrap();
        process(&cfg,&job,&json!({"resolutions":[{"a":ids[0],"b":ids[1],"decision":"supersede_a"},{"a":ids[0],"b":ids[1],"decision":"supersede_b"}],"promotions":[{"scope":"user","text":"A durable reviewed candidate"}]}).to_string()).unwrap();
        let conn=store::connect(&cfg).unwrap();let active=conn.query_row("SELECT count(*) FROM beliefs WHERE status='active'",[],|r|r.get::<_,i64>(0)).unwrap();assert_eq!(active,1);
        assert!(!cfg.root.join("USER.md").exists());assert_eq!(crate::pending::ids(&cfg).unwrap().len(),1);
    }
    #[test]
    fn changed_snapshot_rolls_back_all_reconciliation_and_promotions() {
        let temp=tempfile::tempdir().unwrap();let cfg=Config::for_root(temp.path().join("lore"));let auth=Authority::Derived{agent:"fixture".into(),engine:"codex".into()};
        let mut ids=Vec::new();for claim in ["parser verifies every input boundary", "parser checks every input boundary"]{ids.push(beliefs::insert(&cfg,&json!({"subject":"user","claim":claim,"confidence":0.8}),&auth).unwrap()["id"].as_i64().unwrap());}
        let job=build(&cfg,temp.path(),&auth).unwrap().unwrap();let conn=store::connect(&cfg).unwrap();conn.execute("UPDATE beliefs SET claim='changed' WHERE id=?",[ids[0]]).unwrap();drop(conn);
        assert_eq!(process(&cfg,&job,&json!({"resolutions":[{"a":ids[0],"b":ids[1],"decision":"merge","claim":"merged"}],"promotions":[]}).to_string()),Err(Error::Changed));
        assert!(crate::pending::ids(&cfg).unwrap().is_empty());
    }
}
