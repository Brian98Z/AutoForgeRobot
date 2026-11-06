#!/usr/bin/env python3
"""Run finite public benchmark tasks through AutoForge agent and isolated builder."""
from __future__ import annotations
import argparse,hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
MODEL_PROFILES={
 'quality':('http://127.0.0.1:8081/v1',r'H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\LLM\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_NL.gguf'),
 'speed':('http://127.0.0.1:8084/v1',r'H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\text_encoders\Qwen\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf'),
}
IMAGE="sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60"
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--inventory',required=True);p.add_argument('--partition',choices=('development','heldout'),default='development');p.add_argument('--task-id',action='append');p.add_argument('--lock',default=str(ROOT/'fixtures/benchmark/lock.json'));p.add_argument('--model-profile',choices=sorted(MODEL_PROFILES),default=os.environ.get('AUTOFORGE_MODEL_PROFILE','quality'));p.add_argument('--base-url');p.add_argument('--model');a=p.parse_args()
 profile_base,profile_model=MODEL_PROFILES[a.model_profile];base_url=a.base_url or os.environ.get('AUTOFORGE_API_BASE') or profile_base;model=a.model or os.environ.get('AUTOFORGE_MODEL') or profile_model
 inv=json.loads(Path(a.inventory).read_text(encoding='utf-8')); manifest=json.loads((ROOT/'fixtures/benchmark/tasks.json').read_text(encoding='utf-8')); tasks=manifest['tasks']; contract_path=ROOT/'fixtures/benchmark/contracts.json'; contracts_bytes=contract_path.read_bytes(); contracts=json.loads(contracts_bytes.decode('utf-8'))['contracts']
 lock=json.loads(Path(a.lock).read_text(encoding='utf-8'))
 if hashlib.sha256(contracts_bytes).hexdigest()!=lock['contracts_sha256'] or hashlib.sha256((ROOT/'fixtures/benchmark/tasks.json').read_bytes()).hexdigest()!=lock['public_manifest_sha256']: raise SystemExit('HOLD: locked public task or acceptance contracts changed; no model contacted')
 if inv.get('sha256')!=lock['inventory_sha256'] or inv.get('partition_key')!=lock['partition_key'] or inv.get('heldout_fraction')!=lock['heldout_fraction']: raise SystemExit('HOLD: inventory does not match pre-optimization lock; no model contacted')
 rebuilt=__import__('application_evaluation').benchmark_inventory(tasks,heldout_fraction=inv['heldout_fraction'],partition_key=inv['partition_key'])
 if rebuilt!=inv: raise SystemExit('HOLD: task manifest or inventory changed; no model contacted')
 __import__('application_evaluation').validate_inventory(inv); byid={r['task_id']:r for r in inv['tasks']}
 if set(byid)!={t['task_id'] for t in tasks} or set(contracts)!={t['task_id'] for t in tasks}: raise SystemExit('HOLD: task or oracle inventory mismatch; no model contacted')
 selected=[t for t in tasks if byid[t['task_id']]['partition']==a.partition and (not a.task_id or t['task_id'] in a.task_id)]
 if a.task_id and set(a.task_id)!={t['task_id'] for t in selected}: raise SystemExit('HOLD: task selection outside locked partition')
 run=ROOT/'bootstrap-runs'/('benchmark-'+uuid.uuid4().hex);run.mkdir(parents=True,exist_ok=False); reports=[]
 for task in selected:
  tid=task['task_id']; work=run/tid;work.mkdir(); req={"schema":1,"goal":task['public_input']+" Create main.py only, read JSON objects one per input line, print one JSON response per line and no other stdout. Implement exactly this public contract.","references":[]};rp=work/'request.json';rp.write_text(json.dumps(req,ensure_ascii=False),encoding='utf-8'); cp=work/'contract.json';cp.write_text(json.dumps(contracts[tid]),encoding='utf-8'); refs=work/'references';refs.mkdir();out=work/'session'
  cmd=[sys.executable,str(ROOT/'application_build.py'),'run','--request',str(rp),'--contract',str(cp),'--reference-store',str(refs),'--engine','docker','--image',IMAGE,'--output',str(out),'--mode','agent','--max-turns','12','--token-budget','500000','--time-budget','200','--tool-budget','64','--allow-model','--trusted-adapter']
  started=time.monotonic();proc=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=220,check=False,env={**os.environ,'AUTOFORGE_API_BASE':base_url,'AUTOFORGE_MODEL':model,'AUTOFORGE_MODEL_PROFILE':a.model_profile,'AUTOFORGE_API_KEY':'','AUTOFORGE_TOOL_CHOICE':'auto'}); elapsed=time.monotonic()-started
  try:cli=json.loads(proc.stdout)
  except ValueError:cli=None
  events=[]
  ep=out/'events.jsonl'
  if ep.exists():
   for line in ep.read_text(encoding='utf-8').splitlines():
    try:
     e=json.loads(line); events.append(e)
    except ValueError:continue
  usage=[e.get('usage') for e in events if e.get('type')=='model_usage' and isinstance(e.get('usage'),dict)]
  usage_complete=(len(usage)==sum(e.get('type')=='model_call_intent' for e in events) and all(type(u.get(k)) is int and u[k]>=0 for u in usage for k in ('prompt_tokens','completion_tokens')))
  input_tokens=sum(u['prompt_tokens'] for u in usage) if usage_complete else None
  output_tokens=sum(u['completion_tokens'] for u in usage) if usage_complete else None
  tool_names={e['call_id']:e['name'] for e in events if e.get('type')=='tool_started' and isinstance(e.get('call_id'),str) and isinstance(e.get('name'),str)}
  report={"task_id":tid,"status":cli.get('status','HOLD') if isinstance(cli,dict) else 'HOLD',"task_status":cli.get('task_status') if isinstance(cli,dict) else None,"returncode":proc.returncode,"elapsed_seconds":elapsed,"model_call_intents":sum(e.get('type')=='model_call_intent' for e in events),"tool_event_count":sum(e.get('type')=='tool_result' for e in events),"mutation_count":mutations,"input_tokens":input_tokens,"output_tokens":output_tokens,"token_usage_status":"measured_server_reported" if usage_complete else "NOT_RECORDED","tool_events":[{k:e[k] for k in ('type','turn','name','timestamp') if k in e} for e in events if e.get('type') in ('tool_started','tool_result','model_call_intent','model_usage')],"request_sha256":digest(rp),"acceptance_sha256":digest(cp),"image_id":IMAGE,"image_sha256":hashlib.sha256(IMAGE.encode()).hexdigest(),"model_profile":a.model_profile,"base_url":base_url,"model_id":model,"source_sha256":{n:digest(ROOT/n) for n in ('agent_runtime.py','agent_tools.py','application_build.py','application_builder.py','agent_business_check.py')} ,"cost_microunits":0,"cost_source":"configured local cost budget; not billing evidence","human_touches":0,"human_intervention":False,"cli_result":cli,"stdout_sha256":hashlib.sha256(proc.stdout.encode()).hexdigest(),"stderr_sha256":hashlib.sha256(proc.stderr.encode()).hexdigest(),"oracle_sha256":digest(cp),"authority":"one actual local model run; shared provider and acceptance oracle prevent independent-confirmation claim"}
  path=work/'run.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');reports.append(report)
 summary={"schema":1,"run_dir":str(run),"inventory_sha256":inv['sha256'],"task_order":[x['task_id'] for x in selected],"runs":reports,"status":"OBSERVED_NOT_INDEPENDENTLY_CONFIRMED","limitations":["Single run per task; no stability inference.","Same model and oracle authority; behavior reports are not independent dual evidence.","Cost is configured local zero, not billing evidence; token usage is measured only when every call reports complete server usage.","Heldout assignments were fixed in supplied inventory before driver execution."]}
 (run/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({"run_dir":str(run),"task_order":summary['task_order'],"status":summary['status']}));return 0
if __name__=='__main__':raise SystemExit(main())
