#!/usr/bin/env python3
"""Standalone runtime smoke for AutoForge business components.
Run local checks with: python checks/application_components_check.py
The driver creates a temporary trusted local HTTPS receiver and exercises authorized
GET integration + idempotent webhook delivery; OpenSSL is required only for this fixture.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, json, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import ssl, os
from pathlib import Path
import application_components as c
class LocalReceiver(BaseHTTPRequestHandler):
    requests=[]
    def do_GET(self):
        type(self).requests.append(('GET',self.path,b''));self.send_response(200);self.end_headers();self.wfile.write(b'{"ok":true}')
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')));type(self).requests.append(('POST',self.path,body));self.send_response(200);self.end_headers();self.wfile.write(b'{"accepted":true}')
    def log_message(self,*args):pass
def local_https(base):
    key=base/'localhost.key';cert=base/'localhost.crt'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    context=ssl.create_default_context(cafile=str(cert));server=HTTPServer(('127.0.0.1',0),LocalReceiver);server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_context.load_cert_chain(str(cert),str(key));server.socket=server_context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();return server,context
def rejected(fn):
    try: fn()
    except c.ComponentError: return
    raise AssertionError('unsafe or invalid operation was accepted')
def main():
    p=argparse.ArgumentParser();a=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='autoforge-components-smoke-') as td:
        base=Path(td);root=base/'runtime'; now=[1000.0]
        rt=c.ComponentRuntime(root,clock=lambda:now[0])
        # Durable upload/object storage, deduplication, path safety, and indexed search.
        meta=rt.upload('assets/hello.txt',b'hello durable world','text/plain')
        assert rt.download('assets/hello.txt')==(meta,b'hello durable world')
        rejected(lambda:rt.upload('../escape',b'x'))
        rejected(lambda:rt.upload('assets/hello.txt',b'other'))
        job=rt.enqueue({'work':'once'},max_attempts=2,idempotency_key='queue-smoke')
        assert rt.enqueue({'work':'once'},max_attempts=2,idempotency_key='queue-smoke')['id']==job['id']
        rejected(lambda:rt.enqueue({'different':'payload'},max_attempts=2,idempotency_key='queue-smoke'))
        rt=c.ComponentRuntime(root,clock=lambda:now[0]);claim=rt.claim();assert claim['id']==job['id'] and claim['attempt']==1
        rejected(lambda:rt.complete(claim['id']))
        assert rt.fail(claim['id'],'transient',lease_token=claim['lease_token'],effect='not_started')['state']=='queued'
        claim=rt.claim();assert claim['attempt']==2
        assert rt.fail(claim['id'],'terminal',lease_token=claim['lease_token'],effect='failed_no_effect')['state']=='dead'
        assert rt.claim() is None
        rejected(lambda:rt.fail(claim['id'],'duplicate',lease_token=claim['lease_token']))
        exp=rt.enqueue({'lease':'expiry'},max_attempts=3);old=rt.claim(lease_seconds=2);assert old['id']==exp['id']
        now[0]+=3;assert rt.claim() is None
        with rt._connect() as db:assert db.execute('SELECT state FROM queue WHERE id=?',(exp['id'],)).fetchone()['state']=='unknown'
        stale=rt.enqueue({'lease':'stale'});old=rt.claim(lease_seconds=2);now[0]+=3
        assert rt.claim() is None;rejected(lambda:rt.complete(old['id'],lease_token=old['lease_token']))
        concurrent=[rt.enqueue({'parallel':i}) for i in range(8)];claimed=[];errors=[]
        def worker():
            try:
                while True:
                    item=rt.claim()
                    if item is None:break
                    claimed.append(item)
            except Exception as e:errors.append(e)
        threads=[threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads];[t.join() for t in threads]
        assert not errors and len(claimed)==len(concurrent) and len({x['id'] for x in claimed})==len(concurrent)
        # Persistent scheduled jobs enqueue one deterministic occurrence per due slot.
        rt.schedule({'task':'daily'},interval_seconds=10,schedule_id='smoke-schedule')
        runs=rt.run_scheduled();assert len(runs)==1 and len(rt.run_scheduled())==0
        now[0]+=35;runs=rt.run_scheduled();assert len(runs)==1
        # Bounded TTL cache expires deterministically.
        rt.cache_set('ephemeral',{'v':1},ttl_seconds=5);assert rt.cache_get('ephemeral')['hit']
        now[0]+=6;assert not rt.cache_get('ephemeral')['hit']
        # Import/export uses explicit schema, refuses collision and stays outside data root.
        rt.put_record('one',{'answer':42});export=base/'records.json';rt.export_json(export)
        other=c.ComponentRuntime(base/'other',clock=lambda:now[0]);assert other.import_json(export)['imported']==1
        rejected(lambda:other.import_json(export))
        # Network calls are never implicit and unauthorized/unconfigured destinations reject.
        rejected(lambda:rt.integrate('https://example.invalid/'))
        rejected(lambda:c.ComponentRuntime(base/'no-net',authorized_hosts={'example.invalid'}).integrate('https://example.invalid/'))
        server,context=local_https(base);url=f'https://127.0.0.1:{server.server_port}/receiver'
        try:
            net=c.ComponentRuntime(base/'authorized',authorized_hosts={f'127.0.0.1:{server.server_port}'},network_authorized=True,tls_context=context)
            assert net.integrate(url)['status']==200
            sent=net.notify(url,{'smoke':'autoforge'},idempotency_key='webhook-smoke');assert sent['state']=='sent'
            assert net.notify(url,{'smoke':'autoforge'},idempotency_key='webhook-smoke')==sent
            rejected(lambda:net.notify(url,{'different':1},idempotency_key='webhook-smoke'))
            assert [x[0] for x in LocalReceiver.requests]==['GET','POST']
        finally:server.shutdown();server.server_close()
        # Consumer-visible standalone CLI paths exercise real durable service operations.
        cli_root=base/'cli-runtime'; payload=base/'upload.bin';payload.write_bytes(b'cli upload')
        def cli(*args):
            r=subprocess.run([sys.executable,str(Path(c.__file__).resolve()),'--root',str(cli_root),*args],capture_output=True,text=True,check=False)
            if r.returncode:raise AssertionError(f'CLI {args[0]} failed: {r.stdout} {r.stderr}')
            return json.loads(r.stdout)['result']
        cli('upload','cli/item.bin',str(payload));cli('search','item')
        q=cli('enqueue','{"cli":true}','--max-attempts','1');claimed=cli('claim');assert claimed['id']==q['id']
        cli('queue-complete',q['id'],claimed['lease_token']);cli('schedule','{"scheduled":true}','--interval','60','--id','cli-schedule');cli('dispatch-scheduled')
        cli('cache-set','cli-key','{"ok":true}','--ttl','60');assert cli('cache-get','cli-key')['hit']
        cli('export',str(base/'cli-export.json'))
        assert cli('status')['counts']['objects']==1
        names=['durable upload/object storage','persistent queue and finite retries','scheduled jobs','application search','bounded TTL cache','authorized webhook notification','import/export','authorized third-party HTTP integration']
        print(json.dumps({'status':'PASS','components':names,'network':'local trusted HTTPS receiver verified','runtime':'standalone CLI exercised'},ensure_ascii=False))
if __name__=='__main__':main()
