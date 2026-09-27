//! Provider transcript parsing and descriptor-owned incremental FTS indexing.
//! Payloads are scrubbed before truncation and before any persistent write.
use std::{collections::{BTreeMap,BTreeSet},fs::{self,File},os::unix::{fs::{FileExt,MetadataExt},ffi::OsStrExt,io::{AsRawFd,FromRawFd}},path::{Component,Path,PathBuf},sync::OnceLock};
use rusqlite::{params,Connection,OptionalExtension,TransactionBehavior};
use serde_json::{json,Value};
use crate::{beliefs::{crop,one_line},config::{self,Config},scrub,store,Error,Result};
const MAX_FILE:u64=256*1024*1024;
const MAX_LINE:usize=8*1024*1024;
const MAX_MESSAGES:usize=20000;
#[derive(Clone,Debug,Default)]pub struct Metadata{pub cwd:Option<String>,pub title:Option<String>,pub first_ts:Option<String>,pub last_ts:Option<String>,pub session_id:Option<String>,pub internal:bool,pub engine:String}
#[derive(Clone,Debug)]pub struct Message{pub ts:String,pub role:String,pub content:String}
fn string(v:&Value,cap:usize)->Option<String>{v.as_str().filter(|s|!s.is_empty()&&s.len()<=cap&&!s.contains('\0')).map(str::to_owned)}
fn engine(v:&Value)->String{v.as_str().filter(|s|!s.is_empty()&&s.len()<=32&&s.bytes().all(|c|c.is_ascii_lowercase()||c.is_ascii_digit()||matches!(c,b'_'|b'-'))).unwrap_or("claude").to_owned()}
fn safe_optional(value:&Option<String>)->Result<Option<String>>{value.as_deref().map(scrub::scrub).transpose()}
fn clean_text(raw:&str)->Result<String>{Ok(crop(&scrub::scrub(raw)?,4000))}
fn boilerplate()->&'static regex::Regex{static RE:OnceLock<regex::Regex>=OnceLock::new();RE.get_or_init(||regex::Regex::new(r"(?s)<command-(?:message|name|args)>.*?</command-(?:message|name|args)>|<local-command-(?:caveat|stdout)>.*?</local-command-(?:caveat|stdout)>|<system-reminder>.*?</system-reminder>|<task-notification>.*?</task-notification>").expect("static transcript grammar"))}
pub fn extract_text(content:&Value)->String{let raw=if let Some(s)=content.as_str(){s.to_owned()}else{content.as_array().into_iter().flatten().filter(|b|b["type"]=="text").filter_map(|b|b["text"].as_str()).collect::<Vec<_>>().join(" ")};boilerplate().replace_all(&raw,"").trim().to_owned()}
fn parse_record(meta:&mut Metadata,rows:&mut Vec<Message>,v:&Value,codex:bool)->Result<()> {
    if !v.is_object(){return Ok(())}
    if codex {
        let payload=&v["payload"];
        if v["type"]=="session_meta"{if meta.session_id.is_none(){meta.session_id=string(&payload["id"],128).or_else(||string(&payload["session_id"],128))}if meta.cwd.is_none(){meta.cwd=string(&payload["cwd"],4096)}meta.internal=meta.internal||payload.get("parent_thread_id").is_some_and(|v|!v.is_null()&&v!=&json!(""))||payload.get("agent_path").is_some_and(|v|!v.is_null()&&v!=&json!(""))||payload["thread_source"]=="subagent";return Ok(())}
        if v["type"]!="response_item"||payload["type"]!="message"{return Ok(())}
        let Some(role)=payload["role"].as_str().filter(|s|matches!(*s,"user"|"assistant")) else{return Ok(())};
        let text=payload["content"].as_array().into_iter().flatten().filter(|b|matches!(b["type"].as_str(),Some("input_text"|"output_text"))).filter_map(|b|b["text"].as_str()).collect::<Vec<_>>().join(" ");let text=text.trim();if text.is_empty(){return Ok(())}
        let ts=scrub::scrub(&string(&v["timestamp"],128).unwrap_or_default())?;if !ts.is_empty(){if meta.first_ts.is_none(){meta.first_ts=Some(ts.clone())}meta.last_ts=Some(ts.clone())}rows.push(Message{ts,role:role.into(),content:clean_text(text)?});
    }else{
        if v.get("engine").is_some(){meta.engine=engine(&v["engine"])}
        if let Some(ts)=string(&v["timestamp"],128){if meta.first_ts.is_none(){meta.first_ts=Some(ts.clone())}meta.last_ts=Some(ts)}
        if meta.cwd.is_none(){meta.cwd=string(&v["cwd"],4096)}
        if v["type"]=="custom-title"{if let Some(title)=string(&v["customTitle"],4096){meta.title=Some(title)}}else if v["type"]=="ai-title"&&meta.title.is_none(){meta.title=string(&v["aiTitle"],4096)}
        let Some(role)=v["type"].as_str().filter(|s|matches!(*s,"user"|"assistant")) else{return Ok(())};if v["isMeta"].as_bool().unwrap_or(false){return Ok(())}let text=extract_text(&v["message"]["content"]);if !text.is_empty(){rows.push(Message{ts:scrub::scrub(&string(&v["timestamp"],128).unwrap_or_default())?,role:role.into(),content:clean_text(&text)?})}
    }
    if rows.len()>MAX_MESSAGES{return Err(Error::TooLarge)}Ok(())
}
/// Fixed-size pread snapshots: a logical path is never reopened here. Offsets
/// remain unchanged; shortening, non-UTF8, or oversized records fail honestly.
fn lines(file:&File,size:u64,mut visit:impl FnMut(usize,&str,bool)->Result<()>)->Result<usize>{
    if size>MAX_FILE{return Err(Error::TooLarge)}let mut offset=0;let mut pending=Vec::new();let mut number=0;let mut buf=[0u8;65536];
    while offset<size{let n=file.read_at(&mut buf[..(size-offset).min(65536) as usize],offset)?;if n==0{return Err(Error::Changed)}offset+=n as u64;
        for byte in &buf[..n]{pending.push(*byte);if pending.len()>MAX_LINE{return Err(Error::TooLarge)}if *byte==b'\n'{number+=1;visit(number,std::str::from_utf8(&pending).map_err(|_|Error::InvalidRequest)?,true)?;pending.clear();}}
    }
    if !pending.is_empty(){number+=1;visit(number,std::str::from_utf8(&pending).map_err(|_|Error::InvalidRequest)?,false)?}Ok(number)
}
fn descriptor(file:&File)->Result<fs::Metadata>{let st=file.metadata()?;if !st.is_file()||st.uid()!=unsafe{libc::geteuid()}||st.nlink()!=1{return Err(Error::UnsafePath)}Ok(st)}
fn open_regular(path:&Path)->Result<File>{
    if !path.is_absolute(){return Err(Error::InvalidRequest)}
    // Walk from an owned directory fd, rejecting symlinks at every component.
    // Checking path ancestors before a normal open leaves a replacement race.
    let mut parent=File::open("/")?;let names=path.components().filter_map(|c|match c{Component::RootDir|Component::CurDir=>None,Component::Normal(s)=>Some(Ok(s)),_=>Some(Err(Error::UnsafePath))}).collect::<Result<Vec<_>>>()?;
    for (index,name) in names.iter().enumerate(){let name=std::ffi::CString::new(name.as_bytes()).map_err(|_|Error::UnsafePath)?;let last=index+1==names.len();let flags=libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|if last{libc::O_NONBLOCK}else{libc::O_DIRECTORY};let fd=unsafe{libc::openat(parent.as_raw_fd(),name.as_ptr(),flags)};if fd<0{return Err(Error::UnsafePath)}parent=unsafe{File::from_raw_fd(fd)};}
    descriptor(&parent)?;Ok(parent)
}

fn stamp(st:&fs::Metadata)->String{format!("{}:{}",st.mtime() as f64+st.mtime_nsec() as f64/1e9,st.len())}
pub fn parse_transcript_fd(file:&File,codex:bool)->Result<(Metadata,Vec<Message>)>{let st=descriptor(file)?;let mut meta=Metadata{engine:if codex{"codex"}else{"claude"}.into(),..Metadata::default()};let mut rows=Vec::new();lines(file,st.len(),|_,raw,_|{if let Ok(v)=serde_json::from_str(raw){parse_record(&mut meta,&mut rows,&v,codex)?}Ok(())})?;meta.cwd=safe_optional(&meta.cwd)?;meta.title=safe_optional(&meta.title)?;meta.first_ts=safe_optional(&meta.first_ts)?;meta.last_ts=safe_optional(&meta.last_ts)?;Ok((meta,rows))}
fn emit(cfg:&Config,conn:&Connection,sid:&str,project:&str,_new_rows:&[Message])->Result<()> {
    if !cfg.sync.enabled||!cfg.sync.classes.contains("sessions"){return Ok(())}
    let pk=store::project_key_for_slug(conn,project)?;let mid=store::machine_id(conn)?;
    let (cwd,title,first,last,count,engine):(Option<String>,Option<String>,Option<String>,Option<String>,u64,String)=conn.query_row("SELECT cwd,title,first_ts,last_ts,messages,engine FROM sessions WHERE session_id=?",[sid],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,crate::beliefs::sql_count(r,4)?,r.get(5)?)))?;
    store::append_op(cfg,conn,"session","upsert",Some(&pk),&json!({"session_id":sid,"project_key":pk,"machine_id":mid,"cwd":cwd,"title":title,"first_ts":first,"last_ts":last,"messages":count,"engine":engine}))?;
    // Canonical msgs is a complete replacement, never a tail or a chunk.
    // Read the current persisted snapshot inside this same mutation lock.
    let mut stmt=conn.prepare("SELECT ts,role,content FROM msg WHERE session_id=? ORDER BY rowid LIMIT 20001")?;let rows=stmt.query_map([sid],|r|Ok(json!({"ts":r.get::<_,String>(0)?,"role":r.get::<_,String>(1)?,"content":r.get::<_,String>(2)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
    if rows.len()>MAX_MESSAGES{return Err(Error::TooLarge)}
    let payload=json!({"session_id":sid,"rows":rows});if serde_json::to_vec(&payload).map_err(|_|Error::Unavailable)?.len()>crate::MAX_FRAME_BYTES{return Err(Error::TooLarge)}
    store::append_op(cfg,conn,"session","msgs",Some(&pk),&payload)?;Ok(())
}
fn identity(path:&Path)->Result<(String,String)>{let sid=path.file_stem().and_then(|s|s.to_str()).filter(|s|config::valid_id(s)).ok_or(Error::InvalidRequest)?;let project=path.parent().and_then(Path::file_name).and_then(|s|s.to_str()).filter(|s|config::valid_slug(s)).ok_or(Error::InvalidRequest)?;Ok((sid.into(),project.into()))}
fn write_rows(conn:&Connection,sid:&str,project:&str,rows:&[Message])->Result<()>{let mut stmt=conn.prepare("INSERT INTO msg(session_id,project,ts,role,content) VALUES(?,?,?,?,?)")?;for row in rows{stmt.execute(params![sid,project,row.ts,row.role,row.content])?;}Ok(())}
/// The caller owns the descriptor and transaction. A savepoint isolates read
/// failure even when cursor zero has already deleted previously indexed rows.
pub fn index_live_fd(cfg:&Config,conn:&Connection,file:&File,logical_path:&Path)->Result<(usize,usize)>{
    if !logical_path.is_absolute()||logical_path.components().any(|c|matches!(c,Component::ParentDir)){return Err(Error::InvalidRequest)}let st=descriptor(file)?;let (sid,project)=identity(logical_path)?;let owned=conn.is_autocommit();if owned{conn.execute_batch("BEGIN IMMEDIATE")?}let savepoint=format!("index_live_{}",uuid::Uuid::new_v4().simple());conn.execute_batch(&format!("SAVEPOINT {savepoint}"))?;
    let result=(||{
        let start=conn.query_row("SELECT coalesce(lines_indexed,0) FROM files WHERE path=?",[logical_path.to_string_lossy().as_ref()],|r|r.get::<_,i64>(0)).optional()?.unwrap_or(0);let start=usize::try_from(start).map_err(|_|Error::InvalidRequest)?;
        let old=conn.query_row("SELECT cwd,title,engine FROM sessions WHERE session_id=?",[&sid],|r|Ok((r.get::<_,Option<String>>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,String>(2)?))).optional()?;
        let mut meta=Metadata{cwd:old.as_ref().and_then(|x|x.0.clone()),title:old.as_ref().and_then(|x|x.1.clone()),engine:old.as_ref().map_or_else(||"claude".into(),|x|x.2.clone()),..Metadata::default()};let original=(meta.cwd.clone(),meta.title.clone(),meta.engine.clone());let mut rows=Vec::new();let mut consumed=start;let mut incomplete=false;
        if start==0{conn.execute("DELETE FROM msg WHERE session_id=?",[&sid])?;}
        let total=lines(file,st.len(),|i,raw,newline|{if i<=start{return Ok(())}match serde_json::from_str::<Value>(raw){Ok(v)=>{consumed=i;parse_record(&mut meta,&mut rows,&v,false)?},Err(_)=>{if newline{consumed=i}else{incomplete=true}}}Ok(())})?;
        // A replaced/truncated file cannot retain a cursor beyond EOF. Retry
        // atomically from zero using the same owned descriptor snapshot.
        if total<start{conn.execute("DELETE FROM files WHERE path=?",[logical_path.to_string_lossy().as_ref()])?;return index_live_fd(cfg,conn,file,logical_path)}
        meta.cwd=safe_optional(&meta.cwd)?;meta.title=safe_optional(&meta.title)?;meta.first_ts=safe_optional(&meta.first_ts)?;meta.last_ts=safe_optional(&meta.last_ts)?;write_rows(conn,&sid,&project,&rows)?;
        if !rows.is_empty()||(meta.cwd.clone(),meta.title.clone(),meta.engine.clone())!=original||start==0{
            let count:i64=conn.query_row("SELECT count(*) FROM msg WHERE session_id=?",[&sid],|r|r.get(0))?;if count<0{return Err(Error::InvalidRequest)}
            let first=rows.first().map(|r|r.ts.clone());let last=rows.last().map(|r|r.ts.clone());
            conn.execute("INSERT INTO sessions(session_id,project,cwd,title,first_ts,last_ts,messages,engine) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET messages=excluded.messages,cwd=coalesce(sessions.cwd,excluded.cwd),title=coalesce(excluded.title,sessions.title),last_ts=coalesce(excluded.last_ts,sessions.last_ts),engine=excluded.engine",params![sid,project,meta.cwd,meta.title,first,last,count,meta.engine])?;
            emit(cfg,conn,&sid,&project,&rows)?;
        }
        conn.execute("INSERT OR REPLACE INTO files(path,stamp,lines_indexed) VALUES(?,?,?)",params![logical_path.to_string_lossy(),if incomplete{None}else{Some(stamp(&st))},crate::beliefs::sql_integer(consumed)?])?;Ok((rows.len(),consumed))
    })();
    match result{Ok(value)=>{if let Err(error)=conn.execute_batch(&format!("RELEASE {savepoint}")){if owned{let _=conn.execute_batch("ROLLBACK");}return Err(error.into())}if owned{if let Err(error)=conn.execute_batch("COMMIT"){let _=conn.execute_batch("ROLLBACK");return Err(error.into())}}Ok(value)},Err(error)=>{let _=conn.execute_batch(&format!("ROLLBACK TO {savepoint}; RELEASE {savepoint}"));if owned{let _=conn.execute_batch("ROLLBACK");}Err(error)}}
}
pub fn live(cfg:&Config,req:&Value)->Result<Value>{let cwd=crate::beliefs::text(req,"cwd",4096)?;let sid=crate::beliefs::text(req,"session_id",128)?;if !config::valid_id(sid){return Err(Error::InvalidRequest)}let path=cfg.projects.join(config::project_slug(Path::new(cwd))).join(format!("{sid}.jsonl"));let file=open_regular(&path)?;let conn=store::connect(cfg)?;let (messages,lines)=index_live_fd(cfg,&conn,&file,&path)?;Ok(json!({"messages":messages,"lines_consumed":lines}))}
fn collect(root:&Path,depth:usize,out:&mut Vec<PathBuf>,suffix:&str)->Result<()> {
    if !root.exists(){return Ok(())}if depth>16{return Err(Error::TooLarge)}let st=fs::symlink_metadata(root)?;if !st.is_dir()||st.file_type().is_symlink(){return Err(Error::UnsafePath)}
    for entry in fs::read_dir(root)?{let entry=entry?;let ty=entry.file_type()?;if ty.is_dir(){collect(&entry.path(),depth+1,out,suffix)?}else if ty.is_file()&&entry.file_name().to_string_lossy().ends_with(suffix){out.push(entry.path());if out.len()>100000{return Err(Error::TooLarge)}}}Ok(())
}
pub fn index(cfg:&Config,req:&Value)->Result<Value>{
    let force=req.get("force").map_or(Some(false),Value::as_bool).ok_or(Error::InvalidRequest)?;let mut conn=store::connect(cfg)?;let tx=conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut sidecars=Vec::new();collect(&cfg.projects,0,&mut sidecars,".codex.json")?;let mut threads=BTreeSet::new();for path in sidecars.into_iter().filter(|p|p.parent().and_then(Path::parent)==Some(cfg.projects.as_path())){if let Ok(bytes)=crate::files::read_regular(&path,65536){if let Ok(v)=serde_json::from_slice::<Value>(&bytes){if let Some(id)=v["thread_id"].as_str().filter(|s|config::valid_id(s)){threads.insert(id.to_owned());}}}}
    tx.execute_batch("CREATE TABLE IF NOT EXISTS index_state(key TEXT PRIMARY KEY,value TEXT)")?;let state=serde_json::to_string(&threads).map_err(|_|Error::Unavailable)?;let previous=tx.query_row("SELECT value FROM index_state WHERE key='codex_sidecar_threads'",[],|r|r.get::<_,String>(0)).optional()?;
    let mut stmt=tx.prepare("SELECT path,stamp FROM files")?;let mut cached: BTreeMap<String,Option<String>>=stmt.query_map([],|r|Ok((r.get(0)?,r.get(1)?)))?.collect::<std::result::Result<_,_>>()?;drop(stmt);
    if previous.as_ref()!=Some(&state){for path in cached.keys().filter(|p|Path::new(p).starts_with(&cfg.codex_sessions)).cloned().collect::<Vec<_>>(){tx.execute("DELETE FROM files WHERE path=?",[&path])?;cached.remove(&path);}}
    tx.execute("INSERT OR REPLACE INTO index_state(key,value) VALUES('codex_sidecar_threads',?)",[state])?;
    for thread in &threads{let sid=format!("codex:{thread}");tx.execute("DELETE FROM msg WHERE session_id=?",[&sid])?;tx.execute("DELETE FROM sessions WHERE session_id=?",[sid])?;}
    let mut claude=Vec::new();collect(&cfg.projects,0,&mut claude,".jsonl")?;claude.retain(|p|p.parent().and_then(Path::parent)==Some(cfg.projects.as_path()));let mut codex=Vec::new();collect(&cfg.codex_sessions,0,&mut codex,".jsonl")?;let mut indexed=0;let mut skipped=0;
    for (path,native) in claude.into_iter().map(|p|(p,false)).chain(codex.into_iter().map(|p|(p,true))){let file=open_regular(&path)?;let st=descriptor(&file)?;let pathkey=path.to_string_lossy().into_owned();let current=stamp(&st);
        // Full parsing precedes native-cache admission: session_meta may occur
        // after a large preamble, and a new sidecar must still remove duplicates.
        let parsed=if native{Some(parse_transcript_fd(&file,true)?)}else{None};
        if let Some((meta,_))=&parsed{if meta.session_id.as_ref().is_some_and(|id|threads.contains(id)){tx.execute("DELETE FROM files WHERE path=?",[&pathkey])?;continue}}
        if !force&&cached.get(&pathkey).and_then(Option::as_ref)==Some(&current){skipped+=1;continue}
        let (meta,rows)=match parsed{Some(v)=>v,None=>parse_transcript_fd(&file,false)?};
        let (sid,project)=if native{let Some(id)=meta.session_id.as_deref().filter(|s|config::valid_id(s)) else{continue};let Some(cwd)=meta.cwd.as_deref() else{continue};if meta.internal{continue}(format!("codex:{id}"),config::project_slug(Path::new(cwd)))}else{identity(&path)?};
        tx.execute("DELETE FROM msg WHERE session_id=?",[&sid])?;write_rows(&tx,&sid,&project,&rows)?;tx.execute("INSERT OR REPLACE INTO sessions(session_id,project,cwd,title,first_ts,last_ts,messages,engine) VALUES(?,?,?,?,?,?,?,?)",params![sid,project,meta.cwd,meta.title,meta.first_ts,meta.last_ts,crate::beliefs::sql_integer(rows.len())?,meta.engine])?;
        tx.execute("INSERT OR REPLACE INTO files(path,stamp) VALUES(?,?)",params![pathkey,current])?;emit(cfg,&tx,&sid,&project,&rows)?;indexed+=1;
    }tx.commit()?;Ok(json!({"indexed":indexed,"skipped":skipped}))
}
pub fn fts_expr(query:&str,op:&str)->String{let mut tokens=Vec::new();let mut current=String::new();for c in query.chars(){if c.is_ascii_alphanumeric()||matches!(c,'_'|'.'|'/'|':'|'-'){current.push(c)}else if !current.is_empty(){tokens.push(format!("\"{current}\""));current.clear()}}if !current.is_empty(){tokens.push(format!("\"{current}\""))}tokens.join(op)}
fn valid_session(s:&str)->bool{config::valid_id(s)||s.strip_prefix("codex:").is_some_and(config::valid_id)}
pub fn search(cfg:&Config,req:&Value)->Result<Value>{
    let cwd=crate::beliefs::text(req,"cwd",4096)?;let query=crate::beliefs::text(req,"query",200)?;if query.chars().any(char::is_control){return Err(Error::InvalidRequest)}let conn=store::connect(cfg)?;let slug=config::project_slug(Path::new(cwd));let exprs=[fts_expr(query," "),fts_expr(query," OR ")];
    for scope in [Some(slug.as_str()),None]{for expr in &exprs{if expr.is_empty(){continue}let mut stmt=conn.prepare("SELECT session_id,project,snippet(msg,4,'[',']','…',16) FROM msg WHERE msg MATCH ? AND (? IS NULL OR project=?) ORDER BY bm25(msg) LIMIT 20")?;let rows=stmt.query_map(params![expr,scope,scope],|r|Ok((r.get::<_,String>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,String>(2)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;let mut hits=Vec::new();for (sid,project,snippet) in rows{let Some(project)=project else{continue};if valid_session(&sid)&&config::valid_slug(&project){hits.push(json!({"session_id":sid,"project":project,"snippet":crop(&one_line(&scrub::scrub(&snippet)?),280)}))}}if !hits.is_empty(){return Ok(json!(hits))}}
        // LIKE fallback escapes metacharacters; code identifiers and punctuation
        // are literal rather than wildcard expressions or executable FTS syntax.
        let pattern=format!("%{}%",query.replace('\\',"\\\\").replace('%',"\\%").replace('_',"\\_"));let mut stmt=conn.prepare("SELECT session_id,project,content FROM msg WHERE content LIKE ? ESCAPE '\\' AND (? IS NULL OR project=?) LIMIT 20")?;let mut hits=Vec::new();for row in stmt.query_map(params![pattern,scope,scope],|r|Ok((r.get::<_,String>(0)?,r.get::<_,Option<String>>(1)?,r.get::<_,String>(2)?)))?{let (sid,project,content)=row?;let Some(project)=project else{continue};if valid_session(&sid)&&config::valid_slug(&project){hits.push(json!({"session_id":sid,"project":project,"snippet":crop(&one_line(&scrub::scrub(&content)?),280)}))}}if !hits.is_empty(){return Ok(json!(hits))}
    }Ok(json!([]))
}
pub fn session(cfg:&Config,req:&Value)->Result<Value>{let sid=crate::beliefs::text(req,"session_id",134)?;if !valid_session(sid){return Err(Error::InvalidRequest)}let (offset,limit)=crate::beliefs::page(req,50)?;let conn=store::connect(cfg)?;let meta=conn.query_row("SELECT project,cwd,title,first_ts,last_ts,messages,engine FROM sessions WHERE session_id=?",[sid],|r|Ok(json!({"session_id":sid,"project":r.get::<_,Option<String>>(0)?,"cwd":r.get::<_,Option<String>>(1)?,"title":r.get::<_,Option<String>>(2)?,"first_ts":r.get::<_,Option<String>>(3)?,"last_ts":r.get::<_,Option<String>>(4)?,"messages":crate::beliefs::sql_count(r,5)?,"engine":r.get::<_,String>(6)?}))).optional()?.ok_or(Error::Changed)?;let mut stmt=conn.prepare("SELECT ts,role,content FROM msg WHERE session_id=? ORDER BY rowid LIMIT ? OFFSET ?")?;let mut rows=Vec::new();for row in stmt.query_map(params![sid,limit,offset],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)))?{let (ts,role,content)=row?;rows.push(json!({"ts":scrub::scrub(&ts)?,"role":role,"content":scrub::scrub(&content)?}))}let value=scrub::scrub_json(&json!({"session":meta,"rows":rows}))?;if serde_json::to_vec(&value).map_err(|_|Error::Unavailable)?.len()>crate::MAX_FRAME_BYTES-512{return Err(Error::TooLarge)}Ok(value)}

#[cfg(test)]mod tests{
    use super::*;use std::{fs::OpenOptions,io::{Seek,SeekFrom,Write}};
    fn fixture()->(tempfile::TempDir,Config){let temp=tempfile::tempdir_in("/home/docwilde/.cache/t").unwrap();let mut cfg=Config::for_root(temp.path().join("lore"));cfg.sync.enabled=true;cfg.sync.classes=["sessions".into(),"beliefs".into()].into_iter().collect();(temp,cfg)}
    fn user(content:&str)->String{format!("{}\n",json!({"type":"user","cwd":"/owned/fixture","timestamp":"2026-09-27T00:00:00Z","message":{"content":content}}))}
    fn count(conn:&Connection)->u64{conn.query_row("SELECT count(*) FROM msg",[],|r|crate::beliefs::sql_count(r,0)).unwrap()}
    #[test]fn live_fd_keeps_offset_retries_partial_tail_and_full_live_interleaving(){
        let(_temp,cfg)=fixture();let dir=cfg.projects.join("fixture");fs::create_dir_all(&dir).unwrap();let path=dir.join("owned.jsonl");fs::write(&path,format!("{}{{\"type\":\"assistant\",\"message\":{{\"content\":\"partial",user("first entry"))).unwrap();let mut file=open_regular(&path).unwrap();file.seek(SeekFrom::Start(7)).unwrap();let conn=store::connect(&cfg).unwrap();assert_eq!(index_live_fd(&cfg,&conn,&file,&path).unwrap(),(1,1));assert_eq!(file.stream_position().unwrap(),7);assert_eq!(count(&conn),1);assert_eq!(index_live_fd(&cfg,&conn,&file,&path).unwrap(),(0,1));
        OpenOptions::new().append(true).open(&path).unwrap().write_all(b" complete\"}}\n").unwrap();assert_eq!(index_live_fd(&cfg,&conn,&file,&path).unwrap(),(1,2));assert_eq!(count(&conn),2);drop(conn);
        assert_eq!(index(&cfg,&json!({"force":true})).unwrap()["indexed"],1);let conn=store::connect(&cfg).unwrap();assert_eq!(index_live_fd(&cfg,&conn,&file,&path).unwrap(),(2,2));assert_eq!(count(&conn),2);
        let ops:u64=conn.query_row("SELECT count(*) FROM sync_ops WHERE class='session'",[],|r|crate::beliefs::sql_count(r,0)).unwrap();assert!(ops>=8);let raw:String=conn.query_row("SELECT payload FROM sync_ops WHERE class='session' AND op='msgs' ORDER BY seq DESC LIMIT 1",[],|r|r.get(0)).unwrap();assert_eq!(serde_json::from_str::<Value>(&raw).unwrap()["rows"].as_array().unwrap().len(),2);
    }
    #[test]fn failed_fd_pass_rolls_back_delete_but_preserves_caller_transaction(){
        let(_temp,cfg)=fixture();let dir=cfg.projects.join("fixture");fs::create_dir_all(&dir).unwrap();let path=dir.join("owned.jsonl");fs::write(&path,user("previous searchable row")).unwrap();let file=open_regular(&path).unwrap();let conn=store::connect(&cfg).unwrap();index_live_fd(&cfg,&conn,&file,&path).unwrap();
        conn.execute_batch("BEGIN IMMEDIATE;UPDATE files SET lines_indexed=0;INSERT INTO reviewed(session_id,project,ts) VALUES('caller','fixture','now')").unwrap();fs::write(&path,b"\xff\n").unwrap();assert!(index_live_fd(&cfg,&conn,&file,&path).is_err());assert!(!conn.is_autocommit());assert_eq!(count(&conn),1);assert_eq!(conn.query_row("SELECT count(*) FROM reviewed WHERE session_id='caller'",[],|r|crate::beliefs::sql_count(r,0)).unwrap(),1);conn.execute_batch("ROLLBACK").unwrap();
        // Swapping the logical pathname must never redirect an already-owned fd.
        fs::write(&path,user("descriptor truth")).unwrap();let owned=open_regular(&path).unwrap();fs::rename(&path,dir.join("old.jsonl")).unwrap();fs::write(&path,user("replacement decoy")).unwrap();conn.execute("UPDATE files SET lines_indexed=0",[]).unwrap();index_live_fd(&cfg,&conn,&owned,&path).unwrap();let content:String=conn.query_row("SELECT content FROM msg",[],|r|r.get(0)).unwrap();assert_eq!(content,"descriptor truth");
    }
    #[test]fn native_codex_late_header_dedup_recovers_when_owned_sidecar_disappears(){
        let(_temp,cfg)=fixture();let claude=cfg.projects.join("fixture");fs::create_dir_all(&claude).unwrap();fs::create_dir_all(&cfg.codex_sessions).unwrap();let rollout=cfg.codex_sessions.join("rollout.jsonl");let preamble=format!("{}\n",json!({"type":"event_msg","payload":"x".repeat(70000)}));let meta=format!("{}\n",json!({"type":"session_meta","payload":{"id":"thread-owned","cwd":"/owned/fixture"}}));let row=format!("{}\n",json!({"type":"response_item","timestamp":"now","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"native search fact"},{"type":"reasoning","text":"must not index"}]}}));fs::write(&rollout,format!("{preamble}{meta}{row}")).unwrap();index(&cfg,&json!({})).unwrap();let conn=store::connect(&cfg).unwrap();assert_eq!(count(&conn),1);drop(conn);
        let sidecar=claude.join("doxa.codex.json");fs::write(&sidecar,json!({"thread_id":"thread-owned"}).to_string()).unwrap();fs::write(claude.join("doxa.jsonl"),user("one owned conversation")).unwrap();index(&cfg,&json!({})).unwrap();let conn=store::connect(&cfg).unwrap();assert_eq!(count(&conn),1);assert_eq!(conn.query_row("SELECT count(*) FROM sessions WHERE session_id='codex:thread-owned'",[],|r|crate::beliefs::sql_count(r,0)).unwrap(),0);drop(conn);fs::remove_file(sidecar).unwrap();index(&cfg,&json!({})).unwrap();let conn=store::connect(&cfg).unwrap();assert_eq!(count(&conn),2);let content:String=conn.query_row("SELECT content FROM msg WHERE session_id='codex:thread-owned'",[],|r|r.get(0)).unwrap();assert_eq!(content,"native search fact");
    }
    #[test]fn transcript_scrubbing_precedes_character_truncation_and_literal_search(){
        let(_temp,cfg)=fixture();let dir=cfg.projects.join("fixture");fs::create_dir_all(&dir).unwrap();let path=dir.join("owned.jsonl");let secret="sk-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ012345";fs::write(&path,user(&format!("{} {secret} literal_identifier 100%", "a".repeat(3990)))).unwrap();let (meta,rows)=parse_transcript_fd(&open_regular(&path).unwrap(),false).unwrap();assert_eq!(meta.engine,"claude");assert!(!rows[0].content.contains("sk-"));assert!(rows[0].content.chars().count()<=4000);
        fs::write(&path,user("literal_identifier and 100% exact")).unwrap();index(&cfg,&json!({})).unwrap();assert!(!search(&cfg,&json!({"cwd":"/fixture","query":"literal_identifier"})).unwrap().as_array().unwrap().is_empty());assert!(search(&cfg,&json!({"cwd":"/fixture","query":"no_such_identifier"})).unwrap().as_array().unwrap().is_empty());assert_eq!(fts_expr("a OR b; \"c\""," "),"\"a\" \"OR\" \"b\" \"c\"");
    }
    #[test]fn malformed_negative_cursor_and_session_count_are_refused(){let(_temp,cfg)=fixture();let dir=cfg.projects.join("fixture");fs::create_dir_all(&dir).unwrap();let path=dir.join("owned.jsonl");fs::write(&path,user("one row")).unwrap();let file=open_regular(&path).unwrap();let conn=store::connect(&cfg).unwrap();index_live_fd(&cfg,&conn,&file,&path).unwrap();conn.execute("UPDATE files SET lines_indexed=-1",[]).unwrap();assert!(index_live_fd(&cfg,&conn,&file,&path).is_err());assert!(conn.is_autocommit());assert_eq!(count(&conn),1);conn.execute("UPDATE sessions SET messages=-1",[]).unwrap();assert!(session(&cfg,&json!({"session_id":"owned"})).is_err());}

    #[test]fn nullable_remote_project_does_not_poison_local_search_or_session_metadata(){let(_temp,cfg)=fixture();let conn=store::connect(&cfg).unwrap();conn.execute("INSERT INTO sessions(session_id,project,messages,engine) VALUES('codex:remote',NULL,1,'codex')",[]).unwrap();conn.execute("INSERT INTO msg(session_id,project,ts,role,content) VALUES('codex:remote',NULL,'now','user','shared keyword')",[]).unwrap();conn.execute("INSERT INTO msg(session_id,project,ts,role,content) VALUES('local','owned-project','now','user','shared keyword')",[]).unwrap();drop(conn);let hits=search(&cfg,&json!({"cwd":"/owned/fixture","query":"shared keyword"})).unwrap();assert_eq!(hits.as_array().unwrap().len(),1);assert_eq!(hits[0]["session_id"],"local");let remote=session(&cfg,&json!({"session_id":"codex:remote"})).unwrap();assert!(remote["session"]["project"].is_null());assert_eq!(remote["rows"][0]["content"],"shared keyword");}

}
