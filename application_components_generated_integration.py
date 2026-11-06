#!/usr/bin/env python3
"""Trusted, operator-side bridge acceptance for generated task events and components.

Candidate source is only run by application_service's exact-image isolated container.
The bridge derives a bounded event from a successful observed HTTP response; candidate
code never receives a new network endpoint or component-service credential.
"""
from __future__ import annotations
import hashlib, json, secrets, tempfile, ssl, subprocess, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import application_components as components
import application_service

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'bootstrap-runs/generated-web-extended-ce8932caad84/repair-source/baseline-v2'
MANIFEST = json.loads((SOURCE / 'manifest.json').read_text(encoding='utf-8'))
IMAGE = 'sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60'
ENGINE = 'docker'
class TLSReceiver(BaseHTTPRequestHandler):
    requests=[]
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.requests.append(raw);self.send_response(200);self.end_headers();self.wfile.write(b'{"accepted":true}')
    def log_message(self,*args): pass

def bridge_event(runtime, *, method, path, status, body, idempotency_key):
    """Map only an observed successful task-create response to a durable event."""
    if method != 'POST' or path != '/api/tasks' or status != 201:
        raise ValueError('event is not an accepted task-created response')
    if not isinstance(body, dict) or set(body) != {'id', 'title'} or type(body['id']) is not int:
        raise ValueError('task response shape is invalid')
    title = body['title']
    if not isinstance(title, str) or not title.strip(): raise ValueError('task title is invalid')
    payload = {'kind': 'task-created', 'source': 'generated-web-http', 'task_id': body['id'], 'title': title}
    queued = runtime.enqueue(payload, idempotency_key=idempotency_key)
    record_id = 'generated-task:' + idempotency_key
    if runtime.status()['counts']['records'] == 0:
        record = runtime.put_record(record_id, {'event': payload, 'queue_job_id': queued['id']})
    else: record = {'id': record_id, 'stored': False, 'replayed': True}
    return {'job': queued, 'record': record}

def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--authorize-image',action='store_true',help='explicitly authorize the recorded local exact image for this isolated run');a=p.parse_args()
    if not a.authorize_image: raise SystemExit('refusing container dispatch without --authorize-image')
    if not SOURCE.is_dir(): raise SystemExit('retained generated source missing')
    for row in MANIFEST['files']:
        raw=(SOURCE/row['path']).read_bytes()
        if len(raw)!=row['size'] or hashlib.sha256(raw).hexdigest()!=row['sha256']: raise SystemExit('retained generated candidate differs from its manifest')
    admin='admin-'+secrets.token_urlsafe(32);reader='reader-'+secrets.token_urlsafe(32)
    contract={'schema':1,'health_path':'/health','scenarios':[{'name':'task-api-to-components-event','requests':[
      {'method':'POST','path':'/api/tasks','headers':{'Authorization':'Bearer {$secret_file:ADMIN}'},'body':{'title':'event from generated task'},'expected':{'status':201,'body':{'id':1,'title':'event from generated task'}}},
      {'method':'GET','path':'/api/tasks','headers':{'Authorization':'Bearer {$secret_file:ADMIN}'},'body':None,'expected':{'status':200,'body':{'items':[{'id':1,'title':'event from generated task'}]}}},
      {'method':'POST','path':'/api/tasks','headers':{'Authorization':'Bearer {$secret_file:READER}'},'body':{'title':'reader denied'},'expected':{'status':403,'body':{'error':'forbidden'}}},
      {'method':'POST','path':'/api/tasks','headers':{},'body':{'title':'unauthenticated denied'},'expected':{'status':401,'body':{'error':'unauthorized'}}}] }]}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    if a.output.exists(): raise SystemExit('output path already exists; refusing overwrite')
    with tempfile.TemporaryDirectory(prefix='af-generated-components-') as td:
      admin_file=Path(td)/'ADMIN';reader_file=Path(td)/'READER';admin_file.write_text(admin,encoding='utf-8');reader_file.write_text(reader,encoding='utf-8')
      import os
      os.chmod(admin_file,0o600);os.chmod(reader_file,0o600)
      result=application_service.run(MANIFEST,contract,ENGINE,IMAGE,['python','-I','-c','import runpy,sys;sys.path.insert(0,"/candidate");runpy.run_path("/candidate/main.py",run_name="__main__")'],timeout=45,
        secret_mounts=[{'source':str(admin_file),'target':'/run/autoforge/secrets/ADMIN','read_only':True,'name':'ADMIN','sha256':hashlib.sha256(admin_file.read_bytes()).hexdigest()},{'source':str(reader_file),'target':'/run/autoforge/secrets/READER','read_only':True,'name':'READER','sha256':hashlib.sha256(reader_file.read_bytes()).hexdigest()}])
      if not result.get('build_pass') or not result.get('test_pass'):
        report={'schema':1,'status':'HOLD','scope':'generated HTTP candidate ran in exact-image network-none container; trusted bridge dispatch withheld after HTTP contract failure','service_result':result}
      else:
        rows=result['http_results'][0]['requests'];response=rows[0]['actual']['body'];state_root=Path(td)/'components';clock=lambda:2000000000.0
        rt=components.ComponentRuntime(state_root,clock=clock,authorized_hosts=set(),network_authorized=False)
        bridged=bridge_event(rt,method='POST',path='/api/tasks',status=rows[0]['actual']['status'],body=response,idempotency_key='generated-http-task-1')
        duplicate=bridge_event(rt,method='POST',path='/api/tasks',status=rows[0]['actual']['status'],body=response,idempotency_key='generated-http-task-1')
        claimed=rt.claim();assert claimed and claimed['payload']['task_id']==response['id']
        try:rt.complete(claimed['id'],{'accepted':True},lease_token='wrong')
        except components.ComponentError: bad_lease_refused=True
        else: raise AssertionError('stale lease accepted')
        done=rt.complete(claimed['id'],{'accepted':True},lease_token=claimed['lease_token'])
        rt.cache_set('generated-task:1',response,ttl_seconds=60);assert rt.cache_get('generated-task:1')['hit']
        rt.upload('generated/tasks/1.json',json.dumps(response).encode(),'application/json');assert rt.search('generated/tasks')
        rt.schedule({'task_id':1},interval_seconds=60,first_run=clock(),schedule_id='generated-task-schedule');scheduled=rt.run_scheduled();assert scheduled
        export=Path(td)/'export.json';rt.export_json(export);fresh=components.ComponentRuntime(Path(td)/'imported');fresh.import_json(export)
        key=Path(td)/'receiver.key';crt=Path(td)/'receiver.crt'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(crt),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        receiver=HTTPServer(('127.0.0.1',0),TLSReceiver);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(str(crt),str(key));receiver.socket=ctx.wrap_socket(receiver.socket,server_side=True);thread=threading.Thread(target=receiver.serve_forever,daemon=True);thread.start()
        try:
          origin=f'https://127.0.0.1:{receiver.server_port}/events';net=components.ComponentRuntime(state_root,clock=clock,authorized_hosts={f'127.0.0.1:{receiver.server_port}'},network_authorized=True,tls_context=ssl.create_default_context(cafile=str(crt)))
          notice=net.notify(origin,{'event':'task-created','task_id':response['id']},idempotency_key='generated-event-notice-1')
          integrated=net.integrate(origin,method='POST',payload={'operation':'index-task','task_id':response['id']},idempotency_key='generated-event-integration-1')
          assert notice['state']=='sent' and integrated['state']=='sent' and len(TLSReceiver.requests)==2
          report={'schema':1,'status':'PASS','scope':'trusted controller composes observed generated task response into durable component capabilities; candidate itself receives no component endpoint or capability','candidate_sha256':MANIFEST['candidate_sha256'],'image_id':IMAGE,'http_contract':result,'bridge':{'response':response,'queue_job_id':bridged['job']['id'],'replay_job_id':duplicate['job']['id'],'record_id':bridged['record']['id'],'claimed_payload':claimed['payload'],'invalid_lease_refused':bad_lease_refused,'completion':done,'cache_hit':True,'object_search_hit':True,'scheduled_dispatch_count':len(scheduled),'export_import_records':fresh.status()['counts']['records'],'authorized_tls_notification':notice,'authorized_tls_integration':integrated,'tls_receiver_requests':len(TLSReceiver.requests),'after_runtime_restart':components.ComponentRuntime(state_root,clock=clock).status()['counts']},'authority':{'candidate_network':'none','source_mount':'read-only','secrets':'ephemeral private mounts','TLS':'trusted controller only; exact loopback host:port allowlisted with verified ephemeral certificate','candidate_endpoint_authority_expanded':False}}
        finally: receiver.shutdown();receiver.server_close();thread.join(timeout=2)
    raw=json.dumps(report,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode();report['sha256']=hashlib.sha256(raw).hexdigest();a.output.write_text(json.dumps(report,sort_keys=True,indent=2,ensure_ascii=False)+'\n',encoding='utf-8');print(json.dumps({'status':report['status'],'report':str(a.output),'sha256':report['sha256']}));return 0 if report['status']=='PASS' else 2
if __name__=='__main__': raise SystemExit(main())
