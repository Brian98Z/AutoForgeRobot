#!/usr/bin/env python3
"""Exercise browser runner against a trusted authored loopback fixture, never a candidate."""
from __future__ import annotations
import hashlib, http.server, json, socket, tempfile, threading
from pathlib import Path
import application_browser as browser

HTML='''<!doctype html><html><body><main><h1>Fixture ready</h1><div id="text-only">unexpected item</div><div id="truly-empty" style="min-height:20px"></div><form><label for="task">Task</label><input id="task"><label><input id="choice" type="checkbox">Accept</label><button id="save" type="button">Save</button></form><p role="status" id="status">Empty</p><ul id="items"></ul><script>document.getElementById('choice').addEventListener('change',e=>{document.getElementById('status').textContent=e.target.checked?'Checked':'Unchecked';});document.getElementById('task').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();document.getElementById('save').click();}});document.getElementById('save').onclick=()=>{const v=document.getElementById('task').value;document.getElementById('items').innerHTML=v?'<li>'+v+'</li>':'';document.getElementById('status').textContent=v?'Saved':'Required';};</script></main></body></html>'''
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/','/index.html'): self.send_error(404); return
        b=HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*args): pass

def main():
    checks=['loopback origin and typed contract validation']
    contract={'schema':1,'start_path':'/','steps':[{'action':'assert_text','selector':'h1','text':'Fixture ready'},{'action':'assert_label','name':'Task'},{'action':'check','selector':'#choice','value':True},{'action':'assert_status','role':'status','text':'Checked'},{'action':'check','selector':'#choice','value':False},{'action':'assert_status','role':'status','text':'Unchecked'},{'action':'focus','selector':'#task'},{'action':'assert_focused','selector':'#task'},{'action':'fill','selector':'#task','value':'Persisted task'},{'action':'press','selector':'#task','value':'Enter'},{'action':'assert_status','role':'status','text':'Saved'},{'action':'assert_list','selector':'#items','count':1},{'action':'reload'},{'action':'assert_form','selector':'#task','value':''},{'action':'screenshot','name':'fixture'}]}
    for invalid_value in ('true',1,None):
        try:browser._contract({'schema':1,'start_path':'/','steps':[{'action':'check','selector':'#choice','value':invalid_value}]});raise AssertionError('non-boolean checkbox value accepted')
        except browser.BrowserRefused:pass
    for malformed in (
        {'action':'assert_text','selector':'#text-only','text':[]},
        {'action':'assert_text','selector':'#text-only','text':'x'*2049},
        {'action':'assert_label','name':[]},
        {'action':'assert_role','role':'not-an-aria-role','name':'Task'},
    ):
        try: browser._contract({'schema':1,'start_path':'/','steps':[malformed]})
        except browser.BrowserRefused: pass
        else: raise AssertionError('malformed assertion could bypass bounded semantic checks')
    for bad in ('http://example.com:1234','http://127.0.0.1','https://127.0.0.1:8080'):
        try: browser._origin(bad); raise AssertionError('external or implicit origin accepted')
        except browser.BrowserRefused: pass
    try: browser._contract({'schema':1,'start_path':'//example.com','steps':contract['steps']}); raise AssertionError('protocol-relative path accepted')
    except browser.BrowserRefused: pass
    att={'schema':1,'status':'READY','origin':'http://127.0.0.1:9999','candidate_sha256':'0'*64,'service_id':'test','adapter_verified':True}
    try: browser.validate_attestation(att,att['origin'],att['candidate_sha256'],authorized=False); raise AssertionError('missing operator authorization accepted')
    except browser.BrowserRefused: pass
    for invalid in ([{'action':'assert_status','role':'main','text':'not an announcement'}],[{'action':'press','selector':'#task','value':'not-a-real-action'}]):
        try: browser._contract({'schema':1,'start_path':'/','steps':invalid}); raise AssertionError('invalid browser assertion accepted')
        except browser.BrowserRefused: pass
    checks.append('invalid status roles and unsupported keyboard names rejected')
    checks.append('unapproved browser run rejected')
    if __import__('importlib').util.find_spec('playwright') is None:
        print(json.dumps({'schema':1,'host_logic':'PASS','browser':'BLOCKED','reason':'Playwright is not installed; no installation attempted','checks':checks},ensure_ascii=False)); return 2
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler); worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
    try:
        port=server.server_address[1]; origin=f'http://127.0.0.1:{port}'; digest=hashlib.sha256(b'trusted-authored-fixture').hexdigest(); att={'schema':1,'status':'READY','origin':origin,'candidate_sha256':digest,'service_id':'trusted-fixture','adapter_verified':True}
        with tempfile.TemporaryDirectory() as d:
            try: result=browser.run(origin,digest,att,contract,Path(d),authorized=True)
            except browser.BrowserRefused as exc:
                if 'BLOCKED' not in str(exc): raise
                print(json.dumps({'schema':1,'host_logic':'PASS','browser':'BLOCKED','reason':str(exc),'checks':checks},ensure_ascii=False)); return 2
            assert result['status']=='PASS' and len(result['artifacts'])==1 and Path(d,'fixture.png').is_file()
            boundary={'schema':1,'start_path':'/','steps':[{'action':'assert_empty','selector':'#text-only'}]}
            try:browser.run(origin,digest,att,boundary,Path(d)/'negative',authorized=True)
            except browser.BrowserRefused: pass
            else:raise AssertionError('text-only nonempty element accepted as empty')
            boundary['steps'][0]['selector']='#truly-empty'
            assert browser.run(origin,digest,att,boundary,Path(d)/'positive',authorized=True)['status']=='PASS'
            bad_semantics={'schema':1,'start_path':'/','steps':[{'action':'assert_label','name':'Missing label'}]}
            try: browser.run(origin,digest,att,bad_semantics,Path(d)/'bad-label',authorized=True)
            except browser.BrowserRefused: pass
            else: raise AssertionError('browser accepted missing accessible label')
        checks.append('text-only nonempty rejected and visible empty element accepted by real browser')
        checks.append('real headless Playwright navigation, declarative interaction, reload and screenshot hash against authored static fixture')
    finally: server.shutdown(); server.server_close(); worker.join(timeout=2)
    print(json.dumps({'schema':1,'host_logic':'PASS','browser':'PASS','checks':checks},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())