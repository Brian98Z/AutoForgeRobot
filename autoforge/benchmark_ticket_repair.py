#!/usr/bin/env python3
"""Model-mediated repair of an immutable ticket candidate with public validators."""
from __future__ import annotations
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
import benchmark_independent_check as independent
import application_builder as builder
import applications
ROOT=Path(__file__).resolve().parents[1]
DEFAULT_TASK='ticket-status-001'
IMAGE=independent.IMAGE

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--engine',choices=('docker','podman'),default='docker')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--base-url',default=os.environ.get('AUTOFORGE_API_BASE','http://127.0.0.1:8081/v1'))
    p.add_argument('--model',default=os.environ.get('AUTOFORGE_MODEL'))
    p.add_argument('--image',default=IMAGE)
    p.add_argument('--source-candidate',type=Path)
    p.add_argument('--public-feedback',default='')
    p.add_argument('--task-id',choices=sorted(independent.DEFAULT_CANDIDATES),default=DEFAULT_TASK)
    p.add_argument('--max-turns',type=int,default=12)
    a=p.parse_args()
    if not a.model: print('HOLD: configure AUTOFORGE_MODEL to the owned local model instance',file=sys.stderr); return 2
    task_id=a.task_id
    source_path=a.source_candidate.resolve() if a.source_candidate else independent.DEFAULT_CANDIDATES[task_id]
    raw=source_path.read_bytes();candidate=json.loads(raw.decode('utf-8'));clean,contents=applications.validate_candidate(candidate)
    if len(contents)!=1 or 'main.py' not in contents: print('HOLD: source candidate must contain exactly main.py',file=sys.stderr); return 2
    if a.source_candidate and not a.public_feedback.strip(): print('HOLD: follow-up repair requires public validation feedback',file=sys.stderr); return 2
    if a.image!=IMAGE: print('HOLD: exact benchmark image differs from locked local image',file=sys.stderr); return 2
    manifest_bytes=independent.MANIFEST.read_bytes();lock=json.loads(independent.LOCK.read_text(encoding='utf-8'))
    if independent.sha(manifest_bytes)!=lock['public_manifest_sha256']: print('HOLD: public task manifest no longer matches lock',file=sys.stderr); return 2
    task=next(t for t in json.loads(manifest_bytes)['tasks'] if t['task_id']==task_id)
    work=a.output.resolve();work.mkdir(parents=True,exist_ok=False);source_root=work/'original-source';source_root.mkdir()
    (source_root/'main.py').write_bytes(contents['main.py'])
    feedback=('\nPublic validator feedback: '+a.public_feedback) if a.public_feedback else ''
    request={'schema':1,'goal':task['public_input']+' Repair the observed defect in the existing implementation. Keep the public protocol. Run the existing validator before editing; after editing, validate both the public acceptance contract and independent public-semantics cases.'+feedback,'references':[]}
    req=work/'request.json';req.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
    contract_bytes=(ROOT/'fixtures/benchmark/contracts.json').read_bytes()
    if independent.sha(contract_bytes)!=lock['contracts_sha256']: print('HOLD: baseline acceptance contract differs from lock',file=sys.stderr);return 2
    public_contract=json.loads(contract_bytes)['contracts'][task_id]
    independent_cases=independent.scenarios(task_id,21104,80)
    independent_expected=[independent.expected(task_id,row) for row in independent_cases]
    validator_scenarios=[]
    for index,start in enumerate(range(0,len(independent_cases),builder.MAX_REQUESTS)):
        validator_scenarios.append({'name':f'independent-public-{21104}-{index}','requests':independent_cases[start:start+builder.MAX_REQUESTS],'expected':independent_expected[start:start+builder.MAX_REQUESTS]})
    contract_obj={'schema':1,'entrypoint':'main.py','scenarios':[*public_contract['scenarios'],*validator_scenarios]}
    contract=work/'public-contract.json';contract.write_text(json.dumps(contract_obj,ensure_ascii=False),encoding='utf-8')
    refs=work/'references';refs.mkdir();session=work/'repair-session'
    cmd=[sys.executable,str(ROOT / 'autoforge' / 'application_build.py'),'run','--request',str(req),'--contract',str(contract),'--reference-store',str(refs),'--engine',a.engine,'--image',a.image,'--output',str(session),'--mode','agent','--max-turns',str(a.max_turns),'--token-budget','200000','--time-budget','300','--tool-budget','48','--project',str(source_root),'--allow-model','--trusted-adapter']
    before=hashlib.sha256(raw).hexdigest();started=time.monotonic()
    env={**os.environ,'AUTOFORGE_API_BASE':a.base_url,'AUTOFORGE_MODEL':a.model,'AUTOFORGE_API_KEY':'','AUTOFORGE_TOOL_CHOICE':'auto'}
    proc=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=330,check=False,env=env)
    try:cli=json.loads(proc.stdout)
    except ValueError:cli=None
    events_path=session/'events.jsonl';events=[json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()] if events_path.exists() else []
    report={'schema':1,'status':'HOLD','model_mediated':True,'task_id':task_id,'source_candidate_path':str(source_path.relative_to(ROOT)) if source_path.is_relative_to(ROOT) else str(source_path),'source_candidate_sha256':before,'source_candidate_sha256_after':independent.sha(source_path.read_bytes()),'source_candidate_unchanged':independent.sha(source_path.read_bytes())==before,'public_task_sha256':hashlib.sha256(independent.canon(task)).hexdigest(),'public_manifest_sha256':independent.sha(manifest_bytes),'public_acceptance_fixture_sha256':independent.sha(contract_bytes),'repair_contract_sha256':independent.sha(contract.read_bytes()),'independent_suite_expected_sha256':independent.sha(independent.canon(independent_expected)),'independent_suite_input_sha256':independent.sha(independent.canon(independent_cases)),'image_id':a.image,'model_id':a.model,'model_base_url':a.base_url,'max_turns':a.max_turns,'repair_cli_status':cli.get('status') if isinstance(cli,dict) else 'HOLD','repair_cli_task_status':cli.get('task_status') if isinstance(cli,dict) else None,'model_call_intents':sum(event.get('type')=='model_call_intent' for event in events),'tool_call_count':sum(event.get('type')=='tool_result' for event in events),'returncode':proc.returncode,'elapsed_seconds':time.monotonic()-started,'stdout_sha256':independent.sha(proc.stdout.encode()),'stderr_sha256':independent.sha(proc.stderr.encode()),'session':str(session.relative_to(ROOT)) if session.is_relative_to(ROOT) else str(session),'limitations':['Repair requires the configured model, engine and exact image.','No independent human semantic authorization or separate producer/oracle authority is claimed.','The input candidate is never modified; repair is staged in a new session.']}
    if isinstance(cli,dict) and proc.returncode==0 and (session/'candidate.json').is_file():
        repaired=json.loads((session/'candidate.json').read_text(encoding='utf-8'));repaired_clean,_=applications.validate_candidate(repaired)
        info=builder.preflight(a.engine,a.image);stage=applications.stage_candidate(repaired_clean,work/'repaired-validation');staged=json.loads(Path(stage['manifest']).read_text(encoding='utf-8'))
        result=builder.build({'schema':1,'workspace_manifest':staged,'candidate_sha256':stage['candidate_sha256']},contract_obj,a.engine,a.image,info)
        report['repaired_candidate_sha256']=stage['candidate_sha256']
        report['public_and_independent_revalidation']={'status':'PASS' if result['build_pass'] and result['test_pass'] else 'FAIL','public_acceptance_scenarios':len(public_contract['scenarios']),'independent_suite_cases':len(independent_cases),'seed':21104,'container_observations':result['observations']}
        report['status']='PASS' if report['source_candidate_unchanged'] and report['repair_cli_status']=='PASS' and report['public_and_independent_revalidation']['status']=='PASS' else 'FAIL'
    report['evidence_sha256']=independent.sha(independent.canon(report));dest=work/'repair-evidence.json';dest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':report['status'],'evidence':str(dest),'repair_cli_status':report['repair_cli_status']}));return 0 if report['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())