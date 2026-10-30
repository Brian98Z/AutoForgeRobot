"""Run an identical strict JSON corpus through Python and Julia providers.

Writes durable parity evidence under protocol-evidence/; set JULIA to override
its executable. This is an operator check, not a test-suite side effect.
"""
import json
import os
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parent
BASE = {"schema": 1, "task": {"id": "parity", "goal": "unicode ☃", "public_cases": [{"input": [1, 2], "expected": 3}]}, "round": 1, "baseline_wat": '(module (func (export "solve") (param $x i64) (param $y i64) (result i64) (i64.add (local.get $x) (local.get $y))))', "feedback": [{"input": [1, 2], "expected": 3, "actual": None, "error": None}, {"error": "compile failed"}], "rejected_wats": ["α"]}

def compact(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

def corpus():
    good = [compact(BASE), compact({**BASE, "task": {**BASE["task"], "goal": "𝄞 café / 日本語"}, "rejected_wats": ["x"] * 8}), compact(BASE).replace("unicode ☃", "valid\\u0001control")]
    good.append(compact({**BASE,'feedback':[{'input':[1,2],'expected':3,'actual':-(1<<63),'error':None}]}))
    import hashlib
    content='(module (func (export "solve") (param i64 i64) (result i64) (i64.add (local.get 0) (local.get 1))))'
    ref={'id':'owned','kind':'wat','origin':'local','license':'owned','content':content,'content_sha256':hashlib.sha256(content.encode()).hexdigest()}
    good.append(compact({**BASE,'task':{**BASE['task'],'references':[ref]}}))
    bad = [
        compact({**BASE, "unknown": 1}),
        compact({**BASE, "task": {**BASE["task"], "unknown": 1}}),
        compact({**BASE, "task": {**BASE["task"], "public_cases": [{"input": [1, 2], "expected": 3, "x": 0}]}}),
        compact({**BASE, "feedback": [{"error": "x", "extra": 1}]}),
        compact({**BASE, "feedback": [{"input": [1, 2], "expected": 3, "actual": 1.5}]}),
        compact({**BASE, "feedback": [{"input": [1, 2], "expected": 3, "actual": 1e3}]}),
        compact({**BASE, "feedback": [{"input": [1, 2], "expected": 3, "actual": 1 << 63}]}),
        compact({**BASE, "rejected_wats": ["x"] * 9}),
        compact({**BASE, "task": {**BASE["task"], "public_cases": [{"input": [True, 2], "expected": 3}]}}),
        compact(BASE) + " {}",
        compact(BASE).replace('"schema":1', '"schema":1,"\\u0073chema":1'),
        compact(BASE).replace('"schema":1', '"schema":1,"schema":1'),
        compact(BASE).replace('☃', '\\ud800'),
        b'{"schema":1}\xff',
        compact(BASE).replace('unicode ☃','raw\x01control'),
        compact(BASE)+',{}',
        compact(BASE)+' true',
        compact(BASE).replace('"input":[1,2]','"input":[1,2],"in\\u0070ut":[1,2]'),
        compact({**BASE,'task':{**BASE['task'],'public_cases':[{'input':[1,2,3],'expected':3}]}}),
        compact({**BASE,'feedback':[{'input':[1,2],'expected':3,'actual':True}]}),
    ]
    bad.extend(compact({**BASE,'task':{**BASE['task'],'references':refs}}) for refs in ([{**ref,'content_sha256':'0'*64}], [ref]*5, [{**ref,'kind':'shell'}], [{**ref,'extra':True}]))
    return [("valid", x) for x in good] + [("invalid", x) for x in bad]

def invoke(command, payload):
    try:
        p = subprocess.run(command, input=payload.encode("utf-8") if isinstance(payload, str) else payload, capture_output=True, timeout=30)
        return {"accepted": p.returncode == 0, "returncode": p.returncode,
                "stdout": p.stdout.decode("utf-8", "replace"), "stderr": p.stderr.decode("utf-8", "replace")}
    except Exception as exc:
        return {"error": repr(exc)}

def main():
    julia = os.environ.get('JULIA',str(ROOT/'runtime/julia-1.13.3/bin/julia.exe'))
    julia_project = ROOT / "providers"
    commands = {"python": [os.environ.get("PYTHON", __import__("sys").executable), str(ROOT / "providers/python_agent.py")],
                "julia": [julia, "--startup-file=no", "--project=" + str(julia_project), str(ROOT / "providers/julia_agent.jl")]}
    evidence = {"run_id": uuid.uuid4().hex, "cases": []}
    for index, (expected, payload) in enumerate(corpus()):
        row = {"index": index, "expected": expected, "payload": payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload}
        for provider, command in commands.items():
            result = invoke(command, payload)
            result['matches_expectation'] = result.get('returncode') == (0 if expected=='valid' else 2) and (expected=='valid' or not result.get('stdout'))
            row[provider] = result
        row["parity"] = row["python"].get("accepted") == row["julia"].get("accepted")
        evidence["cases"].append(row)
    evidence["status"] = "PASS" if all(c["parity"] and c["python"]["matches_expectation"] and c["julia"]["matches_expectation"] for c in evidence["cases"]) else "FAIL"
    outdir = ROOT / 'bootstrap-runs' / 'protocol-evidence'
    outdir.mkdir(exist_ok=True)
    path = outdir / (evidence["run_id"] + ".json")
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": evidence["status"], "evidence": str(path)}, ensure_ascii=False))
    if evidence["status"] != "PASS": raise SystemExit(1)

if __name__ == "__main__":
    main()
