"""Existing proposal bootstrap followed by native-tool independent platform validation."""
import argparse,json,os,sys,time
from pathlib import Path
import applications,application_proposer,agent_runtime,agent_budget
ROOT=Path(__file__).resolve().parents[1]

def main():
 p=argparse.ArgumentParser();p.add_argument('--request',type=Path,required=True);p.add_argument('--builder-json',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--max-turns',type=int,default=16);p.add_argument('--required-file',action='append',default=[]);p.add_argument('--previous-candidate',type=Path);p.add_argument('--feedback',default='');a=p.parse_args()
 os.environ.update(AUTOFORGE_API_BASE='http://127.0.0.1:8084/v1',AUTOFORGE_MODEL=r'H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\text_encoders\Qwen\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf',AUTOFORGE_API_KEY='',AUTOFORGE_TOOL_CHOICE='auto')
 req=applications._request_payload(json.loads(a.request.read_text(encoding='utf8')))
 if req['references']:raise ValueError('this bounded bootstrap supports no external references')
 argv=json.loads(a.builder_json)
 a.output.mkdir(parents=True,exist_ok=False)
 payload={'schema':1,'requirement':req,'references':[],'previous_validation':{}}
 if a.previous_candidate:
  previous,_=applications.validate_candidate(json.loads(a.previous_candidate.read_text(encoding='utf8')))
  payload['previous_candidate']=previous
  payload['previous_validation']={'public_feedback':a.feedback,'authority':'untrusted diagnostic, not permission or acceptance'}
 (a.output/'proposal-request.json').write_text(json.dumps(payload),encoding='utf8')
 started=time.monotonic()
 try:
  body={'model':os.environ['AUTOFORGE_MODEL'],'messages':[{'role':'system','content':'Return only JSON {"schema":1,"goal":...,"references":[],"files":[{"path":...,"content":...}]}. Goal exactly equals requirement.goal. Complete compact source, no commentary. Never execute code or claim validation.'},{'role':'user','content':json.dumps(payload)}],'response_format':{'type':'json_object'},'max_tokens':6000,'temperature':0,'chat_template_kwargs':{'enable_thinking':False}}
  body['response_format']={'type':'json_schema','json_schema':{'name':'candidate','strict':True,'schema':{'type':'object','properties':{'schema':{'const':1},'goal':{'const':req['goal']},'references':{'type':'array','maxItems':0,'items':{'type':'string'}},'files':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'}},'required':['path','content'],'additionalProperties':False}}},'required':['schema','goal','references','files'],'additionalProperties':False}}}
  if a.required_file:
   body['response_format']['json_schema']['schema']['properties']['files']={'type':'array','minItems':len(a.required_file),'maxItems':len(a.required_file),'items':{'type':'object','properties':{'path':{'type':'string','enum':a.required_file},'content':{'type':'string','minLength':1}},'required':['path','content'],'additionalProperties':False}}
   body['messages'][0]['content']+=' Required files in exact order: '+json.dumps(a.required_file)+'. Include complete working content for EVERY required file.'
  response=application_proposer.completion(body)
  choice=response['choices'][0]
  (a.output/'proposal-response.txt').write_text(choice.get('message',{}).get('content',''),encoding='utf8')
  if choice.get('finish_reason')!='stop' or choice['message'].get('refusal'):raise ValueError('proposal incomplete or refused')
  candidate,_=applications.validate_candidate(application_proposer._json_loads(choice['message']['content'],'candidate'))
  if a.required_file and sorted(f['path'] for f in candidate['files'])!=sorted(a.required_file):raise ValueError('proposal omitted or duplicated required file')
  if candidate['goal']!=req['goal'] or candidate['references']!=[]:raise ValueError('proposal changed goal/reference identity')
  (a.output/'proposal-usage.json').write_text(json.dumps({'model':response.get('model'),'usage':response.get('usage'),'finish_reason':choice['finish_reason'],'thinking_disabled':True}),encoding='utf8')
  (a.output/'proposal-candidate.json').write_text(json.dumps(candidate,ensure_ascii=False,indent=2),encoding='utf8')
  stage=applications.stage_candidate(candidate,a.output/'initial-stage')
  manifest=json.loads(Path(stage['manifest']).read_text(encoding='utf8'))
  observed=applications._run_json_command(argv,{'schema':1,'workspace_manifest':manifest,'candidate_sha256':stage['candidate_sha256']})
  (a.output/'initial-validation.json').write_text(json.dumps(observed,indent=2),encoding='utf8')
  if observed.get('build_pass') is True and observed.get('test_pass') is True:
   report={'status':'PASS','generation_mode':'model proposal with independent real platform acceptance','bootstrap_calls':1,'candidate_sha256':stage['candidate_sha256'],'validation':observed}
   (a.output/'outcome.json').write_text(json.dumps(report,indent=2),encoding='utf8');print(json.dumps(report));return 0
  report=agent_runtime.run(req,[],argv,a.output/'session',a.max_turns,limits=agent_budget.Limits(model_calls=a.max_turns,tool_calls=64,tokens=500000,seconds=600),initial_candidate=candidate,allowed_tools=['list_files','read_file','create_file','edit_file','search_files','validate'])
  report['generation_mode']='proposal bootstrap plus native-tool independent validation/repair';report['bootstrap_calls']=1
 except (ValueError,OSError) as e:report={'status':'HOLD','error':str(e),'generation_mode':'proposal bootstrap','elapsed_seconds':time.monotonic()-started}
 (a.output/'outcome.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(report));return 0 if report['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
