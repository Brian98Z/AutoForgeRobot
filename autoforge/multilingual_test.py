"""Exercise Rust controller / Python and Julia providers with exact i64 UTF-8 wire data."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import paths

ROOT=Path(__file__).resolve().parents[1]


def main():
    out=ROOT/'bootstrap-runs'/('multilingual-'+uuid.uuid4().hex[:12]);out.mkdir(parents=True)
    env=dict(os.environ,AUTOFORGE_PROJECT=str(ROOT),AUTOFORGE_PROVIDER='offline')
    exe=paths.release_binary(ROOT)
    julia=paths.julia_binary(ROOT)
    providers={'python':[sys.executable,ROOT/'providers/python_agent.py'],
               'julia':[julia,'--startup-file=no',f'--project={ROOT / "providers"}',ROOT/'providers/julia_agent.jl']}
    report=dict(status='RUNNING',checks=[],provider_invalid_inputs=[],commands=[],errors=[])
    start=time.monotonic()
    def invoke(argv,data=None):
        p=subprocess.run(list(map(str,argv)),input=data,capture_output=True,cwd=ROOT,env=env,timeout=240)
        entry=dict(argv=list(map(str,argv)),exit=p.returncode,stdout=p.stdout.decode('utf-8'),stderr=p.stderr.decode('utf-8'))
        report['commands'].append(entry)
        return entry
    def call(*args,code=0):
        r=invoke([exe,*args]);assert r['exit']==code,r
        return json.loads(r['stdout'] or r['stderr'])
    def passed(name,**extra):
        report['checks'].append(dict(name=name,**extra));print('PASS:',name,flush=True)
    def wrap(n):return (n+(1<<63))%(1<<64)-(1<<63)
    try:
        report['versions']={name:invoke(argv)['stdout'].strip() for name,argv in
            [('python',[sys.executable,'--version']),('julia',[julia,'--version']),('rustc',['rustc','--version'])]}
        report['source_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
            [ROOT/'providers/python_agent.py',ROOT/'providers/julia_agent.jl',ROOT/'providers/protocol.jl',exe]}
        state=out/'中文 状态 é';call('init',state)
        values=[-(1<<63),-(1<<63)+1,-9007199254740993,-1,0,1,9007199254740993,(1<<63)-1]
        points=[(x,y) for x in values for y in values]
        for index,(name,fn) in enumerate([('加法',lambda x,y:wrap(x+y)),('乘法',lambda x,y:wrap(x*y)),('最小值',min),('最大值',max)]):
            task=dict(schema=1,id=f'跨语言-{index}',goal=f'{name} / café / 日本語 / العربية / 😀',
                      public_cases=[dict(input=[x,y],expected=fn(x,y)) for x,y in points],
                      holdout_cases=[dict(input=[17,-23],expected=fn(17,-23))],max_rounds=2)
            path=out/(name+' é.json');path.write_text(json.dumps(task,ensure_ascii=False),encoding='utf-8')
            result=call('evolve',state,path);assert result['decision']=='PROMOTE',result
            for x,y in points:
                assert call('execute',state,x,y)['output']==fn(x,y)
            passed('dual-provider-Rust-WASM-'+name,executions=len(points),release=result['release'])
        assert call('replay',state)['verified_releases']==4
        passed('unicode-paths-goals-and-four-release-replay')
        previous=call('status',state)['active']
        invalid=['9223372036854775808','-9223372036854775809','1.5','1e3','true','"12"']
        for token in invalid:
            raw='{"schema":1,"id":"invalid","goal":"拒绝","public_cases":[{"input":['+token+',1],"expected":0}],"holdout_cases":[{"input":[17,23],"expected":40}],"max_rounds":1}'
            path=out/'invalid.json';path.write_text(raw,encoding='utf-8')
            refusal=call('evolve',state,path,code=1)
            assert 'InvalidContract: schema/goal/cases/round limits' not in refusal['error'],refusal
            assert call('status',state)['active']==previous
        passed('Rust-rejects-invalid-i64-without-state-change',cases=len(invalid))
        # Validate each numeric position before baseline reuse can bypass synthesis.
        baseline='(module (func (export "solve") (param i64 i64) (result i64) i64.const 0))'
        for field in ('public-input','public-expected','feedback-input','feedback-expected','schema','round'):
            for token in invalid:
                request=dict(schema=1,task=dict(id='invalid',goal='reject',public_cases=[dict(input=[0,1],expected=1)]),
                             round=1,baseline_wat=baseline,feedback=[dict(input=[0,1],expected=1)])
                if field=='public-input':request['task']['public_cases'][0]['input'][0]='TOKEN'
                elif field=='public-expected':request['task']['public_cases'][0]['expected']='TOKEN'
                elif field=='feedback-input':request['feedback'][0]['input'][0]='TOKEN'
                elif field=='feedback-expected':request['feedback'][0]['expected']='TOKEN'
                else:request[field]='TOKEN'
                raw=json.dumps(request).replace('"TOKEN"',token).encode()
                responses={name:invoke(argv,raw) for name,argv in providers.items()}
                outcomes={name:r['exit'] for name,r in responses.items()}
                report['provider_invalid_inputs'].append(dict(field=field,token=token,exit_codes=outcomes))
                assert all(r['exit']==2 and not r['stdout'] for r in responses.values()),(field,token,responses)
        passed('both-providers-strict-numeric-wire-rejection',requests=72)
        for label,raw in [('unsupported-schema',b'{"schema":2,"task":{"id":"x","goal":"x","public_cases":[]},"round":1,"baseline_wat":null,"feedback":[]}'),('truncated-json',b'{'),('oversize',b' '*262145)]:
            for name,argv in providers.items():
                r=invoke(argv,raw);assert r['exit']==2 and not r['stdout'],r
            passed('both-providers-reject-'+label)
        report['status']='PASS'
        report['standalone_parser_equivalence']='DIVERGENT' if any(len(set(row['exit_codes'].values()))>1 for row in report['provider_invalid_inputs']) else 'MATCHED_FOR_TESTED_CASES'
        report['compatibility_verdict']='PARTIAL' if report['standalone_parser_equivalence']=='DIVERGENT' else 'PASS_FOR_TESTED_CASES'
    except Exception as exc:
        report['status']='FAIL';report['errors'].append(f'{type(exc).__name__}: {exc}');raise
    finally:
        report['elapsed_seconds']=time.monotonic()-start
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Evidence:',out/'report.json',flush=True)


if __name__=='__main__':main()
