#!/usr/bin/env python3
"""Measure browser quality for an isolated integrated Web candidate without editing sealed inputs."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, hashlib, json, os, shutil, uuid
from pathlib import Path
import application_browser
import application_builder
import application_service
import application_dependencies as deps
import application_toolchain as toolchain
ROOT=Path(__file__).resolve().parents[1]
RUNS=ROOT/'bootstrap-runs'
SOURCE=RUNS/'frontend-framework-20261104'/'generated-backend-preact-integration-rerun4'
BACKEND=RUNS/'generated-web-extended-ce8932caad84'/'candidate'
IMAGE=os.environ.get('AUTOFORGE_QUALITY_IMAGE') or 'sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60'
FRAME=RUNS/'frontend-framework-20261104'
CACHE=FRAME/'cache'
PREACT='3ee2f67884ecaad0652a3bb75d1b6734518dc276f0af653e9fb2d24bfc441c27'
TYPESCRIPT='10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3'
NODE_IMAGE=os.environ.get('AUTOFORGE_QUALITY_NODE_IMAGE') or 'sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da'
def sha(data:bytes)->str:return hashlib.sha256(data).hexdigest()
def manifest(root:Path,goal:str)->dict:
    rows=[]
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.name!='manifest.json':
            raw=path.read_bytes(); rows.append({'path':path.relative_to(root).as_posix(),'size':len(raw),'sha256':sha(raw)})
    body={'schema':1,'goal':goal,'references':[],'files':rows}
    body['candidate_sha256']=sha(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
    body['workspace']=str(root.resolve())
    (root/'manifest.json').write_text(json.dumps(body,sort_keys=True,indent=2)+'\n',encoding='utf8')
    return body
def contract(steps):return {'schema':1,'start_path':'/','steps':steps}
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--engine',choices=('docker','podman'),default='docker');p.add_argument('--authorize-browser',action='store_true');p.add_argument('--output',type=Path,required=True);p.add_argument('--browser-engines',default='chromium,firefox,webkit');a=p.parse_args(argv)
    if not a.authorize_browser:raise SystemExit('BLOCKED: explicit --authorize-browser required')
    if a.output.exists():raise SystemExit('refusing to overwrite evidence')
    run_id=uuid.uuid4().hex
    out=a.output.resolve();out.mkdir(parents=True)
    work=out/'integrated-copy';shutil.copytree(SOURCE,work)
    # Authored changes only: preserve retained generated backend and label the integration glue.
    text='''LOGIN_HTML = """<!DOCTYPE html>
<html><head><title>Sign in</title></head>
<body>
<h1>Sign in</h1>
<form id="login">
<label for="token">Bearer token</label>
<input type="password" id="token" placeholder="Bearer token">
<button type="submit">Login</button>
</form>
<div id="error" role="alert" aria-live="assertive"></div>
<script src="/ui/login.js"></script>
</body></html>"""
TASKS_HTML = """<!DOCTYPE html>
<html><head><title>Tasks</title></head>
<body>
<h1>Tasks</h1>
<form id="create-area">
<label for="title">Task title</label>
<input type="text" id="title" disabled>
<button id="create" type="submit" disabled>Create</button>
</form>
<div id="error" role="alert" aria-live="assertive"></div>
<p id="notice" role="status" aria-live="polite"></p>
<ul id="tasks"></ul>
<p id="empty">No tasks</p>
<div id="readonly" role="status" aria-live="polite" style="display:none">Read only</div>
<script src="/ui/preact.umd.js"></script>
<script src="/ui/dist/client.js"></script>
</body></html>"""
def page(role):
    if role == "reader":
        return TASKS_HTML.replace('<form id="create-area">','<form id="create-area" hidden>').replace('<div id="readonly" role="status" aria-live="polite" style="display:none">','<div id="readonly" role="status" aria-live="polite">')
    return TASKS_HTML
'''
    (work/'html.py').write_text(text,encoding='utf8')
    tsx='''declare const preact:any;\nconst h=preact.h;\nfunction cookie(n:string):string{const m=document.cookie.match(new RegExp("(^| )"+n+"=([^;]+)"));return m?m[2]:"";}\nasync function load():Promise<void>{const r=await fetch("/ui/tasks");if(r.status===401){location.href="/";return;}const d=await r.json();const items=d.items||[];const list=document.getElementById("tasks");if(list)preact.render(h(preact.Fragment,null,...items.map((t:any)=>h("li",{key:t.id},h("span",null,t.title)))),list);const empty=document.getElementById("empty");if(empty)empty.style.display=items.length?"none":"";}\nasync function initialize():Promise<void>{const csrf=await fetch("/ui/csrf");if(!csrf.ok)throw new Error("Unable to prepare secure task form");const value=await csrf.json();if(!value.csrf)throw new Error("Unable to prepare secure task form");document.cookie="csrf="+value.csrf+"; Path=/";const form=document.getElementById("create-area");const field=document.getElementById("title") as HTMLInputElement;const button=document.getElementById("create") as HTMLButtonElement;if(form)form.addEventListener("submit",async(e:any)=>{e.preventDefault();const error=document.getElementById("error");const notice=document.getElementById("notice");const r=await fetch("/ui/tasks",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":cookie("csrf")},body:JSON.stringify({title:field.value})});if(r.ok){field.value="";if(error)error.textContent="";if(notice)notice.textContent="Task created";await load();}else if(error)error.textContent="Unable to create task";});await load();if(field)field.disabled=false;if(button)button.disabled=false;}\ninitialize().catch(()=>{const error=document.getElementById("error");if(error)error.textContent="Unable to prepare secure task form";});\n'''
    (work/'src'/'client.tsx').write_text(tsx,encoding='utf8')
    (work/'package.json').write_text('{"name":"autoforge-web-quality-preact","version":"1.0.0","private":true,"dependencies":{"preact":"10.26.4"},"devDependencies":{"typescript":"5.9.3"}}\n',encoding='utf8')
    (work/'tsconfig.json').write_text('{"compilerOptions":{"target":"ES2020","module":"commonjs","moduleResolution":"node","jsx":"react","jsxFactory":"h","outDir":"ui/dist","rootDir":"src","strict":true,"skipLibCheck":true},"include":["src/**/*.tsx"]}\n',encoding='utf8')
    records=[json.loads((CACHE/(d+'.json')).read_text(encoding='utf8')) for d in (PREACT,TYPESCRIPT)]
    packages=[{'name':'preact','version':'10.26.4','source':'npmjs','artifact_sha256':PREACT,'license':'MIT','dependencies':{}},{'name':'typescript','version':'5.9.3','source':'npmjs','artifact_sha256':TYPESCRIPT,'license':'Apache-2.0','dependencies':{}}]
    lock={'schema':1,'ecosystem':'typescript','toolchain':{'name':'node','version':'operator-image','container_image':NODE_IMAGE},'packages':packages}
    env=deps.environment_manifest(lock,sources={'npmjs':'https://registry.npmjs.org'},artifacts=records,cache=CACHE)
    cm=manifest(work,'Retained generated Web backend and authored Preact framework accessibility repair')
    build=toolchain.run(env,cm,CACHE,a.engine,out/'framework-build',candidate_root=work,image=NODE_IMAGE,build_argv=['/bin/sh','-c','node node_modules/typescript/bin/tsc -p tsconfig.json'],repeat=True,timeout=600)
    if build['status']!='PASS':raise RuntimeError('offline authored Preact TypeScript build failed')
    shutil.copyfile(out/'framework-build'/'ui'/'dist'/'client.js',work/'ui'/'dist'/'client.js')
    shutil.copyfile(SOURCE/'preact.umd.js',work/'preact.umd.js');shutil.copyfile(work/'preact.umd.js',work/'ui'/'preact.umd.js')
    main=work/'main.py'
    data=main.read_text(encoding='utf8')
    # Authored integration framing: do not rely on tunnel EOF to delimit bodies.
    data=data.replace('self.send_header("Content-Type", "application/json")\n        self.end_headers()', 'self.send_header("Content-Type", "application/json")\n        self.send_header("Content-Length", str(len(json.dumps(body).encode())) if body is not None else "0")\n        self.end_headers()')
    data=data.replace('self.send_header("Content-Type", mime)\n        self.end_headers()', 'self.send_header("Content-Type", mime)\n        self.send_header("Content-Length", str(len(data)))\n        self.end_headers()')
    data=data.replace('self.send_header("Content-Type", "text/html")\n        self.end_headers()', 'self.send_header("Content-Type", "text/html")\n        self.send_header("Content-Length", str(len(body.encode() if isinstance(body,str) else body)))\n        self.end_headers()')
    data=data.replace('self.end_headers()\n                    self.wfile.write(json.dumps(r[1]).encode())', 'self.send_header("Content-Length", str(len(json.dumps(r[1]).encode())))\n                    self.end_headers()\n                    self.wfile.write(json.dumps(r[1]).encode())')
    data=data.replace('self.end_headers()\n                self.wfile.write(json.dumps(r[1]).encode())', 'self.send_header("Content-Length", str(len(json.dumps(r[1]).encode())))\n                self.end_headers()\n                self.wfile.write(json.dumps(r[1]).encode())')
    main.write_text(data,encoding='utf8')
    (work/'integration-label.json').write_text(json.dumps({'model_generated_backend':True,'authored_framework_glue':True,'quality_repair_authored':True,'framework':'Preact 10.26.4','compiler':'TypeScript 5.9.3'},sort_keys=True)+'\n')
    cm=manifest(work,'Retained generated Web backend and authored Preact framework accessibility repair')
    browser_steps=[{'action':'assert_role','role':'heading','name':'Sign in'},{'action':'assert_label','name':'Bearer token'},{'action':'focus','selector':'#token'},{'action':'assert_focused','selector':'#token'},{'action':'fill','selector':'#token','value':'{$secret_file:ADMIN}'},{'action':'press','selector':'#token','value':'Enter'},{'action':'assert_role','role':'heading','name':'Tasks'},{'action':'assert_label','name':'Task title'},{'action':'assert_enabled','selector':'#create'},{'action':'focus','selector':'#title'},{'action':'assert_focused','selector':'#title'},{'action':'fill','selector':'#title','value':'Framework persisted task'},{'action':'press','selector':'#title','value':'Enter'},{'action':'assert_status','role':'status','text':'Task created'},{'action':'assert_list','selector':'#tasks','count':1},{'action':'assert_text','selector':'#tasks','text':'Framework persisted task'},{'action':'reload'},{'action':'assert_list','selector':'#tasks','count':1},{'action':'assert_text','selector':'#tasks','text':'Framework persisted task'},{'action':'screenshot','name':'admin-keyboard-a11y'}]
    service_contract={'schema':1,'health_path':'/health','scenarios':[{'name':'backend-health-and-auth-denials','requests':[{'method':'GET','path':'/health','body':None,'expected':{'status':200,'body':{'ok':True}}},{'method':'GET','path':'/api/tasks','body':None,'expected':{'status':401,'body':{'error':'unauthorized'}}},{'method':'POST','path':'/api/tasks','body':{'title':'reader forbidden'},'headers':{'Authorization':'Bearer {$secret_file:READER}'},'expected':{'status':403,'body':{'error':'forbidden'}}}]}]}
    secrets=BACKEND.parent/'private';mounts=[{'source':str(secrets/name),'target':'/run/autoforge/secrets/'+name,'read_only':True,'name':name,'sha256':sha((secrets/name).read_bytes())} for name in ('ADMIN','READER')]
    service_results={}
    for engine_name in [x.strip() for x in a.browser_engines.split(',') if x.strip()]:
        service_results[engine_name]=application_service.run(cm,service_contract,a.engine,IMAGE,['python','-I','-c','import runpy,sys;sys.path.insert(0,"/candidate");runpy.run_path("/candidate/main.py",run_name="__main__")'],timeout=60,browser_contract=contract(browser_steps),browser_output=out/('browser-'+engine_name),browser_authorized=True,browser_engine=engine_name,measure_resources=True,secret_mounts=mounts)
    primary=service_results.get('chromium',next(iter(service_results.values())))
    resource=primary.get('resource_witness')
    limits={'cpu_usage_usec':60_000_000,'memory_peak_bytes':256*1024*1024}
    threshold_status='UNRESOLVED' if not resource else ('PASS' if resource['cpu_usage_usec']<=limits['cpu_usage_usec'] and resource['memory_peak_bytes']<=limits['memory_peak_bytes'] else 'FAIL')
    negative_root=out/'negative-missing-task-label'
    shutil.copytree(work,negative_root)
    negative_html=negative_root/'html.py'
    negative_html.write_text(negative_html.read_text(encoding='utf8').replace('<label for="title">Task title</label>','<span>Task title</span>'),encoding='utf8')
    negative_manifest=manifest(negative_root,'Deliberately broken accessible task label; never promoted')
    negative_report=application_service.run(negative_manifest,service_contract,a.engine,IMAGE,['python','-I','-c','import runpy,sys;sys.path.insert(0,"/candidate");runpy.run_path("/candidate/main.py",run_name="__main__")'],timeout=60,browser_contract=contract(browser_steps),browser_output=out/'browser-negative-label',browser_authorized=True,browser_engine='chromium',secret_mounts=mounts)
    rejected=negative_report.get('browser',{}).get('status')=='FAIL' and not negative_report.get('test_pass')
    low_limit_rejected=bool(resource) and not (resource['cpu_usage_usec']<=0 and resource['memory_peak_bytes']<=0)
    negative={'status':'PASS' if rejected and low_limit_rejected else 'FAIL','missing_accessible_label':{'detected':rejected,'candidate_sha256':negative_manifest['candidate_sha256'],'report':negative_report},'zero_resource_limits':{'status':'FAIL' if low_limit_rejected else 'PASS','expected':'FAIL','limits':{'cpu_usage_usec':0,'memory_peak_bytes':0},'observed':resource}}
    (out/'quality-evidence.json').write_text(json.dumps({'schema':1,'candidate_sha256':cm['candidate_sha256'],'backend_original_sha256':json.loads((BACKEND/'manifest.json').read_text())['candidate_sha256'],'authored_quality_repair':True,'framework_build':build,'business_browser_20_step_contract':True,'browser_engines':service_results,'resource_witness':resource,'resource_thresholds':limits,'resource_threshold_status':threshold_status,'negative_regressions':negative,'scope_limits':['Authored Preact integration copy, not model-generated frontend repair.','cgroup CPU and memory.peak cover isolated service container only; browser engines run on host and their CPU/peak RAM are NOT MEASURED.','memory.peak is cgroup aggregate peak, not process RSS.','Role/name, focus, live-region DOM text and declared browser actions are measured; no screen reader or WCAG-wide certification.','Chrome brand distinct from Chromium engine; no brand inference for Firefox/WebKit.']},sort_keys=True,indent=2)+'\n',encoding='utf8')
    overall='PASS' if threshold_status=='PASS' and negative['status']=='PASS' and all(v.get('test_pass') for v in service_results.values()) else 'FAIL'
    print(json.dumps({'status':overall,'engines':{k:v.get('browser',{}).get('status',v.get('test_pass')) for k,v in service_results.items()},'negative':negative['status'],'evidence':str(out/'quality-evidence.json'),'screenshot_dir':str(out/'browser-chromium')},sort_keys=True));return 0 if overall=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
