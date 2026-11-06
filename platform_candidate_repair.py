"""Import a model-authored snapshot into a new native-tool repair session; never resume unknown effects."""
import argparse,hashlib,json,os,sys
from pathlib import Path
import applications,agent_runtime,agent_budget

def main():
 p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path,required=True);p.add_argument('--builder-json',required=True);p.add_argument('--feedback',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 os.environ.update(AUTOFORGE_API_BASE='http://127.0.0.1:8084/v1',AUTOFORGE_MODEL=r'H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\text_encoders\Qwen\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf',AUTOFORGE_API_KEY='',AUTOFORGE_TOOL_CHOICE='auto')
 raw=a.candidate.read_bytes();candidate,_=applications.validate_candidate(json.loads(raw.decode('utf8')))
 goal=candidate['goal']+' Public repair feedback: '+a.feedback+' FIRST call validate before any edit. Use read_file SHA, then ONE method or statement per edit; new/old <=1500 characters. Never whole-file replace. Preserve all requirements.'
 candidate={**candidate,'goal':goal};request={'schema':1,'goal':goal,'references':candidate['references']}
 if request['references']:raise ValueError('only reference-free platform snapshots supported')
 a.output.mkdir(parents=True,exist_ok=False)
 (a.output/'import.json').write_text(json.dumps({'source':str(a.candidate.resolve()),'sha256':hashlib.sha256(raw).hexdigest(),'new_session_not_resume':True,'feedback':a.feedback}),encoding='utf8')
 report=agent_runtime.run(request,[],json.loads(a.builder_json),a.output/'session',24,limits=agent_budget.Limits(model_calls=24,tool_calls=96,tokens=500000,seconds=600),initial_candidate=candidate,allowed_tools=['list_files','read_file','create_file','edit_file','search_files','validate'])
 (a.output/'outcome.json').write_text(json.dumps(report,indent=2),encoding='utf8');print(json.dumps(report));return 0 if report['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
