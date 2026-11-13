#!/usr/bin/env python3
"""Run and independently exercise a candidate HTTP service in an isolated Linux container."""
from __future__ import annotations
import argparse, hashlib, http.client, json, os, re, signal, socket, subprocess, sys, threading, time, uuid
from pathlib import Path
from typing import Any
import application_builder
import container_env

MAX_LOG=262144
MAX_BODY=1048576

class ServiceFailure(ValueError): pass

def _run(argv: list[str], timeout: int=15) -> bytes:
    try: p=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False,env=container_env.engine_env(),close_fds=True)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e: raise ServiceFailure(f'container command unavailable or timed out: {argv[0]}') from e
    if p.returncode: raise ServiceFailure(f'container command failed ({argv[0]}): {p.stderr[:2048].decode("utf-8","replace")}')
    if len(p.stdout)>65536: raise ServiceFailure('container response exceeded limit')
    return p.stdout
def _bounded_logs(engine:str,name:str)->str:
    try:
        proc=subprocess.Popen([engine,'logs','--tail','500',name],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=container_env.engine_env(),close_fds=True)
    except OSError: return ''
    data=bytearray(); overflow=False
    def drain()->None:
        nonlocal overflow
        assert proc.stdout is not None
        while True:
            chunk=proc.stdout.read(4096)
            if not chunk: break
            if len(data)<MAX_LOG: data.extend(chunk[:MAX_LOG-len(data)])
            if len(data)>=MAX_LOG: overflow=True
    import threading
    worker=threading.Thread(target=drain,daemon=True); worker.start()
    try: proc.wait(timeout=5)
    except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=2); overflow=True
    worker.join(timeout=2)
    if worker.is_alive(): overflow=True
    text=data.decode('utf-8','replace')
    return text+('\n[logs truncated]' if overflow else '')

def preflight(engine: str, image: str) -> dict[str,str]:
    return application_builder.preflight(engine,image)

def _json(data: bytes) -> Any:
    if len(data)>MAX_BODY: raise ServiceFailure('HTTP body exceeds limit')
    try: return json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError,json.JSONDecodeError) as e: raise ServiceFailure('service returned invalid JSON') from e

def _contract(raw: Any) -> list[dict[str,Any]]:
    if not isinstance(raw,dict) or set(raw)!={'schema','health_path','scenarios'} or type(raw['schema']) is not int or raw['schema']!=1: raise ServiceFailure('contract must have schema 1, health_path, scenarios')
    hp=raw['health_path']; scenarios=raw['scenarios']
    if not isinstance(hp,str) or not hp.startswith('/') or any(c in hp for c in '\r\n') or len(hp)>2048 or not isinstance(scenarios,list) or not scenarios or len(scenarios)>8: raise ServiceFailure('invalid health path or scenarios')
    total=0; names=set()
    for s in scenarios:
        if not isinstance(s,dict) or set(s)-{'name','requests','restart_after'} or not {'name','requests'}<=set(s) or not isinstance(s['name'],str) or not s['name'] or s['name'] in names or not isinstance(s['requests'],list) or not s['requests'] or len(s['requests'])>32 or ('restart_after' in s and (type(s['restart_after']) is not int or not 0<=s['restart_after']<len(s['requests']))): raise ServiceFailure('invalid scenario')
        names.add(s['name'])
        for r in s['requests']:
            if not isinstance(r,dict) or not {'method','path','body','expected'}<=set(r) or set(r)-{'method','path','body','expected','headers'} or not isinstance(r['method'],str) or not re.fullmatch(r'[A-Z]{3,8}',r['method']) or not isinstance(r['path'],str) or not r['path'].startswith('/') or any(c in r['path'] for c in '\r\n') or len(r['path'])>2048 or not isinstance(r['expected'],dict) or set(r['expected'])!={'status','body'} or type(r['expected']['status']) is not int or not 100<=r['expected']['status']<=599: raise ServiceFailure('invalid HTTP request/expectation')
            headers=r.get('headers',{})
            if not isinstance(headers,dict) or len(headers)>32 or any(not isinstance(k,str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}",k) or not isinstance(v,str) or len(v)>4096 or any(c in v for c in '\r\n') for k,v in headers.items()): raise ServiceFailure('invalid HTTP headers')
            total+=len(json.dumps(r,ensure_ascii=False).encode())
    if total>MAX_BODY: raise ServiceFailure('contract exceeds byte limit')
    return scenarios

def _request(port:int, method:str, path:str, body:Any, headers:dict[str,str]|None=None, cookie:str|None=None) -> dict[str,Any]:
    payload=None if body is None else json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()
    if payload is not None and len(payload)>MAX_BODY: raise ServiceFailure('request body exceeds limit')
    request_headers={'Content-Type':'application/json'} if payload is not None else {}
    request_headers.update(headers or {})
    if cookie is not None: request_headers['Cookie']=cookie
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
    try:
        try:
            c.request(method,path,body=payload,headers=request_headers)
            r=c.getresponse(); data=r.read(MAX_BODY+1)
            if len(data)>MAX_BODY: raise ServiceFailure('response body exceeds limit')
            result={'status':r.status,'body':_json(data) if data else None}
            if r.getheader('Set-Cookie'): result['_set_cookie']=r.getheader('Set-Cookie')
            return result
        except ServiceFailure: raise
        except (OSError,http.client.HTTPException,ValueError) as exc: raise ServiceFailure('HTTP transport/protocol failure') from None
    finally: c.close()


def run(manifest: Any, contract_raw: Any, engine: str, image: str, command: list[str], timeout: int=30, *, browser_contract:dict[str,Any]|None=None, browser_output:Path|None=None, browser_authorized:bool=False, browser_engine:str='chromium', measure_resources:bool=False, secret_mounts:list[dict[str,Any]]|None=None) -> dict[str,Any]:
    if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x or '\x00' in x for x in command): raise ServiceFailure('service command must be a nonempty argv array')
    if browser_contract is not None and (not browser_authorized or browser_output is None): raise ServiceFailure('browser contract requires explicit browser_authorized=True and browser_output')
    if secret_mounts is not None and (not isinstance(secret_mounts,list) or len(secret_mounts)>32): raise ServiceFailure('secret mounts must be a bounded list')
    secret_args=[]
    for item in secret_mounts or []:
        if not isinstance(item,dict) or set(item)!={'source','target','read_only','name','sha256'} or item['read_only'] is not True or not isinstance(item['source'],str) or not isinstance(item['target'],str) or not item['target'].startswith('/run/autoforge/secrets/') or not isinstance(item['name'],str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',item['name']) or not isinstance(item['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',item['sha256']): raise ServiceFailure('invalid private secret mount descriptor')
        source=Path(item['source'])
        if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']: raise ServiceFailure('private secret file missing or changed')
        staged_target='/run/autoforge-staged/'+item['name']
        secret_args.extend(['--mount',f"type=bind,src={source.resolve(strict=True)},dst={staged_target},readonly"])
    secret_values={}
    for item in secret_mounts or []:
        secret_values[item['name']]=Path(item['source']).read_text(encoding='utf-8')
    def resolve(value):
        if isinstance(value,str):
            return re.sub(r'\{\$secret_file:([A-Z][A-Z0-9_]{0,63})\}',lambda m: secret_values[m.group(1)] if m.group(1) in secret_values else (_ for _ in ()).throw(ServiceFailure('contract references unmounted private secret')),value)
        if isinstance(value,list): return [resolve(x) for x in value]
        if isinstance(value,dict): return {k:resolve(v) for k,v in value.items()}
        return value
    def resolve_scenario(value, values):
        if isinstance(value, str):
            return re.sub(r'\{\$runtime:([A-Z][A-Z0-9_]{0,63})\}', lambda match: values[match.group(1)], value)
        if isinstance(value, list): return [resolve_scenario(item, values) for item in value]
        if isinstance(value, dict): return {key: resolve_scenario(item, values) for key, item in value.items()}
        return value
    referenced=set(re.findall(r'\{\$secret_file:([A-Z][A-Z0-9_]{0,63})\}',json.dumps(contract_raw,ensure_ascii=False)))
    if referenced-set(secret_values): raise ServiceFailure('contract references unmounted private secret')
    contract_raw=resolve(contract_raw)
    def scrub(value):
        if isinstance(value,str):
            for secret in secret_values.values():
                if secret: value=value.replace(secret,'[REDACTED]')
            return value
        if isinstance(value,list): return [scrub(x) for x in value]
        if isinstance(value,dict): return {k:scrub(v) for k,v in value.items()}
        return value
    root,digest=application_builder._manifest(manifest,require_entrypoint=False)
    if not any(row['path']=='main.py' for row in manifest['files']):
        return {'schema':1,'candidate_sha256':digest,'build_pass':False,'test_pass':False,
                'observations':['HTTP entrypoint main.py is absent; create it before validation.'],
                'limitations':['Candidate integrity verified; no container or browser was dispatched.'],
                'http_results':[],'browser':None,'scenarios':[],'logs':''}
    scenarios=_contract(contract_raw); info=preflight(engine,image)
    name='autoforge-svc-'+uuid.uuid4().hex
    volume='autoforge-svc-vol-'+uuid.uuid4().hex
    volume_created=False
    from container_http_tunnel import Tunnel
    tunnel=None
    try:
        _run([engine,'volume','create',volume]); volume_created=True
        mounts=[(item['name'],'/run/autoforge-staged/'+item['name']) for item in (secret_mounts or [])]
        if mounts:
            bootstrap="import os,sys; mounts=__MOUNTS__; dst='/run/autoforge/secrets'; os.makedirs(dst,mode=0o700,exist_ok=True);\nif os.stat('/state').st_uid==0:\n os.chmod('/state',0o700); os.chown('/state',65534,65534)\nfor name,src in mounts:\n data=open(src,'rb').read(); fd=os.open(dst+'/'+name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400); os.write(fd,data); os.close(fd); os.chown(dst+'/'+name,65534,65534)\nos.chmod(dst,0o500); os.chown(dst,65534,65534); os.setgid(65534); os.setuid(65534); os.execvp(sys.argv[1],sys.argv[1:])".replace('__MOUNTS__',repr(mounts))
            entrypoint=['python','-I','-c',bootstrap,*command]
        else: entrypoint=command
        argv=[engine,'run','-d','--init','--name',name,'--network=none','--read-only','--user=0:0' if mounts else '--user=65534:65534','--cap-drop=ALL',*( ['--cap-add=CHOWN','--cap-add=SETUID','--cap-add=SETGID','--cap-add=DAC_READ_SEARCH'] if mounts else [] ),'--security-opt=no-new-privileges','--pids-limit=64','--memory=256m','--cpus=1','--mount',f'type=bind,src={root},dst=/candidate,readonly',*secret_args,'--mount',f'type=volume,src={volume},dst=/state','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777','--tmpfs','/run/autoforge:rw,noexec,nosuid,nodev,size=1m,mode=1777','--workdir','/candidate','--entrypoint',entrypoint[0],image,*entrypoint[1:]]
        cid=_run(argv).decode().strip()
        tunnel=Tunnel(engine,cid,container_env.engine_env())
        port=tunnel.port; deadline=time.monotonic()+timeout; ready=False
        while time.monotonic()<deadline:
            if application_builder.STOP_REQUESTED: raise ServiceFailure('service run stopped')
            try:
                if _request(port,'GET',contract_raw['health_path'],None)['status']==200: ready=True; break
            except (OSError,http.client.HTTPException,ServiceFailure): pass
            state=_run([engine,'inspect','--format','{{.State.Running}}',cid]).decode().strip()
            if state!='true': break
            time.sleep(.2)
        if not ready:
            startup_logs=_bounded_logs(engine,name)
            runtime_contract='Candidate source is read-only at /candidate; persistent writable data belongs under /state. Private runtime files are mounted under /run/autoforge/secrets; do not generate replacement credentials or copy them into candidate source.'
            startup_state=_run([engine,'inspect','--format','{{json .State}}',cid]).decode()[:2000]
            for secret in secret_values.values():
                if secret: startup_logs=startup_logs.replace(secret,'[REDACTED]')
                if secret: startup_state=startup_state.replace(secret,'[REDACTED]')
            return {'schema':1,'candidate_sha256':digest,'build_pass':False,'test_pass':False,
                    'observations':['health readiness timed out or service exited; '+startup_logs[-4096:],
                                    'container state: '+startup_state,
                                    'Verify that the configured entrypoint starts a persistent service listening on port 8000 and remains running; defining handlers alone does not start a server.',runtime_contract],
                    'limitations':['Completed disposable service startup attempt; cleanup runs before returning.','No HTTP/browser acceptance was reached.']}
        observations=[]
        http_results=[]
        execution_deadline=time.monotonic()+timeout
        for scenario in scenarios:
            rows=[]
            request_results=[]
            cookie=None
            scenario_secrets={**secret_values}
            for index,req in enumerate(scenario['requests']):
                if time.monotonic() >= execution_deadline: raise ServiceFailure('HTTP scenario execution timed out')
                if application_builder.STOP_REQUESTED: raise ServiceFailure('service run stopped')
                headers=resolve_scenario(req.get('headers',{}),scenario_secrets)
                if cookie and 'Cookie' not in headers: headers={**headers,'Cookie':cookie}
                try:
                    actual=_request(port,req['method'],req['path'],resolve_scenario(req['body'],scenario_secrets),headers)
                except ServiceFailure as exc:
                    message=str(exc)
                    for secret in secret_values.values():
                        if secret: message=message.replace(secret,'[REDACTED]')
                    rows.append(False)
                    request_results.append({'index':index,'phase':'request','pass':False,'error':message[:2000]})
                    break
                passed=actual['status']==req['expected']['status'] and actual['body']==resolve_scenario(req['expected']['body'],scenario_secrets)
                if req['path']=='/ui/login' and actual['status']==200:
                    cookie_header=actual.pop('_set_cookie',None)
                    if not cookie_header: passed=False
                    else: cookie=cookie_header.split(';',1)[0]
                rows.append(passed)
                request_results.append({'index':index,'pass':passed,'expected':scrub(req['expected']),'actual':scrub(actual),
                                        **({'error':f"HTTP status expected {req['expected']['status']}, received {actual['status']}; body match={actual['body']==resolve_scenario(req['expected']['body'],scenario_secrets)}"} if not passed else {})})
                if not passed: break
                if scenario.get('restart_after') == index and passed:
                    try:
                        _run([engine,'restart',cid],timeout=10)
                        restart_deadline=min(execution_deadline,time.monotonic()+10)
                        while time.monotonic()<restart_deadline:
                            if application_builder.STOP_REQUESTED: raise ServiceFailure('service run stopped')
                            try:
                                if _request(port,'GET',contract_raw['health_path'],None)['status']==200: break
                            except ServiceFailure: pass
                            time.sleep(.2)
                        else: raise ServiceFailure('container restart readiness timed out')
                    except ServiceFailure as exc:
                        rows[-1]=False
                        request_results[-1].update({'pass':False,'phase':'restart','error':scrub(str(exc))[:2000]})
                        break
            observations.append({'name':scenario['name'],'pass':len(rows)==len(scenario['requests']) and all(rows)})
            http_results.append({'scenario':scenario['name'],'requests':request_results})
            for row in request_results:
                if not row['pass']:
                    request=scenario['requests'][row['index']]
                    detail={'method':request['method'],'path':request['path'],'expected':request['expected']}
                    observations.append({'name':f"{scenario['name']} request index {row['index']}: "+row.get('error','response status/body mismatch')+'; public contract '+json.dumps(scrub(detail),ensure_ascii=False,separators=(',',':'))[:4096],'pass':False})
        browser_result=None
        if browser_contract is not None:
            import application_browser
            origin=f'http://127.0.0.1:{port}'
            attestation={'schema':1,'status':'READY','adapter_verified':True,'origin':origin,'candidate_sha256':digest,'service_id':cid}
            try:
                browser_result=application_browser.run(origin,digest,attestation,resolve(browser_contract),browser_output,timeout=min(120,timeout),authorized=browser_authorized,browser_engine=browser_engine)
            except Exception as exc:
                if isinstance(exc,application_browser.BrowserRefused) and str(exc).startswith('BLOCKED:'):
                    raise
                message=str(exc)
                for secret in secret_values.values():
                    if secret: message=message.replace(secret,'[REDACTED]')
                error,separator,diagnostics=message.partition('; browser diagnostics: ')
                browser_result={'status':'FAIL','error':f'{type(exc).__name__}: {error[:2000]}',
                                'diagnostics':separator+diagnostics[-4096:] if separator else ''}
                observations.append({'name':'browser acceptance: '+browser_result['error']+browser_result['diagnostics'],'pass':False})
        resource_witness=None
        if measure_resources:
            metrics_code='''import json
base="/sys/fs/cgroup/"
def read(n):
 with open(base+n,encoding="ascii") as f:return f.read()
stat={k:int(v) for line in read("cpu.stat").splitlines() for k,v in [line.split()] if k in ("usage_usec","user_usec","system_usec")}
print(json.dumps({"schema":1,"cpu_usage_usec":stat["usage_usec"],"user_cpu_usec":stat.get("user_usec"),"system_cpu_usec":stat.get("system_usec"),"memory_current_bytes":int(read("memory.current")),"memory_peak_bytes":int(read("memory.peak"))},sort_keys=True,separators=(",",":")))'''
            try:
                measured=_run([engine,'exec',cid,'python','-I','-c',metrics_code],timeout=5)
                metric_data=json.loads(measured)
                if not isinstance(metric_data,dict) or set(metric_data)!={'schema','cpu_usage_usec','user_cpu_usec','system_cpu_usec','memory_current_bytes','memory_peak_bytes'} or metric_data['schema']!=1 or any(type(metric_data[k]) is not int or metric_data[k]<0 for k in ('cpu_usage_usec','memory_current_bytes','memory_peak_bytes')): raise ValueError('invalid resource counters')
                resource_witness={**metric_data,'source':'trusted engine exec reads container cgroup-v2 cpu.stat, memory.current and memory.peak after HTTP/browser workload and before container cleanup','scope':'container aggregate, includes service startup, health/contract/browser operations and measurement interpreter; memory.peak is cgroup peak, not process RSS'}
            except (OSError,ValueError,ServiceFailure,json.JSONDecodeError) as exc:
                raise ServiceFailure('trusted container CPU/cgroup peak memory measurement unavailable') from exc
        logs=_bounded_logs(engine,name)
        if not all(x['pass'] for x in observations):
            observations.append({'name':'Bounded service logs: '+scrub(logs[-4096:]),'pass':False})
        passed=all(x['pass'] for x in observations) and (browser_result is None or browser_result['status']=='PASS')
        return scrub({'schema':1,'candidate_sha256':digest,'image_id':info['image_id'],'build_pass':True,'test_pass':passed,'observations':[f"{x['name']}: {'matched' if x['pass'] else 'mismatch'}" for x in observations], 'http_results':http_results,'browser':browser_result,'resource_witness':resource_witness,'limitations':['Only declared HTTP and optional declarative browser scenarios are compared; no general correctness proof.','HTTP scenarios run sequentially against shared service state.','Dedicated disposable volume preserves database state across container restart; no storage quota guarantee.','Candidate network is none; host loopback TCP is forwarded through bounded engine exec pipes.']+(['CPU and cgroup peak-memory metrics are trusted engine-read aggregate counters; memory.peak is not process RSS.'] if measure_resources else []),'scenarios':observations,'logs':logs})
    finally:
        if tunnel is not None:
            try:tunnel.close()
            except Exception:pass
        try: _bounded_logs(engine,name)
        except Exception: pass
        try: subprocess.run([engine,'rm','-f',name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False,env=container_env.engine_env(),close_fds=True)
        except Exception: pass
        if volume_created:
            try: _run([engine,'volume','rm',volume],timeout=10)
            except Exception: pass

def main() -> int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--adapter',action='store_true',required=True); p.add_argument('--engine',choices=('docker','podman'),required=True); p.add_argument('--image',required=True); p.add_argument('--contract',type=Path,required=True); p.add_argument('--command-json',required=True,help='JSON argv executed only as container entrypoint'); p.add_argument('--timeout',type=int,default=30); p.add_argument('--browser-contract',type=Path); p.add_argument('--browser-output',type=Path); p.add_argument('--browser-engine',choices=('chromium','firefox','webkit'),default='chromium'); p.add_argument('--measure-resources',action='store_true'); p.add_argument('--authorize-browser',action='store_true'); p.add_argument('--secret-mounts',type=Path); a=p.parse_args(argv)
    try:
        data=sys.stdin.buffer.read(application_builder.MAX_INPUT+1)
        if len(data)>application_builder.MAX_INPUT: raise ServiceFailure('adapter stdin exceeds byte limit')
        envelope=application_builder._strict_loads(data)
        if not isinstance(envelope,dict) or set(envelope)!={'schema','workspace_manifest','candidate_sha256'} or type(envelope['schema']) is not int or envelope['schema']!=1: raise ServiceFailure('adapter input must be exact schema-1 workspace_manifest envelope')
        manifest=envelope['workspace_manifest']
        if not isinstance(manifest,dict) or manifest.get('candidate_sha256')!=envelope['candidate_sha256']: raise ServiceFailure('adapter envelope candidate digest mismatch')
        contract=application_builder._load_json(a.contract,application_builder.MAX_INPUT)
        browser_contract=application_builder._load_json(a.browser_contract,application_builder.MAX_INPUT) if a.browser_contract else None
        secret_data=application_builder._load_json(a.secret_mounts,application_builder.MAX_INPUT) if a.secret_mounts else None
        if secret_data is not None and (not isinstance(secret_data,dict) or set(secret_data)!={'schema','mounts'} or secret_data.get('schema')!=1 or not isinstance(secret_data['mounts'],list)): raise ServiceFailure('secret mount envelope invalid')
        secret_mounts=secret_data['mounts'] if secret_data else None
        detail=run(manifest,contract,a.engine,a.image,json.loads(a.command_json),a.timeout,browser_contract=browser_contract,browser_output=a.browser_output,browser_authorized=a.authorize_browser,browser_engine=a.browser_engine,measure_resources=a.measure_resources,secret_mounts=secret_mounts)
        report={key:detail[key] for key in ('schema','candidate_sha256','build_pass','test_pass','observations','limitations')}
        encoded=application_builder._json_bytes(report)
        if len(encoded)>application_builder.MAX_REPORT: raise ServiceFailure('service adapter report exceeds limit')
        sys.stdout.buffer.write(encoded+b'\n'); return 0
    except application_builder.MissingPrerequisite as e:
        sys.stderr.buffer.write(json.dumps({'status':'BLOCKED','kind':'MISSING_PREREQUISITE','reason':str(e)},ensure_ascii=True).encode()+b'\n'); return 3
    except (OSError,ValueError,json.JSONDecodeError,application_builder.BuildFailure,application_builder.MissingPrerequisite) as e:
        sys.stderr.buffer.write(json.dumps({'status':'FAIL','reason':str(e)},ensure_ascii=True).encode()+b'\n'); return 2
if __name__=='__main__': raise SystemExit(main())
