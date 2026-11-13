#!/usr/bin/env python3
"""Run real, bounded container security and application acceptance checks."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, hashlib, json, os, subprocess, sys, tempfile, time, uuid
from pathlib import Path
import application_builder as builder
import application_service as service
import application_database as database
import container_env
ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'fixtures'/'container_acceptance'
def canon(x): return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def command(argv,timeout=12):
    p=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False,env=container_env.engine_env(),close_fds=True)
    if len(p.stdout)>65536 or len(p.stderr)>65536: raise RuntimeError('engine command response exceeded limit')
    return p
def manifest(path):
    files=[]
    for p in sorted(path.rglob('*')):
        if p.is_file() and p.name!='manifest.json':
            data=p.read_bytes();files.append({'path':p.relative_to(path).as_posix(),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    base={'schema':1,'goal':'trusted acceptance fixture','references':[],'files':files};digest=hashlib.sha256(canon(base)).hexdigest()
    raw={**base,'candidate_sha256':digest,'workspace':str(path.resolve())};(path/'manifest.json').write_bytes(canon(raw))
    return {'schema':1,'workspace_manifest':raw,'candidate_sha256':digest}
class CheckFailure(RuntimeError):pass
def security(engine,image,host_canary):
    name='autoforge-sec-'+uuid.uuid4().hex; root=Path(tempfile.mkdtemp(prefix='autoforge-mount-'))
    code="import os,socket,json; d={'uid':os.getuid(),'caps':open('/proc/self/status').read().split('CapEff:\\t')[1].splitlines()[0]};\ntry:open('/candidate/pwn','w').write('x');d['source_write']=True\nexcept OSError:d['source_write']=False\ntry:open('/host-secret-canary').read();d['host_secret']=True\nexcept OSError:d['host_secret']=False\ntry:socket.create_connection(('1.1.1.1',443),timeout=1);d['internet']=True\nexcept OSError:d['internet']=False\nprint(json.dumps(d))"
    argv=[engine,'run','--rm','--name',name,'--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=64','--memory=256m','--cpus=1','--mount',f'type=bind,src={root},dst=/candidate,readonly','--entrypoint','python',image,'-I','-c',code]
    try:
        with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
            proc=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=container_env.engine_env(),close_fds=True)
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                command([engine,'kill',name],5);proc.kill();proc.wait(timeout=3);raise CheckFailure('security probe timed out')
            out.seek(0,os.SEEK_END)
            if out.tell()>65536:raise CheckFailure('security probe output exceeded limit')
            out.seek(0);payload=out.read(65537)
            p=type('Completed',(),{'stdout':payload,'returncode':proc.returncode})()
    finally:
        command([engine,'rm','-f',name],6);root.rmdir()
    if p.returncode:raise CheckFailure('security probe exited unsuccessfully')
    try:facts=json.loads(p.stdout.decode('utf-8'))
    except Exception as exc:raise CheckFailure('invalid security probe output') from exc
    if facts.get('uid')==0 or facts.get('caps')!='0000000000000000' or facts.get('source_write') or facts.get('host_secret') or facts.get('internet'):raise CheckFailure('security boundary assertion failed')
    checks={}
    # Output flood uses the builder's actual bounded capture path.
    flood=Path(tempfile.mkdtemp(prefix='autoforge-output-'))
    # UID 65534 must traverse the mount source; mkdtemp defaults to 0700.
    os.chmod(flood,0o755)
    try:
        (flood/'main.py').write_text('import sys'+chr(10)+'for _ in sys.stdin: print("x"*1048576,flush=True)'+chr(10),encoding='utf-8')
        out,err,code,timed=builder._container(engine,image,flood,b'{"op":"x"}'+bytes([10]),5)
        checks['output']={'outcome':'bounded' if timed else 'completed','stdout_bytes':len(out),'stderr_bytes':len(err),'exit_code':code}
        if not timed or len(out)!=builder.MAX_OUT or len(err)>builder.MAX_ERR:raise CheckFailure('output flood did not reach and respect host capture bounds')
    finally:
        for child in flood.iterdir():child.unlink()
        flood.rmdir()
    for label,src,limit in [('timeout','import time;time.sleep(30)',1),('pids',"import os,json; p=[]\nfor i in range(100):\n try:p.append(os.fork())\n except OSError:break\n if p[-1]==0:os._exit(0)\nfor pid in p:os.waitpid(pid,0)\nprint(json.dumps({'created':len(p)}))",5)]:
        n='autoforge-sec-'+uuid.uuid4().hex;argv=[engine,'run','--name',n,'--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=64','--memory=256m','--cpus=1','--entrypoint','python',image,'-I','-c',src];start=time.monotonic();outcome='completed';size=0;payload=b''
        try:
            with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
                proc=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=out,stderr=err,env=container_env.engine_env(),close_fds=True)
                try:proc.wait(timeout=limit)
                except subprocess.TimeoutExpired:outcome='timeout';command([engine,'kill',n],5);proc.kill();proc.wait(timeout=3)
                out.seek(0,os.SEEK_END);size=out.tell();out.seek(0);payload=out.read(1024)
                if label=='pids':
                    try:created=json.loads(payload).get('created',101)
                    except Exception:created=101
                    if not 1<=created<64:outcome='pids_limit_not_applied'
        finally:command([engine,'rm','-f',n],6)
        checks[label]={'outcome':outcome,'elapsed_seconds':round(time.monotonic()-start,3),'output_bytes':size,'observed_forks':created if label=='pids' else None}
    if checks['timeout']['outcome']!='timeout' or checks['output']['outcome']!='bounded' or checks['pids']['outcome']!='completed':raise CheckFailure('resource boundary assertion failed')
    return {'status':'PASS','observed':facts,'resources':checks,'canary':'known unmounted path unreadable; not proof of all host-secret isolation'}
def orphans(engine):
    p=command([engine,'ps','-a','--filter','name=autoforge-sec-','--format','{{.Names}}'],8)
    if p.returncode:raise CheckFailure('cannot inspect security-probe cleanup')
    remaining=[x for x in p.stdout.decode().splitlines() if x.startswith('autoforge-sec-')]
    return {'status':'PASS' if not remaining else 'FAIL','remaining':remaining}
def save(output,report):
    rp=output/'acceptance.json';rp.write_bytes(canon(report)+b'\n')
    fixtures={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(FIXTURES.iterdir()) if p.is_file()}
    (output/'manifest.json').write_bytes(canon({'schema':1,'report':'acceptance.json','sha256':hashlib.sha256(rp.read_bytes()).hexdigest(),'fixtures':fixtures})+b'\n')
def run(a):
    if not a.output.is_absolute():raise ValueError('--output must be absolute')
    if a.output.exists():raise ValueError('--output must be absent')
    a.output.mkdir(parents=True)
    report={'schema':1,'status':'HOLD','engine':a.engine,'checks':{}}
    try:
        info=builder.preflight(a.engine,a.python_image);report['python_image']=info['image_id']
        if a.postgres_image:database.postgres_preflight(a.engine,a.postgres_image)
    except builder.MissingPrerequisite as exc:
        report.update(status='BLOCKED',reason=str(exc),created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()));save(a.output,report);return report
    except (builder.BuildFailure,OSError) as exc:
        report.update(status='HOLD',reason=str(exc),created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()));save(a.output,report);return report
    try:
        with tempfile.TemporaryDirectory(prefix='autoforge-accept-') as td:
            tmp=Path(td);secret=tmp/'host-only-secret';secret.write_text('HOST_SECRET_CANARY_'+uuid.uuid4().hex,encoding='ascii');secret.chmod(0o600)
            report['checks']['security']=security(a.engine,a.python_image,secret);report['checks']['orphan_inspect']=orphans(a.engine)
            fixed=json.loads((ROOT/'fixtures/business/contract.json').read_text(encoding='utf-8'))
            bad=tmp/'wrong';bad.mkdir();(bad/'main.py').write_text('import sys\nfor _ in sys.stdin: print("{}")\n',encoding='utf-8');bm=manifest(bad);br=builder.build(bm,fixed,a.engine,a.python_image,info)
            good=tmp/'correct';good.mkdir();(good/'main.py').write_bytes((ROOT/'fixtures/business/main.py').read_bytes());gm=manifest(good);gr=builder.build(gm,fixed,a.engine,a.python_image,info)
            report['checks']['inventory']={'wrong_rejected':not br['test_pass'],'correct_pass':gr['build_pass'] and gr['test_pass'],'oracle':'trusted fixed fixture contract, not model proof','wrong_sha256':bm['candidate_sha256'],'correct_sha256':gm['candidate_sha256']}
            if not report['checks']['inventory']['wrong_rejected'] or not report['checks']['inventory']['correct_pass']:raise CheckFailure('inventory contract acceptance failed')
            http=tmp/'http';http.mkdir();(http/'main.py').write_bytes((FIXTURES/'http_app.py').read_bytes());hm=manifest(http);hc=json.loads((FIXTURES/'http_contract.json').read_text(encoding='utf-8'))
            secret_mounts=[]
            for secret_name,secret_value in [('FIXTURE_AUTH_TOKEN','fixture-token-alpha'),('OTHER_TENANT_TOKEN','fixture-token-beta'),('READ_ONLY_TOKEN','fixture-token-readonly')]:
                secret_path=tmp/('secret-'+secret_name);secret_path.write_text(secret_value,encoding='utf-8');secret_path.chmod(0o600)
                secret_mounts.append({'source':str(secret_path),'target':'/run/autoforge/secrets/'+secret_name,'read_only':True,'name':secret_name,'sha256':hashlib.sha256(secret_path.read_bytes()).hexdigest()})
            hr=service.run(hm['workspace_manifest'],hc,a.engine,a.python_image,['python','-I','/candidate/main.py'],timeout=30,secret_mounts=secret_mounts)
            log=hr.pop('logs',b'');log=log.encode('utf-8') if isinstance(log,str) else log
            hr['logs']={'sha256':hashlib.sha256(log).hexdigest(),'bytes':len(log)};report['checks']['http_service']=hr
            if not hr.get('build_pass') or not hr.get('test_pass'):raise CheckFailure('HTTP auth/CRUD/pagination/idempotency/revoke contract failed')
            if a.postgres_image:
                h=database.postgres_start(a.engine,a.postgres_image)
                try:report['checks']['postgresql']=database.postgres_accept(a.engine,h,a.migration,a.rollback)
                finally:database.postgres_stop(a.engine,h['container'],h.get('network'),h.get('volume'))
        report['checks']['orphan_inspect_final']=orphans(a.engine)
        if report['checks']['orphan_inspect_final']['status']!='PASS':raise CheckFailure('orphan container detected')
        report['status']='PASS'
    except Exception as exc:report['status']='HOLD';report['reason']=str(exc)
    report['created_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());save(a.output,report);return report
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--engine',choices=['docker'],required=True);p.add_argument('--python-image',required=True);p.add_argument('--postgres-image');p.add_argument('--output',type=Path,required=True);p.add_argument('--migration',default='CREATE TABLE acceptance (id INTEGER PRIMARY KEY);');p.add_argument('--rollback',default='DROP TABLE acceptance;')
    a=p.parse_args()
    try:
        r=run(a);print(json.dumps({'status':r['status'],'output':str(a.output.resolve()),'checks':sorted(r['checks'])},sort_keys=True));return 0 if r['status']=='PASS' else (3 if r['status']=='BLOCKED' else 2)
    except builder.MissingPrerequisite as exc:print(json.dumps({'status':'BLOCKED','reason':str(exc)},file=sys.stderr));return 3
    except (ValueError,OSError,builder.BuildFailure) as exc:print(json.dumps({'status':'HOLD','reason':str(exc)},sort_keys=True),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
