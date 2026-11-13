"""Finite real-model/container pause observation, never replaying uncertain validation."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse,hashlib,json,os,subprocess,sys,threading,time,uuid
from pathlib import Path
import agent_budget,agent_runtime,agent_session
ROOT=Path(__file__).resolve().parents[1]
MODULES=ROOT/'autoforge'
IMAGE=os.environ.get('AUTOFORGE_RECOVERY_IMAGE') or 'sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60'
def worker(output,resume=False):
    request=json.loads((output/'request.json').read_text(encoding='utf-8'))
    initial=json.loads((output/'initial.json').read_text(encoding='utf-8'))
    argv=[sys.executable,str(MODULES/'application_builder.py'),'--engine','docker','--image',IMAGE,'--contract',str(output/'contract.json')]
    report=agent_runtime.run(request,[],argv,output/'session',max_turns=12,resume=resume,initial_candidate=None if resume else initial,limits=agent_budget.Limits(model_calls=12,tool_calls=32,tokens=500000,seconds=180),allowed_tools=['list_files','read_file','edit_file','validate'])
    print(json.dumps(report));return 0
def main():
    p=argparse.ArgumentParser();p.add_argument('--worker',type=Path);p.add_argument('--resume',action='store_true');a=p.parse_args()
    if a.worker:return worker(a.worker,a.resume)
    out=ROOT/'bootstrap-runs'/('container-recovery-live-'+uuid.uuid4().hex);out.mkdir(parents=True)
    request={'schema':1,'goal':'Repair the existing JSON-lines double-integer program: for each input {value:integer} return {double:2*value}, preserving its two-second work delay. Validate the existing program first, inspect its actual independent feedback, then make a local repair and validate. Preserve sentinel.txt. Do not remove the delay.','references':[]}
    source='import json,sys,time\nfor line in sys.stdin:\n x=json.loads(line);time.sleep(2);print(json.dumps({"double":3*x["value"]}),flush=True)\n'
    initial={'schema':1,'goal':request['goal'],'references':[],'files':[{'path':'main.py','content':source},{'path':'sentinel.txt','content':'unrelated user work must survive\n'}]}
    contract={'schema':1,'entrypoint':'main.py','scenarios':[{'name':'signed-integers','requests':[{'value':2},{'value':-3}],'expected':[{'double':4},{'double':-6}]}]}
    for n,v in [('request',request),('initial',initial),('contract',contract)]: (out/(n+'.json')).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    cmd=[sys.executable,str(Path(__file__).resolve()),'--worker',str(out)]
    start=time.monotonic();observed=None;pause_time=None;seen=[]
    with (out/'worker.stdout').open('wb') as stdout,(out/'worker.stderr').open('wb') as stderr:
        proc=subprocess.Popen(cmd,cwd=ROOT,stdout=stdout,stderr=stderr)
        try:
            while proc.poll() is None and time.monotonic()-start<150:
                cp=subprocess.run(['docker','ps','--format','{{json .}}'],capture_output=True,text=True,timeout=5)
                for row in cp.stdout.splitlines():
                    item=json.loads(row)
                    if not item.get('Names','').startswith('autoforge-'):continue
                    inspect=json.loads(subprocess.check_output(['docker','inspect',item['ID']],timeout=5))[0]
                    mounts=inspect.get('Mounts',[])
                    if len(seen)<40:
                        seen.append({'name':inspect['Name'],'running':inspect['State']['Running'],
                                     'sources':[m.get('Source') for m in mounts],
                                     'destinations':[m.get('Destination') for m in mounts]})
                    if any(str(out/'session'/'revisions').replace('\\','/').casefold() in m.get('Source','').replace('\\','/').casefold() for m in mounts):
                        observed={'id':inspect['Id'],'name':inspect['Name'],'image':inspect['Image'],'running':inspect['State']['Running'],'mounts':[{'source':m['Source'],'destination':m['Destination'],'rw':m['RW']} for m in mounts]}
                        token=json.loads((out/'session'/'.control.json').read_text())['token'];agent_session.enqueue_control(out/'session',token,'pause',request_id='pause-running-container')
                        pause_time=time.monotonic();break
                if observed:break
                time.sleep(.15)
            proc.wait(timeout=45)
        except BaseException:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
            raise
    paused=agent_session.load(out/'session')
    pause_latency=None if pause_time is None else time.monotonic()-pause_time
    before={'model_calls':paused['budget']['model_calls'],'tools':paused['tool_calls'],'inflight':paused['inflight'],'status':paused['task_status'],'reason':paused['reason_code']}
    if observed:
        check=subprocess.run(['docker','inspect',observed['id']],capture_output=True,text=True,timeout=5)
        if check.returncode==0:
            # Own exact identity and read-only candidate mount were observed above; preserve inspect evidence before removal.
            (out/'interrupted-container.json').write_text(check.stdout,encoding='utf-8')
            subprocess.run(['docker','rm','-f',observed['id']],check=True,capture_output=True,timeout=10)
        gone=subprocess.run(['docker','inspect',observed['id']],capture_output=True,timeout=5).returncode!=0
    else:gone=None
    resumed=subprocess.run(cmd+['--resume'],capture_output=True,text=True,timeout=180,cwd=ROOT)
    after=agent_session.load(out/'session')
    blocked={'status':after['task_status'],'reason':after['reason_code'],'inflight':after['inflight'],'model_calls':after['budget']['model_calls'],'tools':after['tool_calls']}
    receipt=None;finished=None
    if observed and gone and blocked['reason']=='unknown_tool_side_effect':
        import agent_reconcile
        builder=[sys.executable,str(MODULES/'application_builder.py'),'--engine','docker','--image',IMAGE,'--contract',str(out/'contract.json')]
        receipt=agent_reconcile.reconcile(out/'session',token,operator='authorized-local-test-controller',source='exact owned container inspected and absent; disposable JSONL validation only',builder_argv=builder)
        continued=subprocess.run(cmd+['--resume'],capture_output=True,text=True,timeout=180,cwd=ROOT)
        finished=agent_session.load(out/'session')
    intact=finished is not None and next(f['content'] for f in finished['candidate']['files'] if f['path']=='sentinel.txt')=='unrelated user work must survive\n'
    evidence={'schema':1,'scope':'Real model and exact-image container pause, unknown refusal, explicit operator reconciliation, resumed repair and independent acceptance.','observed_container':observed,'container_absent_after_cleanup':gone,'pause_return_seconds':pause_latency,'paused':before,'blocked':blocked,'reconciliation':receipt,'finished':None if finished is None else {'status':finished['task_status'],'reason':finished['reason_code'],'budget':finished['budget']},'containers_seen':seen,'sentinel_intact':intact,'resume_exit':resumed.returncode,'resume_stdout':resumed.stdout,'status':'PASS' if observed and gone and blocked['reason']=='unknown_tool_side_effect' and blocked['model_calls']==before['model_calls'] and blocked['tools']==before['tools'] and intact and finished['task_status']=='SUCCEEDED' else 'HOLD','limitations':['Explicit reconciliation limited to built-in disposable network-none JSONL validation, never arbitrary side effects.','Public delay fixture is not the inventory business target.']}
    (out/'verification.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({'status':evidence['status'],'report':str(out/'verification.json')}));return 0 if evidence['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
