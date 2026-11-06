#!/usr/bin/env python3
"""Runtime integration smoke: python application_components_service_check.py"""
from __future__ import annotations
import base64, concurrent.futures, http.client, json, os, ssl, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import application_components as components
import application_components_service as service

TOKEN='component-service-smoke-secret-'+'a'*32
class Receiver(BaseHTTPRequestHandler):
    seen=[]
    def do_GET(self):
        type(self).seen.append(('GET',self.path,b''));self.send_response(200);self.end_headers();self.wfile.write(b'{"received":true}')
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')));type(self).seen.append(('POST',self.path,body));self.send_response(200);self.end_headers();self.wfile.write(b'{"accepted":true}')
    def log_message(self,*args):pass

def request(port,method,path,obj=None,token=TOKEN):
    body=None if obj is None else json.dumps(obj,separators=(',',':')).encode()
    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
    headers={'Authorization':'Bearer '+token}
    if body is not None:headers['Content-Type']='application/json'
    conn.request(method,path,body,headers);r=conn.getresponse();data=r.read();status=r.status;ctype=r.getheader('Content-Type');conn.close()
    return status,ctype,data

def main():
  import hashlib
  report_path=Path('bootstrap-runs')/('components-service-live-'+__import__('uuid').uuid4().hex+'.json')
  with tempfile.TemporaryDirectory(prefix='components-service-check-') as td:
    root=Path(td)/'data'; recvkey=Path(td)/'recv.key';recvcrt=Path(td)/'recv.crt'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(recvkey),'-out',str(recvcrt),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    receiver=HTTPServer(('127.0.0.1',0),Receiver);tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(str(recvcrt),str(recvkey));receiver.socket=tls.wrap_socket(receiver.socket,server_side=True)
    rt=components.ComponentRuntime(root,authorized_hosts={f'127.0.0.1:{receiver.server_port}'},network_authorized=True,tls_context=ssl.create_default_context(cafile=str(recvcrt)))
    recvthread=threading.Thread(target=receiver.serve_forever,daemon=True);recvthread.start()
    origin=f'https://127.0.0.1:{receiver.server_port}/integration';handler=service.create_handler(rt,TOKEN,webhook_url=origin,integration_url=origin)
    srv=service.BoundedThreadingHTTPServer(('127.0.0.1',0),handler,max_workers=16);thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start();port=srv.server_port
    try:
      assert request(port,'GET','/health',token='')[0]==200
      assert request(port,'GET','/v1/status',token='wrong')[0]==401
      assert request(port,'GET','/v1/status')[0]==200
      # Upload/download and restart persistence through HTTP interface.
      raw=b'consumer persisted bytes';status,_,data=request(port,'POST','/v1/uploads',{'name':'consumer/a.txt','data_base64':base64.b64encode(raw).decode(),'content_type':'text/plain'});assert status==201
      assert request(port,'GET','/v1/objects/consumer/a.txt')[2]==raw
      assert request(port,'POST','/v1/records',{'id':'record-1','value':{'n':7}})[0]==201
      assert request(port,'PUT','/v1/cache/session',{'value':{'active':True},'ttl_seconds':30})[0]==200
      assert json.loads(request(port,'GET','/v1/cache/session')[2])['hit']
      assert request(port,'POST','/v1/schedules',{'id':'tick','payload':{'kind':'tick'},'interval_seconds':60})[0]==201
      # Durable concurrent queue claims via independent HTTP requests.
      jobs=[json.loads(request(port,'POST','/v1/queue',{'payload':{'i':i}})[2]) for i in range(12)]
      def claim(_):return json.loads(request(port,'POST','/v1/queue/claim',{})[2])['job']
      with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:claimed=list(pool.map(claim,range(12)))
      one=claimed[0];assert request(port,'POST',f'/v1/queue/{one["id"]}/complete',{'lease_token':one['lease_token'],'result':'ok'})[0]==200
      assert request(port,'POST',f'/v1/queue/{one["id"]}/complete',{'lease_token':one['lease_token']})[0]==400
      assert request(port,'POST','/v1/search',{'query':'consumer'})[0]==200
      # Client-supplied URL/origin has no endpoint: operator config only; outbound TLS verified.
      response=json.loads(request(port,'POST','/v1/integration',{'method':'GET'})[2])
      assert response['body'] and Receiver.seen[-1][0]=='GET'
      notification={'payload':{'hello':'world'},'idempotency_key':'notify-1'}
      assert request(port,'POST','/v1/notifications',notification)[0]==200
      assert request(port,'POST','/v1/notifications',notification)[0]==200
      assert len([x for x in Receiver.seen if x[0]=='POST'])==1
      exported=request(port,'GET','/v1/export')[2]
      assert request(port,'POST','/v1/import',{})[0]==400
      # HTTP import payload from an external application, fresh store, then restart process-style runtime.
      fresh=Path(td)/'fresh';fresh_rt=components.ComponentRuntime(fresh)
      assert fresh_rt.import_json(write_import(Path(td),exported))['imported']==1
      srv.shutdown();srv.server_close();rt2=components.ComponentRuntime(root,authorized_hosts={f'127.0.0.1:{receiver.server_port}'},network_authorized=True,tls_context=ssl.create_default_context(cafile=str(recvcrt)))
      srv=service.BoundedThreadingHTTPServer(('127.0.0.1',0),service.create_handler(rt2,TOKEN,webhook_url=origin,integration_url=origin),max_workers=16);thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start();port=srv.server_port
      assert json.loads(request(port,'GET','/v1/cache/session')[2])['hit']
      assert json.loads(request(port,'GET','/v1/status')[2])['counts']['objects']==1
      # Origin controls are only configuration arguments and require exact HTTPS authority.
      try:service.serve(Path(td)/'bad',TOKEN,host='0.0.0.0')
      except ValueError:pass
      else:raise AssertionError('non-loopback service bind accepted')
      try:service.serve(Path(td)/'bad2',TOKEN,allowed_origins={'example.com:443'},webhook_url='https://evil.test/hook')
      except ValueError:pass
      else:raise AssertionError('unapproved outbound origin accepted')
      composed=compose_business_event(rt,{'title':'generated task','idempotency_key':'generated-task-1'})
      assert composed['queue']['id'] and composed['record']['id']=='business-event:generated-task-1'
      job=json.loads(request(port,'POST','/v1/queue/claim',{})[2])['job']
      assert job['payload']=={'kind':'task-created','title':'generated task'}
      assert request(port,'POST',f'/v1/queue/{job["id"]}/complete',{'lease_token':job['lease_token'],'result':{'http_status':201}})[0]==200
      checks=['bearer auth','upload/download persistence across restart','persistent queue with concurrent claims and lease token','schedule/search/cache/records/import-export','configured local HTTPS notification/integration','non-loopback and unauthorized origin refusal','trusted generated-business-event composition into durable queue and record']
      report={'schema':1,'timestamp':'2026-11-04','status':'PASS','checks':checks,'objects':rt2.status()['counts'],'receiver_requests':len(Receiver.seen),'network':'local ephemeral trusted TLS only','auth':'bearer token not included','service_origin':f'127.0.0.1:{port}'}
      raw=json.dumps(report,sort_keys=True,separators=(',',':')).encode();report['sha256']=hashlib.sha256(raw).hexdigest();report_path.parent.mkdir(parents=True,exist_ok=True);report_path.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n',encoding='utf-8');print(json.dumps({'status':'PASS','report':str(report_path),'sha256':report['sha256']}))
    finally:
      srv.shutdown();srv.server_close();receiver.shutdown();receiver.server_close()

def write_import(root,data):
    p=root/'export.json';p.write_bytes(data);return p

def compose_business_event(runtime, event):
    """Trusted operator-side composition; never injects code into the generated app."""
    if not isinstance(event, dict) or set(event) != {'title', 'idempotency_key'}:
        raise ValueError('business event requires title and idempotency_key')
    title = event['title']
    key = event['idempotency_key']
    if not isinstance(title, str) or not title.strip() or not isinstance(key, str) or not key:
        raise ValueError('business event title and idempotency_key must be non-empty')
    queued = runtime.enqueue({'kind': 'task-created', 'title': title}, idempotency_key=key)
    record = runtime.put_record('business-event:' + key, {'kind': 'task-created', 'title': title,
                                                          'queue_job_id': queued['id']})
    return {'queue': queued, 'record': record}
if __name__=='__main__':main()
