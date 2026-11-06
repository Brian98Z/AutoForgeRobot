"""Exercise real CLI preflight without model calls or candidate execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import application_build as entry
import application_builder as builder
import references

ROOT=Path(__file__).resolve().parent

class ModelTrap(BaseHTTPRequestHandler):
    calls=0
    def log_message(self,*args):pass
    def do_POST(self):
        type(self).calls+=1
        self.send_response(500);self.end_headers()


def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),ModelTrap)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='autoforge-preflight-') as td:
            root=Path(td);out=root/'run';env=os.environ.copy()
            env.update(AUTOFORGE_API_BASE=f'http://127.0.0.1:{server.server_port}/v1',AUTOFORGE_MODEL='trap')
            env.pop('AUTOFORGE_API_KEY',None)
            # Empty PATH guarantees unavailable engine even on a container-enabled workstation.
            env['PATH']=''
            common=['--request',str(ROOT/'fixtures/business/request.json'),'--contract',str(ROOT/'fixtures/business/contract.json'),
                    '--reference-store',str(root/'store'),'--image','sha256:'+'0'*64,'--output',str(out)]
            def invoke(action,extra=()):
                return subprocess.run([sys.executable,str(ROOT/'application_build.py'),action,*common,*extra],env=env,capture_output=True,encoding='utf-8',timeout=30)
            checked=invoke('check');assert checked.returncode==3,(checked.stdout,checked.stderr)
            assert json.loads(checked.stdout)['checks']['container']['status']=='BLOCKED'
            refused=invoke('run');assert refused.returncode==2
            blocked=invoke('run',['--allow-model','--trusted-adapter']);assert blocked.returncode==3,(blocked.stdout,blocked.stderr)
            assert ModelTrap.calls==0 and not out.exists()
            for extra in (
                ['--validation-kind','heldout','--oracle-ledger',str(root/'ledger.json'),'--max-oracle-queries','0'],
                ['--validation-kind','heldout','--oracle-ledger',str(root/'ledger.json'),'--max-oracle-queries','1','--mode','proposal'],
                ['--oracle-ledger',str(root/'ledger.json'),'--max-oracle-queries','1']):
                invalid_policy=invoke('run',['--allow-model','--trusted-adapter',*extra])
                assert invalid_policy.returncode==2 and ModelTrap.calls==0 and not out.exists()
            # Exact oracle copies cannot be introduced through the public reference catalog.
            package=references.ingest(ROOT/'fixtures/business/contract.json',root/'store','test corpus','owned')
            req=json.loads((ROOT/'fixtures/business/request.json').read_text(encoding='utf-8'));req['references']=[package['package_id']]
            request_path=root/'request.json';request_path.write_text(json.dumps(req),encoding='utf-8')
            with patch.object(builder,'preflight',return_value={'engine':'test','image_id':'test','server_os':'linux'}):
                report,_=entry.inspect(request_path,ROOT/'fixtures/business/contract.json',root/'store','docker','sha256:'+'0'*64)
            assert 'inputs' in report['blockers']
            assert ModelTrap.calls==0
            print(json.dumps({'status':'PASS','checks':['check invokes no model','missing authorization refuses run','missing engine prevents model invocation','blocked run creates no output','exact oracle reference refused'],'scope':'real CLI blocking path; container success path not exercised'}))
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)

if __name__=='__main__':main()
