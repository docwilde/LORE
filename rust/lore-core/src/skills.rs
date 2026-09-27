//! Learned recipes and local track record. Stored descriptions remain data;
//! only a frozen reviewer may record outcomes or alter the usage ledger.
use std::{collections::{BTreeMap,BTreeSet},fs,path::{Path,PathBuf}};
use serde_json::{json,Value};
use crate::{config::{Config,valid_skill_name},files,gate::{self,Authority},Error,Result};
const MAX_SKILLS:usize=400;
const MAX_SOURCE:usize=1024*1024;
pub fn overlap_tokens(text:&str)->BTreeSet<String>{
    text.to_lowercase().split(|c:char|!c.is_ascii_lowercase()&&!c.is_ascii_digit()&&c!='_').filter(|word|word.len()>=3).map(str::to_owned).collect()
}
pub fn containment(a:&BTreeSet<String>,b:&BTreeSet<String>)->f64{if a.is_empty(){0.0}else{a.intersection(b).count() as f64/a.len() as f64}}
fn skill_files(cfg:&Config)->Result<Vec<(String,PathBuf)>>{
    let mut result=Vec::new();if !cfg.skills.try_exists()?{return Ok(result);}let meta=fs::symlink_metadata(&cfg.skills)?;if !meta.is_dir(){return Err(Error::UnsafePath);}
    for(index,entry)in fs::read_dir(&cfg.skills)?.take(MAX_SKILLS+1).enumerate(){if index>=MAX_SKILLS{return Err(Error::TooLarge);}let entry=entry?;let name=entry.file_name().to_str().filter(|name|valid_skill_name(name)).map(str::to_owned);if !entry.file_type()?.is_dir(){continue;}let Some(name)=name else{continue;};let path=entry.path().join("SKILL.md");if path.try_exists()?{result.push((name,path));}}
    result.sort_by(|a,b|a.0.cmp(&b.0));Ok(result)
}
pub fn installed(cfg:&Config)->Result<Vec<String>>{Ok(skill_files(cfg)?.into_iter().map(|(name,_)|name).collect())}
pub fn learned(cfg:&Config)->Result<BTreeMap<String,String>>{
    let mut result=BTreeMap::new();let mut total=0;for(name,path)in skill_files(cfg)?{
        let bytes=files::read_regular(&path,MAX_SOURCE)?;total+=bytes.len();if total>8*MAX_SOURCE{return Err(Error::TooLarge);}let text=std::str::from_utf8(&bytes).map_err(|_|Error::InvalidRequest)?;let head=text.chars().take(600).collect::<String>();if !head.contains("lore-learned"){continue;}
        let description=head.lines().find_map(|line|line.strip_prefix("description:").map(str::trim)).unwrap_or("").trim_matches('"');
        result.insert(name,gate::one_line(&crate::scrub::scrub(description)?));
    }Ok(result)
}
pub fn load_usage(cfg:&Config)->Result<Value>{let path=cfg.root.join("skill_usage.json");if !path.try_exists()?{return Ok(json!({}));}let value:Value=serde_json::from_slice(&files::read_regular(&path,MAX_SOURCE)?).map_err(|_|Error::Unavailable)?;if !value.is_object(){return Err(Error::Unavailable);}Ok(value)}
fn save_usage(cfg:&Config,value:&Value)->Result<()>{let bytes=serde_json::to_vec_pretty(value).map_err(|_|Error::Unavailable)?;if bytes.len()>MAX_SOURCE{return Err(Error::TooLarge);}files::atomic_write(&cfg.root.join("skill_usage.json"),&bytes)}
fn count(row:&Value,key:&str)->u64{row[key].as_u64().unwrap_or(0)}
pub fn record_line(row:&Value)->String{let mut parts=vec![format!("used {}x",count(row,"uses"))];if count(row,"ok")>0||count(row,"fail")>0{parts.push(format!("{} ok / {} failed",count(row,"ok"),count(row,"fail")));}if let Some(last)=row["last_outcome"].as_str().filter(|last|matches!(*last,"success"|"failure"|"unclear")){parts.push(format!("last: {last}"));}parts.join(", ")}
pub fn candidates(cfg:&Config,prompt:&str,limit:usize)->Result<Vec<Value>>{
    if prompt.chars().count()>8192||limit>20{return Err(Error::InvalidRequest);}let learned=learned(cfg)?;let usage=load_usage(cfg)?;let want=overlap_tokens(prompt);let need=if want.len()>=3{2}else{1};let mut rows=Vec::new();
    for(name,desc)in learned{let shared=want.intersection(&overlap_tokens(&format!("{} {desc}",name.replace('-'," ")))).count();if !want.is_empty()&&shared<need{continue;}
        let row=&usage[&name];let last=row["last_outcome"].as_str().filter(|last|matches!(*last,"success"|"failure"|"unclear")).unwrap_or("");let ok=count(row,"ok");let fail=count(row,"fail");let uses=count(row,"uses");
        rows.push(json!({"name":name,"desc":desc,"ok":ok,"fail":fail,"uses":uses,"last":last,"confirmed":ok>0&&last!="failure","tested":uses>0,"overlap":shared}));
    }
    rows.sort_by(|a,b|b["confirmed"].as_bool().cmp(&a["confirmed"].as_bool()).then_with(||b["tested"].as_bool().cmp(&a["tested"].as_bool())).then_with(||(count(b,"ok") as i128-count(b,"fail") as i128).cmp(&(count(a,"ok") as i128-count(a,"fail") as i128))).then_with(||count(b,"uses").cmp(&count(a,"uses"))).then_with(||count(b,"overlap").cmp(&count(a,"overlap"))).then_with(||a["name"].as_str().cmp(&b["name"].as_str())));
    rows.truncate(limit);Ok(rows)
}
pub fn candidates_op(cfg:&Config,req:&Value)->Result<Value>{let prompt=req.get("prompt").map_or(Some(""),Value::as_str).ok_or(Error::InvalidRequest)?;let limit=req.get("limit").map_or(Some(4),Value::as_u64).filter(|limit|*limit<=20).ok_or(Error::InvalidRequest)?;Ok(json!(candidates(cfg,prompt,limit as usize)?))}
fn require_reviewer(authority:&Authority)->Result<()>{if matches!(authority,Authority::Derived{..}){Ok(())}else{Err(Error::Untrusted)}}
fn append_tail(row:&mut Value,key:&str,value:Value){if !row[key].is_array(){row[key]=json!([]);}let list=row[key].as_array_mut().unwrap();list.push(value);if list.len()>10{list.drain(..list.len()-10);}}
/// Read git's public hash without running git or hooks. A missing/unusable
/// symbolic ref is unknown, never fabricated attribution.
pub fn repo_head(cwd:&Path)->Option<String>{
    let root=cwd.ancestors().find(|root|root.join(".git").exists())?;let marker=root.join(".git");let git=if marker.is_dir(){marker}else{let bytes=files::read_regular(&marker,4096).ok()?;let text=std::str::from_utf8(&bytes).ok()?;root.join(text.trim().strip_prefix("gitdir: ")?)};
    let common=files::read_regular(&git.join("commondir"),4096).ok().and_then(|bytes|String::from_utf8(bytes).ok()).map(|relative|git.join(relative.trim())).unwrap_or_else(||git.clone());
    let bytes=files::read_regular(&git.join("HEAD"),4096).ok()?;let head=std::str::from_utf8(&bytes).ok()?.trim();let hash=if let Some(reference)=head.strip_prefix("ref: "){
        if !reference.starts_with("refs/")||reference.contains("..")||reference.contains('\\'){return None;}
        let direct=files::read_regular(&common.join(reference),4096).ok().and_then(|bytes|String::from_utf8(bytes).ok());
        direct.or_else(||files::read_regular(&common.join("packed-refs"),MAX_SOURCE).ok().and_then(|bytes|String::from_utf8(bytes).ok()).and_then(|text|text.lines().find_map(|line|line.split_once(' ').filter(|(_,name)|*name==reference).map(|(hash,_)|hash.to_owned()))))?
    }else{head.to_owned()};let hash=hash.trim();if hash.len()==40||hash.len()==64{if hash.bytes().all(|byte|byte.is_ascii_hexdigit()){return Some(hash[..12].to_lowercase());}}None
}
pub fn record_outcomes(cfg:&Config,data:&Value,cwd:&Path,authority:&Authority)->Result<usize>{
    require_reviewer(authority)?;let learned=learned(cfg)?;let path=cfg.root.join("skill_usage.json");let _lock=files::Locks::acquire(&cfg.root,&[path],cfg.timeout)?;let mut usage=load_usage(cfg)?;let head=repo_head(cwd);let mut recorded=0;
    for outcome in data["skill_outcomes"].as_array().into_iter().flatten().take(10){let Some(name)=outcome["name"].as_str().filter(|name|learned.contains_key(*name))else{continue;};let Some(result)=outcome["outcome"].as_str().filter(|outcome|matches!(*outcome,"success"|"failure"|"unclear"))else{continue;};
        if !usage[name].is_object(){usage[name]=json!({"uses":0});}let row=&mut usage[name];let reason=gate::one_line(&crate::scrub::scrub(outcome["reason"].as_str().unwrap_or(""))?).chars().take(200).collect::<String>();
        let key=if result=="success"{Some("ok")}else if result=="failure"{Some("fail")}else{None};if let Some(key)=key{row[key]=json!(count(row,key).checked_add(1).ok_or(Error::TooLarge)?);}
        row["last_outcome"]=json!(result);row["last_reason"]=json!(reason);row["last"]=json!(crate::utcnow());if let Some(head)=&head{append_tail(row,"heads",json!(head));}
        append_tail(row,"trail",json!({"o":result,"h":head,"r":reason.chars().take(80).collect::<String>()}));append_tail(row,"by",json!(authority.agent()));recorded+=1;
    }if recorded>0{save_usage(cfg,&usage)?;}Ok(recorded)
}
pub fn record_usage(cfg:&Config,messages:&[crate::index::Message],authority:&Authority)->Result<()> {
    require_reviewer(authority)?;if messages.len()>20000{return Err(Error::TooLarge);}let learned=learned(cfg)?;if learned.is_empty(){return Ok(());}let path=cfg.root.join("skill_usage.json");let _lock=files::Locks::acquire(&cfg.root,&[path],cfg.timeout)?;let mut usage=load_usage(cfg)?;let mut changed=false;
    for message in messages{if message.role!="tool"{continue;}let Some(name)=message.content.strip_prefix("Skill: ").map(str::trim).filter(|name|learned.contains_key(*name))else{continue;};if !usage[name].is_object(){usage[name]=json!({"uses":0});}let row=&mut usage[name];row["uses"]=json!(count(row,"uses").checked_add(1).ok_or(Error::TooLarge)?);row["last"]=json!(crate::utcnow());changed=true;}
    if changed{save_usage(cfg,&usage)?;}Ok(())
}
pub fn update_admitted(action:&str,row:&Value)->bool{
    let n=count(row,"ok").saturating_add(count(row,"fail"));if action=="retire"{return n>=3;}if action!="update"{return false;}
    let trail=row["trail"].as_array();let last=trail.and_then(|trail|trail.last());let success=trail.into_iter().flatten().rev().find(|item|item["o"]=="success").and_then(|item|item["h"].as_str()).filter(|head|!head.is_empty());
    let hard=last.filter(|last|last["o"]=="failure").and_then(|last|last["r"].as_str()).is_some_and(|reason|{let reason=reason.to_lowercase();["error","traceback","exit code","not found","no such file","failed"].iter().any(|word|reason.contains(word))});
    let same_head=success.is_some()&&last.and_then(|last|last["h"].as_str())==success;n>=if hard&&same_head{1}else{2}
}

#[cfg(test)]mod tests{
    use super::*;
    fn auth()->Authority{Authority::Derived{agent:"reviewer".into(),engine:"claude".into()}}
    fn install(cfg:&Config,name:&str,desc:&str){files::atomic_write(&cfg.skills.join(name).join("SKILL.md"),crate::pending::skill_file_text(name,Some(desc),"body").as_bytes()).unwrap();}
    #[test]fn prompt_overlap_and_track_record_match_canonical_tiers(){
        let temp=tempfile::tempdir().unwrap();let cfg=Config::for_root(temp.path().join("lore"));install(&cfg,"network-wireguard","wireguard nmcli linux setup");install(&cfg,"cloud-tunnel","cloudflare tunnel setup");
        assert_eq!(candidates(&cfg,"wireguard nmcli linux setup",4).unwrap().len(),1);let rows=candidates(&cfg,"",4).unwrap();assert!(rows.iter().all(|row|row["tested"]==false));
        record_outcomes(&cfg,&json!({"skill_outcomes":[{"name":"network-wireguard","outcome":"success","reason":"tests passed"}]}),temp.path(),&auth()).unwrap();assert_eq!(candidates(&cfg,"",4).unwrap()[0]["name"],"network-wireguard");
    }
    #[test]fn graduated_update_guard_excludes_drift_and_requires_more_for_retirement(){
        let one=json!({"ok":1,"fail":0,"trail":[{"o":"success","h":"same"},{"o":"failure","h":"same","r":"exit code 1"}]});assert!(update_admitted("update",&one));assert!(!update_admitted("retire",&one));
        let mut drift=one.clone();drift["trail"][1]["h"]=json!("new");assert!(!update_admitted("update",&drift));assert!(update_admitted("retire",&json!({"fail":3})));
    }
    #[test]fn head_attribution_uses_linked_checkout_not_main_checkout(){
        let temp=tempfile::tempdir().unwrap();let main=temp.path().join("main");let checkout=temp.path().join("checkout");let git=main.join(".git");let worktree=git.join("worktrees/fixture");
        files::atomic_write(&git.join("HEAD"),b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n").unwrap();files::atomic_write(&worktree.join("HEAD"),b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n").unwrap();files::atomic_write(&worktree.join("commondir"),b"../..\n").unwrap();files::atomic_write(&checkout.join(".git"),format!("gitdir: {}\n",worktree.display()).as_bytes()).unwrap();
        assert_eq!(repo_head(&checkout),Some("bbbbbbbbbbbb".into()));assert_eq!(record_line(&json!({})),"used 0x");
    }
    #[test]fn outcomes_ignore_unknown_skills_and_json_identity_cannot_grant_reviewer(){
        let temp=tempfile::tempdir().unwrap();let cfg=Config::for_root(temp.path().join("lore"));install(&cfg,"fixture","recipe");let data=json!({"skill_outcomes":[{"name":"unknown","outcome":"success"}]});assert_eq!(record_outcomes(&cfg,&data,temp.path(),&auth()),Ok(0));
        let model=Authority::Model{agent:"m".into(),engine:"codex".into(),session_id:"s".into()};assert_eq!(record_outcomes(&cfg,&data,temp.path(),&model),Err(Error::Untrusted));assert!(!cfg.root.join("skill_usage.json").exists());
    }
}
