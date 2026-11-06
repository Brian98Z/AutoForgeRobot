"""Reusable local authentication primitives: password hashes and revocable sessions.

HTTP integration is deliberately left to trusted callers. Raw credentials and
session tokens are never returned in storage records or logged by this module.
"""
from __future__ import annotations
import hashlib, hmac, json, os, secrets, time
from pathlib import Path
from typing import Any
import application_checkpoint as checkpoint

class AuthRefused(ValueError): pass

def _canonical(v:Any)->bytes:return checkpoint.canonical(v)
def _atomic(p:Path,b:bytes)->None:checkpoint._atomic(p,b)
def _path(p:Path)->None:
    try: checkpoint.regular_chain(p)
    except Exception as e: raise AuthRefused(str(e)) from e

def hash_password(password:str, *, salt:bytes|None=None, iterations:int=600_000)->dict[str,Any]:
    if not isinstance(password,str) or len(password)<12 or len(password)>4096: raise AuthRefused('password must be 12..4096 characters')
    if type(iterations)is not int or not 100_000<=iterations<=2_000_000: raise AuthRefused('PBKDF2 iterations out of policy')
    salt=secrets.token_bytes(16) if salt is None else salt
    if len(salt)<16: raise AuthRefused('salt must be at least 16 bytes')
    digest=hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,iterations,dklen=32)
    return {'scheme':'pbkdf2-sha256','iterations':iterations,'salt':salt.hex(),'digest':digest.hex()}

def verify_password(password:str, record:dict[str,Any])->bool:
    if not isinstance(record,dict) or set(record)!={'scheme','iterations','salt','digest'} or record.get('scheme')!='pbkdf2-sha256': return False
    try:
        salt=bytes.fromhex(record['salt']); expected=bytes.fromhex(record['digest']); iterations=record['iterations']
        if type(iterations)is not int or not 100_000<=iterations<=2_000_000 or len(salt)<16 or len(expected)!=32:return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,iterations,dklen=32)
        return hmac.compare_digest(actual,expected)
    except (ValueError,TypeError,AttributeError):return False

class SessionStore:
    def __init__(self,path:Path, *, clock=time.time):
        self.path=Path(path); self.clock=clock; self.path.parent.mkdir(parents=True,exist_ok=True); _path(self.path)
        self.lock_path=self.path.with_name(self.path.name+'.lock')
    def _read(self)->dict[str,Any]:
        _path(self.path)
        if not self.path.exists():return {'schema':1,'sessions':{}}
        try: env=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as e:raise AuthRefused(f'session store unreadable: {e}') from e
        if not isinstance(env,dict) or set(env)!={'schema','state','sha256'} or env['schema']!=1 or env['sha256']!=checkpoint.digest(_canonical(env['state'])) or not isinstance(env['state'],dict) or not isinstance(env['state'].get('sessions'),dict):raise AuthRefused('session store integrity failure')
        return env['state']
    def _save(self,state:dict[str,Any])->None:
        env={'schema':1,'state':state,'sha256':checkpoint.digest(_canonical(state))}; _atomic(self.path,_canonical(env)+b'\n')
    def create(self,user_id:str,tenant_id:str,roles:list[str],ttl_seconds:int=3600)->tuple[str,dict[str,Any]]:
        if not user_id or not tenant_id or not isinstance(roles,list) or not roles or any(not isinstance(x,str) or not x for x in roles):raise AuthRefused('user, tenant, and roles required')
        if type(ttl_seconds)is not int or not 1<=ttl_seconds<=30*86400:raise AuthRefused('ttl must be 1..2592000 seconds')
        token=secrets.token_urlsafe(32); token_hash=hashlib.sha256(token.encode()).hexdigest(); now=int(self.clock())
        try:
            with checkpoint.ExclusiveLock(self.lock_path):
                state=self._read(); state['sessions'][token_hash]={'user_id':user_id,'tenant_id':tenant_id,'roles':sorted(set(roles)),'created_at':now,'expires_at':now+ttl_seconds,'revoked':False}; self._save(state)
        except checkpoint.ResumeRefused as e: raise AuthRefused(str(e)) from e
        return token,{'user_id':user_id,'tenant_id':tenant_id,'roles':sorted(set(roles)),'expires_at':now+ttl_seconds}
    def authenticate(self,token:str)->dict[str,Any]:
        if not isinstance(token,str) or not token:raise AuthRefused('invalid session')
        try:
            with checkpoint.ExclusiveLock(self.lock_path):
                state=self._read(); record=state['sessions'].get(hashlib.sha256(token.encode()).hexdigest()); now=int(self.clock())
                if not record or record.get('revoked') or record.get('expires_at',0)<=now:raise AuthRefused('invalid or expired session')
                return {k:record[k] for k in ('user_id','tenant_id','roles','expires_at')}
        except checkpoint.ResumeRefused as e: raise AuthRefused(str(e)) from e
    def revoke(self,token:str)->None:
        if not isinstance(token,str) or not token:raise AuthRefused('invalid session')
        try:
            with checkpoint.ExclusiveLock(self.lock_path):
                state=self._read(); key=hashlib.sha256(token.encode()).hexdigest(); rec=state['sessions'].get(key)
                if rec is None:return
                rec['revoked']=True; rec['revoked_at']=int(self.clock()); self._save(state)
        except checkpoint.ResumeRefused as e: raise AuthRefused(str(e)) from e

def authorize(principal:dict[str,Any]|None, *, role:str, tenant_id:str, resource_tenant_id:str|None=None)->None:
    if not isinstance(principal,dict):raise AuthRefused('authentication required')
    if tenant_id!=principal.get('tenant_id') or (resource_tenant_id is not None and resource_tenant_id!=principal.get('tenant_id')):raise AuthRefused('cross-tenant access refused')
    roles=principal.get('roles')
    if not isinstance(roles,list) or role not in roles:raise AuthRefused('required role missing')

def redact_auth_fields(value:Any)->Any:
    """Return recursively redacted data for model/log boundaries; never serialize tokens."""
    secret={'password','passwd','token','session','authorization','cookie','secret','credential','api_key','apikey','private_key'}
    if isinstance(value,dict):return {k:('[REDACTED]' if str(k).casefold() in secret else redact_auth_fields(v)) for k,v in value.items()}
    if isinstance(value,list):return [redact_auth_fields(v) for v in value]
    return value
