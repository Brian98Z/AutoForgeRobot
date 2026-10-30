use std::{fs::{self, OpenOptions}, io::Write, path::Path};
#[cfg(unix)]
use std::fs::File;

pub type Result<T, E = String> = std::result::Result<T, E>;

fn sync_dir(path: &Path) -> Result<()> {
    #[cfg(unix)] { File::open(path).and_then(|f|f.sync_all()).map_err(|e|format!("sync directory {}: {e}",path.display())) }
    #[cfg(windows)] { let _=path;Ok(()) }
    #[cfg(not(any(unix,windows)))] {let _=path;Err("durable publication unsupported on this platform".into())}
}

#[cfg(windows)]
fn publish_noreplace(src: &Path, dst: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    #[link(name = "kernel32")]
    extern "system" { fn MoveFileExW(existing: *const u16, new: *const u16, flags: u32) -> i32; }
    let a: Vec<u16> = src.as_os_str().encode_wide().chain(Some(0)).collect();
    let b: Vec<u16> = dst.as_os_str().encode_wide().chain(Some(0)).collect();
    const MOVEFILE_WRITE_THROUGH: u32 = 0x8;
    let ok = unsafe { MoveFileExW(a.as_ptr(), b.as_ptr(), MOVEFILE_WRITE_THROUGH) };
    if ok == 0 { return Err(format!("no-replace durable publication {} -> {} failed: {}", src.display(), dst.display(), std::io::Error::last_os_error())); }
    Ok(())
}

#[cfg(unix)]
fn publish_noreplace(src: &Path, dst: &Path) -> Result<()> {
    // A same-filesystem hard link is an atomic no-clobber publication.
    fs::hard_link(src, dst).map_err(|e| format!("no-replace publication {}: {e}", dst.display()))?;
    if let Some(parent) = dst.parent() { if let Err(e) = sync_dir(parent) { eprintln!("warning: publication committed at {} but directory sync failed: {e}", dst.display()); } }
    Ok(())
}
pub fn publish_file(dst: &Path, data: &[u8]) -> Result<()> {
    let parent=dst.parent().ok_or("destination has no parent")?;
    let name=dst.file_name().ok_or("destination has no filename")?.to_string_lossy();
    let tmp=parent.join(format!(".{name}.{}.stage",rand::random::<u64>()));
    let result=(|| {
        let mut file=OpenOptions::new().write(true).create_new(true).open(&tmp).map_err(|e|format!("create staged file: {e}"))?;
        file.write_all(data).and_then(|_|file.sync_all()).map_err(|e|format!("sync staged file: {e}"))?;
        drop(file);
        publish_noreplace(&tmp,dst)?;
        Ok(())
    })();
    if tmp.exists(){if let Err(e)=fs::remove_file(&tmp){if result.is_ok(){eprintln!("warning: committed publication but could not remove stage {}: {e}",tmp.display());}}}
    result
}

#[cfg(unix)]
fn sync_tree_dirs(path:&Path)->Result<()> {
    for item in fs::read_dir(path).map_err(|e|e.to_string())? {
        let item=item.map_err(|e|e.to_string())?;
        if item.file_type().map_err(|e|e.to_string())?.is_dir(){sync_tree_dirs(&item.path())?;}
    }
    sync_dir(path)
}

pub fn publish_dir_noreplace(src:&Path,dst:&Path)->Result<()> {
    #[cfg(unix)] {
        sync_tree_dirs(src)?;
        use std::os::unix::ffi::OsStrExt;
        let a=std::ffi::CString::new(src.as_os_str().as_bytes()).map_err(|e|e.to_string())?;
        let b=std::ffi::CString::new(dst.as_os_str().as_bytes()).map_err(|e|e.to_string())?;
        #[cfg(target_os="linux")] {
            unsafe extern "C" { fn renameat2(olddirfd:i32,oldpath:*const i8,newdirfd:i32,newpath:*const i8,flags:u32)->i32; }
            const AT_FDCWD:i32=-100;const RENAME_NOREPLACE:u32=1;
            let rc=unsafe{renameat2(AT_FDCWD,a.as_ptr(),AT_FDCWD,b.as_ptr(),RENAME_NOREPLACE)};
            if rc!=0{return Err(format!("atomic no-replace directory publish failed: {}",std::io::Error::last_os_error()));}
            if let Err(e)=sync_dir(dst.parent().ok_or("destination has no parent")?){eprintln!("warning: directory publication committed but durability unconfirmed: {e}");}
        }
        #[cfg(not(target_os="linux"))] { let _=(a,b); return Err("atomic no-replace directory publication unsupported on this Unix platform".into()); }
    }
    #[cfg(windows)] { publish_noreplace(src,dst)?; }
    #[cfg(not(any(unix,windows)))] { let _=(src,dst); return Err("durable directory publication unsupported on this platform".into()); }
    Ok(())
}

pub fn sync_parent(path:&Path)->Result<()> {sync_dir(path.parent().ok_or("path has no parent")?)}

