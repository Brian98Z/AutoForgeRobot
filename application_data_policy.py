"""Explicit endpoint-bound disclosure, redaction, retention and deletion controls."""
from __future__ import annotations
import hashlib, json, os, re, time
from pathlib import Path
from urllib.parse import urlsplit

class DataPolicyRefused(ValueError): pass

SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
SECRET_KEYS = {"password", "passwd", "token", "authorization", "cookie", "secret", "credential", "api_key", "apikey", "private_key"}

def endpoint_identity(url: str) -> str:
    if not isinstance(url, str): raise DataPolicyRefused("endpoint must be exact URL text")
    u=urlsplit(url)
    if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise DataPolicyRefused("endpoint must be an exact HTTP(S) origin/path without credentials, query or fragment")
    return f"{u.scheme}://{u.netloc}{u.path or '/'}"

def disclose(*, endpoint: str, configured_endpoint: str|None, sensitivity: str, max_sensitivity: str, value, max_bytes: int, redaction_keys=()) -> dict:
    exact=endpoint_identity(endpoint)
    if configured_endpoint is None or exact != endpoint_identity(configured_endpoint): raise DataPolicyRefused("no exact authorized endpoint binding; no provider fallback")
    if sensitivity not in SENSITIVITY or max_sensitivity not in SENSITIVITY or SENSITIVITY[sensitivity] > SENSITIVITY[max_sensitivity]: raise DataPolicyRefused("disclosure sensitivity exceeds endpoint grant")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1_000_000: raise DataPolicyRefused("invalid disclosure byte bound")
    if not isinstance(redaction_keys,(list,tuple,set,frozenset)) or any(not isinstance(k,str) or not k for k in redaction_keys): raise DataPolicyRefused("redaction keys must be explicit text names")
    keys=SECRET_KEYS|{k.casefold() for k in redaction_keys}
    def clean(v):
        if isinstance(v,dict): return {k:("[REDACTED]" if str(k).casefold() in keys else clean(x)) for k,x in v.items()}
        if isinstance(v,list): return [clean(x) for x in v]
        return v
    encoded=json.dumps(clean(value),ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
    if len(encoded)>max_bytes: raise DataPolicyRefused("redacted disclosure exceeds byte bound")
    return {"endpoint":exact,"sensitivity":sensitivity,"payload":json.loads(encoded),"bytes":len(encoded),"sha256":hashlib.sha256(encoded).hexdigest()}

def _safe_target(path: str|Path, allowed_root: str|Path) -> Path:
    root_raw=Path(allowed_root)
    if root_raw.is_symlink(): raise DataPolicyRefused("data root symlink refused")
    root=root_raw.resolve(strict=True); raw=Path(path)
    if not raw.is_absolute(): raise DataPolicyRefused("retention path must be absolute")
    try:
        relative=raw.absolute().relative_to(root_raw.absolute())
    except ValueError as exc:
        raise DataPolicyRefused("retention path outside dedicated data root") from exc
    cur=root
    for part in relative.parts[:-1]:
        cur=cur/part
        if cur.is_symlink(): raise DataPolicyRefused("symlink path refused")
    target=cur/relative.parts[-1]
    if target.is_symlink() or (target.exists() and not target.is_file()): raise DataPolicyRefused("target must be a regular file")
    return target

def retain(path: str|Path, allowed_root: str|Path, data: bytes, *, expires_at: int, now: int|None=None) -> dict:
    if not isinstance(data,bytes) or type(expires_at) is not int or expires_at <= (int(time.time()) if now is None else now): raise DataPolicyRefused("retention requires bytes and a future expiry")
    target=_safe_target(path,allowed_root); target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists(): raise DataPolicyRefused("retention target already exists")
    tmp=target.with_name(target.name+".tmp")
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"wb") as f: f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,target)
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass
    return {"path":str(target),"sha256":hashlib.sha256(data).hexdigest(),"expires_at":expires_at}

def delete_expired(path: str|Path, allowed_root: str|Path, *, expires_at: int, audit_path: str|Path, now: int|None=None) -> dict:
    current=int(time.time()) if now is None else now
    if type(expires_at) is not int or expires_at>current: raise DataPolicyRefused("retained data not proven expired")
    target=_safe_target(path,allowed_root)
    if not target.is_file(): raise DataPolicyRefused("retained file missing")
    digest=hashlib.sha256(target.read_bytes()).hexdigest();audit=Path(audit_path);audit.parent.mkdir(parents=True,exist_ok=True)
    record={"event":"expired_data_deleted","path":str(target),"sha256":digest,"expires_at":expires_at,"deleted_at":current}
    with audit.open("a",encoding="utf-8") as f:f.write(json.dumps(record,sort_keys=True)+"\n");f.flush();os.fsync(f.fileno())
    target.unlink()
    return {"deleted":True,"sha256":digest,"audit_path":str(audit)}
