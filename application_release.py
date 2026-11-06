"""Application release gate and local delivery orchestration.

The gate validates candidate-bound governance and measurement evidence; it never
executes candidate code. Deployment's health argv is a separately trusted operator
adapter, exactly as required by application_delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import application_builder as builder
import application_delivery as delivery
import application_dependencies as dependencies
import application_evaluation as evaluation
import application_quality as quality
import application_spec as spec_module

class ReleaseRefused(ValueError):
    pass

def _load(path: Path) -> Any:
    raw = Path(path).read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ReleaseRefused(f"input too large: {path}")
    return json.loads(raw.decode("utf-8"))

def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")).hexdigest()

def preflight(candidate_dir: Path, approved_spec: dict[str,Any], quality_policy: dict[str,Any],
              quality_records: list[dict[str,Any]], dual_evidence: dict[str,Any],
              dependency_manifest: dict[str,Any], deployment_authorization: dict[str,Any],
              deployment_root: Path, *, engine: str, image: str,
              authorized_na_reasons=()) -> dict[str,Any]:
    """Return sealed PASS only if every governance, evidence, scope and env gate passes.

    dual_evidence is {schema:1,candidate_sha256,spec_sha256,proposal:{...},validator:{...}};
    each side has application_evaluation.reconcile_evidence's exact typed shape.
    """
    blockers=[]; checks={}
    try:
        candidate, files, digest = delivery._read_candidate(Path(candidate_dir).absolute())
        checks['candidate']={'status':'PASS','candidate_sha256':digest}
    except Exception as exc:
        raise ReleaseRefused(f"candidate invalid: {exc}") from exc
    try:
        approved=spec_module.validate_spec(approved_spec,require_approved=True)
        checks['spec']={'status':'PASS','spec_sha256':approved['sha256']}
        if candidate['goal']!=approved['goal']:
            raise ReleaseRefused('candidate goal does not match approved specification')
    except Exception as exc:
        raise ReleaseRefused(f"approved specification required: {exc}") from exc
    # Exact scope approval (root + application type + deployment lifecycle scopes);
    # the approval is a policy artifact, not a per-candidate confirmation.
    try:
        delivery._approval(deployment_authorization,Path(deployment_root).absolute(),'application')
        checks['authorization']={'status':'PASS','deployment_root':str(Path(deployment_root).resolve()),'candidate_type':'application'}
    except Exception as exc:
        raise ReleaseRefused(f"deployment authorization refused: {exc}") from exc
    try:
        if not isinstance(dependency_manifest,dict) or set(dependency_manifest)!={'candidate_sha256','lock','sources','artifacts','cache'} or dependency_manifest.get('candidate_sha256')!=digest:
            raise ReleaseRefused('environment evidence must bind exact candidate, lock, sources, artifacts and cache')
        cache=Path(dependency_manifest['cache'])
        environment=dependencies.environment_manifest(dependency_manifest['lock'],sources=dependency_manifest['sources'],artifacts=dependency_manifest['artifacts'],cache=cache)
        cache_info=dependencies.verify_cache(cache,dependency_manifest['artifacts'],expected_sources=set(dependency_manifest['sources']))
        if environment['cache_sha256']!=cache_info['cache_sha256']:
            raise ReleaseRefused('dependency cache digest mismatch')
        checks['environment']={'status':'PASS','lock_sha256':environment['lock_sha256'],'cache_sha256':environment['cache_sha256'],'toolchain':environment['toolchain'],'manifest':environment}
    except Exception as exc:
        raise ReleaseRefused(f"dependency environment blocked: {exc}") from exc
    try:
        q=quality.evaluate(digest,quality_policy,quality_records,authorized_na_reasons=authorized_na_reasons)
        checks['quality']={'status':q['status'],'report':q}
        if q['status']!='PASS': blockers.extend(q['blockers'])
    except Exception as exc:
        raise ReleaseRefused(f"quality evidence refused: {exc}") from exc
    try:
        if not isinstance(dual_evidence,dict) or dual_evidence.get('schema')!=1:
            raise ReleaseRefused('dual evaluation evidence envelope malformed')
        criterion=dual_evidence.get('criterion')
        keys={'schema','candidate_sha256','spec_sha256','proposal','validator'}
        if criterion is not None:
            if criterion!='independent_implementation': raise ReleaseRefused('unknown evaluation criterion')
            keys.add('criterion')
        if set(dual_evidence)!=keys:
            raise ReleaseRefused('dual evaluation evidence envelope malformed')
        if dual_evidence['candidate_sha256']!=digest or dual_evidence['spec_sha256']!=approved['sha256']:
            raise ReleaseRefused('dual evaluation evidence is not bound to exact candidate and approved spec')
        if criterion=='independent_implementation':
            reconciled=evaluation.reconcile_independent_implementation(dual_evidence['proposal'],dual_evidence['validator'])
        else:
            reconciled=evaluation.reconcile_evidence(dual_evidence['proposal'],dual_evidence['validator'])
        checks['independent_evaluation']={'status':reconciled['outcome'],'criterion':criterion or 'dual_lane','report':reconciled}
        if reconciled['outcome']!='PASS': blockers.append('independent_evaluation:'+reconciled['outcome'])
    except Exception as exc:
        raise ReleaseRefused(f"dual evaluation evidence refused: {exc}") from exc
    try:
        runtime=builder.preflight(engine,image)
        checks['container']={'status':'PASS',**runtime}
    except (OSError,ValueError,builder.BuildFailure,builder.MissingPrerequisite) as exc:
        checks['container']={'status':'BLOCKED','reason':str(exc)}
        blockers.append('container runtime unavailable or invalid')
    status='PASS' if not blockers else 'HOLD'
    payload={'schema':1,'status':status,'candidate_sha256':digest,'spec_sha256':approved['sha256'],'deployment_root':str(Path(deployment_root).resolve()),'checks':checks,'blockers':blockers,'limitations':['Preflight verifies declarations and recorded evidence; it does not execute the candidate or establish measurement truth.','Container availability is checked against the exact local image; this gate does not claim a deployment smoke test.']}
    return {**payload,'release_sha256':_sha(payload)}

def pack(candidate_dir:Path, report:dict[str,Any], dependency_manifest:dict[str,Any], engine_metadata:dict[str,Any], output:Path)->dict[str,Any]:
    if report.get('schema')!=1 or report.get('status')!='PASS' or report.get('candidate_sha256')!=delivery._read_candidate(Path(candidate_dir).absolute())[2]:
        raise ReleaseRefused('release preflight must PASS for exact candidate before packaging')
    unsigned={k:v for k,v in report.items() if k!='release_sha256'}
    if not isinstance(report.get('release_sha256'),str) or report['release_sha256']!=_sha(unsigned):raise ReleaseRefused('release report seal mismatch')
    checks=report.get('checks')
    if not isinstance(checks,dict) or any(not isinstance(checks.get(k),dict) or checks[k].get('status')!='PASS' for k in ('candidate','spec','authorization','environment','quality','independent_evaluation','container')):raise ReleaseRefused('release report is missing a passing required gate')
    if not isinstance(dependency_manifest,dict) or dependency_manifest.get('candidate_sha256')!=report.get('candidate_sha256'):
        raise ReleaseRefused('package environment must bind the preflight candidate')
    packed_environment=dependencies.environment_manifest(dependency_manifest['lock'],sources=dependency_manifest['sources'],artifacts=dependency_manifest['artifacts'],cache=Path(dependency_manifest['cache']))
    if packed_environment!=report.get('checks',{}).get('environment',{}).get('manifest'):
        raise ReleaseRefused('package environment differs from preflight evidence')
    release_metadata={**engine_metadata,'release_bundle':{'schema':1,'candidate_sha256':report['candidate_sha256'],'spec_sha256':report['spec_sha256'],'source_lock':dependency_manifest.get('lock'),'source_allowlist':dependency_manifest.get('sources'),'environment_manifest':report['checks']['environment']['manifest'],'instructions':{'run':'Use the recorded locked install argv in an isolated container plan; never run candidate code on the host.','upgrade':'Create and approve a new candidate/spec/environment binding, rerun release check, and create a new immutable package.','rollback':'Use application_operations.py transition <root> <authorization.json> --engine docker --image-id <exact-id> --action rollback --request-id <unique-id>.'}}}
    return delivery.create_pack(candidate_dir,report,dependency_manifest,release_metadata,output)
def promote(deployment_root:Path, *, authorization:dict[str,Any], request_id:str, image_id:str, engine:str, stop_file:Path|None=None)->dict[str,Any]:
    import application_operations
    return application_operations.transition(deployment_root,authorization=authorization,image_id=image_id,engine=engine,action='promote',request_id=request_id,stop_file=stop_file)

def rollback(deployment_root:Path, *, authorization:dict[str,Any], request_id:str, image_id:str, engine:str, stop_file:Path|None=None)->dict[str,Any]:
    import application_operations
    return application_operations.transition(deployment_root,authorization=authorization,image_id=image_id,engine=engine,action='rollback',request_id=request_id,stop_file=stop_file)
def main(argv=None)->int:


    ap=argparse.ArgumentParser(description=__doc__); sub=ap.add_subparsers(dest='command',required=True)
    p=sub.add_parser('check');
    for name in ('candidate','spec','quality_policy','quality_evidence','dual_evidence','environment','authorization','deployment_root'):
        p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--engine',choices=('docker','podman'),required=True); p.add_argument('--image',required=True); p.add_argument('--authorized-na',action='append',default=[]); p.add_argument('--output',type=Path)
    p=sub.add_parser('pack'); p.add_argument('--candidate',required=True,type=Path); p.add_argument('--report',required=True,type=Path); p.add_argument('--environment',required=True,type=Path); p.add_argument('--engine-metadata',required=True,type=Path); p.add_argument('--output',required=True,type=Path)
    for action in ('promote','rollback'):
        p=sub.add_parser(action);p.add_argument('--root',required=True,type=Path);p.add_argument('--authorization',required=True,type=Path);p.add_argument('--image-id',required=True);p.add_argument('--engine',choices=('docker',),required=True);p.add_argument('--request-id',required=True);p.add_argument('--stop-file',type=Path)
    a=ap.parse_args(argv)
    try:
        if a.command=='check':
            out=preflight(a.candidate,_load(a.spec),_load(a.quality_policy),_load(a.quality_evidence),_load(a.dual_evidence),_load(a.environment),_load(a.authorization),a.deployment_root,engine=a.engine,image=a.image,authorized_na_reasons=a.authorized_na)
            code=0 if out['status']=='PASS' else 2
            if a.output: a.output.write_text(json.dumps(out,ensure_ascii=False,sort_keys=True)+'\n',encoding='utf-8')
        elif a.command=='pack': out=pack(a.candidate,_load(a.report),_load(a.environment),_load(a.engine_metadata),a.output); code=0
        else:
            out=(promote if a.command=='promote' else rollback)(a.root,authorization=_load(a.authorization),request_id=a.request_id,image_id=a.image_id,engine=a.engine,stop_file=a.stop_file)
            code=0 if out.get('status') in {'PROMOTED','ROLLED_BACK'} else 2
        print(json.dumps(out,ensure_ascii=False,sort_keys=True)); return code
    except (ReleaseRefused,delivery.DeliveryRefused,OSError,ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
        print(json.dumps({'schema':1,'status':'HOLD','reason':str(exc)},ensure_ascii=False),file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())
