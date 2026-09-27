//! Private bounded file access, shared locks and durable atomic replacement.
use std::{fs::{self,File,OpenOptions},io::{Read,Write},path::{Path,PathBuf},time::{Duration,Instant}};
#[cfg(unix)]
use std::os::{fd::{AsRawFd,FromRawFd},unix::{ffi::OsStrExt,fs::{MetadataExt,OpenOptionsExt,PermissionsExt}}};
use crate::{Error,Result};

pub fn private_dir(path:&Path)->Result<()> {
    // Never traverse a planted directory link while installing private state.
    // Configured roots can be canonicalized once by their trusted caller.
    for ancestor in path.ancestors() {
        if fs::symlink_metadata(ancestor).is_ok_and(|meta|meta.file_type().is_symlink()) {return Err(Error::UnsafePath);}
    }
    if !path.exists() {
        if let Some(parent)=path.parent() {if !parent.is_dir(){private_dir(parent)?;}}
        fs::create_dir(path).or_else(|e|if e.kind()==std::io::ErrorKind::AlreadyExists {Ok(())}else{Err(e)})?;
    }
    let metadata=fs::symlink_metadata(path)?;
    if !metadata.file_type().is_dir(){return Err(Error::UnsafePath);}
    #[cfg(unix)] {
        if metadata.uid()!=unsafe{libc::geteuid()}{return Err(Error::UnsafePath);}
        fs::set_permissions(path,fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
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
pub fn atomic_write(path:&Path,bytes:&[u8])->Result<()> {
    let parent=path.parent().ok_or(Error::UnsafePath)?;private_dir(parent)?;
    if let Ok(meta)=fs::symlink_metadata(path) {
        if !meta.is_file(){return Err(Error::UnsafePath);}
        #[cfg(unix)] if meta.nlink()!=1 || meta.uid()!=unsafe{libc::geteuid()} {return Err(Error::UnsafePath);}
    }
    let mut temp=tempfile::NamedTempFile::new_in(parent)?;
    #[cfg(unix)] temp.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
    temp.write_all(bytes)?;temp.as_file().sync_all()?;
    temp.persist(path).map_err(|_|Error::Unavailable)?;
    File::open(parent)?.sync_all()?;Ok(())
}
pub struct Locks {files:Vec<File>}
impl Locks {
    pub fn acquire(root:&Path,paths:&[PathBuf],timeout:Duration)->Result<Self> {
        let lock_dir=root.join(".locks");private_dir(&lock_dir)?;
        let mut paths=paths.to_vec();paths.sort();paths.dedup();
        let mut guard=Self{files:Vec::new()};let until=Instant::now()+timeout;
        for path in paths {
            if !path.is_absolute(){return Err(Error::UnsafePath);}
            let lock=lock_dir.join(format!("{}.lock",crate::digest(path.to_string_lossy().as_bytes())));
            let mut options=OpenOptions::new();options.read(true).write(true).create(true);
            #[cfg(unix)] options.mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC);
            let file=options.open(lock)?;let metadata=file.metadata()?;
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
