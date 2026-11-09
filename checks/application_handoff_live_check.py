#!/usr/bin/env python3
"""Exercise candidate handoff under restrictive POSIX umask and real UID-65534 HTTP startup."""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'autoforge'))
import applications
import application_builder
import application_service

POSIX_PROBE = '''import hashlib,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,'/controller')
import applications,application_builder
base=Path(tempfile.mkdtemp());base.chmod(0o755)
os.umask(0o077)
candidate={'schema':1,'goal':'non-root handoff','references':[],'files':[{'path':'main.py','content':'print(7)\\n'},{'path':'nested/data.txt','content':'runtime input'}]}
applications.stage_candidate(candidate,base/'revision')
m=json.loads((base/'revision/manifest.json').read_text());root,digest=application_builder._manifest(m)
os.setgid(65534);os.setuid(65534)
assert os.getuid()==65534
for row in m['files']:
 assert hashlib.sha256((root/row['path']).read_bytes()).hexdigest()==row['sha256']
assert json.loads((root/'manifest.json').read_text())['candidate_sha256']==digest
try:(root/'main.py').write_text('changed')
except PermissionError:pass
else:raise AssertionError('non-root candidate source became writable')
print(json.dumps({'uid':os.getuid(),'candidate_sha256':digest,'readable':True,'writable':False}))
'''
SERVICE = '''import json,os
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
state=Path('/state/started')
if state.exists() and Path('/candidate/nested/mode.txt').read_text()=='fail-restart':raise SystemExit(7)
state.write_text('started')
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  body=json.dumps({'uid':os.getuid(),'value':Path('/candidate/nested/value.txt').read_text()}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine',default='docker')
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    application_builder.preflight(args.engine,args.image)
    command=[args.engine,'run','--rm','--network=none','--read-only','--cap-drop=ALL','--cap-add=SETUID','--cap-add=SETGID','--security-opt=no-new-privileges','--mount',f'type=bind,src={ROOT / "autoforge"},dst=/controller,readonly','--tmpfs','/tmp:rw,nosuid,nodev,mode=1777','--entrypoint','python',args.image,'-I','-c',POSIX_PROBE]
    proc=subprocess.run(command,capture_output=True,timeout=30,check=True)
    posix=json.loads(proc.stdout)
    reports={}
    with tempfile.TemporaryDirectory(prefix='autoforge-handoff-') as td:
        base=Path(td)
        private=base/'secret';private.write_text('handoff-fixture-not-production')
        mounts=[{'source':str(private),'target':'/run/autoforge/secrets/TOKEN','read_only':True,'name':'TOKEN','sha256':hashlib.sha256(private.read_bytes()).hexdigest()}]
        for mode in ('normal','fail-restart'):
            candidate={'schema':1,'goal':'HTTP non-root handoff','references':[],'files':[{'path':'main.py','content':SERVICE},{'path':'nested/value.txt','content':'immutable-input'},{'path':'nested/mode.txt','content':mode}]}
            applications.stage_candidate(candidate,base/mode)
            manifest=json.loads((base/mode/'manifest.json').read_text())
            expected={'status':200,'body':{'uid':65534,'value':'immutable-input'}}
            contract={'schema':1,'health_path':'/health','scenarios':[{'name':'restart','restart_after':0,'requests':[{'method':'GET','path':'/health','body':None,'expected':expected},{'method':'GET','path':'/health','body':None,'expected':expected}]}]}
            report=application_service.run(manifest,contract,args.engine,args.image,['python','-I','/candidate/main.py'],timeout=20,secret_mounts=mounts)
            assert report['candidate_sha256']==manifest['candidate_sha256']
            if mode=='normal':
                assert report['build_pass'] and report['test_pass'],report
                assert report['http_results'][0]['requests'][1]['actual']==expected,report
            else:
                assert report['build_pass'] and not report['test_pass'],report
                row=report['http_results'][0]['requests'][0]
                assert row['phase']=='restart' and not row['pass'] and 'restart' in row['error'],report
            reports[mode]=report
    result={'schema':1,'status':'PASS','posix_restrictive_umask':posix,'services':reports,'limitations':['Authored regression candidates, not the external frozen benchmark candidates.','No production-security or arbitrary generated-UI claim.']}
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True))
    return 0
if __name__=='__main__':raise SystemExit(main())
