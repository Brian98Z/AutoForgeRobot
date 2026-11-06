"""Real process resume tests; operator fixtures parse source, never run candidate code."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory(prefix='autoforge-resume-') as temp:
        root=Path(temp); req=root/'request.json'; req.write_text(json.dumps({'schema':1,'goal':'repair syntax','references':[]}),encoding='utf-8')
        proposer=root/'proposer.py'; builder=root/'builder.py'
        proposer.write_text('''import json,sys,pathlib
p=json.load(sys.stdin)
f=pathlib.Path(__file__).with_suffix('.calls'); f.write_text(str(int(f.read_text())+1 if f.exists() else 1))
old=p.get('previous_candidate')
source='def value(:\\n return 7\\n' if old is None else old['files'][0]['content'].replace('value(:','value():')
print(json.dumps(dict(schema=1,goal=p['requirement']['goal'],references=[],files=[dict(path='main.py',content=source)])))
''',encoding='utf-8')
        builder.write_text('''import ast,json,sys,pathlib
p=json.load(sys.stdin); m=p['workspace_manifest']; issues=[]
try: ast.parse(pathlib.Path(m['workspace'],'main.py').read_text(encoding='utf-8'))
except SyntaxError: issues=['SyntaxError']
print(json.dumps(dict(schema=1,candidate_sha256=p['candidate_sha256'],build_pass=not issues,test_pass=not issues,observations=issues,limitations=['AST only; source not executed'])))
''',encoding='utf-8')
        def command(output,proposal=proposer):
            return [sys.executable,str(ROOT / 'autoforge' / 'applications.py'),'cycle','--request',str(req),'--proposer-command',json.dumps([sys.executable,str(proposal)]),'--builder-command',json.dumps([sys.executable,str(builder)]),'--trusted-adapter','--rounds','2','--output',str(output),'--reference-store',str(root/'refs')]
        def invoke(cmd,code):
            p=subprocess.run(cmd,capture_output=True,encoding='utf-8',timeout=30)
            assert p.returncode==code,(p.returncode,p.stdout,p.stderr)
            return p
        out=root/'run'; base=command(out)
        invoke(base+['--max-new-rounds','1'],2)
        paused=json.loads((out/'report.json').read_text(encoding='utf-8'));assert paused['rounds_completed']==1
        invoke(base+['--resume'],0)
        report=json.loads((out/'report.json').read_text(encoding='utf-8'));assert [r['status'] for r in report['history']]==['FAIL','PASS']
        invoke(base+['--resume'],0);assert proposer.with_suffix('.calls').read_text()=='2'
        original=builder.read_bytes();builder.write_bytes(original+b'\n# changed\n')
        assert 'changed' in invoke(base+['--resume'],2).stderr
        builder.write_bytes(original)
        source=Path(report['history'][0]['validation']['stage']['directory'])/'main.py'; saved=source.read_bytes();source.write_bytes(b'changed')
        invoke(base+['--resume'],2);source.write_bytes(saved)
        checkpoint=out/'cycle-state.json'; state=checkpoint.read_bytes();checkpoint.write_bytes(state.replace(b'"schema":1',b'"schema":2',1))
        invoke(base+['--resume'],2);checkpoint.write_bytes(state)
        request_saved=req.read_bytes();req.write_text(json.dumps({'schema':1,'goal':'changed','references':[]}),encoding='utf-8')
        invoke(base+['--resume'],2);req.write_bytes(request_saved)
        slow=root/'slow.py'; marker=root/'started'
        slow.write_text('import pathlib,time\npathlib.Path('+repr(str(marker))+').write_text("ready")\ntime.sleep(60)\n',encoding='utf-8')
        interrupted=root/'interrupted'; ibase=command(interrupted,slow)
        child=subprocess.Popen(ibase,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=os.name!='nt')
        try:
            deadline=time.monotonic()+10
            while not marker.exists() and time.monotonic()<deadline:
                if child.poll() is not None:raise AssertionError('cycle exited before inflight test')
                time.sleep(.05)
            assert marker.exists()
            assert 'lock' in invoke(ibase+['--resume'],2).stderr
        finally:
            if os.name=='nt':subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            else:
                import signal
                os.killpg(child.pid,signal.SIGKILL)
            child.wait(timeout=10)
        assert 'in flight' in invoke(ibase+['--resume'],2).stderr
        print(json.dumps({'status':'PASS','scope':'real CLI processes with AST-only operator adapters','checks':['pause-resume source repair','terminal no-repeat','adapter mutation refused','candidate mutation refused','checkpoint corruption refused','request mutation refused','concurrent lock refused','killed inflight refuses replay']}))

if __name__=='__main__':main()
