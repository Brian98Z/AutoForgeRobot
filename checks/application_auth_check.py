"""Auth primitives and authored loopback HTTP integration; no generated app claim."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import tempfile
from pathlib import Path
import application_auth as auth
import json, threading, http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def http_check(store,now):
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_GET(self):
   token=self.headers.get('Authorization','').removeprefix('Bearer ')
   try:principal=store.authenticate(token)
   except auth.AuthRefused:status=401
   else:
    try:auth.authorize(principal,role='admin' if self.path.endswith('/admin') else 'reader',tenant_id=self.path.split('/')[1])
    except auth.AuthRefused:status=403
    else:status=200
   self.send_response(status);self.send_header('Content-Length','0');self.end_headers()
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
 worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
 def request(path,token=''):
  c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
  try:
   c.request('GET',path,headers={'Authorization':'Bearer '+token} if token else {})
   r=c.getresponse();r.read();return r.status
  finally:c.close()
 try:
  token,_=store.create('http-user','tenant-A',['reader'],ttl_seconds=10)
  assert request('/tenant-A/read',token)==200
  assert request('/tenant-A/read')==401
  assert request('/tenant-A/admin',token)==403
  assert request('/tenant-B/read',token)==403
  store.revoke(token);assert request('/tenant-A/read',token)==401
  token,_=store.create('http-expiring','tenant-A',['reader'],ttl_seconds=1)
  now[0]+=1;assert request('/tenant-A/read',token)==401
 finally:server.shutdown();server.server_close();worker.join(timeout=3)

def main():
 with tempfile.TemporaryDirectory(prefix='auth-check-') as td:
  record=auth.hash_password('correct horse battery staple')
  assert auth.verify_password('correct horse battery staple',record) and not auth.verify_password('wrong password',record)
  now=[1000]; store=auth.SessionStore(Path(td)/'sessions.json',clock=lambda:now[0])
  token,principal=store.create('u1','tenant-A',['reader'],ttl_seconds=10)
  raw=(Path(td)/'sessions.json').read_text(encoding='utf-8')
  assert token not in raw and store.authenticate(token)==principal
  auth.authorize(principal,role='reader',tenant_id='tenant-A',resource_tenant_id='tenant-A')
  for action in (lambda:auth.authorize(None,role='reader',tenant_id='tenant-A'),lambda:auth.authorize(principal,role='admin',tenant_id='tenant-A'),lambda:auth.authorize(principal,role='reader',tenant_id='tenant-B',resource_tenant_id='tenant-B')):
   try:action()
   except auth.AuthRefused:pass
   else:raise AssertionError('unauthorized access passed')
  store.revoke(token)
  try:store.authenticate(token)
  except auth.AuthRefused:pass
  else:raise AssertionError('revoked session accepted')
  token2,_=store.create('u2','tenant-A',['reader'],ttl_seconds=2); now[0]+=3
  try:store.authenticate(token2)
  except auth.AuthRefused:pass
  else:raise AssertionError('expired session accepted')
  assert auth.redact_auth_fields({'Authorization':'Bearer secret','nested':{'password':'x'}})=={'Authorization':'[REDACTED]','nested':{'password':'[REDACTED]'}}
  http_check(store,now)
  print(json.dumps({'status':'PASS','checks':['password hashing','opaque hashed session storage','expiry','revocation','role enforcement','tenant isolation','redaction'],'http_integration':{'status':'PASS','cases':{'authorized':200,'unauthenticated':401,'role_denied':403,'cross_tenant':403,'revoked':401,'expired_at_boundary':401},'scope':'authored loopback handler using real SessionStore; not generated application or container secret injection'}}))
  import os, application_secrets as secrets_api
  with tempfile.TemporaryDirectory(prefix='secret-check-') as td:
   root=Path(td);manifest=secrets_api.prepare_runtime_secrets({'API_TOKEN':'operator-secret'},private_root=root,session_id='session-a',allowed_names={'API_TOKEN'})
   mount=Path(manifest['mounts'][0]['source'])
   assert manifest['values_exposed'] is False
   if os.name!='nt':assert mount.stat().st_mode & 0o777 == 0o600 and mount.parent.stat().st_mode & 0o777 == 0o700
   config=secrets_api.resolve_secret_files({'token':{'$secret_file':manifest['mounts'][0]['target']},'port':8080},mounted_root=mount.parent)
   assert config=={'token':'operator-secret','port':8080} and 'operator-secret' not in json.dumps(manifest)
   try:secrets_api.resolve_secret_files({'token':{'$secret_file':'/etc/passwd'}},mounted_root=mount.parent)
   except secrets_api.SecretInjectionRefused:pass
   else:raise AssertionError('cross-mount secret reference accepted')
   cleaned=secrets_api.cleanup_runtime_secrets(manifest,audit_path=root/'audit.jsonl')
   assert cleaned['deleted']==1 and not mount.exists() and 'operator-secret' not in (root/'audit.jsonl').read_text()
   try:secrets_api.prepare_runtime_secrets({'lowercase':'x'},private_root=root,session_id='bad-name')
   except secrets_api.SecretInjectionRefused:pass
   else:raise AssertionError('unrestricted secret name accepted')
   try:secrets_api.prepare_runtime_secrets({'FIXTURE_AUTH_TOKEN':'x'},private_root=root,session_id='session-a',allowed_names={'OTHER'})
   except secrets_api.SecretInjectionRefused:pass
   else:raise AssertionError('non-allowlisted secret accepted')
if __name__=='__main__':main()
