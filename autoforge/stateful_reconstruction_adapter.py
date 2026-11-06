#!/usr/bin/env python3
"""Trusted builder adapter for full persisted-stateful reconstruction acceptance."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import application_builder, applications, reference_reconstruction


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store",required=True)
    parser.add_argument("--package-id",required=True)
    parser.add_argument("--contract",required=True)
    parser.add_argument("--engine",choices=("docker","podman"),required=True)
    parser.add_argument("--image",required=True)
    parser.add_argument("--node-image",required=True)
    args=parser.parse_args(argv)
    try:
        envelope=application_builder._strict_loads(sys.stdin.buffer.read(application_builder.MAX_INPUT+1))
        if not isinstance(envelope,dict) or set(envelope)!={"schema","workspace_manifest","candidate_sha256"} or envelope["schema"]!=1:
            raise ValueError("expected exact staged workspace manifest envelope")
        manifest=envelope["workspace_manifest"]
        if not isinstance(manifest,dict): raise ValueError("workspace manifest missing")
        root,digest=application_builder._manifest(manifest)
        if digest!=envelope["candidate_sha256"]: raise ValueError("candidate digest differs from staged manifest")
        ref_info=application_builder.preflight(args.engine,args.node_image)
        cand_info=application_builder.preflight(args.engine,args.image)
        report=reference_reconstruction.evaluate_stateful(args.store,args.package_id,root,args.contract,
            engine=args.engine,image=args.image,reference_engine=args.engine,reference_image=args.node_image)
        passed=report["status"]=="PASS"
        observations=[]
        for label,key in (("reference","reference_run"),("candidate","candidate_run")):
            for row in report[key]["scenarios"]:
                observations.append(f"{label} {row['scenario']}: {'PASS' if row['behavior_pass'] else 'FAIL'}")
                if label!="candidate" or row["behavior_pass"]:
                    continue
                observations.append(f"candidate {row['scenario']}: outer_exit={row['outer_exit_code']} timed_out_or_overflow={row['timed_out_or_overflow']} parse_error={row.get('parse_error',False)}")
                for index,call in enumerate(row.get("invocations",[])):
                    if not call["behavior_pass"]:
                        observations.append(f"candidate {row['scenario']} invocation {index}: exit={call['exit_code']} parse_error={call.get('parse_error',False)} response_count_matches={call.get('response_count_matches','unknown')} mismatch_indexes={call.get('mismatch_indexes',[])} locations={','.join(call.get('failure_locations',[]))}")
        value={"schema":1,"candidate_sha256":digest,"build_pass":True,"test_pass":passed,
               "observations":observations[:128],"limitations":report["limitations"][:128]}
        print(json.dumps(value,ensure_ascii=False,separators=(",",":")))
        return 0
    except (OSError,ValueError,KeyError,application_builder.BuildFailure,application_builder.MissingPrerequisite,reference_reconstruction.ReconstructionHold) as exc:
        digest=envelope.get("candidate_sha256") if isinstance(locals().get("envelope"),dict) else "0"*64
        value={"schema":1,"candidate_sha256":digest,"build_pass":False,"test_pass":False,
               "observations":[f"stateful acceptance blocked: {type(exc).__name__}: {str(exc)[:500]}"],
               "limitations":["Full stateful acceptance was blocked; no candidate was certified."]}
        print(json.dumps(value,ensure_ascii=False,separators=(",",":")))
        return 0


if __name__=="__main__":
    raise SystemExit(main())
