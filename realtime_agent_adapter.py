"""Trusted native-agent builder adapter for actual isolated SSE acceptance."""
from __future__ import annotations
import argparse, hashlib, json, sys, time, uuid
from pathlib import Path
import application_builder as builder
import application_platform as platform

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--contract',type=Path,required=True)
    p.add_argument('--image',required=True)
    p.add_argument('--command-json',required=True)
    p.add_argument('--evidence-root',type=Path,required=True)
    a=p.parse_args()
    envelope=builder._strict_loads(sys.stdin.buffer.read(builder.MAX_INPUT+1))
    if not isinstance(envelope,dict) or set(envelope)!={'schema','workspace_manifest','candidate_sha256'} or envelope['schema']!=1:raise ValueError('invalid builder envelope')
    manifest=envelope['workspace_manifest'];_,digest=platform._manifest(manifest)
    if digest!=envelope['candidate_sha256']:raise ValueError('candidate envelope mismatch')
    contract=platform._strict(a.contract);platform._event_contract(contract)
    command=platform._strict_argv(a.command_json)
    execution={'engine':'docker','image':a.image,'command_sha256':hashlib.sha256(platform._canonical(command)).hexdigest(),'contract_sha256':hashlib.sha256(platform._canonical(contract)).hexdigest(),'protocol':'sse','url':contract['url'],'timeout_seconds':20}
    auth={'schema':1,'authorize':True,'platform':'realtime','target':contract['target'],'candidate_sha256':digest,'execution':execution,'expires_at':time.time()+180,'nonce':uuid.uuid4().hex}
    try:
        report=platform._realtime(manifest,contract,auth,'docker',a.image,command,20)
        passed=report['status']=='PASS'
        observations=[r['name']+': '+('PASS' if r['pass'] else 'FAIL') for r in report['scenarios']]
    except (OSError,ValueError) as e:
        report={'status':'FAIL','candidate_sha256':digest,'reason':str(e)[:3000]}
        passed=False;observations=[report['reason']]
    a.evidence_root.mkdir(parents=True,exist_ok=True)
    destination=a.evidence_root/(uuid.uuid4().hex+'.json')
    with destination.open('x',encoding='utf8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({'schema':1,'candidate_sha256':digest,'build_pass':passed,'test_pass':passed,'observations':observations,'limitations':['Exact isolated SSE scenario acceptance only; independent report '+str(destination)]}))
    return 0
if __name__=='__main__':raise SystemExit(main())
