"""Build authored Preact glue around retained generated backend in a locked offline container, then exercise the isolated service in Chromium."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, hashlib, json, shutil
from pathlib import Path
import application_dependencies as deps
import application_toolchain as toolchain
import application_service

ROOT=Path(__file__).resolve().parents[1]
RUNS=ROOT/'bootstrap-runs'
FRAME=RUNS/'frontend-framework-20261104'
BACKEND=RUNS/'generated-web-extended-ce8932caad84'/'candidate'
CACHE=FRAME/'cache'
PREACT='3ee2f67884ecaad0652a3bb75d1b6734518dc276f0af653e9fb2d24bfc441c27'
TYPESCRIPT='10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3'

def sha(data:bytes)->str:return hashlib.sha256(data).hexdigest()
def file_manifest(root:Path,goal:str)->dict:
    rows=[]
    for p in sorted(root.rglob('*')):
        if p.is_file() and p.name!='manifest.json':
            raw=p.read_bytes(); rows.append({'path':p.relative_to(root).as_posix(),'size':len(raw),'sha256':sha(raw)})
    m={'schema':1,'goal':goal,'references':[],'files':rows,'workspace':str(root.resolve())}
    canonical={key:m[key] for key in ('schema','goal','references','files')}
    m['candidate_sha256']=sha(json.dumps(canonical,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
    (root/'manifest.json').write_text(json.dumps(m,sort_keys=True,indent=2)+'\n',encoding='utf8')
    return m

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--engine',choices=('docker','podman'),default='docker')
    ap.add_argument('--node-image',required=True); ap.add_argument('--python-image',required=True)
    ap.add_argument('--candidate-out',type=Path,required=True); ap.add_argument('--build-out',type=Path,required=True); ap.add_argument('--service-out',type=Path,required=True)
    ap.add_argument('--authorize-browser',action='store_true',help='authorize managed Chromium acceptance')
    a=ap.parse_args()
    try:
        for p in (a.candidate_out,a.build_out,a.service_out):
            if p.exists(): raise ValueError('candidate/build/service output paths must be unique and absent')
        a.candidate_out.mkdir(parents=True)
        shutil.copytree(BACKEND,a.candidate_out,dirs_exist_ok=True)
        # Authored UI adapter; all generated backend files remain byte-identical.
        (a.candidate_out/'src').mkdir()
        (a.candidate_out/'package.json').write_text('{"name":"autoforge-generated-backend-preact-integration","version":"1.0.0","private":true,"dependencies":{"preact":"10.26.4"},"devDependencies":{"typescript":"5.9.3"}}\n',encoding='utf8')
        (a.candidate_out/'tsconfig.json').write_text('{"compilerOptions":{"target":"ES2020","module":"commonjs","moduleResolution":"node","jsx":"react","jsxFactory":"h","outDir":"ui/dist","rootDir":"src","strict":true,"skipLibCheck":true},"include":["src/**/*.tsx"]}\n',encoding='utf8')
        (a.candidate_out/'src'/'client.tsx').write_text('''declare const preact:any;\nconst h=preact.h;\nfunction cookie(name:string):string{const m=document.cookie.match(new RegExp("(^| )"+name+"=([^;]+)"));return m?m[2]:"";}\nasync function load():Promise<void>{const r=await fetch("/ui/tasks");if(r.status===401){location.href="/";return;}const d=await r.json();const items=d.items||[];const list=document.getElementById("tasks");if(list)preact.render(h(preact.Fragment,null,...items.map((t:any)=>h("li",{key:t.id},h("span",null,t.title)))),list);const empty=document.getElementById("empty");if(empty)empty.style.display=items.length?"none":"";}\nconst button=document.getElementById("create");if(button)button.addEventListener("click",async()=>{const field=document.getElementById("title") as HTMLInputElement;const error=document.getElementById("error");const r=await fetch("/ui/tasks",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":cookie("csrf")},body:JSON.stringify({title:field.value})});if(r.ok){field.value="";if(error)error.textContent="";await load();}else if(error)error.textContent="Unable to create task";});load();\n''',encoding='utf8')
        html=a.candidate_out/'html.py'; data=html.read_text(encoding='utf8')
        data=data.replace('<script src="/ui/list.js"></script>\n<script src="/ui/create.js"></script>\n<script src="/ui/delete.js"></script>','<script src="/ui/preact.umd.js"></script>\n<script src="/ui/dist/client.js"></script>')
        html.write_text(data,encoding='utf8')
        mainpy=a.candidate_out/'main.py'; data=mainpy.read_text(encoding='utf8')
        data=data.replace('"/ui/delete.js")','"/ui/delete.js", "/ui/preact.umd.js", "/ui/dist/client.js")')
        mainpy.write_text(data,encoding='utf8')
        (a.candidate_out/'integration-label.json').write_text('{"model_generated_backend":true,"authored_framework_glue":true,"framework":"Preact 10.26.4","compiler":"TypeScript 5.9.3"}\n',encoding='utf8')
        # Include only exact framework artifacts in the environment (not unrelated cache entries).
        records=[json.loads((CACHE/(digest+'.json')).read_text(encoding='utf8')) for digest in (PREACT,TYPESCRIPT)]
        packages=[{'name':'preact','version':'10.26.4','source':'npmjs','artifact_sha256':PREACT,'license':'MIT','dependencies':{}},{'name':'typescript','version':'5.9.3','source':'npmjs','artifact_sha256':TYPESCRIPT,'license':'Apache-2.0','dependencies':{}}]
        lock={'schema':1,'ecosystem':'typescript','toolchain':{'name':'node','version':'operator-image','container_image':a.node_image},'packages':packages}
        env=deps.environment_manifest(lock,sources={'npmjs':'https://registry.npmjs.org'},artifacts=records,cache=CACHE)
        cm=file_manifest(a.candidate_out,'Retained generated web backend with authored Preact integration glue')
        build=toolchain.run(env,cm,CACHE,a.engine,a.build_out,candidate_root=a.candidate_out,image=a.node_image,build_argv=['/bin/sh','-c','node node_modules/typescript/bin/tsc -p tsconfig.json'],repeat=True,timeout=600)
        if build['status']!='PASS': raise ValueError('offline framework build was not deterministic PASS')
        for rel in ('ui/dist/client.js','preact.umd.js'):
            source=a.build_out/rel
            if not source.is_file(): raise ValueError('offline compiler output missing '+rel)
            target=a.candidate_out/rel; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(source,target)
        shutil.copyfile(a.candidate_out/'preact.umd.js',a.candidate_out/'ui'/'preact.umd.js')
        # Browser flow authenticates by accessible keyboard flow, creates against shared backend DB, reloads and verifies persistence.
        browser={'schema':1,'start_path':'/','steps':[{'action':'assert_role','role':'heading','name':'Sign in'},{'action':'assert_role','role':'textbox','name':'Bearer token'},{'action':'focus','selector':'#token'},{'action':'assert_focused','selector':'#token'},{'action':'fill','selector':'#token','value':'{$secret_file:ADMIN}'},{'action':'click','selector':'#login button'},{'action':'wait','ms':700},{'action':'assert_role','role':'heading','name':'Tasks'},{'action':'assert_label','name':'Task title'},{'action':'assert_empty','selector':'#tasks'},{'action':'fill','selector':'#title','value':'Framework persisted task'},{'action':'click','selector':'#create'},{'action':'wait','ms':700},{'action':'assert_list','selector':'#tasks','count':1},{'action':'assert_text','selector':'#tasks','text':'Framework persisted task'},{'action':'reload'},{'action':'wait','ms':500},{'action':'assert_list','selector':'#tasks','count':1},{'action':'assert_text','selector':'#tasks','text':'Framework persisted task'},{'action':'screenshot','name':'framework-generated-backend'}]}
        secrets=BACKEND.parent/'private'
        mounts=[{'source':str(secrets/name),'target':'/run/autoforge/secrets/'+name,'read_only':True,'name':name,'sha256':sha((secrets/name).read_bytes())} for name in ('ADMIN','READER')]
        sm=file_manifest(a.candidate_out,'Generated backend plus authored Preact runtime integration; backend files retained byte-identically')
        contract={'schema':1,'health_path':'/health','scenarios':[{'name':'backend-health-and-auth-denials','requests':[{'method':'GET','path':'/health','body':None,'expected':{'status':200,'body':{'ok':True}}},{'method':'GET','path':'/api/tasks','body':None,'expected':{'status':401,'body':{'error':'unauthorized'}}},{'method':'POST','path':'/api/tasks','body':{'title':'reader forbidden'},'headers':{'Authorization':'Bearer {$secret_file:READER}'},'expected':{'status':403,'body':{'error':'forbidden'}}}]}]}
        service=application_service.run(sm,contract,a.engine,a.python_image,['python','-I','-c','import runpy,sys;sys.path.insert(0,"/candidate");runpy.run_path("/candidate/main.py",run_name="__main__")'],timeout=60,browser_contract=browser,browser_output=a.service_out,browser_authorized=a.authorize_browser,secret_mounts=mounts)
        result={'schema':1,'status':'PASS' if service['test_pass'] and build['status']=='PASS' else 'FAIL','model_generated_backend':True,'authored_framework_glue':True,'backend_original_manifest_sha256':json.loads((BACKEND/'manifest.json').read_text(encoding='utf8'))['candidate_sha256'],'integration_candidate_sha256':sm['candidate_sha256'],'dependency_lock_sha256':env['lock_sha256'],'cache_sha256':env['cache_sha256'],'build':build,'service':service,'cross_engine_claim':False,'host_candidate_execution':False}
        a.service_out.mkdir(parents=True,exist_ok=True); (a.service_out/'integration-evidence.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n',encoding='utf8')
        print(json.dumps(result,sort_keys=True)); return 0 if result['status']=='PASS' else 2
    except (OSError,ValueError,KeyError,deps.DependencyRefused,toolchain.ToolchainRefused,application_service.ServiceFailure) as exc:
        print(json.dumps({'status':'BLOCKED','reason':str(exc),'host_candidate_execution':False},sort_keys=True));return 2
if __name__=='__main__':raise SystemExit(main())
