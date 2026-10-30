"""Finite conformance against an independent sparse-tape oracle, not a TC certificate."""
import argparse
import copy
import hashlib
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import time
import uuid

import machine

HERE = Path(__file__).resolve().parent


def transition(q, read, nxt, write, move):
    return dict(state=q, read=read, next=nxt, write=write, move=move)


def definition(transitions, states=('q', 'h'), symbols=('_', '0', '1'), halt=('h',)):
    return dict(schema=1, states=list(states), symbols=list(symbols), blank='_',
                start=states[0], halt=list(halt), transitions=transitions)


def oracle_initial(raw, tape):
    return dict(state=raw['start'], head=0, steps=0,
                tape={i:s for i,s in enumerate(tape) if s != raw['blank']})


def oracle_next(raw, cfg):
    if cfg['state'] in raw['halt']:
        return None
    symbol = cfg['tape'].get(cfg['head'], raw['blank'])
    return next((row for row in raw['transitions']
                 if row['state']==cfg['state'] and row['read']==symbol), None)


def oracle_step(raw, cfg):
    row = oracle_next(raw, cfg)
    if row is None:
        raise ValueError('no successor')
    tape = dict(cfg['tape'])
    if row['write'] == raw['blank']:
        tape.pop(cfg['head'], None)
    else:
        tape[cfg['head']] = row['write']
    return dict(state=row['next'], head=cfg['head']+{'L':-1,'R':1,'S':0}[row['move']],
                steps=cfg['steps']+1, tape=tape)


def observation(cfg):
    return {**{k:cfg[k] for k in ('state','head','steps')},
            'tape': [[i,s] for i,s in sorted(cfg['tape'].items())]}


def reject(function):
    try:
        function()
    except (ValueError, TypeError):
        return
    raise AssertionError('invalid operation accepted')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed',type=int,default=2112026)
    parser.add_argument('--random-cases',type=int,default=600)
    args=parser.parse_args()
    if args.random_cases < 500:
        parser.error('at least 500 cases required')
    output=HERE/'results'/('verify-'+uuid.uuid4().hex[:12]);output.mkdir(parents=True)
    report=dict(seed=args.seed,finite_conformance='RUNNING',constructive_proof='NOT_KERNEL_CERTIFIED',
                dimensions=[],random_cases=0,local_cases=0,comparisons=0,cli=[],errors=[])
    started=time.monotonic()

    def save():
        report['elapsed_seconds']=round(time.monotonic()-started,3)
        report['file_sha256']={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest()
                               for name in ('machine.py','verify.py','PROOF.md','CONTRACT.md')}
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

    def passed(name):
        report['dimensions'].append(name);save();print('PASS:',name,flush=True)

    def compare(program, actual, expected):
        assert machine.observe(program,actual)==observation(expected), (actual,expected)
        report['comparisons']+=1

    def differential(raw,tape,limit=100):
        program=machine.compile_machine(raw)
        actual=machine.initial(program,tape);expected=oracle_initial(raw,tape)
        compare(program,actual,expected)
        for _ in range(limit):
            row=oracle_next(raw,expected)
            if row is None:
                reject(lambda:machine.step(program,actual));break
            old=copy.deepcopy(actual)
            actual=machine.step(program,actual)
            assert old != actual  # cumulative steps must advance even when tape does not
            expected=oracle_step(raw,expected);compare(program,actual,expected)
        return program,actual,expected

    def cli(argv,code=0):
        proc=subprocess.run([sys.executable,str(HERE/'machine.py'),*map(str,argv)],
                            capture_output=True,encoding='utf-8',timeout=30)
        report['cli'].append(dict(args=list(map(str,argv)),returncode=proc.returncode,
                                  stdout=proc.stdout,stderr=proc.stderr));save()
        assert proc.returncode==code,(argv,proc.stdout,proc.stderr)
        return json.loads(proc.stdout if code==0 else proc.stderr)

    try:
        # Exhaust all combinations in this explicitly bounded neighborhood family.
        contexts=[[],['0'],['1','_']]
        for move,write,left,right in itertools.product('LRS',('_','0','1'),contexts,contexts):
            raw=definition([transition('q','0','h',write,move)])
            p=machine.compile_machine(raw)
            cfg=dict(state='q',left=list(left),current='0',right=list(right),head=-2,steps=3)
            sparse={-2:'0'}
            for j,s in enumerate(reversed(left),1):
                if s!='_':sparse[-2-j]=s
            for j,s in enumerate(reversed(right),1):
                if s!='_':sparse[-2+j]=s
            expected=dict(state='q',head=-2,steps=3,tape=sparse)
            compare(p,cfg,expected)
            untouched=copy.deepcopy(cfg);got=machine.step(p,cfg)
            assert cfg==untouched
            compare(p,got,oracle_step(raw,expected));reject(lambda:machine.step(p,got))
            report['local_cases']+=1
        passed('bounded-exhaustive-L-R-S-write-stack-neighborhoods')
        rng=random.Random(args.seed)
        for _ in range(args.random_cases):
            states=[f'q{i}' for i in range(rng.randint(1,4))]+['h']
            rows=[transition(q,s,rng.choice(states),rng.choice(['_','0','1']),rng.choice('LRS'))
                  for q in states[:-1] for s in ['_','0','1'] if rng.random()<.72]
            raw=definition(rows,states)
            differential(raw,[rng.choice(['_','0','1']) for _ in range(rng.randint(0,8))],100)
            report['random_cases']+=1
        passed('seeded-arbitrary-machine-sparse-tape-differential')
        increment=json.loads((HERE/'examples/increment.json').read_text())
        p,got,_=differential(increment,['1','1','1'])
        assert machine.observe(p,got)['tape']==[[-1,'1'],[0,'0'],[1,'0'],[2,'0']]
        passed('binary-increment-with-negative-head-and-carry')
        unary=definition([transition('scan','1','scan','1','R'),transition('scan','+','end','1','R'),
                          transition('end','1','end','1','R'),transition('end','_','erase','_','L'),
                          transition('erase','1','h','_','S')],('scan','end','erase','h'),('_','1','+'))
        for a,b in [(0,0),(0,3),(2,1),(4,0),(5,7)]:
            p,c,_=differential(unary,['1']*a+['+']+['1']*b)
            assert machine.observe(p,c)['tape']==[[i,'1'] for i in range(a+b)]
        passed('unary-addition-including-zero-operands')
        flip=definition([transition('q','0','h','1','S'),transition('q','1','h','0','S')])
        for symbol,want in [('0','1'),('1','0')]:
            p,c,_=differential(flip,[symbol]);assert machine.observe(p,c)['tape']==[[0,want]]
        passed('stationary-write-bit-flip')
        erase=definition([transition('q','1','h','_','S')]);p,c,_=differential(erase,['1'])
        assert machine.observe(p,c)['tape']==[]
        passed('blank-write-erases-sparse-tape-cell')
        p,c,_=differential(increment,[]);assert c['state']=='done' and c['head']==-1
        passed('empty-input-and-left-extension')
        for tape in [[],['0'],['1'],['1','0','1'],['1']*32]:
            p,oracle_final,_=differential(increment,tape,200)
            cfg=machine.initial(p,tape);original=copy.deepcopy(cfg)
            whole=machine.run(p,cfg,200,200);total=0
            assert cfg==original
            while True:
                part=machine.run(p,cfg,1,200);total+=part['executed']
                cfg=machine.restore(p,machine.checkpoint(p,part))
                if part['status']!='PAUSED':break
            assert cfg==whole['config']==oracle_final and total==whole['executed']
        passed('nontrivial-one-step-checkpoint-resume-equivalence')
        loop=definition([transition('q','_','q','_','S')],('q',),('_',),())
        lp=machine.compile_machine(loop);initial=machine.initial(lp,[])
        paused=machine.run(lp,initial,37,2);assert paused['status']=='PAUSED' and paused['executed']==37
        passed('nonhalting-machine-pauses-not-halts')
        hp=machine.compile_machine(definition([transition('q','_','h','_','S')]))
        assert machine.run(hp,machine.initial(hp,[]),1,1)['status']=='HALTED'
        sp=machine.compile_machine(definition([]))
        assert machine.run(sp,machine.initial(sp,[]),0,1)['status']=='STUCK'
        passed('halt-stuck-versus-budget-precedence')
        march=json.loads((HERE/'examples/march.json').read_text());mp=machine.compile_machine(march)
        cfg=machine.initial(mp,[]);limited=machine.run(mp,cfg,20,4)
        assert limited['status']=='RESOURCE_LIMIT' and limited['executed']==3
        assert limited['config']==machine.run(mp,cfg,3,100)['config']
        assert machine.run(mp,machine.initial(mp,['_']*5),1,4)['executed']==0
        passed('resource-refusal-keeps-pre-transition-state')
        stop=output/'STOP';stop.write_text('operator',encoding='utf-8')
        stopped=machine.run(mp,limited['config'],20,100,stop)
        assert stopped['status']=='STOPPED' and stopped['executed']==0 and stopped['config']==limited['config']
        passed('operator-stop-priority')
        cp=machine.checkpoint(lp,paused);corrupt=copy.deepcopy(cp);corrupt['config']['steps']+=1
        reject(lambda:machine.restore(lp,corrupt));reject(lambda:machine.restore(mp,cp))
        assert machine.restore(lp,cp)==paused['config']
        passed('checkpoint-tamper-and-cross-machine-rejection')
        invalid=[{**loop,'extra':1},{**loop,'schema':True},{**loop,'states':['q','q']},
                 {**loop,'symbols':['_','_']},{**loop,'transitions':loop['transitions']*2},
                 {**loop,'transitions':[transition('q','x','q','_','S')]},
                 {**loop,'halt':['q']},{**loop,'transitions':[transition('q','_','q','_','X')]}]
        for raw in invalid:reject(lambda raw=raw:machine.compile_machine(raw))
        passed('malformed-schema-and-transition-rejection')
        raw=copy.deepcopy(march);p=machine.compile_machine(raw);raw['states'].clear();view=p.raw;view['transitions'].clear()
        assert machine.run(p,machine.initial(p,[]),1,10)['executed']==1
        cfg=machine.initial(p,[]);before=copy.deepcopy(cfg);machine.step(p,cfg);machine.run(p,cfg,3,10)
        assert cfg==before
        passed('program-freeze-and-caller-state-nonmutation')
        source=output/'machine.json';source.write_text(json.dumps(increment),encoding='utf-8')
        first=output/'first.json';last=output/'last.json'
        a=cli(['run',source,'--input','["1","1","1"]','--steps',3,'--cells',100,'--output',first])
        b=cli(['resume',source,first,'--steps',20,'--cells',100,'--output',last])
        assert a['status']=='PAUSED' and b['status']=='HALTED' and b['observation']['steps']==8
        old=first.read_bytes();cli(['run',source,'--input','[]','--steps',1,'--cells',10,'--output',first],1)
        assert first.read_bytes()==old
        passed('real-cli-pause-resume-and-no-overwrite')
        for label,content,needle in [('unknown',json.dumps({**loop,'extra':1}),'unknown'),('duplicate','{"schema":1,"schema":1}','duplicate')]:
            bad=output/(label+'.json');bad.write_text(content,encoding='utf-8');dest=output/(label+'-result.json')
            error=cli(['run',bad,'--input','[]','--steps',1,'--cells',10,'--output',dest],1)
            assert needle in error['error'] and not dest.exists()
        passed('cli-parses-and-rejects-unknown-and-duplicate-fields')
        for steps,cells,needle in [(1000001,10,'steps'),(1,100001,'cells')]:
            dest=output/f'cap-{needle}.json'
            error=cli(['run',source,'--input','[]','--steps',steps,'--cells',cells,'--output',dest],1)
            assert needle in error['error'] and not dest.exists()
        passed('cli-hard-step-and-storage-caps')
        report['finite_conformance']='PASS'
    except Exception as exc:
        report['finite_conformance']='FAIL';report['errors'].append(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        save();print('Evidence:',output/'report.json',flush=True)
    print(json.dumps({k:report[k] for k in ('finite_conformance','constructive_proof','random_cases','local_cases','comparisons','elapsed_seconds')},indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
