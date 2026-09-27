//! Private bounded file access, shared locks and durable atomic replacement.
use std::{fs::{self,File,OpenOptions},io::{Read,Write},path::{Path,PathBuf},time::{Duration,Instant}};
#[cfg(unix)]
use std::os::{fd::AsRawFd,unix::fs::{MetadataExt,OpenOptionsExt,PermissionsExt}};
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
pub fn read_regular(path:&Path,cap:usize)->Result<Vec<u8>> {
    let mut options=OpenOptions::new();options.read(true);
    #[cfg(unix)] options.custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC);
    let mut file=options.open(path)?;
    let meta=file.metadata()?;
    if !meta.is_file() || meta.len()>cap as u64 {return Err(Error::TooLarge);}
    #[cfg(unix)] if meta.nlink()!=1 || meta.uid()!=unsafe{libc::geteuid()} {return Err(Error::UnsafePath);}
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
