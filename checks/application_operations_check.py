"""Regression checks for release authorization, measurement policy and integrity boundaries.
Real container execution is intentionally separate: run application_operations_live_check.py
with explicit local engine/image authorization to collect deployment evidence.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib,json,tempfile
from pathlib import Path
import application_operations as ops
import application_quality as quality
def main():
    with tempfile.TemporaryDirectory(prefix='autoforge-operations-check-') as td:
        root=Path(td)/'deploy';root.mkdir()
        auth={'schema':1,'approved':True,'deployment_root':str(root.resolve()),'candidate_types':['application'],'scopes':['deploy','health_check','pointer_switch','rollback'],'image_ids':['sha256:'+'a'*64],'environment':'test','allow_irreversible_migrations':False,'stop_conditions':{'max_error_rate':0.05,'max_latency_ms_p50':1000}}
        ops._authorize(auth,root,'sha256:'+'a'*64)
        for mutate in (lambda a:a.update(deployment_root=str(Path(td)/'outside')),lambda a:a.update(image_ids=['sha256:'+'b'*64]),lambda a:a.update(scopes=['deploy'])):
            bad=dict(auth);mutate(bad)
            try:ops._authorize(bad,root,'sha256:'+'a'*64)
            except ops.OperationsRefused:pass
        try:ops._authorize(auth,root,'sha256:'+'b'*64)
        except ops.OperationsRefused:pass
        else:raise AssertionError('wrong exact image ID accepted')
        assert ops._config({'TOKEN':{'$secret_file':'/run/autoforge/secrets/SECRET'}},{'SECRET'})[0]['TOKEN']=='/run/autoforge/secrets/SECRET'
        for invalid in ({'TOKEN':{'$secret_file':'/etc/passwd'}},{'TOKEN':{'$secret_file':'/run/autoforge/secrets/OTHER'}},{'TOKEN':{'$secret_file':'/run/autoforge/secrets/SECRET','extra':1}}):
            try:ops._config(invalid,{'SECRET'})
            except ops.OperationsRefused:pass
            else:raise AssertionError('invalid secret reference accepted')
        with tempfile.TemporaryDirectory(prefix='autoforge-private-root-check-') as private:
            manifest=ops.secrets_module.prepare_runtime_secrets({'SECRET':'opaque-check-value'},private_root=private,session_id='check',allowed_names={'SECRET'})
            secret_path=Path(manifest['mounts'][0]['source']);assert secret_path.read_text()=='opaque-check-value'
            cleanup=ops.secrets_module.cleanup_runtime_secrets(manifest,audit_path=Path(td)/'cleanup.jsonl')
            assert cleanup['deleted']==1 and not secret_path.exists() and 'opaque-check-value' not in Path(td,'cleanup.jsonl').read_text()
        try:ops._config({'TOKEN':object()},{'SECRET'})
        except ops.OperationsRefused:pass
        else:raise AssertionError('non-scalar secret config accepted')
        missing_root=Path(td)/'must-not-be-created'
        missing_auth={**auth,'deployment_root':str(missing_root.resolve())}
        for private_args in ({'secret_values':{'SECRET':'opaque-check-value'}},{'config':{'TOKEN':{'$secret_file':'/etc/passwd'}}}):
            args={'pack':Path(td)/'absent-pack','root':missing_root,'authorization':missing_auth,'request_id':'invalid-private','image_id':'sha256:'+'a'*64,'engine':'docker','command':['python','/candidate/main.py'],'contract':{},'config':{}}
            args.update(private_args)
            try:ops.deploy(**args)
            except ops.OperationsRefused:pass
            else:raise AssertionError('invalid private deployment dispatched')
            assert not missing_root.exists(), 'invalid private configuration mutated release root'
        candidate=hashlib.sha256(b'candidate').hexdigest();cats=sorted(quality.CATEGORIES);policy={'schema':1,'requirements':{c:'required' for c in cats}}
        records=[quality.seal_record({'schema':1,'candidate_sha256':candidate,'category':c,'status':('UNRESOLVED' if c=='anti_cheat' else 'PASS'),'value':('independent adapter unavailable' if c=='anti_cheat' else True),'unit':('reason' if c=='anti_cheat' else 'observed'),'source':'runtime-contract'}) for c in cats]
        assert quality.evaluate(candidate,policy,records)['status']=='HOLD'
        records=[r for r in records if r['category']!='anti_cheat']
        assert quality.evaluate(candidate,policy,records)['status']=='HOLD'
        optional={'schema':1,'requirements':{c:('optional' if c=='anti_cheat' else 'required') for c in cats}}
        assert quality.evaluate(candidate,optional,records)['status']=='PASS'
        tampered=[dict(x) for x in records];tampered[0]['value']=False
        try:quality.evaluate(candidate,policy,tampered)
        except quality.QualityRefused:pass
        else:raise AssertionError('tampered operational evidence accepted')
        try:ops._record(root,'replay',{'state':'unknown'});ops._record(root,'replay',{'state':'complete'})
        except ops.OperationsRefused:pass
        else:raise AssertionError('unknown side effect request replayed')
    print(json.dumps({'status':'PASS','checks':['exact root/image/lifecycle scopes','secret reference scope and restrictive staging cleanup','required-vs-optional quality thresholds','evidence tamper refusal','no replay of duplicate side effects'],'deployment_runtime':'NOT_TESTED'}))
if __name__=='__main__':main()
