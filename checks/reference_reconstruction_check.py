#!/usr/bin/env python3
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json,sys,tempfile,hashlib,subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "autoforge"))
import references,application_builder,reference_reconstruction as runner
import application_builder as builder
def source_path_check(root):
 src=root/'main.py';src.write_text('import json,sys\nfor line in sys.stdin: print(json.dumps({"ok":json.loads(line)["x"]}))\n',encoding='utf-8')
 store=root/'legacy-store';pkg=references.ingest(src,store,'operator-authored legacy source fixture','MIT',kind='source')
 cand=root/'legacy-candidate';cand.mkdir();(cand/'main.py').write_text(src.read_text(encoding='utf-8'),encoding='utf-8')
 contract={'schema':1,'reference_package_id':pkg['package_id'],'license_acknowledged':True,'entrypoint':'main.py','scenarios':[{'name':'one','requests':[{'x':1}],'expected':[{'ok':1}]}],'obligations':['Returns ok value']}
 cp=root/'legacy-contract.json';cp.write_text(json.dumps(contract),encoding='utf-8')
 oldpre=builder.preflight;oldrun=builder._container
 builder.preflight=lambda engine,image:{'engine':engine,'server_os':'linux','image_id':image}
 def legacy_run(engine,image,work,payload,timeout,**kwargs):
  proc=subprocess.run([sys.executable,str(work/'main.py')],input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
  return proc.stdout,proc.stderr,proc.returncode,False
 builder._container=legacy_run
 wrong=root/'legacy-wrong';wrong.mkdir();(wrong/'main.py').write_text('import json,sys\nfor line in sys.stdin: print(json.dumps({"ok":False}))\n',encoding='utf-8')
 try:
  assert runner.evaluate(store,pkg['package_id'],cand,cp,engine='docker',image='sha256:'+'a'*64)['status']=='PASS'
  assert runner.evaluate(store,pkg['package_id'],wrong,cp,engine='docker',image='sha256:'+'a'*64)['status']=='FAIL'
 finally: builder.preflight=oldpre;builder._container=oldrun
def stateful_source_check(root):
    fixture=Path(__file__).resolve().parents[1]/"fixtures"/"stateful_reference"
    store=root/"stateful-store"
    pkg=references.ingest(fixture,store,"AutoForge stateful source fixture authored for this task","MIT",kind="source")
    contract=json.loads((fixture/"contract.json").read_text(encoding="utf-8"));contract["reference_package_id"]=pkg["package_id"]
    contract_path=root/"stateful-contract.json";contract_path.write_text(json.dumps(contract),encoding="utf-8")
    candidate=root/"stateful-candidate";candidate.mkdir()
    (candidate/"main.py").write_text("import json,sys\nfor l in sys.stdin: print(json.dumps({'error':'invalid'}))\n",encoding="utf-8")
    oldpre,oldrun=builder.preflight,builder._run
    runner_calls=[]
    builder.preflight=lambda engine,image:{"engine":engine,"server_os":"linux","image_id":image}
    def fake_run(engine,image,work,invocations,timeout,*,runtime,state_file,precondition=None):
        import json
        runner_calls.append((runtime,len(invocations),state_file,precondition))
        state=(dict(precondition["value"]) if precondition and precondition.get("kind")=="state" else {"balance":0,"revision":0})
        outputs=[]
        for batch in invocations:
            lines=batch.decode("utf-8").splitlines(); emitted=[]
            for line in lines:
                try: request=json.loads(line)
                except ValueError: emitted.append({"error":"invalid"});continue
                amount=request.get("amount") if isinstance(request,dict) else None
                if isinstance(request,dict) and request.get("op")=="credit" and type(amount) is int and 0<amount<=9007199254740991 and state["balance"]+amount<=1000000:
                    state["balance"]+=amount;state["revision"]+=1;emitted.append(dict(state))
                elif isinstance(request,dict) and request.get("op")=="debit" and type(amount) is int and 0<amount<=state["balance"]:
                    state["balance"]-=amount;state["revision"]+=1;emitted.append(dict(state))
                elif isinstance(request,dict) and request.get("op")=="inspect": emitted.append(dict(state))
                else: emitted.append({"error":"invalid"})
            outputs.append({"code":0,"out":b"".join(json.dumps(value,separators=(",",":")).encode()+b"\n" for value in emitted),"err":b""})
        return outputs,b"",0,False
    old_restart=runner._stateful_restart_container
    hold_reason=[]
    try:
        runner._stateful_restart_container=fake_run
        try: runner.evaluate_stateful(store,pkg["package_id"],candidate,contract_path,engine="docker",image="sha256:"+"a"*64,reference_engine="docker",reference_image="sha256:"+"b"*64)
        except runner.ReconstructionHold as exc: hold_reason.append(str(exc))
    finally:
        runner._stateful_restart_container=old_restart
        builder.preflight=oldpre
        builder._run=oldrun
    if hold_reason: raise AssertionError("stateful fixture contract rejected: "+hold_reason[0])
    assert any(item[0]=="node" for item in runner_calls) and any(item[0]=="python" for item in runner_calls)
    assert all(item[2]=="/tmp/autoforge-stateful-ledger.json" for item in runner_calls)
def main():
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); source_path_check(root); stateful_source_check(root); store=root/'store'; artifact=root/'artifact'; artifact.mkdir()
   js="'use strict';\nlet total=0;\nfunction step(r){if(r.op==='add'&&Number.isSafeInteger(r.value)){total+=r.value;return {total};}return {error:'invalid'};}\nconst rl=require('node:readline').createInterface({input:process.stdin});rl.on('line',line=>console.log(JSON.stringify(step(JSON.parse(line)))));\n"
   (artifact/'main.js').write_text(js,encoding='utf-8')
   digest=hashlib.sha256((artifact/'main.js').read_bytes()).hexdigest()
   ref=references.ingest(artifact,store,'user-owned exact JavaScript MIT test artifact '+digest,'MIT',kind='source')
   evidence_file=root/'evidence.json'; evidence_file.write_text(json.dumps({'records':[{'evidence_id':'ev_smoke','operation':'analyze_javascript_application','authority':'shipped-artifact','confidence':'derived','normalized_result':{'root_artifact_sha256':digest,'graph':{'nodes':[{'identity':{'sha256':digest}}]}},'limitations':['authored fixture only']}]},separators=(',',':')),encoding='utf-8')
   evidence=references.ingest(evidence_file,store,'REA exported Evidence fixture; inert','Evidence not executable',kind='rea-evidence')
   contract={'schema':1,'evidence_package_id':evidence['package_id'],'evidence_ids':['ev_smoke'],'artifact_sha256':digest,'artifact_files':{'main.js':digest},'reference_package_id':ref['package_id'],'reference_main_js_sha256':digest,'license_acknowledged':True,'reference_entrypoint':'main.js','candidate_entrypoint':'main.py','scenarios':[{'name':'accumulate','requests':[{'op':'add','value':4},{'op':'add','value':-1}],'expected':[{'total':4},{'total':3}]},{'name':'reject-boolean-and-unknown','requests':[{'op':'add','value':True},{'op':'subtract','value':3}],'expected':[{'error':'invalid'},{'error':'invalid'}]}],'obligations':['Integer additions accumulate across JSON lines','Boolean values and unknown operations are rejected without mutation']}
   cp=root/'contract.json';cp.write_text(json.dumps(contract),encoding='utf-8')
   good=root/'good';good.mkdir();(good/'main.py').write_text('import json,sys\ntotal=0\nfor line in sys.stdin:\n r=json.loads(line)\n if r.get("op")=="add" and type(r.get("value")) is int:\n  total+=r["value"]; print(json.dumps({"total":total}))\n else: print(json.dumps({"error":"invalid"}))\n',encoding='utf-8')
   bad=root/'bad';bad.mkdir();(bad/'main.py').write_text('import json,sys\nfor line in sys.stdin: print(json.dumps({"total":0}))\n',encoding='utf-8')
   oldpre=application_builder.preflight;oldrun=application_builder._container
   application_builder.preflight=lambda engine,image:{'engine':engine,'server_os':'linux','image_id':image}
   def run(engine,image,work,payload,timeout,*,runtime='python'):
    if runtime=='node': proc=subprocess.run(['node',str(work/'main.js')],input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    else: proc=subprocess.run([sys.executable,str(work/'main.py')],input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    return proc.stdout,proc.stderr,proc.returncode,False
   application_builder._container=run
   try:
    failed=runner.evaluate_evidence(store,evidence['package_id'],ref['package_id'],bad,cp,artifact,engine='docker',image='sha256:'+'a'*64,reference_engine='docker',reference_image='sha256:'+'b'*64)
    passed=runner.evaluate_evidence(store,evidence['package_id'],ref['package_id'],good,cp,artifact,engine='docker',image='sha256:'+'a'*64,reference_engine='docker',reference_image='sha256:'+'b'*64)
    assert failed['status']=='FAIL' and passed['status']=='PASS'
    assert failed['reference_run']['behavior_pass'] and passed['candidate_run']['behavior_pass']
    assert failed['evidence']['evidence_ids']==['ev_smoke'] and failed['reference']['kind']=='byte-identical-shipped-javascript'
    altered=dict(contract); altered['reference_main_js_sha256']='0'*64; badcp=root/'bad-contract.json';badcp.write_text(json.dumps(altered),encoding='utf-8')
    try: runner.evaluate_evidence(store,evidence['package_id'],ref['package_id'],good,badcp,artifact,engine='docker',image='sha256:'+'a'*64,reference_engine='docker',reference_image='sha256:'+'b'*64)
    except runner.ReconstructionHold as exc: assert 'digest does not match' in str(exc)
    else: raise AssertionError('wrong reference bytes must be held')
    try: runner.evaluate_evidence(store,evidence['package_id'],evidence['package_id'],good,cp,artifact,engine='docker',image='sha256:'+'a'*64,reference_engine='docker',reference_image='sha256:'+'b'*64)
    except runner.ReconstructionHold as exc: assert 'separately licensed source' in str(exc)
    else: raise AssertionError('REA Evidence must never be executable reference')
   finally: application_builder.preflight=oldpre; application_builder._container=oldrun
  print('reference_reconstruction_check: PASS (legacy source path retained; inert Evidence bound; exact Node artifact positive/negative execution)')
if __name__=='__main__':main()
