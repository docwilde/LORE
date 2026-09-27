//! Private bounded file access, shared locks and durable atomic replacement.
use std::{fs::{self,File},io::{Read,Write},path::{Path,PathBuf},time::{Duration,Instant}};
#[cfg(unix)]
use std::os::{fd::{AsRawFd,FromRawFd},unix::{ffi::OsStrExt,fs::{MetadataExt,PermissionsExt}}};
use crate::{Error,Result};

pub fn private_dir(path:&Path)->Result<()> {
    #[cfg(unix)] {
        use std::{ffi::CString,path::Component};
        if !path.is_absolute(){return Err(Error::UnsafePath);}
        let parts=path.components().filter_map(|part|match part{Component::RootDir|Component::CurDir=>None,Component::Normal(name)=>Some(Ok(name)),_=>Some(Err(Error::UnsafePath))}).collect::<Result<Vec<_>>>()?;
        if parts.is_empty(){return Err(Error::UnsafePath);}
        let mut parent=File::open("/")?;
        for(index,name)in parts.iter().enumerate(){
            let name=CString::new(name.as_bytes()).map_err(|_|Error::UnsafePath)?;
            let flags=libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC;
            let mut fd=unsafe{libc::openat(parent.as_raw_fd(),name.as_ptr(),flags)};
            let mut created=false;
            if fd<0&&std::io::Error::last_os_error().raw_os_error()==Some(libc::ENOENT){
                if parent.metadata()?.uid()!=unsafe{libc::geteuid()}{return Err(Error::UnsafePath);}
                if unsafe{libc::mkdirat(parent.as_raw_fd(),name.as_ptr(),0o700)}!=0&&std::io::Error::last_os_error().raw_os_error()!=Some(libc::EEXIST){return Err(Error::Unavailable);}
                fd=unsafe{libc::openat(parent.as_raw_fd(),name.as_ptr(),flags)};created=true;
            }
            if fd<0{return Err(Error::UnsafePath);}
            let directory=unsafe{File::from_raw_fd(fd)};
            if created||index+1==parts.len(){
                if directory.metadata()?.uid()!=unsafe{libc::geteuid()}{return Err(Error::UnsafePath);}
                directory.set_permissions(fs::Permissions::from_mode(0o700))?;
            }
            parent=directory;
        }
        Ok(())
    }
    #[cfg(not(unix))] {let _=path;Err(Error::Unsupported)}
}
/// Open every path component relative to the previously pinned directory.
/// Ancestor links are refused without changing any directory permissions.
#[cfg(unix)]
fn open_walk(path:&Path,directory:bool)->Result<File> {
    use std::{ffi::CString,path::Component};
    if !path.is_absolute(){return Err(Error::UnsafePath);}
    let mut parts=Vec::new();
    for part in path.components(){match part{Component::RootDir|Component::CurDir=>{},Component::Normal(name)=>parts.push(name),_=>return Err(Error::UnsafePath)}}
    let mut current=File::open("/")?;
    for (index,name) in parts.iter().enumerate(){
        let name=CString::new(name.as_bytes()).map_err(|_|Error::UnsafePath)?;
        let flags=libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK
            |if directory||index+1<parts.len(){libc::O_DIRECTORY}else{0};
        let fd=unsafe{libc::openat(current.as_raw_fd(),name.as_ptr(),flags)};
        if fd<0{let error=std::io::Error::last_os_error();return Err(if matches!(error.raw_os_error(),Some(libc::ELOOP)|Some(libc::ENOTDIR)){Error::UnsafePath}else{error.into()});}
        current=unsafe{File::from_raw_fd(fd)};
    }
    Ok(current)
}
pub fn open_directory(path:&Path)->Result<File> {
    #[cfg(unix)] {let file=open_walk(path,true)?;let meta=file.metadata()?;
        if !meta.is_dir()||meta.uid()!=unsafe{libc::geteuid()}{return Err(Error::UnsafePath);}Ok(file)}
    #[cfg(not(unix))] {let _=path;Err(Error::Unsupported)}
}
pub fn open_regular(path:&Path,cap:usize)->Result<File> {
    #[cfg(unix)] {let file=open_walk(path,false)?;let meta=file.metadata()?;
        if !meta.is_file(){return Err(Error::UnsafePath);}if meta.len()>cap as u64{return Err(Error::TooLarge);}
        if meta.nlink()!=1||meta.uid()!=unsafe{libc::geteuid()}{return Err(Error::UnsafePath);}Ok(file)}
    #[cfg(not(unix))] {let _=(path,cap);Err(Error::Unsupported)}
}
/// The directory descriptor remains alive until enumeration finishes, so a
/// path replacement cannot redirect the entries to another directory.
pub fn directory_names(path:&Path,cap:usize)->Result<Vec<std::ffi::OsString>> {
    let directory=open_directory(path)?;
    #[cfg(target_os="linux")] let entries=fs::read_dir(format!("/proc/self/fd/{}",directory.as_raw_fd()))?;
    #[cfg(not(target_os="linux"))] let entries=fs::read_dir(path)?;
    let mut names=Vec::new();for (index,entry) in entries.enumerate(){if index>=cap{return Err(Error::TooLarge);}names.push(entry?.file_name());}
    // Non-Linux systems have no proc descriptor path. Refuse a changed path
    // rather than returning entries from a replacement directory.
    #[cfg(all(unix,not(target_os="linux")))] {let checked=open_directory(path)?;let before=directory.metadata()?;let after=checked.metadata()?;
        if before.dev()!=after.dev()||before.ino()!=after.ino(){return Err(Error::Changed);}}
    Ok(names)
}
pub fn read_regular(path:&Path,cap:usize)->Result<Vec<u8>> {
    let mut file=open_regular(path,cap)?;
    let mut bytes=Vec::new();Read::by_ref(&mut file).take(cap as u64+1).read_to_end(&mut bytes)?;
    if bytes.len()>cap{return Err(Error::TooLarge);}Ok(bytes)
}
/// Create an owned file relative to an already pinned private directory.
#[cfg(unix)]
pub(crate) fn create_private_file(directory:&File,name:&std::ffi::OsStr,exclusive:bool)->Result<File> {
    let meta=directory.metadata()?;if !meta.is_dir()||meta.uid()!=unsafe{libc::geteuid()}{return Err(Error::UnsafePath);}
    let name=leaf(name)?;
    let flags=libc::O_RDWR|libc::O_CREAT|libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC|if exclusive{libc::O_EXCL}else{0};
    let fd=unsafe{libc::openat(directory.as_raw_fd(),name.as_ptr(),flags,0o600)};
    if fd<0{return Err(if std::io::Error::last_os_error().raw_os_error()==Some(libc::EEXIST){Error::Changed}else{Error::UnsafePath});}
    let file=unsafe{File::from_raw_fd(fd)};let meta=file.metadata()?;
    if !meta.is_file()||meta.nlink()!=1||meta.uid()!=unsafe{libc::geteuid()}{return Err(Error::UnsafePath);}
    file.set_permissions(fs::Permissions::from_mode(0o600))?;Ok(file)
}
#[cfg(unix)]
fn leaf(name:&std::ffi::OsStr)->Result<std::ffi::CString> {
    let bytes=name.as_bytes();if bytes.is_empty()||bytes==b"."||bytes==b".."||bytes.contains(&b'/'){return Err(Error::UnsafePath);}
    std::ffi::CString::new(bytes).map_err(|_|Error::UnsafePath)
}
/// Pin an owned entry without following its leaf or any replaced parent path.
#[cfg(unix)]
pub(crate) fn open_entry_at(directory:&File,name:&std::ffi::OsStr,is_directory:bool)->Result<File> {
    let name=leaf(name)?;let flags=libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC|if is_directory{libc::O_DIRECTORY}else{0};
    let fd=unsafe{libc::openat(directory.as_raw_fd(),name.as_ptr(),flags)};
    if fd<0{return Err(if std::io::Error::last_os_error().raw_os_error()==Some(libc::ENOENT){Error::Changed}else{Error::UnsafePath});}
    let file=unsafe{File::from_raw_fd(fd)};let meta=file.metadata()?;
    if meta.uid()!=unsafe{libc::geteuid()}||if is_directory{!meta.is_dir()}else{!meta.is_file()||meta.nlink()!=1}{return Err(Error::UnsafePath);}
    Ok(file)
}
#[cfg(unix)]
pub(crate) fn open_regular_at(directory:&File,name:&std::ffi::OsStr,cap:usize)->Result<File> {
    let file=open_entry_at(directory,name,false)?;if file.metadata()?.len()>cap as u64{return Err(Error::TooLarge);}Ok(file)
}
#[cfg(unix)]
fn same_entry(left:&File,right:&File)->Result<bool>{let a=left.metadata()?;let b=right.metadata()?;Ok(a.dev()==b.dev()&&a.ino()==b.ino())}
/// Move only into an absent leaf. The pinned source remains the proof of the
/// entry actually moved; a changed leaf or failed post-rename sync is partial.
#[cfg(unix)]
pub(crate) fn rename_at(source:&File,old:&std::ffi::OsStr,destination:&File,new:&std::ffi::OsStr,is_directory:bool)->Result<()> {
    let proof=open_entry_at(source,old,is_directory)?;let old=leaf(old)?;let new=leaf(new)?;
    #[cfg(target_os="linux")]
    let result=unsafe{libc::renameat2(source.as_raw_fd(),old.as_ptr(),destination.as_raw_fd(),new.as_ptr(),libc::RENAME_NOREPLACE)};
    #[cfg(target_os="macos")]
    let result=unsafe{libc::renameatx_np(source.as_raw_fd(),old.as_ptr(),destination.as_raw_fd(),new.as_ptr(),libc::RENAME_EXCL)};
    #[cfg(not(any(target_os="linux",target_os="macos")))]
    {if is_directory{return Err(Error::Unsupported);}return link_move_at(source,std::ffi::OsStr::from_bytes(old.as_bytes()),destination,std::ffi::OsStr::from_bytes(new.as_bytes()));}
    #[cfg(any(target_os="linux",target_os="macos"))]
    {
        if result!=0{return Err(if std::io::Error::last_os_error().raw_os_error()==Some(libc::EEXIST){Error::Changed}else{Error::Unavailable});}
        let actual=open_entry_at(destination,std::ffi::OsStr::from_bytes(new.as_bytes()),is_directory).map_err(|_|Error::MayHaveApplied)?;
        if !same_entry(&proof,&actual).map_err(|_|Error::MayHaveApplied)?{return Err(Error::MayHaveApplied);}
        source.sync_all().map_err(|_|Error::MayHaveApplied)?;destination.sync_all().map_err(|_|Error::MayHaveApplied)?;Ok(())
    }
}
/// Recovery links never replace a reused proposal ID. All changes remain
/// relative to pinned owned directories, including the temporary second link.
#[cfg(unix)]
pub(crate) fn link_move_at(source:&File,old:&std::ffi::OsStr,destination:&File,new:&std::ffi::OsStr)->Result<()> {
    let proof=open_entry_at(source,old,false)?;let old_name=leaf(old)?;let new_name=leaf(new)?;
    if unsafe{libc::linkat(source.as_raw_fd(),old_name.as_ptr(),destination.as_raw_fd(),new_name.as_ptr(),0)}!=0{return Err(if std::io::Error::last_os_error().raw_os_error()==Some(libc::EEXIST){Error::Changed}else{Error::Unavailable});}
    // The expected second hard link is allowed only during this owned move.
    let mut st=std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe{libc::fstatat(destination.as_raw_fd(),new_name.as_ptr(),st.as_mut_ptr(),libc::AT_SYMLINK_NOFOLLOW)}!=0{return Err(Error::MayHaveApplied);}
    let st=unsafe{st.assume_init()};let meta=proof.metadata().map_err(|_|Error::MayHaveApplied)?;
    if st.st_dev!=meta.dev()||st.st_ino!=meta.ino()||st.st_nlink!=2||meta.nlink()!=2{return Err(Error::MayHaveApplied);}
    if unsafe{libc::unlinkat(source.as_raw_fd(),old_name.as_ptr(),0)}!=0{return Err(Error::MayHaveApplied);}
    source.sync_all().map_err(|_|Error::MayHaveApplied)?;destination.sync_all().map_err(|_|Error::MayHaveApplied)?;Ok(())
}
#[cfg(unix)]
pub(crate) fn unlink_at(directory:&File,name:&std::ffi::OsStr)->Result<()> {
    let _proof=open_entry_at(directory,name,false)?;let name=leaf(name)?;
    if unsafe{libc::unlinkat(directory.as_raw_fd(),name.as_ptr(),0)}!=0{return Err(Error::Unavailable);}
    directory.sync_all().map_err(|_|Error::MayHaveApplied)
}
pub fn atomic_write(path:&Path,bytes:&[u8])->Result<()> {
    let parent=path.parent().ok_or(Error::UnsafePath)?;private_dir(parent)?;
    let directory=open_directory(parent)?;
    #[cfg(unix)] {
        let name=std::ffi::CString::new(path.file_name().ok_or(Error::UnsafePath)?.as_bytes()).map_err(|_|Error::UnsafePath)?;
        atomic_replace(&directory,&name,bytes)
    }
    #[cfg(not(unix))] {let _=(bytes,directory);Err(Error::Unsupported)}
}
#[cfg(unix)]
fn atomic_replace(directory:&File,name:&std::ffi::CStr,bytes:&[u8])->Result<()> {
        let fd=unsafe{libc::openat(directory.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC)};
        if fd>=0{let existing=unsafe{File::from_raw_fd(fd)};let meta=existing.metadata()?;
            if !meta.is_file()||meta.nlink()!=1||meta.uid()!=unsafe{libc::geteuid()}{return Err(Error::UnsafePath);}
        }else if std::io::Error::last_os_error().raw_os_error()!=Some(libc::ENOENT){return Err(Error::UnsafePath);}
        let temporary=format!(".lore-{}.tmp",uuid::Uuid::new_v4());
        let temp_name=std::ffi::CString::new(temporary.as_bytes()).map_err(|_|Error::UnsafePath)?;
        let mut temp=create_private_file(directory,std::ffi::OsStr::new(&temporary),true)?;
        let result=(||{
            temp.write_all(bytes)?;temp.sync_all()?;
            if unsafe{libc::renameat(directory.as_raw_fd(),temp_name.as_ptr(),directory.as_raw_fd(),name.as_ptr())}!=0{return Err(Error::Unavailable);}
            directory.sync_all().map_err(|_|Error::MayHaveApplied)?;Ok(())
        })();
        unsafe{libc::unlinkat(directory.as_raw_fd(),temp_name.as_ptr(),0)};
        result
}
pub struct Locks {files:Vec<File>}
impl Locks {
    pub fn acquire(root:&Path,paths:&[PathBuf],timeout:Duration)->Result<Self> {
        let lock_dir=root.join(".locks");private_dir(&lock_dir)?;
        let directory=open_directory(&lock_dir)?;
        let mut paths=paths.to_vec();paths.sort();paths.dedup();
        let mut guard=Self{files:Vec::new()};let until=Instant::now()+timeout;
        for path in paths {
            if !path.is_absolute(){return Err(Error::UnsafePath);}
            let name=format!("{}.lock",crate::digest(path.to_string_lossy().as_bytes()));
            #[cfg(unix)] let file=create_private_file(&directory,std::ffi::OsStr::new(&name),false)?;
            #[cfg(not(unix))] return Err(Error::Unsupported);let metadata=file.metadata()?;
            if !metadata.is_file(){return Err(Error::UnsafePath);}
            #[cfg(unix)] {
                if metadata.uid()!=unsafe{libc::geteuid()} || metadata.nlink()!=1{return Err(Error::UnsafePath);}
                while unsafe{libc::flock(file.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}!=0 {
                    if Instant::now()>=until{return Err(Error::Timeout);}
                    let code=std::io::Error::last_os_error().raw_os_error();
                    if !matches!(code,Some(libc::EWOULDBLOCK)|Some(libc::EINTR)){return Err(Error::Unavailable);}
                    std::thread::sleep(Duration::from_millis(2));
                }
            }
            #[cfg(not(unix))] return Err(Error::Unsupported);
            guard.files.push(file);
        }
        Ok(guard)
    }
}
impl Drop for Locks {
    fn drop(&mut self) {
        #[cfg(unix)] for file in self.files.iter().rev(){unsafe{libc::flock(file.as_raw_fd(),libc::LOCK_UN)};}
    }
}

#[cfg(test)] mod tests {
    use super::*;
    #[cfg(unix)] #[test]
    fn pinned_create_claim_recover_unlink_and_retire_ignore_replaced_directories() {
        use std::{ffi::OsStr,os::unix::fs::symlink};
        let temp=tempfile::tempdir().unwrap();let source=temp.path().join("source");let destination=temp.path().join("destination");
        private_dir(&source).unwrap();private_dir(&destination).unwrap();let src=open_directory(&source).unwrap();let dst=open_directory(&destination).unwrap();
        let outside=temp.path().join("outside");private_dir(&outside).unwrap();fs::write(outside.join("proposal"),b"outside original").unwrap();
        let old_source=temp.path().join("old-source");let old_destination=temp.path().join("old-destination");fs::rename(&source,&old_source).unwrap();fs::rename(&destination,&old_destination).unwrap();
        symlink(&outside,&source).unwrap();symlink(&outside,&destination).unwrap();
        let mut proposal=create_private_file(&src,OsStr::new("proposal"),true).unwrap();proposal.write_all(b"owned proposal").unwrap();proposal.sync_all().unwrap();src.sync_all().unwrap();
        let inode=proposal.metadata().unwrap().ino();rename_at(&src,OsStr::new("proposal"),&dst,OsStr::new("claimed"),false).unwrap();
        assert_eq!(open_regular_at(&dst,OsStr::new("claimed"),100).unwrap().metadata().unwrap().ino(),inode);
        link_move_at(&dst,OsStr::new("claimed"),&src,OsStr::new("proposal")).unwrap();assert_eq!(fs::read(old_source.join("proposal")).unwrap(),b"owned proposal");
        assert_eq!(open_regular_at(&src,OsStr::new("proposal"),100).unwrap().metadata().unwrap().nlink(),1);unlink_at(&src,OsStr::new("proposal")).unwrap();
        fs::create_dir(old_source.join("learned-skill")).unwrap();fs::write(old_source.join("learned-skill/SKILL.md"),b"owned learned skill").unwrap();
        rename_at(&src,OsStr::new("learned-skill"),&dst,OsStr::new("retired-skill"),true).unwrap();assert_eq!(fs::read(old_destination.join("retired-skill/SKILL.md")).unwrap(),b"owned learned skill");
        assert_eq!(fs::read(outside.join("proposal")).unwrap(),b"outside original");assert_eq!(fs::read_dir(&outside).unwrap().count(),1);
    }
    #[cfg(unix)] #[test]
    fn pinned_claims_refuse_links_invalid_leaves_and_existing_destinations() {
        use std::{ffi::OsStr,os::unix::fs::symlink};
        let temp=tempfile::tempdir().unwrap();let directory=open_directory(temp.path()).unwrap();
        fs::write(temp.path().join("proposal"),b"source").unwrap();fs::write(temp.path().join("occupied"),b"preserved").unwrap();
        assert_eq!(rename_at(&directory,OsStr::new("proposal"),&directory,OsStr::new("occupied"),false),Err(Error::Changed));
        assert_eq!(link_move_at(&directory,OsStr::new("proposal"),&directory,OsStr::new("occupied")),Err(Error::Changed));
        assert_eq!(fs::read(temp.path().join("proposal")).unwrap(),b"source");assert_eq!(fs::read(temp.path().join("occupied")).unwrap(),b"preserved");
        for name in ["", ".", "..", "../escape", "a/b"]{assert!(matches!(create_private_file(&directory,OsStr::new(name),true),Err(Error::UnsafePath)));}
        symlink("proposal",temp.path().join("linked")).unwrap();assert_eq!(unlink_at(&directory,OsStr::new("linked")),Err(Error::UnsafePath));
        fs::hard_link(temp.path().join("proposal"),temp.path().join("hardlink")).unwrap();assert_eq!(rename_at(&directory,OsStr::new("proposal"),&directory,OsStr::new("claimed"),false),Err(Error::UnsafePath));
        assert!(!temp.path().join("claimed").exists());assert_eq!(fs::read(temp.path().join("proposal")).unwrap(),b"source");
    }
    #[cfg(unix)] #[test]
    fn directory_swap_cannot_redirect_atomic_writes_or_private_chmod() {
        use std::os::unix::fs::symlink;
        let temp=tempfile::tempdir().unwrap();let outside=temp.path().join("outside");fs::create_dir(&outside).unwrap();
        fs::set_permissions(&outside,fs::Permissions::from_mode(0o755)).unwrap();fs::write(outside.join("memory"),b"outside").unwrap();
        let active=temp.path().join("active");private_dir(&active).unwrap();let pinned=open_directory(&active).unwrap();
        let moved=temp.path().join("moved");fs::rename(&active,&moved).unwrap();symlink(&outside,&active).unwrap();
        atomic_replace(&pinned,c"memory",b"owned replacement").unwrap();
        assert_eq!(fs::read(moved.join("memory")).unwrap(),b"owned replacement");assert_eq!(fs::read(outside.join("memory")).unwrap(),b"outside");
        assert_eq!(private_dir(&active),Err(Error::UnsafePath));assert_eq!(fs::metadata(&outside).unwrap().permissions().mode()&0o777,0o755);
        assert_eq!(atomic_write(&active.join("memory"),b"bad"),Err(Error::UnsafePath));
    }
    #[cfg(unix)] #[test] fn ancestor_links_are_refused_without_touching_outside_permissions() {
        use std::os::unix::fs::{symlink,PermissionsExt};
        let temp=tempfile::tempdir().unwrap();let outside=temp.path().join("outside");fs::create_dir(&outside).unwrap();
        fs::set_permissions(&outside,fs::Permissions::from_mode(0o755)).unwrap();fs::write(outside.join("fixture"),b"owned outside data").unwrap();
        let linked=temp.path().join("linked");symlink(&outside,&linked).unwrap();
        assert_eq!(read_regular(&linked.join("fixture"),100),Err(Error::UnsafePath));
        assert!(matches!(open_directory(&linked),Err(Error::UnsafePath)));
        assert_eq!(fs::metadata(&outside).unwrap().permissions().mode()&0o777,0o755);
        assert_eq!(fs::read(outside.join("fixture")).unwrap(),b"owned outside data");
        let real=temp.path().join("real");fs::create_dir(&real).unwrap();fs::write(real.join("fixture"),b"pinned").unwrap();
        let mut fd=open_regular(&real.join("fixture"),100).unwrap();fs::rename(&real,temp.path().join("old")).unwrap();symlink(&outside,&real).unwrap();
        let mut body=String::new();fd.read_to_string(&mut body).unwrap();assert_eq!(body,"pinned");
        assert_eq!(read_regular(&real.join("fixture"),100),Err(Error::UnsafePath));
    }
}
