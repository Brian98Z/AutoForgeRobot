"""Explicit operator reconciliation for interrupted built-in disposable JSONL validation."""
from __future__ import annotations
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
import agent_session
import application_builder
import application_checkpoint as checkpoint


def reconcile(output:Path,token:str,*,operator:str,source:str,builder_argv:list[str])->dict:
    """Clear unknown validation only after exact adapter and container-absence checks.

    This never handles HTTP adapters, mounted databases, arbitrary adapters or external writes.
    """
    if not operator.strip() or not source.strip():raise ValueError('explicit operator and evidence source required')
    output=Path(output).resolve(strict=True)
    with checkpoint.ExclusiveLock(output/'session.lock'):
        agent_session.authenticate(output,token,require_active=True)
        state=agent_session.load(output)
        inflight=state.get('inflight')
        if state.get('reason_code')!='unknown_tool_side_effect' or not isinstance(inflight,dict) or inflight.get('kind')!='tool' or inflight.get('name')!='validate':raise ValueError('only unknown validate can be reconciled')
        committed=[m for m in state['messages'] if m.get('role')=='tool' and m.get('tool_call_id')==inflight.get('call_id')]
        if len(committed)!=1 or 'error' not in json.loads(committed[0]['content']):raise ValueError('interruption result not committed; manual external-effect investigation required')
        if inflight.get('candidate_sha256')!=hashlib.sha256(checkpoint.canonical(state['candidate'])).hexdigest():raise ValueError('candidate changed since interrupted dispatch')
        argv=state['config']['builder_argv']
        command=state['config'].get('builder_command_sha256')
        raw=builder_argv
        if not isinstance(raw,list) or any(not isinstance(x,str) for x in raw):raise ValueError('explicit builder argv required')
        builder_path=Path(application_builder.__file__).resolve()
        engine='docker'
        if len(raw)!=8 or Path(raw[0]).resolve()!=Path(sys.executable).resolve() or Path(raw[1]).resolve()!=builder_path or raw[2]!='--engine' or raw[3]!='docker' or raw[4]!='--image' or raw[6]!='--contract':raise ValueError('only built-in Docker JSONL builder is eligible')
        image=raw[5];contract=raw[7]
        if checkpoint._argv_identity(raw)!=argv or hashlib.sha256(checkpoint.canonical(checkpoint._safe_argv(raw))).hexdigest()!=command:raise ValueError('builder or contract changed since interruption')
        application_builder.preflight(engine,image)
        ids=subprocess.run([engine,'ps','-aq'],capture_output=True,text=True,check=True,timeout=15).stdout.split()
        matches=[]
        if ids:
            inventory=json.loads(subprocess.run([engine,'inspect',*ids],capture_output=True,text=True,check=True,timeout=15).stdout)
            prefix=str(output/'revisions').replace('\\','/').casefold()+'/'
            for container in inventory:
                for mount in container.get('Mounts',[]):
                    mounted=mount.get('Source','').replace('\\','/').casefold().rstrip('/')+'/'
                    if mounted.startswith(prefix):matches.append(container['Id'])
        if matches:raise ValueError('owned validation container still exists; inspect and clean exact owner before reconciliation')
        receipt={'schema':1,'kind':'disposable-jsonl-validation-reconciled','session_id':state['session_id'],'inflight':inflight,'candidate_sha256':hashlib.sha256(checkpoint.canonical(state['candidate'])).hexdigest(),'builder_command_sha256':command,'image_id':image,'operator':operator,'source':source,'observed_at':time.time(),'matching_containers':[],'outcome':'interrupted disposable validation not accepted; new validation explicitly permitted','limitations':['Only built-in network-none JSONL validation with read-only candidate mount and ephemeral tmpfs.','No assertion about HTTP/database/payment or arbitrary adapter effects.']}
        receipt_digest=hashlib.sha256(checkpoint.canonical(receipt)).hexdigest()
        receipt_path=output/('reconciliation-'+receipt_digest+'.json')
        checkpoint._atomic(receipt_path,checkpoint.canonical(receipt)+b'\n')
        with agent_session.EventJournal(output,resume=True) as journal:journal.append({'type':'validation_reconciled','receipt_sha256':receipt_digest,'receipt':receipt_path.name,'call_id':inflight['call_id']})
        state['inflight']=None;state.update(status='HOLD',task_status='PAUSED',reason_code='validation_reconciled')
        state['observations'].append('Operator reconciled disposable validation; no accepted result; resume may request a new validation.')
        agent_session.save(output,state)
        return {'status':'RECONCILED','receipt':str(receipt_path),'receipt_sha256':receipt_digest,'task_status':'PAUSED'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('output',type=Path);p.add_argument('--operator',required=True);p.add_argument('--source',required=True);p.add_argument('--builder-argv',required=True,help='exact trusted original JSON argv');a=p.parse_args()
    try:
        token=os.environ.get('AGENT_SESSION_CONTROL_TOKEN','')
        result=reconcile(a.output,token,operator=a.operator,source=a.source,builder_argv=json.loads(a.builder_argv));print(json.dumps(result));return 0
    except (ValueError,OSError,subprocess.SubprocessError) as exc:print(json.dumps({'status':'BLOCKED','reason':str(exc)}));return 2
if __name__=='__main__':raise SystemExit(main())
