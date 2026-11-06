#!/usr/bin/env python3
"""Evaluate candidate-bound recorded evidence or measure isolated JSONL scenarios."""
from __future__ import annotations
import argparse, hashlib, json, math, re, sys
from pathlib import Path
import application_builder as _builder

CATEGORIES={'security','latency','resource','accessibility','compatibility','anti_cheat','data_invariant'}
STATUSES={'PASS','FAIL','FLAKY','UNRESOLVED','NOT_APPLICABLE'}
class QualityRefused(ValueError): pass

def _canonical(v): return json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
def _sha(v): return hashlib.sha256(_canonical(v)).hexdigest()
def seal_record(record):
    clean=dict(record); clean.pop('evidence_sha256',None); return {**clean,'evidence_sha256':_sha(clean)}
def _digest(v,where):
    if not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v): raise QualityRefused(f'{where} must be lowercase SHA-256')
    return v

def evaluate(candidate_sha256, policy, records, *, authorized_na_reasons=()):
    candidate_sha256=_digest(candidate_sha256,'candidate_sha256')
    if not isinstance(policy,dict) or set(policy)!={'schema','requirements'} or type(policy['schema']) is not int or policy['schema']!=1 or not isinstance(policy['requirements'],dict) or set(policy['requirements'])!=CATEGORIES: raise QualityRefused('policy must define exactly all seven quality categories')
    if not isinstance(records,list) or len(records)>256: raise QualityRefused('records must be a bounded list')
    if not isinstance(authorized_na_reasons,(list,tuple,set,frozenset)) or any(not isinstance(x,str) for x in authorized_na_reasons): raise QualityRefused('authorized N/A reasons must be explicit strings')
    seen=set(); checked={}
    for r in records:
        required={'schema','candidate_sha256','category','status','value','unit','source','evidence_sha256'}
        if not isinstance(r,dict) or set(r)!=required or type(r['schema']) is not int or r['schema']!=1: raise QualityRefused('malformed typed evidence record')
        if r['candidate_sha256']!=candidate_sha256: raise QualityRefused('evidence is not bound to candidate digest')
        c=r['category']; status=r['status']
        if c not in CATEGORIES or status not in STATUSES or c in seen: raise QualityRefused('unknown, duplicate or invalid quality status evidence')
        seen.add(c)
        _digest(r['evidence_sha256'],'evidence_sha256')
        unsigned={k:v for k,v in r.items() if k!='evidence_sha256'}
        if r['evidence_sha256']!=_sha(unsigned): raise QualityRefused(f'{c} evidence integrity hash mismatch')
        if not isinstance(r['source'],str) or not r['source'].strip() or len(r['source'])>512: raise QualityRefused(f'{c} evidence source required')
        if status=='NOT_APPLICABLE':
            reason=r['value']
            if not isinstance(reason,str) or not reason.strip() or reason not in authorized_na_reasons: raise QualityRefused(f'{c} N/A requires exact authorized reason')
            if r['unit']!='reason': raise QualityRefused(f'{c} N/A unit must be reason')
        elif status in ('PASS','FAIL'):
            if type(r['value']) not in (int,float,bool,str) or (isinstance(r['value'],float) and (r['value']!=r['value'] or abs(r['value'])==float('inf'))): raise QualityRefused(f'{c} evidence value must be finite scalar')
            if not isinstance(r['unit'],str) or not r['unit'] or len(r['unit'])>64: raise QualityRefused(f'{c} evidence unit required')
        else:
            if not isinstance(r['value'],str) or not r['value'].strip() or not isinstance(r['unit'],str) or not r['unit']: raise QualityRefused(f'{c} unresolved evidence requires explanation and unit')
        checked[c]=r
    outcomes={}; blockers=[]
    for c in sorted(CATEGORIES):
        requirement=policy['requirements'][c]
        if requirement not in ('required','optional'): raise QualityRefused(f'{c} requirement must be required or optional')
        r=checked.get(c)
        if r is None:
            outcomes[c]='UNRESOLVED'
            if requirement=='required':blockers.append(f'{c}: missing evidence')
        elif r['status']=='PASS': outcomes[c]='PASS'
        elif r['status']=='NOT_APPLICABLE': outcomes[c]='NOT_APPLICABLE'
        else:
            outcomes[c]=r['status']
            blockers.append(f"{c}: {r['status']}")
    status='PASS' if not blockers else 'HOLD'
    payload={'schema':1,'status':status,'candidate_sha256':candidate_sha256,'outcomes':outcomes,'blockers':blockers,'evidence_sha256':{c:r['evidence_sha256'] for c,r in sorted(checked.items())},'limitations':['Gate evaluates typed recorded evidence and hash integrity; it does not itself perform measurements.','A PASS/FAIL observation is only as authoritative as its named source.','Operational measurements must come from application_operations runtime evidence; declarations alone are not measurements.']}
    return {**payload,'report_sha256':_sha(payload)}

def _inventory_cases(seed, count):
    """Generate private add/list metamorphic traces and their independent state oracle."""
    import random
    rng=random.Random(seed)
    cases=[]
    for case_index in range(count):
        state={}; requests=[]; expected=[]
        for _ in range(6):
            sku=f'probe-{case_index}-{rng.getrandbits(64):016x}'
            quantity=rng.randrange(0,1_000_000)
            requests.append({'op':'add','sku':sku,'quantity':quantity})
            state[sku]=quantity
            expected.append({'ok':True,'sku':sku,'quantity':quantity})
            requests.append({'op':'list'})
            expected.append({'ok':True,'items':[{'sku':key,'quantity':state[key]} for key in sorted(state)]})
        cases.append({'name':f'independent-inventory-{case_index}','requests':requests,'expected':expected})
    return cases

def measure(candidate_manifest, contract, thresholds, *, engine, image, authorized_na_reasons=()):
    import time
    import application_builder as builder
    import application_quality_resources as resource_runner
    required_thresholds={'schema','repeats','max_latency_ms','max_output_bytes','max_peak_memory_bytes','max_cpu_usage_usec','na'}
    if not isinstance(thresholds,dict) or not required_thresholds.issubset(thresholds) or set(thresholds)-required_thresholds-{'security_scenarios','anti_cheat'} or type(thresholds['schema']) is not int or thresholds['schema']!=1:
        raise QualityRefused('thresholds must define schema, repeats, max_latency_ms, max_output_bytes, max_peak_memory_bytes, max_cpu_usage_usec and na')
    repeats=thresholds['repeats']; latency=thresholds['max_latency_ms']; output_limit=thresholds['max_output_bytes']; memory_limit=thresholds['max_peak_memory_bytes']; cpu_limit=thresholds['max_cpu_usage_usec']
    if type(repeats)is not int or not 2<=repeats<=5 or type(latency) not in (int,float) or not math.isfinite(latency) or not 0<latency<=50000 or type(output_limit)is not int or not 1<=output_limit<=builder.MAX_OUT or type(memory_limit)is not int or not 1<=memory_limit<=256*1024*1024 or type(cpu_limit)is not int or not 1<=cpu_limit<=60_000_000:
        raise QualityRefused('invalid explicit repeat, latency, output, peak-memory or CPU bound')
    if not isinstance(thresholds['na'],dict) or set(thresholds['na'])-CATEGORIES:
        raise QualityRefused('na must map quality categories to explicit reasons')
    info=builder.preflight(engine,image)
    root,digest=builder._manifest(candidate_manifest)
    scenarios=builder._contract(contract)
    policy={'schema':1,'requirements':{c:'required' for c in CATEGORIES}}
    na=thresholds['na']
    if any(not isinstance(c,str) or not isinstance(reason,str) or not reason.strip() for c,reason in na.items()):
        raise QualityRefused('N/A reasons must be nonempty strings')
    if any(reason not in authorized_na_reasons for reason in na.values()):
        raise QualityRefused('every N/A reason requires exact explicit authorization')
    if any(c in na and c not in ('accessibility','compatibility') for c in na):
        raise QualityRefused('only accessibility and compatibility may be explicitly N/A')
    policy={'schema':1,'requirements':{c:('optional' if c in na else 'required') for c in CATEGORIES}}
    scenarios_by_name={s['name']:s for s in scenarios}
    anti_policy=thresholds.get('anti_cheat'); anti_cases=[]
    if anti_policy is not None:
        if (not isinstance(anti_policy,dict) or set(anti_policy)!={'schema','category','seed','cases'} or type(anti_policy['schema']) is not int or anti_policy['schema']!=1 or anti_policy['category']!='inventory-jsonl' or type(anti_policy['seed']) is not int or not 0<=anti_policy['seed']<=0xffffffff or type(anti_policy['cases']) is not int or not 1<=anti_policy['cases']<=4):
            raise QualityRefused('anti_cheat requires schema 1, category inventory-jsonl, a uint32 seed and 1..4 cases')
        anti_cases=_inventory_cases(anti_policy['seed'],anti_policy['cases'])
    observations=[]; repeated=[]
    for run_index in range(repeats):
        run=[]
        for scenario in scenarios + anti_cases:
            current_root,current_digest=builder._manifest(candidate_manifest)
            if current_digest!=digest or current_root!=root: raise QualityRefused('candidate source changed during measurement')
            payload=b''.join(builder._json_bytes(req)+b'\n' for req in scenario['requests'])
            start=time.monotonic()
            try: out,err,code,timed_out,resource_metrics=resource_runner._container_with_resources(engine,image,root,payload,max(1,min(60,int(latency/1000)+1)))
            except resource_runner.ResourceProbeUnavailable as exc: raise QualityRefused(f'resource measurement unavailable; refusing quality decision: {exc}') from exc
            elapsed=(time.monotonic()-start)*1000
            actual=None
            if not timed_out and code==0:
                try: actual=[builder._strict_loads(line.encode('utf-8')) for line in out.decode('utf-8','strict').splitlines()]
                except (UnicodeError,ValueError,builder.BuildFailure): actual=None
            current_root,current_digest=builder._manifest(candidate_manifest)
            if current_digest!=digest or current_root!=root: raise QualityRefused('candidate source changed during measurement')
            matches=actual is not None and _canonical(actual)==_canonical(scenario['expected'])
            run.append({'scenario':scenario['name'],'elapsed_ms':elapsed,'stdout_bytes':len(out),'stderr_bytes':len(err),'exit_code':code,'timed_out':timed_out,'actual':actual,'expected':scenario['expected'],'matches':matches,'resource':resource_metrics})
        repeated.append(run)
        observations.append([{k:v for k,v in item.items() if k not in {'actual','resource'} and not (item['scenario'].startswith('independent-inventory-') and k=='expected')}|{'actual_sha256':_sha(item['actual']) if item['actual'] is not None else None,'resource':item['resource']} for item in run])
    def category_status(cat):
        if cat in na:return ('NOT_APPLICABLE',na[cat],'reason')
        if cat=='latency':
            vals=[x['elapsed_ms'] for run in repeated for x in run]
            passed=all(x['elapsed_ms']<=latency and not x['timed_out'] for run in repeated for x in run)
            return ('PASS' if passed else 'FAIL',max(vals,default=0),'ms')
        if cat=='resource':
            samples=[item['resource'] for run in repeated for item in run]
            peak=max((x['peak_memory_bytes'] for x in samples),default=None); cpu=max((x['cpu_usage_usec'] for x in samples),default=None)
            output_peak=max((x['stdout_bytes']+x['stderr_bytes'] for run in repeated for x in run),default=None)
            if peak is None or cpu is None or output_peak is None:return ('UNRESOLVED','cgroup resource or output metrics unavailable','bytes and microseconds')
            value=json.dumps({'peak_memory_bytes':peak,'cpu_usage_usec':cpu,'combined_output_bytes':output_peak,'limits':{'peak_memory_bytes':memory_limit,'cpu_usage_usec':cpu_limit,'combined_output_bytes':output_limit}},sort_keys=True,separators=(',',':'))
            return ('PASS' if peak<=memory_limit and cpu<=cpu_limit and output_peak<=output_limit else 'FAIL',value,'cgroup_v2_peak_bytes_cpu_usec_and_output_bytes')
        if cat=='security':
            names=thresholds.get('security_scenarios',[])
            if not isinstance(names,list) or not names or any(not isinstance(n,str) for n in names) or len(names)!=len(set(names)) or any(n not in scenarios_by_name for n in names):
                return ('UNRESOLVED','no valid explicit security_scenarios in thresholds','result')
            cases={n:[next(x for x in run if x['scenario']==n) for run in repeated] for n in names}
            if any(any(x['actual'] is None for x in runs) for runs in cases.values()):
                return ('FAIL','one or more named checks did not produce valid JSON responses','result')
            varied=any(len({_canonical(x['actual']) for x in runs})>1 for runs in cases.values())
            passed=all(all(x['matches'] for x in runs) for runs in cases.values())
            return ('FLAKY' if varied else ('PASS' if passed else 'FAIL'),'explicit security contract cases only','result')
        if cat=='anti_cheat':
            if not anti_policy:return ('UNRESOLVED','no operator-supplied scoped anti-cheat policy','result')
            start=len(scenarios); stop=start+len(anti_cases)
            checks=[item for run in repeated for item in run[start:stop]]
            varied=any(len({_canonical(run[i]['actual']) if run[i]['actual'] is not None else b'<invalid>' for run in repeated})>1 for i in range(start,stop))
            passed=len(checks)==len(anti_cases)*repeats and all(item['matches'] for item in checks)
            value=json.dumps({'policy_sha256':_sha(anti_policy),'seed_sha256':hashlib.sha256(str(anti_policy['seed']).encode()).hexdigest(),'cases':len(anti_cases),'matched':sum(item['matches'] for item in checks)},sort_keys=True,separators=(',',':'))
            return ('FLAKY' if varied else ('PASS' if passed else 'FAIL'),value,'independent_inventory_metamorphic')
        if cat=='data_invariant':
            vals=[x['matches'] for run in repeated for x in run]
            varied=any(len({_canonical(run[i]['actual']) if run[i]['actual'] is not None else b'<invalid>' for run in repeated})>1 for i in range(len(scenarios)))
            return ('FLAKY' if varied else ('PASS' if all(vals) else 'FAIL'),'repeat responses inconsistent' if varied else ('all exact responses matched' if all(vals) else 'one or more responses differed'),'result')
        return ('UNRESOLVED','requires a scoped explicit measurement adapter','result')
    records=[]
    for cat in sorted(CATEGORIES):
        status,value,unit=category_status(cat)
        records.append(seal_record({'schema':1,'candidate_sha256':digest,'category':cat,'status':status,'value':value,'unit':unit,'source':f'application_quality.measure:{image}'}))
    result=evaluate(digest,policy,records,authorized_na_reasons=authorized_na_reasons)
    result['limitations']=['Measurements cover explicit operator-supplied JSONL scenarios plus optional seeded hidden inventory traces in fresh isolated containers.','Latency includes cold container startup. Resource records use controller-read cgroup-v2 memory.peak bytes and cpu.stat usage_usec; counters include isolated init/interpreter and bounded measurement overhead; output bytes are independently bounded.','Security is limited to explicitly named JSONL cases; anti_cheat covers only declared inventory-jsonl add/list behavior, not universal anti-cheat. Accessibility and compatibility require the real-browser adapter or exact operator-authorized N/A.']
    result['measurements']={'image_id':info['image_id'],'repeats':repeats,'scenarios':[s['name'] for s in scenarios],'runs':observations,'thresholds':{'max_latency_ms':latency,'max_output_bytes':output_limit,'max_peak_memory_bytes':memory_limit,'max_cpu_usage_usec':cpu_limit},'resource_scope':'trusted controller reads cgroup-v2 memory.peak bytes and cpu.stat CPU microseconds while isolated container remains alive; includes trusted init/interpreter overhead; output bytes are separate','security_scope':'only operator-authored named contract scenarios; no universal security claim','anti_cheat_scope':('inventory-jsonl add/list metamorphic cases only; no universal claim' if anti_policy else 'not measured; operator-supplied scoped policy required'),'anti_cheat_policy_sha256':_sha(anti_policy) if anti_policy else None,'anti_cheat_seed_sha256':hashlib.sha256(str(anti_policy['seed']).encode()).hexdigest() if anti_policy else None}
    result.pop('report_sha256',None); result['report_sha256']=_sha(result)
    return result
def _load(path):
    raw=Path(path).read_bytes()
    if len(raw)>4*1024*1024: raise QualityRefused('input exceeds 4 MiB')
    return json.loads(raw.decode('utf-8'))
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--candidate-sha256'); p.add_argument('--policy',type=Path); p.add_argument('--evidence',type=Path); p.add_argument('--candidate-manifest',type=Path); p.add_argument('--contract',type=Path); p.add_argument('--thresholds',type=Path); p.add_argument('--engine',choices=('docker','podman'),default='docker'); p.add_argument('--image'); p.add_argument('--authorized-na',action='append',default=[]); p.add_argument('--output',type=Path); a=p.parse_args()
    try:
        if a.candidate_manifest or a.contract or a.thresholds:
            if not all((a.candidate_manifest,a.contract,a.thresholds,a.image,a.output)) or a.policy or a.evidence or a.candidate_sha256:
                raise QualityRefused('measurement mode requires candidate-manifest, contract, thresholds, image and output only')
            manifest=_load(a.candidate_manifest); result=measure(manifest,_load(a.contract),_load(a.thresholds),engine=a.engine,image=a.image,authorized_na_reasons=a.authorized_na)
        else:
            if not all((a.candidate_sha256,a.policy,a.evidence)): raise QualityRefused('recorded mode requires candidate-sha256, policy and evidence')
            result=evaluate(a.candidate_sha256,_load(a.policy),_load(a.evidence),authorized_na_reasons=a.authorized_na)
        data=json.dumps(result,ensure_ascii=False,sort_keys=True)+'\n'
        if a.output: a.output.write_text(data,encoding='utf-8')
        else: print(data,end='')
        return 0 if result['status']=='PASS' else 2
    except (OSError,ValueError,json.JSONDecodeError,_builder.MissingPrerequisite,_builder.BuildFailure) as e:
        print(json.dumps({'schema':1,'status':'REFUSED','reason':str(e)},ensure_ascii=False),file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())