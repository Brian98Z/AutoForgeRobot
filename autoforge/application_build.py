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

ROOT = Path(__file__).resolve().parents[1]
# Sibling modules live beside this file. The frozen dispatcher resolves a script by basename,
# so an exact source path works both frozen and from source; ROOT/'x.py' only worked frozen.
MODULES = Path(__file__).resolve().parent


def inspect(request_path: Path, contract_path: Path, reference_store: Path,
            engine: str, image: str, output: Path | None = None, resume: bool = False,
            adapter_kind: str = 'jsonl') -> tuple[dict, dict | None]:
    """Read local inputs and engine metadata only; never contact the model."""
    checks = {}; blockers = []; request = None
    try:
        regular_chain(request_path); regular_chain(contract_path)
        request = applications._request_payload(builder._load_json(request_path, applications.MAX_REQUEST_BYTES))
        references = applications._resolve_references(reference_store, request['references'])
        contract = builder._load_json(contract_path, builder.MAX_INPUT)
        if adapter_kind == 'http':
            import application_service
            scenarios = application_service._contract(contract)
        elif adapter_kind == 'jsonl':
            scenarios = builder._contract(contract)
        else:
            raise ValueError('unsupported application adapter')
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
                if (output/'session.json').is_file():
                    from agent_session import load
                    load(output)
                else:
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
    parser.add_argument('--max-turns',type=int,choices=range(1,1001),default=12)
    parser.add_argument('--action-profile', choices=('full', 'small'), default='small', help='small exposes create-only plus bounded exact edits, avoiding nullable whole-file replacement')
    parser.add_argument('--token-budget',type=int,default=1_000_000)
    parser.add_argument('--time-budget',type=float,default=600)
    parser.add_argument('--tool-budget',type=int,default=96)
    parser.add_argument('--cost-budget-microunits',type=int,default=0)
    parser.add_argument('--input-price-microunits-per-million',type=int,default=0)
    parser.add_argument('--output-price-microunits-per-million',type=int,default=0)
    parser.add_argument('--project',type=Path,help='import existing project into candidate memory; never overwrites source')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--max-new-rounds',type=int)
    parser.add_argument('--allow-model',action='store_true',help='authorize sending public context/source to configured model; may incur charges')
    parser.add_argument('--trusted-adapter',action='store_true',help='authorize the built-in container adapter and operator-selected image')
    parser.add_argument('--adapter-kind',choices=('jsonl','http'),default='jsonl')
    parser.add_argument('--service-command',help='trusted JSON argv, executed inside container only')
    parser.add_argument('--secret-mounts',type=Path,help='restricted JSON schema-1 secret mount descriptors; contents remain controller-private')
    parser.add_argument('--browser-contract',type=Path,help='independent browser acceptance contract for HTTP adapter')
    parser.add_argument('--browser-output',type=Path,help='new directory for actual browser screenshots')
    parser.add_argument('--authorize-browser',action='store_true')
    parser.add_argument('--validation-kind',choices=('public','heldout'),default='public')
    parser.add_argument('--oracle-ledger',type=Path)
    parser.add_argument('--max-oracle-queries',type=int)
    parser.add_argument('--stateful-package-id', help='licensed reference package ID for the persisted-stateful reconstruction adapter')
    parser.add_argument('--stateful-contract', type=Path, help='independent persisted-stateful contract bound to the licensed reference')
    parser.add_argument('--node-image', help='exact local Node image ID for stateful reference execution')
    args=parser.parse_args()
    try:
        if args.max_new_rounds is not None and args.max_new_rounds<1:raise ValueError('max-new-rounds must be positive')
        if args.action=='run' and (not args.allow_model or not args.trusted_adapter or args.output is None):
            raise ValueError('run requires --output, --allow-model and --trusted-adapter')
        if args.validation_kind=='heldout' and (args.action!='run' or args.mode!='agent' or args.oracle_ledger is None or args.max_oracle_queries is None):
            raise ValueError('heldout validation requires run --mode agent, --oracle-ledger and explicit --max-oracle-queries')
        if args.validation_kind=='heldout' and not 1 <= args.max_oracle_queries <= 10000:
            raise ValueError('max-oracle-queries must be in 1..10000')
        if args.validation_kind=='public' and (args.oracle_ledger is not None or args.max_oracle_queries is not None):
            raise ValueError('oracle ledger and cap are only valid with --validation-kind heldout')
        if args.resume and args.output is None:raise ValueError('resume requires output')
        if args.mode=='agent' and args.max_new_rounds is not None:raise ValueError('agent uses --max-turns and durable --resume, not partial proposal rounds')
        if args.project is not None and (args.mode!='agent' or args.resume):raise ValueError('--project requires fresh agent mode')
        if args.adapter_kind == 'http' and not args.service_command:
            raise ValueError('HTTP adapter requires --service-command JSON argv')
        if args.service_command:
            command=json.loads(args.service_command)
            if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x for x in command):
                raise ValueError('service command must be nonempty JSON argv')
        if args.browser_contract is not None:
            if args.adapter_kind != 'http' or not args.authorize_browser or args.browser_output is None:
                raise ValueError('browser acceptance requires HTTP adapter, --authorize-browser and --browser-output')
            import application_browser
            regular_chain(args.browser_contract)
            application_browser._contract(builder._load_json(args.browser_contract,builder.MAX_INPUT))
            if args.browser_output.exists():
                raise ValueError('browser output must be absent')
        secret_mounts=[]
        if args.secret_mounts is not None:
            if args.project is not None and (args.secret_mounts.resolve()==args.project.resolve() or args.project.resolve() in args.secret_mounts.resolve().parents or args.secret_mounts.resolve() in args.project.resolve().parents): raise ValueError('secret manifest must be outside candidate project tree')
            regular_chain(args.secret_mounts)
            secret_data=builder._load_json(args.secret_mounts,builder.MAX_INPUT)
            if not isinstance(secret_data,dict) or set(secret_data)!={'schema','mounts'} or type(secret_data['schema']) is not int or secret_data['schema']!=1 or not isinstance(secret_data['mounts'],list) or not 1<=len(secret_data['mounts'])<=32: raise ValueError('secret mounts must use schema 1 with 1..32 descriptors')
            names=set()
            for item in secret_data['mounts']:
                if not isinstance(item,dict) or set(item)!={'source','target','read_only','name','sha256'} or item['read_only'] is not True or not isinstance(item['name'],str) or not __import__('re').fullmatch(r'[A-Z][A-Z0-9_]{0,63}',item['name']) or item['name'] in names or item['target']!='/run/autoforge/secrets/'+item['name']: raise ValueError('secret mount descriptor invalid')
                regular_chain(Path(item['source'])); source=Path(item['source'])
                if args.output is not None:
                    try: source.resolve().relative_to(args.output.resolve())
                    except ValueError: pass
                    else: raise ValueError('secret source must be outside model session output')
                if args.project is not None:
                    try: source.resolve().relative_to(args.project.resolve())
                    except ValueError: pass
                    else: raise ValueError('secret source must be outside candidate source tree')
                names.add(item['name']);secret_mounts.append(item)
        referenced_names=set()
        if args.browser_contract is not None:
            browser_data=builder._load_json(args.browser_contract,builder.MAX_INPUT)
            referenced_names.update(__import__('re').findall(r'\{\$secret_file:([A-Z][A-Z0-9_]{0,63})\}',json.dumps(browser_data,ensure_ascii=False)))
        if args.secret_mounts is not None:
            descriptor_names={item['name'] for item in secret_mounts}
            if not referenced_names<=descriptor_names: raise ValueError('browser contract requires unmounted private secret names')
        elif referenced_names: raise ValueError('browser contract requires a private secret mount manifest')
        report,request=inspect(args.request,args.contract,args.reference_store,args.engine,args.image,args.output,args.resume,args.adapter_kind)
        if args.action=='check' or report['status']!='READY':
            print(json.dumps(report,ensure_ascii=True,indent=2));return 0 if report['status']=='READY' else 3
        # Recheck oracle bytes before passing its absolute path to the trusted builder.
        if hashlib.sha256(args.contract.read_bytes()).hexdigest()!=report['checks']['inputs']['contract_sha256']:
            raise ValueError('contract changed after preflight')
        proposal=[sys.executable,str(MODULES/'application_proposer.py')]
        build=[sys.executable,str(MODULES/'application_builder.py'),'--engine',args.engine,'--image',args.image,
               '--contract',str(args.contract.resolve())]
        if args.adapter_kind == 'http':
            build=[sys.executable,str(MODULES/'application_service.py'),'--adapter','--engine',args.engine,
                   '--image',args.image,'--contract',str(args.contract.resolve()),'--command-json',args.service_command]
            if args.browser_contract is not None:
                build += ['--browser-contract',str(args.browser_contract.resolve()),'--browser-output',str(args.browser_output.resolve()),'--authorize-browser']
            if secret_mounts:
                build += ['--secret-mounts',str(args.secret_mounts.resolve())]
        if args.validation_kind=='heldout':
            ledger=args.oracle_ledger.resolve()
            protected=[args.output.resolve(),args.request.resolve(),args.contract.resolve(),args.reference_store.resolve()]
            if args.project is not None:
                protected.append(args.project.resolve())
            if any(ledger==p or ledger in p.parents or p in ledger.parents for p in protected):
                raise ValueError('oracle ledger must be separate from candidate/output and public inputs')
            build=[sys.executable,str(MODULES/'application_oracle.py'),'--ledger',str(ledger),'--contract',str(args.contract.resolve()),
                   '--contract-sha256',report['checks']['inputs']['contract_sha256'],
                   '--max-queries',str(args.max_oracle_queries),'--builder-argv',json.dumps(build,separators=(',',':'))]
        if args.mode=='agent':
            import agent_runtime
            import agent_budget
            initial=None
            if args.project is not None:
                from application_project import import_project
                initial=import_project(args.project,goal=request['goal'],references=request['references'])['candidate']
            limits=agent_budget.Limits(model_calls=args.max_turns,tool_calls=args.tool_budget,tokens=args.token_budget,seconds=args.time_budget,
                cost_microunits=args.cost_budget_microunits,input_microunits_per_million=args.input_price_microunits_per_million,
                output_microunits_per_million=args.output_price_microunits_per_million)
            result=agent_runtime.run(request,applications._resolve_references(args.reference_store,request['references']),build,args.output,args.max_turns,
                resume=args.resume,limits=limits,initial_candidate=initial,
                allowed_tools=['list_files','read_file','create_file','edit_file','search_files','read_reference','search_references','validate'] if args.action_profile == 'small' else None)
        else:
            result=applications.cycle(request,proposal,build,args.output,args.rounds,True,args.reference_store,args.resume,args.max_new_rounds)
        print(json.dumps(result,ensure_ascii=True,indent=2))
        return 0 if result['status']=='PASS' else 2
    except (OSError, ValueError, builder.BuildFailure) as exc:
        print(json.dumps({'status':'HOLD','reason':str(exc)},ensure_ascii=True),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
