"""Preflight and run the built-in model/container application pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import applications
import application_builder as builder
import application_proposer as proposer
from application_checkpoint import regular_chain

ROOT = Path(__file__).resolve().parent


def inspect(request_path: Path, contract_path: Path, reference_store: Path,
            engine: str, image: str, output: Path | None = None, resume: bool = False) -> tuple[dict, dict | None]:
    """Read local inputs and engine metadata only; never contact the model."""
    checks = {}; blockers = []; request = None
    try:
        regular_chain(request_path); regular_chain(contract_path)
        request = applications._request_payload(builder._load_json(request_path, applications.MAX_REQUEST_BYTES))
        references = applications._resolve_references(reference_store, request['references'])
        contract = builder._load_json(contract_path, builder.MAX_INPUT)
        scenarios = builder._contract(contract)
        # The oracle must not be handed to the proposer as a reference document.
        oracle_bytes = contract_path.read_bytes()
        oracle_digest = hashlib.sha256(oracle_bytes).hexdigest()
        if any(hashlib.sha256(f['content'].encode('utf-8')).hexdigest() == oracle_digest
               for package in references for f in package['files']):
            raise ValueError('business contract is also a public reference; refusing oracle disclosure')
        checks['inputs'] = {'status':'PASS', 'scenarios':len(scenarios),
                            'requests':sum(len(s['requests']) for s in scenarios),
                            'contract_sha256':oracle_digest}
    except (OSError, ValueError, builder.BuildFailure) as exc:
        checks['inputs'] = {'status':'BLOCKED', 'reason':str(exc)}
        blockers.append('inputs')
    if output is not None:
        try:
            regular_chain(output)
            if contract_path.resolve().is_relative_to(output.resolve()):
                raise ValueError('contract must be outside the application output directory')
            if resume:
                if not output.is_dir():raise ValueError('resume output must already exist')
                from application_checkpoint import load
                load(output/'cycle-state.json')
            else:
                applications._check_absent_directory(output)
            checks['output'] = {'status':'PASS'}
        except (OSError, ValueError) as exc:
            checks['output'] = {'status':'BLOCKED','reason':str(exc)};blockers.append('output')
    model = proposer.check_config()
    checks['model_configuration'] = model
    if not model['endpoint_valid']:blockers.append('model_configuration')
    try:
        checks['container'] = {'status':'PASS', **builder.preflight(engine,image)}
    except (OSError, ValueError, builder.BuildFailure, builder.MissingPrerequisite) as exc:
        checks['container'] = {'status':'BLOCKED','reason':str(exc)};blockers.append('container')
    return {'schema':1,'status':'READY' if not blockers else 'BLOCKED','blockers':blockers,
            'checks':checks,'model_contacted':False,'candidate_executed':False,
            'limitations':['Readiness is a point-in-time check, not a sandbox or business correctness certification.',
                           'Model connectivity and image Python availability are not exercised by preflight.',
                           'Oracle disclosure check rejects exact file copies only, not paraphrased or manually embedded answers.']},request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','run'))
    parser.add_argument('--request',type=Path,required=True)
    parser.add_argument('--contract',type=Path,required=True)
    parser.add_argument('--reference-store',type=Path,required=True)
    parser.add_argument('--engine',choices=('docker','podman'),default='docker')
    parser.add_argument('--image',required=True,help='exact local sha256 image ID; no image is pulled')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--rounds',type=int,choices=range(1,9),default=3)
    parser.add_argument('--mode',choices=('agent','proposal'),default='agent',help='agent uses native tool calls; proposal retains whole-candidate cycle')
    parser.add_argument('--max-turns',type=int,choices=range(1,13),default=12)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--max-new-rounds',type=int)
    parser.add_argument('--allow-model',action='store_true',help='authorize sending public context/source to configured model; may incur charges')
    parser.add_argument('--trusted-adapter',action='store_true',help='authorize the built-in container adapter and operator-selected image')
    args=parser.parse_args()
    try:
        if args.max_new_rounds is not None and args.max_new_rounds<1:raise ValueError('max-new-rounds must be positive')
        if args.action=='run' and (not args.allow_model or not args.trusted_adapter or args.output is None):
            raise ValueError('run requires --output, --allow-model and --trusted-adapter')
        if args.resume and args.output is None:raise ValueError('resume requires output')
        if args.mode=='agent' and (args.resume or args.max_new_rounds is not None):raise ValueError('agent mode does not yet support resume or partial-round continuation; use a fresh output')
        report,request=inspect(args.request,args.contract,args.reference_store,args.engine,args.image,args.output,args.resume)
        if args.action=='check' or report['status']!='READY':
            print(json.dumps(report,ensure_ascii=True,indent=2));return 0 if report['status']=='READY' else 3
        # Recheck oracle bytes before passing its absolute path to the trusted builder.
        if hashlib.sha256(args.contract.read_bytes()).hexdigest()!=report['checks']['inputs']['contract_sha256']:
            raise ValueError('contract changed after preflight')
        proposal=[sys.executable,str(ROOT/'application_proposer.py')]
        build=[sys.executable,str(ROOT/'application_builder.py'),'--engine',args.engine,'--image',args.image,
               '--contract',str(args.contract.resolve())]
        if args.mode=='agent':
            import agent_runtime
            result=agent_runtime.run(request,applications._resolve_references(args.reference_store,request['references']),build,args.output,args.max_turns)
        else:
            result=applications.cycle(request,proposal,build,args.output,args.rounds,True,args.reference_store,args.resume,args.max_new_rounds)
        print(json.dumps(result,ensure_ascii=True,indent=2))
        return 0 if result['status']=='PASS' else 2
    except (OSError, ValueError, builder.BuildFailure) as exc:
        print(json.dumps({'status':'HOLD','reason':str(exc)},ensure_ascii=True),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
