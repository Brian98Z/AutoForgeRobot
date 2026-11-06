"""Authorized local container release operations for sealed application packages.

Only exact locally present image IDs run. Candidate containers remain network=none,
read-only, non-root and resource-limited; the existing exec-pipe tunnel is used for
health and business probes. No generated code runs on the host.
"""
from __future__ import annotations
import argparse, hashlib, http.client, json, os, re, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path
from typing import Any
import application_builder as builder
import application_checkpoint as checkpoint
import application_delivery as delivery
import application_secrets as secrets_module
from container_http_tunnel import Tunnel

class OperationsRefused(ValueError): pass
LABEL='io.omp.autoforge.owner'
OWNER='autoforge-application-operations-v1'


def _run(engine:str,args:list[str],timeout:int=20)->str:
    try:r=subprocess.run([engine,*args],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False,env={'PATH':os.environ.get('PATH','')},close_fds=True)
    except (OSError,subprocess.TimeoutExpired) as e:raise OperationsRefused(f'container engine command failed: {args[0]}') from e
    if r.returncode: raise OperationsRefused(f'container engine command failed ({args[0]}): {r.stderr[:1000].decode("utf-8","replace")}')
    return r.stdout.decode('utf-8','replace').strip()

def _run_named_database_helper(engine:str,name:str,argv:list[str],result:dict[str,Any])->dict[str,Any]:
    state=''
    try:
        _run(engine,argv,timeout=30)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            state=_run(engine,['inspect','--format','{{.State.Status}}',name],timeout=10)
            if state in {'exited','dead'}:break
        exit_code=_run(engine,['inspect','--format','{{.State.ExitCode}}',name],timeout=10)
        output=_run(engine,['logs',name],timeout=15)
        if output.splitlines()[-1:] and output.splitlines()[-1].startswith('MIGRATION_FAILED_RESTORED:'):raise OperationsRefused('migration failed and snapshot was restored and hash-verified: '+output.splitlines()[-1].split(':',1)[1])
        if output.splitlines()[-1:] and output.splitlines()[-1].startswith('MIGRATION_RECOVERY_UNVERIFIED:'):raise OperationsRefused('migration recovery could not be verified: '+output.splitlines()[-1].split(':',1)[1])
        if exit_code!='0':raise OperationsRefused(f'database helper exited unsuccessfully ({exit_code}); output={output[-800:]}')
        if result.get('migration')=='PASS' and output.splitlines()[-1:]!=['MIGRATION_PASS']:raise OperationsRefused('migration helper did not return verified success')
        result['helper_output']=output
        return result
    finally:
        if _run(engine,['ps','-aq','--filter',f'name=^{name}$']):
            labels=json.loads(_run(engine,['inspect','--format','{{json .Config.Labels}}',name],timeout=10))
            if labels.get(LABEL)!=OWNER:raise OperationsRefused('refusing to remove an unowned state volume helper')
            state=_run(engine,['inspect','--format','{{.State.Status}}',name],timeout=10)
            if state not in {'exited','dead'}:
                _run(engine,['kill','--signal','KILL',name],timeout=10)
                state=_run(engine,['inspect','--format','{{.State.Status}}',name],timeout=10)
                if state not in {'exited','dead'}:raise OperationsRefused('state volume helper termination unresolved')
            _run(engine,['rm',name],timeout=10)
        if _run(engine,['ps','-aq','--filter',f'name=^{name}$']):raise OperationsRefused('state volume helper remains; reconciliation required')
def _stateful_database(engine:str,image_id:str,volume:str,migration_sql:str|None,expected_version:int|None,target_version:int|None,root:Path,helpername:str|None=None)->dict[str,Any]:
    if not re.fullmatch(r'af-state-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}',volume):raise OperationsRefused('invalid AutoForge state volume name')
    if (migration_sql is None)!=(expected_version is None and target_version is None):raise OperationsRefused('migration SQL and paired schema versions are required together')
    if migration_sql is not None and (not isinstance(migration_sql,str) or not migration_sql.strip() or len(migration_sql.encode())>262144):raise OperationsRefused('bounded operator-authored migration SQL required')
    if migration_sql is not None and (type(expected_version)is not int or type(target_version)is not int or not 0<=expected_version<=target_version<=2147483647 or expected_version==target_version):raise OperationsRefused('schema versions must be increasing nonnegative 31-bit integers')
    scope=hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()
    found=_run(engine,['volume','ls','-q','--filter',f'name=^{volume}$'])
    if not found:_run(engine,['volume','create','--label',f'{LABEL}={OWNER}','--label',f'io.omp.autoforge.state-root={scope}',volume])
    labels=json.loads(_run(engine,['volume','inspect','--format','{{json .Labels}}',volume]))
    if labels.get(LABEL)!=OWNER or labels.get('io.omp.autoforge.state-root')!=scope:raise OperationsRefused('state volume is not owned by this exact deployment root')
    result={'volume':volume,'migration':'not_requested','backup':'not_requested','data_rollback':'not_claimed'}
    if migration_sql is None:
        if not helpername or not re.fullmatch(r'af-migrate-[0-9a-f]{32}',helpername):raise OperationsRefused('recorded AutoForge volume initializer identity required')
        script="import os;os.chown('/state',65534,65534)"
        run_args=['run','-d','--name',helpername,'--label',f'{LABEL}={OWNER}','--network=none','--read-only','--user=0:0','--cap-drop=ALL','--cap-add=CHOWN','--pids-limit=8','--memory=32m','--cpus=.5','--security-opt=no-new-privileges','--mount',f'type=volume,src={volume},dst=/state','--entrypoint','python',image_id,'-I','-c',script]
        return _run_named_database_helper(engine,helpername,run_args,result)
    if not helpername or not re.fullmatch(r'af-migrate-[0-9a-f]{32}',helpername):raise OperationsRefused('recorded AutoForge migration helper identity required')
    users=_run(engine,['ps','-aq'])
    for cid in filter(None,users.splitlines()):
        info=json.loads(_run(engine,['inspect','--format','{{json .}}',cid]))
        if (info.get('State',{}).get('Running') or info.get('State',{}).get('Restarting') or info.get('State',{}).get('Status')=='restarting') and any(m.get('Name')==volume for m in info.get('Mounts',[])):raise OperationsRefused('migration requires every container mounting state volume to be stopped')
    script="""import hashlib,os,sqlite3,stat,sys
p='/state/tasks.sqlite'; sql=sys.argv[1]; expected=int(sys.argv[2]); target=int(sys.argv[3]); snapshot='/tmp/autoforge-pre-migration.sqlite'; c=None; before=None
try:
 st=os.lstat(p) if os.path.lexists(p) else None
 if st is not None and (not stat.S_ISREG(st.st_mode) or st.st_nlink!=1):raise ValueError('database is not a single-link regular file')
 if st is None:
  os.chown('/state',0,0)
  c=sqlite3.connect(p); c.close(); os.chown(p,0,0);os.chmod(p,0o600)
 else:
  os.chown('/state',0,0);os.chown(p,0,0);os.chmod(p,0o600)
 c=sqlite3.connect(p,timeout=30,isolation_level=None)
 if c.execute('PRAGMA user_version').fetchone()[0]!=expected:raise ValueError('schema version mismatch')
 b=sqlite3.connect(snapshot); c.backup(b); b.close(); before=hashlib.sha256(open(snapshot,'rb').read()).hexdigest()
 c.execute('BEGIN IMMEDIATE')
 def auth(a,x,y,d,t):return sqlite3.SQLITE_OK if a==sqlite3.SQLITE_PRAGMA and str(x).lower()=='user_version' else (sqlite3.SQLITE_DENY if a in {sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH,sqlite3.SQLITE_TRANSACTION,sqlite3.SQLITE_SAVEPOINT,sqlite3.SQLITE_ALTER_TABLE} or a==sqlite3.SQLITE_FUNCTION and str(y or x).lower() in {'load_extension','writefile'} else sqlite3.SQLITE_OK)
 stm=[];cur=''
 for ch in sql:
  cur+=ch
  if sqlite3.complete_statement(cur):
   if cur.strip():stm.append(cur.strip())
   cur=''
 if cur.strip():raise ValueError('incomplete SQL')
 c.set_authorizer(auth)
 for statement in stm:c.execute(statement)
 c.set_authorizer(None);c.execute('PRAGMA user_version='+str(target));c.commit();c.close();c=None
 os.chown('/state',65534,65534);os.chown(p,65534,65534);print('MIGRATION_PASS')
except BaseException as error:
 if c is not None:
  try:c.set_authorizer(None);c.rollback();c.close()
  except Exception:pass
 try:
  if before is None:raise ValueError('verified snapshot missing; refusing restore')
  st=os.lstat(p)
  if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:raise ValueError('database is not a single-link regular file')
  os.chown('/state',0,0);os.chown(p,0,0);os.chmod(p,0o600)
  data=open(snapshot,'rb').read()
  fd=os.open(p,os.O_WRONLY|getattr(os,'O_NOFOLLOW',0))
  try:
   os.ftruncate(fd,0)
   view=memoryview(data)
   while view:view=view[os.write(fd,view):]
   os.fsync(fd)
  finally:os.close(fd)
  if hashlib.sha256(open(p,'rb').read()).hexdigest()!=before:raise ValueError('database snapshot byte digest mismatch after restore')
  os.chmod(p,0o644);os.chown('/state',65534,65534);os.chown(p,65534,65534)
  print('MIGRATION_FAILED_RESTORED:'+type(error).__name__+':'+str(error)[:400])
 except BaseException as restore_error:print('MIGRATION_RECOVERY_UNVERIFIED:'+type(restore_error).__name__+':'+str(restore_error)[:300])
 sys.exit(0)
"""
    helper_args=['run','-d','--name',helpername,'--label',f'{LABEL}={OWNER}','--network=none','--read-only','--user=0:0','--cap-drop=ALL','--cap-add=CHOWN','--pids-limit=16','--memory=128m','--cpus=1','--security-opt=no-new-privileges','--mount',f'type=volume,src={volume},dst=/state','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m','--entrypoint','python',image_id,'-I','-c',script,migration_sql,str(expected_version),str(target_version)]
    return _run_named_database_helper(engine,helpername,helper_args,{**result,'migration':'PASS','schema_version':target_version,'migration_sha256':hashlib.sha256(migration_sql.encode()).hexdigest(),'data_rollback':'not_claimed'})
def _backup_state(engine:str,image_id:str,volume:str,destination:Path,root:Path)->dict[str,Any]:
    dest=Path(destination).absolute(); root=Path(root).absolute()
    if dest.exists() or dest.is_symlink() or not dest.parent.is_dir():raise OperationsRefused('backup destination must be absent and have an existing parent')
    if not re.fullmatch(r'af-state-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}',volume):raise OperationsRefused('invalid AutoForge state volume')
    scope=hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    labels=json.loads(_run(engine,['volume','inspect','--format','{{json .Labels}}',volume]))
    if labels.get(LABEL)!=OWNER or labels.get('io.omp.autoforge.state-root')!=scope:raise OperationsRefused('state volume is not owned by this exact deployment root')
    for cid in filter(None,_run(engine,['ps','-aq']).splitlines()):
        container=json.loads(_run(engine,['inspect','--format','{{json .}}',cid]))
        state=container.get('State',{})
        if (state.get('Running') or state.get('Restarting') or state.get('Status')=='restarting') and any(m.get('Name')==volume for m in container.get('Mounts',[])):raise OperationsRefused('backup requires all containers mounting state volume to be stopped')
    info=builder.preflight(engine,image_id)
    if info.get('image_id')!=image_id:raise OperationsRefused('backup requires exact resolved local image ID')
    stage=Path(tempfile.mkdtemp(prefix='.autoforge-db-backup-',dir=dest.parent));stage.chmod(0o700)
    try:
        script="import os,stat,sqlite3; p='/state/tasks.sqlite'; fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)); st=os.fstat(fd); assert stat.S_ISREG(st.st_mode) and st.st_nlink==1; src=sqlite3.connect('/proc/self/fd/'+str(fd)); dst=sqlite3.connect('/export/backup.sqlite'); src.backup(dst); dst.close(); src.close(); os.close(fd)"
        _run(engine,['run','--rm','--network=none','--read-only','--user=0:0','--cap-drop=ALL','--security-opt=no-new-privileges','--mount',f'type=volume,src={volume},dst=/state,readonly','--mount',f'type=bind,src={str(stage.resolve())},dst=/export','--entrypoint','python',image_id,'-I','-c',script],timeout=60)
        target=stage/'backup.sqlite'
        if not target.is_file() or target.is_symlink():raise OperationsRefused('verified container backup output missing')
        try:os.link(target,dest)
        except FileExistsError as exc:raise OperationsRefused('backup destination appeared during export') from exc
        return {'path':str(dest),'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'data_rollback':'not_claimed'}
    except Exception as exc:
        if isinstance(exc,OperationsRefused):raise
        raise OperationsRefused(f'database backup failed: {type(exc).__name__}: {str(exc)[:300]}') from exc
    finally:shutil.rmtree(stage,ignore_errors=True)
def _authorize(auth:dict[str,Any],root:Path,image_id:str)->None:
    required={'schema','approved','deployment_root','candidate_types','scopes','image_ids','environment','allow_irreversible_migrations','stop_conditions'}
    if not isinstance(auth,dict) or set(auth)!=required or auth.get('schema')!=1 or auth.get('approved') is not True:raise OperationsRefused('explicit release-scope authorization required')
    if not isinstance(auth['candidate_types'],list) or any(not isinstance(v,str) for v in auth['candidate_types']) or not isinstance(auth['scopes'],list) or any(not isinstance(v,str) for v in auth['scopes']) or not isinstance(auth['image_ids'],list) or any(not isinstance(v,str) for v in auth['image_ids']):raise OperationsRefused('authorization allowlists must be exact typed string lists')
    if auth['deployment_root']!=str(root.resolve()) or 'application' not in auth['candidate_types'] or not {'deploy','health_check','pointer_switch','rollback'}<=set(auth['scopes']):raise OperationsRefused('authorization root, type, or lifecycle scope mismatch')
    if image_id not in auth['image_ids'] or not auth['environment'] or type(auth['allow_irreversible_migrations']) is not bool:raise OperationsRefused('exact image/environment/data-migration scope not authorized')
    limits=auth['stop_conditions']
    if not isinstance(limits,dict) or set(limits)!={'max_error_rate','max_latency_ms_p50'} or type(limits['max_error_rate']) not in (int,float) or not 0<=limits['max_error_rate']<=1 or type(limits['max_latency_ms_p50']) not in (int,float) or limits['max_latency_ms_p50']<=0:raise OperationsRefused('authorized finite error/latency stop conditions required')
def _config(config:dict[str,Any],secret_names:set[str]|None=None)->tuple[dict[str,str],dict[str,str]]:
    if not isinstance(config,dict):raise OperationsRefused('runtime config must be a mapping')
    result={}
    for k,v in config.items():
        if not isinstance(k,str) or not k or not k.replace('_','').isalnum():raise OperationsRefused('runtime config names must be alphanumeric identifiers')
        if isinstance(v,dict):
            if set(v)!={'$secret_file'} or not isinstance(v['$secret_file'],str):raise OperationsRefused('runtime secret config must use an explicit secret-file reference')
            name=v['$secret_file'].rsplit('/',1)[-1] if v['$secret_file'].startswith('/run/autoforge/secrets/') else ''
            if not name or v['$secret_file']!='/run/autoforge/secrets/'+name or not secrets_module._NAME.fullmatch(name) or name not in (secret_names or set()):raise OperationsRefused('runtime config references an unavailable secret')
            result[k]=v['$secret_file']
        elif isinstance(v,(str,int,float,bool)):
            text=str(v)
            if '\x00' in k+text:raise OperationsRefused('runtime config contains NUL')
            result[k]=text
        else:raise OperationsRefused('runtime config must map names to scalar values or explicit secret-file references')
    return result,{}
def _measure(engine,cid):
    try:
        raw=_run(engine,['stats','--no-stream','--format','{{json .}}',cid],10)
        value=json.loads(raw)
        return {'memory':value.get('MemUsage'),'cpu_percent':value.get('CPUPerc'),'pids':value.get('PIDs')}
    except Exception as e:return {'status':'UNAVAILABLE','reason':str(e)}
def _health(tunnel,contract):
    try:
        c=http.client.HTTPConnection('127.0.0.1',tunnel.port,timeout=3); c.request('GET',contract['health_path']); r=c.getresponse(); r.read(1024*1024); status=r.status; c.close(); return status
    except (OSError,http.client.HTTPException):return None
def _probe(tunnel,contract):
    import application_service
    start=time.monotonic(); outcomes=[]; errors=0; latencies=[]
    for scenario in contract['scenarios']:
        ok=True
        for req in scenario['requests']:
            if builder.STOP_REQUESTED:raise OperationsRefused('operator STOP requested')
            t=time.monotonic()
            try:actual=application_service._request(tunnel.port,req['method'],req['path'],req.get('body'),req.get('headers')); passed=actual==req['expected']
            except Exception:passed=False
            latencies.append((time.monotonic()-t)*1000); errors+=not passed; ok &= passed
        outcomes.append({'name':scenario['name'],'pass':bool(ok)})
    ordered=sorted(latencies)
    return {'scenarios':outcomes,'requests':len(latencies),'error_rate':errors/len(latencies) if latencies else 0.0,'latency_ms_p50':ordered[(len(ordered)-1)//2] if ordered else 0.0,'elapsed_s':time.monotonic()-start,'business_pass':not errors}
def _record(root,request_id,value):
    try:request_id=delivery._request_id(request_id)
    except Exception as exc:raise OperationsRefused(str(exc)) from exc
    ledger=root/'.operations-ledger';ledger.mkdir(parents=True,exist_ok=True);target=ledger/(request_id+'.json')
    if target.exists():raise OperationsRefused('request already recorded; reconcile unknown side effects, never replay')
    for existing in ledger.glob('*.json'):
        prior=delivery._load(existing)
        if prior.get('state') in {'prepare','unknown'}:raise OperationsRefused(f'unresolved operation requires reconcile: {prior.get("request_id")}')
    checkpoint._atomic(target,checkpoint.canonical(value)+b'\n')
def deploy(pack:Path,root:Path,*,authorization:dict[str,Any],request_id:str,image_id:str,engine:str,command:list[str],contract:dict[str,Any],config:dict[str,Any],phase:str='shadow',stop_file:Path|None=None,fault_after:int|None=None,secret_values:dict[str,str]|None=None,private_root:Path|None=None,state_volume:str|None=None,migration_sql:str|None=None,expected_version:int|None=None,target_version:int|None=None)->dict[str,Any]:
    root=Path(root).absolute()
    if phase not in {'shadow','canary'}:raise OperationsRefused('invalid deployment stage')
    try:request_id=delivery._request_id(request_id)
    except Exception as exc:raise OperationsRefused(str(exc)) from exc
    _authorize(authorization,root,image_id)
    if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x or '\x00' in x for x in command):raise OperationsRefused('container command must be explicit argv')
    if (secret_values is None)!=(private_root is None):raise OperationsRefused('secret values and an explicit private root must be supplied together')
    if secret_values is not None:
        try:
            if not isinstance(secret_values,dict) or not secret_values:raise ValueError()
            for secret_name,value in secret_values.items():
                if not isinstance(secret_name,str) or not secrets_module._NAME.fullmatch(secret_name) or not isinstance(value,str) or not value or len(value.encode())>16384 or '\x00' in value:raise ValueError()
            private=Path(private_root)
            if private.is_symlink():raise ValueError()
            resolved=private.resolve(strict=True)
            if not resolved.is_dir() or resolved!=private.absolute():raise ValueError()
        except (OSError,ValueError,TypeError) as exc:raise OperationsRefused('explicit private root or secret configuration invalid') from exc
        secret_names=set(secret_values)
    else:secret_names=set()
    if secret_values is not None:
        try:
            observable=' '.join(command)+json.dumps(contract,ensure_ascii=False)+json.dumps(config,ensure_ascii=False)
            if any(value in observable for value in secret_values.values()):raise OperationsRefused('secret values must not appear in command or runtime configuration')
        except (TypeError,ValueError) as exc:
            if isinstance(exc,OperationsRefused):raise
            raise OperationsRefused('secret-bearing deployment configuration is invalid') from exc
    env_values,unused=_config(config,secret_names)
    if state_volume is not None:
        if not re.fullmatch(r'af-state-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}',state_volume):raise OperationsRefused('state volume must use the explicit AutoForge-owned name')
        if migration_sql is not None and not authorization['allow_irreversible_migrations']:raise OperationsRefused('authorization does not permit schema migration')
        env_values.setdefault('DB_PATH','/state/tasks.sqlite')
    elif migration_sql is not None or expected_version is not None or target_version is not None:raise OperationsRefused('database migration requires an explicit state volume')
    root.mkdir(parents=True,exist_ok=True)
    import application_service
    if not isinstance(contract,dict) or set(contract)!={'schema','health_path','scenarios'} or contract.get('schema')!=1:raise OperationsRefused('HTTP contract schema invalid')
    try:application_service._contract(contract)
    except Exception as exc:raise OperationsRefused(f'HTTP health/business contract invalid: {exc}') from exc
    packed=delivery.validate_pack(pack);info=builder.preflight(engine,image_id)
    if info.get('image_id')!=image_id:raise OperationsRefused('engine did not resolve the exact authorized image ID')
    with checkpoint.ExclusiveLock(root/'.delivery.lock'):
        ledger=root/'.operations-ledger'
        if (ledger/(request_id+'.json')).exists():raise OperationsRefused('request ID already exists; reconcile unknown external effects, never replay')
        if stop_file and stop_file.exists():raise OperationsRefused('operator STOP has highest priority')
        rev=packed['candidate_sha256']+'-'+request_id;name='af-release-'+uuid.uuid4().hex
        versions=root/'deployments';package=versions/rev
        if package.exists():raise OperationsRefused('immutable package revision already exists')
        secret_manifest=None;cid=None;tunnel=None;keep_container=False;database_result={'status':'NOT_CONFIGURED','data_rollback':'not_claimed'}
        if secret_values is not None:
            try:secret_manifest=secrets_module.prepare_runtime_secrets(secret_values,private_root=private_root,session_id=request_id,allowed_names=secret_names)
            except Exception as exc:raise OperationsRefused('could not prepare private runtime secrets') from exc
        try:
            helpername='af-migrate-'+uuid.uuid4().hex if state_volume is not None else None
            _record(root,request_id,{'schema':1,'request_id':request_id,'state':'prepare','effect':'copy_package_start_container_and_optional_database_migration','phase':phase,'revision':rev,'candidate_sha256':packed['candidate_sha256'],'image_id':image_id,'container_name':name,'state_volume':state_volume,'migration_helper_name':helpername,'created_at':time.time(),'secret_names':sorted(secret_names)})
            import shutil
            shutil.copytree(pack,package)
            installed=delivery.validate_pack(package)
            if installed['candidate_sha256']!=packed['candidate_sha256']:raise OperationsRefused('sealed package source changed during install')
            if state_volume is not None:
                labels=json.loads(_run(engine,['volume','inspect','--format','{{json .Labels}}',state_volume]))
                if labels.get(LABEL)!=OWNER or labels.get('io.omp.autoforge.state-root')!=hashlib.sha256(str(root.resolve()).encode()).hexdigest():raise OperationsRefused('state volume is not owned by this exact deployment root')
                if migration_sql is not None and not authorization['allow_irreversible_migrations']:raise OperationsRefused('authorization does not permit schema migration')
                database_result=_stateful_database(engine,image_id,state_volume,migration_sql,expected_version,target_version,root,helpername)
                database_result['status']='READY'
            elif migration_sql is not None or expected_version is not None or target_version is not None:raise OperationsRefused('database migration requires an explicit state volume')
            env=dict(env_values);env['PYTHONDONTWRITEBYTECODE']='1'
            argv=['run','-d','--name',name,'--label',f'{LABEL}={OWNER}','--label',f'io.omp.autoforge.revision={rev}','--network=none','--read-only','--user=0:0' if secret_manifest else '--user=65534:65534','--cap-drop=ALL',*( ['--cap-add=CHOWN','--cap-add=SETUID','--cap-add=SETGID','--cap-add=DAC_READ_SEARCH'] if secret_manifest else [] ),'--security-opt=no-new-privileges','--pids-limit=64','--memory=256m','--cpus=1','--mount',f'type=bind,src={str((package/"candidate").resolve())},dst=/candidate,readonly',*( ['--mount',f'type=volume,src={state_volume},dst=/state'] if state_volume else [] ),'--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777','--workdir','/candidate']
            for k,v in sorted(env.items()):argv += ['--env',f'{k}={v}']
            if secret_manifest:
                argv.extend(['--tmpfs','/run/autoforge:rw,noexec,nosuid,nodev,size=1m,mode=1777'])
                for item in secret_manifest['mounts']:argv.extend(['--mount',f"type=bind,src={item['source']},dst=/run/autoforge-staged/{item['name']},readonly"])
                mounts=[(item['name'],'/run/autoforge-staged/'+item['name']) for item in secret_manifest['mounts']]
                bootstrap="import os,sys; mounts=__MOUNTS__; dst='/run/autoforge/secrets'; os.makedirs(dst,mode=0o700,exist_ok=True);\nfor name,src in mounts:\n data=open(src,'rb').read(); target=dst+'/'+name\n with open(target,'xb') as f: f.write(data)\n os.chmod(target,0o400); os.chown(target,65534,65534)\nopen('/run/autoforge/ready','w').close(); os.chmod(dst,0o500); os.chown(dst,65534,65534); os.setgid(65534); os.setuid(65534); os.execvp(sys.argv[1],sys.argv[1:])".replace('__MOUNTS__',repr(mounts))
                argv.extend(['--entrypoint','python',image_id,'-I','-c',bootstrap,*command])
            else:argv.extend(['--entrypoint',command[0],image_id,*command[1:]])
            cid=_run(engine,argv).splitlines()[0]
            labels=json.loads(_run(engine,['inspect','--format','{{json .Config.Labels}}',cid]))
            if labels.get(LABEL)!=OWNER:raise OperationsRefused('container ownership label readback mismatch')
            if secret_manifest:
                ready_deadline=time.monotonic()+15
                while time.monotonic()<ready_deadline:
                    if _run(engine,['inspect','--format','{{.State.Running}}',cid])!='true':raise OperationsRefused('secret bootstrap exited before candidate start')
                    if _run(engine,['exec',cid,'python','-I','-c',"import os;print('ready' if os.path.isfile('/run/autoforge/ready') else 'waiting')"])=='ready':break
                    time.sleep(.1)
                else:raise OperationsRefused('secret bootstrap did not finish safe copy')
                try:secrets_module.cleanup_runtime_secrets(secret_manifest,audit_path=root/'secret-cleanup.jsonl')
                except Exception as exc:raise OperationsRefused('private source cleanup failed after container safe copy') from exc
                secret_manifest=None
            tunnel=Tunnel(engine,cid,{'PATH':os.environ.get('PATH','')});deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                if stop_file and stop_file.exists():raise OperationsRefused('operator STOP requested')
                if _health(tunnel,contract)==200:break
                if _run(engine,['inspect','--format','{{.State.Running}}',cid])!='true':break
                time.sleep(.25)
            else:raise OperationsRefused('HTTP readiness timeout')
            if _health(tunnel,contract)!=200:raise OperationsRefused('HTTP readiness failed')
            if fault_after is not None:
                if type(fault_after) is not int or fault_after<0:raise OperationsRefused('fault-after must be nonnegative')
                _run(engine,['kill','--signal','KILL',cid])
            evidence=_probe(tunnel,contract);resources=_measure(engine,cid);limits=authorization['stop_conditions']
            if evidence['error_rate']>limits['max_error_rate'] or evidence['latency_ms_p50']>limits['max_latency_ms_p50']:
                evidence['business_pass']=False;evidence['stop_reason']='configured_error_or_latency_threshold'
            if not evidence['business_pass']:
                status='ROLLED_BACK' if phase=='canary' else 'STOPPED';rec=delivery._load(ledger/(request_id+'.json'));rec.update({'state':'complete','status':status,'measurements':evidence,'resources':resources,'fault_injection':'post-readiness container kill' if fault_after is not None else None,'updated_at':time.time()});delivery._write_json(ledger/(request_id+'.json'),rec)
                raise OperationsRefused('configured release stop condition triggered; candidate stopped, active pointer unchanged')
            rec=delivery._load(ledger/(request_id+'.json'));rec.update({'state':'complete','status':phase.upper(),'container_id':cid,'command':command,'contract':contract,'measurements':evidence,'resources':resources,'config_sha256':checkpoint.digest(checkpoint.canonical(env)),'state_volume':state_volume,'database':database_result,'data_rollback':'not_claimed','created_at':time.time(),'updated_at':time.time()});delivery._write_json(ledger/(request_id+'.json'),rec)
            return {'status':phase.upper(),'revision':rev,'container_id':cid,'measurements':evidence,'resources':resources,'database':database_result,'data_rollback':'not_claimed'}
        except BaseException as exc:
            if tunnel is not None:tunnel.close()
            if cid and not keep_container:
                try:
                    own=json.loads(_run(engine,['inspect','--format','{{json .Config.Labels}}',cid])).get(LABEL)==OWNER
                    if own:_run(engine,['rm','-f',cid])
                except Exception:pass
            if secret_manifest:
                try:secrets_module.cleanup_runtime_secrets(secret_manifest,audit_path=root/'secret-cleanup.jsonl')
                except Exception:pass
            if isinstance(exc,Exception) and secret_values:
                message=str(exc)
                for value in secret_values.values():message=message.replace(value,'[REDACTED]')
                raise OperationsRefused(message) from None
            raise
        finally:
            if tunnel is not None:tunnel.close()
def reconcile(root:Path,request_id:str,*,engine:str,evidence:dict[str,Any])->dict[str,Any]:
    root=Path(root).absolute();request_id=delivery._request_id(request_id)
    with checkpoint.ExclusiveLock(root/'.delivery.lock'):
        ledger=root/'.operations-ledger';record=delivery._load(ledger/(request_id+'.json'))
        if record.get('state') not in {'prepare','unknown'}:raise OperationsRefused('only incomplete operations requests can be reconciled')
        if not isinstance(evidence,dict) or evidence.get('request_id')!=request_id or not isinstance(evidence.get('evidence_id'),str) or not evidence['evidence_id'].strip():raise OperationsRefused('operator reconciliation evidence required')
        helper_name=record.get('migration_helper_name')
        names=[]
        for candidate_name in (record.get('migration_helper_name'),record.get('container_name')):
            if candidate_name and candidate_name not in names:names.append(candidate_name)
        observed=[]
        for name in names:
            try:
                found=_run(engine,['ps','-aq','--filter',f'name=^{name}$'])
                if not found:continue
                cid=found.splitlines()[0];info=json.loads(_run(engine,['inspect','--format','{{json .}}',cid]))
                if info.get('Config',{}).get('Labels',{}).get(LABEL)!=OWNER:raise OperationsRefused('recorded name belongs to an unowned container')
                state=info.get('State',{}).get('Status');observed.append({'container_id':cid,'name':name,'state':state,'owned':True})
                if evidence.get('stop_owned_container') is True:
                    _run(engine,['rm','-f',cid])
                    if _run(engine,['ps','-aq','--filter',f'name=^{name}$']):raise OperationsRefused('owned container cleanup not verified')
                    observed[-1]['state']='removed'
                elif state not in {'exited','dead'}:raise OperationsRefused('operation container is not terminal; stop all owned side effects to reconcile')
            except OperationsRefused:raise
            except Exception as exc:raise OperationsRefused(f'reconciliation failed; side effect remains unknown ({type(exc).__name__})') from exc
        if record.get('migration_helper_name') and not evidence.get('database_outcome_evidence_id'):raise OperationsRefused('database migration outcome evidence required for reconciliation')
        record.update({'state':'reconciled','reconciliation':{'evidence_id':evidence['evidence_id'],'database_outcome_evidence_id':evidence.get('database_outcome_evidence_id'),'observed':observed},'updated_at':time.time()})
        delivery._write_json(ledger/(request_id+'.json'),record)
        return record

def transition(root:Path,*,authorization:dict[str,Any],image_id:str,engine:str,action:str,request_id:str,stop_file:Path|None=None)->dict[str,Any]:
    root=Path(root).absolute(); _authorize(authorization,root,image_id)
    if action not in {'promote','rollback'}:raise OperationsRefused('transition must promote or rollback')
    with checkpoint.ExclusiveLock(root/'.delivery.lock'):
        if stop_file and stop_file.exists():raise OperationsRefused('operator STOP has highest priority')
        records=[];ledger=root/'.operations-ledger'
        if (ledger/(request_id+'.json')).exists():raise OperationsRefused('request ID already exists; reconcile rather than replay')
        if ledger.exists():
            for p in sorted(ledger.glob('*.json')):
                item=delivery._load(p)
                if item.get('state')=='complete':records.append(item)
        pointer=root/'active.json';prior=delivery._load(pointer) if pointer.exists() else None
        if action=='rollback':
            if not prior or not prior.get('previous'):raise OperationsRefused('no previous revision to restore')
            revision=prior['previous']
            candidates=[r for r in records if r.get('revision')==revision and r.get('image_id')==image_id]
        else:
            candidates=[r for r in records if r.get('phase') in {'shadow','canary'} and r.get('image_id')==image_id and r.get('status') in {'SHADOW','CANARY'}]
            if not candidates:raise OperationsRefused('no successful authorized shadow/canary revision')
            revision=max(candidates,key=lambda x:x['created_at'])['revision']
            candidates=[r for r in records if r.get('revision')==revision and r.get('image_id')==image_id]
        source=next(iter(candidates),None)
        if source is None or not source.get('container_id'):raise OperationsRefused('target exact-image runtime is not owned and available')
        if source.get('state_volume'):
            labels=json.loads(_run(engine,['volume','inspect','--format','{{json .Labels}}',source['state_volume']]))
            scope=hashlib.sha256(str(root.resolve()).encode()).hexdigest()
            if labels.get(LABEL)!=OWNER or labels.get('io.omp.autoforge.state-root')!=scope:raise OperationsRefused('state volume ownership changed from exact deployment root')
        package=root/'deployments'/revision
        if delivery.validate_pack(package)['candidate_sha256']!=source['candidate_sha256']:raise OperationsRefused('sealed release package integrity failed')
        if source.get('secret_names'):
            # Private tmpfs belongs to the still-running authorized revision. Never
            # rebuild it without credentials or retain raw values for re-creation.
            live=source['container_id']
            info=json.loads(_run(engine,['inspect','--format','{{json .}}',live]))
            if info.get('Config',{}).get('Labels',{}).get(LABEL)!=OWNER or info.get('Config',{}).get('Labels',{}).get('io.omp.autoforge.revision')!=revision or info.get('Image')!=image_id or not info.get('State',{}).get('Running'):
                raise OperationsRefused('secret-bearing target runtime is not the live exact owned revision')
            tunnel=Tunnel(engine,live,{'PATH':os.environ.get('PATH','')})
            try:
                if _health(tunnel,source['contract'])!=200:raise OperationsRefused('secret-bearing target health check failed; pointer unchanged')
                proof=_probe(tunnel,source['contract'])
            finally:tunnel.close()
            if not proof['business_pass']:raise OperationsRefused('secret-bearing target business check failed; pointer unchanged')
            if stop_file and stop_file.exists():raise OperationsRefused('operator STOP requested before pointer switch')
            _record(root,request_id,{'schema':1,'request_id':request_id,'state':'prepare','effect':'live_private_target_health_then_pointer_switch','action':action,'target_revision':revision,'prior':prior,'created_at':time.time()})
            delivery._write_json(pointer,{'schema':1,'revision':revision,'candidate_sha256':source['candidate_sha256'],'previous':prior.get('revision') if prior else None,'request_id':request_id,'code_config_only':True,'data_rollback':'not claimed; secrets re-provisioning required after runtime loss'})
            rec=delivery._load(ledger/(request_id+'.json'));rec.update({'state':'complete','revision':revision,'health_probe':proof,'updated_at':time.time()});delivery._write_json(ledger/(request_id+'.json'),rec)
            return {'status':'PROMOTED' if action=='promote' else 'ROLLED_BACK','revision':revision,'health_probe':proof,'data_rollback':'not_claimed','secret_runtime':'existing owned tmpfs; no credential replay'}
        checkname='af-release-check-'+uuid.uuid4().hex
        cmd=source.get('command',['python','-I','-u','/candidate/main.py'])
        checkargv=['run','-d','--name',checkname,'--label',f'{LABEL}={OWNER}','--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=64','--memory=256m','--cpus=1','--mount',f'type=bind,src={str((package/"candidate").resolve())},dst=/candidate,readonly',*( ['--mount',f'type=volume,src={source["state_volume"]},dst=/state'] if source.get('state_volume') else [] ),'--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777','--workdir','/candidate','--entrypoint',cmd[0],image_id,*cmd[1:]]
        checkcid=None
        _record(root,request_id,{'schema':1,'request_id':request_id,'state':'prepare','effect':'target_health_check_then_pointer_switch','action':action,'target_revision':revision,'prior':prior,'container_name':checkname,'created_at':time.time()})
        try:
            checkcid=_run(engine,checkargv).splitlines()[0]
            checklabels=json.loads(_run(engine,['inspect','--format','{{json .Config.Labels}}',checkcid]))
            if checklabels.get(LABEL)!=OWNER:raise OperationsRefused('verification container ownership mismatch')
            tunnel=Tunnel(engine,checkcid,{'PATH':os.environ.get('PATH','')})
            try:
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    if stop_file and stop_file.exists():raise OperationsRefused('operator STOP requested')
                    if _health(tunnel,source['contract'])==200:break
                    time.sleep(.25)
                else:raise OperationsRefused('target revision health check failed; pointer unchanged')
                proof=_probe(tunnel,source['contract'])
            finally:tunnel.close()
            if not proof['business_pass']:raise OperationsRefused('target business invariant probe failed; pointer unchanged')
            if stop_file and stop_file.exists():raise OperationsRefused('operator STOP requested before pointer switch')
            pointer_value={'schema':1,'revision':revision,'candidate_sha256':source['candidate_sha256'],'previous':prior.get('revision') if prior else None,'request_id':request_id,'code_config_only':True,'data_rollback':'not claimed; irreversible migrations require separate operator recovery'}
            delivery._write_json(pointer,pointer_value)
            rec=delivery._load(ledger/(request_id+'.json'));rec.update({'state':'complete','revision':revision,'health_probe':proof,'updated_at':time.time()});delivery._write_json(ledger/(request_id+'.json'),rec)
            return {'status':'PROMOTED' if action=='promote' else 'ROLLED_BACK','revision':revision,'health_probe':proof,'data_rollback':'not_claimed'}
        except BaseException as exc:
            try:
                if checkcid:
                    labels=json.loads(_run(engine,['inspect','--format','{{json .Config.Labels}}',checkcid]))
                    if labels.get(LABEL)==OWNER:_run(engine,['rm','-f',checkcid])
                rec=delivery._load(ledger/(request_id+'.json'));rec.update({'state':'unknown','status':'STOPPED_OR_FAILED','error_type':type(exc).__name__,'updated_at':time.time()});delivery._write_json(ledger/(request_id+'.json'),rec)
            except Exception:pass
            raise
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); s=p.add_subparsers(dest='cmd',required=True)
    q=s.add_parser('deploy');q.add_argument('pack',type=Path);q.add_argument('root',type=Path);q.add_argument('authorization',type=Path);q.add_argument('contract',type=Path);q.add_argument('config',type=Path);q.add_argument('--request-id',required=True);q.add_argument('--image-id',required=True);q.add_argument('--engine',choices=('docker',),required=True);q.add_argument('--command-json',required=True);q.add_argument('--phase',choices=('shadow','canary'),default='shadow');q.add_argument('--stop-file',type=Path);q.add_argument('--fault-after',type=int)
    q.add_argument('--state-volume');q.add_argument('--migration-sql',type=Path);q.add_argument('--expected-version',type=int);q.add_argument('--target-version',type=int)
    q.add_argument('--private-secret-values',type=Path,help='explicit operator-private JSON mapping; values never appear in argv or reports');q.add_argument('--private-root',type=Path)
    q=s.add_parser('reconcile');q.add_argument('root',type=Path);q.add_argument('request_id');q.add_argument('evidence',type=Path);q.add_argument('--engine',choices=['docker'],required=True)
    q=s.add_parser('transition');q.add_argument('root',type=Path);q.add_argument('authorization',type=Path);q.add_argument('--image-id',required=True);q.add_argument('--engine',choices=('docker',),required=True);q.add_argument('--action',choices=('promote','rollback'),required=True);q.add_argument('--request-id',required=True);q.add_argument('--stop-file',type=Path)
    q=s.add_parser('database-backup');q.add_argument('root',type=Path);q.add_argument('authorization',type=Path);q.add_argument('--image-id',required=True);q.add_argument('--engine',choices=('docker',),required=True);q.add_argument('--state-volume',required=True);q.add_argument('--output',type=Path,required=True)
    a=p.parse_args(argv)
    try:
        if a.cmd=='database-backup':
            auth=delivery._load(a.authorization);_authorize(auth,a.root.absolute(),a.image_id)
            out=_backup_state(a.engine,a.image_id,a.state_volume,a.output,a.root)
        elif a.cmd=='reconcile':out=reconcile(a.root,a.request_id,engine=a.engine,evidence=delivery._load(a.evidence))
        else:
            auth=delivery._load(a.authorization)
            if a.cmd=='deploy':out=deploy(a.pack,a.root,authorization=auth,request_id=a.request_id,image_id=a.image_id,engine=a.engine,command=json.loads(a.command_json),contract=delivery._load(a.contract),config=delivery._load(a.config),phase=a.phase,stop_file=a.stop_file,fault_after=a.fault_after,secret_values=delivery._load(a.private_secret_values) if a.private_secret_values else None,private_root=a.private_root,state_volume=a.state_volume,migration_sql=a.migration_sql.read_text(encoding='utf-8') if a.migration_sql else None,expected_version=a.expected_version,target_version=a.target_version)
            else:out=transition(a.root,authorization=auth,image_id=a.image_id,engine=a.engine,action=a.action,request_id=a.request_id,stop_file=a.stop_file)
        print(json.dumps(out,sort_keys=True));return 0
    except (OperationsRefused,delivery.DeliveryRefused,checkpoint.ResumeRefused,OSError,ValueError) as e:print(f'REFUSED: {e}',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
