#!/usr/bin/env python3
"""Revalidate a current repair, then pair fresh model runs on an explicitly selected task."""
from __future__ import annotations
import argparse, hashlib, json, math, os, subprocess, sys, time, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "bootstrap-runs/experience-current-revalidation-5e15606e7d9b"
IMAGE = "sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60"
MODEL = r"H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\text_encoders\Qwen\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf"
BASE_URL = "http://127.0.0.1:8082/v1"
COUPON_GOAL = ("Build a Python standard-library JSON-lines coupon ledger in main.py. Fresh process has empty redeemed codes. "
               "op redeem with nonempty string code returns {ok:true,redeemed:true} first use, {ok:true,redeemed:false} repeated. "
               "op status with valid code returns {ok:true,redeemed:boolean}. Invalid request type, malformed/missing/empty/nonstring code, "
               "unknown op returns {ok:false,error:invalid} without mutation and continues. Codes are case-sensitive. "
               "Return exactly one JSON response per input line and no other stdout. Validate before claiming success.")
COUPON_TAGS = ["coupon", "redeem", "state-validation", "jsonl", "python"]
CATALOG_GOAL = ("Build a Python standard-library JSON-lines book catalog in main.py. Maintain books keyed by nonempty title with nonempty author. "
                "Each fresh process starts empty. op add inserts or updates a book and returns {ok:true,title,author}; "
                "op list returns {ok:true,books:[{title,author},...]} sorted by title ascending. Invalid request types, "
                "empty/non-string title or author, and unknown operations return {ok:false,error:invalid_item} without mutation. "
                "Malformed JSON returns {ok:false,error:invalid_request}; continue after every invalid line. "
                "Return exactly one JSON response per input line and no other stdout. Validate before claiming success.")
CATALOG_TAGS = ["ordering", "jsonl", "python"]

PREREGISTERED_PAIRS = {
    "s1": {"delivery": "reference", "arm_order": "baseline-first"},
    "s2": {"delivery": "atomic-summary", "arm_order": "reuse-first"},
    "s3": {"delivery": "atomic-summary", "arm_order": "baseline-first"},
    "s4": {"delivery": "reference", "arm_order": "reuse-first"},
}

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def run_validator(snapshot: Path, contract: Path, dest: Path) -> dict:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    envelope = {"schema": 1, "workspace_manifest": manifest, "candidate_sha256": manifest.get("candidate_sha256")}
    proc = subprocess.run([sys.executable, str(ROOT / "application_builder.py"), "--engine", "docker", "--image", IMAGE, "--contract", str(contract)], input=json.dumps(envelope), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False)
    if proc.returncode != 0: raise RuntimeError(f"current isolated validation failed to run: {proc.stderr.strip()[:500]}")
    report = json.loads(proc.stdout)
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report

def _experience_read_evidence(events: list[dict], summary: str, after_sha256: str) -> dict:
    """Separate observed reading from a later opportunity to act; neither proves use."""
    starts = {e.get('call_id'): e.get('name') for e in events if e.get('type') == 'tool_started'}
    reads = [(index, e) for index, e in enumerate(events)
             if e.get('type') == 'tool_result' and starts.get(e.get('call_id')) == 'read_reference'
             and isinstance(e.get('result'), dict) and isinstance(e['result'].get('content'), str)
             and summary and after_sha256 and summary in e['result']['content'] and after_sha256 in e['result']['content']]
    opportunity = any(type(read.get('turn')) is int and any(
        e.get('type') == 'model_call_intent' and type(e.get('turn')) is int and e['turn'] > read['turn']
        for e in events[index + 1:]) for index, read in reads)
    return {'experience_read_observed': bool(reads), 'post_read_model_opportunity_observed': opportunity}

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=MODEL, help="exact local model file path")
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--prepare-only", action="store_true", help="revalidate and prepare, without dispatching a model")
    p.add_argument("--target", choices=("coupon", "catalog"), default="coupon")
    p.add_argument("--overlap-interval", help="optional observed external run interval START,END unix seconds; used only to qualify timing comparability")
    p.add_argument("--delivery", choices=("reference", "atomic-summary"), default="reference", help="reuse arm delivery; default preserves full-reference read route")
    p.add_argument("--arm-order", choices=("baseline-first", "reuse-first"), default="baseline-first")
    p.add_argument("--sample-id", choices=tuple(PREREGISTERED_PAIRS), help="fixed four-pair delivery/order assignment")
    p.add_argument("--write-preregistration", action="store_true", help="write the finite plan before model runs")
    a = p.parse_args()
    if a.write_preregistration and (a.sample_id or "--delivery" in sys.argv or "--arm-order" in sys.argv or "--target" in sys.argv):
        raise ValueError("write-preregistration is standalone; sample options are forbidden")
    if a.sample_id:
        assignment = PREREGISTERED_PAIRS[a.sample_id]
        a.target = "catalog"
        if "--delivery" in sys.argv or "--arm-order" in sys.argv:
            raise ValueError("--sample-id cannot be combined with manual delivery/order overrides")
        a.delivery, a.arm_order = assignment["delivery"], assignment["arm_order"]
    if a.write_preregistration:
        plan = {"schema": 1, "kind": "experience-summary-intervention-four-pair-preregistration", "samples": PREREGISTERED_PAIRS,
                "budgets_per_arm": {"model_calls": 12, "tool_calls": 48, "tokens": 500000, "seconds": 240},
                "task": "same authored catalog task/contract/starter per pair; only difference between arms is absence vs selected experience delivery (full-reference package or one-line summary); arm order counterbalanced",
                "secondary": ["actual model/tool calls", "server-reported total tokens"],
                "positive_rule": "atomic-summary acceptance count >= full-reference count; all four pairs completed; every full-reference arm records actual read; atomic summary is in the reuse prompt; and summary delivery uses fewer model calls in each both-accepted pair; otherwise no positive result",
                "no_replay": "No retries, no unknown in-flight replay, no sample dropping, no budget changes, no selective stopping; parent serializes endpoint calls.",
                "limitations": ["Four descriptive pairs cannot establish causal or general benefit.", "No seed-level model determinism guarantee.", "Timing uses shared local endpoint and is descriptive only, not exclusive-server latency.", "No-match full-tag/environment retrieval is a pre-run HOLD, not a skipped sample."]}
        path = ROOT / "bootstrap-runs/experience-summary-preregistered-20261105.json"
        if path.exists(): raise RuntimeError("preregistration is immutable and already exists")
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"preregistration": str(path), "samples": 4, "status": "PREPARED"}))
        return 0
    overlap_interval = None
    if a.overlap_interval:
        try:
            start_s, end_s = (float(part) for part in a.overlap_interval.split(",", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("overlap interval must be START,END unix seconds") from exc
        if not math.isfinite(start_s) or not math.isfinite(end_s) or start_s < 0 or end_s <= start_s:
            raise ValueError("overlap interval must have finite nonnegative start earlier than end")
        overlap_interval = [start_s, end_s]
    run_id = uuid.uuid4().hex[:12]
    out = ROOT / "bootstrap-runs" / f"experience-current-pair-{run_id}"
    out.mkdir(parents=True, exist_ok=False)
    validation_contract = ROOT / "fixtures/business/contract.json"
    if digest(validation_contract) != "1fb7420ce64d9009a54a473e69bd6740807ec20fc5e12c2a46168d038adadfa3":
        raise RuntimeError("locked current-revalidation contract changed; no experience recorded")
    if a.target == "catalog":
        contract = ROOT / "fixtures/experience_transfer/catalog_contract.json"
        goal, task_tags = CATALOG_GOAL, CATALOG_TAGS
        task_project = ROOT / "fixtures/experience_transfer/starter"
    else:
        contract = ROOT / "bootstrap-runs/current-atom-pair-1874d7960ead/contract.json"
        goal, task_tags = COUPON_GOAL, COUPON_TAGS
        task_project = None
    if not contract.is_file(): raise RuntimeError("selected task contract is unavailable")
    before, after = SOURCE / "before", SOURCE / "after"
    before_report = run_validator(before, validation_contract, out / "before-validation.json")
    after_report = run_validator(after, validation_contract, out / "after-validation.json")
    if before_report.get("test_pass") is True or after_report.get("test_pass") is not True:
        raise RuntimeError("fresh current-builder validation did not reproduce FAIL-to-PASS; no experience recorded")
    import application_experience_adapter as adapter
    import references
    store = out / "reference-store"
    before_ingest = references.ingest(before / "main.py", store, "current revalidation before source", "internal", kind="source")
    after_ingest = references.ingest(after / "main.py", store, "current revalidation after source", "internal", kind="source")
    env = {"adapter": "application_builder.jsonl-container", "engine": "docker-linux", "image": IMAGE,
           "runtime": "python-3.11.18", "scope": "stdlib-python-jsonl-inventory-ordering",
           "executor_sources": {name: digest(ROOT / name) for name in adapter.EXECUTOR_SOURCE_ALLOWLIST}}
    before_package = references._load_package(store, before_ingest["package_id"])
    after_package = references._load_package(store, after_ingest["package_id"])
    before_entry, after_entry = before_package["files"][0], after_package["files"][0]
    before_ref = {"package_id": before_ingest["package_id"], "path": before_entry["path"], "sha256": before_entry["sha256"], "permission": "inspect"}
    after_ref = {"package_id": after_ingest["package_id"], "path": after_entry["path"], "sha256": after_entry["sha256"], "permission": "inspect"}
    evidence = {"schema": 1, "status": "PASS", "stage": "validation", "independent": True,
                "validator_id": f"current-container-revalidation:{digest(ROOT / 'application_builder.py')}",
                "before_sha256": before_ref["sha256"], "after_sha256": after_ref["sha256"],
                "environment": env, "contract_sha256": digest(validation_contract),
                "before_report_sha256": digest(out / "before-validation.json"),
                "after_report_sha256": digest(out / "after-validation.json"),
                "before_test_pass": before_report["test_pass"], "after_test_pass": after_report["test_pass"],
                "provenance": "Authored immutable inventory snapshots freshly checked by current isolated builder; FAIL-to-PASS records SKU ordering correction, not a model-authored repair."}
    evidence_path = out / "current-validator-evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence_ingest = references.ingest(evidence_path, store, "composite current validator evidence", "internal", kind="source")
    evidence_package = references._load_package(store, evidence_ingest["package_id"])
    evidence_entry = evidence_package["files"][0]
    evidence_ref = {"package_id": evidence_ingest["package_id"], "path": evidence_entry["path"], "sha256": evidence_entry["sha256"], "permission": "inspect"}
    exp_id = f"current-ordering-{run_id}"
    record = {"schema": 1, "experience_id": exp_id, "problem_tags": ["ordering", "jsonl", "python"],
              "before_sha256": before_ref["sha256"], "after_sha256": after_ref["sha256"],
              "repair_summary": "Change inventory list ordering from descending to ascending SKU.", "environment": env,
              "validator": {"status": "PASS", "report_sha256": evidence_ref["sha256"], "validator_id": evidence["validator_id"], "independent": True, "report_source": evidence_ref},
              "invalidation_conditions": ["ordering semantics change", "JSON-lines contract changes", "Python runtime major/minor changes", "executor or container image identity changes"],
              "source": after_ref}
    record_input = out / "record-input.json"
    record_input.write_text(json.dumps({"record": record, "before_snapshot": before_ref, "after_snapshot": after_ref}), encoding="utf-8")
    experience_dir = out / "experiences"
    adapter.record(record_input, experience_dir / f"{exp_id}.json", store)
    task_id = f"experience-pair-{run_id}"
    task = out / "task.json"
    task.write_text(json.dumps({"task_id": task_id, "goal": goal}), encoding="utf-8")
    context_path = out / "context.json"
    context = adapter.prepare_task_context(experience_dir, store, task_tags, env, task, context_path, executor_root=ROOT, delivery=a.delivery)
    eligible = context["retrieved"]["references"]
    if a.sample_id and len(eligible) != 1:
        raise RuntimeError("registered four-pair sample requires exactly one full-tag validated experience; retain HOLD if absent or ambiguous")
    exported = adapter.export_reference(experience_dir, store, task_tags, env, store, executor_root=ROOT) if eligible else None
    (out / "contract.json").write_bytes(contract.read_bytes())
    contract_sha = digest(out / "contract.json")
    starter_files = {}
    if task_project:
        import application_project
        imported = application_project.import_project(task_project, goal=goal, references=[])
        frozen_project = out / "frozen-starter"
        frozen_project.mkdir()
        for entry in imported["candidate"]["files"]:
            path = frozen_project / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(entry["content"], encoding="utf-8", newline="\n")
            starter_files[entry["path"]] = digest(path)
        task_project = frozen_project
    prep = {"schema": 1, "status": "PREPARED", "experience_id": exp_id, "task_id": task_id, "target": a.target,
            "task_tags": task_tags, "delivery": a.delivery, "eligible_experience_ids": [row["experience_id"] for row in eligible],
            "reuse_arm": "run" if eligible else "skipped_no_relevant_experience",
            "reuse_policy": "all requested task tags and full exact environment required; no-match is retained HOLD, never substituted or dropped",
            "before_sha256": before_ref["sha256"], "after_sha256": after_ref["sha256"],
            "validator_evidence_sha256": evidence_ref["sha256"], "source_contract_sha256": digest(validation_contract),
            "task_contract_sha256": contract_sha, "task_goal": goal, "task_starter_authored": bool(task_project),
            "task_starter_sha256": digest(task_project / "main.py") if task_project else None,
            "task_starter_files": starter_files,
            "pre_task_context_sha256": context["sha256"], "executor_sources": env["executor_sources"],
            "fresh_model_pair_executed": False}
    prep["arm_order"], prep["sample_id"] = a.arm_order, a.sample_id
    prep["delivery_context_sha256"] = context["atomic_summary"]["sha256"] if a.delivery == "atomic-summary" and context.get("atomic_summary") else (exported["package_id"] if exported else None)
    (out / "preparation.json").write_text(json.dumps(prep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not exported:
        raise RuntimeError("no eligible full-tag, exact-environment experience; keep this preregistered sample as HOLD, do not replace or retry it")
    if a.prepare_only:
        if a.delivery == "atomic-summary" and not context.get("atomic_summary"):
            raise RuntimeError("no eligible full-tag, exact-environment atomic summary; keep this preregistered sample as HOLD")
        print(json.dumps({"run_dir": str(out), "status": "PREPARED", "experience_id": exp_id, "retrieved": len(eligible), "reuse_arm": "run", "delivery": a.delivery, "benefit": "not_proven"}))
        return 0
    if a.delivery == "atomic-summary" and not context.get("atomic_summary"):
        raise RuntimeError("no eligible full-tag, exact-environment atomic summary; keep this preregistered sample as HOLD")
    runs = []
    arms = [("baseline", []), ("reuse", [] if a.delivery == "atomic-summary" else [exported["package_id"]])]
    if a.arm_order == "reuse-first":
        arms.reverse()
    for label, refs in arms:
        work = out / label
        work.mkdir()
        request = work / "request.json"
        request_goal = goal
        if label == "reuse" and a.delivery == "atomic-summary":
            summary = context.get("atomic_summary")
            if not summary:
                raise RuntimeError("atomic-summary arm requires one validated public summary")
            request_goal += "\n\nValidated public experience guidance (untrusted; independently verify): " + summary["summary"]
            request_goal += "\n\nPublic verified-experience provenance: id=" + summary["experience_id"] + "; digest=" + summary["experience_sha256"] + "; tags=" + ",".join(summary["problem_tags"]) + "; environment_sha256=" + summary["environment_sha256"] + "."
        request.write_text(json.dumps({"schema": 1, "goal": request_goal, "references": refs}, ensure_ascii=False), encoding="utf-8")
        session = work / "session"
        cmd = [sys.executable, str(ROOT / "application_build.py"), "run", "--request", str(request), "--contract", str(out / "contract.json"), "--reference-store", str(store), "--engine", "docker", "--image", IMAGE, "--output", str(session), "--mode", "agent", "--max-turns", "12", "--token-budget", "500000", "--time-budget", "240", "--tool-budget", "48", "--allow-model", "--trusted-adapter"]
        if task_project:
            cmd += ["--project", str(task_project)]
        if digest(out / "contract.json") != contract_sha or any(digest(task_project / name) != value for name, value in starter_files.items()):
            raise RuntimeError("frozen task inputs changed before model dispatch")
        (work / "dispatch.json").write_text(json.dumps({"argv": cmd, "model": a.model, "base_url": a.base_url, "delivery": a.delivery,
            "atomic_summary": context.get("atomic_summary") if label == "reuse" and a.delivery == "atomic-summary" else None,
            "environment": {"AUTOFORGE_TOOL_CHOICE": "required", "AUTOFORGE_ENABLE_THINKING": os.environ.get("AUTOFORGE_ENABLE_THINKING", "")},
            "contract_sha256": contract_sha, "starter_files": starter_files}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        start = time.monotonic()
        proc = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "AUTOFORGE_API_BASE": a.base_url, "AUTOFORGE_MODEL": a.model, "AUTOFORGE_API_KEY": "", "AUTOFORGE_TOOL_CHOICE": "required"}, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=270, check=False)
        (work / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
        (work / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
        try: cli = json.loads(proc.stdout)
        except ValueError: cli = {"status": "HOLD", "reason": "non-JSON CLI output"}
        ep = session / "events.jsonl"
        events = [json.loads(line) for line in ep.read_text(encoding="utf-8").splitlines()] if ep.exists() else []
        starts = {e.get("call_id"): e.get("name") for e in events if e.get("type") == "tool_started"}
        read_evidence = _experience_read_evidence(events, record['repair_summary'], record['after_sha256'])
        validations = [e.get("result", {}).get("status") for e in events if e.get("type") == "tool_result" and starts.get(e.get("call_id")) == "validate" and isinstance(e.get("result"), dict)]
        if label == "reuse" and a.delivery == "atomic-summary":
            read_evidence = {"experience_read_observed": False, "experience_delivery_observed": "atomic-summary-injected",
                             "summary_sha256": context["atomic_summary"]["sha256"], "post_read_model_opportunity_observed": False}
        else:
            read_evidence["experience_delivery_observed"] = "full-reference-read" if read_evidence["experience_read_observed"] else "not-observed"
        runs.append({"label": label, "returncode": proc.returncode, "runtime": cli, "elapsed_seconds": time.monotonic() - start,
                     "validations": validations, **read_evidence, "run_dir": str(session)})
    run_intervals = []
    for run in runs:
        events_path = Path(run["run_dir"]) / "events.jsonl"
        if events_path.is_file():
            event_rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            started = next((event["timestamp"] for event in event_rows if event.get("type") == "run_started"), None)
            ended = next((event["timestamp"] for event in reversed(event_rows) if event.get("type") == "run_finished"), None)
            if started is not None and ended is not None: run_intervals.append([started, ended])
    observed_overlap = bool(overlap_interval and any(left < overlap_interval[1] and overlap_interval[0] < right for left, right in run_intervals))
    by_label = {run["label"]: run for run in runs}
    baseline_run, reuse_run = by_label["baseline"], by_label["reuse"]
    report = {"schema": 1, "status": "OBSERVED", "task_id": task_id, "target": a.target,
              "pairing": {"same_goal": True, "same_contract_sha256": contract_sha, "same_starter_sha256": prep["task_starter_sha256"],
                          "same_budgets": True, "delivery": a.delivery, "reuse_arm": "run" if exported else "skipped_no_relevant_experience",
                          "arm_order": a.arm_order, "sample_id": a.sample_id,
                          "distinct_fresh_sessions": len(runs) == 2 and runs[0]["runtime"].get("session_id") != runs[1]["runtime"].get("session_id"),
                          "external_overlap_interval": overlap_interval,
                          "shared_endpoint_overlap_observed": observed_overlap,
                          "elapsed_comparison_interpretable": False if observed_overlap else "not_evaluated"},
              "source_experience": {"id": exp_id, "tags": record["problem_tags"], "repair_summary": record["repair_summary"],
                                    "authored_source_fail_to_pass": True, "model_authored_repair": False,
                                    "before_sha256": before_ref["sha256"], "after_sha256": after_ref["sha256"], "validator_evidence_sha256": evidence_ref["sha256"]},
              "pre_task_context_sha256": context["sha256"], "atomic_summary_sha256": context.get("atomic_summary", {}).get("sha256"), "runs": runs,
              "observed_outcome_delta": {"baseline_status": baseline_run["runtime"].get("status"), "reuse_status": reuse_run["runtime"].get("status"),
                                           "baseline_validation_events": baseline_run["validations"], "reuse_validation_events": reuse_run["validations"],
                                           "baseline_elapsed_seconds": baseline_run["elapsed_seconds"], "reuse_elapsed_seconds": reuse_run["elapsed_seconds"],
                                           "baseline_tool_calls": baseline_run["runtime"].get("tool_calls"), "reuse_tool_calls": reuse_run["runtime"].get("tool_calls"),
                                           "baseline_budget": baseline_run["runtime"].get("budget"), "reuse_budget": reuse_run["runtime"].get("budget")}, 
              "reuse_experience_delivery_observed": next((run.get("experience_delivery_observed") for run in runs if run["label"] == "reuse"), "not-observed"),
              "benefit": "not_proven", "limitations": ["One paired sample is descriptive and does not establish causal or stable transfer benefit.",
                  "Source experience is an authored inventory ordering correction, not model-generated evidence and not a catalog-domain repair.",
                  "Target uses the same authored descending-order starter in both arms and a distinct book-catalog contract.",
                  "Elapsed comparison is not evaluated unless an external overlap interval is supplied.",
                  "Reference read and atomic-summary injection are separate delivery evidence; neither implies semantic use.",
                  "No positive claim is embedded here; aggregate the complete four-pair plan against its predeclared rule only."]}
    (out / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reuse_run = next((run for run in runs if run["label"] == "reuse"), None)
    print(json.dumps({"run_dir": str(out), "status": report["status"], "benefit": report["benefit"],
                      "reuse_arm": report["pairing"]["reuse_arm"], "reuse_read_observed": bool(reuse_run and reuse_run["experience_read_observed"])}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
