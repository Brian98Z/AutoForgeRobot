#!/usr/bin/env python3
"""Exercise the real manifest->trusted stateful adapter path with an intentionally wrong candidate."""
from __future__ import annotations
import argparse, json, sys, tempfile
from pathlib import Path
import application_builder, applications, references, reference_reconstruction

ROOT=Path(__file__).resolve().parent
FIXTURE=ROOT/"fixtures"/"stateful_reference"


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store",required=True)
    p.add_argument("--engine",choices=("docker","podman"),required=True)
    p.add_argument("--image",required=True)
    p.add_argument("--node-image",required=True)
    args=p.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="autoforge-stateful-adapter-check-") as temp:
        root=Path(temp)
        # The isolated Python child must resolve staged sibling modules while
        # retaining -I and the container's read-only/network-disabled boundary.
        modules=root/'modules';modules.mkdir()
        (modules/'balance.py').write_text("import json\nfrom pathlib import Path\np=Path('/tmp/autoforge-stateful-ledger.json')\ndef change(delta):\n try: value=json.loads(p.read_text())['balance']\n except FileNotFoundError: value=0\n value+=delta\n p.write_text(json.dumps({'balance':value}))\n return value\n",encoding='utf-8')
        (modules/'main.py').write_text("import json,sys\nfrom balance import change\nfor line in sys.stdin: print(json.dumps({'balance':change(json.loads(line)['delta'])}))\n",encoding='utf-8')
        calls,stderr,code,timed=reference_reconstruction._stateful_restart_container(
            args.engine,args.image,modules,[b'{"delta":3}\n',b'{"delta":-1}\n'],20,
            runtime='python',state_file='/tmp/autoforge-stateful-ledger.json')
        if timed or code!=0 or len(calls)!=2 or any(row['code']!=0 for row in calls):
            raise AssertionError('isolated sibling import/restart failed: '+repr((calls,stderr)))
        if [json.loads(row['out']) for row in calls]!=[{'balance':3},{'balance':2}]:
            raise AssertionError('sibling-module balance did not persist across fresh processes')
        source=root/"source";source.mkdir()
        for name in ("main.js","ledger.js"): (source/name).write_bytes((FIXTURE/name).read_bytes())
        pkg=references.ingest(source,args.store,"AutoForge stateful reconstruction fixture authored for this task","MIT",kind="source")
        contract=json.loads((FIXTURE/"contract.json").read_text(encoding="utf-8"));contract["reference_package_id"]=pkg["package_id"]
        contract_path=root/"contract.json";contract_path.write_text(json.dumps(contract,ensure_ascii=False),encoding="utf-8")
        wrong={"schema":1,"goal":"intentional adapter negative probe","references":[pkg["package_id"]],
               "files":[{"path":"main.py","content":"import json,sys\nfor line in sys.stdin: print(json.dumps({'wrong':True}))\n"}]}
        stage=applications.stage_candidate(wrong,root/"candidate")
        manifest=applications._read_json(Path(stage["manifest"]),application_builder.MAX_INPUT)
        envelope={"schema":1,"workspace_manifest":manifest,"candidate_sha256":stage["candidate_sha256"]}
        argv=[sys.executable,str(ROOT/"stateful_reconstruction_adapter.py"),"--store",str(Path(args.store).resolve()),
              "--package-id",pkg["package_id"],"--contract",str(contract_path),"--engine",args.engine,
              "--image",args.image,"--node-image",args.node_image]
        payload=applications._run_json_command(argv,envelope)
        if not isinstance(payload,dict) or set(payload)!={"schema","candidate_sha256","build_pass","test_pass","observations","limitations"}:
            raise AssertionError("trusted executor did not receive the exact adapter report")
        if payload["candidate_sha256"]!=stage["candidate_sha256"] or payload["build_pass"] is not True or payload["test_pass"] is not False:
            raise AssertionError("known behavioral FAIL was not returned as a completed digest-bound adapter result: "+json.dumps(payload,ensure_ascii=False))
        if not any("persistent-credit-debit-and-restart" in row for row in payload["observations"]):
            raise AssertionError("full persisted-state contract scenarios were not exercised")
        if not any('candidate ' in row and 'mismatch_indexes=' in row for row in payload['observations']):
            raise AssertionError('candidate response mismatch diagnostics were lost')
        # Candidate messages can echo hidden inputs: expose coordinates/classes,
        # never raw traceback messages or response payloads.
        crash={**wrong,'files':[{'path':'main.py','content':"raise ValueError('HIDDEN_STATE_PAYLOAD_DO_NOT_EXPOSE')\n"}]}
        crash_stage=applications.stage_candidate(crash,root/'crashing-candidate')
        crash_manifest=applications._read_json(Path(crash_stage['manifest']),application_builder.MAX_INPUT)
        crash_payload=applications._run_json_command(argv,{'schema':1,'workspace_manifest':crash_manifest,'candidate_sha256':crash_stage['candidate_sha256']})
        if crash_payload['test_pass'] is not False or not any('main.py:1' in row and 'ValueError' in row and 'exit=1' in row for row in crash_payload['observations']):
            raise AssertionError('crash class and candidate source coordinates were not preserved')
        if 'HIDDEN_STATE_PAYLOAD_DO_NOT_EXPOSE' in json.dumps(crash_payload):
            raise AssertionError('candidate exception message leaked through acceptance feedback')
        duplicate={**wrong,'files':[{'path':'main.py','content':"import sys\nfor line in sys.stdin: print('{\"balance\":0,\"balance\":1}')\n"}]}
        duplicate_stage=applications.stage_candidate(duplicate,root/'duplicate-output-candidate')
        duplicate_manifest=applications._read_json(Path(duplicate_stage['manifest']),application_builder.MAX_INPUT)
        duplicate_payload=applications._run_json_command(argv,{'schema':1,'workspace_manifest':duplicate_manifest,'candidate_sha256':duplicate_stage['candidate_sha256']})
        if duplicate_payload['build_pass'] is not True or duplicate_payload['test_pass'] is not False or not any('invocation ' in row and 'parse_error=True' in row for row in duplicate_payload['observations']):
            raise AssertionError('duplicate-key output must be rejected as completed parse failure, not a blocked adapter')
        print(json.dumps({"status":"PASS","adapter_exit":"completed_report","candidate_sha256":stage["candidate_sha256"],
                          "build_pass":payload["build_pass"],"test_pass":payload["test_pass"],
                          "scenario_observations":payload["observations"]},ensure_ascii=False,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
