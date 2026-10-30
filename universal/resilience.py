"""Next-round real execution, persistence races, and proof/score fault injection."""
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

import machine

HERE=Path(__file__).resolve().parent
PROJECT=HERE.parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resign(payload):
    unsigned={k:v for k,v in payload.items() if k!='checksum'}
    payload['checksum']=hashlib.sha256(json.dumps(unsigned,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return payload


def main():
    root=HERE/'results'/('resilience-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    report=dict(status='RUNNING',checks=[],commands=[],errors=[],scope='finite stress and negative controls; not expanded proof certification')
    started=time.monotonic()
    protected=[HERE/'machine.py',HERE/'formal/Simulation.lean',HERE/'benchmark-policy.json']
    originals={str(p):digest(p) for p in protected}

    def save():
        report['elapsed_seconds']=round(time.monotonic()-started,4)
        report['source_hashes']={str(p.relative_to(PROJECT)):digest(p) for p in protected+[Path(__file__)]}
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

    def passed(name,**data):
        report['checks'].append(dict(name=name,status='PASS',**data));save();print('PASS:',name,flush=True)

    def command(argv,code=0,timeout=120):
        p=subprocess.run(list(map(str,argv)),capture_output=True,encoding='utf-8',timeout=timeout)
        report['commands'].append(dict(argv=list(map(str,argv)),exit=p.returncode,stdout=p.stdout,stderr=p.stderr));save()
        assert p.returncode==code,(argv,p.returncode,p.stdout,p.stderr)
        return p

    def cli(*args,code=0):
        p=command([sys.executable,HERE/'machine.py',*args],code)
        return json.loads(p.stdout if code==0 else p.stderr)

    def reject(fn):
        try:fn()
        except (ValueError,TypeError):return
        raise AssertionError('invalid state accepted')

    def rejects_existing(fn):
        try:fn()
        except FileExistsError:return
        raise AssertionError('conflicting existing output accepted')

    try:
        raw=json.loads((HERE/'examples/march.json').read_text());program=machine.compile_machine(raw)
        initial=machine.initial(program,[])
        full=machine.run(program,initial,100000,100000)
        assert full['status']=='RESOURCE_LIMIT' and full['executed']==99999
        obs=machine.observe(program,full['config'])
        assert obs==dict(state='march',head=99999,steps=99999,tape=[[i,'1'] for i in range(99999)])
        passed('exact-100000-slot-cap-before-overflow',executed=full['executed'])
        cfg=initial;total=0
        for _ in range(100):
            part=machine.run(program,cfg,1000,100000);total+=part['executed']
            # Actual JSON encoding/decoding at each boundary, not shared in-memory objects.
            payload=json.loads(json.dumps(machine.checkpoint(program,part)))
            cfg=machine.restore(program,payload)
        assert cfg==full['config'] and total==99999
        passed('100-json-checkpoint-resume-slices-match-single-run',slices=100,total_steps=total)
        longfile=root/'large-state.json';longfile.write_text(json.dumps(machine.checkpoint(program,full)),encoding='utf-8')
        value=cli('resume',HERE/'examples/march.json',longfile,'--steps',1,'--cells',100000,'--output',root/'capped.json')
        assert value['status']=='RESOURCE_LIMIT' and value['executed']==0
        passed('cli-restores-large-state-at-cap',checkpoint_bytes=longfile.stat().st_size)

        base=machine.checkpoint(program,machine.run(program,initial,5,100))
        mutations=[('negative-steps',lambda c:c.update(steps=-1)),('bool-head',lambda c:c.update(head=True)),
                   ('head-outside-history',lambda c:c.update(head=100)),('undeclared-state',lambda c:c.update(state='bad')),
                   ('undeclared-cell',lambda c:c['left'].append('bad')),('extra-field',lambda c:c.update(extra=1))]
        for name,mutate in mutations:
            altered=copy.deepcopy(base);mutate(altered['config']);resign(altered)
            reject(lambda:machine.restore(program,altered))
        passed('checksum-valid-but-invalid-configuration-rejected',cases=len(mutations))
        for label,content in [('truncated','{"schema":1'),('duplicate','{"schema":1,"schema":1}'),
                              ('nan','{"schema":NaN}'),('bad-utf8',b'\xff\xfe\x00')]:
            path=root/(label+'.json');path.write_bytes(content if isinstance(content,bytes) else content.encode())
            dest=root/(label+'-out.json')
            failure=cli('resume',HERE/'examples/march.json',path,'--steps',1,'--cells',100,'--output',dest,code=1)
            assert failure['error'] and not dest.exists()
        passed('malformed-json-encoding-and-duplicate-keys-rejected',cases=4)
        huge=root/'oversize.json';huge.write_bytes(b' '*(machine.MAX_INPUT_BYTES+1))
        error=cli('resume',HERE/'examples/march.json',huge,'--steps',1,'--cells',100,'--output',root/'oversize-out.json',code=1)
        assert 'exceeds' in error['error'];huge.unlink()
        passed('input-file-byte-limit-enforced')
        wide='x'*128
        wide_raw=dict(schema=1,states=['q'],symbols=['_',wide],blank='_',start='q',halt=[],
                      transitions=[dict(state='q',read='_',next='q',write=wide,move='R')])
        wide_path=root/'wide-symbol.json';wide_path.write_text(json.dumps(wide_raw))
        dest=root/'unresumable.json'
        failure=cli('run',wide_path,'--input','[]','--steps',40000,'--cells',50000,'--output',dest,code=1)
        assert 'checkpoint exceeds' in failure['error'] and not dest.exists()
        passed('output-checkpoint-byte-limit-prevents-unresumable-file')

        # Exercise publication boundaries in-process with filesystem-call fault injection.
        atomic_dir=root/'atomic';atomic_dir.mkdir()
        atomic=atomic_dir/'checkpoint.json';payload=b'{"ok":true}\n'
        real_unlink=machine.os.unlink
        def fail_temp_cleanup(name, *args, **kwargs):
            if Path(name).name.startswith('.checkpoint.json.') and str(name).endswith('.tmp'):
                raise PermissionError('injected cleanup refusal')
            return real_unlink(name, *args, **kwargs)
        machine.os.unlink=fail_temp_cleanup
        try:
            warnings=machine._atomic_create(atomic,payload)
        finally:
            machine.os.unlink=real_unlink
        assert atomic.read_bytes()==payload and any('cleanup failed' in warning for warning in warnings)
        assert machine._atomic_create(atomic,payload)==[]
        rejects_existing(lambda:machine._atomic_create(atomic,b'different'))
        passed('checkpoint-postcommit-cleanup-warning-and-idempotent-retry')

        failed=atomic_dir/'failed.json'
        real_publish=machine._publish_no_replace
        def fail_publish(*args,**kwargs):raise OSError('injected publication failure')
        machine._publish_no_replace=fail_publish
        try:
            try:machine._atomic_create(failed,payload)
            except OSError:pass
            else:raise AssertionError('link failure did not propagate')
        finally:machine._publish_no_replace=real_publish
        assert not failed.exists()
        passed('checkpoint-precommit-link-failure-leaves-no-destination')

        symlink=atomic_dir/'linked.json'
        try:
            symlink.symlink_to(atomic)
        except OSError as exc:
            if getattr(exc,'winerror',None)!=1314:raise
            report['checks'].append(dict(name='checkpoint-symlink-target-rejected',status='NOT_EVALUATED',reason='Windows symlink creation privilege unavailable'))
            save()
        else:
            rejects_existing(lambda:machine._atomic_create(symlink,payload))
            passed('checkpoint-symlink-target-rejected')

        failed_sync=atomic_dir/'failed-sync.json'
        real_fsync=machine.os.fsync
        def fail_fsync(fd):raise OSError('injected file sync failure')
        machine.os.fsync=fail_fsync
        try:
            try:machine._atomic_create(failed_sync,payload)
            except OSError:pass
            else:raise AssertionError('file sync failure did not propagate')
        finally:machine.os.fsync=real_fsync
        assert not failed_sync.exists()
        passed('checkpoint-precommit-file-sync-failure-leaves-no-destination')

        race_dest=root/'race.json'
        argv=[sys.executable,str(HERE/'machine.py'),'run',str(HERE/'examples/increment.json'),'--input','["1","1"]',
              '--steps','20','--cells','100','--output',str(race_dest)]
        children=[]
        try:
            children=[subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf-8') for _ in range(2)]
            results=[]
            for child in children:
                out,err=child.communicate(timeout=30);results.append(child.returncode)
                report['commands'].append(dict(argv=argv,exit=child.returncode,stdout=out,stderr=err))
            assert sorted(results)==[0,0]
            inc=machine.compile_machine(json.loads((HERE/'examples/increment.json').read_text()))
            state=machine.restore(inc,json.loads(race_dest.read_text()))
            assert machine.observe(inc,state)['tape']==[[-1,'1'],[0,'0'],[1,'0']]
            assert not list(root.glob('.race.json.*.tmp'))
        finally:
            for child in children:
                if child.poll() is None:child.kill();child.wait()
        passed('concurrent-identical-checkpoint-publication-is-idempotent',exit_codes=results)

        # Worker handshake proves one transition happened before parent requests stop.
        helper=root/'stop_worker.py'
        helper.write_text('import sys,json\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport machine\np=machine.compile_machine(json.loads(Path(sys.argv[1],"examples/march.json").read_text()))\nc=machine.run(p,machine.initial(p,[]),1,100000)["config"]\nprint("READY",flush=True)\nr=machine.run(p,c,1000000,100000,Path(sys.argv[2]))\nprint(json.dumps({"status":r["status"],"executed":r["executed"],"head":r["config"]["head"],"steps":r["config"]["steps"]}),flush=True)\n',encoding='utf-8')
        stop=root/'STOP'
        proc=subprocess.Popen([sys.executable,str(helper),str(HERE),str(stop)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf-8')
        try:
            q=queue.Queue();thread=threading.Thread(target=lambda:q.put(proc.stdout.readline()),daemon=True);thread.start()
            assert q.get(timeout=10).strip()=='READY'
            stop_start=time.perf_counter();stop.write_text('operator stop',encoding='utf-8')
            stdout,stderr=proc.communicate(timeout=10);latency=time.perf_counter()-stop_start
            result=json.loads(stdout)
            assert proc.returncode==0 and result['status']=='STOPPED' and result['steps']>=1
            assert result['head']==result['steps']
            report['commands'].append(dict(action='running-stop',stdout=stdout,stderr=stderr,exit=proc.returncode))
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
        passed('stop-request-after-worker-start',response_seconds=latency,worker_result=result)

        # Change a semantic definition, not only a trivial theorem: proofs must catch it.
        lean=PROJECT/'runtime/lean-4.35.0-windows/bin/lean.exe'
        formal=(HERE/'formal/Simulation.lean').read_text(encoding='utf-8')
        needle='| .here => .right 0'
        assert formal.count(needle)==1
        mutant=root/'WrongShift.lean';mutant.write_text(formal.replace(needle,'| .here => .right 1'),encoding='utf-8')
        failed=command([lean,mutant],1)
        assert 'error:' in failed.stdout and ('unsolved goals' in failed.stdout or 'Type mismatch' in failed.stdout)
        passed('lean-rejects-real-head-shift-semantic-mutation')
        command([sys.executable,HERE/'kernel_check.py'])
        passed('unchanged-nine-theorem-kernel-suite')

        altered=root/'mutant';altered.mkdir()
        for name in ['machine.py','verify.py','benchmark.py','benchmark-policy.json','PROOF.md','CONTRACT.md']:
            shutil.copy2(HERE/name,altered/name)
        shutil.copytree(HERE/'examples',altered/'examples')
        implementation=(altered/'machine.py').read_text(encoding='utf-8')
        needle='config["steps"] += 1';assert implementation.count(needle)==1
        (altered/'machine.py').write_text(implementation.replace(needle,'config["steps"] += 2'),encoding='utf-8')
        negative=root/'negative-score'
        command([sys.executable,altered/'benchmark.py','--repeats',3,'--output',negative],1)
        score=json.loads((negative/'report.json').read_text())
        assert score['score']==0 and score['status']=='FAIL'
        passed('score-correctness-gate-rejects-mutated-counter')
        bench=root/'benchmark'
        command([sys.executable,HERE/'benchmark.py','--repeats',11,'--output',bench])
        score=json.loads((bench/'report.json').read_text())
        assert score['status']=='PASS' and 0<=score['score']<=100
        # Recompute displayed weighted score independently from raw measurements and policy.
        policy=score['policy'];weighted=0;weight_sum=0
        for name,w in policy['weights'].items():
            if name in score['workloads']:
                measured=score['workloads'][name]['steps']/score['workloads'][name]['median_seconds']
                points=min(100,100*measured/policy['targets'][name+'_transitions_per_second'])
            else:
                component='restore' if name.startswith('restore') else 'checkpoint'
                stat='p95_seconds_nearest_rank' if name.endswith('p95') else 'median_seconds'
                measured=score['checkpoint'][component][stat]
                points=min(100,100*policy['targets'][name+'_seconds_max']/measured)
            weighted+=w*points;weight_sum+=w
        assert abs(round(weighted/weight_sum,4)-score['score'])<.00001
        passed('baseline-benchmark-and-independent-score-recomputation',score=score['score'])
        assert all(digest(Path(path))==value for path,value in originals.items())
        passed('production-runtime-proof-and-policy-unchanged')
        report['status']='PASS'
    except Exception as error:
        report['status']='FAIL';report['errors'].append(f'{type(error).__name__}: {error}');raise
    finally:
        save();print('Evidence:',root/'report.json',flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
