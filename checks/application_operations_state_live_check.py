"""Opt-in operations lifecycle smoke on an unchanged retained generated Web app.

Authorization is operator supplied and bound to the exact deployment root/image.
The local image digest identifies the image; it is not a safety approval.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, hashlib, json, shutil, tempfile, uuid
from pathlib import Path
import application_delivery as delivery
import application_operations as ops

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATE=ROOT/'bootstrap-runs'/'generated-web-extended-ce8932caad84'/'candidate'
DEFAULT_EVIDENCE=ROOT/'bootstrap-runs'/'generated-web-extended-ce8932caad84'/'verification.json'

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--engine',choices=['docker'],required=True);p.add_argument('--image-id',required=True)
    p.add_argument('--deployment-root',type=Path,required=True);p.add_argument('--authorization',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--candidate',type=Path,default=DEFAULT_CANDIDATE)
    p.add_argument('--run-id',default=uuid.uuid4().hex);a=p.parse_args(argv)
    root=a.deployment_root.absolute()
    if not root.is_absolute() or not a.output.is_absolute():raise SystemExit('deployment root and output must be absolute')
    auth=delivery._load(a.authorization)
    if auth.get('deployment_root')!=str(root.resolve()) or a.image_id not in auth.get('image_ids',[]) or auth.get('approved') is not True:raise SystemExit('operator authorization must bind exact root and image')
    if not auth.get('allow_irreversible_migrations') or not {'deploy','health_check','pointer_switch','rollback'}<=set(auth.get('scopes',[])):raise SystemExit('authorization lacks migration or lifecycle scope')
    if ops.builder.preflight(a.engine,a.image_id).get('image_id')!=a.image_id:raise SystemExit('container engine did not resolve exact image ID')
    candidate=a.candidate.resolve(strict=True)
    if not (candidate/'manifest.json').is_file() or 'DB_PATH = "/state/tasks.sqlite"' not in (candidate/'db.py').read_text(encoding='utf-8'):raise SystemExit('candidate is not compatible retained generated Web app')
    _,_,digest=delivery._read_candidate(candidate)
    prior=delivery._load(DEFAULT_EVIDENCE)
    if prior.get('candidate_sha256')!=digest or prior.get('build_pass') is not True or prior.get('test_pass') is not True:raise SystemExit('existing generated candidate evidence is absent or mismatched')
    volume='af-state-'+''.join(c for c in a.run_id if c.isalnum())[:48]
    with tempfile.TemporaryDirectory(prefix='autoforge-generated-web-ops-') as td:
        base=Path(td);staged=base/'candidate';shutil.copytree(candidate,staged)
        if delivery._read_candidate(staged)[2]!=digest:raise AssertionError('candidate digest changed while staging')
        evidence_hash=hashlib.sha256(DEFAULT_EVIDENCE.read_bytes()).hexdigest()
        validation={'schema':1,'status':'PASS','candidate_sha256':digest,'validator':'existing-generated-web-verification','source_evidence_sha256':evidence_hash,'scope':'prior generated-candidate result; deployment operations exercised separately'}
        pack=base/'pack';delivery.create_pack(staged,validation,{'schema':1,'dependencies':[],'lock_sha256':hashlib.sha256(b'').hexdigest()},{'engine':'docker','image_id':a.image_id,'source':'operator exact local image'},pack)
        contract={'schema':1,'health_path':'/health','scenarios':[{'name':'generated-health','requests':[{'method':'GET','path':'/health','body':None,'expected':{'status':200,'body':{'ok':True}}}]}]}
        command=['python','-I','-u','-c',"import runpy,sys;sys.path.insert(0,'/candidate');runpy.run_path('/candidate/main.py',run_name='__main__')"];migration='CREATE TABLE IF NOT EXISTS migration_probe(value TEXT NOT NULL);'
        secrets={'ADMIN':uuid.uuid4().hex+'-admin','READER':uuid.uuid4().hex+'-reader'}
        private=root.parent/('autoforge-private-'+a.run_id);private.mkdir(mode=0o700);private.chmod(0o700)
        deployment=ops.deploy(pack,root,authorization=auth,request_id=a.run_id+'-deploy',image_id=a.image_id,engine=a.engine,command=command,contract=contract,config={},phase='shadow',secret_values=secrets,private_root=private,state_volume=volume)
        tunnel=ops.Tunnel(a.engine,deployment['container_id'],{'PATH':__import__('os').environ.get('PATH','')})
        try:
            from application_service import _request
            headers={'Authorization':'Bearer '+secrets['ADMIN'],'Content-Type':'application/json'}
            created=_request(tunnel.port,'POST','/api/tasks',{'title':'operations-persisted'},headers)
            listed=_request(tunnel.port,'GET','/api/tasks',None,headers)
            if created!={'status':201,'body':{'id':1,'title':'operations-persisted'}} or listed!={'status':200,'body':{'items':[{'id':1,'title':'operations-persisted'}]}}:raise AssertionError(f'generated authenticated write/read failed: {created!r} {listed!r}')
        finally:tunnel.close()
        ops._run(a.engine,['rm','-f',deployment['container_id']])
        backup=ops._backup_state(a.engine,a.image_id,volume,root.parent/(a.run_id+'-backup.sqlite'),root)
        try:ops._stateful_database(a.engine,a.image_id,volume,'CREATE TABLE tasks (id INTEGER);',0,1,root,'af-migrate-'+uuid.uuid4().hex)
        except ops.OperationsRefused as exc:
            if 'hash-verified' not in str(exc):raise
        migrated=ops._stateful_database(a.engine,a.image_id,volume,migration,0,1,root,'af-migrate-'+uuid.uuid4().hex)
        if migrated.get('schema_version')!=1:raise AssertionError('migration did not update database schema version')
        baseline_private=root.parent/('autoforge-private-'+a.run_id+'-baseline');baseline_private.mkdir(mode=0o700);baseline_private.chmod(0o700)
        baseline=ops.deploy(pack,root,authorization=auth,request_id=a.run_id+'-baseline',image_id=a.image_id,engine=a.engine,command=command,contract=contract,config={},phase='shadow',secret_values=secrets,private_root=baseline_private,state_volume=volume)
        baseline_promoted=ops.transition(root,authorization=auth,image_id=a.image_id,engine=a.engine,action='promote',request_id=a.run_id+'-baseline-promote')
        restart_private=root.parent/('autoforge-private-'+a.run_id+'-restart');restart_private.mkdir(mode=0o700);restart_private.chmod(0o700)
        persisted=ops.deploy(pack,root,authorization=auth,request_id=a.run_id+'-restart',image_id=a.image_id,engine=a.engine,command=command,contract=contract,config={},phase='shadow',secret_values=secrets,private_root=restart_private,state_volume=volume)
        tunnel=ops.Tunnel(a.engine,persisted['container_id'],{'PATH':__import__('os').environ.get('PATH','')})
        try:
            actual=_request(tunnel.port,'GET','/api/tasks',None,{'Authorization':'Bearer '+secrets['ADMIN']})
            if actual!={'status':200,'body':{'items':[{'id':1,'title':'operations-persisted'}]}}:raise AssertionError(f'generated data did not persist across restart: {actual!r}')
        finally:tunnel.close()
        ops._run(a.engine,['rm','-f',persisted['container_id']])
        persisted_private=root.parent/('autoforge-private-'+a.run_id+'-persisted');persisted_private.mkdir(mode=0o700);persisted_private.chmod(0o700)
        persisted=ops.deploy(pack,root,authorization=auth,request_id=a.run_id+'-persisted',image_id=a.image_id,engine=a.engine,command=command,contract=contract,config={},phase='shadow',secret_values=secrets,private_root=persisted_private,state_volume=volume)
        before=json.loads((root/'active.json').read_text(encoding='utf-8'))
        try:ops.deploy(pack,root,authorization=auth,request_id=a.run_id+'-failed-canary',image_id=a.image_id,engine=a.engine,command=command,contract=contract,config={},phase='canary',secret_values=secrets,private_root=restart_private,state_volume=volume,fault_after=1)
        except ops.OperationsRefused as exc:
            if 'configured release stop condition triggered' not in str(exc):raise
        else:raise AssertionError('monitor failed to stop killed generated candidate')
        if before!=json.loads((root/'active.json').read_text(encoding='utf-8')):raise AssertionError('failed canary changed active pointer')
        promoted=ops.transition(root,authorization=auth,image_id=a.image_id,engine=a.engine,action='promote',request_id=a.run_id+'-promote')
        rolled=ops.transition(root,authorization=auth,image_id=a.image_id,engine=a.engine,action='rollback',request_id=a.run_id+'-rollback')
        if rolled['revision']!=baseline['revision']:raise AssertionError('code rollback did not restore prior baseline')
        report={'schema':1,'status':'PASS','candidate_sha256':digest,'prior_generated_evidence':{'path':str(DEFAULT_EVIDENCE),'sha256':evidence_hash},'deployment':deployment,'backup':backup,'failed_migration_recovery':'prior database bytes restored and hash-verified','monitor_fault':'SIGKILL observed by business probe; active pointer unchanged','promote':promoted,'rollback':rolled,'data_rollback':'not_claimed','limitations':['No generated candidate source edits; prior evidence is separately identified and not broadened.','Image digest identity is not image safety approval.','Rollback changes code/config pointer; database data rollback is not claimed.','The state volume is retained for operator review/cleanup.']}
        a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,sort_keys=True,ensure_ascii=False)+'\n',encoding='utf-8')
        print(json.dumps({'status':'PASS','evidence':str(a.output),'candidate_sha256':digest,'volume':volume,'data_rollback':'not_claimed'}));return 0
if __name__=='__main__':raise SystemExit(main())
