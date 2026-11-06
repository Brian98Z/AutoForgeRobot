"""Acquire and run a real Preact + TypeScript frontend build in an offline exact-image container.

Registry metadata and tarballs are fetched only after --authorized; tarballs flow through
application_dependencies.acquire_https. This proves compiler build and Node-side framework rendering,
not browser rendering. No npm install or candidate code runs on the host.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, base64, hashlib, json, urllib.error, urllib.parse, urllib.request
from pathlib import Path
import application_dependencies as deps
import application_toolchain as toolchain

REGISTRY='https://registry.npmjs.org'
SOURCE='npmjs'
PACKAGES=(('preact','10.26.4'),('preact-render-to-string','6.3.1'),('typescript','5.9.3'))

class FrontendRefused(ValueError): pass

def _metadata(name:str,version:str)->dict:
    url=f'{REGISTRY}/{urllib.parse.quote(name,safe="@")}/{urllib.parse.quote(version,safe="")}'
    request=urllib.request.Request(url,headers={'User-Agent':'AutoForge-frontend-acceptance/1','Accept':'application/json','Accept-Encoding':'identity'},method='GET')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),deps._NoRedirect())
    try:
        with opener.open(request,timeout=30) as response:
            if response.status!=200 or response.geturl()!=url:raise FrontendRefused('npm registry metadata response was not direct HTTP 200')
            raw=response.read(4*1024*1024+1)
    except (OSError,urllib.error.URLError,urllib.error.HTTPError,TimeoutError) as exc:raise FrontendRefused(f'npm metadata retrieval failed for {name}@{version}') from exc
    if len(raw)>4*1024*1024:raise FrontendRefused('npm metadata response exceeds 4 MiB')
    try:doc=json.loads(raw)
    except (UnicodeError,ValueError) as exc:raise FrontendRefused('npm metadata is not JSON') from exc
    if not isinstance(doc,dict) or doc.get('name')!=name or doc.get('version')!=version:raise FrontendRefused('npm metadata identity mismatch')
    dist=doc.get('dist')
    if not isinstance(dist,dict) or not isinstance(dist.get('tarball'),str) or not isinstance(dist.get('integrity'),str):raise FrontendRefused('npm metadata lacks tarball and SRI integrity')
    parsed=urllib.parse.urlsplit(dist['tarball'])
    if parsed.scheme!='https' or parsed.netloc.casefold()!='registry.npmjs.org' or parsed.username or parsed.password or parsed.fragment:raise FrontendRefused('npm tarball URL is outside exact registry origin')
    sri=[]
    for item in dist['integrity'].split():
        if item.startswith('sha512-'):
            try:sri.append(base64.b64decode(item[7:],validate=True))
            except ValueError:raise FrontendRefused('malformed npm SHA-512 integrity')
    if not sri or any(len(value)!=64 for value in sri):raise FrontendRefused('npm registry SHA-512 integrity unavailable')
    license_value=doc.get('license')
    if isinstance(license_value,dict):license_value=license_value.get('type')
    if not isinstance(license_value,str) or not license_value.strip():raise FrontendRefused(f'npm license identifier unavailable for {name}@{version}')
    return {'metadata':doc,'metadata_sha256':hashlib.sha256(raw).hexdigest(),'tarball_url':dist['tarball'],'sha512':sri,'license':license_value}

def _version_key(value:str)->tuple:
    import re
    match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?',value)
    if not match:return (-1,-1,-1,1,())
    major,minor,patch,pre=match.groups()
    identifiers=tuple((0,int(x)) if x.isdigit() else (1,x) for x in (pre or '').split('.') if x)
    return int(major),int(minor),int(patch),0 if pre else 1,identifiers

def _versions(name:str)->list[str]:
    url=f'{REGISTRY}/{urllib.parse.quote(name,safe="@")}'
    request=urllib.request.Request(url,headers={'User-Agent':'AutoForge-frontend-acceptance/1','Accept':'application/json','Accept-Encoding':'identity'},method='GET')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),deps._NoRedirect())
    try:
        with opener.open(request,timeout=30) as response:
            if response.status!=200 or response.geturl()!=url:raise FrontendRefused('npm versions response was not direct HTTP 200')
            raw=response.read(16*1024*1024+1)
    except (OSError,urllib.error.URLError,urllib.error.HTTPError,TimeoutError) as exc:raise FrontendRefused(f'npm version listing failed for {name}') from exc
    if len(raw)>16*1024*1024:raise FrontendRefused('npm version listing exceeds 16 MiB')
    try:doc=json.loads(raw)
    except (UnicodeError,ValueError) as exc:raise FrontendRefused('npm version listing is not JSON') from exc
    if not isinstance(doc,dict) or not isinstance(doc.get('versions'),dict):raise FrontendRefused('npm version listing malformed')
    return [v for v,info in doc['versions'].items() if isinstance(info,dict) and not info.get('deprecated') and '-' not in v and _version_key(v)[0]>=0]


def _candidate(root:Path)->dict:
    files={
      'package.json':b'{"name":"autoforge-preact-smoke","version":"1.0.0","private":true,"dependencies":{"preact":"10.26.4","preact-render-to-string":"6.3.1"},"devDependencies":{"typescript":"5.9.3"},"scripts":{"build":"tsc -p tsconfig.json"}}\n',
      'tsconfig.json':b'{"compilerOptions":{"target":"ES2020","module":"commonjs","moduleResolution":"node","jsx":"react","jsxFactory":"h","outDir":"dist","rootDir":"src","strict":true,"skipLibCheck":true},"include":["src/**/*.ts","src/**/*.tsx"]}\n',
      'src/main.tsx':b'import { h } from "preact";\nimport { renderToString } from "preact-render-to-string";\nfunction App() { return <main data-framework="preact"><h1>Task list</h1></main>; }\nconst html = renderToString(<App/>);\nif (!html.includes("Task list")) throw new Error("server render failed");\nconsole.log(html);\n',
      'src/client.ts':b'declare const preact:any;\nconst h=preact.h;let items:string[]=[];function App(){return h("main",{"data-framework":"preact"},h("h1",null,"Task list"),h("button",{id:"add",onClick:function(){items.push("Task "+(items.length+1));preact.render(App(),document.getElementById("app"));}},"Add task"),h("ul",null,items.map(function(x){return h("li",null,x)})));}preact.render(App(),document.getElementById("app"));\n',
      'index.html':b'<!doctype html><html><head><meta charset="utf-8"><title>Preact task smoke</title></head><body><div id="app"></div><script src="./preact.umd.js"></script><script src="./dist/client.js"></script></body></html>\n',
      'browser-runtime.json':b'{"package":"preact","version":"10.26.4","asset":"dist/preact.umd.js","source":"verified-lock-tarball"}\n'
    }
    rows=[]
    for rel,data in sorted(files.items()):
        path=root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        rows.append({'path':rel,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    manifest={'schema':1,'goal':'build and server-render generated Preact frontend','references':['registry.npmjs.org exact-version metadata'],'files':rows}
    manifest['candidate_sha256']=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    return manifest

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--authorized',action='store_true',help='explicitly authorize exact-version npm registry metadata and tarball acquisition')
    ap.add_argument('--engine',choices=('docker','podman'),default='docker');ap.add_argument('--node-image',required=True,help='exact locally loaded immutable sha256 image ID')
    ap.add_argument('--cache',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--candidate-out',type=Path,required=True,help='absent directory for the generated, hash-manifested frontend fixture')
    a=ap.parse_args()
    try:
        if not a.authorized:raise FrontendRefused('explicit --authorized required before contacting npm registry')
        if a.output.exists() or a.candidate_out.exists():raise FrontendRefused('output and candidate directories must be absent')
        a.cache.mkdir(parents=True,exist_ok=True)
        metadata={}; queue=list(PACKAGES); package_specs={name:version for name,version in PACKAGES}; edges_by_name={}
        while queue:
            name,version=queue.pop(0)
            if name in metadata:continue
            info=_metadata(name,version); metadata[name]=info; package=info['metadata']; edges={}
            for field in ('dependencies','optionalDependencies','peerDependencies'):
                declarations=package.get(field,{})
                if not isinstance(declarations,dict):raise FrontendRefused(f'malformed npm {field}: {name}@{version}')
                for child,requirement in declarations.items():
                    if not isinstance(child,str) or not isinstance(requirement,str):raise FrontendRefused('malformed npm dependency declaration')
                    child_version=package_specs.get(child)
                    if child_version is None:
                        range_versions=sorted((v for v in _versions(child) if toolchain._npm_matches_any(v,requirement)),key=_version_key)
                        if not range_versions:raise FrontendRefused(f'no published exact version satisfies {child}@{requirement}')
                        if len(package_specs)>=256:raise FrontendRefused('npm dependency graph exceeds 256 package safety bound')
                        child_version=range_versions[-1];package_specs[child]=child_version;queue.append((child,child_version))
                    if not toolchain._npm_matches_any(child_version,requirement):raise FrontendRefused(f'pinned graph version does not satisfy registry edge {name}->{child} {requirement}')
                    edges[child]=child_version
            edges_by_name[name]=edges
        records=[]; lock_packages=[]
        for name,version in sorted(package_specs.items()):
            info=metadata[name]; sha256=hashlib.sha256()
            request=urllib.request.Request(info['tarball_url'],headers={'User-Agent':'AutoForge-artifact-acquirer/1','Accept-Encoding':'identity'},method='GET')
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),deps._NoRedirect())
            try:
                with opener.open(request,timeout=30) as response:
                    if response.status!=200 or response.geturl()!=info['tarball_url']:raise FrontendRefused('npm tarball response was not direct HTTP 200')
                    data=response.read(256*1024*1024+1)
            except (OSError,urllib.error.URLError,urllib.error.HTTPError,TimeoutError) as exc:raise FrontendRefused(f'npm tarball fetch failed for {name}@{version}') from exc
            if len(data)>256*1024*1024:raise FrontendRefused('npm tarball exceeds 256 MiB')
            actual=hashlib.sha256(data).hexdigest()
            if hashlib.sha512(data).digest() not in info['sha512']:raise FrontendRefused(f'npm SHA-512 SRI mismatch: {name}@{version}')
            record=deps.acquire_https(info['tarball_url'],a.cache,actual,source_id=SOURCE,sources={SOURCE:REGISTRY},license_record=info['license'],package_name=name,package_version=version,authorized=True)
            records.append(record)
            lock_packages.append({'name':name,'version':version,'source':SOURCE,'artifact_sha256':actual,'license':info['license'],'dependencies':edges_by_name[name]})
        a.candidate_out.mkdir(parents=True);candidate=_candidate(a.candidate_out)
        lock={'schema':1,'ecosystem':'typescript','toolchain':{'name':'node','version':'operator-image','container_image':a.node_image},'packages':lock_packages}
        env=deps.environment_manifest(lock,sources={SOURCE:REGISTRY},artifacts=records,cache=a.cache)
        result=toolchain.run(env,candidate,a.cache,a.engine,a.output,candidate_root=a.candidate_out,image=a.node_image,build_argv=['/bin/sh','-c','node node_modules/typescript/bin/tsc -p tsconfig.json && node dist/main.js'],repeat=True,timeout=600)
        for run in result['runs']:
            paths={row['path'] for row in run['inventory']}
            if not {'dist/main.js','dist/client.js','index.html','preact.umd.js','browser-runtime.json'}<=paths or 'Task list' not in run['observed_versions']:raise FrontendRefused('isolated frontend build/runtime output lacked browser assets or rendered framework markup')
        report={'status':result['status'],'framework':'Preact','compiler':'TypeScript','candidate_sha256':result['candidate_sha256'],'lock_sha256':env['lock_sha256'],'cache_sha256':env['cache_sha256'],'metadata':[{'name':name,'version':package_specs[name],'metadata_sha256':metadata[name]['metadata_sha256'],'license':metadata[name]['license']} for name in sorted(metadata)],'artifacts':[{'name':r['name'],'version':r['version'],'sha256':r['artifact_sha256'],'size':r['size'],'license':r['license']} for r in records],'runs':result['runs'],'network':'candidate execution disabled','host_candidate_execution':False,'browser_asset_sha256':next(row['sha256'] for row in result['runs'][0]['inventory'] if row['path']=='preact.umd.js'),'browser_interaction_contract':{'initial':'h1 text Task list; button #add; zero li','action':'click #add once','expected_after':'one li text Task 1','second_action':'click #add again','expected_after_second':'two li text Task 1, Task 2'},'browser_evidence':'built local files emitted; browser interaction remains independently verifiable'}
        print(json.dumps(report,sort_keys=True));return 0 if result['status']=='PASS' else 2
    except (OSError,ValueError,KeyError,FrontendRefused,deps.DependencyRefused,toolchain.ToolchainRefused) as exc:
        print(json.dumps({'status':'BLOCKED','reason':str(exc),'host_candidate_execution':False},sort_keys=True));return 2
if __name__=='__main__':raise SystemExit(main())
