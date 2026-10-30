"""Crash-safe checkpoint and locking helpers for application cycles."""
from __future__ import annotations
import hashlib, json, os, shutil
from pathlib import Path
from typing import Any

class ResumeRefused(ValueError):
    """A cycle checkpoint cannot safely be resumed."""

def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
        try:
            fd = os.open(str(path.parent), os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        except OSError: pass
    finally:
        try: tmp.unlink()
        except FileNotFoundError: pass

def regular_chain(path: Path) -> None:
    import stat
    for item in (path.absolute(),*path.absolute().parents):
        if not item.exists() and not item.is_symlink():continue
        st=item.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400:raise ResumeRefused('checkpoint path traverses link/reparse point')

def save(path: Path, state: dict[str, Any]) -> None:
    regular_chain(path)
    _atomic(path, canonical({"schema": 1, "state": state, "sha256": digest(canonical(state))}) + b"\n")

def load(path: Path) -> dict[str, Any]:
    regular_chain(path)
    try:
        with path.open('rb') as stream:raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024: raise ResumeRefused("checkpoint exceeds size limit")
        env = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResumeRefused(f"checkpoint unreadable: {exc}") from exc
    if not isinstance(env, dict) or set(env) != {"schema", "state", "sha256"} or env.get("schema") != 1 or not isinstance(env.get("state"), dict): raise ResumeRefused("checkpoint envelope malformed")
    state = env["state"]
    if env.get("sha256") != digest(canonical(state)): raise ResumeRefused("checkpoint integrity check failed")
    fields = {"schema", "config", "rounds_requested", "history", "previous", "prior_candidate", "next_round", "inflight", "status", "output_identity"}
    if set(state) != fields or state.get("schema") != 1: raise ResumeRefused("checkpoint state malformed or contains unknown fields")
    if not isinstance(state["history"], list) or type(state["next_round"]) is not int or type(state["rounds_requested"]) is not int: raise ResumeRefused("checkpoint state values malformed")
    if not 1 <= state['rounds_requested'] <= 8 or not 1 <= state['next_round'] <= state['rounds_requested']+1 or state['status'] not in ('HOLD','PASS','FAIL','INVALID') or not isinstance(state['previous'],dict):raise ResumeRefused('checkpoint counters/status malformed')
    if state['status']=='PASS' and (not state['history'] or state['history'][-1].get('status')!='PASS'):raise ResumeRefused('checkpoint success lacks committed evidence')
    if state["inflight"] is not None: raise ResumeRefused("cannot resume: external call was in flight; side effects are ambiguous")
    return state

def _argv_identity(argv: list[str]) -> list[dict[str, str]]:
    files = []
    for index, item in enumerate(argv):
        paths = [Path(item)]
        if index == 0 and not paths[0].is_absolute():
            resolved = shutil.which(item)
            if resolved: paths.insert(0, Path(resolved))
        for path in paths:
            try:
                if path.is_file():
                    size = path.stat().st_size
                    if size > 256 * 1024 * 1024:raise ResumeRefused('adapter identity file exceeds hashing bound')
                    hasher=hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda:stream.read(1048576),b''):hasher.update(chunk)
                    files.append({"argument_sha256":digest(item.encode()),"path_sha256":digest(str(path.resolve()).encode()),"sha256":hasher.hexdigest()})
                    break
            except OSError: continue
    return files

def _safe_argv(argv: list[str]) -> list[str]:
    safe=[]; redact=False
    for item in argv:
        upper=item.upper()
        if redact: safe.append("<credential>"); redact=False; continue
        if any(token in upper for token in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            if "=" in item: safe.append(item.split("=",1)[0]+"=<credential>")
            else: safe.append(item); redact=True
        else: safe.append(item)
    return safe

def configuration(req: Any, proposer: list[str], builder: list[str], rounds: int, refs: Any, reference_store: Path, source_paths: list[Path]) -> dict[str, Any]:
    sources=[]
    for path in source_paths:
        try: sources.append({"name":path.name,"sha256":digest(path.read_bytes())})
        except OSError as exc: raise ResumeRefused(f"cannot fingerprint source {path.name}: {exc}") from exc
    safe_config={key:os.environ.get(key,'') for key in ('AUTOFORGE_API_BASE','AUTOFORGE_MODEL')}
    commands={"proposer_sha256":digest(canonical(_safe_argv(proposer))),"builder_sha256":digest(canonical(_safe_argv(builder))),"proposer_files":_argv_identity(proposer),"builder_files":_argv_identity(builder)}
    return {"request_sha256":digest(canonical(req)),"references_sha256":digest(canonical(refs)),"reference_store":str(reference_store.resolve()),"rounds_requested":rounds,"commands":commands,"sources":sources,"api_config_sha256":digest(canonical(safe_config))}

class ExclusiveLock:
    """OS-managed exclusive lock, released automatically when process exits."""
    def __init__(self,path:Path): self.path,self.stream=path,None
    def __enter__(self):
        regular_chain(self.path)
        self.stream=self.path.open("a+b")
        try:
            if os.name=="nt":
                import msvcrt
                self.stream.seek(0)
                if not self.stream.read(1): self.stream.write(b"0"); self.stream.flush()
                self.stream.seek(0); msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,BlockingIOError) as exc:
            self.stream.close(); self.stream=None; raise ResumeRefused("another application cycle holds the output lock") from exc
        return self
    def __exit__(self,*_exc):
        if self.stream is not None:
            try:
                if os.name=="nt":
                    import msvcrt
                    self.stream.seek(0); msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(),fcntl.LOCK_UN)
            finally: self.stream.close()
