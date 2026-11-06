#!/usr/bin/env python3
"""Exercise experience adapter against real immutable package and run artifacts."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import application_experience_adapter as adapter
import application_learning as learning
import applications
import references
import experience_complex_evaluation as complex_evaluation


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def ingest_json(root: Path, store: Path, filename: str, value: dict) -> dict:
    source = root / filename
    source.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    result = references.ingest(source, store, "adapter check artifact", "internal", kind="source")
    package = references._load_package(store, result["package_id"])
    entry = package["files"][0]
    return {"package_id": result["package_id"], "path": entry["path"],
            "sha256": entry["sha256"], "permission": "inspect"}


def run_artifact(root: Path, run_id: str, task_id: str, status: str, validation_status: str) -> Path:
    run = root / run_id
    run.mkdir()
    report = {"run_id": run_id, "task_id": task_id, "status": status}
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    events = [
        {"seq": 1, "type": "tool_started", "call_id": "validate-1", "name": "validate"},
        {"seq": 2, "type": "tool_result", "call_id": "validate-1", "result": {"status": validation_status}},
    ]
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return run


def main() -> None:
    rows = [{'sample': sample, 'target': target, 'arm': arm, 'independent_pass': True, 'usage_complete': True,
             'calls': 2 if arm == 'baseline' else 1, 'tokens': 50 if arm == 'baseline' else 25,
             'guidance_exposed': arm == 'reuse', 'guidance_exposed_calls': 1 if arm == 'reuse' else 0}
            for sample, target, _ in complex_evaluation.PAIRS for arm in ('baseline', 'reuse')]
    check(complex_evaluation.positive_rule_satisfied(rows), 'fully exposed bounded improvement refused')
    for field, value in (('guidance_exposed', False), ('guidance_exposed_calls', 0),
                         ('usage_complete', False), ('independent_pass', False), ('tokens', None),
                         ('tokens', 0), ('calls', True), ('calls', 0), ('calls', 4),
                         ('target', 'unregistered-task')):
        invalid = [dict(row) for row in rows]
        invalid[1][field] = value
        check(not complex_evaluation.positive_rule_satisfied(invalid),
              f'invalid paired benefit evidence admitted: {field}={value}')
    duplicate = [dict(row) for row in rows]
    duplicate[2:4] = [dict(row) for row in rows[:2]]
    check(not complex_evaluation.positive_rule_satisfied(duplicate), 'duplicate pair replaced a missing pair')
    tied = [{**row, 'calls': 1, 'tokens': 25} for row in rows]
    check(not complex_evaluation.positive_rule_satisfied(tied), 'equal calls were counted as benefit')
    expensive = [{**row, 'tokens': 51 if row['arm'] == 'reuse' else 50} for row in rows]
    check(not complex_evaluation.positive_rule_satisfied(expensive), 'more reuse tokens counted as benefit')
    missing = [dict(row) for row in rows]
    missing[1]['tokens'] = None
    totals = complex_evaluation.arm_totals(missing)
    check(totals['reuse']['tokens'] is None and totals['baseline']['tokens'] == 200,
          'missing usage collapsed into zero or erased known baseline usage')
    with tempfile.TemporaryDirectory(prefix="experience-adapter-check-") as tmp:
        root = Path(tmp)
        store = root / "reference-store"
        env = {"runtime": "python-3.12", "adapter": "isolated-container"}
        executor_root = root / "trusted-executor"
        executor_root.mkdir()
        for filename in adapter.EXECUTOR_SOURCE_ALLOWLIST:
            (executor_root / filename).write_text('trusted-fixture-'+filename, encoding='utf-8')
        env = {"runtime": "python-3.12", "adapter": "isolated-container",
               "executor_sources": {name: hashlib.sha256((executor_root / name).read_bytes()).hexdigest()
                                    for name in adapter.EXECUTOR_SOURCE_ALLOWLIST}}
        before_sha, after_sha = "a" * 64, "b" * 64
        before = ingest_json(root, store, "before.json", {"snapshot": "before"})
        after = ingest_json(root, store, "after.json", {"snapshot": "after"})
        before["sha256"], after["sha256"] = before_sha, after_sha
        # The fixture's declared snapshot identities must be the exact hashes recorded.
        before_doc = root / "before.json"; after_doc = root / "after.json"
        before["sha256"] = hashlib.sha256(before_doc.read_bytes()).hexdigest()
        after["sha256"] = hashlib.sha256(after_doc.read_bytes()).hexdigest()
        before_sha, after_sha = before["sha256"], after["sha256"]
        validator_report = {"status": "PASS", "stage": "validation", "independent": True,
                            "validator_id": "independent-acceptance-v1", "before_sha256": before_sha,
                            "after_sha256": after_sha, "environment": env}
        report_ref = ingest_json(root, store, "validation-report.json", validator_report)
        source_ref = after
        raw = {"schema": 1, "experience_id": "actual-repair-1", "problem_tags": ["validation", "input"],
               "before_sha256": before_sha, "after_sha256": after_sha,
               "repair_summary": "Validate quantity before updating persistent state.", "environment": env,
               "validator": {"status": "PASS", "report_sha256": report_ref["sha256"],
                             "validator_id": validator_report["validator_id"], "independent": True,
                             "report_source": report_ref},
               "invalidation_conditions": ["runtime identity changes", "validator contract changes"],
               "source": source_ref}
        # A proven edit may transfer to a different file, but never overwrite callers or guess matches.
        source_before = root / 'repair-before.py'; source_after = root / 'repair-after.py'
        source_before.write_bytes(b'value = 1\n')
        source_after.write_bytes(b'value = 2\n')
        def source_binding(path):
            result = references.ingest(path, store, 'exact repair source', 'internal', kind='source')
            entry = references._load_package(store, result['package_id'])['files'][0]
            return {'package_id': result['package_id'], 'path': entry['path'], 'sha256': entry['sha256'], 'permission': 'inspect'}
        repair_before, repair_after = source_binding(source_before), source_binding(source_after)
        repair_report = {**validator_report, 'stage': 'validation', 'independent': True,
                         'before_sha256': repair_before['sha256'], 'after_sha256': repair_after['sha256'],
                         'before_test_pass': False, 'after_test_pass': True}
        repair_evidence = ingest_json(root, store, 'repair-evidence.json', repair_report)
        repair_record = {**raw, 'experience_id': 'bounded-repair', 'before_sha256': repair_before['sha256'],
                         'after_sha256': repair_after['sha256'], 'source': repair_after,
                         'validator': {**raw['validator'], 'report_sha256': repair_evidence['sha256'], 'report_source': repair_evidence}}
        repair_input = root / 'repair-input.json'
        repair_input.write_text(json.dumps({'record': repair_record, 'before_snapshot': repair_before,
                                           'after_snapshot': repair_after}), encoding='utf-8')
        candidate = {'schema': 1, 'goal': 'Maintain a separate module', 'references': [],
                     'files': [{'path': 'other.py', 'content': '# keep\nvalue = 1\n'}, {'path': 'user.txt', 'content': 'sentinel'}]}
        untouched = json.loads(json.dumps(candidate))
        digest = hashlib.sha256(candidate['files'][0]['content'].encode()).hexdigest()
        repaired = adapter.reuse_repair(repair_input, store, candidate, 'other.py', digest, ['validation'], env, executor_root)
        check(candidate == untouched and repaired['candidate']['files'][0]['content'] == '# keep\nvalue = 2\n'
              and repaired['candidate']['files'][1] == candidate['files'][1]
              and repaired['independent_acceptance_required'] is True and repaired['execution_authorized'] is False,
              'repair transfer changed source input, unrelated files, or granted acceptance')
        nested_text = 'def run():\r\n    value = 1\r\n'
        nested = {**candidate, 'files': [{'path': 'other.py', 'content': nested_text}]}
        nested_result = adapter.reuse_repair(repair_input, store, nested, 'other.py',
                                             hashlib.sha256(nested_text.encode()).hexdigest(), ['validation'], env, executor_root)
        check(nested_result['candidate']['files'][0]['content'] == 'def run():\r\n    value = 2\r\n',
              'single-statement transfer lost target indentation or CRLF')
        for text, expected, target_env, tags in (
                ('# keep\nvalue = 1\n', '0' * 64, env, ['validation']),
                ('value = 1\nvalue = 1\n', None, env, ['validation']),
                ('value = 3\n', None, env, ['validation']),
                ('value = 1\n    value = 1\n', None, env, ['validation']),
                ('# value = 1\n', None, env, ['validation']),
                ('value = 1\n', None, {**env, 'runtime': 'other'}, ['validation']),
                ('value = 1\n', None, env, ['security'])):
            rejected_candidate = {**candidate, 'files': [{'path': 'other.py', 'content': text}]}
            before_rejection = json.loads(json.dumps(rejected_candidate))
            try:
                adapter.reuse_repair(repair_input, store, rejected_candidate, 'other.py',
                                     expected or hashlib.sha256(text.encode()).hexdigest(), tags, target_env, executor_root)
            except ValueError:
                pass
            else:
                raise AssertionError('stale, ambiguous, missing or inapplicable repair was applied')
            check(rejected_candidate == before_rejection, 'refused repair mutated candidate')
        input_path = root / "record-input.json"
        input_path.write_text(json.dumps({"record": raw, "before_snapshot": before,
                                         "after_snapshot": after}), encoding="utf-8")
        exp_path = root / "experiences" / "actual-repair-1.json"
        result = adapter.record(input_path, exp_path, store)
        check(exp_path.is_file() and result["experience_id"] == "actual-repair-1", "record did not persist")
        selected = adapter.select(root / "experiences", store, ["validation"], env, 2, executor_root)
        check(len(selected["references"]) == 1, "matching environment experience not selected")
        narrow = adapter.select(root / "experiences", store, ["validation", "security"], env, 2, executor_root)
        check(not narrow["references"] and "every requested problem tag" in narrow["omitted"][0]["reason"], "partial tag overlap was treated as a relevant transfer")
        missing_one = json.loads(json.dumps(env)); missing_one["executor_sources"].pop("application_builder.py")
        incomplete = adapter.select(root / "experiences", store, ["validation"], missing_one, executor_root=executor_root)
        check(not incomplete["references"] and "incomplete allowlisted identity" in incomplete["omitted"][0]["reason"], "missing one executor digest was accepted")
        missing_identity = adapter.select(root / "experiences", store, ["validation"], {k:v for k,v in env.items() if k!="executor_sources"}, executor_root=executor_root)
        check(not missing_identity["references"] and "missing current executor_sources" in missing_identity["omitted"][0]["reason"], "missing executor identity was not explicitly rejected")
        check(adapter.select(root / "experiences", store, ["validation"], {"runtime": "other"})["references"] == [], "environment mismatch was selected")
        changed_env = json.loads(json.dumps(env)); changed_env["executor_sources"]["application_learning.py"] = "0" * 64
        changed = adapter.select(root / "experiences", store, ["validation"], changed_env, executor_root=executor_root)
        check(not changed["references"] and "identity mismatch" in changed["omitted"][0]["reason"], "mismatched current executor identity was accepted")
        package = adapter.export_reference(root / "experiences", store, ["validation"], env, store, executor_root=executor_root)
        resolved = applications._resolve_references(store, [package["package_id"]])
        check(len(resolved) == 1 and resolved[0]["package_id"] == package["package_id"],
              "export is not a usable immutable standard reference package")
        task_file = root / "task.json"; task_file.write_text(json.dumps({"task_id": "new-task"}), encoding="utf-8")
        context_path = root / "task-context.json"
        context = adapter.prepare_task_context(root / "experiences", store, ["validation"], env, task_file, context_path, executor_root=executor_root)
        check(context["task_id"] == "new-task" and context["retrieved"]["references"][0]["experience_id"] == "actual-repair-1", "pre-task context not bound to validated retrieval")
        atomic_context_path = root / "atomic-task-context.json"
        atomic_context = adapter.prepare_task_context(root / "experiences", store, ["validation"], env, task_file,
                                                      atomic_context_path, executor_root=executor_root, delivery="atomic-summary")
        check("atomic_summary" not in context and atomic_context["atomic_summary"]["summary"] == raw["repair_summary"],
              "summary injection is not opt-in or differs from validated summary")
        context_unsigned = {key: value for key, value in atomic_context["atomic_summary"].items() if key != "sha256"}
        check(atomic_context["atomic_summary"]["sha256"] == references.digest(references.canonical(context_unsigned)),
              "atomic context summary digest did not validate")
        atomic = adapter.atomic_summary_context(selected)
        check(atomic["kind"] == "public-atomic-experience-summary" and atomic["summary"] == raw["repair_summary"],
              "atomic summary did not preserve the validated repair fact")
        check(atomic["environment_sha256"] == references.digest(references.canonical(env)) and
              "source" not in atomic and "validator" not in atomic and "after_sha256" not in atomic,
              "atomic summary leaked source, validator, or answer-like snapshot data")
        check(atomic['invalidation_conditions'] == raw['invalidation_conditions'] and
              atomic_context['atomic_summary']['invalidation_conditions'] == raw['invalidation_conditions'],
              'summary delivery lost the recorded applicability invalidation conditions')
        atomic['invalidation_conditions'].append('caller mutation')
        check(selected['references'][0]['invalidation_conditions'] == raw['invalidation_conditions'],
              'summary mutation changed validated source conditions')
        for conditions in (None, [], [''], [1]):
            invalid_selection = json.loads(json.dumps(selected))
            invalid_selection['references'][0]['invalidation_conditions'] = conditions
            invalid_selection['sha256'] = references.digest(references.canonical(
                {key: value for key, value in invalid_selection.items() if key != 'sha256'}))
            try:
                adapter.atomic_summary_context(invalid_selection)
            except learning.LearningRefused:
                pass
            else:
                raise AssertionError('atomic summary accepted absent or invalid applicability conditions')
        check(len(atomic["summary"]) <= 280, "atomic summary exceeded bound")
        try:
            adapter.atomic_summary_context({**selected, "references": []})
        except learning.LearningRefused:
            pass
        else:
            raise AssertionError("tampered selection accepted for atomic summary")
        stale_context = adapter.prepare_task_context(root / "experiences", store, ["validation"], {"runtime": "changed", "executor_sources": env["executor_sources"]}, task_file, root / "stale-context.json", executor_root=executor_root)
        check(stale_context["retrieved"]["references"] == [], "stale experience entered context for a different environment")
        exported = json.loads(resolved[0]["files"][0]["content"])
        # Empty retrieval is valid for a new environment; it must not carry the prior environment's record.
        check(exported["references"][0]["experience_id"] == "actual-repair-1" and
              "content" not in exported["references"][0]["source"], "export disclosed source contents")
        # Mutating either the immutable experience record or its evidence source must fail closed.
        tampered = json.loads(exp_path.read_text(encoding="utf-8")); tampered["repair_summary"] = "forged"
        exp_path.write_text(json.dumps(tampered), encoding="utf-8")
        try:
            adapter.select(root / "experiences", store, ["validation"], env, executor_root=executor_root)
        except (ValueError, learning.LearningRefused):
            pass
        else:
            raise AssertionError("tampered experience accepted")
        exp_path.write_text(json.dumps(result), encoding="utf-8")
        # Comparing actual reports and journals gives measured counts but no causal benefit claim.
        run_a = run_artifact(root, "run-a", "same-task", "FAIL", "FAIL")
        run_b = run_artifact(root, "run-b", "same-task", "PASS", "PASS")
        compared = adapter.compare(run_a, run_b)
        check(compared["sample_match"] is True and compared["benefit"] == "not_proven" and
              compared["validation_fail_count_delta"] == -1, "comparison did not derive journal outcome counts")
        use_context = root / "use-context.json"
        use_context_payload = {"schema": 1, "kind": "pre-task-experience-context", "task_id": "same-task", "environment": env, "retrieved": adapter.select(root / "experiences", store, ["validation"], env, executor_root=executor_root), "limitations": ["test"]}
        use_context_payload["sha256"] = references.digest(references.canonical(use_context_payload))
        use_context.write_text(json.dumps(use_context_payload), encoding="utf-8")
        unconsumed = {k:v for k,v in raw.items() if k!="sha256"}
        unconsumed["experience_id"] = "not-in-task-context"
        learning.save_experience(root / "experiences" / "not-in-task-context.json", unconsumed, store)
        use = learning.record_experience_use(root / "experiences", reference_store=store, problem_tags=["validation"], environment=env, task_id="same-task", baseline_run=run_a, reused_run=run_b, context_path=use_context, executor_root=executor_root)
        check(use["benefit"] == "observed_paired_improvement_noncausal" and use["experience_ids"] == ["actual-repair-1"], "paired improvement was not bound to retrieved experience or falsely causal")
        forged = json.loads(use_context.read_text()); forged["task_id"] = "wrong-task"; use_context.write_text(json.dumps(forged))
        try: learning.record_experience_use(root / "experiences", reference_store=store, problem_tags=["validation"], environment=env, task_id="same-task", baseline_run=run_a, reused_run=run_b, context_path=use_context)
        except learning.LearningRefused: pass
        else: raise AssertionError("unbound task-context reuse was recorded")
        run_c = run_artifact(root, "run-c", "different-task", "PASS", "PASS")
        mismatch = adapter.compare(run_a, run_c)
        check(mismatch["sample_match"] is False and mismatch["benefit"] == "not_proven" and
              "validation_count_delta" not in mismatch, "mismatched task was treated as paired benefit")


if __name__ == "__main__":
    main()
