"""Run reproducible minimal Python, Node, TypeScript and Java builds in operator-prepared exact local images.
Requires locally loaded Linux image IDs and an artifact-cache manifest. Never pulls images, acquires artifacts,
or starts/stops the shared engine. Containers created by application_toolchain use disposable autoforge-toolchain-
identifiers and are forcibly removed in the runner's finally cleanup; image IDs are never removed.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, hashlib, json, shutil, tempfile
from pathlib import Path
import application_dependencies as deps
import application_toolchain as toolchain
def _candidate(root:Path, files:dict[str,bytes])->dict:
    rows=[]
    for rel,data in sorted(files.items()):
        p=root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        rows.append({'path':rel,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    canonical={'schema':1,'goal':'live toolchain acceptance','references':[],'files':rows}
    canonical['candidate_sha256']=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    return canonical
def _run_one(base:Path,engine:str,eco:str,image:str,cache:Path,records:list,packages:list,files:dict,argv:list,repeat:bool)->dict:
    src=base/('candidate-'+eco);src.mkdir();candidate=_candidate(src,files)
    lock={'schema':1,'ecosystem':eco,'toolchain':{'name':{'python':'python','node':'node','typescript':'node','java':'java'}[eco],'version':'operator-image','container_image':image},'packages':packages}
    sources={r['source']:('https://registry.npmjs.org' if eco in {'node','typescript'} else 'https://repo.maven.apache.org') for r in records}
    manifest=deps.environment_manifest(lock,sources=sources,artifacts=records,cache=cache)
    out=base/('output-'+eco)
    result=toolchain.run(manifest,candidate,cache,engine,out,candidate_root=src,image=image,build_argv=argv,repeat=repeat)
    if result['status']!='PASS' or not result['runs']:raise AssertionError(f'{eco} build failed: {result}')
    expected={'python':'python-ok','node':'node-ok','typescript':'typescript-ok','java':'java-ok'}[eco]
    for run in result['runs']:
        paths={item['path'] for item in run['inventory']}
        if eco=='python' and 'main.py' not in paths:raise AssertionError('Python smoke output omitted source')
        if eco=='node' and 'index.js' not in paths:raise AssertionError('Node smoke output omitted source')
        if eco=='typescript' and not {'dist/main.js','src/main.ts'}<=paths:raise AssertionError('TypeScript smoke output omitted compiler output/source')
        if eco=='java' and not {'Main.java','Main.class'}<=paths:raise AssertionError('Java smoke output omitted class/source')
        if expected not in run['observed_versions']:raise AssertionError(f'{eco} fixture did not execute expected main output')
    shutil.rmtree(out)
    return {'ecosystem':eco,'image_id':image,'status':'PASS','versions':[r['observed_versions'] for r in result['runs']],'deterministic':result['deterministic']}
def main()->int:
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--engine',choices=('docker','podman'),default='docker');ap.add_argument('--python-image',required=True);ap.add_argument('--node-image',required=True);ap.add_argument('--java-image',required=True);ap.add_argument('--cache',type=Path,required=True);ap.add_argument('--artifacts',type=Path,required=True,help='Cache records must include TypeScript 5.9.3: registry.npmjs.org/typescript/-/typescript-5.9.3.tgz sha256 computed from bytes (official registry integrity sha512-jl1vZzPDinLr9eUt3J/t7V6FgNEw9QjvBPdysz9KfQDD41fQrC2Y4vKQdiaUpFT4bXlb1RHhLpp8wtm6M5TgSw==). This is compiler-only smoke; a real framework smoke additionally requires every exact registry tgz, SHA-256, license record, and dependency edge for that framework lock.');ap.add_argument('--repeat',action='store_true');a=ap.parse_args()
    images={'python':a.python_image,'node':a.node_image,'typescript':a.node_image,'java':a.java_image}
    try:
        records=json.loads(a.artifacts.read_text(encoding='utf8')); verified=deps.verify_cache(a.cache,records,expected_sources={r['source'] for r in records})
        if verified['artifacts']!=sorted(({k:r[k] for k in ('name','version','source','artifact_sha256','license','size')} for r in records),key=lambda r:(r['name'].casefold(),r['artifact_sha256'])):raise ValueError('artifact manifest/cache discrepancy')
        with tempfile.TemporaryDirectory(prefix='autoforge-live-check-') as td:
            base=Path(td);out=[]
            cases=[('python',{}, {'main.py':b'print("python-ok")\n'},['python','main.py']),('node',{}, {'package.json':b'{"name":"autoforge-smoke","version":"1.0.0","private":true}\n','index.js':b'console.log("node-ok")\n'},['node','index.js']),('typescript',{}, {'package.json':b'{"name":"autoforge-smoke-ts","version":"1.0.0","private":true,"devDependencies":{"typescript":"^5.9.0"},"scripts":{"build":"tsc -p tsconfig.json"}}\n','tsconfig.json':b'{"compilerOptions":{"target":"ES2020","module":"commonjs","outDir":"dist","strict":true},"include":["src/**/*.ts"]}\n','src/main.ts':b'console.log("typescript-ok")\n'},['/bin/sh','-c','npm run build --offline --ignore-scripts --no-audit --no-fund && node dist/main.js']),('java',{}, {'Main.java':b'public class Main { public static void main(String[] args) { System.out.println("java-ok"); } }\n'},['javac','Main.java'])]
            for eco,_,files,argv in cases:
                selected=[r for r in records if eco=='typescript' and r['name'].casefold()=='typescript']
                packages=[{k:r[k] for k in ('name','version','source','artifact_sha256','license')} for r in selected]
                if eco=='typescript':
                    expected='10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3'
                    if len(selected)!=1 or selected[0]['version']!='5.9.3' or selected[0]['artifact_sha256']!=expected:raise ValueError('TypeScript acceptance requires official TypeScript 5.9.3 artifact SHA-256')
                    packages[0]['dependencies']={}
                out.append(_run_one(base,a.engine,eco,images[eco],a.cache,selected,packages,files,argv,a.repeat))
            pyroot=base/'negative-python';pyroot.mkdir();pycandidate=_candidate(pyroot,{'main.py':b'import autoforge_intentionally_missing_dependency_987654321\n'})
            pymanifest={'schema':1,'ecosystem':'python','toolchain':{'name':'python','version':'3.11','container_image':a.python_image},'packages':[]}
            pyenv=deps.environment_manifest(pymanifest,sources={},artifacts=[],cache=a.cache)
            pyout=base/'negative-output'
            try:toolchain.run(pyenv,pycandidate,a.cache,a.engine,pyout,candidate_root=pyroot,build_argv=['python','main.py'],image=a.python_image)
            except toolchain.ToolchainRefused:pass
            else:raise AssertionError('missing Python dependency unexpectedly resolved offline')
            if pyout.exists():raise AssertionError('failed offline build left outputs')
            conflictroot=base/'negative-conflict';conflictroot.mkdir()
            conflictcandidate=_candidate(conflictroot,{'package.json':b'{"name":"conflict","version":"1.0.0","dependencies":{"typescript":"0.0.0"}}\n'})
            ts=next(r for r in records if r['name'].casefold()=='typescript')
            env=deps.environment_manifest({'schema':1,'ecosystem':'typescript','toolchain':{'name':'node','version':'22','container_image':a.node_image},'packages':[{'name':ts['name'],'version':ts['version'],'source':ts['source'],'artifact_sha256':ts['artifact_sha256'],'license':ts['license'],'dependencies':{}}]},sources={ts['source']:'https://registry.npmjs.org'},artifacts=[ts],cache=a.cache)
            blocked=toolchain.check(env,conflictroot,conflictcandidate,a.cache,a.engine,a.node_image)
            if blocked['status']!='BLOCKED':raise AssertionError('conflicting root dependency version accepted')
            wrong={**ts,'artifact_sha256':'0'*64}
            try:deps.verify_cache(a.cache,[wrong],expected_sources={ts['source']})
            except deps.DependencyRefused:pass
            else:raise AssertionError('wrong cached artifact digest accepted')
            print(json.dumps({'status':'PASS','runs':out,'artifact_cache_sha256':verified['cache_sha256'],'network':'disabled','host_execution':'not used','cleanup':'all ephemeral toolchain containers are removed by application_toolchain; no images removed'},sort_keys=True));return 0
    except (OSError,ValueError,KeyError,AssertionError,deps.DependencyRefused,toolchain.ToolchainRefused) as exc:
        print(json.dumps({'status':'BLOCKED','reason':str(exc),'image_ids_preserved':True},sort_keys=True));return 2
if __name__=='__main__':raise SystemExit(main())
