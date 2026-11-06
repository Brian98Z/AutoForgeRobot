"""Finite complex-task transfer experiment; no hidden answers or repeated sampling."""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys, time
from pathlib import Path
import applications
import application_proposer as proposer
from experience_paired_live_driver import IMAGE, MODEL, run_validator
from experience_inventory_evaluation import environment

ROOT = Path(__file__).resolve().parent
SPEC_ROOT = ROOT / 'fixtures/experience_transfer/complex'
PAIRS = [('p1', 'reservation', 'baseline'), ('p2', 'wallet', 'reuse'),
         ('p3', 'reservation', 'reuse'), ('p4', 'wallet', 'baseline')]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def save(path, value):
    with Path(path).open('x', encoding='utf8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def prepare(out, source):
    out.mkdir(parents=True, exist_ok=False)
    source_plan = load(source / 'preregistration.json')
    if source_plan['source_environment'] != environment():
        raise ValueError('source executor is no longer current')
    if load(source / 'source-before.json').get('test_pass') is not False or load(source / 'source-after.json').get('test_pass') is not True:
        raise ValueError('source before/after evidence not established')
    context = load(source / 'inventory-list/context.json')
    summary = context['atomic_summary']['summary']
    specs = load(SPEC_ROOT / 'specs.json')
    targets = {}
    for task in specs['tasks']:
        target = out / task['id']; target.mkdir()
        save(target / 'contract.json', load(SPEC_ROOT / task['contract']))
        targets[task['id']] = {'goal': task['goal'], 'contract_sha256': sha(target / 'contract.json')}
    if set(targets) != {'reservation', 'wallet'}:
        raise ValueError('fixed target inventory mismatch')
    plan = {'schema': 1, 'kind': 'complex-partial-mechanism-transfer', 'pairs': PAIRS, 'targets': targets,
            'summary': summary, 'source_environment': environment(), 'model': MODEL,
            'base_url': 'http://127.0.0.1:8084/v1', 'image': IMAGE,
            'budget': {'proposals': 3, 'seconds': 240, 'token_upper_bound': 100000, 'output_tokens_per_proposal': 8192},
            'driver_sha256': sha(__file__), 'proposer_sha256': sha(ROOT / 'application_proposer.py'),
            'source_evidence': {str(p): sha(p) for p in [source / 'source-evidence.json', source / 'experiences/current.json', source / 'inventory-list/context.json']},
            'transfer_mapping': {'source': 'SQLite SKU lexical ascending output', 'targets': 'SQLite item/account identifier lexical ascending output',
                 'authority': 'coordinator inference, not exact task/environment equivalence',
                 'excluded': ['transaction correctness', 'idempotency', 'reservation/transfer state transitions']},
            'positive_rule': 'All eight candidates independently PASS; complete usage; every reuse request must contain the frozen guidance in its user message; reuse total proposal calls strictly lower and tokens no greater. Otherwise HOLD.',
            'stopping': 'Exactly four pairs; no retries, sample removal, budget changes or extra samples after outcomes.',
            'scope': 'Bounded descriptive partial-mechanism transfer; no statistical significance, general benefit or causal proof.'}
    save(out / 'preregistration.json', plan)
    print(json.dumps({'status': 'PREPARED', 'path': str(out)}))


def worker(out, sample, arm):
    plan = load(out / 'preregistration.json')
    target = next(p[1] for p in plan['pairs'] if p[0] == sample)
    work = out / sample / arm
    goal = plan['targets'][target]['goal']
    guidance = []
    if arm == 'reuse':
        guidance = [{'kind': 'historical-untrusted-summary', 'summary': plan['summary'],
                     'mapping': plan['transfer_mapping']}]
    request = {'schema': 1, 'goal': goal, 'references': []}
    payload = {'schema': 1, 'requirement': request, 'references': guidance,
               'previous_validation': {}, 'previous_candidate': None}
    os.environ.update(AUTOFORGE_API_BASE=plan['base_url'], AUTOFORGE_MODEL=plan['model'],
                      AUTOFORGE_API_KEY='', AUTOFORGE_ENABLE_THINKING='false')
    real_completion = proposer.completion
    calls = []; guidance_exposed_calls = 0; reserved = 0; started = time.monotonic()
    def measured(body):
        nonlocal reserved, guidance_exposed_calls
        bound = len(json.dumps(body, ensure_ascii=False).encode('utf8')) + 8192
        if reserved + bound > 100000 or time.monotonic() - started >= 240:
            raise ValueError('fixed token/time budget exhausted before model dispatch')
        reserved += bound
        body = {**body, 'max_tokens': 8192}
        exposed = False
        if arm == 'reuse':
            for message in body.get('messages', []):
                if not isinstance(message, dict) or message.get('role') != 'user' or not isinstance(message.get('content'), str):
                    continue
                try:
                    sent = json.loads(message['content'])
                except ValueError:
                    continue
                if isinstance(sent, dict) and sent.get('references') == guidance:
                    exposed = True
                    break
        calls.append({'served_model': None, 'usage': None, 'guidance_exposed': exposed})
        guidance_exposed_calls += int(exposed)
        save(work / ('model-request-%d.json' % len(calls)), body)
        envelope = real_completion(body)
        calls[-1] = {'served_model': envelope.get('model'), 'usage': envelope.get('usage'), 'guidance_exposed': exposed}
        save(work / ('model-response-%d.json' % len(calls)), envelope)
        return envelope
    proposer.completion = measured
    final = {'test_pass': False}; status = 'HOLD'; reason = None
    try:
        for turn in range(1, 4):
            candidate = proposer._request(payload)
            save(work / ('candidate-%d.json' % turn), candidate)
            stage = applications.stage_candidate(candidate, work / ('stage-%d' % turn))
            final = run_validator(Path(stage['manifest']).parent, out / target / 'contract.json', work / ('validation-%d.json' % turn))
            if final.get('build_pass') is True and final.get('test_pass') is True:
                status = 'PASS'; break
            payload['previous_candidate'] = candidate
            payload['previous_validation'] = final
        if status == 'PASS':
            final = run_validator(Path(stage['manifest']).parent, out / target / 'contract.json', work / 'independent-final.json')
            if final.get('test_pass') is not True: status = 'HOLD'
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        reason = str(exc)
    finally:
        proposer.completion = real_completion
    usages = [r.get('usage') for r in calls]
    complete = bool(calls) and all(isinstance(u, dict) and type(u.get('total_tokens')) is int and u['total_tokens'] > 0 for u in usages)
    guidance_exposed = arm == 'reuse' and bool(calls) and guidance_exposed_calls == len(calls)
    row = {'sample': sample, 'target': target, 'arm': arm, 'status': status,
           'independent_pass': final.get('test_pass') is True and status == 'PASS',
           'calls': len(calls), 'tokens': sum(u['total_tokens'] for u in usages) if complete else None,
           'usage_complete': complete, 'guidance_exposed': guidance_exposed,
           'guidance_exposed_calls': guidance_exposed_calls, 'reason': reason,
           'elapsed_seconds': time.monotonic() - started, 'reserved_tokens': reserved}
    save(work / 'result.json', row)


def arm_totals(rows):
    def measured_sum(arm_rows, field):
        values = [row.get(field) for row in arm_rows]
        return sum(values) if values and all(type(value) is int and value >= 0 for value in values) else None
    totals = {}
    for arm in ('baseline', 'reuse'):
        selected = [row for row in rows if row.get('arm') == arm]
        totals[arm] = {'calls': measured_sum(selected, 'calls'),
                       'tokens': measured_sum(selected, 'tokens'),
                       'passed': sum(row.get('independent_pass') is True for row in selected)}
    return totals


def positive_rule_satisfied(rows):
    expected = {(sample, target, arm) for sample, target, _ in PAIRS for arm in ('baseline', 'reuse')}
    if len(rows) != len(expected) or any(not isinstance(row, dict) for row in rows):
        return False
    if {(row.get('sample'), row.get('target'), row.get('arm')) for row in rows} != expected:
        return False
    for row in rows:
        if (row.get('independent_pass') is not True or row.get('usage_complete') is not True
                or type(row.get('calls')) is not int or not 1 <= row['calls'] <= 3
                or type(row.get('tokens')) is not int or row['tokens'] <= 0):
            return False
        if row['arm'] == 'reuse' and (row.get('guidance_exposed') is not True
                or type(row.get('guidance_exposed_calls')) is not int
                or row['guidance_exposed_calls'] != row['calls']):
            return False
    totals = arm_totals(rows)
    return (totals['reuse']['calls'] < totals['baseline']['calls']
            and totals['reuse']['tokens'] <= totals['baseline']['tokens'])


def run(out):
    plan = load(out / 'preregistration.json')
    if plan['driver_sha256'] != sha(__file__) or plan['proposer_sha256'] != sha(ROOT / 'application_proposer.py') or plan['source_environment'] != environment():
        raise ValueError('frozen implementation changed')
    for path, digest in plan['source_evidence'].items():
        if sha(path) != digest: raise ValueError('source evidence changed')
    with (out / 'started.lock').open('x') as stream: stream.write(sha(out / 'preregistration.json'))
    rows = []
    for sample, target, first in plan['pairs']:
        for arm in [first, 'reuse' if first == 'baseline' else 'baseline']:
            if sha(out / target / 'contract.json') != plan['targets'][target]['contract_sha256']:
                raise ValueError('target contract changed')
            work = out / sample / arm; work.mkdir(parents=True)
            command = [sys.executable, '-B', __file__, 'worker', str(out), '--sample', sample, '--arm', arm]
            save(work / 'dispatch.json', {'argv': command, 'plan_sha256': sha(out / 'preregistration.json')})
            try:
                p = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=240, check=False)
                (work / 'stdout.txt').write_bytes(p.stdout); (work / 'stderr.txt').write_bytes(p.stderr)
                row = load(work / 'result.json')
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                row = {'sample': sample, 'target': target, 'arm': arm, 'status': 'HOLD', 'independent_pass': False,
                       'usage_complete': False, 'calls': None, 'tokens': None, 'reason': str(exc)}
                if not (work / 'result.json').exists(): save(work / 'result.json', row)
            rows.append(row)
            print(json.dumps(row), flush=True)
    totals = arm_totals(rows)
    positive = positive_rule_satisfied(rows)
    save(out / 'aggregate.json', {'schema': 1, 'status': 'PASS_BOUNDED_BENEFIT' if positive else 'HOLD',
         'positive_rule_satisfied': positive, 'totals': totals, 'runs': rows,
         'preregistration_sha256': sha(out / 'preregistration.json'), 'scope': plan['scope']})
    print(json.dumps({'positive_rule_satisfied': positive, 'totals': totals}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'run', 'worker']); p.add_argument('output', type=Path)
    p.add_argument('--source', type=Path); p.add_argument('--sample'); p.add_argument('--arm', choices=['baseline', 'reuse'])
    a = p.parse_args(); out = a.output.resolve()
    if a.command == 'prepare':
        if not a.source: p.error('--source required')
        prepare(out, a.source.resolve())
    elif a.command == 'worker': worker(out, a.sample, a.arm)
    else: run(out)
