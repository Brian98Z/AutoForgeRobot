"""Generate a realtime service through native tools and its isolated SSE validator."""
import argparse,json,os,sys
from pathlib import Path
import agent_runtime,agent_budget,application_builder
ROOT=Path(__file__).resolve().parent
GOAL='''Implement a compact Python notification HTTP/SSE service, stdlib only, port 8000. main.py starts ThreadingHTTPServer. argv[1] is bearer token. GET /health=200. Missing/wrong Authorization on GET /events and POST /publish,/stop=401. POST /publish body {message:string} appends history and returns202; invalid JSON/message=400 without mutation. GET /events streams id: 1,2,3 etc, event: message, data: compact JSON {message:string}; flush and terminate each frame with two newlines. Last-Event-ID reconnect sends only later history, then waits for new events. POST /stop auth returns202 and later /events=410. Thread-safe history, do not hold lock while socket writes. Keep server alive after stop.
IMPORTANT tools create_file/edit_file accept max1500 characters. Write terse code without comments/docstrings. Build 4 SMALL modules: state.py shared condition/history/stopped under400; getpart.py GET handler under1400; postpart.py POST handler under1400; main.py combines mixins with BaseHTTPRequestHandler and ThreadingHTTPServer under500. Each path is ROOT relative (main.py, never candidate/main.py). Implement only one module per create_file. Existing path edits require read_file SHA and baseline validate first. Validate actual network service after creation. No hand-coded test answers, hardcoded message list, stdout JSONL or fake validation.'''
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--contract',type=Path,required=True);p.add_argument('--image',required=True);a=p.parse_args()
 application_builder.preflight('docker',a.image)
 os.environ.update(AUTOFORGE_API_BASE='http://127.0.0.1:8084/v1',AUTOFORGE_MODEL=r'H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\text_encoders\Qwen\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf',AUTOFORGE_API_KEY='',AUTOFORGE_TOOL_CHOICE='auto')
 a.output.mkdir(parents=True,exist_ok=False)
 request={'schema':1,'goal':GOAL,'references':[]}
 (a.output/'request.json').write_text(json.dumps(request),encoding='utf8')
 argv=[sys.executable,str(ROOT/'realtime_agent_adapter.py'),'--contract',str(a.contract.resolve()),'--image',a.image,'--command-json',json.dumps(['python','-u','/candidate/main.py','autoforge-public-test-token']),'--evidence-root',str((a.output/'validations').resolve())]
 report=agent_runtime.run(request,[],argv,a.output/'session',24,limits=agent_budget.Limits(model_calls=24,tool_calls=96,tokens=500000,seconds=600),allowed_tools=['list_files','read_file','create_file','edit_file','search_files','validate'])
 (a.output/'outcome.json').write_text(json.dumps(report,indent=2),encoding='utf8');print(json.dumps(report));return 0 if report['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
