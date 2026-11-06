"""Real provider replacement via existing project ABI, without rebuilding the controller.

The alternate providers are operator-authored fixtures, not autonomous self-modification.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
EXE = PROJECT / 'target/release/autoforge.exe'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    output = PROJECT / 'bootstrap-runs' / ('extension-' + uuid.uuid4().hex[:12])
    output.mkdir(parents=True)
    staged = output / 'alternate-project'
    (staged / 'providers').mkdir(parents=True)
    for file in ['python_agent.py', 'julia_agent.jl']:
        shutil.copy2(PROJECT / 'fixtures/bitwise' / file, staged / 'providers' / file)
    for file in ['Project.toml', 'Manifest.toml', 'protocol.jl']:
        shutil.copy2(PROJECT / 'providers' / file, staged / 'providers' / file)
    shutil.copy2(PROJECT / 'Cargo.lock', staged / 'Cargo.lock')
    environment = {k:v for k,v in os.environ.items() if not k.startswith('AUTOFORGE_')}
    environment.update(AUTOFORGE_PROJECT=str(staged), AUTOFORGE_PYTHON=sys.executable,
                       AUTOFORGE_JULIA=str(PROJECT/'runtime/julia-1.13.3/bin/julia.exe'),
                       AUTOFORGE_PROVIDER='offline')
    report = dict(status='running', controller_before=digest(EXE), checks=[], commands=[],
                  qualification='operator-authored extension; no autonomous grammar invention')
    report_path = output / 'report.json'

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def call(*args, code=0):
        start = time.monotonic()
        p = subprocess.run([str(EXE), *map(str,args)], cwd=PROJECT, env=environment,
                           capture_output=True, encoding='utf-8', timeout=210)
        report['commands'].append(dict(args=list(map(str,args)), exit=p.returncode,
                                      stdout=p.stdout, stderr=p.stderr,
                                      elapsed_seconds=round(time.monotonic()-start,4)))
        save()
        if p.returncode != code:
            raise AssertionError(f'{args}: expected {code}, got {p.returncode}: {p.stdout} {p.stderr}')
        return json.loads(p.stdout or p.stderr)

    def check(name):
        report['checks'].append(name)
        print('PASS:',name,flush=True)
        save()

    state = output / 'state'
    task = output / 'xor.json'
    points = [(0,0),(1,1),(2,3),(-1,2),(91,-11),(7,12),(-(1<<63),(1<<63)-1)]
    task.write_text(json.dumps(dict(schema=1,id='bitwise-extension',goal='64-bit XOR',
        public_cases=[dict(input=[a,b],expected=a^b) for a,b in points],
        holdout_cases=[dict(input=[13,27],expected=13^27),dict(input=[-32,87],expected=-32^87)],
        max_rounds=2)), encoding='utf-8')
    original_python = (staged/'providers/python_agent.py').read_bytes()
    try:
        call('init',state)
        promoted = call('evolve',state,task)
        assert promoted['decision']=='PROMOTE'
        for a,b in [(122,-19),(-(1<<63),-1),(4567,8888)]:
            assert call('execute',state,a,b)['output']==a^b
        check('New bitwise grammar accepted through unchanged controller protocol')
        assert digest(EXE)==report['controller_before']
        report['controller_after']=digest(EXE)
        check('Controller binary unchanged across extension admission')
        release=json.loads((state/'blobs'/promoted['release']).read_text())['payload']
        assert release['manifest']['python_provider']==digest(staged/'providers/python_agent.py')
        assert release['manifest']['julia_provider']==digest(staged/'providers/julia_agent.jl')
        check('Alternate provider identities bound in signed release provenance')
        assert call('replay',state)['verified_releases']==1
        check('Extension artifact source rebuild and execution replay')
        (staged/'providers/python_agent.py').rename(staged/'providers/python_agent.disabled')
        failure=call('evolve',state,task,code=1)
        assert 'python_agent.py' in failure['error']
        assert call('status',state)['active']==promoted['release']
        check('Unavailable extension proposer fails closed without fallback promotion')
        (staged/'providers/python_agent.disabled').rename(staged/'providers/python_agent.py')
        # Mutant emits an invalid typed ABI. It is deliberately faulty, not a claimed implementation.
        (staged/'providers/python_agent.py').write_text(
            'import json,sys\njson.load(sys.stdin)\nprint(json.dumps({"wat":\'(module (func (export "solve") (param i32) (result i32) local.get 0))\',"strategy":"ABI-mutation"}))\n',encoding='utf-8')
        assert call('evolve',state,task,code=2)['decision']=='HOLD'
        assert call('status',state)['active']==promoted['release']
        check('Incompatible extension ABI rejected; accepted artifact remains active')
        (staged/'providers/python_agent.py').write_bytes(original_python)
        assert call('execute',state,77,51)['output']==77^51
        report['status']='passed'
    except Exception as error:
        report['status']='failed'
        report['error']=str(error)
        raise
    finally:
        (staged/'providers/python_agent.py').write_bytes(original_python)
        disabled=staged/'providers/python_agent.disabled'
        if disabled.exists():
            disabled.unlink()
        save()
        print('Evidence:',report_path,flush=True)


if __name__ == '__main__':
    main()
