#!/usr/bin/env python3
"""Independent, seeded semantic checking of immutable generated benchmark candidates."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, hashlib, json, random, sys, time
from pathlib import Path
import application_builder as builder
import applications

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'fixtures/benchmark/tasks.json'
LOCK=ROOT/'fixtures/benchmark/lock.json'
DEFAULT_CANDIDATES={
 'inventory-order-001':ROOT/'bootstrap-runs/benchmark-9f1b8e4eab92456283dd9473196b2268/inventory-order-001/session/candidate.json',
 'order-validation-001':ROOT/'bootstrap-runs/benchmark-302a806172364ac8a5245c5a052ca1bf/order-validation-001/session/candidate.json',
 'ticket-status-001':ROOT/'bootstrap-runs/benchmark-9f1b8e4eab92456283dd9473196b2268/ticket-status-001/session/candidate.json',
 'content-render-001':ROOT/'bootstrap-runs/benchmark-302a806172364ac8a5245c5a052ca1bf/content-render-001/session/candidate.json',
}
IMAGE='sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60'
def sha(raw:bytes)->str:return hashlib.sha256(raw).hexdigest()
def canon(v)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def expected(task_id,request):
    """Evaluate one value directly from the task's public semantics."""
    if not isinstance(request,dict):return {'error':'invalid_content'} if task_id=='content-render-001' else ({'error':'invalid_input'} if task_id=='inventory-order-001' else {'accepted':False})
    if task_id=='inventory-order-001':
        items=request.get('items')
        if request.get('op')!='sort' or not isinstance(items,list) or any(not isinstance(row,dict) or not isinstance(row.get('id'),str) or type(row.get('quantity')) is not int for row in items):return {'error':'invalid_input'}
        return {'items':[row for _,row in sorted(enumerate(items),key=lambda pair:(-pair[1]['quantity'],pair[0]))]}
    if task_id=='order-validation-001':return {'accepted':request.get('op')=='validate_order' and type(request.get('quantity')) is int and request['quantity']>=1}
    if task_id=='ticket-status-001':
        src=request.get('from');dst=request.get('to')
        if request.get('op')!='transition' or not isinstance(src,str) or not isinstance(dst,str):return {'accepted':False}
        valid=src=='open' and dst=='resolved'
        return {'accepted':valid,'status':'resolved' if valid else src}
    if task_id=='content-render-001':
        title=request.get('title');body=request.get('body')
        return {'text':title+'\n'+body} if request.get('op')=='render' and isinstance(title,str) and isinstance(body,str) else {'error':'invalid_content'}
    raise ValueError('unknown benchmark task')
def scenarios(task_id,seed,count):
    rng=random.Random(seed);cases=[]
    if task_id=='inventory-order-001':
        cases=[{'op':'sort','items':[]},{'op':'sort','items':[{'id':'z','quantity':2},{'id':'a','quantity':2},{'id':'low','quantity':-1}]},{'op':'sort','items':'not-list'},{'op':'sort','items':[{'id':'x','quantity':True}]},{'op':'sort','items':[{'quantity':1}]},{'op':'wrong','items':[]},None]
        cases.extend([{'op':'sort','items':[{'id':7,'quantity':1}]},{'op':'sort','items':[{'id':'no-quantity'}]},[{'op':'sort','items':[]}],'not-an-object',False,7])
        for _ in range(count):cases.append({'op':'sort','items':[{'id':f'{i}-{rng.randrange(1000)}','quantity':rng.randint(-30,30)} for i in range(rng.randint(0,18))]})
        cases.append({'op':'sort','items':[]})
    elif task_id=='order-validation-001':
        cases=[{'op':'validate_order','quantity':n} for n in (-1,0,1,2,2**31)]+[{'op':'validate_order','quantity':True},{'op':'validate_order','quantity':'1'},{'op':'validate_order','quantity':1.5},{'op':'validate_order','quantity':None},{'op':'other','quantity':3},{},None,{'op':'validate_order','quantity':False},{'op':'validate_order','quantity':-1.0},[], 'not-an-object', False, 7]
        cases += [{'op':'validate_order','quantity':rng.randint(-1000,1000)} for _ in range(count)];cases.append({'op':'validate_order','quantity':1})
    elif task_id=='ticket-status-001':
        cases=[{'op':'transition','from':'open','to':'resolved'},{'op':'transition','from':'open','to':'open'},{'op':'transition','from':'resolved','to':'open'},{'op':'transition','from':3,'to':'resolved'},{'op':'transition','from':'open','to':7},{'op':'transition','from':'open'},{'op':'transition','to':'resolved'},{'op':'transition','from':'pending','to':'closed'},{'op':'wrong','from':'open','to':'resolved'},None,[], 'not-an-object', False, 7]
        states=['open','resolved','pending','closed',''];cases += [{'op':'transition','from':rng.choice(states),'to':rng.choice(states)} for _ in range(count)];cases.append({'op':'transition','from':'open','to':'resolved'})
    elif task_id=='content-render-001':
        cases=[{'op':'render','title':'','body':''},{'op':'render','title':'x','body':'y'},{'op':'render','title':'line1\nline2','body':'tail\nbody'},{'op':'render','title':'x'},{'op':'render','title':None,'body':'z'},{'op':'render','title':'x','body':3},None]
        alphabet=['a','\n','λ','🙂',' '];cases += [{'op':'render','title':''.join(rng.choice(alphabet) for _ in range(rng.randrange(8))),'body':''.join(rng.choice(alphabet) for _ in range(rng.randrange(16)))} for _ in range(count)];cases.append({'op':'render','title':'ok','body':'continues'})
    else:raise ValueError('unknown task id')
    return cases

def run(task_ids,engine,image,outdir,seed,count,repetitions,candidate_overrides=None):
    candidates = dict(DEFAULT_CANDIDATES)
    for task_id, path in (candidate_overrides or {}).items():
        candidates[task_id] = Path(path).resolve()
    manifest_bytes=MANIFEST.read_bytes();lock=json.loads(LOCK.read_text(encoding='utf-8'));manifest=json.loads(manifest_bytes)
    if sha(manifest_bytes)!=lock['public_manifest_sha256']:raise ValueError('public task manifest differs from locked digest')
    contracts_path=ROOT/'fixtures/benchmark/contracts.json';contracts_bytes=contracts_path.read_bytes()
    if sha(contracts_bytes)!=lock['contracts_sha256']:raise ValueError('locked baseline acceptance contract differs from its digest')
    if image!=IMAGE:raise ValueError('only the exact previously authorized local benchmark image is accepted')
    unknown=set(task_ids)-set(candidates)
    if unknown:raise ValueError('unknown task id selection')
    if not task_ids or len(set(task_ids))!=len(task_ids):raise ValueError('task ids must be a nonempty unique list')
    if not 1<=repetitions<=20:raise ValueError('repetitions must be 1..20')
    if not 1<=count<=5000:raise ValueError('cases-per-task must be 1..5000')
    if set(candidate_overrides or {})-set(task_ids):raise ValueError('candidate override must be within selected task ids')
    info=builder.preflight(engine,image);outdir.mkdir(parents=True,exist_ok=False);rows=[]
    for tid in task_ids:
        task=next(t for t in manifest['tasks'] if t['task_id']==tid);candidate_path=candidates[tid]
        raw=candidate_path.read_bytes();candidate=json.loads(raw.decode('utf-8'));clean,contents=applications.validate_candidate(candidate)
        if len(contents)!=1 or 'main.py' not in contents:raise ValueError('candidate must be a single-file main.py')
        source=contents['main.py']
        for repetition in range(repetitions):
            sample_seed=seed+repetition;cases=scenarios(tid,sample_seed,count);expected_rows=[expected(tid,c) for c in cases]
            observations=[];passed=True;started=time.monotonic()
            for batch_index,start in enumerate(range(0,len(cases),builder.MAX_REQUESTS)):
                batch=cases[start:start+builder.MAX_REQUESTS];batch_expected=expected_rows[start:start+builder.MAX_REQUESTS]
                contract={'schema':1,'entrypoint':'main.py','scenarios':[{'name':f'independent-{sample_seed}-{batch_index}','requests':batch,'expected':batch_expected}]}
                stage=applications.stage_candidate(clean,outdir/(tid+'-sample-'+str(repetition)+'-batch-'+str(batch_index)));stage_manifest=json.loads(Path(stage['manifest']).read_text(encoding='utf-8'))
                result=builder.build({'schema':1,'workspace_manifest':stage_manifest,'candidate_sha256':stage['candidate_sha256']},contract,engine,image,info)
                passed=passed and result['build_pass'] and result['test_pass'];observations.extend(result['observations'])
            status='PASS' if passed else 'FAIL'
            row={'task_id':tid,'sample':repetition,'status':status,'candidate_path':str(candidate_path),'candidate_sha256':hashlib.sha256(applications._json_bytes(clean)).hexdigest(),'source_sha256':sha(source),'immutable_candidate_file_sha256':sha(raw),'task_sha256':hashlib.sha256(canon(task)).hexdigest(),'public_manifest_sha256':sha(manifest_bytes),'acceptance_fixture_sha256':sha(contracts_bytes),'image_id':info['image_id'],'builder_sha256':sha(Path(builder.__file__).read_bytes()),'independent_checker_sha256':sha(Path(__file__).read_bytes()),'validator_sha256':sha(Path(applications.__file__).read_bytes()),'scenario_seed':sample_seed,'generated_case_count':count,'case_count':len(cases),'scenario_input_sha256':sha(canon(cases)),'independent_expected_sha256':sha(canon(expected_rows)),'elapsed_seconds':time.monotonic()-started,'container_observations':observations,'limitations':['Public-semantics executable comparison only; no human semantic authorization or second independent reviewer supplied.','Exact container image identity is local observation; image contents remain operator-trusted.','Candidate provenance is the immutable file at the recorded path, not a cryptographically signed producer attestation.']}
            row['evidence_sha256']=sha(canon(row));rows.append(row)
    report={'schema':1,'status':'PASS' if rows and all(r['status']=='PASS' for r in rows) else 'FAIL','scope':'independently derived seeded executable semantics for historical generated candidates; no producer/oracle authority manufactured','candidate_mapping':{tid:{'path':str(path),'sha256':sha(path.read_bytes())} for tid,path in sorted(candidates.items()) if tid in task_ids},'task_order':[r['task_id'] for r in rows],'runs':rows,'semantic_approval':'HOLD','missing_authorization':'External independent semantic reviewer approval of this executable reference implementation and its public-spec interpretation.','authority_limits':['No separate model, producer, or oracle identity is claimed.','Comparison PASS is not dual semantic approval.'],'sha256':None}
    report['sha256']=sha(canon(report));path=outdir/'independent-comparison.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');return report,path
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--engine',choices=('docker','podman'),required=True);p.add_argument('--image',default=IMAGE);p.add_argument('--output',type=Path,required=True);p.add_argument('--seed',type=int,default=21104);p.add_argument('--cases-per-task',type=int,default=80);p.add_argument('--repetitions',type=int,default=1);p.add_argument('--task-id',action='append');p.add_argument('--candidate',action='append',default=[],metavar='TASK_ID=PATH');a=p.parse_args()
    try:
        if a.task_id and len(set(a.task_id))!=len(a.task_id):raise ValueError('duplicate task id selection')
        overrides={}
        for item in a.candidate:
            tid,sep,path=item.partition('=')
            if not sep or not tid or not path or tid in overrides:raise ValueError('candidate must be unique TASK_ID=PATH')
            overrides[tid]=Path(path).resolve()
        if not 1<=a.cases_per_task<=5000:raise ValueError('cases-per-task must be 1..5000')
        if type(a.repetitions) is not int or not 1<=a.repetitions<=20:raise ValueError('repetitions must be 1..20')
        report,path=run(a.task_id or list(DEFAULT_CANDIDATES),a.engine,a.image,a.output,a.seed,a.cases_per_task,a.repetitions,overrides)
        print(json.dumps({'status':report['status'],'semantic_approval':report['semantic_approval'],'report':str(path),'tasks':report['task_order']}));return 0 if report['status']=='PASS' else 2
    except (OSError,ValueError,KeyError,TypeError,builder.BuildFailure,builder.MissingPrerequisite) as exc:print(f'HOLD: {exc}',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())