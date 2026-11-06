#!/usr/bin/env python3
"""Authored disposable acceptance fixture, not model-generated application evidence."""
import html
import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, unquote

DB='/state/container-acceptance.sqlite'
SECRET=Path('/run/autoforge/secrets/FIXTURE_AUTH_TOKEN').read_text(encoding='utf-8')
OTHER=Path('/run/autoforge/secrets/OTHER_TENANT_TOKEN').read_text(encoding='utf-8')
READONLY=Path('/run/autoforge/secrets/READ_ONLY_TOKEN').read_text(encoding='utf-8')
TOKENS={SECRET:('alpha','admin'),OTHER:('beta','admin'),READONLY:('alpha','reader')}
SESSIONS={}

def db():
    connection=sqlite3.connect(DB)
    connection.execute('CREATE TABLE IF NOT EXISTS items (tenant TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, idem TEXT, PRIMARY KEY(tenant,id), UNIQUE(tenant,idem))')
    connection.execute('CREATE TABLE IF NOT EXISTS revoked (token TEXT PRIMARY KEY)')
    connection.commit()
    return connection

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,obj,headers=None):
        data=json.dumps(obj,separators=(',',':')).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        for key,value in (headers or {}).items(): self.send_header(key,value)
        self.end_headers();self.wfile.write(data)
    def body(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=65536: raise ValueError('invalid body length')
        value=json.loads(self.rfile.read(size))
        if not isinstance(value,dict): raise ValueError('body must be object')
        return value
    def bearer(self):
        token=self.headers.get('Authorization','').removeprefix('Bearer ')
        auth=TOKENS.get(token)
        if not auth: self.send(401,{'error':'unauthorized'});return None
        c=db()
        try: revoked=c.execute('SELECT 1 FROM revoked WHERE token=?',(token,)).fetchone()
        finally:c.close()
        if revoked:self.send(401,{'error':'revoked'});return None
        return (*auth,token)
    def session(self):
        cookie=self.headers.get('Cookie','')
        sid=next((part.strip()[4:] for part in cookie.split(';') if part.strip().startswith('sid=')),None)
        return SESSIONS.get(sid)
    def page(self):
        auth=self.session()
        if not auth:
            body='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Sign in</title></head><body><main><h1>Sign in</h1><p id="error" role="alert"></p><form id="login"><label for="token">Access token</label><input id="token" type="password" required autocomplete="off"><button>Sign in</button></form></main><script>document.querySelector('#login').onsubmit=async e=>{e.preventDefault();let r=await fetch('/ui/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.querySelector('#token').value})});if(r.ok)location.reload();else document.querySelector('#error').textContent='Sign-in failed'};</script></body></html>'''
        else:
            tenant,role,csrf=auth
            body='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Items</title><style>body{font:16px system-ui;margin:32px auto;padding:0 20px;max-width:760px;color:#17212b}input,button{font:inherit;padding:8px;margin:4px}li{padding:8px 0}li button{margin-left:12px}#error{color:#9b1c1c}button:focus-visible,input:focus-visible{outline:3px solid #1758b6;outline-offset:2px}</style></head><body><main><h1>Items</h1><p id="permission">__ROLE__</p><p id="status" role="status">Loading items</p><p id="error" role="alert"></p><form id="create"><label for="value">Item value</label><input id="value" name="value" required><input id="item-id" type="hidden"><button>Save item</button></form><ul id="items"></ul><button id="refresh" type="button">Refresh list</button><button id="logout" type="button">Sign out</button></main><script>
const csrf=__CSRF__,reader=__READER__,f=document.querySelector('#create'),list=document.querySelector('#items'),status=document.querySelector('#status'),error=document.querySelector('#error');
if(reader)f.remove();
async function refresh(){let r=await fetch('/ui/items');if(!r.ok){error.textContent='Unable to load items';return}let j=await r.json();list.replaceChildren(...j.items.map(x=>{let li=document.createElement('li');li.dataset.itemId=x.id;let text=document.createElement('span');text.textContent=x.value;li.append(text);if(!reader){let edit=document.createElement('button');edit.textContent='Edit';edit.onclick=()=>{f.elements['item-id'].value=x.id;f.elements.value.value=x.value;f.elements.value.focus()};let del=document.createElement('button');del.textContent='Delete';del.onclick=async()=>{let response=await fetch('/ui/items/'+encodeURIComponent(x.id),{method:'DELETE',headers:{'X-CSRF-Token':csrf}});if(response.ok)await refresh();else error.textContent='Unable to delete item'};li.append(edit,del)}return li}));status.textContent=j.items.length?'Items available':'No items yet'}
if(!reader)f.onsubmit=async e=>{e.preventDefault();let id=f.elements['item-id'].value,value=f.elements.value.value;let r=await fetch(id?'/ui/items/'+encodeURIComponent(id):'/ui/items',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(id?{value}:{id:crypto.randomUUID(),value})});if(r.ok){f.reset();error.textContent='';await refresh()}else{let j=await r.json();error.textContent=j.error==='forbidden'?'Permission denied':j.error==='csrf'?'Request verification failed':'Unable to save item'}};
document.querySelector('#refresh').onclick=refresh;document.querySelector('#logout').onclick=async()=>{await fetch('/ui/logout',{method:'POST',headers:{'X-CSRF-Token':csrf}});location.reload()};refresh();
</script></body></html>'''.replace('__ROLE__',html.escape(role)).replace('__CSRF__',json.dumps(csrf)).replace('__READER__',json.dumps(role=='reader'))
        data=body.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.path=='/health':return self.send(200,{'ok':True})
        if self.path=='/':return self.page()
        if self.path=='/ui/items':
            auth=self.session()
            if not auth:return self.send(401,{'error':'unauthorized'})
            tenant=auth[0]
        else:
            auth=self.bearer()
            if not auth:return
            tenant=auth[0]
        c=db()
        try:
            if self.path=='/ui/items':rows=c.execute('SELECT id,value FROM items WHERE tenant=? ORDER BY id',(tenant,)).fetchall();return self.send(200,{'items':[{'id':i,'value':v} for i,v in rows]})
            if self.path.startswith('/items/'):
                row=c.execute('SELECT value FROM items WHERE tenant=? AND id=?',(tenant,self.path.split('/')[2])).fetchone()
                return self.send(200,{'id':self.path.split('/')[2],'value':row[0]}) if row else self.send(404,{'error':'not_found'})
            if self.path.startswith('/items?'):
                query=parse_qs(urlsplit(self.path).query)
                try:offset=int(query.get('offset',['0'])[0]);limit=int(query.get('limit',['10'])[0])
                except ValueError:return self.send(400,{'error':'invalid_pagination'})
                if offset<0 or not 1<=limit<=20:return self.send(400,{'error':'invalid_pagination'})
                rows=c.execute('SELECT id,value FROM items WHERE tenant=? ORDER BY id LIMIT ? OFFSET ?',(tenant,limit,offset)).fetchall();return self.send(200,{'items':[{'id':i,'value':v} for i,v in rows]})
            return self.send(404,{'error':'not_found'})
        finally:c.close()
    def do_POST(self):
        if self.path=='/ui/login':
            try:token=self.body().get('token')
            except ValueError:return self.send(400,{'error':'invalid_request'})
            auth=TOKENS.get(token)
            if not auth:return self.send(401,{'error':'unauthorized'})
            sid=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24);SESSIONS[sid]=(*auth,csrf)
            return self.send(200,{'ok':True},{'Set-Cookie':f'sid={sid}; HttpOnly; SameSite=Strict; Path=/'})
        if self.path.startswith('/ui/'):
            return self.ui_mutate()
        auth=self.bearer()
        if not auth:return
        tenant,role,token=auth
        if role!='admin':return self.send(403,{'error':'forbidden'})
        try:data=self.body()
        except ValueError:return self.send(400,{'error':'invalid_request'})
        c=db()
        try:
            if data.get('op')=='revoke':c.execute('INSERT OR IGNORE INTO revoked(token) VALUES(?)',(token,));c.commit();return self.send(200,{'ok':True})
            if data.get('op')!='create' or not isinstance(data.get('id'),str) or not data['id'] or not isinstance(data.get('value'),str) or not data['value']:return self.send(400,{'error':'invalid_item'})
            idem=self.headers.get('Idempotency-Key');existing=c.execute('SELECT id,value FROM items WHERE tenant=? AND idem=?',(tenant,idem)).fetchone() if idem else None
            if existing:return self.send(200,{'id':existing[0],'value':existing[1]})
            c.execute('INSERT INTO items(tenant,id,value,idem) VALUES(?,?,?,?)',(tenant,data['id'],data['value'],idem));c.commit();return self.send(201,{'id':data['id'],'value':data['value']})
        except sqlite3.IntegrityError:return self.send(409,{'error':'conflict'})
        finally:c.close()
    def ui_mutate(self):
        auth=self.session()
        if not auth:return self.send(401,{'error':'unauthorized'})
        tenant,role,csrf=auth
        if role!='admin':return self.send(403,{'error':'forbidden'})
        if self.headers.get('X-CSRF-Token')!=csrf:return self.send(403,{'error':'csrf'})
        if self.path=='/ui/logout':
            for sid,value in list(SESSIONS.items()):
                if value==auth:del SESSIONS[sid]
            return self.send(200,{'ok':True},{'Set-Cookie':'sid=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/'})
        try:data={} if self.command=='DELETE' else self.body()
        except ValueError:return self.send(400,{'error':'invalid_request'})
        if self.command!='DELETE' and (not isinstance(data.get('value'),str) or not data['value']):return self.send(400,{'error':'invalid_item'})
        c=db()
        try:
            if self.command=='POST' and self.path=='/ui/items':
                if not isinstance(data.get('id'),str) or not data['id']:return self.send(400,{'error':'invalid_item'})
                c.execute('INSERT INTO items(tenant,id,value) VALUES(?,?,?)',(tenant,data['id'],data['value']));c.commit();return self.send(201,{'id':data['id'],'value':data['value']})
            item_id=unquote(self.path.rsplit('/',1)[1])
            if self.command=='DELETE':changed=c.execute('DELETE FROM items WHERE tenant=? AND id=?',(tenant,item_id)).rowcount;c.commit();return self.send(200,{'deleted':True}) if changed else self.send(404,{'error':'not_found'})
            changed=c.execute('UPDATE items SET value=? WHERE tenant=? AND id=?',(data['value'],tenant,item_id)).rowcount;c.commit();return self.send(200,{'id':item_id,'value':data['value']}) if changed else self.send(404,{'error':'not_found'})
        except sqlite3.IntegrityError:return self.send(409,{'error':'conflict'})
        finally:c.close()
    def mutate(self,delete=False):
        if self.path.startswith('/ui/items/'):return self.ui_mutate()
        auth=self.bearer()
        if not auth:return
        tenant,role,_=auth
        if role!='admin':return self.send(403,{'error':'forbidden'})
        parts=self.path.split('/')
        if len(parts)!=3 or parts[1]!='items' or not parts[2]:return self.send(404,{'error':'not_found'})
        c=db();item_id=parts[2]
        try:
            if delete:changed=c.execute('DELETE FROM items WHERE tenant=? AND id=?',(tenant,item_id)).rowcount;c.commit();return self.send(200,{'deleted':item_id}) if changed else self.send(404,{'error':'not_found'})
            try:data=self.body()
            except ValueError:return self.send(400,{'error':'invalid_request'})
            if set(data)!={'value'} or not isinstance(data['value'],str) or not data['value']:return self.send(400,{'error':'invalid_item'})
            changed=c.execute('UPDATE items SET value=? WHERE tenant=? AND id=?',(data['value'],tenant,item_id)).rowcount;c.commit();return self.send(200,{'id':item_id,'value':data['value']}) if changed else self.send(404,{'error':'not_found'})
        finally:c.close()
    def do_PUT(self):return self.mutate()
    def do_DELETE(self):return self.mutate(True)

if __name__=='__main__':ThreadingHTTPServer(('0.0.0.0',8000),Handler).serve_forever()
