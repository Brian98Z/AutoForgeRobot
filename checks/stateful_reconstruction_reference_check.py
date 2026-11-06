#!/usr/bin/env python3
"""Prove the authored persisted-stateful reference and evaluator agree; negative candidate is deliberately wrong."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, json, shutil, tempfile
from pathlib import Path
import references, reference_reconstruction, application_builder

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/"fixtures"/"stateful_reference"


def main(argv=None) -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store",required=True)
    p.add_argument("--engine",choices=("docker","podman"),required=True)
    p.add_argument("--image",required=True,help="exact local Python image sha256 digest")
    p.add_argument("--node-image",required=True,help="exact local Node image sha256 digest")
    args=p.parse_args(argv)
    store=Path(args.store)
    try:
        with tempfile.TemporaryDirectory(prefix="autoforge-stateful-reference-check-") as source_tmp:
            src=Path(source_tmp)
            for name in ("main.js","ledger.js"):
                shutil.copyfile(FIXTURE/name,src/name)
            package=references.ingest(src,store,"AutoForge stateful reconstruction fixture authored for this task","MIT",kind="source")
        contract=json.loads((FIXTURE/"contract.json").read_text(encoding="utf-8"))
        contract["reference_package_id"]=package["package_id"]
        with tempfile.TemporaryDirectory(prefix="autoforge-stateful-negative-") as cand_tmp, tempfile.TemporaryDirectory(prefix="autoforge-stateful-contract-") as contract_tmp:
            candidate=Path(cand_tmp); (candidate/"main.py").write_text("import json,sys\nfor line in sys.stdin: print(json.dumps({'error':'intentionally-wrong-negative-probe'}))\n",encoding="utf-8")
            cp=Path(contract_tmp)/"contract.json";cp.write_text(json.dumps(contract,ensure_ascii=False),encoding="utf-8")
            result=reference_reconstruction.evaluate_stateful(store,package["package_id"],candidate,cp,engine=args.engine,image=args.image,reference_engine=args.engine,reference_image=args.node_image)
        reference_pass=result["reference_run"]["behavior_pass"]
        candidate_rejected=result["status"]=="FAIL" and not result["candidate_run"]["behavior_pass"]
        if not reference_pass or not candidate_rejected:
            raise RuntimeError("authored reference failed an independent oracle scenario or evaluator accepted deliberately wrong candidate")
        print(json.dumps({"status":"PASS","reference_package_id":package["package_id"],"license":"MIT","reference_module_hashes":contract["reference_files"],"reference_scenarios":len(result["reference_run"]["scenarios"]),"reference_behavior_pass":reference_pass,"negative_candidate_rejected":candidate_rejected,"runtime":{"python":result["environment"]["candidate"],"node":result["environment"]["operator_reference"]},"limitations":result["limitations"]},ensure_ascii=False,indent=2))
        return 0
    except (OSError,ValueError,RuntimeError,application_builder.BuildFailure,application_builder.MissingPrerequisite,reference_reconstruction.ReconstructionHold) as exc:
        print(json.dumps({"status":"HOLD","reason":str(exc)},ensure_ascii=False),flush=True)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
