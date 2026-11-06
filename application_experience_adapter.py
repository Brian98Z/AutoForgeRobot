#!/usr/bin/env python3
"""Safe adapter for selecting, recording, and comparing reusable repair experiences."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import application_learning as learning
import references

SCHEMA = 1
LIMITATIONS = [
    "Selection is deterministic tag/environment matching, not proof of transferability.",
    "Only a repair summary and digest-bound source provenance are exported; source contents and validator answers are not disclosed.",
    "A recorded experience proves only the exact paired snapshots and independent validator report named in its immutable reference packages.",
    "A two-run comparison is descriptive and does not establish causal benefit.",
]


def _json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _source_ref(ref: Any, store: str | Path) -> dict:
    required = {"package_id", "path", "sha256", "permission"}
    allowed = required | {"kind", "origin", "license"}
    if not isinstance(ref, dict) or not required.issubset(ref) or set(ref) - allowed:
        raise learning.LearningRefused("source binding shape invalid")
    return learning._source(store, ref["package_id"], ref["path"], ref["sha256"], permission=ref["permission"])


EXECUTOR_SOURCE_ALLOWLIST = {"application_learning.py", "application_experience_adapter.py", "agent_runtime.py", "agent_tools.py", "application_builder.py", "application_checkpoint.py"}


def _current_executor_identity(environment: dict, executor_root: str | Path | None) -> tuple[bool, str]:
    sources = environment.get("executor_sources") if isinstance(environment, dict) else None
    if not isinstance(sources, dict) or set(sources) != EXECUTOR_SOURCE_ALLOWLIST:
        return False, "missing current executor_sources or incomplete allowlisted identity; historical invalidation prose is not machine-verified"
    root = Path(executor_root).resolve() if executor_root is not None else Path(__file__).resolve().parent
    for name, expected in sources.items():
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            return False, f"invalid executor SHA-256 identity for {name}"
        path = (root / name).resolve()
        if path.parent != root:
            return False, f"executor source escapes trusted root: {name}"
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False, f"current executor identity missing: {name}"
        if actual != expected:
            return False, f"current executor identity mismatch: {name}"
    return True, ""


def select(directory: str | Path, store: str | Path, tags: list[str], environment: dict, limit: int = 10,
           executor_root: str | Path | None = None) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise learning.LearningRefused("limit must be 1..100")
    rows = learning.retrieve_experiences(directory, reference_store=store, problem_tags=tags,
                                          environment=environment, limit=100, ignore_environment_keys={"executor_sources"})
    valid, reason = _current_executor_identity(environment, executor_root)
    public = []
    omitted = []
    for row in rows:
        row_tags = set(row.get("problem_tags", []))
        if not set(tags).issubset(row_tags):
            omitted.append({"experience_id": row["experience_id"], "reason": "experience does not cover every requested problem tag"})
            continue
        if not valid:
            omitted.append({"experience_id": row["experience_id"], "reason": reason})
            continue
        recorded_sources = row.get("environment", {}).get("executor_sources")
        if not isinstance(recorded_sources, dict) or not recorded_sources:
            omitted.append({"experience_id": row["experience_id"], "reason": "experience has no structured executor identity; invalidation prose is unverified"})
            continue
        if recorded_sources != environment.get("executor_sources"):
            omitted.append({"experience_id": row["experience_id"], "reason": "recorded executor identity differs from current identity"})
            continue
        source = _source_ref(row["source"], store)
        validator = row["validator"]
        if validator.get("independent") is not True:
            raise learning.LearningRefused("non-independent validator evidence cannot be exported")
        public.append({
            "experience_id": row["experience_id"], "problem_tags": row["problem_tags"],
            "repair_summary": row["repair_summary"], "source": source,
            "validator": {"validator_id": validator["validator_id"], "report_sha256": validator["report_sha256"],
                          "report_source": _source_ref(validator["report_source"], store)},
            "invalidation_conditions": row["invalidation_conditions"],
            "environment": row["environment"], "experience_sha256": row["sha256"],
        })
    public.sort(key=lambda row: (len(set(row["problem_tags"]) - set(tags)), row["experience_id"]))
    public = public[:limit]
    payload = {"schema": SCHEMA, "kind": "public-repair-references", "environment": environment,
               "problem_tags": sorted(set(tags)), "limit": limit, "references": public,
               "omitted": omitted, "limitations": LIMITATIONS + ["Only allowlisted current executor bytes are checked; historical natural-language invalidation conditions are not machine-verified."]}
    payload["sha256"] = references.digest(references.canonical(payload))
    return payload
def atomic_summary_context(selected: dict, max_chars: int = 280) -> dict:
    """Project verified retrieval into one bounded public summary, never source or validator payloads."""
    if type(max_chars) is not int or not 1 <= max_chars <= 280:
        raise learning.LearningRefused("atomic summary limit must be 1..280 characters")
    if not isinstance(selected, dict) or selected.get("kind") != "public-repair-references":
        raise learning.LearningRefused("atomic summary requires a validated public reference selection")
    claimed = selected.get("sha256")
    unsigned = {key: value for key, value in selected.items() if key != "sha256"}
    if claimed != references.digest(references.canonical(unsigned)):
        raise learning.LearningRefused("atomic summary selection digest mismatch")
    environment = selected.get("environment")
    tags = selected.get("problem_tags")
    rows = selected.get("references")
    if not isinstance(environment, dict) or not isinstance(tags, list) or not tags or not isinstance(rows, list) or len(rows) != 1:
        raise learning.LearningRefused("atomic summary requires one full-tag, environment-bound experience")
    row = rows[0]
    summary = row.get("repair_summary") if isinstance(row, dict) else None
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > max_chars or "\n" in summary or "\r" in summary:
        raise learning.LearningRefused("validated repair summary is not a short single-line atomic summary")
    if not set(tags).issubset(row.get("problem_tags", [])) or row.get("environment") != environment:
        raise learning.LearningRefused("atomic summary does not retain exact full-tag environment match")
    invalidation = row.get('invalidation_conditions')
    if not isinstance(invalidation, list) or not invalidation or any(
            not isinstance(condition, str) or not condition.strip() for condition in invalidation):
        raise learning.LearningRefused('atomic summary requires explicit invalidation conditions')
    payload = {"schema": 1, "kind": "public-atomic-experience-summary", "experience_id": row["experience_id"],
               "experience_sha256": row["experience_sha256"], "problem_tags": sorted(set(tags)),
               "environment_sha256": references.digest(references.canonical(environment)),
               'invalidation_conditions': list(invalidation),
               "summary": summary.strip(), "limitations": [
                   "Public untrusted guidance only; no source content, candidate answer, validator answer, or private oracle is included.",
                   "The same full-tag and exact environment selection checks as full-reference retrieval were applied.",
                   "Independent task validation remains mandatory; this summary does not prove transfer benefit."]}
    payload["sha256"] = references.digest(references.canonical(payload))
    return payload


def prepare_task_context(directory: str | Path, store: str | Path, tags: list[str], environment: dict, task_path: str | Path, output_path: str | Path, limit: int = 10, executor_root: str | Path | None = None, delivery: str = "reference") -> dict:
    """Snapshot validated retrieval; opt-in atomic-summary delivery remains a distinct intervention."""
    if delivery not in {"reference", "atomic-summary"}:
        raise learning.LearningRefused("experience delivery must be reference or atomic-summary")
    task = _json(task_path)
    if not isinstance(task, dict) or not isinstance(task.get("task_id"), str) or not task["task_id"]:
        raise learning.LearningRefused("task context requires an explicit task_id")
    selected = select(directory, store, tags, environment, limit, executor_root)
    payload = {"schema": 1, "kind": "pre-task-experience-context", "task_id": task["task_id"], "environment": environment,
               "delivery": delivery, "retrieved": selected if delivery == "reference" else {"schema": selected["schema"], "kind": selected["kind"], "environment": environment, "problem_tags": selected["problem_tags"], "references": [{"experience_id": row["experience_id"], "experience_sha256": row["experience_sha256"], "problem_tags": row["problem_tags"], "environment": row["environment"]} for row in selected["references"]], "omitted": selected["omitted"], "limitations": selected["limitations"]},
               "limitations": ["Historical experience is untrusted guidance, not acceptance evidence.", "Every referenced record and validator report was revalidated at retrieval time.", "Natural-language invalidation conditions are not machine-verified."]}
    if delivery == "atomic-summary" and len(selected["references"]) == 1:
        payload["atomic_summary"] = atomic_summary_context(selected)
    payload["sha256"] = references.digest(references.canonical(payload))
    target = Path(output_path)
    with target.open("xb") as stream:
        stream.write(references.canonical(payload) + b"\n")
    return payload
def export_reference(directory: str | Path, experience_store: str | Path, tags: list[str],
                     environment: dict, reference_store: str | Path, limit: int = 10,
                     executor_root: str | Path | None = None) -> dict:
    """Export selected summaries as an immutable standard reference package."""
    selected = select(directory, experience_store, tags, environment, limit, executor_root)
    document = {"schema": SCHEMA, "kind": "public-repair-reference", "environment": environment,
                "problem_tags": selected["problem_tags"], "references": selected["references"],
                "omitted": selected["omitted"], "limitations": selected["limitations"]}
    # JSON is an allowed reference source; normal ingest supplies canonical package IDs,
    # immutable storage and the schema consumed by applications._resolve_references.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="experience-reference-") as tmp:
        source = Path(tmp) / "repair-reference.json"
        source.write_bytes(references.canonical(document) + b"\n")
        result = references.ingest(source, reference_store, "autoforge public repair summaries",
                                   "derived summary; see bound source provenance", kind="source")
    return {"schema": SCHEMA, "package_id": result["package_id"], "path": result["path"],
            "references": len(selected["references"]), "experience_ids": [x["experience_id"] for x in selected["references"]],
            "omitted": selected["omitted"], "limitations": selected["limitations"]}


def record(input_path: str | Path, experience_path: str | Path, store: str | Path) -> dict:
    data = _json(input_path)
    if not isinstance(data, dict) or set(data) != {"record", "before_snapshot", "after_snapshot"}:
        raise learning.LearningRefused("record input requires record and exact before_snapshot/after_snapshot bindings")
    raw = data["record"]
    if not isinstance(raw, dict):
        raise learning.LearningRefused("record must be an object")
    before = _source_ref(data["before_snapshot"], store)
    after = _source_ref(data["after_snapshot"], store)
    if before["sha256"] != raw.get("before_sha256") or after["sha256"] != raw.get("after_sha256"):
        raise learning.LearningRefused("before/after snapshots do not match the exact recorded digests")
    validator = raw.get("validator")
    if not isinstance(validator, dict) or validator.get("independent") is not True:
        raise learning.LearningRefused("independent validator evidence is required; model self-approval is rejected")
    report_ref = validator.get("report_source")
    bound = _source_ref(report_ref, store)
    package = references._load_package(store, bound["package_id"])
    if package.get("kind") != "source":
        raise learning.LearningRefused("validator evidence must come from an immutable source package")
    entry = next(item for item in package["files"] if item["path"] == bound["path"])
    try:
        report = json.loads(entry["content"])
    except (TypeError, ValueError) as exc:
        raise learning.LearningRefused("validator evidence is not a JSON run-stage report") from exc
    if raw.get("source", {}).get("sha256") != after["sha256"]:
        raise learning.LearningRefused("repair source must be the exact after snapshot")
    if report.get("independent") is not True or report.get("stage") not in {"validation", "validate"}:
        raise learning.LearningRefused("validator report must identify an independent immutable validation run stage")
    # Learning API performs exact PASS, validator, digest, and environment checks against the report.
    clean = learning.record_experience(raw, store)
    learning.save_experience(experience_path, clean, store)
    return clean


def reuse_repair(input_path: str | Path, store: str | Path, candidate: dict, path: str,
                 expected_sha256: str, tags: list[str], environment: dict,
                 executor_root: str | Path | None = None) -> dict:
    """Apply one proven local edit to candidate memory only; acceptance remains independent."""
    import applications
    import agent_tools
    data = _json(input_path)
    if not isinstance(data, dict) or set(data) != {'record', 'before_snapshot', 'after_snapshot'}:
        raise learning.LearningRefused('repair input must bind exact before/after snapshots')
    raw = data['record']
    clean = learning.record_experience(raw, store)
    report_ref = clean['validator']['report_source']
    report_package = references._load_package(store, report_ref['package_id'])
    report = json.loads(next(item['content'] for item in report_package['files'] if item['path'] == report_ref['path']))
    if (report.get('independent') is not True or report.get('stage') not in ('validation', 'validate')
            or report.get('before_test_pass') is not False or report.get('after_test_pass') is not True):
        raise learning.LearningRefused('executable repair requires independent observed FAIL-to-PASS evidence')
    valid, reason = _current_executor_identity(environment, executor_root)
    if not valid:
        raise learning.LearningRefused(reason)
    if clean['environment'] != environment or not tags or not set(tags).issubset(clean['problem_tags']):
        raise learning.LearningRefused('repair environment or full problem tags do not match target')
    snapshots = []
    for key, digest_key in (('before_snapshot', 'before_sha256'), ('after_snapshot', 'after_sha256')):
        ref = _source_ref(data[key], store)
        if ref['sha256'] != clean[digest_key]:
            raise learning.LearningRefused('repair snapshot differs from validated experience')
        package = references._load_package(store, ref['package_id'])
        snapshots.append(next(item['content'] for item in package['files'] if item['path'] == ref['path']))
    before, after = (text.splitlines(keepends=True) for text in snapshots)
    changes = [(i, j, a, b) for op, i, j, a, b in difflib.SequenceMatcher(
        None, before, after, autojunk=False).get_opcodes() if op != 'equal']
    if len(changes) != 1:
        raise learning.LearningRefused('repair reuse requires exactly one contiguous source change')
    i, j, a, b = changes[0]
    old, new = ''.join(before[i:j]), ''.join(after[a:b])
    proposal, _ = applications.validate_candidate(candidate)
    if j - i == 1 and b - a == 1:
        # A statement may move to another nesting level; never normalize inside it.
        old_statement, new_statement = old.strip(' \t\r\n'), new.strip(' \t\r\n')
        target = next((item['content'] for item in proposal['files'] if item['path'] == path), '')
        matches = [line for line in target.splitlines(keepends=True)
                   if line.strip(' \t\r\n') == old_statement]
        if len(matches) != 1:
            raise learning.LearningRefused('repair statement must match exactly one complete target line')
        line = matches[0]
        indent = line[:len(line) - len(line.lstrip(' \t'))]
        ending = line[len(line.rstrip('\r\n')):]
        old, new = line, indent + new_statement + ending
    if not old or not new or max(len(old), len(new)) > agent_tools.MAX_MUTATION_CHARS:
        raise learning.LearningRefused('repair reuse requires a nonempty bounded replacement')
    tool_result = agent_tools.execute('edit_file', {'path': path, 'expected_sha256': expected_sha256,
                                                  'old': old, 'new': new}, proposal, [], lambda _: None)
    return {'schema': 1, 'status': 'CANDIDATE_REPAIRED_PENDING_VALIDATION', 'candidate': proposal,
            'experience_id': clean['experience_id'], 'experience_sha256': clean['sha256'],
            'before_snapshot_sha256': clean['before_sha256'], 'after_snapshot_sha256': clean['after_sha256'],
            'invalidation_conditions': list(clean['invalidation_conditions']), 'edit': tool_result,
            'independent_acceptance_required': True, 'execution_authorized': False,
            'limitations': ['Exact textual match is not semantic applicability proof; target scope is an operator declaration.',
                            'Candidate memory only; source project, validator and release state are unchanged.']}


def _event_rows(run: Path) -> tuple[list[dict], str]:
    path = run / "events.jsonl"
    raw = path.read_bytes()
    rows = []
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise learning.LearningRefused(f"invalid event line {number}") from exc
        if not isinstance(item, dict):
            raise learning.LearningRefused(f"event line {number} is not an object")
        rows.append(item)
    return rows, hashlib.sha256(raw).hexdigest()


def _run_summary(path: str | Path) -> dict:
    run = Path(path)
    report = _json(run / "report.json")
    if not isinstance(report, dict):
        raise learning.LearningRefused("run report must be an object")
    events, events_sha = _event_rows(run)
    tool_names = {e.get("call_id"): e.get("name") for e in events if e.get("type") == "tool_started"}
    validations = [e for e in events if e.get("type") == "tool_result" and
                   tool_names.get(e.get("call_id")) == "validate" and isinstance(e.get("result"), dict)]
    pass_count = sum(e["result"].get("status") == "PASS" for e in validations)
    fail_count = sum(e["result"].get("status") == "FAIL" for e in validations)
    return {"run_id": report.get("run_id", report.get("session_id")), "task_id": report.get("task_id"),
            "outcome": report.get("status", report.get("task_status")),
            "report_sha256": hashlib.sha256((run / "report.json").read_bytes()).hexdigest(),
            "events_sha256": events_sha, "event_count": len(events),
            "validation_count": len(validations), "validation_pass_count": pass_count,
            "validation_fail_count": fail_count, "report": report}


def compare(before_path: str | Path, after_path: str | Path) -> dict:
    before, after = _run_summary(before_path), _run_summary(after_path)
    comparable = bool(before["task_id"] and before["task_id"] == after["task_id"] and
                      before["run_id"] and after["run_id"] and before["run_id"] != after["run_id"])
    result = {"schema": SCHEMA, "kind": "two-run-outcome-comparison", "sample_match": comparable,
              "benefit": "not_proven", "runs": [{k:v for k,v in before.items() if k != "report"},
                                                    {k:v for k,v in after.items() if k != "report"}],
              "limitations": ["Two-run association does not establish causation.",
                              "A missing or mismatched task/run identity is not a valid paired sample."]}
    if comparable:
        result["outcome_changed"] = before["outcome"] != after["outcome"]
        result["validation_count_delta"] = after["validation_count"] - before["validation_count"]
        result["validation_pass_count_delta"] = after["validation_pass_count"] - before["validation_pass_count"]
        result["validation_fail_count_delta"] = after["validation_fail_count"] - before["validation_fail_count"]
    else:
        result["reason"] = "missing or mismatched task_id/run_id; no paired sample"
    result["sha256"] = references.digest(references.canonical(result))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("select"); s.add_argument("directory"); s.add_argument("--reference-store", required=True); s.add_argument("--executor-root")
    s.add_argument("--tag", action="append", required=True); s.add_argument("--environment-json", required=True)
    s.add_argument("--limit", type=int, default=10); s.add_argument("--output", required=True)
    e = sub.add_parser("export"); e.add_argument("directory"); e.add_argument("--experience-store", required=True); e.add_argument("--executor-root")
    e.add_argument("--reference-store", required=True); e.add_argument("--tag", action="append", required=True)
    e.add_argument("--environment-json", required=True); e.add_argument("--limit", type=int, default=10)
    r = sub.add_parser("record"); r.add_argument("input"); r.add_argument("experience_path"); r.add_argument("--reference-store", required=True)
    reuse = sub.add_parser('reuse-repair', help='apply one exact validated edit to a new candidate file; never executes it')
    reuse.add_argument('input'); reuse.add_argument('--reference-store', required=True)
    reuse.add_argument('--candidate', required=True); reuse.add_argument('--path', required=True)
    reuse.add_argument('--expected-sha256', required=True); reuse.add_argument('--tag', action='append', required=True)
    reuse.add_argument('--environment-json', required=True); reuse.add_argument('--executor-root'); reuse.add_argument('--output', required=True)
    u = sub.add_parser("record-use"); u.add_argument("directory"); u.add_argument("--reference-store", required=True); u.add_argument("--tag", action="append", required=True); u.add_argument("--environment-json", required=True); u.add_argument("--task-id", required=True); u.add_argument("--baseline-run", required=True); u.add_argument("--reused-run", required=True); u.add_argument("--context", required=True); u.add_argument("--executor-root")
    c = sub.add_parser("compare"); c.add_argument("before_run"); c.add_argument("after_run"); c.add_argument("--output")
    context = sub.add_parser("prepare"); context.add_argument("directory"); context.add_argument("--reference-store", required=True); context.add_argument("--tag", action="append", required=True); context.add_argument("--environment-json", required=True); context.add_argument("--task", required=True); context.add_argument("--output", required=True); context.add_argument("--limit", type=int, default=10); context.add_argument("--executor-root"); context.add_argument("--delivery", choices=("reference", "atomic-summary"), default="reference")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_task_context(args.directory, args.reference_store, args.tag, _json(args.environment_json), args.task, args.output, args.limit, args.executor_root, args.delivery)
        elif args.command == 'reuse-repair':
            result = reuse_repair(args.input, args.reference_store, _json(args.candidate), args.path,
                                  args.expected_sha256, args.tag, _json(args.environment_json), args.executor_root)
            with Path(args.output).open('xb') as stream:
                stream.write(references.canonical(result) + b'\n')
        elif args.command == "select":
            result = select(args.directory, args.reference_store, args.tag, _json(args.environment_json), args.limit, args.executor_root)
            Path(args.output).write_bytes(references.canonical(result) + b"\n")
        elif args.command == "record":
            result = record(args.input, args.experience_path, args.reference_store)
        elif args.command == "record-use":
            result = learning.record_experience_use(args.directory, reference_store=args.reference_store, problem_tags=args.tag, environment=_json(args.environment_json), task_id=args.task_id, baseline_run=args.baseline_run, reused_run=args.reused_run, context_path=args.context, executor_root=args.executor_root)
        elif args.command == "export":
            result = export_reference(args.directory, args.experience_store, args.tag, _json(args.environment_json), args.reference_store, args.limit, args.executor_root)
        else:
            result = compare(args.before_run, args.after_run)
            if args.output: Path(args.output).write_bytes(references.canonical(result) + b"\n")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, StopIteration) as exc:
        print(f"HOLD: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
