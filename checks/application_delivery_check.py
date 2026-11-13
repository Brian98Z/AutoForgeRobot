"""Filesystem-backed acceptance checks for local delivery lifecycle boundaries.
Run: python checks/application_delivery_check.py. This does not claim an actual
 deployment runtime was exercised; health adapter tests use trusted local argv.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json, tempfile, shutil
from pathlib import Path
import applications
import application_delivery as delivery
def main() -> None:
    with tempfile.TemporaryDirectory(prefix='autoforge-delivery-check-') as td:
        base=Path(td); candidate=base/'candidate'
        applications.stage_candidate({'schema':1,'goal':'local delivery check','references':[],'files':[{'path':'main.py','content':'print("never run")\n'}]},candidate)
        manifest=json.loads((candidate/'manifest.json').read_text())
        evidence={'status':'PASS','candidate_sha256':manifest['candidate_sha256'],'validator':'independent-test-validator','scenarios':['exact digest check']}
        pack=base/'pack'; delivery.create_pack(candidate,evidence,{'lock_sha256':'a'*64,'dependencies':[]},{'engine':'local-adapter','image_digest':'sha256:'+'b'*64},pack)
        assert delivery.validate_pack(pack)['status']=='VALID'
        broken=base/'broken'; shutil.copytree(pack,broken); (broken/'evidence.json').write_text('{"status":"PASS"}',encoding='utf-8')
        try: delivery.validate_pack(broken)
        except delivery.DeliveryRefused: pass
        else: raise AssertionError('invalid evidence accepted')
        tampered=base/'tampered'; shutil.copytree(pack,tampered); (tampered/'candidate'/'main.py').write_text('changed\n',encoding='utf-8')
        try: delivery.validate_pack(tampered)
        except delivery.DeliveryRefused: pass
        else: raise AssertionError('tampered candidate accepted')
        extra=base/'extra-file'; shutil.copytree(candidate,extra); (extra/'secret.txt').write_text('must not package',encoding='utf-8')
        try: delivery.create_pack(extra,evidence,{'dependencies':[]},{'engine':'local'},base/'extra-pack')
        except delivery.DeliveryRefused: pass
        else: raise AssertionError('unlisted candidate file accepted')
        linked=base/'linked'; shutil.copytree(candidate,linked)
        try: (linked/'secret-link').symlink_to(candidate/'main.py')
        except (OSError,NotImplementedError): pass
        else:
            try: delivery.create_pack(linked,evidence,{'dependencies':[]},{'engine':'local'},base/'linked-pack')
            except delivery.DeliveryRefused: pass
            else: raise AssertionError('candidate symlink accepted')
        nested=base/'nested-tamper'; shutil.copytree(pack,nested)
        (nested/'candidate'/'plugins').mkdir()
        (nested/'candidate'/'plugins'/'inventory.json').write_text('untracked secret',encoding='utf-8')
        try: delivery.validate_pack(nested)
        except delivery.DeliveryRefused: pass
        else: raise AssertionError('nested inventory-named candidate file escaped package inventory')
        root=base/'deploy'; approval={'schema':1,'approved':True,'deployment_root':str(root.resolve()),'candidate_types':['application'],'scopes':['deploy','health_check','pointer_switch','rollback']}
        try: delivery.install(pack,root,approval={'schema':1,'approved':True},request_id='bad-auth',candidate_type='application',health_argv=['python','-c','raise SystemExit(0)'],operator_adapter=True)
        except delivery.DeliveryRefused: pass
        else: raise AssertionError('invalid authorization accepted')
        ok=delivery.install(pack,root,approval=approval,request_id='healthy-1',candidate_type='application',health_argv=['python','-c','raise SystemExit(0)'],operator_adapter=True)
        assert ok['status']=='PROMOTED'
        previous=json.loads((root/'active.json').read_text())['revision']
        failed=delivery.install(pack,root,approval=approval,request_id='failed-1',candidate_type='application',health_argv=['python','-c','raise SystemExit(7)'],operator_adapter=True)
        assert failed['status']=='ROLLED_BACK'
        assert json.loads((root/'active.json').read_text())['revision']==previous
        try: delivery.install(pack,root,approval=approval,request_id='healthy-1',candidate_type='application',health_argv=['python','-c','pass'],operator_adapter=True)
        except delivery.DeliveryRefused: pass
        else: raise AssertionError('duplicate request was replayed')
        # Crash ambiguity is an explicit operator reconciliation, never replay.
        ledger=root/'.delivery-ledger'; ambiguous={'schema':1,'request_id':'crash-1','state':'unknown','effect':'deploy_and_switch','candidate_sha256':manifest['candidate_sha256']}
        (ledger/'crash-1.json').write_text(json.dumps(ambiguous),encoding='utf-8')
        try: delivery.install(pack,root,approval=approval,request_id='crash-1',candidate_type='application',health_argv=['python','-c','pass'],operator_adapter=True)
        except delivery.DeliveryRefused: pass
        else: raise AssertionError('ambiguous request replayed')
        reconciled=delivery.reconcile(root,'crash-1',{'request_id':'crash-1','observed_state':'not_executed','evidence_id':'operator-reconciliation-1'})
        assert reconciled['state']=='reconciled_not_executed'
        print(json.dumps({'status':'PASS','checks':['filesystem pack/import','exact digest evidence','tamper detection','authorization denial','trusted adapter promotion','health-failure rollback','request replay refusal','crash ambiguity reconciliation'],'deployment_runtime':'NOT_TESTED'}))
if __name__=='__main__': main()
