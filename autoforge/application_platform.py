#!/usr/bin/env python3
"""Manifest-bound native desktop/Android and realtime service acceptance adapters.

Desktop execution is delegated only to a separately provisioned, hash-pinned native
isolated runner implementing the JSON protocol below. Android is driven through the
operator-selected adb executable and exact emulator/device serial. Neither adapter
executes candidate source directly on the controller host.
"""
from __future__ import annotations
import argparse, hashlib, http.client, json, math, os, re, shlex, shutil, socket, subprocess, sys, time, uuid
from pathlib import Path
from typing import Any
import application_checkpoint
import container_env

MAX_INPUT=4_194_304
MAX_REPORT=1_048_576
MAX_ARTIFACT=1_073_741_824
MAX_SCENARIOS=16
MAX_ASSERTIONS=128
class PlatformRefused(ValueError): pass

def _canonical(v:Any)->bytes:return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def _sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):h.update(chunk)
    return h.hexdigest()
def _strict(p:Path,limit:int=MAX_INPUT)->Any:
    application_checkpoint.regular_chain(p)
    with p.open('rb') as f:b=f.read(limit+1)
    if len(b)>limit:raise PlatformRefused('input byte limit exceeded')
    def pairs(rows):
        d={}
        for k,v in rows:
            if k in d:raise PlatformRefused('duplicate JSON key')
            d[k]=v
        return d
    return json.loads(b.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(PlatformRefused('invalid JSON constant')))
def _manifest(raw:Any)->tuple[Path,str]:
    if not isinstance(raw,dict) or set(raw)!={'schema','goal','references','files','candidate_sha256','workspace'} or type(raw.get('schema')) is not int or raw['schema']!=1:raise PlatformRefused('invalid workspace manifest')
    if not isinstance(raw['workspace'],str) or not isinstance(raw['candidate_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',raw['candidate_sha256']):raise PlatformRefused('invalid manifest workspace/digest')
    root=Path(raw['workspace']).resolve(strict=True)
    rows=raw['files']
    if not isinstance(rows,list) or len(rows)>4096:raise PlatformRefused('invalid manifest file inventory')
    seen=set(); normalized=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'path','size','sha256'}:raise PlatformRefused('invalid manifest file record')
        rel=row['path']
        if not isinstance(rel,str) or not rel or '\\' in rel or rel.startswith('/') or any(x in ('','.','..') for x in rel.split('/')) or rel in seen:raise PlatformRefused('unsafe or duplicate candidate path')
        p=root.joinpath(*rel.split('/'))
        application_checkpoint.regular_chain(p)
        if p.is_symlink() or not p.is_file() or p.resolve().parent!=root and root not in p.resolve().parents:raise PlatformRefused('candidate file missing, linked, or outside workspace')
        st=p.stat()
        if st.st_size!=row['size'] or _sha(p)!=row['sha256']:raise PlatformRefused(f'candidate file changed: {rel}')
        normalized.append({'path':rel,'size':st.st_size,'sha256':row['sha256']})
    body={'schema':1,'goal':raw['goal'],'references':raw['references'],'files':normalized}
    digest=hashlib.sha256(_canonical(body)).hexdigest()
    if digest!=raw['candidate_sha256']:raise PlatformRefused('candidate manifest digest mismatch')
    return root,digest

def _authority(auth:Any,platform:str,target:str,digest:str,execution:dict)->None:
    expires=auth.get('expires_at') if isinstance(auth,dict) else None
    if not isinstance(auth,dict) or set(auth)!={'schema','authorize','platform','target','candidate_sha256','execution','expires_at','nonce'} or type(auth.get('schema')) is not int or auth['schema']!=1 or auth['authorize'] is not True or auth['platform']!=platform or auth['target']!=target or auth['candidate_sha256']!=digest or auth['execution']!=execution or type(expires) not in (int,float) or not math.isfinite(expires) or expires<time.time() or not isinstance(auth['nonce'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',auth['nonce']):raise PlatformRefused('explicit unexpired authority must bind platform, named target, candidate hash, execution tuple and nonce')
def _scenario(raw:Any,kind:str)->list[dict]:
    if not isinstance(raw,dict) or set(raw)!={'schema','target','scenarios'} or raw['schema']!=1 or not isinstance(raw['target'],str) or not raw['target'] or not isinstance(raw['scenarios'],list) or not 1<=len(raw['scenarios'])<=MAX_SCENARIOS:raise PlatformRefused(f'invalid {kind} scenario contract')
    names=set();total=0
    for s in raw['scenarios']:
        if not isinstance(s,dict) or set(s)-{'name','actions','assertions'} or not {'name','actions','assertions'}<=set(s) or not isinstance(s['name'],str) or not s['name'] or s['name'] in names or not isinstance(s['actions'],list) or not isinstance(s['assertions'],list) or not s['assertions']:raise PlatformRefused('invalid scenario shape; every scenario requires independent assertions')
        names.add(s['name']);total+=len(s['actions'])+len(s['assertions'])
        if len(s['actions'])>32 or len(s['assertions'])>32:raise PlatformRefused('scenario action/assertion limit exceeded')
        for a in s['actions']:
            if kind=='desktop':
                if not isinstance(a,dict) or a.get('type') not in {'click','type','key','wait'} or set(a)-({'type','value','target'} if a.get('type')!='wait' else {'type','value'}) or ('value' not in a and a.get('type') in {'type','key','wait'}) or (a.get('type')=='wait' and (type(a.get('value')) is not int or not 0<=a['value']<=5000)) or (a.get('type') in {'click','type','key'} and (not isinstance(a.get('target'),str) or not a['target'] or len(a['target'])>256)) or (a.get('type') in {'type','key'} and (not isinstance(a.get('value'),str) or len(a['value'])>4096)) or (a.get('type')=='key' and a.get('value') not in {'ENTER','TAB','ESC','BACKSPACE','DELETE','HOME','END','UP','DOWN','LEFT','RIGHT','CTRL+A','CTRL+C','CTRL+V','CTRL+X','CTRL+Z'}):raise PlatformRefused('desktop actions require bounded exact UIA target, typed value, allowlisted key or bounded wait')
            if kind=='android' and (not isinstance(a,dict) or set(a)-{'type','text','resource_id','x','y','milliseconds'} or a.get('type') not in {'tap','type','key','wait'}):raise PlatformRefused('unsupported Android UI action')
        for a in s['assertions']:
            if not isinstance(a,dict) or a.get('kind') not in {'text','element','state'}:raise PlatformRefused('unsupported assertion kind')
            if kind=='android':
                if a['kind']=='text' and (set(a)-{'kind','value','target','operator'} or not isinstance(a.get('value'),str) or a.get('operator') not in {'contains','equals','not_contains'} or ('target' in a and not isinstance(a['target'],str))):raise PlatformRefused('text assertion requires an operator and optional exact resource-id target')
                if a['kind']=='element' and (set(a)!={'kind','target','operator','value'} or not isinstance(a['target'],str) or a['operator'] not in {'exists','not_exists'} or type(a['value']) is not bool):raise PlatformRefused('element assertion requires exact resource-id and exists/not_exists boolean')
                if a['kind']=='state' and (set(a)!={'kind','target','operator','value'} or not isinstance(a['target'],str) or a['operator'] not in {'checked','enabled'} or type(a['value']) is not bool):raise PlatformRefused('state assertion requires resource-id and checked/enabled boolean')
            elif set(a)-{'kind','value','target','operator'} or not {'kind','value'}<=set(a) or (a['kind']=='text' and (not isinstance(a['value'],str) or len(a['value'])>4096 or a.get('operator') not in {'contains','equals','not_contains'} or ('target' in a and (not isinstance(a['target'],str) or len(a['target'])>256)))) or (a['kind']=='element' and (not isinstance(a.get('target'),str) or not a['target'] or len(a['target'])>256 or a.get('operator') not in {'exists','not_exists'} or type(a['value']) is not bool)) or (a['kind']=='state' and (not isinstance(a.get('target'),str) or not a['target'] or len(a['target'])>256 or a.get('operator') not in {'checked','enabled'} or type(a['value']) is not bool)):raise PlatformRefused('desktop assertion requires supported bounded kind/operator/target/value')
    if total>MAX_ASSERTIONS:raise PlatformRefused('scenario total limit exceeded')
    return raw['scenarios']

def _trusted_exe(path:str,sha:str)->Path:
    p=Path(path)
    if p.is_symlink() or not p.is_file() or not re.fullmatch(r'[0-9a-f]{64}',sha) or _sha(p)!=sha:raise PlatformRefused('trusted executable missing or SHA-256 mismatch')
    return p.resolve(strict=True)

def _desktop(manifest,contract,authority,runner,runner_sha,timeout,artifact_path,artifact_sha,artifact_args):
    root,digest=_manifest(manifest);target=contract.get('target') if isinstance(contract,dict) else ''
    if type(timeout)is not int or not 1<=timeout<=300:raise PlatformRefused('desktop timeout must be 1..300 seconds')
    _scenario(contract,'desktop');exe=_trusted_exe(runner,runner_sha)
    if not isinstance(artifact_path,str) or not artifact_path or '\\' in artifact_path or artifact_path.startswith('/') or any(x in ('','.','..') for x in artifact_path.split('/')):raise PlatformRefused('portable EXE path must be safe manifest-relative path')
    member=next((x for x in manifest['files'] if x['path']==artifact_path),None)
    if member is None or member['sha256']!=artifact_sha or not artifact_path.lower().endswith('.exe'):raise PlatformRefused('portable EXE must be exact manifest member bound to its hash')
    if not isinstance(artifact_args,list) or len(artifact_args)>32 or any(not isinstance(x,str) or '\x00' in x or len(x)>4096 for x in artifact_args):raise PlatformRefused('invalid portable EXE argument vector')
    execution={'runner_sha256':runner_sha,'timeout_seconds':timeout,'artifact':{'path':artifact_path,'sha256':artifact_sha,'args':artifact_args}}
    _authority(authority,'desktop',target,digest,execution)
    scenarios=contract['scenarios'];run_id=uuid.uuid4().hex
    request={'schema':1,'operation':'build-install-launch-interact','candidate_root':str(root),'candidate_sha256':digest,'target':target,'artifact':execution['artifact'],'scenarios':scenarios,'timeout_seconds':timeout,'run_id':run_id}
    argv=[sys.executable,str(exe)] if exe.suffix.lower()=='.py' else [str(exe)]
    try:p=subprocess.run(argv,input=_canonical(request),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout+45,check=False,env={'PATH':os.environ.get('PATH',''),'SYSTEMROOT':os.environ.get('SYSTEMROOT',''),'WINDIR':os.environ.get('WINDIR',''),'PYTHONPATH':str(Path(__file__).resolve().parents[1])},close_fds=True)
    except (OSError,subprocess.TimeoutExpired) as e:raise PlatformRefused('native isolated desktop runner unavailable or timed out') from e
    if p.returncode:raise PlatformRefused('desktop runner rejected or failed: '+p.stderr[:1024].decode('utf8','replace').strip()+'; no success inferred')
    if len(p.stdout)>MAX_REPORT:raise PlatformRefused('desktop runner report exceeds limit')
    report=json.loads(p.stdout.decode('utf8'))
    if not isinstance(report,dict) or set(report) not in ({'schema','run_id','candidate_sha256','artifact_sha256','target','build','install','launch','sandbox','cleanup','scenarios','limitations'},{'schema','run_id','candidate_sha256','artifact_sha256','target','build','install','launch','sandbox','cleanup','scenarios','limitations','evidence_trust'}) or type(report.get('schema')) is not int or report['schema']!=1 or report.get('run_id')!=run_id or report.get('candidate_sha256')!=digest or report.get('artifact_sha256')!=artifact_sha or report.get('target')!=target:raise PlatformRefused('desktop runner returned unbound/malformed evidence')
    if report.get('evidence_trust')=='guest_writable_untrusted':raise PlatformRefused('Windows Sandbox guest report is writable by candidate code and cannot authorize acceptance; independent host-controlled witness required')
    required={'schema','run_id','candidate_sha256','artifact_sha256','target','build','install','launch','sandbox','cleanup','scenarios','limitations'}
    if report.get('sandbox') is not True or report.get('cleanup') is not True or report.get('build')!='NOT_PERFORMED' or not isinstance(report.get('install'),bool) or not isinstance(report.get('launch'),bool) or not isinstance(report.get('scenarios'),list) or len(report['scenarios'])!=len(scenarios) or not isinstance(report.get('limitations'),list) or len(report['limitations'])>32:raise PlatformRefused('desktop runner omitted sandbox, cleanup, build status or scenario evidence')
    for expected,actual in zip(scenarios,report['scenarios']):
        if not isinstance(actual,dict) or actual.get('name')!=expected['name'] or not isinstance(actual.get('pass'),bool) or len(actual.get('assertions',[]))!=len(expected['assertions']) or any(not isinstance(x,dict) or not isinstance(x.get('pass'),bool) for x in actual['assertions']) or actual['pass'] is not all(x['pass'] for x in actual['assertions']):raise PlatformRefused('desktop scenario assertion evidence is malformed or inconsistent')
    passed=report['install'] is True and report['launch'] is True and all(x['pass'] for x in report['scenarios'])
    return {'schema':1,'kind':'desktop-native-acceptance','candidate_sha256':digest,'target':target,'status':'PASS' if passed else 'FAIL','evidence':report,'limitations':['Build from source was not performed; only manifest-bound portable EXE was copied and launched inside Windows Sandbox.','Results apply only to the exact reported runner, OS and architecture.']+report['limitations']}

def _adb(adb:list[str],serial:str,args:list[str],timeout:int=15)->bytes:
    env={key:os.environ[key] for key in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','ANDROID_HOME','ANDROID_SDK_ROOT') if key in os.environ}
    try:p=subprocess.run([*adb,'-s',serial,*args],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False,env=env,close_fds=True)
    except (OSError,subprocess.TimeoutExpired) as e:raise PlatformRefused('adb unavailable or command timed out') from e
    if p.returncode:raise PlatformRefused('adb command failed: '+p.stderr[:1024].decode('utf8','replace'))
    if len(p.stdout)>MAX_INPUT:raise PlatformRefused('adb output exceeds byte limit')
    return p.stdout

def _android(manifest,contract,authority,adb_path,adb_sha,serial,apk_path,apk_sha,package,activity,timeout,build_evidence=None):
    root,digest=_manifest(manifest);target=contract.get('target') if isinstance(contract,dict) else ''
    if not isinstance(contract,dict) or set(contract)!={'schema','target','scenarios'}:raise PlatformRefused('Android scenario contract requires named target')
    scenarios=_scenario(contract,'android')
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',serial) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]*(?:\.[A-Za-z][A-Za-z0-9_]*)+',package) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]*/\.?[A-Za-z0-9_.$]+',activity):raise PlatformRefused('explicit adb serial, package and launcher component required')
    adb=_trusted_exe(adb_path,adb_sha);apk=Path(apk_path)
    if apk.is_symlink() or not apk.is_file() or apk.stat().st_size>MAX_ARTIFACT or _sha(apk)!=apk_sha:raise PlatformRefused('APK missing, oversized, linked, or SHA-256 mismatch')
    apk_rel=apk.resolve(strict=True).relative_to(root).as_posix() if apk.resolve(strict=True)==root or root in apk.resolve(strict=True).parents else None
    member=next((row for row in manifest['files'] if row['path']==apk_rel),None) if apk_rel is not None else None
    if member is not None:
        if member['sha256']!=apk_sha:raise PlatformRefused('APK path and digest are not an exact manifest member')
        evidence_sha=None
    else:
        build_fields={'status','candidate_sha256','apk_sha256','apk','container_image','container_network','sdk_mount','candidate_mount','candidate_executed','build_tools_version','signing','limitations'}
        if not isinstance(build_evidence,dict) or set(build_evidence)!=build_fields or build_evidence.get('status')!='BUILT' or build_evidence.get('candidate_sha256')!=digest or build_evidence.get('apk_sha256')!=apk_sha or not isinstance(build_evidence.get('apk'),str) or Path(build_evidence['apk']).resolve(strict=True)!=apk.resolve(strict=True) or not re.fullmatch(r'sha256:[0-9a-f]{64}',build_evidence.get('container_image','')) or build_evidence.get('container_network')!='none' or build_evidence.get('sdk_mount')!='read-only' or build_evidence.get('candidate_mount')!='read-only manifest-staged only' or build_evidence.get('candidate_executed') is not False or not re.fullmatch(r'\d+\.\d+\.\d+',build_evidence.get('build_tools_version','')) or build_evidence.get('signing')!='ephemeral debug test key; not trusted/release' or not isinstance(build_evidence.get('limitations'),list):
            raise PlatformRefused('APK outside manifest requires isolated build evidence bound to this candidate and APK digest')
        evidence_sha=hashlib.sha256(_canonical(build_evidence)).hexdigest()
        apk_rel='@isolated-build/'+apk_sha
    _authority(authority,'android',target,digest,{'adb_sha256':adb_sha,'serial':serial,'apk_path':apk_rel,'apk_sha256':apk_sha,'build_evidence_sha256':evidence_sha,'package':package,'activity':activity,'timeout_seconds':timeout})
    devices=_adb([str(adb)],serial,['get-state']).decode().strip()
    if devices!='device':raise PlatformRefused('selected Android serial is not in device state')
    existing=_adb([str(adb)],serial,['shell','pm','list','packages',package]).decode('utf8','replace').splitlines()
    if any(line.strip()==f'package:{package}' for line in existing):raise PlatformRefused('target package is already installed; refusing to overwrite user state')
    build={'status':'NOT_PERFORMED','reason':'APK supplied as immutable candidate manifest member.'}
    if member is None:build={'status':'BUILT','candidate_sha256':digest,'apk_sha256':apk_sha,'build_evidence_sha256':evidence_sha,'container_image':build_evidence['container_image'],'container_network':build_evidence['container_network'],'build_tools_version':build_evidence['build_tools_version'],'candidate_executed':False}
    installed=False;launched=False
    try:
        _adb([str(adb)],serial,['install','-t',str(apk)],timeout);installed=True
        _adb([str(adb)],serial,['shell','am','force-stop',package])
        _adb([str(adb)],serial,['shell','am','start','-W','-n',activity],timeout);launched=True
        results=[]
        for scenario in scenarios:
            for action in scenario['actions']:
                typ=action['type']
                if typ=='tap':
                    if 'resource_id' in action:raise PlatformRefused('adb shell UIAutomator selector tapping is unsupported; use coordinates explicitly')
                    x,y=action.get('x'),action.get('y')
                    if type(x)is not int or type(y)is not int or not (0<=x<100000 and 0<=y<100000):raise PlatformRefused('tap requires bounded explicit integer screen coordinates')
                    _adb([str(adb)],serial,['shell','input','tap',str(x),str(y)])
                elif typ=='type':
                    text=action.get('text')
                    if not isinstance(text,str) or len(text)>512 or any(c in text for c in '\x00\r\n'):raise PlatformRefused('Android text input must be bounded single-line text')
                    _adb([str(adb)],serial,['shell','input','text',shlex.quote(text)])
                elif typ=='key':
                    key=action.get('text')
                    if not isinstance(key,str) or not re.fullmatch(r'(BACK|HOME|ENTER|TAB|DEL|[0-9]{1,3})',key):raise PlatformRefused('unsupported Android keycode')
                    _adb([str(adb)],serial,['shell','input','keyevent',key])
                else:
                    ms=action.get('milliseconds',500)
                    if type(ms)is not int or not 0<=ms<=10000:raise PlatformRefused('wait duration outside 0..10000ms')
                    time.sleep(ms/1000)
            _adb([str(adb)],serial,['shell','uiautomator','dump','/sdcard/window.xml'])
            hierarchy=_adb([str(adb)],serial,['shell','cat','/sdcard/window.xml']).decode('utf8','replace')
            import xml.etree.ElementTree as ET
            try:nodes=list(ET.fromstring(hierarchy).iter('node'))
            except ET.ParseError as exc:raise PlatformRefused('Android UIAutomator returned malformed XML') from exc
            checks=[]
            for assertion in scenario['assertions']:
                kind=assertion['kind'];target_id=assertion.get('target');matches=[n for n in nodes if target_id is None or n.get('resource-id')==target_id]
                if kind=='text':
                    visible=' '.join(part for n in matches for part in (n.get('text',''),n.get('content-desc','')) if part);value=assertion['value'];op=assertion['operator']
                    ok=value in visible if op=='contains' else visible==value if op=='equals' else value not in visible
                elif kind=='element':
                    found=any(n.get('resource-id')==target_id for n in nodes);ok=(found if assertion['operator']=='exists' else not found) is assertion['value']
                else:
                    found=[n for n in nodes if n.get('resource-id')==target_id];attr=assertion['operator'];ok=bool(found) and all((n.get(attr)=='true') is assertion['value'] for n in found)
                checks.append({'kind':kind,'target':target_id,'operator':assertion['operator'],'expected':assertion['value'],'pass':ok})
            results.append({'name':scenario['name'],'pass':all(c['pass'] for c in checks),'assertions':checks})
        _adb([str(adb)],serial,['shell','am','force-stop',package])
        if installed:_adb([str(adb)],serial,['uninstall',package],timeout)
        return {'schema':1,'kind':'android-device-acceptance','candidate_sha256':digest,'target':target,'serial':serial,'package':package,'apk_sha256':apk_sha,'build':build,'install':installed,'launch':launched,'status':'PASS' if all(x['pass'] for x in results) else 'FAIL','scenarios':results,'cleanup':True,'limitations':['Requires an operator-prepared reachable Android emulator/device and platform-tools.','Only a previously absent package is installed; exact owned install is force-stopped and uninstalled afterward.','UI hierarchy assertions and input are not screenshot or business-correctness proof.','Exported components, backend integration and broader device/lifecycle matrix are not proven.']}
    except Exception:
        if installed:
            try:
                _adb([str(adb)],serial,['shell','am','force-stop',package])
                _adb([str(adb)],serial,['uninstall',package],timeout)
            except Exception:pass
        raise

def _event_contract(raw):
    required={'schema','target','url','protocol','scenarios','authentication','unauthorized','stop'}
    if not isinstance(raw,dict) or set(raw)!=required or type(raw['schema']) is not int or raw['schema']!=1 or raw['protocol']!='sse' or not isinstance(raw['url'],str) or not raw['url'].startswith('http://127.0.0.1:'):raise PlatformRefused('authenticated realtime contract supports SSE only; WebSocket authorization/stop acceptance is unsupported')
    if len(raw['url'])>2048 or any(c in raw['url'] for c in '\r\n') or not re.fullmatch(r'http://127\.0\.0\.1:8000(?:/[A-Za-z0-9_./?=&%-]*)?',raw['url']):raise PlatformRefused('realtime endpoint must be bounded container-loopback SSE port 8000 path')
    auth=raw['authentication']
    if not isinstance(auth,dict) or set(auth)!={'header','value'} or not re.fullmatch(r'Authorization|X-[A-Za-z0-9-]{1,64}',auth.get('header','')) or not isinstance(auth.get('value'),str) or len(auth['value'])<8 or len(auth['value'])>4096 or any(c in auth['value'] for c in '\r\n'):raise PlatformRefused('realtime auth header/value must be explicitly bounded')
    probes=raw['unauthorized']
    if not isinstance(probes,list) or len(probes)!=3 or {x.get('operation') for x in probes if isinstance(x,dict)}!={'subscribe','publish','stop'}:raise PlatformRefused('realtime acceptance requires unauthorized subscribe, publish and stop probes')
    for probe in probes:
        if not isinstance(probe,dict) or set(probe)!={'operation','expected_status','path'} or type(probe['expected_status']) is not int or probe['expected_status'] not in (401,403) or not isinstance(probe['path'],str) or not probe['path'].startswith('/') or len(probe['path'])>2048:raise PlatformRefused('invalid unauthorized realtime probe')
    stop=raw['stop']
    if not isinstance(stop,dict) or set(stop)!={'path','body','expected_status'} or not isinstance(stop['path'],str) or not stop['path'].startswith('/') or len(stop['path'])>2048 or not isinstance(stop['body'],dict) or len(_canonical(stop['body']))>65536 or type(stop['expected_status']) is not int or stop['expected_status'] not in (200,202,204):raise PlatformRefused('explicit bounded authenticated stop action required')
    if not isinstance(raw['scenarios'],list) or not 1<=len(raw['scenarios'])<=MAX_SCENARIOS:raise PlatformRefused('realtime scenarios required and bounded')
    names=set()
    for s in raw['scenarios']:
        if not isinstance(s,dict) or set(s)!={'name','send','expected_events','reconnect_after'} or not isinstance(s['name'],str) or not s['name'] or s['name'] in names or not isinstance(s['send'],list) or not isinstance(s['expected_events'],list) or not s['expected_events'] or type(s['reconnect_after']) is not int or not 0<=s['reconnect_after']<len(s['expected_events']):raise PlatformRefused('invalid realtime scenario')
        names.add(s['name'])
        if len(s['send'])!=len(s['expected_events']) or len(s['expected_events'])>128 or not 0<s['reconnect_after']<len(s['expected_events']):raise PlatformRefused('realtime send/event counts must match <=128 and reconnect must precede an event')
    return raw['scenarios']

def _sse_read(sock,deadline):
    sock.settimeout(max(.1,deadline-time.monotonic()));data=bytearray()
    while True:
        chunk=sock.recv(1)
        if not chunk:raise PlatformRefused('SSE stream closed before complete event')
        data.extend(chunk)
        if len(data)>65536:raise PlatformRefused('SSE frame exceeds 64KiB')
        if data.endswith(b'\n\n') or data.endswith(b'\r\n\r\n'):break
    block=bytes(data)
    marker=b'\r\n\r\n' if block.endswith(b'\r\n\r\n') else b'\n\n'
    block=block[:-len(marker)].decode('utf8');event={'data':''}
    for line in block.splitlines():
        if line.startswith('data:'):event['data']+='\n'+line[5:].lstrip()
        elif line.startswith('id:'):event['id']=line[3:].strip()
        elif line.startswith('event:'):event['event']=line[6:].strip()
    event['data']=event['data'].lstrip('\n')
    try:event['json']=json.loads(event['data'])
    except json.JSONDecodeError:event['json']=event['data']
    return event

def _realtime(manifest,contract,authority,engine,image,command,timeout):
    """Run event-stream scenarios against an exact-image, network-none disposable service."""
    import application_service
    import container_http_tunnel
    root,digest=_manifest(manifest);scenarios=_event_contract(contract);target=contract.get('target','');secret=contract['authentication']['value'];known_secrets={secret}
    if secret.startswith('Bearer '):known_secrets.add(secret[len('Bearer '):])
    def scrub(value):
        if isinstance(value,bytes):value=value.decode('utf-8','replace')
        if isinstance(value,str):
            for known in sorted(known_secrets,key=len,reverse=True):
                if known:value=value.replace(known,'[REDACTED]')
            return value
        if isinstance(value,list):return [scrub(item) for item in value]
        if isinstance(value,dict):return {scrub(k) if isinstance(k,str) else k:scrub(v) for k,v in value.items()}
        return value
    if isinstance(image,str) and re.fullmatch(r'[0-9a-f]{64}',image):image='sha256:'+image
    if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x or '\x00' in x for x in command):raise PlatformRefused('realtime service command must be explicit argv')
    _authority(authority,'realtime',target,digest,{'engine':engine,'image':image,'command_sha256':hashlib.sha256(_canonical(command)).hexdigest(),'contract_sha256':hashlib.sha256(_canonical(contract)).hexdigest(),'protocol':contract['protocol'],'url':contract['url'],'timeout_seconds':timeout})
    if type(timeout)is not int or not 1<=timeout<=300:raise PlatformRefused('realtime timeout must be 1..300 seconds')
    try:info=application_service.preflight(engine,image)
    except Exception as e:raise PlatformRefused('configured exact local Linux service image/engine unavailable: '+str(e)) from e
    name='autoforge-rt-'+uuid.uuid4().hex;cid=None;tunnel=None
    runenv=container_env.engine_env()
    def run(argv,seconds=15):
        try:r=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=seconds,check=False,env=runenv,close_fds=True)
        except (OSError,subprocess.TimeoutExpired) as e:raise PlatformRefused('isolated realtime service command unavailable or timed out') from e
        if r.returncode:raise PlatformRefused('isolated realtime service command failed: '+scrub(r.stderr[:1024]))
        if len(r.stdout)>65536:raise PlatformRefused('isolated service response exceeds limit')
        return r.stdout
    def request(port,path,method='GET',body=None,headers=None):
        if not isinstance(path,str) or not path.startswith('/') or any(c in path for c in '\r\n') or len(path)>2048:raise PlatformRefused('request path invalid')
        headers={} if headers is None else headers
        if not isinstance(headers,dict) or len(headers)>32 or any(not isinstance(k,str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}",k) or not isinstance(v,str) or any(c in v for c in '\r\n') or len(v)>4096 for k,v in headers.items()):raise PlatformRefused('request headers invalid')
        data=body or b''
        if not isinstance(data,bytes) or len(data)>65536:raise PlatformRefused('HTTP publish body exceeds 64KiB')
        s=socket.create_connection(('127.0.0.1',port),timeout=3);s.settimeout(3)
        h={'Host':f'127.0.0.1:{port}','Connection':'close',**(headers or {})}
        if data:h['Content-Length']=str(len(data))
        wire=(f'{method} {path} HTTP/1.1\r\n'+'\r\n'.join(f'{k}: {v}' for k,v in h.items())+'\r\n\r\n').encode()+data
        s.sendall(wire);buf=bytearray()
        try:
            while len(buf)<65536:
                b=s.recv(4096)
                if not b:break
                buf.extend(b)
                if b'\r\n\r\n' in buf:break
        finally:s.close()
        head=bytes(buf).split(b'\r\n\r\n',1)[0].decode('latin1','replace')
        status=int(head.split(' ',2)[1]) if len(head.split())>1 else 0
        return status,head
    class BufferedSocket:
        def __init__(self,sock,prefix=b''):self.sock=sock;self.buffer=bytearray(prefix)
        def settimeout(self,value):self.sock.settimeout(value)
        def read_exact(self,n):
            while len(self.buffer)<n:
                chunk=self.sock.recv(min(4096,n-len(self.buffer)))
                if not chunk:raise PlatformRefused('connection closed before bounded read completed')
                self.buffer.extend(chunk)
            result=bytes(self.buffer[:n]);del self.buffer[:n];return result
        def recv(self,n):
            if self.buffer:
                result=bytes(self.buffer[:n]);del self.buffer[:n];return result
            return self.sock.recv(n)
        def sendall(self,data):self.sock.sendall(data)
        def close(self):self.sock.close()
    def read_headers(s,marker=b'\r\n\r\n',limit=16384):
        data=bytearray()
        while marker not in data and len(data)<limit:
            chunk=s.recv(min(4096,limit-len(data)))
            if not chunk:raise PlatformRefused('HTTP handshake closed before complete headers')
            data.extend(chunk)
        if marker not in data:raise PlatformRefused('HTTP handshake headers missing or oversized')
        boundary=data.index(marker)+len(marker)
        return bytes(data[:boundary]),bytes(data[boundary:])
    def connect(path,protocol,last_event_id=None,headers=None):
        auth_headers=''.join(f'{k}: {v}\r\n' for k,v in (headers or {}).items())
        if protocol=='sse':
            raw=socket.create_connection(('127.0.0.1',port),timeout=3);raw.settimeout(3);s=BufferedSocket(raw)
            extra=f'Last-Event-ID: {last_event_id}\r\n' if last_event_id is not None else ''
            s.sendall((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n{auth_headers}{extra}\r\n').encode())
            head,carry=read_headers(s)
            if b' 200 ' not in head.split(b'\r\n',1)[0] or b'content-type: text/event-stream' not in head.lower():s.close();raise PlatformRefused('SSE endpoint returned wrong status/content-type')
            s.buffer.extend(carry);return s
        import base64
        key=base64.b64encode(os.urandom(16)).decode();raw=socket.create_connection(('127.0.0.1',port),timeout=3);raw.settimeout(3);s=BufferedSocket(raw)
        s.sendall((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{auth_headers}\r\n').encode())
        head,carry=read_headers(s);accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        if b' 101 ' not in head.split(b'\r\n',1)[0] or b'Sec-WebSocket-Accept: '+accept.lower() not in head.lower():s.close();raise PlatformRefused('WebSocket upgrade handshake invalid')
        s.buffer.extend(carry);return s
    def ws_send(s,value):
        import os,struct
        payload=_canonical(value);mask=os.urandom(4);n=len(payload)
        if n>65536:raise PlatformRefused('WebSocket message exceeds 64KiB')
        header=bytearray([0x81,0x80|(n if n<126 else 126)])
        if n>=126:header.extend(struct.pack('!H',n))
        s.sendall(header+mask+bytes(x^mask[i%4] for i,x in enumerate(payload)))
    def ws_control(s,opcode,payload):
        import os
        if len(payload)>125:raise PlatformRefused('WebSocket control payload exceeds 125 bytes')
        mask=os.urandom(4);header=bytes([0x80|opcode,0x80|len(payload)])
        s.sendall(header+mask+bytes(x^mask[i%4] for i,x in enumerate(payload)))
    def ws_read(s):
        import json,struct
        opcode=None;payload=bytearray()
        while True:
            first,second=s.read_exact(2);fin=bool(first&0x80);op=first&0x0f;masked=bool(second&0x80);length=second&0x7f
            if first&0x70:raise PlatformRefused('WebSocket reserved bits/extensions unsupported')
            if masked:raise PlatformRefused('server WebSocket frames must not be masked')
            if length==126:length=struct.unpack('!H',s.read_exact(2))[0]
            elif length==127:
                length=struct.unpack('!Q',s.read_exact(8))[0]
                if length<65536:raise PlatformRefused('noncanonical WebSocket 64-bit length')
            if length>65536:raise PlatformRefused('WebSocket frame exceeds 64KiB')
            data=s.read_exact(length)
            if op>=8:
                if not fin or length>125:raise PlatformRefused('invalid WebSocket control frame')
                if op==8:raise PlatformRefused('WebSocket closed before expected event')
                if op==9:ws_control(s,0xA,data);continue
                if op==10:continue
                raise PlatformRefused('unsupported WebSocket control opcode')
            if opcode is None:
                if op not in (1,2):raise PlatformRefused('WebSocket data message must start text/binary')
                opcode=op
            elif op!=0:raise PlatformRefused('nested WebSocket message')
            payload.extend(data)
            if len(payload)>65536:raise PlatformRefused('WebSocket message exceeds 64KiB')
            if fin:break
        if opcode!=1:raise PlatformRefused('binary WebSocket messages unsupported')
        try:return json.loads(payload.decode('utf8'))
        except (ValueError,UnicodeDecodeError):return payload.decode('utf8','replace')
    try:
        cid=run([engine,'run','-d','--name',name,'--log-driver=json-file','--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=64','--memory=256m','--cpus=1','--mount',f'type=bind,src={root},dst=/candidate,readonly','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777','--workdir','/candidate','--entrypoint',command[0],image,*command[1:]]).decode().strip()
        if not re.fullmatch(r'[0-9a-f]{12,64}',cid):raise PlatformRefused('engine returned invalid isolated container ID')
        tunnel=container_http_tunnel.Tunnel(engine,cid,runenv);port=tunnel.port;deadline=time.monotonic()+min(timeout,30);ready=False
        while time.monotonic()<deadline:
            try:
                status,_=request(port,'/health')
                if status==200:ready=True;break
            except (OSError,ValueError,IndexError):pass
            if run([engine,'inspect','--format','{{.State.Running}}',cid]).decode().strip()!='true':break
            time.sleep(.2)
        if not ready:
            try:startup_logs=scrub(application_service._bounded_logs(engine,cid))[-8192:]
            except Exception as exc:startup_logs='log capture failed: '+scrub(str(exc))
            try:container_state=scrub(run([engine,'inspect','--format','{{json .State}}',cid],5))[:4096]
            except Exception as exc:container_state='state capture failed: '+scrub(str(exc))
            raise PlatformRefused('realtime service readiness failed; state='+container_state+'; logs='+startup_logs)
        end=time.monotonic()+timeout;results=[];auth=contract['authentication'];auth_headers={auth['header']:auth['value']};path=contract['url'].split(':8000',1)[-1] or '/events'
        if not path.startswith('/'):raise PlatformRefused('realtime URL path is invalid')
        for probe in contract['unauthorized']:
            if probe['operation']=='subscribe':
                raw=socket.create_connection(('127.0.0.1',port),timeout=3);raw.settimeout(3);sock=BufferedSocket(raw)
                sock.sendall((f"GET {probe['path']} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\nConnection: close\r\n\r\n").encode())
                try:head,_=read_headers(sock);status=int(head.split(b' ',2)[1])
                finally:sock.close()
            elif probe['operation']=='stop':status=request(port,contract['stop']['path'],'POST',_canonical(contract['stop']['body']))[0]
            else:status=request(port,probe['path'],'POST',b'{}')[0]
            if status!=probe['expected_status']:raise PlatformRefused(f"unauthorized {probe['operation']} expected HTTP {probe['expected_status']}, got {status}")
        results.append({'name':'unauthorized-access','pass':True,'assertions':[{'operation':x['operation'],'status':x['expected_status'],'pass':True} for x in contract['unauthorized']]})
        for scenario in scenarios:
            stream=connect(path,contract['protocol'],headers=auth_headers);observed=[]
            try:
                for i,send in enumerate(scenario['send']):
                    if not isinstance(send,dict) or set(send)-{'path','body','headers'} or not {'path','body'}<=set(send):raise PlatformRefused('SSE send must explicitly bind publish path/body')
                    status,_=request(port,send['path'],'POST',_canonical(send['body']) if send['body'] is not None else b'',{'Content-Type':'application/json',**auth_headers,**send.get('headers',{})})
                    if status not in (200,201,202,204):raise PlatformRefused(f'SSE publish returned HTTP {status}')
                    actual=_sse_read(stream,end)
                    observed.append(actual)
                    if i+1==scenario['reconnect_after'] and i+1<len(scenario['send']):
                        cursor=actual.get('id')
                        if cursor is None:raise PlatformRefused('SSE reconnect needs observed event id')
                        stream.close();stream=connect(path,'sse',cursor,auth_headers)
                expected=scenario['expected_events'];checks=[{'index':i,'pass':i<len(observed) and observed[i]==expected[i]} for i in range(len(expected))]
                ids=[x.get('id') for x in observed]
                if contract['protocol']=='sse' and (any(not isinstance(x,str) or not x for x in ids) or any(not ids[i]<ids[i+1] for i in range(len(ids)-1))):checks.append({'index':'strict-monotonic-ids','pass':False})
                ordered=all(check['pass'] for check in checks)
                results.append({'name':scenario['name'],'pass':ordered,'ordered':ordered,'reconnect_after':scenario['reconnect_after'],'observed':observed[:len(expected)],'assertions':checks})
            finally:stream.close()
        stop=contract['stop'];status,_=request(port,stop['path'],'POST',_canonical(stop['body']),auth_headers)
        if status!=stop['expected_status']:raise PlatformRefused(f'authenticated stop expected HTTP {stop["expected_status"]}, got {status}')
        raw=socket.create_connection(('127.0.0.1',port),timeout=3);raw.settimeout(3);sock=BufferedSocket(raw)
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\nConnection: close\r\n{auth['header']}: {auth['value']}\r\n\r\n").encode())
        try:head,_=read_headers(sock);status=int(head.split(b' ',2)[1])
        finally:sock.close()
        if status!=410:raise PlatformRefused(f'post-stop SSE subscription expected HTTP 410, got {status}')
        results.append({'name':'authenticated-stop','pass':True,'assertions':[{'operation':'stop','status':stop['expected_status'],'pass':True},{'operation':'post-stop-subscribe','status':410,'pass':True}]})
        logs=scrub(application_service._bounded_logs(engine,name)[-8192:]) or 'candidate logs unavailable'
        return scrub({'schema':1,'kind':'realtime-protocol-acceptance','candidate_sha256':digest,'target':target,'status':'PASS' if all(x['pass'] for x in results) else 'FAIL','build_pass':True,'test_pass':all(x['pass'] for x in results),'image_id':info.get('image_id'),'protocol':contract['protocol'],'scenarios':results,'cleanup':True,'logs':logs,'limitations':['Service runs in an exact-image local Linux container with network disabled; only the existing loopback tunnel reaches its port 8000.','Event equality and order are compared to authored expectations; this does not prove distributed delivery guarantees or production broker behavior.','This authenticated realtime acceptance is SSE-only; WebSocket authorization and stop are unsupported.','SSE event size is capped at 64KiB; Last-Event-ID cursor is checked against the next event after reconnect.']})
    finally:
        if tunnel is not None:
            tunnel.close()
        if cid:
            run([engine,'rm','-f',cid],10)
            remaining=run([engine,'ps','-aq','--filter',f'id={cid}'],10).decode().strip()
            if remaining:raise PlatformRefused('owned realtime container remains after cleanup')
        else:
            run([engine,'rm','-f',name],10)
            remaining=run([engine,'ps','-aq','--filter',f'name=^{name}$'],10).decode().strip()
            if remaining:raise PlatformRefused('owned realtime container name remains after cleanup')

def main()->int:
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='kind',required=True)
    for kind in ('desktop','android'):
        q=sub.add_parser(kind);q.add_argument('--manifest',type=Path,required=True);q.add_argument('--contract',type=Path,required=True);q.add_argument('--authority',type=Path,required=True);q.add_argument('--output',type=Path,required=True)
        if kind=='desktop':q.add_argument('--runner',required=True);q.add_argument('--runner-sha256',required=True);q.add_argument('--artifact-path',required=True,help='portable EXE path relative to the candidate manifest root');q.add_argument('--artifact-sha256',required=True);q.add_argument('--artifact-args-json',default='[]');q.add_argument('--timeout',type=int,default=120)
        else:
            q.add_argument('--adb',required=True);q.add_argument('--adb-sha256',required=True);q.add_argument('--serial',required=True);q.add_argument('--apk',required=True);q.add_argument('--apk-sha256',required=True);q.add_argument('--build-evidence',type=Path,help='exact output JSON from application_android_build.py build for an APK outside the candidate manifest');q.add_argument('--package',required=True);q.add_argument('--activity',required=True);q.add_argument('--timeout',type=int,default=120)
    r=sub.add_parser('realtime');r.add_argument('--manifest',type=Path,required=True);r.add_argument('--contract',type=Path,required=True);r.add_argument('--authority',type=Path,required=True);r.add_argument('--engine',choices=('docker','podman'),required=True);r.add_argument('--image',required=True);r.add_argument('--command-json',required=True);r.add_argument('--timeout',type=int,default=30);r.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    try:
        manifest=_strict(a.manifest);contract=_strict(a.contract);authority=_strict(a.authority)
        if a.kind=='desktop':report=_desktop(manifest,contract,authority,a.runner,a.runner_sha256,a.timeout,a.artifact_path,a.artifact_sha256,_strict_argv(a.artifact_args_json))
        elif a.kind=='android':report=_android(manifest,contract,authority,a.adb,a.adb_sha256,a.serial,a.apk,a.apk_sha256,a.package,a.activity,a.timeout,_strict(a.build_evidence) if a.build_evidence else None)
        else:report=_realtime(manifest,contract,authority,a.engine,a.image,_strict_argv(a.command_json),a.timeout)
        encoded=_canonical(report)+b'\n'
        if len(encoded)>MAX_REPORT:raise PlatformRefused('report exceeds output limit')
        with a.output.open('xb') as f:f.write(encoded)
        print(encoded.decode(),end='');return 0 if report.get('status')=='PASS' else 2
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError,PlatformRefused) as e:
        reported=str(e)
        if a.kind=='realtime' and 'contract' in locals() and isinstance(contract,dict):
            auth=contract.get('authentication',{})
            secrets={auth.get('value','')} if isinstance(auth,dict) else set()
            value=auth.get('value','') if isinstance(auth,dict) else ''
            if value.startswith('Bearer '):secrets.add(value[len('Bearer '):])
            for secret in sorted(secrets,key=len,reverse=True):
                if secret:reported=reported.replace(secret,'[REDACTED]')
        if getattr(a,'output',None) is not None and not a.output.exists():
            encoded=_canonical({'schema':1,'status':'BLOCKED','reason':reported,'limitations':['No acceptance is inferred from missing runtime prerequisites or incomplete evidence.']})+b'\n'
            try:
                with a.output.open('xb') as f:f.write(encoded)
            except FileExistsError:pass
        print(json.dumps({'schema':1,'status':'BLOCKED','reason':reported,'limitations':['No acceptance is inferred from missing runtime prerequisites or incomplete evidence.']},ensure_ascii=False));return 2

def _strict_argv(raw):
    value=json.loads(raw)
    if not isinstance(value,list):raise PlatformRefused('command JSON must be argv list')
    return value
if __name__=='__main__':raise SystemExit(main())
