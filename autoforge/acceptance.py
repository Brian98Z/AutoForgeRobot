"""Exercise the real CLI and runtimes. No external model calls; no mocks."""
import json
import os
import pathlib
import subprocess
import tempfile
import time

PROJECT = pathlib.Path(__file__).resolve().parents[1]
EXE = PROJECT / 'target/release/autoforge.exe'
ENV = dict(os.environ, AUTOFORGE_PROJECT=str(PROJECT), AUTOFORGE_PROVIDER='offline')
RESULTS = []


def call(*args, code=0):
    p = subprocess.run([str(EXE), *map(str, args)], cwd=PROJECT, env=ENV,
                       capture_output=True, text=True, encoding='utf-8', timeout=240)
    if p.returncode != code:
        raise AssertionError(f'{args}: exit {p.returncode}, expected {code}\n{p.stdout}\n{p.stderr}')
    return json.loads(p.stdout if p.stdout.strip() else p.stderr)


def record(name):
    RESULTS.append(name)
    print('PASS:', name, flush=True)


def save_task(folder, name, fn, *, task_id=None):
    points = [(0, 0), (2, 5), (5, 2), (-8, 3), (7, -9), (-4, -4), (17, 2)]
    task = dict(schema=1, id=task_id or name, goal=name,
                public_cases=[dict(input=list(p), expected=fn(*p)) for p in points],
                holdout_cases=[dict(input=[-23, 11], expected=fn(-23, 11)),
                               dict(input=[11, -23], expected=fn(11, -23))], max_rounds=2)
    path = folder / (name + '.json')
    path.write_text(json.dumps(task), encoding='utf-8')
    return path


def main():
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='autoforge-acceptance-') as d:
        folder = pathlib.Path(d)
        state = folder / 'state'
        call('init', state)
        first = call('evolve', state, PROJECT / 'examples/add.json')
        assert first['decision'] == 'PROMOTE'
        assert call('execute', state, -7, 3)['output'] == -4
        record('Python + Julia independently synthesize addition and promote')
        second = call('evolve', state, PROJECT / 'examples/absdiff.json')
        assert second['decision'] == 'PROMOTE' and second['rounds'] == 2
        assert call('execute', state, -7, 3)['output'] == 10
        record('New goal rejects old candidate then repairs in round 2')
        assert call('replay', state)['verified_releases'] == 2
        record('Signed history, artifacts, source build and acceptance replay')
        call('rollback', state)
        assert call('execute', state, -7, 3)['output'] == -4
        record('Persisted rollback restores previous behavior')
        call('kill', state)
        assert 'KILL' in call('execute', state, 1, 2, code=1)['error']
        call('resume', state)
        assert call('execute', state, 1, 2)['output'] == 3
        record('Latched operator kill and explicit resume')
        assert 'Ambiguous' in call('evolve', state, PROJECT / 'examples/impossible.json', code=1)['error']
        assert call('status', state)['active'] == first['release']
        record('Contradictory goal rejected without state change')
        for name, fn in [('minimum', min), ('maximum', max), ('negative-affine', lambda x,y:-2*x+3*y-5),
                         ('clamp', lambda x,y:max(-3,min(6,x)))]:
            task = save_task(folder, name, fn)
            assert call('evolve', state, task)['decision'] == 'PROMOTE'
            assert call('execute', state, -23, 11)['output'] == fn(-23, 11)
            record('Real dual-runtime synthesis: ' + name)
        previous = call('status', state)['active']
        regression = save_task(folder, 'regression', lambda x,y:x+y, task_id='clamp')
        assert call('evolve', state, regression, code=2)['decision'] == 'HOLD'
        assert call('status', state)['active'] == previous
        record('Same task ID retains old regression anchors and vetoes incompatible change')
        hidden = save_task(folder, 'hidden-reject', lambda x,y:x+y)
        t = json.loads(hidden.read_text()); t['holdout_cases'][0]['expected'] += 1
        hidden.write_text(json.dumps(t))
        assert call('evolve', state, hidden, code=2)['decision'] == 'HOLD'
        assert call('status', state)['active'] == previous
        record('Holdout veto prevents public-only success from promotion')
        proc = subprocess.Popen([str(EXE),'evolve',str(state),str(PROJECT/'examples/absdiff.json')],
                                cwd=PROJECT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.2)
        call('kill', state)
        out, err = proc.communicate(timeout=10)
        assert proc.returncode != 0, (out, err)
        assert call('status', state)['active'] == previous
        call('resume', state)
        record('Kill remains available while controller lock is held')
        rel = json.loads((state/'blobs'/previous).read_text())['payload']
        artifact = state/'blobs'/rel['wasm']
        original = artifact.read_bytes(); artifact.write_bytes(original+b'corrupt')
        assert 'integrity' in call('execute', state, 1, 2, code=1)['error']
        artifact.write_bytes(original)
        record('Tampered artifact rejected before execution')
        entry = state/'ledger/00000000.json'
        original = entry.read_bytes(); envelope=json.loads(original)
        envelope['payload']['event']='FORGED'; entry.write_text(json.dumps(envelope))
        assert 'signature' in call('status', state, code=1)['error']
        entry.write_bytes(original)
        record('Forged evidence rejected by Ed25519 verification')
        assert call('replay', state)['verified_releases'] == 6
    report = dict(passed=RESULTS, count=len(RESULTS), elapsed_seconds=round(time.monotonic()-start,2),
                  model_api='not exercised', production_release='not implemented; offline v0 only')
    (PROJECT/'acceptance-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
