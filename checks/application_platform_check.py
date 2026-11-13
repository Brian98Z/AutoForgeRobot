#!/usr/bin/env python3
"""Platform adapter regressions and optional live container-backed SSE acceptance."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse,hashlib,json,os,shlex,subprocess,sys,tempfile,threading,time,uuid
from pathlib import Path
import xml.etree.ElementTree as ET
import application_platform as p
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
def expect(fn):
    try:fn()
    except (p.PlatformRefused,ValueError,KeyError):return
    raise AssertionError('unsafe or incomplete platform acceptance was accepted')
class PrefixedSocket:
    def __init__(self,sock,prefix):self.sock=sock;self.prefix=bytearray(prefix)
    def settimeout(self,value):self.sock.settimeout(value)
    def recv(self,size):
        if self.prefix:
            data=bytes(self.prefix[:size]);del self.prefix[:size];return data
        return self.sock.recv(size)
    def close(self):self.sock.close()
class EventFixture(BaseHTTPRequestHandler):
    events=[];connections=[];reverse=False
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/health':self.send_response(200);self.end_headers();return
        if self.path!='/events':self.send_error(404);return
        self.close_connection=False;self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache');self.end_headers();self.wfile.flush()
        last=self.headers.get('Last-Event-ID');type(self).connections.append(last);rows=list(type(self).events)
        if type(self).reverse:rows.reverse()
        for ident,value in rows:
            if last is None or int(ident)>=int(last):
                try:self.wfile.write(f'id: {ident}\ndata: {json.dumps(value,separators=(",",":"))}\n\n'.encode());self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError):return
        self.wfile.write(b'\n\n');self.wfile.flush()
    def do_POST(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=65536:self.send_error(400);return
        value=json.loads(self.rfile.read(size));ident=str(value['id']);type(self).events.append((ident,value));self.send_response(202);self.end_headers()
def realtime_smoke():
    EventFixture.events=[];EventFixture.connections=[];EventFixture.reverse=False
    server=ThreadingHTTPServer(('127.0.0.1',0),EventFixture);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();port=server.server_address[1]
    try:
        first={'id':1,'value':'alpha'};second={'id':2,'value':'beta'}
        contract={'schema':1,'target':'authored-sse-fixture','url':'http://127.0.0.1:8000/events','protocol':'sse','authentication':{'header':'Authorization','value':'Bearer authored-fixture-token'},'unauthorized':[{'operation':'subscribe','expected_status':401,'path':'/events'},{'operation':'publish','expected_status':401,'path':'/publish'},{'operation':'stop','expected_status':401,'path':'/stop'}],'stop':{'path':'/stop','body':{'action':'stop'},'expected_status':202},'scenarios':[{'name':'ordered-reconnect','send':[{'path':'/publish','body':first},{'path':'/publish','body':second}],'expected_events':[{'id':'1','data':json.dumps(first,separators=(',',':')),'json':first},{'id':'2','data':json.dumps(second,separators=(',',':')),'json':second}],'reconnect_after':1}] }
        p._event_contract(contract);EventFixture.events=[('1',first),('2',second)]
        def receive(last=None):
            s=__import__('socket').create_connection(('127.0.0.1',port),timeout=2);s.settimeout(2)
            headers=f'Last-Event-ID: {last}\r\n' if last is not None else ''
            s.sendall(f'GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{headers}\r\n'.encode())
            buffered=bytearray()
            while b'\r\n\r\n' not in buffered:buffered.extend(s.recv(4096))
            carry=buffered.split(b'\r\n\r\n',1)[1]
            if carry:s=PrefixedSocket(s,carry)
            event=p._sse_read(s,time.monotonic()+2);s.close();return event
        e1=receive();replay=receive(e1['id']);e2=receive('2')
        if e1['json']!=first or replay!=e1 or e2['json']!=second:raise AssertionError('SSE ordered/reconnect positive assertion failed')
        EventFixture.reverse=True
        assert receive()['json']==second
        return {'ordered_fixture':'PASS','reconnect_replay':'PASS','reverse_delivery_detected':'PASS','scope':'authored host event transport; live container adapter optional'}
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
def guard_smoke():
    with tempfile.TemporaryDirectory(prefix='autoforge-platform-check-') as td:
        root=Path(td);src=root/'candidate';src.mkdir();content=b'real candidate bytes\n';apk=b'APK bytes fixture\n';(src/'main.py').write_bytes(content);(src/'app.apk').write_bytes(apk)
        files=[{'path':'app.apk','size':len(apk),'sha256':hashlib.sha256(apk).hexdigest()},{'path':'main.py','size':len(content),'sha256':hashlib.sha256(content).hexdigest()}]
        base={'schema':1,'goal':'platform smoke','references':[],'files':files};digest=hashlib.sha256(p._canonical(base)).hexdigest();manifest={**base,'candidate_sha256':digest,'workspace':str(src)}
        assert p._manifest(manifest)[1]==digest
        (src/'main.py').write_bytes(b'tampered');expect(lambda:p._manifest(manifest));(src/'main.py').write_bytes(content)
        execution={'adb_sha256':'a'*64,'serial':'emulator-5554','apk_path':'app.apk','apk_sha256':files[0]['sha256'],'build_evidence_sha256':None,'package':'org.example.app','activity':'org.example.app/.Main','timeout_seconds':120}
        auth={'schema':1,'authorize':True,'platform':'android','target':'acceptance-device','candidate_sha256':digest,'execution':execution,'expires_at':9999999999,'nonce':'x'*16}
        p._authority(auth,'android','acceptance-device',digest,execution)
        expect(lambda:p._authority(auth,'android','acceptance-device',digest,{**execution,'serial':'other-device'}))
        expect(lambda:p._authority(auth,'android','acceptance-device',digest,{**execution,'apk_sha256':'0'*64}))
        for expiry in (float('nan'),float('inf')):expect(lambda v=expiry:p._authority({**auth,'expires_at':v},'android','acceptance-device',digest,execution))
        outside_apk=root/'built.apk';outside_apk.write_bytes(b'APK generated by isolated build\n');outside_sha=hashlib.sha256(outside_apk.read_bytes()).hexdigest()
        build_evidence={'status':'BUILT','candidate_sha256':digest,'apk_sha256':outside_sha,'apk':str(outside_apk),'container_image':'sha256:'+'a'*64,'container_network':'none','sdk_mount':'read-only','candidate_mount':'read-only manifest-staged only','candidate_executed':False,'build_tools_version':'36.0.0','signing':'ephemeral debug test key; not trusted/release','limitations':['fixture']}
        derived_execution={'adb_sha256':hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),'serial':'emulator-5554','apk_path':'@isolated-build/'+outside_sha,'apk_sha256':outside_sha,'build_evidence_sha256':hashlib.sha256(p._canonical(build_evidence)).hexdigest(),'package':'org.example.app','activity':'org.example.app/.Main','timeout_seconds':120}
        valid_contract={'schema':1,'target':'acceptance-device','scenarios':[{'name':'acceptance','actions':[],'assertions':[{'kind':'text','operator':'equals','value':'Ready'}]}]}
        runner=str(Path(sys.executable).resolve())
        trusted_adb=p._sha(Path(runner))
        base_args=(manifest,valid_contract,{**auth,'execution':derived_execution},runner,trusted_adb,'emulator-5554',str(outside_apk),outside_sha,'org.example.app','org.example.app/.Main',120)
        for invalid in (None,{**build_evidence,'candidate_sha256':'0'*64},{**build_evidence,'apk_sha256':'0'*64}):
            calls=[]
            try:
                from unittest.mock import patch
                with patch.object(p,'_adb',side_effect=lambda *a,**k:calls.append(a)):
                    p._android(*base_args,invalid)
            except p.PlatformRefused as exc:
                assert 'isolated build evidence bound to this candidate and APK digest' in str(exc),exc
            else:raise AssertionError('missing/mismatched isolated build receipt was accepted')
            assert not calls,'ADB ran before the build-evidence refusal'
        altered_execution={**derived_execution,'build_evidence_sha256':'0'*64}
        expect(lambda:p._authority({**auth,'execution':altered_execution},'android','acceptance-device',digest,derived_execution))
        android={'schema':1,'target':'acceptance-device','scenarios':[{'name':'login','actions':[{'type':'type','text':'x; touch /sdcard/pwned'}],'assertions':[{'kind':'text','target':'org.example.app:id/status','operator':'equals','value':'Welcome'}]}]}
        assert len(p._scenario(android,'android'))==1 and shlex.quote(android['scenarios'][0]['actions'][0]['text']).startswith("'")
        xml='<hierarchy><node text="Welcome" resource-id="org.example.app:id/unrelated" content-desc="" checked="false" enabled="true"/></hierarchy>';nodes=list(ET.fromstring(xml).iter('node'))
        assert not any(n.get('resource-id')=='org.example.app:id/status' for n in nodes)
        desktop={'schema':1,'target':'Fixture Window','scenarios':[{'name':'portable','actions':[{'type':'type','target':'query','value':'safe text'},{'type':'wait','value':10}],'assertions':[{'kind':'element','target':'submit','operator':'exists','value':True},{'kind':'state','target':'submit','operator':'enabled','value':True}]}]}
        assert len(p._scenario(desktop,'desktop'))==1
        expect(lambda:p._scenario({**desktop,'scenarios':[{'name':'bad','actions':[{'type':'wait','value':5001}],'assertions':desktop['scenarios'][0]['assertions']}]},'desktop'))
        expect(lambda:p._scenario({**desktop,'scenarios':[{'name':'bad','actions':[{'type':'key','target':'x','value':'x; Start-Process evil'}],'assertions':desktop['scenarios'][0]['assertions']}]},'desktop'))
        row=next(x for x in files if x['path']=='main.py')
        expect(lambda:p._desktop(manifest,desktop,{},'missing-runner','0'*64,30,'main.py',row['sha256'],[]))
        ws_contract={'schema':1,'target':'unsupported-ws','url':'ws://127.0.0.1:8000/events','protocol':'websocket','authentication':{'header':'Authorization','value':'Bearer unsupported-ws-token'},'unauthorized':[{'operation':'subscribe','expected_status':401,'path':'/events'},{'operation':'publish','expected_status':401,'path':'/publish'},{'operation':'stop','expected_status':401,'path':'/stop'}],'stop':{'path':'/stop','body':{'action':'stop'},'expected_status':202},'scenarios':[{'name':'unsupported','send':[{}],'expected_events':[{}],'reconnect_after':0}]}
        expect(lambda:p._event_contract(ws_contract))
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--live-realtime',action='store_true');ap.add_argument('--engine',choices=('docker','podman'),default='docker');ap.add_argument('--image',help='exact local Linux sha256 image ID; required with --live-realtime');ap.add_argument('--report',type=Path,help='absent path for persistent evidence');a=ap.parse_args()
    checks=guard_smoke();realtime=realtime_smoke();live=None
    if a.live_realtime:
        if not a.image:raise SystemExit('--live-realtime requires --image sha256:<exact-local-id>')
        raw_image=a.image.removeprefix('sha256:');image='sha256:'+raw_image;argv=['python','-I','-u','/candidate/main.py']
        with tempfile.TemporaryDirectory(prefix='autoforge-realtime-live-') as td:
            root=Path(td);candidate=root/'candidate';candidate.mkdir();fixture=candidate/'main.py'
            fixture.write_text('''import json,threading,sys
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
events=[];cv=threading.Condition();stopped=False;TOKEN="operator-authorized-live-token"
def authorized(h):return h.headers.get("Authorization")=="Bearer "+TOKEN
print("candidate diagnostic authorization: Bearer "+TOKEN,file=sys.stderr,flush=True)
print("candidate diagnostic bare token: "+TOKEN,file=sys.stderr,flush=True)
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def reply(self,status,headers=None):self.send_response(status);[self.send_header(k,v) for k,v in (headers or {}).items()];self.end_headers()
 def do_GET(self):
  global stopped
  if self.path=="/health":self.reply(200);return
  if self.path!="/events":self.reply(404);return
  if not authorized(self):self.reply(401);return
  if stopped:self.reply(410);return
  self.send_response(200);self.send_header("Content-Type","text/event-stream");self.end_headers();self.wfile.flush();last=self.headers.get("Last-Event-ID");cursor=int(last) if last is not None else 0
  while True:
   with cv:
    rows=[e for e in events if int(e["id"])>cursor]
    if not rows and not stopped:cv.wait(timeout=5);rows=[e for e in events if int(e["id"])>cursor]
    if stopped:return
   if not rows:return
   for e in rows:
    try:self.wfile.write(("id: "+e["id"]+"\\ndata: "+json.dumps(e["body"],separators=(",",":"))+"\\n\\n").encode());self.wfile.flush();cursor=int(e["id"])
    except (BrokenPipeError,ConnectionResetError):return
 def do_POST(self):
  global stopped
  if self.path=="/publish" and not authorized(self):self.reply(401);return
  if self.path=="/stop" and not authorized(self):self.reply(401);return
  v=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
  if self.path=="/publish":
   e={"id":str(v["id"]),"body":v}
   with cv:events.append(e);cv.notify_all()
   self.reply(202);return
  if self.path=="/stop":
   with cv:stopped=True;cv.notify_all()
   self.reply(202);return
  self.reply(404)
ThreadingHTTPServer(("0.0.0.0",8000),H).serve_forever()
''',encoding='utf8')
            rows=[]
            for rel in ('main.py',):
                data=(candidate/rel).read_bytes();rows.append({'path':rel,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            body={'schema':1,'goal':'Realtime SSE acceptance fixture','references':[],'files':rows};digest=hashlib.sha256(p._canonical(body)).hexdigest();manifest={**body,'candidate_sha256':digest,'workspace':str(candidate)}
            values=[{'id':1,'value':'alpha'},{'id':2,'value':'Bearer operator-authorized-live-token'}]
            expected=[{'id':str(v['id']),'data':json.dumps(v,separators=(',',':')),'json':v} for v in values]
            contract={'schema':1,'target':'autoforge-realtime-fixture','url':'http://127.0.0.1:8000/events','protocol':'sse','authentication':{'header':'Authorization','value':'Bearer operator-authorized-live-token'},'unauthorized':[{'operation':'subscribe','expected_status':401,'path':'/events'},{'operation':'publish','expected_status':401,'path':'/publish'},{'operation':'stop','expected_status':401,'path':'/stop'}],'stop':{'path':'/stop','body':{'action':'stop'},'expected_status':202},'scenarios':[{'name':'ordered-reconnect-with-secret-echo','send':[{'path':'/publish','body':v} for v in values],'expected_events':expected,'reconnect_after':1}]}
            authority={'schema':1,'authorize':True,'platform':'realtime','target':contract['target'],'candidate_sha256':digest,'execution':{'engine':a.engine,'image':image,'command_sha256':hashlib.sha256(p._canonical(argv)).hexdigest(),'contract_sha256':hashlib.sha256(p._canonical(contract)).hexdigest(),'protocol':'sse','url':contract['url'],'timeout_seconds':30},'expires_at':time.time()+300,'nonce':uuid.uuid4().hex}
            try:
                manifest_path=root/'manifest.json';contract_path=root/'contract.json';authority_path=root/'authority.json';output_path=root/'adapter-report.json'
                manifest_path.write_bytes(p._canonical(manifest));contract_path.write_bytes(p._canonical(contract));authority_path.write_bytes(p._canonical(authority))
                cli=subprocess.run([sys.executable,str(Path(p.__file__).resolve()),'realtime','--manifest',str(manifest_path),'--contract',str(contract_path),'--authority',str(authority_path),'--engine',a.engine,'--image',image,'--command-json',json.dumps(argv),'--timeout','30','--output',str(output_path)],capture_output=True,check=False,timeout=120)
                if cli.returncode!=0:
                    secrets=(contract['authentication']['value'],contract['authentication']['value'].removeprefix('Bearer '))
                    diagnostics=(cli.stdout+cli.stderr).decode('utf8','replace')
                    for secret in secrets:diagnostics=diagnostics.replace(secret,'[REDACTED]')
                    raise AssertionError('live realtime CLI failed: '+diagnostics[-2048:])
                report=json.loads(output_path.read_text(encoding='utf8'));cli_stdout=cli.stdout
                secrets=(contract['authentication']['value'],contract['authentication']['value'].removeprefix('Bearer '))
                if any(secret.encode() in cli_stdout for secret in secrets):raise AssertionError('realtime CLI stdout leaked exact authorization value')
                if any(secret in p._canonical(report).decode('utf8') for secret in secrets):raise AssertionError('realtime report leaked exact authorization value')
                if report['scenarios'][1]['observed'][1]['json']['value']!='[REDACTED]':raise AssertionError('event equality failed before outbound secret scrubbing')
                if 'candidate diagnostic authorization: [REDACTED]' not in report.get('logs',''):raise AssertionError('candidate auth diagnostic was absent or not redacted')
                live={'status':report['status'],'scenarios':report['scenarios'],'cleanup':report['cleanup'],'image_id':report['image_id'],'exact_secrets_absent_from_report_and_stdout':True,'candidate_diagnostic_redacted':True,'event_secret_scrubbed_after_match':True}
            except Exception as exc:
                safe_error=str(exc).replace(contract['authentication']['value'],'[REDACTED]').replace(contract['authentication']['value'].removeprefix('Bearer '),'[REDACTED]')
                failure={'schema':1,'kind':'autoforge-realtime-live-smoke','manifest_sha256':digest,'contract_sha256':hashlib.sha256(p._canonical(contract)).hexdigest(),'authority_sha256':hashlib.sha256(p._canonical(authority)).hexdigest(),'status':'FAIL','error':safe_error}
                out=a.report or (Path.cwd()/('application-platform-realtime-'+uuid.uuid4().hex+'.json'))
                out.parent.mkdir(parents=True,exist_ok=True)
                with out.open('xb') as stream:stream.write(p._canonical(failure)+b'\n')
                raise
            if report['status']!='PASS':raise AssertionError(f'live realtime acceptance failed: {live}')
            evidence={'schema':1,'kind':'autoforge-realtime-live-smoke','manifest_sha256':digest,'contract_sha256':hashlib.sha256(p._canonical(contract)).hexdigest(),'authority_sha256':hashlib.sha256(p._canonical(authority)).hexdigest(),'report':report,'status':report['status']}
            out=a.report or (Path.cwd()/('application-platform-realtime-'+uuid.uuid4().hex+'.json'))
            out.parent.mkdir(parents=True,exist_ok=True)
            with out.open('xb') as stream:stream.write(p._canonical(evidence)+b'\n')
            live['evidence_path']=str(out)
            evidence_bytes=out.read_bytes()
            if contract['authentication']['value'].encode() in evidence_bytes:raise AssertionError('persisted smoke evidence leaked exact authorization value')
    print(json.dumps({'status':'PASS','checks':checks,'host_realtime':realtime,'live_realtime':live,'scope':'manifest-bound authored realtime SSE producer/subscriber smoke with authenticated authorization, unauthorized rejection, Last-Event-ID reconnect, authenticated stop and post-stop refusal; not a generated application or general websocket/production acceptance'},sort_keys=True))
if __name__=='__main__':main()
