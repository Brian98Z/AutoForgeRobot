"""Private operator secret files for explicitly owned runtime mounts."""
from __future__ import annotations
import hashlib, json, os, re, subprocess, getpass
from pathlib import Path

class SecretInjectionRefused(ValueError): pass
_NAME=re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
def _restrict_acl(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        user=getpass.getuser()
        args=["icacls",str(path),"/inheritance:r","/grant:r",f"{user}:F","SYSTEM:F"]
        if directory: args.extend(["/T","/C"])
        try: subprocess.run(args,check=True,capture_output=True,timeout=10)
        except (OSError,subprocess.SubprocessError) as exc: raise SecretInjectionRefused("could not restrict Windows secret ACL") from exc
    else:
        os.chmod(path,0o700 if directory else 0o600)

def prepare_runtime_secrets(secret_values: dict[str,str], *, private_root: str|Path, session_id: str, allowed_names: set[str]|None=None) -> dict:
    if not isinstance(session_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",session_id): raise SecretInjectionRefused("session id malformed")
    if not isinstance(secret_values,dict) or not secret_values or len(secret_values)>32: raise SecretInjectionRefused("provide 1..32 operator secrets")
    if allowed_names is not None and (not isinstance(allowed_names,set) or not set(secret_values)<=allowed_names): raise SecretInjectionRefused("secret name outside explicit allowlist")
    for name,value in secret_values.items():
        if not isinstance(name,str) or not _NAME.fullmatch(name) or not isinstance(value,str) or not value or len(value.encode())>16384 or "\x00" in value: raise SecretInjectionRefused("secret names/values must be bounded text")
    raw_root=Path(private_root)
    if raw_root.is_symlink(): raise SecretInjectionRefused("private root symlink refused")
    try: raw_root.resolve(strict=True).relative_to(raw_root.absolute())
    except (OSError,ValueError) as exc: raise SecretInjectionRefused("private root resolves through an unexpected parent") from exc
    root=raw_root.resolve(strict=True)
    if not root.is_dir(): raise SecretInjectionRefused("private root must be a real directory")
    directory=root/(session_id+"-"+os.urandom(12).hex())
    directory.mkdir(mode=0o700)
    _restrict_acl(directory,directory=True)
    files=[]
    try:
        for name,value in sorted(secret_values.items()):
            p=directory/name
            fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            data=value.encode("utf-8")
            with os.fdopen(fd,"wb") as f: f.write(data);f.flush();os.fsync(f.fileno())
            files.append({"source":str(p.resolve(strict=True)),"target":"/run/autoforge/secrets/"+name,"read_only":True,"name":name,"sha256":hashlib.sha256(data).hexdigest()})
            _restrict_acl(p,directory=False)
    except BaseException:
        for item in files:
            try:Path(item["source"]).unlink()
            except OSError:pass
        try:directory.rmdir()
        except OSError:pass
        raise
    return {"private_root":str(root),"directory":str(directory),"session_id":session_id,"mounts":files,"secret_names":[x["name"] for x in files],"values_exposed":False}

def secrets_module_token():
    return os.urandom(12).hex()

def resolve_secret_files(config: dict, *, mounted_root: str|Path) -> dict[str,str]:
    if not isinstance(config,dict): raise SecretInjectionRefused("runtime config must be a mapping")
    root=Path(mounted_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir(): raise SecretInjectionRefused("mounted secret root invalid")
    resolved={}
    for key,value in config.items():
        if isinstance(value,dict) and set(value)=={"$secret_file"}:
            target=value["$secret_file"]
            if not isinstance(target,str) or not target.startswith("/run/autoforge/secrets/"): raise SecretInjectionRefused("secret reference outside fixed mount")
            name=target.rsplit("/",1)[-1]
            if not _NAME.fullmatch(name): raise SecretInjectionRefused("secret reference name invalid")
            p=root/name
            if p.is_symlink() or not p.is_file() or p.resolve(strict=True).parent!=root: raise SecretInjectionRefused("secret mount file missing or unsafe")
            if p.stat().st_size>16384: raise SecretInjectionRefused("secret file exceeds bound")
            resolved[key]=p.read_text(encoding="utf-8")
        elif isinstance(value,(str,int,float,bool)) or value is None: resolved[key]=value
        else: raise SecretInjectionRefused("config value must be scalar or explicit secret-file reference")
    return resolved

def cleanup_runtime_secrets(manifest: dict, *, audit_path: str|Path) -> dict:
    if not isinstance(manifest,dict) or not isinstance(manifest.get("mounts"),list): raise SecretInjectionRefused("invalid secret ownership manifest")
    root_raw=Path(manifest.get("private_root",""));directory_raw=Path(manifest.get("directory",""))
    if root_raw.is_symlink() or directory_raw.is_symlink(): raise SecretInjectionRefused("secret ownership symlink refused")
    root=root_raw.resolve(strict=True);directory=directory_raw.resolve(strict=True)
    session_id=manifest.get("session_id")
    if not isinstance(session_id,str) or root not in directory.parents or not directory.name.startswith(session_id+"-"): raise SecretInjectionRefused("secret directory ownership check failed")
    removed=[]
    for item in manifest["mounts"]:
        p=Path(item.get("source",""))
        if p.is_symlink() or p.parent!=directory or p.name!=item.get("name") or not p.is_file(): raise SecretInjectionRefused("refusing unowned secret path")
        digest=hashlib.sha256(p.read_bytes()).hexdigest()
        if digest!=item.get("sha256"): raise SecretInjectionRefused("secret changed after mount; retain for operator inspection")
        p.unlink();removed.append({"name":p.name,"sha256":digest})
    directory.rmdir();audit=Path(audit_path);audit.parent.mkdir(parents=True,exist_ok=True)
    with audit.open("a",encoding="utf-8") as f:f.write(json.dumps({"event":"runtime_secrets_cleaned","session_id":manifest["session_id"],"files":removed},sort_keys=True)+"\n")
    return {"deleted":len(removed),"audit_path":str(audit),"values_exposed":False}
