"""Filesystem-backed release gate checks; candidate payload is never executed."""
from __future__ import annotations
import json
import tempfile
from pathlib import Path
import application_delivery as delivery
import application_dependencies as deps
import application_quality as quality
import application_release as release
import application_spec as spec

def _approved_spec():
    raw={'schema':1,'spec_id':'release-check','version':1,'goal':'Release checked artifact','requirements':[{'id':'R1','statement':'Ship exact candidate','provenance':'user','source':'test request','inputs':['candidate'],'outputs':['pack'],'failures':['reject invalid evidence'],'invariants':['candidate hash is bound'],'state_transitions':['staged to packed'],'non_goals':['execute candidate'],'acceptance_ids':['A1'],'ambiguity_ids':[],'conflict_ids':[]}],'entities':[{'id':'Artifact','fields':[{'name':'digest','type':'sha256','required':True,'constraints':['exact'],'provenance':'user','source':'test request'}],'constraints':['immutable'],'roles':['operator']}],'tasks':[{'id':'verify','title':'Verify release','depends_on':[],'requirement_ids':['R1'],'acceptance_ids':['A1'],'kind':'verification'}],'oracle':{'public_acceptance_ids':['A1'],'hidden_oracle_ids':['H1'],'separation':'Hidden answers stay unavailable.'},'ambiguities':[],'conflicts':[],'non_goals':['candidate execution'],'approval':{'status':'draft','operator':'','breaking_change_authorized':False,'authorization_source':''}}
    return spec.approve_spec(spec.propose_spec(raw),operator='release-check-operator',authorization_source='explicit fixture authorization')

def main():
  with tempfile.TemporaryDirectory(prefix='autoforge-release-check-') as td:
    root=Path(td); candidate=root/'candidate'; delivery.applications.stage_candidate({'schema':1,'goal':'Release checked artifact','references':[],'files':[{'path':'src/main.py','content':'raise SystemExit("must not execute")\n'}]},candidate)
    digest=json.loads((candidate/'manifest.json').read_text())['candidate_sha256']; approved=_approved_spec()
    authroot=root/'deploy'; auth={'schema':1,'approved':True,'deployment_root':str(authroot.resolve()),'candidate_types':['application'],'scopes':['deploy','health_check','pointer_switch','rollback']}
    cats={c:('optional' if c=='anti_cheat' else 'required') for c in quality.CATEGORIES}; qp={'schema':1,'requirements':cats}; records=[quality.seal_record({'schema':1,'candidate_sha256':digest,'category':c,'status':('UNRESOLVED' if c=='anti_cheat' else 'PASS'),'value':('independent adapter unavailable' if c=='anti_cheat' else True),'unit':('reason' if c=='anti_cheat' else 'observed'),'source':'trusted-fixture-review'}) for c in sorted(quality.CATEGORIES)]
    records=[record for record in records if record['category']!='anti_cheat']
    ev={'schema':1,'candidate_sha256':digest,'spec_sha256':approved['sha256']}; ev['proposal']={'status':'PASS','evidence_sha256':'a'*64,'producer_id':'proposal-operator','model_id':'model-a','oracle_id':'oracle-a','observed_at':'fixture'}; ev['validator']={'status':'PASS','evidence_sha256':'b'*64,'producer_id':'validator-operator','model_id':'model-b','oracle_id':'oracle-b','observed_at':'fixture'}
    lock={'schema':1,'ecosystem':'python','toolchain':{'name':'python','version':'3.12','container_image':'sha256:'+'c'*64},'packages':[]}; sources={'fixture':'local://fixture'}; cache=root/'cache'; cache.mkdir()
    env={'candidate_sha256':digest,'lock':lock,'sources':sources,'artifacts':[],'cache':str(cache)}
    original=release.builder.preflight; release.builder.preflight=lambda engine,image:{'engine':engine,'server_os':'linux','image_id':image}
    try:
      report=release.preflight(candidate,approved,qp,records,ev,env,auth,authroot,engine='docker',image=lock['toolchain']['container_image'])
      assert report['status']=='PASS',report
      required={'schema':1,'requirements':{c:'required' for c in quality.CATEGORIES}}
      assert release.preflight(candidate,approved,required,records,ev,env,auth,authroot,engine='docker',image=lock['toolchain']['container_image'])['status']=='HOLD'
      packed=release.pack(candidate,report,env,{'engine':'fixture','image_digest':lock['toolchain']['container_image']},root/'pack')
      assert packed['status']=='VALID'
      bad=dict(ev); bad['candidate_sha256']='0'*64
      try: release.preflight(candidate,approved,qp,records,bad,env,auth,authroot,engine='docker',image=lock['toolchain']['container_image'])
      except release.ReleaseRefused: pass
      else: raise AssertionError('foreign dual evidence accepted')
      incomplete=release.preflight(candidate,approved,qp,records[:-1],ev,env,auth,authroot,engine='docker',image=lock['toolchain']['container_image'])
      assert incomplete['status']=='HOLD'
      denied=dict(auth); denied['deployment_root']=str((root/'other').resolve())
      try: release.preflight(candidate,approved,qp,records,ev,env,denied,authroot,engine='docker',image=lock['toolchain']['container_image'])
      except release.ReleaseRefused: pass
      else: raise AssertionError('wrong local deployment scope accepted')
      release.builder.preflight=lambda engine,image: (_ for _ in ()).throw(builder_missing())
      blocked=release.preflight(candidate,approved,qp,records,ev,env,auth,authroot,engine='docker',image=lock['toolchain']['container_image'])
      assert blocked['status']=='HOLD' and blocked['checks']['container']['status']=='BLOCKED'
    finally: release.builder.preflight=original
    print(json.dumps({'status':'PASS','checks':['explicit optional anti-cheat policy preserves supported release path','required unresolved anti-cheat keeps release preflight on HOLD'],'candidate_execution':'NOT_RUN','deployment_smoke':'NOT_TESTED'}))

def builder_missing():
  import application_builder as builder
  return builder.MissingPrerequisite('fixture: unavailable runtime')
if __name__=='__main__': main()
