#!/usr/bin/env python3
"""Authenticated loopback HTTP interface to AutoForge's durable local components.

Run with python autoforge/application_components_service.py --root ./component-data --token-file ./service.token.
Only binds loopback; outbound HTTPS origins are operator-configured, never request-selected.
"""
from __future__ import annotations
import argparse, base64, hmac, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import application_components as components

MAX_JSON=components.MAX_QUEUE_PAYLOAD
MAX_UPLOAD=components.MAX_OBJECT

class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads=True
    request_queue_size=16
    def __init__(self,*args,max_workers=16,**kwargs):
        self._slots=threading.BoundedSemaphore(max_workers);super().__init__(*args,**kwargs)
    def process_request(self,request,client_address):
        if not self._slots.acquire(blocking=False):
            request.close();return
        try:super().process_request(request,client_address)
        except BaseException:self._slots.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self._slots.release()


def _json_bytes(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def create_handler(runtime,token,*,webhook_url=None,integration_url=None):
    if not isinstance(token,str) or len(token)<32 or len(token)>4096:raise ValueError('service token must contain 32..4096 characters')
    class Handler(BaseHTTPRequestHandler):
        timeout=5
        server_version='AutoForgeComponents/1'
        def setup(self):
            super().setup();self.connection.settimeout(5)
        def log_message(self,*args):pass
        def _send(self,status,body,content_type='application/json'):
            self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def _error(self,status,message):self._send(status,_json_bytes({'error':message}))
        def _auth(self):
            auth=self.headers.get('Authorization','')
            supplied=auth[7:] if auth.startswith('Bearer ') else ''
            try:return len(supplied)==len(token) and hmac.compare_digest(supplied.encode('utf-8'),token.encode('utf-8'))
            except (UnicodeEncodeError,TypeError):return False
        def _body(self,maximum=MAX_JSON):
            raw=self.headers.get('Content-Length')
            if raw is None or not raw.isdigit():raise ValueError('Content-Length required')
            n=int(raw)
            if n<0 or n>maximum:raise OverflowError('request body exceeds limit')
            data=self.rfile.read(n)
            if len(data)!=n:raise ValueError('incomplete request body')
            return data
        def _object(self,maximum=MAX_JSON):
            raw=self._body(maximum)
            try:obj=json.loads(raw)
            except (UnicodeDecodeError,json.JSONDecodeError) as e:raise ValueError('invalid JSON') from e
            if not isinstance(obj,dict):raise ValueError('JSON body must be an object')
            return obj
        def _route(self):
            path=urlsplit(self.path)
            if path.scheme or path.netloc or path.query or path.fragment:raise ValueError('invalid request target')
            route=path.path.split('/')
            if self.command=='GET' and path.path=='/health':return self._send(200,_json_bytes({'status':'ok'}))
            if not self._auth():return self._error(401,'bearer token required')
            if self.command=='GET' and path.path=='/v1/status':return self._send(200,_json_bytes(runtime.status()))
            if self.command=='POST' and path.path=='/v1/uploads':
                obj=self._object((MAX_UPLOAD*4+2)//3);name=obj.get('name');data=obj.get('data_base64');ctype=obj.get('content_type','application/octet-stream')
                if not isinstance(data,str):raise ValueError('data_base64 must be base64 text')
                try:blob=base64.b64decode(data,validate=True)
                except Exception as e:raise ValueError('invalid base64 upload') from e
                if len(blob)>MAX_UPLOAD:raise OverflowError('decoded upload exceeds limit')
                return self._send(201,_json_bytes(runtime.upload(name,blob,ctype,overwrite=obj.get('overwrite') is True)))
            if self.command=='GET' and len(route)>=4 and route[:3]==['','v1','objects']:
                name='/'.join(route[3:]);meta,data=runtime.download(name);return self._send(200,data,meta['content_type'])
            if self.command=='POST' and path.path=='/v1/queue':
                o=self._object();result=runtime.enqueue(o.get('payload'),max_attempts=o.get('max_attempts',3),delay=o.get('delay',0),idempotency_key=o.get('idempotency_key'));return self._send(201,_json_bytes(result))
            if self.command=='POST' and path.path=='/v1/queue/claim':
                o=self._object();return self._send(200,_json_bytes({'job':runtime.claim(lease_seconds=o.get('lease_seconds',60))}))
            if self.command=='POST' and len(route)==5 and route[:3]==['','v1','queue'] and route[4] in ('complete','fail'):
                o=self._object();job_id=route[3];token=o.get('lease_token')
                result=runtime.complete(job_id,o.get('result'),lease_token=token) if route[4]=='complete' else runtime.fail(job_id,o.get('error','failed'),lease_token=token,retry_delay=o.get('retry_delay',0),effect=o.get('effect','unknown'))
                return self._send(200,_json_bytes(result))
            if self.command=='POST' and path.path=='/v1/schedules':
                o=self._object();return self._send(201,_json_bytes(runtime.schedule(o.get('payload'),interval_seconds=o.get('interval_seconds'),first_run=o.get('first_run'),schedule_id=o.get('id'))))
            if self.command=='POST' and path.path=='/v1/schedules/dispatch':return self._send(200,_json_bytes({'runs':runtime.run_scheduled()}))
            if self.command=='POST' and path.path=='/v1/search':
                o=self._object();return self._send(200,_json_bytes({'results':runtime.search(o.get('query'),limit=o.get('limit',50))}))
            if self.command=='GET' and len(route)==4 and route[:3]==['','v1','cache']:
                return self._send(200,_json_bytes(runtime.cache_get(route[3])))
            if self.command=='PUT' and len(route)==4 and route[:3]==['','v1','cache']:
                o=self._object();return self._send(200,_json_bytes(runtime.cache_set(route[3],o.get('value'),ttl_seconds=o.get('ttl_seconds'))))
            if self.command=='POST' and path.path=='/v1/records':
                o=self._object();return self._send(201,_json_bytes(runtime.put_record(o.get('id'),o.get('value'))))
            if self.command=='GET' and path.path=='/v1/export':
                # Export contents are returned inline, capped to the same export ceiling.
                import tempfile
                with tempfile.TemporaryDirectory(prefix='component-export-') as td:
                    p=Path(td)/'export.json';runtime.export_json(p);return self._send(200,p.read_bytes())
            if self.command=='POST' and path.path=='/v1/import':
                raw=self._body(components.MAX_EXPORT)
                import tempfile
                with tempfile.TemporaryDirectory(prefix='component-import-') as td:
                    p=Path(td)/'import.json';p.write_bytes(raw);return self._send(200,_json_bytes(runtime.import_json(p)))
            if self.command=='POST' and path.path=='/v1/notifications':
                if not webhook_url:raise ValueError('webhook origin is not configured by operator')
                o=self._object();return self._send(200,_json_bytes(runtime.notify(webhook_url,o.get('payload'),idempotency_key=o.get('idempotency_key'),headers=o.get('headers'))))
            if self.command=='POST' and path.path=='/v1/integration':
                if not integration_url:raise ValueError('integration origin is not configured by operator')
                o=self._object();return self._send(200,_json_bytes(runtime.integrate(integration_url,method=o.get('method','GET'),payload=o.get('payload'),headers=o.get('headers'),idempotency_key=o.get('idempotency_key'))))
            return self._error(404,'route not found')
        def do_GET(self):self._dispatch()
        def do_POST(self):self._dispatch()
        def do_PUT(self):self._dispatch()
        def _dispatch(self):
            try:self._route()
            except OverflowError as e:self._error(413,str(e))
            except PermissionError as e:self._error(403,str(e))
            except components.ComponentError as e:self._error(400,str(e))
            except (ValueError,TypeError,KeyError) as e:self._error(400,str(e))
            except (BrokenPipeError,ConnectionResetError):pass
    return Handler

def serve(root,token,host='127.0.0.1',port=8765,allowed_origins=None,webhook_url=None,integration_url=None):
    if host not in ('127.0.0.1','::1','localhost'):raise ValueError('service binds only to loopback')
    origins=set(allowed_origins or ())
    if (webhook_url or integration_url) and not origins:raise ValueError('outbound URLs require explicit --allow-origin entries')
    for endpoint in (webhook_url,integration_url):
        if endpoint and f'{urlsplit(endpoint).hostname}:{urlsplit(endpoint).port or 443}' not in origins:raise ValueError('configured endpoint origin is not authorized')
    runtime=components.ComponentRuntime(root,authorized_hosts=origins,network_authorized=bool(origins))
    server=BoundedThreadingHTTPServer((host,port),create_handler(runtime,token,webhook_url=webhook_url,integration_url=integration_url),max_workers=16)
    try:server.serve_forever(poll_interval=.25)
    finally:server.server_close()

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--token-file',type=Path,required=True);p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8765);p.add_argument('--allow-origin',action='append',default=[]);p.add_argument('--webhook-url');p.add_argument('--integration-url');a=p.parse_args(argv)
    try:
        if a.token_file.is_symlink() or not a.token_file.is_file():raise ValueError('token file must be an existing regular non-symlink file')
        token=a.token_file.read_text(encoding='utf-8').strip()
        if len(token)<32 or len(token)>4096:raise ValueError('service token must contain 32..4096 characters')
        serve(a.root,token,a.host,a.port,a.allow_origin,a.webhook_url,a.integration_url);return 0
    except (OSError,ValueError) as e:
        print(json.dumps({'status':'ERROR','error':str(e)}));return 2
if __name__=='__main__':raise SystemExit(main())
