#!/usr/bin/env python3
"""Run the real model-generated stateful reconstruction and independent container comparison."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, json, sys
from pathlib import Path
import application_builder, application_proposer
import stateful_reconstruction_live_driver


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--engine", choices=("docker", "podman"), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--node-image", required=True)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--token-budget", type=int, default=500000)
    parser.add_argument("--contract", default=str(Path(__file__).resolve().parents[1] / "fixtures" / "stateful_reference" / "contract.json"))
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if args.prepare_only:
        return stateful_reconstruction_live_driver.main_from_args(args)
    try:
        application_builder.preflight(args.engine, args.image)
        application_builder.preflight(args.engine, args.node_image)
    except (OSError, ValueError, application_builder.BuildFailure, application_builder.MissingPrerequisite) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc), "required": "Linux Docker/Podman engine and exact local Python/Node image IDs"}, ensure_ascii=False))
        return 3
    model = application_proposer.check_config()
    if not model["endpoint_valid"] or not model["transport_allowed"]:
        print(json.dumps({"status": "BLOCKED", "reason": "application_build model endpoint is not configured or transport is disallowed; set AUTOFORGE_API_BASE/AUTOFORGE_MODEL and permitted credentials", "model_configuration": model}, ensure_ascii=False))
        return 3
    return stateful_reconstruction_live_driver.main_from_args(args)


if __name__ == "__main__":
    raise SystemExit(main())
