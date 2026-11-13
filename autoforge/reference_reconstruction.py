#!/usr/bin/env python3
"""Isolated JSON-lines behavioral comparison against an operator-authored licensed reference contract."""
from __future__ import annotations
import argparse, hashlib, json, math, os, stat, tempfile
from pathlib import Path
import references
import application_builder
import container_env

class ReconstructionHold(ValueError): pass

def _sha(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def _canon(value) -> bytes: return references.canonical(value)

def _regular_chain(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    for part in (absolute, *absolute.parents):
        st = part.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ReconstructionHold("path contains symlink or reparse point")
    return absolute

def _snapshot_candidate(source: Path, destination: Path) -> list[dict]:
    manifest = source / "manifest.json"
    if manifest.exists():
        # Accept a staged workspace only when its own manifest and every digest are valid.
        try: raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc: raise ReconstructionHold("staged candidate manifest is invalid") from exc
        if not isinstance(raw, dict) or not {"files", "workspace", "candidate_sha256"} <= set(raw) or Path(raw["workspace"]).resolve() != source.resolve():
            raise ReconstructionHold("staged candidate manifest does not bind this workspace")
        if not isinstance(raw["files"],list) or any(not isinstance(item,dict) or set(item)!={"path","size","sha256"} for item in raw["files"]): raise ReconstructionHold("staged candidate manifest file records are invalid")
        listed = {item["path"]: item for item in raw["files"]}
        if len(listed)!=len(raw["files"]): raise ReconstructionHold("staged candidate manifest contains duplicate paths")
        actual = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file() and p.name != "manifest.json"}
        try:
            canonical={"schema":1,"goal":raw.get("goal"),"references":raw.get("references"),"files":[{"path":item["path"],"size":item["size"],"sha256":item["sha256"]} for item in raw["files"]]}
            computed=_sha(_canon(canonical))
        except (KeyError,TypeError,ValueError) as exc: raise ReconstructionHold("staged candidate manifest payload invalid") from exc
        if computed!=raw["candidate_sha256"]: raise ReconstructionHold("staged candidate manifest candidate digest mismatch")
    else:
        listed = None
    snapshot=[]
    for node in sorted(source.rglob("*")):
        _regular_chain(node)
        if node.is_dir(): continue
        if not node.is_file(): raise ReconstructionHold("candidate contains nonregular file")
        rel=node.relative_to(source).as_posix()
        if rel=="manifest.json": continue
        if rel.startswith("../") or "\\" in rel or any(part in {"", ".", ".."} for part in rel.split("/")): raise ReconstructionHold("candidate path is unsafe")
        data=node.read_bytes(); digest=_sha(data)
        if listed is not None and (listed[rel].get("sha256")!=digest or listed[rel].get("size")!=len(data)):
            raise ReconstructionHold(f"candidate manifest digest mismatch: {rel}")
        target=destination.joinpath(*Path(rel).parts);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        snapshot.append({"path":rel,"sha256":digest,"size":len(data)})
    if not any(item["path"]=="main.py" for item in snapshot): raise ReconstructionHold("candidate main.py is absent")
    return snapshot

def evaluate(store, package_id, candidate, contract_path, *, engine, image):
    package=references._load_package(store,package_id)
    if package["kind"]!="source": raise ReconstructionHold("reference must be licensed source, not REA evidence or candidate output")
    contract=json.loads(Path(contract_path).read_text(encoding="utf-8"))
    required={"schema","reference_package_id","license_acknowledged","entrypoint","scenarios","obligations"}
    if not isinstance(contract,dict) or set(contract)!=required or type(contract["schema"]) is not int or contract["schema"]!=1: raise ReconstructionHold("invalid operator-authored behavior contract")
    if contract["reference_package_id"]!=package_id or contract["license_acknowledged"] is not True: raise ReconstructionHold("contract must bind package and explicit license acknowledgement")
    if contract["entrypoint"]!="main.py": raise ReconstructionHold("entrypoint must be main.py")
    source_files={f["path"]:f for f in package["files"]}
    if "main.py" not in source_files: raise ReconstructionHold("licensed reference package lacks main.py JSON-lines entrypoint")
    if not isinstance(contract["obligations"],list) or not contract["obligations"] or any(not isinstance(x,str) or not x.strip() for x in contract["obligations"]): raise ReconstructionHold("nonempty operator-authored behavior obligations required")
    scenarios=application_builder._contract({"schema":1,"entrypoint":"main.py","scenarios":contract["scenarios"]})
    candidate_root=_regular_chain(Path(candidate))
    if not candidate_root.is_dir(): raise ReconstructionHold("candidate must be a regular staged directory")
    info=application_builder.preflight(engine,image)
    reports={}
    with tempfile.TemporaryDirectory(prefix="autoforge-reference-run-") as temp:
        root=Path(temp)
        os.chmod(root,0o755)
        for label, files in (("reference",source_files),("candidate",None)):
            scenario_results=[]
            for index,scenario in enumerate(scenarios):
                work=root/f"{label}-{index}";work.mkdir()
                if files is not None:
                    snapshot=[]
                    for rel,row in files.items():
                        target=work.joinpath(*Path(rel).parts);target.parent.mkdir(parents=True,exist_ok=True);data=row["content"].encode("utf-8");target.write_bytes(data)
                        snapshot.append({"path":rel,"sha256":row["sha256"],"size":len(data)})
                else:
                    snapshot=_snapshot_candidate(candidate_root,work)
                payload=b"".join(application_builder._json_bytes(req)+b"\n" for req in scenario["requests"])
                out,err,code,timed=application_builder._container(engine,image,work,payload,8)
                result={"scenario":scenario["name"],"exit_code":code,"timed_out":timed,"stdout_sha256":_sha(out),"stderr_sha256":_sha(err),"stdout_bytes":len(out),"snapshot_files":snapshot}
                try:
                    actual=[json.loads(line) for line in out.decode("utf-8","strict").splitlines()]
                    expected=scenario["expected"]
                    result["behavior_pass"]=not timed and code==0 and len(actual)==len(expected) and all(_canon(a)==_canon(e) for a,e in zip(actual,expected))
                except (UnicodeError,ValueError): result["behavior_pass"]=False;result["parse_error"]=True
                scenario_results.append(result)
            reports[label]={"behavior_pass":bool(scenario_results) and all(x["behavior_pass"] for x in scenario_results),"scenarios":scenario_results}
    if not reports["reference"]["behavior_pass"]: raise ReconstructionHold("operator-authored contract failed against selected reference; reference not accepted as fixture")
    result={"schema":1,"kind":"isolated-reference-reconstruction","status":"PASS" if reports["candidate"]["behavior_pass"] else "FAIL","reference":{"package_id":package_id,"origin":package["origin"],"license":package["license"],"source_sha256":_sha(_canon(package))},"candidate":{"path":str(candidate_root.resolve())},"contract_sha256":_sha(_canon(contract)),"environment":info,"reference_run":reports["reference"],"candidate_run":reports["candidate"],"obligations":contract["obligations"],"limitations":["Only operator-declared JSON-lines scenarios are checked; not general business correctness.","Reference and candidate are untrusted and run independently in a fresh isolated container per scenario.",*package["limitations"]]}
    result["sha256"]=_sha(_canon(result));return result



def _evidence_records(package):
    if package.get("kind") != "rea-evidence":
        raise ReconstructionHold("evidence binding requires a rea-evidence package")
    by_id = {}
    for item in package.get("upstream_evidence", []):
        record = item.get("record")
        evidence_id = item.get("evidence_id")
        if not isinstance(record, dict) or record.get("evidence_id", record.get("id")) != evidence_id:
            raise ReconstructionHold("REA evidence record identity mismatch")
        if evidence_id in by_id:
            raise ReconstructionHold("duplicate REA evidence id")
        by_id[evidence_id] = record
    return by_id


def _artifact_snapshot(root, expected):
    root = _regular_chain(Path(root))
    if not root.is_dir() or not isinstance(expected, dict) or not expected:
        raise ReconstructionHold("artifact root and exact file digest map are required")
    seen = {}
    for node in sorted(root.rglob("*")):
        _regular_chain(node)
        if node.is_dir():
            continue
        if not node.is_file():
            raise ReconstructionHold("artifact contains a nonregular file")
        rel = node.relative_to(root).as_posix()
        data = node.read_bytes()
        seen[rel] = {"path": rel, "size": len(data), "sha256": _sha(data)}
    if set(seen) != set(expected):
        raise ReconstructionHold("artifact file set differs from explicit contract binding")
    for rel, digest in expected.items():
        if not isinstance(digest, str) or seen[rel]["sha256"] != digest:
            raise ReconstructionHold(f"artifact digest mismatch: {rel}")
    return root, [seen[key] for key in sorted(seen)]




def evaluate_evidence(store, evidence_package_id, reference_package_id, candidate, contract_path,
                      artifact_root, *, engine, image, reference_engine=None, reference_image=None):
    """Compare inert REA records, the exact bound JavaScript artifact, and candidate.

    Only SHA-256-bound shipped JavaScript bytes run in a network-disabled
    sandbox. REA Evidence JSON is never executed.
    """
    evidence = references._load_package(store, evidence_package_id)
    by_id = _evidence_records(evidence)
    package = references._load_package(store, reference_package_id)
    if package.get("kind") != "source" or not package.get("license", "").strip():
        raise ReconstructionHold("reference provenance must be a separately licensed source package")
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    required = {"schema", "evidence_package_id", "evidence_ids", "artifact_sha256", "artifact_files",
                "reference_package_id", "reference_main_js_sha256", "license_acknowledged", "reference_entrypoint", "candidate_entrypoint", "scenarios", "obligations"}
    if not isinstance(contract, dict) or set(contract) != required or type(contract.get("schema")) is not int or contract["schema"] != 1:
        raise ReconstructionHold("invalid evidence-derived contract")
    if contract["evidence_package_id"] != evidence_package_id or contract["reference_package_id"] != reference_package_id or contract["license_acknowledged"] is not True:
        raise ReconstructionHold("contract must bind evidence/reference packages and explicit license acknowledgement")
    ids = contract["evidence_ids"]
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or any(item not in by_id for item in ids):
        raise ReconstructionHold("contract must select unique evidence IDs present in the immutable package")
    root, artifact_files = _artifact_snapshot(artifact_root, contract["artifact_files"])
    if not any(item["sha256"] == contract["artifact_sha256"] for item in artifact_files):
        raise ReconstructionHold("selected artifact digest is not present in bound artifact files")
    source_artifact_evidence = set()
    for evidence_id in ids:
        record = by_id[evidence_id]
        if record.get("operation") == "analyze_javascript_application":
            normalized = record.get("normalized_result")
            if not isinstance(normalized, dict):
                raise ReconstructionHold(f"selected application evidence has no normalized artifact result: {evidence_id}")
            graph = normalized.get("graph")
            nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
            known_digests = {normalized.get("root_artifact_sha256")}
            known_digests.update(node.get("identity", {}).get("sha256") for node in nodes if isinstance(node, dict) and isinstance(node.get("identity"), dict))
            if contract["artifact_sha256"] not in known_digests:
                raise ReconstructionHold(f"selected application evidence does not name bound artifact: {evidence_id}")
            source_artifact_evidence.add(evidence_id)
        elif record.get("operation") == "trace_javascript_semantics":
            links = record.get("evidence_links", [])
            normalized = record.get("normalized_result")
            contexts = normalized.get("evidence_contexts", []) if isinstance(normalized, dict) else []
            linked_artifact = any(isinstance(context.get("artifact"), dict) and context["artifact"].get("sha256") == contract["artifact_sha256"] for context in contexts if isinstance(context, dict))
            if not isinstance(links, list) or not any(link in ids for link in links) or not linked_artifact:
                raise ReconstructionHold(f"selected semantic trace lacks exact linked artifact provenance: {evidence_id}")
        elif record.get("operation") == "fixture_static_evidence" and isinstance(record.get("normalized_result"), dict) and record["normalized_result"].get("root_artifact_sha256") == contract["artifact_sha256"]:
            source_artifact_evidence.add(evidence_id)
        else:
            raise ReconstructionHold(f"unsupported evidence operation for behavioral reconstruction: {evidence_id}")
    if not source_artifact_evidence:
        raise ReconstructionHold("selected Evidence must include an exact artifact-bound application record")
    if contract["reference_entrypoint"] != "main.js" or contract["candidate_entrypoint"] != "main.py":
        raise ReconstructionHold("reference and candidate entrypoints must be main.js and main.py respectively")
    reference_files = {row["path"]: row for row in package["files"]}
    reference_main = reference_files.get("main.js")
    if reference_main is None or reference_main["sha256"] != contract["reference_main_js_sha256"]:
        raise ReconstructionHold("licensed reference main.js digest does not match its explicit contract binding")
    if reference_main["sha256"] != contract["artifact_sha256"]:
        raise ReconstructionHold("licensed reference main.js must be byte-identical to the REA-bound artifact")
    ref_files = {"main.js": {"path": "main.js", "content": reference_main["content"], "sha256": reference_main["sha256"]}}
    if not isinstance(contract["obligations"], list) or not contract["obligations"] or any(not isinstance(x, str) or not x.strip() for x in contract["obligations"]):
        raise ReconstructionHold("nonempty behavior obligations required")
    scenarios = application_builder._contract({"schema": 1, "entrypoint": "main.py", "scenarios": contract["scenarios"]})
    candidate_root = _regular_chain(Path(candidate))
    if not candidate_root.is_dir():
        raise ReconstructionHold("candidate must be a regular staged directory")
    if (reference_engine is None) != (reference_image is None):
        raise ReconstructionHold("reference engine and image must be supplied together")
    reference_engine = reference_engine or engine
    reference_image = reference_image or image
    reference_info = application_builder.preflight(reference_engine, reference_image)
    candidate_info = application_builder.preflight(engine, image)
    reports = {}
    with tempfile.TemporaryDirectory(prefix="autoforge-rea-reconstruction-") as temp:
        temp_root = Path(temp)
        os.chmod(temp_root,0o755)
        for label, files in (("operator_reference", ref_files), ("candidate", None)):
            results = []
            for index, scenario in enumerate(scenarios):
                work = temp_root / f"{label}-{index}"
                work.mkdir()
                if files is None:
                    snapshot = _snapshot_candidate(candidate_root, work)
                else:
                    snapshot = []
                    for rel, row in files.items():
                        target = work.joinpath(*Path(rel).parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        data = row["content"].encode("utf-8")
                        target.write_bytes(data)
                        snapshot.append({"path": rel, "sha256": row["sha256"], "size": len(data)})
                payload = b"".join(application_builder._json_bytes(req) + b"\n" for req in scenario["requests"])
                if label == "operator_reference":
                    out, err, code, timed = application_builder._container(reference_engine, reference_image, work, payload, 8, runtime="node")
                else:
                    out, err, code, timed = application_builder._container(engine, image, work, payload, 8)
                item = {"scenario": scenario["name"], "exit_code": code, "timed_out": timed,
                        "stdout_sha256": _sha(out), "stderr_sha256": _sha(err), "stdout_bytes": len(out),
                        "snapshot_files": snapshot}
                try:
                    actual = [json.loads(line) for line in out.decode("utf-8", "strict").splitlines()]
                    expected = scenario["expected"]
                    item["behavior_pass"] = not timed and code == 0 and len(actual) == len(expected) and all(_canon(a) == _canon(e) for a, e in zip(actual, expected))
                except (UnicodeError, ValueError):
                    item["behavior_pass"] = False
                    item["parse_error"] = True
                results.append(item)
            reports[label] = {"behavior_pass": bool(results) and all(item["behavior_pass"] for item in results), "scenarios": results}
    if not reports["operator_reference"]["behavior_pass"]:
        raise ReconstructionHold("independent scenarios failed against the selected operator reference")
    result = {"schema": 1, "kind": "rea-evidence-derived-reconstruction", "status": "PASS" if reports["candidate"]["behavior_pass"] else "FAIL",
              "evidence": {"package_id": evidence_package_id, "evidence_ids": ids, "package_sha256": _sha(_canon(evidence)),
                           "artifact_root": str(root.resolve()), "artifact_sha256": contract["artifact_sha256"], "artifact_files": artifact_files},
              "reference": {"package_id": reference_package_id, "origin": package["origin"], "license": package["license"], "package_sha256": _sha(_canon(package)),
                            "kind": "byte-identical-shipped-javascript", "entrypoint": contract["reference_entrypoint"], "main_js_sha256": reference_main["sha256"]},
              "candidate": {"path": str(candidate_root.resolve())}, "contract_sha256": _sha(_canon(contract)),
              "environment": {"operator_reference": reference_info, "candidate": candidate_info}, "reference_run": reports["operator_reference"], "candidate_run": reports["candidate"],
              "obligations": contract["obligations"],
              "limitations": ["REA evidence records remain inert; only byte-identical, SHA-256-bound JavaScript bytes are executed as the reference.",
                              "Acceptance is bounded to explicit operator scenarios; no claim of general behavioral equivalence or broad correctness.",
                              "Only explicit operator scenarios are tested; static REA evidence does not prove runtime behavior or broad correctness.",
                              "Reference and candidate run independently in fresh network-disabled Linux containers.", *evidence["limitations"], *package["limitations"]]}
    result["sha256"] = _sha(_canon(result))
    return result



def _stateful_restart_container(engine, image, work, invocations, timeout, *, runtime, state_file, precondition=None):
    """Run fresh child processes in one isolated sandbox with only ephemeral shared /tmp."""
    import base64, subprocess, uuid
    if runtime not in {"python", "node"} or not isinstance(invocations, list) or not invocations:
        raise ReconstructionHold("restart invocations and runtime must be explicit")
    if any(not isinstance(payload, bytes) or len(payload) > application_builder.MAX_INPUT for payload in invocations):
        raise ReconstructionHold("restart invocation batches must be bounded byte payloads")
    payload = _canon({"invocations": [base64.b64encode(item).decode("ascii") for item in invocations], "state_file": state_file, "precondition": precondition})
    if len(payload) > application_builder.MAX_INPUT:
        raise ReconstructionHold("aggregate restart payload exceeds input byte limit")
    work = _regular_chain(Path(work))
    if runtime == "python":
        command = ["python", "-I", "-c", (
            "import base64,json,subprocess,sys,os\nx=json.loads(sys.stdin.buffer.read());p=x['state_file'];v=x.get('precondition')\n"
            "if v is None:\n try: os.unlink(p)\n except FileNotFoundError: pass\n"
            "elif v.get('kind')=='state': open(p,'wb').write(json.dumps(v['value'],separators=(',',':')).encode())\n"
            "elif v.get('kind')=='bytes': open(p,'wb').write(base64.b64decode(v['value']))\n"
            "rows=[]\nfor encoded in x['invocations']:\n"
            " q=subprocess.run(['python','-I','-u','-c',\"import runpy,sys;sys.path.insert(0,'/candidate');runpy.run_path('/candidate/main.py',run_name='__main__')\"],input=base64.b64decode(encoded),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8,check=False)\n"
            " rows.append({'code':q.returncode,'out':base64.b64encode(q.stdout).decode(),'err':base64.b64encode(q.stderr).decode()})\n"
            "sys.stdout.write(json.dumps(rows,separators=(',',':'))+'\\n')")]
    else:
        command = ["node", "-e", (
            "const cp=require('node:child_process'),fs=require('node:fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));"
            "if(x.precondition===null){try{fs.unlinkSync(x.state_file)}catch(e){if(e.code!=='ENOENT')throw e}}"
            "else if(x.precondition.kind==='state')fs.writeFileSync(x.state_file,JSON.stringify(x.precondition.value));"
            "else if(x.precondition.kind==='bytes')fs.writeFileSync(x.state_file,Buffer.from(x.precondition.value,'base64'));"
            "const rows=[];for(const encoded of x.invocations){const q=cp.spawnSync('node',['main.js'],{input:Buffer.from(encoded,'base64'),timeout:8000,maxBuffer:1048576});"
            "rows.push({code:q.status===null?-1:q.status,out:(q.stdout||Buffer.alloc(0)).toString('base64'),err:(q.stderr||Buffer.alloc(0)).toString('base64')});}"
            "process.stdout.write(JSON.stringify(rows)+'\\n');")]
    name = "autoforge-stateful-" + uuid.uuid4().hex
    cmd = [engine, "run", "--rm", "--name", name, "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
           "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m", "--cpus=1", "--mount", f"type=bind,src={work},dst=/candidate,readonly",
           "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777", "--workdir", "/candidate", "--interactive", "--entrypoint", command[0], image, *command[1:]]
    try:
        proc = subprocess.run(cmd, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                              check=False, env=container_env.engine_env(), close_fds=True)
    except subprocess.TimeoutExpired:
        return [], b"", -1, True
    if len(proc.stdout) > application_builder.MAX_OUT or len(proc.stderr) > application_builder.MAX_ERR:
        return [], proc.stderr[:application_builder.MAX_ERR], proc.returncode, True
    try:
        rows = json.loads(proc.stdout.decode("utf-8", "strict"))
        output = [{"code": row["code"], "out": base64.b64decode(row["out"], validate=True), "err": base64.b64decode(row["err"], validate=True)} for row in rows]
    except (UnicodeError, ValueError, KeyError, TypeError):
        return [], proc.stderr, proc.returncode, True
    return output, proc.stderr, proc.returncode, False


def evaluate_stateful(store, package_id, candidate, contract_path, *, engine, image, reference_engine=None, reference_image=None):
    """Compare persisted multi-module JS state with a Python candidate across process restarts."""
    package = references._load_package(store, package_id)
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    required = {"schema", "reference_package_id", "license_acknowledged", "reference_entrypoint", "candidate_entrypoint", "state_file", "state_file_max_bytes", "reference_files", "source_observations", "scenarios", "raw_scenarios", "obligations", "independent_expected_outputs"}
    if package.get("kind") != "source" or not package.get("license", "").strip():
        raise ReconstructionHold("stateful reference must be a licensed source package")
    if not isinstance(contract, dict) or set(contract) != required or type(contract.get("schema")) is not int or contract["schema"] != 1:
        raise ReconstructionHold("invalid persisted stateful contract")
    if contract["reference_package_id"] != package_id or contract["license_acknowledged"] is not True or contract["reference_entrypoint"] != "main.js" or contract["candidate_entrypoint"] != "main.py":
        raise ReconstructionHold("persisted contract package/license/entrypoint binding failed")
    state_file = contract["state_file"]
    if not isinstance(state_file, str) or not state_file.startswith("/tmp/") or "/../" in state_file or "\\" in state_file:
        raise ReconstructionHold("state file must be a fixed safe /tmp path")
    limit = contract["state_file_max_bytes"]
    if type(limit) is not int or not 1 <= limit <= application_builder.MAX_LINE:
        raise ReconstructionHold("persisted state byte limit must be positive and bounded")
    if contract["independent_expected_outputs"] is not True:
        raise ReconstructionHold("independent expected outputs must be explicitly acknowledged")
    if (reference_engine is None) != (reference_image is None):
        raise ReconstructionHold("reference engine and image must be supplied together")
    reference_engine, reference_image = reference_engine or engine, reference_image or image
    by_path = {row["path"]: row for row in package["files"]}
    pinned = contract["reference_files"]
    if not isinstance(pinned, dict) or set(pinned) != {"main.js", "ledger.js"}:
        raise ReconstructionHold("contract must bind exactly main.js and ledger.js")
    ref_files = {}
    for path, digest in pinned.items():
        if path not in by_path or by_path[path]["sha256"] != digest:
            raise ReconstructionHold(f"licensed reference module digest mismatch: {path}")
        ref_files[path] = by_path[path]
    observations = contract["source_observations"]
    if not isinstance(observations, list) or len(observations) != 2 or {item.get("path") for item in observations if isinstance(item, dict)} != set(pinned):
        raise ReconstructionHold("per-module source observations are incomplete")
    for row in observations:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "observation"} or row["sha256"] != pinned.get(row["path"]) or not isinstance(row["observation"], str) or not row["observation"].strip():
            raise ReconstructionHold("source observation provenance is invalid")
    obligations = contract["obligations"]
    if not isinstance(obligations, list) or not obligations or any(not isinstance(item, str) or not item.strip() for item in obligations):
        raise ReconstructionHold("nonempty behavioral obligations required")
    declared_scenarios=contract["scenarios"]
    if not isinstance(declared_scenarios,list): raise ReconstructionHold("scenarios must be an array")
    persisted_rows=[row for row in declared_scenarios if isinstance(row,dict) and ("invocations" in row or "precondition" in row or "precondition_state" in row or "precondition_state_bytes" in row)]
    normal_rows=[row for row in declared_scenarios if row not in persisted_rows]
    normal=application_builder._contract({"schema":1,"entrypoint":"main.py","scenarios":normal_rows}) if normal_rows else []
    names = {row["name"] for row in normal}
    def validate_raw(row):
        if not isinstance(row, dict) or set(row) != {"name", "invocations"} or not isinstance(row["name"], str) or not row["name"] or row["name"] in names or len(row["name"]) > 100:
            raise ReconstructionHold("invalid or duplicate raw-input scenario")
        names.add(row["name"])
        invocations = row["invocations"]
        if not isinstance(invocations, list) or not invocations or len(invocations) > application_builder.MAX_REQUESTS:
            raise ReconstructionHold("raw input needs bounded invocation list")
        batches=[];expected_groups=[]
        for invocation in invocations:
            if not isinstance(invocation, dict) or set(invocation) != {"lines", "expected"} or not isinstance(invocation["lines"], list) or not invocation["lines"] or len(invocation["lines"]) > application_builder.MAX_REQUESTS or not isinstance(invocation["expected"], list) or len(invocation["expected"]) != len(invocation["lines"]):
                raise ReconstructionHold("raw invocation lines and expected outputs are malformed")
            lines=invocation["lines"];expected_rows=invocation["expected"]
            if any(not isinstance(line,str) or len(line.encode("utf-8"))>application_builder.MAX_LINE for line in lines):
                raise ReconstructionHold("raw input line is invalid or over the byte limit")
            batches.append(b"\n".join(line.encode("utf-8") for line in lines)+b"\n");expected_groups.append(expected_rows)
        return row["name"],batches,expected_groups,None
    persisted_cases=[];aggregate=0
    for row in persisted_rows:
        if not isinstance(row,dict) or not isinstance(row.get("name"),str) or not row["name"] or row["name"] in names:
            raise ReconstructionHold("invalid or duplicate persisted scenario")
        names.add(row["name"]);precondition=None
        if "precondition_state" in row:
            state=row["precondition_state"]
            if not isinstance(state,dict) or set(state)!={"balance","revision"} or any(type(state[k]) not in (int,float) or state[k]<0 or (type(state[k]) is float and not math.isfinite(state[k])) for k in state): raise ReconstructionHold("invalid persisted precondition")
            precondition={"kind":"state","value":state}
        elif "precondition_state_bytes" in row:
            import base64
            value=b"not-json" if row["precondition_state_bytes"]=="not-json" else b"x"*(limit+1)
            precondition={"kind":"bytes","value":base64.b64encode(value).decode("ascii")}
        else:
            precondition=None
        if "invocations" in row:
            batches=[];expected_groups=[]
            if not isinstance(row["invocations"],list) or not row["invocations"]: raise ReconstructionHold("persisted invocations must be nonempty")
            for invocation in row["invocations"]:
                if not isinstance(invocation,dict) or set(invocation)!={"requests","expected"} or not isinstance(invocation["requests"],list) or not invocation["requests"] or not isinstance(invocation["expected"],list) or len(invocation["requests"])!=len(invocation["expected"]): raise ReconstructionHold("persisted invocation malformed")
                data=b"".join(application_builder._json_bytes(req)+b"\n" for req in invocation["requests"]);aggregate+=len(data);batches.append(data);expected_groups.append(invocation["expected"])
        else:
            requests,expected=row.get("requests"),row.get("expected")
            if not isinstance(requests,list) or not requests or not isinstance(expected,list) or len(requests)!=len(expected): raise ReconstructionHold("persisted requests require expected outputs")
            data=b"".join(application_builder._json_bytes(req)+b"\n" for req in requests);aggregate+=len(data);batches=[data];expected_groups=[expected]
        persisted_cases.append((row["name"],batches,expected_groups,precondition))
    if aggregate>application_builder.MAX_INPUT: raise ReconstructionHold("aggregate persisted input exceeds byte limit")
    raw_scenarios=contract["raw_scenarios"]
    if not isinstance(raw_scenarios,list) or not raw_scenarios or len(raw_scenarios)>application_builder.MAX_SCENARIOS:
        raise ReconstructionHold("malformed-input recovery scenarios must be bounded and nonempty")
    raw_cases=[validate_raw(row) for row in raw_scenarios]
    candidate_root=_regular_chain(Path(candidate))
    if not candidate_root.is_dir(): raise ReconstructionHold("candidate must be a regular staged directory")
    ref_info=application_builder.preflight(reference_engine,reference_image)
    cand_info=application_builder.preflight(engine,image)
    reports={}
    with tempfile.TemporaryDirectory(prefix="autoforge-stateful-reconstruction-") as temp:
        os.chmod(temp,0o755)
        for label,runtime,run_engine,run_image in (("operator_reference","node",reference_engine,reference_image),("candidate","python",engine,image)):
            cases=[(row["name"],[b"".join(application_builder._json_bytes(req)+b"\n" for req in row["requests"])],[row["expected"]],None,"same_process") for row in normal]
            cases.extend((name,batches,expected,precondition,"malformed_recovery") for name,batches,expected,precondition in raw_cases)
            cases.extend((name,batches,expected,precondition,"restart_persistence") for name,batches,expected,precondition in persisted_cases)
            results=[]
            for index,(name,batches,expected_groups,precondition,kind) in enumerate(cases):
                work=Path(temp)/f"{label}-{index}";work.mkdir()
                if label=="candidate": snapshot=_snapshot_candidate(candidate_root,work)
                else:
                    snapshot=[]
                    for rel,row in ref_files.items():
                        target=work.joinpath(*Path(rel).parts);target.parent.mkdir(parents=True,exist_ok=True);data=row["content"].encode("utf-8");target.write_bytes(data);snapshot.append({"path":rel,"sha256":row["sha256"],"size":len(data)})
                calls,stderr,outer_code,timed=_stateful_restart_container(run_engine,run_image,work,batches,20,runtime=runtime,state_file=state_file,precondition=precondition)
                item={"scenario":name,"kind":kind,"process_restarts":len(batches),"outer_exit_code":outer_code,"timed_out_or_overflow":timed,"stderr_sha256":_sha(stderr),"snapshot_files":snapshot};decoded=[]
                try:
                    if len(calls)!=len(batches): raise ValueError("invocation-count mismatch")
                    for call,expected in zip(calls,expected_groups):
                        decoded_call={"exit_code":call["code"],"stdout_sha256":_sha(call["out"]),"stderr_sha256":_sha(call["err"]),"stdout_bytes":len(call["out"]),"behavior_pass":False}
                        if label=="candidate" and call["code"]!=0:
                            decoded_call["failure_locations"]=application_builder._failure_locations(call["err"],work)
                        decoded.append(decoded_call)
                        try:
                            actual=[json.loads(line,object_pairs_hook=application_builder._strict_object) for line in call["out"].decode("utf-8","strict").splitlines()]
                        except (UnicodeError,ValueError,TypeError,KeyError,application_builder.BuildFailure):
                            decoded_call["parse_error"]=True
                            continue
                        decoded_call["response_count_matches"]=len(actual)==len(expected)
                        decoded_call["mismatch_indexes"]=[index for index,(a,b) in enumerate(zip(actual,expected)) if _canon(a)!=_canon(b)][:16]
                        decoded_call["behavior_pass"]=call["code"]==0 and len(actual)==len(expected) and not decoded_call["mismatch_indexes"]
                    item["invocations"]=decoded;item["behavior_pass"]=not timed and outer_code==0 and all(call["behavior_pass"] for call in decoded)
                except (UnicodeError,ValueError,TypeError,KeyError): item["behavior_pass"]=False;item["parse_error"]=True;item["invocations"]=decoded
                results.append(item)
            reports[label]={"behavior_pass":bool(results) and all(row["behavior_pass"] for row in results),"scenarios":results}
    if not reports["operator_reference"]["behavior_pass"]:
        raise ReconstructionHold("independent persistence/recovery scenarios failed against licensed source")
    result={"schema":1,"kind":"isolated-persistent-multimodule-stateful-reconstruction","status":"PASS" if reports["candidate"]["behavior_pass"] else "FAIL",
            "reference":{"package_id":package_id,"origin":package["origin"],"license":package["license"],"source_sha256":_sha(_canon(package)),"files":pinned,"source_observations":observations},
            "candidate":{"path":str(candidate_root.resolve()),"entrypoint":"main.py"},"contract_sha256":_sha(_canon(contract)),"environment":{"operator_reference":ref_info,"candidate":cand_info},
            "reference_run":reports["operator_reference"],"candidate_run":reports["candidate"],"obligations":obligations,
            "limitations":["Persistence is tested across child-process restarts only within one isolated container's ephemeral /tmp; every new scenario container starts clean.","Requests within each child are sequential; no concurrent-writer linearizability or crash-during-write guarantees are claimed.","Malformed recovery, overflow, timeout, and output bounds are tested only for explicit bounded scenarios.","Reference and candidate run in separate exact network-disabled containers with readonly source mounts.",*package["limitations"]]}
    result["sha256"]=_sha(_canon(result));return result




def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store",required=True)
    p.add_argument("--package-id")
    p.add_argument("--evidence-package-id")
    p.add_argument("--reference-package-id")
    p.add_argument("--candidate",required=True)
    p.add_argument("--contract",required=True)
    p.add_argument("--artifact-root")
    p.add_argument("--engine",choices=("docker","podman"),required=True)
    p.add_argument("--reference-engine",choices=("docker","podman"))
    p.add_argument("--reference-image",help="exact local Node image digest; required for REA evidence route")
    p.add_argument("--image",required=True)
    p.add_argument("--stateful",action="store_true")
    p.add_argument("--output",required=True)
    a=p.parse_args(argv)
    try:
        if a.stateful:
            if not a.package_id or a.evidence_package_id or a.reference_package_id or a.artifact_root:
                raise ReconstructionHold("stateful route requires package-id and excludes evidence arguments")
            result=evaluate_stateful(a.store,a.package_id,a.candidate,a.contract,engine=a.engine,image=a.image,reference_engine=a.reference_engine,reference_image=a.reference_image)
        elif a.evidence_package_id or a.reference_package_id or a.artifact_root:
            if not (a.evidence_package_id and a.reference_package_id and a.artifact_root) or a.package_id:
                raise ReconstructionHold("evidence route requires evidence-package-id, reference-package-id, artifact-root and no package-id")
            if not a.reference_engine or not a.reference_image:
                raise ReconstructionHold("REA evidence route requires explicit Node reference engine and image digest")
            result=evaluate_evidence(a.store,a.evidence_package_id,a.reference_package_id,a.candidate,a.contract,a.artifact_root,engine=a.engine,image=a.image,reference_engine=a.reference_engine,reference_image=a.reference_image)
        else:
            if not a.package_id:
                raise ReconstructionHold("original source route requires package-id")
            result=evaluate(a.store,a.package_id,a.candidate,a.contract,engine=a.engine,image=a.image)
        with Path(a.output).open("xb") as stream:stream.write(_canon(result)+b"\n")
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 0 if result["status"]=="PASS" else 1
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({"status":"HOLD","reason":str(exc)}))
        return 2
if __name__=="__main__":raise SystemExit(main())
