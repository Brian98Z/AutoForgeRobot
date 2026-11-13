#!/usr/bin/env python3
"""Durable, local application business components with an explicit network boundary.

This is a conservative single-host runtime: SQLite is the source of truth, files are
content-addressed durable objects, and network calls require exact host/port authority.
No generated code is loaded or executed here.
"""
from __future__ import annotations
import argparse, hashlib, http.client, json, os, re, sqlite3, time, urllib.parse, uuid
from pathlib import Path
from typing import Any

MAX_OBJECT = 16 * 1024 * 1024
MAX_EXPORT = 32 * 1024 * 1024
MAX_CACHE_ITEMS = 1024
MAX_CACHE_VALUE = 256 * 1024
MAX_HTTP_BODY = 1024 * 1024
MAX_STORED_ROWS = 100_000
MAX_OBJECT_STORAGE = 256 * 1024 * 1024
MAX_QUEUE_PAYLOAD = 256 * 1024

class ComponentError(ValueError): pass


def _json(value: Any) -> str:
    try: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as e: raise ComponentError('value is not finite JSON data') from e


def _safe_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 512 or '\x00' in value:
        raise ComponentError('invalid object name')
    if value.startswith('/') or '\\' in value or any(part in ('.', '..') for part in value.split('/')):
        raise ComponentError('object name must be a safe relative path')
    return value

def _is_reparse(path: Path) -> bool:
    try:
        import stat
        return path.is_symlink() or bool(getattr(path.lstat(),'st_file_attributes',0) & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400))
    except FileNotFoundError:return False

class ComponentRuntime:
    """Local durable components with an explicit network boundary."""
    def __init__(self, root: str | Path, *, authorized_hosts: set[str] | None = None,
                 network_authorized: bool = False, tls_context=None, clock=time.time):
        raw=Path(root).expanduser()
        if _is_reparse(raw):raise ComponentError('runtime root may not be a symlink or reparse point')
        raw.mkdir(parents=True,exist_ok=True)
        self.root=raw.resolve(strict=True)
        self.blobs=self.root/'objects'
        if _is_reparse(self.blobs):raise ComponentError('object root may not be a symlink or reparse point')
        self.blobs.mkdir(exist_ok=True)
        self.db=self.root/'components.sqlite3'
        if _is_reparse(self.db):raise ComponentError('database may not be a symlink or reparse point')
        self.clock=clock
        self.authorized_hosts={h.lower() for h in (authorized_hosts or set())}
        self.network_authorized=network_authorized
        self.tls_context=tls_context
        if self.tls_context is not None and not isinstance(self.tls_context,__import__('ssl').SSLContext):raise ComponentError('tls_context must be a verified SSLContext')
        self._init_db()
    def _connect(self):
        if _is_reparse(self.db) or _is_reparse(self.root) or _is_reparse(self.blobs):raise ComponentError('runtime storage path changed to reparse point')
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, exc_type, exc, tb):
                try:return super().__exit__(exc_type, exc, tb)
                finally:self.close()
        c=sqlite3.connect(self.db,timeout=10,factory=ClosingConnection);c.row_factory=sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA journal_mode=WAL');return c
    def _init_db(self):
        with self._connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS objects(name TEXT PRIMARY KEY, digest TEXT NOT NULL, size INTEGER NOT NULL, content_type TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, available REAL NOT NULL, lease_until REAL, lease_token TEXT, result TEXT, error TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY, payload TEXT NOT NULL, interval REAL NOT NULL, next_run REAL NOT NULL, enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL, touched REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, operation TEXT NOT NULL, outcome TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL);''')
            columns={r[1] for r in c.execute('PRAGMA table_info(queue)')}
            if 'lease_token' not in columns:c.execute('ALTER TABLE queue ADD COLUMN lease_token TEXT')
            columns={r[1] for r in c.execute('PRAGMA table_info(idempotency)')}
            if 'request_digest' not in columns:c.execute('ALTER TABLE idempotency ADD COLUMN request_digest TEXT')
    def upload(self,name:str,data:bytes,content_type='application/octet-stream',*,overwrite=False)->dict:
        name=_safe_name(name)
        if not isinstance(data,bytes) or len(data)>MAX_OBJECT:raise ComponentError('upload must be bytes no larger than 16 MiB')
        if not isinstance(content_type,str) or len(content_type)>256 or '\r' in content_type or '\n' in content_type:raise ComponentError('invalid content type')
        digest=hashlib.sha256(data).hexdigest();shard=self.blobs/digest[:2]
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT digest,size FROM objects WHERE name=?',(name,)).fetchone()
            if old and not overwrite:raise ComponentError('object name already exists')
            if not old and c.execute('SELECT COUNT(*) FROM objects').fetchone()[0]>=MAX_STORED_ROWS:raise ComponentError('object capacity reached')
            if shard.exists() and (_is_reparse(shard) or self.blobs not in shard.resolve().parents):raise ComponentError('unsafe object shard path')
            shard.mkdir(exist_ok=True);path=shard/digest[2:]
            if not path.exists():
                new_usage=c.execute('SELECT COALESCE(SUM(size),0) FROM objects').fetchone()[0]-(old['size'] if old else 0)
                if new_usage+len(data)>MAX_OBJECT_STORAGE:raise ComponentError('object storage quota reached')
                tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
                try:
                    with tmp.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
                    try:os.link(tmp,path)
                    except FileExistsError:pass
                finally:tmp.unlink(missing_ok=True)
            c.execute('INSERT INTO objects VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET digest=excluded.digest,size=excluded.size,content_type=excluded.content_type,created=excluded.created',(name,digest,len(data),content_type,self.clock()))
            if old and old['digest']!=digest and c.execute('SELECT COUNT(*) FROM objects WHERE digest=?',(old['digest'],)).fetchone()[0]==0:
                stale=self.blobs/old['digest'][:2]/old['digest'][2:]
                if not _is_reparse(stale):stale.unlink(missing_ok=True)
        return {'name':name,'sha256':digest,'size':len(data),'content_type':content_type}
    def download(self,name:str)->tuple[dict,bytes]:
        with self._connect() as c: row=c.execute('SELECT * FROM objects WHERE name=?',( _safe_name(name),)).fetchone()
        if not row: raise ComponentError('object not found')
        p=self.blobs/row['digest'][:2]/row['digest'][2:]; data=p.read_bytes()
        if len(data)!=row['size'] or hashlib.sha256(data).hexdigest()!=row['digest']: raise ComponentError('stored object integrity failure')
        return ({'name':row['name'],'sha256':row['digest'],'size':row['size'],'content_type':row['content_type']},data)
    def enqueue(self,payload:Any,*,max_attempts=3,delay=0,idempotency_key:str|None=None)->dict:
        if type(max_attempts) is not int or not 1<=max_attempts<=10 or type(delay) not in (int,float) or not 0<=delay<=86400: raise ComponentError('retry count must be 1..10 and delay 0..86400 seconds')
        body=_json(payload)
        if len(body.encode())>MAX_QUEUE_PAYLOAD:raise ComponentError('queue payload exceeds 256 KiB')
        key=idempotency_key
        if key is not None and (not isinstance(key,str) or not key or len(key)>256): raise ComponentError('invalid idempotency key')
        now=self.clock(); job_id=uuid.uuid4().hex
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if key:
                prior=c.execute('SELECT operation,outcome,request_digest FROM idempotency WHERE key=?',(key,)).fetchone()
                if prior:
                    if prior['operation']!='queue':raise ComponentError('idempotency key already used for another operation')
                    expected=json.loads(prior['request_digest'])
                    if expected[:2]!=[body,max_attempts] or expected[2]!=now+delay:raise ComponentError('idempotency key reused with different queue parameters')
                    return json.loads(prior['outcome'])
            if c.execute('SELECT COUNT(*) FROM queue').fetchone()[0]>=MAX_STORED_ROWS:raise ComponentError('queue capacity reached')
            result={'id':job_id,'state':'queued','attempts':0,'max_attempts':max_attempts,'available_at':now+delay}
            c.execute('INSERT INTO queue(id,payload,state,attempts,max_attempts,available,created) VALUES(?,?,?,0,?,?,?)',(job_id,body,'queued',max_attempts,now+delay,now))
            if key:c.execute('INSERT INTO idempotency(key,operation,outcome,created,request_digest) VALUES(?,?,?,?,?)',(key,'queue',_json(result),now,_json([body,max_attempts,now+delay])))
        return result
    def claim(self,*,lease_seconds=60)->dict|None:
        if type(lease_seconds) not in (int,float) or not 1<=lease_seconds<=3600: raise ComponentError('lease must be 1..3600 seconds')
        now=self.clock()
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE queue SET state='unknown',lease_until=NULL,lease_token=NULL,error=COALESCE(error,'lease expired; side-effect outcome unknown') WHERE state='leased' AND lease_until<=?",(now,))
            r=c.execute("SELECT * FROM queue WHERE state='queued' AND available<=? ORDER BY created,id LIMIT 1",(now,)).fetchone()
            if not r:return None
            token=uuid.uuid4().hex
            cur=c.execute("UPDATE queue SET state='leased',attempts=attempts+1,lease_until=?,lease_token=? WHERE id=? AND state='queued'",(now+lease_seconds,token,r['id']))
            if cur.rowcount!=1:raise ComponentError('claim lost; retry claim')
            return {'id':r['id'],'payload':json.loads(r['payload']),'attempt':r['attempts']+1,'max_attempts':r['max_attempts'],'lease_until':now+lease_seconds,'lease_token':token}
    def complete(self,job_id:str,result:Any=None,*,lease_token:str|None=None)->dict:
        encoded=_json(result)
        if len(encoded.encode())>MAX_QUEUE_PAYLOAD:raise ComponentError('queue result exceeds 256 KiB')
        if not lease_token:raise ComponentError('active lease token required')
        with self._connect() as c:
            cur=c.execute("UPDATE queue SET state='done',result=?,lease_until=NULL,lease_token=NULL WHERE id=? AND state='leased' AND lease_token=? AND lease_until>?",(encoded,job_id,lease_token,self.clock()))
            if cur.rowcount!=1:raise ComponentError('unknown, expired, or stale lease; completion refused')
        return {'id':job_id,'state':'done'}
    def fail(self,job_id:str,error:str,*,lease_token:str|None=None,retry_delay=0,effect='unknown')->dict:
        if not isinstance(error,str) or len(error)>2048 or type(retry_delay) not in (int,float) or not 0<=retry_delay<=86400:raise ComponentError('invalid error or retry delay')
        if effect not in ('unknown','failed_no_effect','not_started'):raise ComponentError('effect must be unknown, failed_no_effect, or not_started')
        if not lease_token:raise ComponentError('active lease token required')
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT state,attempts,max_attempts FROM queue WHERE id=? AND lease_token=? AND lease_until>?',(job_id,lease_token,self.clock())).fetchone()
            if not r or r['state']!='leased':raise ComponentError('unknown, expired, or stale lease; failure refused')
            state=('dead' if r['attempts']>=r['max_attempts'] else 'queued') if effect!='unknown' else 'unknown'
            c.execute('UPDATE queue SET state=?,available=?,lease_until=NULL,lease_token=NULL,error=? WHERE id=? AND lease_token=?',(state,self.clock()+retry_delay,error,job_id,lease_token))
        return {'id':job_id,'state':state,'attempts':r['attempts'],'max_attempts':r['max_attempts']}
    def schedule(self,payload:Any,*,interval_seconds:float,first_run:float|None=None,schedule_id:str|None=None)->dict:
        if type(interval_seconds) not in (int,float) or not 1<=interval_seconds<=31536000:raise ComponentError('schedule interval must be 1 second..1 year')
        sid=schedule_id or uuid.uuid4().hex
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',sid):raise ComponentError('invalid schedule id')
        body=_json(payload)
        if len(body.encode())>MAX_QUEUE_PAYLOAD:raise ComponentError('scheduled payload exceeds 256 KiB')
        when=self.clock() if first_run is None else first_run
        if type(when) not in (int,float) or not __import__('math').isfinite(when):raise ComponentError('scheduled time must be finite')
        with self._connect() as c:
            if c.execute('SELECT COUNT(*) FROM schedules').fetchone()[0]>=MAX_STORED_ROWS:raise ComponentError('schedule capacity reached')
            c.execute('INSERT INTO schedules VALUES(?,?,?,?,1)',(sid,body,interval_seconds,when))
        return {'id':sid,'next_run':when,'interval_seconds':interval_seconds,'enabled':True}

    def search(self,query:str,*,limit=50)->list[dict]:
        if not isinstance(query,str) or not query or len(query)>256 or type(limit) is not int or not 1<=limit<=100:raise ComponentError('query required (max 256 chars), limit 1..100')
        with self._connect() as c:rows=c.execute('SELECT name,digest,size,content_type,created FROM objects ORDER BY name LIMIT ?',(MAX_STORED_ROWS,)).fetchall()
        return [dict(r) for r in rows if query.casefold() in r['name'].casefold()][:limit]
    def run_scheduled(self,*,limit=100)->list[dict]:
        if type(limit) is not int or not 1<=limit<=1000:raise ComponentError('limit must be 1..1000')
        now=self.clock(); out=[]
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run,id LIMIT ?', (now,limit)).fetchall()
            for r in rows:
                key=f'schedule:{r["id"]}:{int(r["next_run"]*1000000)}'; payload=_json({'schedule_id':r['id'],'scheduled_for':r['next_run'],'payload':json.loads(r['payload'])})
                old=c.execute('SELECT operation,outcome FROM idempotency WHERE key=?',(key,)).fetchone()
                if old:
                    if old['operation']!='queue':raise ComponentError('schedule idempotency key collision')
                    result=json.loads(old['outcome'])
                else:
                    if c.execute('SELECT COUNT(*) FROM queue').fetchone()[0]>=MAX_STORED_ROWS:raise ComponentError('queue capacity reached')
                    job_id=uuid.uuid4().hex;result={'id':job_id,'state':'queued','attempts':0,'max_attempts':3,'available_at':now}
                    c.execute('INSERT INTO queue(id,payload,state,attempts,max_attempts,available,created) VALUES(?,?,?,0,3,?,?)',(job_id,payload,'queued',now,now))
                    c.execute('INSERT INTO idempotency(key,operation,outcome,created,request_digest) VALUES(?,?,?,?,?)',(key,'queue',_json(result),now,_json([payload,3,now])))
                elapsed=max(0,now-r['next_run']); next_run=r['next_run']+(int(elapsed//r['interval'])+1)*r['interval']
                c.execute('UPDATE schedules SET next_run=? WHERE id=? AND next_run=?',(next_run,r['id'],r['next_run']))
                out.append({'schedule_id':r['id'],'job':result,'next_run':next_run})
        return out
    def cache_set(self,key:str,value:Any,*,ttl_seconds:float)->dict:
        if not isinstance(key,str) or not key or len(key)>512 or type(ttl_seconds) not in (int,float) or not 0<ttl_seconds<=86400:raise ComponentError('cache key required; TTL must be 0..86400 seconds')
        encoded=_json(value)
        if len(encoded.encode())>MAX_CACHE_VALUE:raise ComponentError('cache value exceeds 256 KiB')
        now=self.clock()
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM cache WHERE expires<=?',(now,))
            count=c.execute('SELECT COUNT(*) FROM cache WHERE key<>?',(key,)).fetchone()[0]
            if count>=MAX_CACHE_ITEMS:raise ComponentError('cache capacity reached')
            c.execute('INSERT INTO cache VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,expires=excluded.expires,touched=excluded.touched',(key,encoded,now+ttl_seconds,now))
        return {'key':key,'expires_at':now+ttl_seconds}
    def cache_get(self,key:str)->dict:
        now=self.clock()
        with self._connect() as c:
            r=c.execute('SELECT value,expires FROM cache WHERE key=?',(key,)).fetchone()
            if not r:return {'hit':False}
            if r['expires']<=now:c.execute('DELETE FROM cache WHERE key=?',(key,));return {'hit':False}
            c.execute('UPDATE cache SET touched=? WHERE key=?',(now,key));return {'hit':True,'value':json.loads(r['value']),'expires_at':r['expires']}
    def _authorized_request(self,url:str,method:str,body:bytes|None,*,headers:dict|None=None)->dict:
        if not self.network_authorized: raise ComponentError('network access requires network_authorized=True')
        u=urllib.parse.urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.fragment: raise ComponentError('only credential-free HTTPS URLs are allowed')
        host=u.hostname.lower();port=u.port or 443
        if f'{host}:{port}' not in self.authorized_hosts:raise ComponentError('exact destination host/port is not explicitly authorized')
        if body is not None and len(body)>MAX_HTTP_BODY:raise ComponentError('network request body exceeds 1 MiB')
        path=urllib.parse.urlunsplit(('', '',u.path or '/',u.query,''));h={'Accept':'application/json','User-Agent':'AutoForge-Components/1'}
        for k,v in (headers or {}).items():
            if not isinstance(k,str) or not isinstance(v,str) or k.lower() in ('host','content-length','connection','authorization','cookie','proxy-authorization') or '\r' in k+v or '\n' in k+v:raise ComponentError('unsafe HTTP header')
            h[k]=v
        if body is not None:h['Content-Type']='application/json'
        import ssl
        conn=http.client.HTTPSConnection(host,port,timeout=5,context=self.tls_context or ssl.create_default_context())
        try:
            conn.request(method,path,body=body,headers=h);res=conn.getresponse();data=res.read(MAX_HTTP_BODY+1)
            if len(data)>MAX_HTTP_BODY:raise ComponentError('network response exceeds 1 MiB')
            if not 200<=res.status<300:raise ComponentError(f'authorized endpoint returned HTTP {res.status}')
            return {'status':res.status,'headers':dict(res.getheaders()),'body':data.decode('utf-8','replace')}
        except OSError as e:raise ComponentError(f'authorized network request failed: {type(e).__name__}') from e
        finally:conn.close()
    def _request_digest(self,url,method,body,headers):
        return hashlib.sha256(_json([url,method,body.decode('utf-8') if body is not None else None,headers or {}]).encode()).hexdigest()
    def _reserve_effect(self,key,operation,digest):
        if not isinstance(key,str) or not key or len(key)>256:raise ComponentError('external side effect requires an idempotency key')
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT operation,outcome,request_digest FROM idempotency WHERE key=?',(key,)).fetchone()
            if old:
                if old['operation']!=operation:raise ComponentError('idempotency key already used for another operation')
                if old['request_digest']!=digest:raise ComponentError('idempotency key reused with different request')
                prior=json.loads(old['outcome'])
                if prior.get('state')!='sent':raise ComponentError('external outcome unknown; manual reconciliation required')
                return prior
            if c.execute('SELECT COUNT(*) FROM idempotency').fetchone()[0]>=MAX_STORED_ROWS:raise ComponentError('idempotency capacity reached')
            c.execute('INSERT INTO idempotency(key,operation,outcome,created,request_digest) VALUES(?,?,?,?,?)',(key,operation,_json({'state':'unknown','request_digest':digest}),self.clock(),digest))
        return None
    def _mark_effect_sent(self,key,operation,result):
        with self._connect() as c:c.execute('UPDATE idempotency SET outcome=? WHERE key=? AND operation=?',(_json(result),key,operation))

    def notify(self,url:str,payload:Any,*,idempotency_key:str,headers:dict|None=None)->dict:
        body=_json(payload).encode()
        if len(body)>MAX_HTTP_BODY:raise ComponentError('webhook payload exceeds 1 MiB')
        digest=self._request_digest(url,'POST',body,headers)
        reserved=self._reserve_effect(idempotency_key,'webhook',digest)
        if reserved:return reserved
        response=self._authorized_request(url,'POST',body,headers=headers)
        result={'state':'sent','http_status':response['status'],'request_digest':digest};self._mark_effect_sent(idempotency_key,'webhook',result);return result
    def integrate(self,url:str,*,method='GET',payload:Any=None,headers:dict|None=None,idempotency_key:str|None=None)->dict:
        if method not in ('GET','POST','PUT','DELETE'):raise ComponentError('unsupported integration method')
        body=None if method=='GET' else _json(payload).encode()
        if method=='GET':return self._authorized_request(url,method,body,headers=headers)
        if not body or len(body)>MAX_HTTP_BODY:raise ComponentError('integration body exceeds 1 MiB')
        digest=self._request_digest(url,method,body,headers);reserved=self._reserve_effect(idempotency_key,'integration',digest)
        if reserved:return reserved
        response=self._authorized_request(url,method,body,headers=headers)
        done={'state':'sent','http_status':response['status'],'body':response['body'],'request_digest':digest};self._mark_effect_sent(idempotency_key,'integration',done);return done
    def export_json(self,path:str|Path,*,overwrite=False)->dict:
        dest=Path(path).expanduser()
        if _is_reparse(dest):raise ComponentError('export destination may not be a symlink or reparse point')
        if dest.exists() and not overwrite:raise ComponentError('export destination already exists')
        dest=dest.resolve()
        if dest==self.db.resolve() or self.root in dest.parents or dest==self.root:raise ComponentError('export destination must be outside runtime root')
        with self._connect() as c:rows=[dict(r) for r in c.execute('SELECT id,value,created FROM records ORDER BY id')]
        data=_json({'schema':1,'records':rows}).encode()
        if len(data)>MAX_EXPORT:raise ComponentError('export exceeds 32 MiB')
        dest.parent.mkdir(parents=True,exist_ok=True);tmp=dest.with_name(dest.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with tmp.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            if overwrite:os.replace(tmp,dest)
            else:
                try:os.link(tmp,dest)
                except FileExistsError as e:raise ComponentError('export destination already exists') from e
        finally:tmp.unlink(missing_ok=True)
        return {'path':str(dest),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'records':len(rows)}
    def import_json(self,path:str|Path,*,max_bytes=MAX_EXPORT)->dict:
        raw=Path(path).expanduser()
        if _is_reparse(raw):raise ComponentError('import source may not be a symlink or reparse point')
        src=raw.resolve()
        if src==self.db.resolve() or self.root in src.parents or src==self.root:raise ComponentError('import source must be outside runtime root')
        size=src.stat().st_size
        if size>max_bytes or size>MAX_EXPORT:raise ComponentError('import exceeds size limit')
        data=src.read_bytes()
        if len(data)!=size:raise ComponentError('import changed while being read')
        try:doc=json.loads(data)
        except (UnicodeDecodeError,json.JSONDecodeError) as e:raise ComponentError('invalid import JSON') from e
        if not isinstance(doc,dict) or set(doc)!={'schema','records'} or doc['schema']!=1 or not isinstance(doc['records'],list) or len(doc['records'])>100000:raise ComponentError('unsupported import schema')
        prepared=[]
        for row in doc['records']:
            if not isinstance(row,dict) or set(row)!={'id','value','created'} or not isinstance(row['id'],str) or not row['id'] or len(row['id'])>256 or type(row['created']) not in (int,float):raise ComponentError('invalid import record')
            prepared.append((row['id'],_json(row['value']),row['created']))
        if len({x[0] for x in prepared})!=len(prepared):raise ComponentError('duplicate record IDs in import')
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT COUNT(*) FROM records').fetchone()[0]+len(prepared)>MAX_STORED_ROWS:raise ComponentError('record capacity reached')
            if any(c.execute('SELECT 1 FROM records WHERE id=?',(x[0],)).fetchone() for x in prepared):raise ComponentError('import would overwrite existing records')
            c.executemany('INSERT INTO records VALUES(?,?,?)',prepared)
        return {'imported':len(prepared),'sha256':hashlib.sha256(data).hexdigest()}
    def put_record(self,record_id:str,value:Any)->dict:
        if not isinstance(record_id,str) or not record_id or len(record_id)>256:raise ComponentError('invalid record id')
        encoded=_json(value)
        if len(encoded.encode())>MAX_CACHE_VALUE:raise ComponentError('record value exceeds 256 KiB')
        with self._connect() as c:
            if c.execute('SELECT COUNT(*) FROM records').fetchone()[0]>=MAX_STORED_ROWS:raise ComponentError('record capacity reached')
            c.execute('INSERT INTO records VALUES(?,?,?)',(record_id,encoded,self.clock()))
        return {'id':record_id,'stored':True}
    def status(self)->dict:
        with self._connect() as c:
            counts={table:c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('objects','queue','schedules','cache','records')}
        return {'root':str(self.root),'database':str(self.db),'counts':counts,'network_authorized':self.network_authorized,'authorized_hosts':sorted(self.authorized_hosts)}


def main(argv=None)->int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path('./autoforge-components'));p.add_argument('--allow-host',action='append',default=[]);p.add_argument('--authorize-network',action='store_true');sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    u=sub.add_parser('upload');u.add_argument('name');u.add_argument('file',type=Path);u.add_argument('--content-type',default='application/octet-stream');u.add_argument('--overwrite',action='store_true')
    d=sub.add_parser('download');d.add_argument('name');d.add_argument('file',type=Path)
    q=sub.add_parser('enqueue');q.add_argument('json');q.add_argument('--max-attempts',type=int,default=3);q.add_argument('--idempotency-key')
    sub.add_parser('claim');s=sub.add_parser('schedule');s.add_argument('json');s.add_argument('--interval',type=float,required=True);s.add_argument('--id')
    sub.add_parser('dispatch-scheduled');f=sub.add_parser('search');f.add_argument('query')
    a=sub.add_parser('cache-set');a.add_argument('key');a.add_argument('json');a.add_argument('--ttl',type=float,required=True)
    a=sub.add_parser('cache-get');a.add_argument('key')
    a=sub.add_parser('export');a.add_argument('file',type=Path);a.add_argument('--overwrite',action='store_true')
    a=sub.add_parser('import');a.add_argument('file',type=Path)
    a=sub.add_parser('webhook');a.add_argument('url');a.add_argument('json');a.add_argument('--idempotency-key',required=True)
    a=sub.add_parser('integrate');a.add_argument('url');a.add_argument('--method',default='GET',choices=('GET','POST','PUT','DELETE'));a.add_argument('--json');a.add_argument('--idempotency-key')
    q=sub.add_parser('queue-complete');q.add_argument('id');q.add_argument('lease_token');q.add_argument('--result',default='null')
    q=sub.add_parser('queue-fail');q.add_argument('id');q.add_argument('lease_token');q.add_argument('error');q.add_argument('--retry-delay',type=float,default=0)
    x=p.parse_args(argv)
    try:
        rt=ComponentRuntime(x.root,authorized_hosts=set(x.allow_host),network_authorized=x.authorize_network)
        if x.command=='status':out=rt.status()
        elif x.command=='upload':out=rt.upload(x.name,x.file.read_bytes(),x.content_type,overwrite=x.overwrite)
        elif x.command=='download':
            meta,data=rt.download(x.name);dest=x.file.resolve();dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data);out={**meta,'path':str(dest)}
        elif x.command=='enqueue':out=rt.enqueue(json.loads(x.json),max_attempts=x.max_attempts,idempotency_key=x.idempotency_key)
        elif x.command=='claim':out=rt.claim()
        elif x.command=='schedule':out=rt.schedule(json.loads(x.json),interval_seconds=x.interval,schedule_id=x.id)
        elif x.command=='dispatch-scheduled':out=rt.run_scheduled()
        elif x.command=='search':out=rt.search(x.query)
        elif x.command=='cache-set':out=rt.cache_set(x.key,json.loads(x.json),ttl_seconds=x.ttl)
        elif x.command=='cache-get':out=rt.cache_get(x.key)
        elif x.command=='export':out=rt.export_json(x.file,overwrite=x.overwrite)
        elif x.command=='import':out=rt.import_json(x.file)
        elif x.command=='webhook':out=rt.notify(x.url,json.loads(x.json),idempotency_key=x.idempotency_key)
        elif x.command=='integrate':out=rt.integrate(x.url,method=x.method,payload=json.loads(x.json) if x.json else None,idempotency_key=x.idempotency_key)
        elif x.command=='queue-complete':out=rt.complete(x.id,json.loads(x.result),lease_token=x.lease_token)
        else:out=rt.fail(x.id,x.error,lease_token=x.lease_token,retry_delay=x.retry_delay)
        print(_json({'status':'OK','result':out}));return 0
    except (ComponentError,OSError,ValueError) as e:
        print(_json({'status':'ERROR','error':str(e)}));return 2
if __name__=='__main__':raise SystemExit(main())
