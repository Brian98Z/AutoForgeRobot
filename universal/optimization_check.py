"""Alternating-order before/after comparison against a frozen implementation.
Baseline path is trusted operator code. No default historical implementation is bundled.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import statistics
import time
import tracemalloc
import uuid

import machine

HERE=Path(__file__).resolve().parent


def load(path):
    spec=importlib.util.spec_from_file_location('frozen_baseline',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=21)
    args=parser.parse_args()
    if args.repeats<7:parser.error('at least 7 alternating-order samples required')
    before=load(args.baseline)
    root=HERE/'results'/('optimization-check-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    report=dict(status='RUNNING',baseline=str(args.baseline),repeats=args.repeats,regressions={},metrics={},errors=[],
                baseline_sha256=hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
                current_sha256=hashlib.sha256((HERE/'machine.py').read_bytes()).hexdigest())
    raw=json.loads((HERE/'examples/march.json').read_text())
    programs=[m.compile_machine(raw) for m in (before,machine)]
    cfg=dict(state='march',left=['1','_']*20000,current='1',right=['_','1']*20000,head=-100,steps=80000)
    snapshot=copy.deepcopy(cfg)
    initial_results=[dict(status='PAUSED',config=copy.deepcopy(cfg),executed=0) for _ in range(2)]
    cp=[m.checkpoint(p,r) for m,p,r in zip((before,machine),programs,initial_results)]
    try:
        # Exact historical wire bytes/digest and absolute-order observations must be preserved.
        assert cp[0]==cp[1]
        observed=machine.observe(programs[1],cfg)
        expected=before.observe(programs[0],cfg)
        assert observed==expected and observed['tape']==sorted(observed['tape'])
        assert cfg==snapshot
        restored=machine.restore(programs[1],cp[0]);restored['left'].append('1')
        assert cp[0]['config']==snapshot
        report['regressions']['negative-head-both-stacks-order-and-checkpoint-compatibility']='PASS'
        rejected=0
        for side in ('left','right'):
            for index in (0,20000,39999):
                for invalid in (None,False,[],42,'undeclared'):
                    value=copy.deepcopy(cfg);value[side][index]=invalid
                    for mod,p in zip((before,machine),programs):
                        try:mod.observe(p,value)
                        except (ValueError,TypeError):rejected+=1
                        else:raise AssertionError('invalid stack member accepted')
        report['regressions']['invalid-stack-member-rejections']=rejected
        rng=random.Random(20261031)
        for _ in range(500):
            small=dict(state='march',left=[rng.choice(['_','1']) for _ in range(rng.randrange(25))],
                       current=rng.choice(['_','1']),right=[rng.choice(['_','1']) for _ in range(rng.randrange(25))],
                       head=rng.randrange(-50,51),steps=100)
            assert machine.observe(programs[1],small)==before.observe(programs[0],small)
            assert machine.run(programs[1],small,20,1000)==before.run(programs[0],small,20,1000)
        report['regressions']['random-two-stack-equivalence']=500
        absent=root/'absent-STOP'
        operations={
            'observe_80001_slots':lambda m,p,i:m.observe(p,cfg),
            'checkpoint_80001_slots':lambda m,p,i:m.checkpoint(p,initial_results[i]),
            'restore_80001_slots':lambda m,p,i:m.restore(p,cp[i]),
            'run_30000_steps':lambda m,p,i:m.run(p,m.initial(p,[]),30000,100000),
            'run_5000_steps_stop_check':lambda m,p,i:m.run(p,m.initial(p,[]),5000,100000,absent),
        }
        for name,operation in operations.items():
            assert operation(before,programs[0],0)==operation(machine,programs[1],1)
            samples=[[],[]]
            for turn in range(args.repeats):
                for i in ((0,1) if turn%2==0 else (1,0)):
                    start=time.perf_counter();result=operation((before,machine)[i],programs[i],i)
                    samples[i].append(time.perf_counter()-start)
            med=[statistics.median(s) for s in samples]
            peaks=[]
            for i in range(2):
                tracemalloc.start()
                try:
                    operation((before,machine)[i],programs[i],i)
                    peaks.append(tracemalloc.get_traced_memory()[1])
                finally:tracemalloc.stop()
            report['metrics'][name]=dict(before_seconds=samples[0],after_seconds=samples[1],
                before_median_seconds=med[0],after_median_seconds=med[1],speedup=med[0]/med[1],
                before_tracemalloc_peak=peaks[0],after_tracemalloc_peak=peaks[1])
            print(name,'speedup',round(med[0]/med[1],3),flush=True)
        assert cfg==snapshot
        report['status']='PASS'
    except Exception as exc:
        report['status']='FAIL';report['errors'].append(f'{type(exc).__name__}: {exc}');raise
    finally:
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Evidence:',root/'report.json',flush=True)


if __name__=='__main__':main()
