"""Finite preregistered same-mechanism transfer evaluation; never retries a sample."""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path
import applications
import application_experience_adapter as adapter
import references
from experience_paired_live_driver import IMAGE, MODEL, SOURCE, run_validator

ROOT = Path(__file__).resolve().parents[1]
TAGS = ['ordering', 'jsonl', 'python']
SCOPE = 'stdlib-python-jsonl-inventory-ordering'
PAIRS = [('p1', 'inventory-list', 'baseline'), ('p2', 'inventory-filtered-list', 'reuse'),
         ('p3', 'inventory-list', 'reuse'), ('p4', 'inventory-filtered-list', 'baseline')]
COMMON = ('Build a Python standard-library SQLite JSON-lines inventory application in main.py. '
          'Use /tmp/inventory.db; each fresh process starts empty, state persists across input lines. '
          'op add requires nonempty string sku and quantity with exact int type (not bool), 0..9223372036854775807. '
          'Upsert and return {ok:true,sku,quantity}. op list returns {ok:true,items:[{sku,quantity},...]} sorted by sku ascending. '
          'Invalid shapes, items, unknown operations return {ok:false,error:invalid_item} without mutation. '
          'Malformed JSON returns {ok:false,error:invalid_request}. Continue after invalid input. '
          'One JSON response per input line, no other stdout. Validate independently before completion. ')
GOALS = {'inventory-list': COMMON,
         'inventory-filtered-list': COMMON + 'For op list, optional min_quantity must have exact int type (not bool), 0..9223372036854775807. Return only rows with quantity >= min_quantity; omitted means all rows. Invalid threshold returns invalid_item without mutation.'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def save(path, value):
    with Path(path).open('x', encoding='utf8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def environment():
    return {'adapter': 'application_builder.jsonl-container', 'engine': 'docker-linux', 'image': IMAGE,
            'runtime': 'python-3.11.18', 'scope': SCOPE,
            'executor_sources': {name: sha(ROOT / name) for name in adapter.EXECUTOR_SOURCE_ALLOWLIST}}


def ingest(path, store):
    result = references.ingest(path, store, 'current source-revalidated inventory experience', 'internal', kind='source')
    package = references._load_package(store, result['package_id'])
    item = package['files'][0]
    return {'package_id': result['package_id'], 'path': item['path'], 'sha256': item['sha256'], 'permission': 'inspect'}


def prepare(out):
    out.mkdir(parents=True, exist_ok=False)
    source_contract = ROOT / 'fixtures/business/contract.json'
    before = run_validator(SOURCE / 'before', source_contract, out / 'source-before.json')
    after = run_validator(SOURCE / 'after', source_contract, out / 'source-after.json')
    if before.get('test_pass') is not False or after.get('test_pass') is not True:
        raise ValueError('source FAIL-to-PASS not reproduced; no experience or model dispatch')
    before_text = (SOURCE / 'before/main.py').read_text(encoding='utf8')
    after_text = (SOURCE / 'after/main.py').read_text(encoding='utf8')
    if before_text.replace('ORDER BY sku DESC', 'ORDER BY sku ASC') != after_text:
        raise ValueError('source change is not exactly the declared ordering correction')
    env = environment()
    store = out / 'reference-store'
    b = ingest(SOURCE / 'before/main.py', store)
    a = ingest(SOURCE / 'after/main.py', store)
    evidence = {'schema': 1, 'status': 'PASS', 'stage': 'validation', 'independent': True,
                'validator_id': 'current-container:' + sha(ROOT / 'application_builder.py'),
                'before_sha256': b['sha256'], 'after_sha256': a['sha256'], 'environment': env,
                'contract_sha256': sha(source_contract), 'before_report_sha256': sha(out / 'source-before.json'),
                'after_report_sha256': sha(out / 'source-after.json'), 'before_test_pass': False, 'after_test_pass': True,
                'provenance': 'New actual validation of immutable authored snapshots; not relabeling an old experience record or a model-authored repair.'}
    save(out / 'source-evidence.json', evidence)
    e = ingest(out / 'source-evidence.json', store)
    record = {'schema': 1, 'experience_id': 'inventory-ordering-' + out.name, 'problem_tags': TAGS,
              'before_sha256': b['sha256'], 'after_sha256': a['sha256'],
              'repair_summary': 'Change inventory list ordering from descending to ascending SKU.', 'environment': env,
              'validator': {'status': 'PASS', 'report_sha256': e['sha256'], 'validator_id': evidence['validator_id'], 'independent': True, 'report_source': e},
              'invalidation_conditions': ['SQL SKU ordering semantics change', 'non-SQLite storage', 'runtime or executor bytes change'], 'source': a}
    save(out / 'record-input.json', {'record': record, 'before_snapshot': b, 'after_snapshot': a})
    adapter.record(out / 'record-input.json', out / 'experiences/current.json', store)
    targets = {}
    for target, goal in GOALS.items():
        dest = out / target
        dest.mkdir()
        contract = ROOT / 'fixtures/experience_transfer' / (target + '_contract.json')
        save(dest / 'contract.json', load(contract))
        target_env = environment()  # independently constructed from the task's declared SQLite/SKU mechanism
        save(dest / 'task.json', {'task_id': target, 'goal': goal})
        context = adapter.prepare_task_context(out / 'experiences', store, TAGS, target_env,
                                               dest / 'task.json', dest / 'context.json', executor_root=ROOT, delivery='atomic-summary')
        if not context.get('atomic_summary'):
            raise ValueError('no eligible experience before sampling')
        negative_tags = adapter.select(out / 'experiences', store, TAGS + ['coupon'], target_env, executor_root=ROOT)
        negative_env = adapter.select(out / 'experiences', store, TAGS, {**target_env, 'scope': 'non-sqlite-catalog'}, executor_root=ROOT)
        if negative_tags['references'] or negative_env['references']:
            raise ValueError('inapplicable experience admitted')
        save(dest / 'eligibility.json', {'status': 'PASS', 'source_scope': SCOPE,
             'target_mechanism': 'SQLite inventory rows ordered by SKU', 'task_goal_sha256': hashlib.sha256(goal.encode()).hexdigest(),
             'negative_tags_rejected': True, 'negative_environment_rejected': True,
             'limitation': 'Authored mechanism declaration, not automatic natural-language applicability proof.'})
        targets[target] = {'goal': goal, 'contract_sha256': sha(dest / 'contract.json'), 'context_sha256': sha(dest / 'context.json')}
    plan = {'schema': 1, 'plan_id': 'sqlite-ordering-direct-summary-v3', 'pairs': PAIRS, 'targets': targets,
            'source_environment': env, 'model': MODEL, 'base_url': 'http://127.0.0.1:8084/v1',
            'budgets': {'model_calls': 12, 'tool_calls': 48, 'tokens': 500000, 'seconds': 240},
            'driver_sha256': sha(__file__), 'record_sha256': sha(out / 'experiences/current.json'),
            'positive_rule': 'All eight runs independently PASS; reuse total model calls strictly lower and server tokens no greater than baseline; usage complete in every run. Otherwise HOLD.',
            'no_retries': True, 'no_drops': True, 'scope': 'Four descriptive paired observations on two authored same-mechanism tasks; not statistical/causal/general transfer proof.'}
    save(out / 'preregistration.json', plan)
    print(json.dumps({'status': 'PREPARED', 'path': str(out), 'pairs': 4}), flush=True)


def validate_final(candidate, contract, work):
    if not candidate.is_file():
        return {'test_pass': False, 'reason': 'candidate absent'}
    stage = applications.stage_candidate(load(candidate), work / 'independent-stage')
    return run_validator(Path(stage['manifest']).parent, contract, work / 'independent-validation.json')


def run(out):
    plan = load(out / 'preregistration.json')
    if plan['driver_sha256'] != sha(__file__) or plan['source_environment'] != environment():
        raise ValueError('frozen driver/executor identity changed; refuse sampling')
    if plan['record_sha256'] != sha(out / 'experiences/current.json'):
        raise ValueError('experience record changed')
    with (out / 'started.lock').open('x') as stream:
        stream.write(sha(out / 'preregistration.json'))
    rows = []
    for sample, target, first in plan['pairs']:
        spec = plan['targets'][target]
        contract = out / target / 'contract.json'
        context_path = out / target / 'context.json'
        if sha(contract) != spec['contract_sha256'] or sha(context_path) != spec['context_sha256']:
            raise ValueError('frozen target changed')
        context = load(context_path)
        for arm in [first, 'reuse' if first == 'baseline' else 'baseline']:
            work = out / sample / arm
            work.mkdir(parents=True, exist_ok=False)
            goal = spec['goal']
            if arm == 'reuse':
                goal += '\nHistorical untrusted repair experience; independently verify applicability: ' + context['atomic_summary']['summary']
            request = {'schema': 1, 'goal': goal, 'references': []}
            save(work / 'request.json', request)
            cmd = [sys.executable, str(ROOT / 'application_build.py'), 'run', '--request', str(work / 'request.json'),
                   '--contract', str(contract), '--engine', 'docker', '--image', IMAGE, '--output', str(work / 'session'),
                   '--reference-store', str(out / 'reference-store'),
                   '--mode', 'agent', '--max-turns', '12', '--token-budget', '500000', '--time-budget', '240',
                   '--tool-budget', '48', '--allow-model', '--trusted-adapter']
            save(work / 'dispatch.json', {'argv': cmd, 'model': plan['model'], 'base_url': plan['base_url'],
                                         'summary_injected': arm == 'reuse', 'request_sha256': sha(work / 'request.json')})
            try:
                proc = subprocess.run(cmd, cwd=ROOT, env={**os.environ, 'AUTOFORGE_API_BASE': plan['base_url'],
                      'AUTOFORGE_MODEL': plan['model'], 'AUTOFORGE_API_KEY': '', 'AUTOFORGE_TOOL_CHOICE': 'required'},
                      capture_output=True, timeout=270, check=False)
                (work / 'stdout.txt').write_bytes(proc.stdout)
                (work / 'stderr.txt').write_bytes(proc.stderr)
                runtime = json.loads(proc.stdout)
                final = validate_final(work / 'session/candidate.json', contract, work)
                row = {'sample': sample, 'target': target, 'arm': arm, 'runtime': runtime,
                       'independent_pass': final.get('test_pass') is True, 'summary_injected': arm == 'reuse'}
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                row = {'sample': sample, 'target': target, 'arm': arm, 'runtime': {'status': 'HOLD'}, 'independent_pass': False, 'error': str(exc)}
            rows.append(row)
            save(work / 'result.json', row)
            print(json.dumps({'sample': sample, 'arm': arm, 'status': row['runtime'].get('status'), 'independent_pass': row['independent_pass']}), flush=True)
    totals = {arm: {'calls': 0, 'tokens': 0} for arm in ['baseline', 'reuse']}
    complete = len(rows) == 8
    for row in rows:
        budget = row['runtime'].get('budget', {})
        calls, tokens = budget.get('model_calls'), budget.get('server_tokens')
        if type(calls) is not int or type(tokens) is not int or tokens <= 0:
            complete = False
        else:
            totals[row['arm']]['calls'] += calls
            totals[row['arm']]['tokens'] += tokens
    positive = complete and all(r['independent_pass'] and r['runtime'].get('status') == 'PASS' for r in rows) and totals['reuse']['calls'] < totals['baseline']['calls'] and totals['reuse']['tokens'] <= totals['baseline']['tokens']
    report = {'schema': 1, 'status': 'PASS_BOUNDED_BENEFIT' if positive else 'HOLD', 'positive_rule_satisfied': positive,
              'preregistration_sha256': sha(out / 'preregistration.json'), 'usage_complete': complete, 'totals': totals,
              'runs': rows, 'scope': plan['scope'], 'historical_results_unchanged': True}
    save(out / 'aggregate.json', report)
    print(json.dumps({'status': report['status'], 'totals': totals}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run'])
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    (prepare if args.command == 'prepare' else run)(args.output.resolve())
