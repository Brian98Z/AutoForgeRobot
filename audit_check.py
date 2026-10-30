"""Real expansion checks for hidden-only search, conditional rollback and runner commit windows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import iterate

ROOT=Path(__file__).resolve().parent


def main():
    root=ROOT/'bootstrap-runs'/('audit-check-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    report={'status':'RUNNING','checks':[]}
    exe=ROOT/'target/release/autoforge.exe'
    env=dict(os.environ,AUTOFORGE_PROJECT=str(ROOT),AUTOFORGE_PROVIDER='offline')
    def call(*args,code=0):
        p=subprocess.run([str(exe),*map(str,args)],cwd=ROOT,env=env,capture_output=True,encoding='utf-8',timeout=120)
        assert p.returncode==code,(p.stdout,p.stderr)
        return json.loads(p.stdout or p.stderr)
    def passed(name):report['checks'].append(name);print('PASS:',name,flush=True)
    try:
        state=root/'search';call('init',state)
        task={'schema':1,'id':'hidden-search','goal':'fit public examples without repeating rejected hypotheses',
              'public_cases':[{'input':[0,0],'expected':0}],
              'holdout_cases':[{'input':[2,3],'expected':6},{'input':[-3,4],'expected':-12}], 'max_rounds':8}
        path=root/'search.json';path.write_text(json.dumps(task),encoding='utf-8')
        outcome=call('evolve',state,path);assert outcome['decision']=='PROMOTE'
        assert call('execute',state,7,9)['output']==63
        payload=json.loads((state/'blobs'/outcome['release']).read_text(encoding='utf-8'))['payload']
        assert outcome['rounds']>1
        assert len([a for a in payload['attempts'] if a['line']=='julia' and a['decision']=='PASS'])>1
        report['search_rounds']=outcome['rounds'];passed('hidden-only-rejection-advances-and-preserves-passing-line')
        call('rollback',state,'0'*64,code=1)
        assert call('status',state)['active']==outcome['release']
        passed('conditional-rollback-refuses-stale-active')
        # Same contract through runner validates passing attempts in final round, not all history.
        tasks=root/'tasks.json';tasks.write_text(json.dumps([task]),encoding='utf-8')
        iterate.main(['--rounds','1','--proposal-rounds','8','--tasks',str(tasks),'--output',str(root/'runner-search')])
        passed('runner-accepts-asymmetric-pass-history')
        interrupted=root/'interrupted'
        real=iterate.Runner.commit_round
        def fail_after(self,row):
            real(self,row)
            raise KeyboardInterrupt('injected after receipt publication')
        iterate.Runner.commit_round=fail_after
        try:
            try:iterate.main(['--rounds','1','--output',str(interrupted)])
            except KeyboardInterrupt:pass
        finally:iterate.Runner.commit_round=real
        assert call('status',interrupted/'state')['active'] is not None
        iterate.main(['--rounds','1','--resume',str(interrupted)])
        passed('committed-round-interruption-does-not-compensate')
        crash=root/'bootstrap-window';crash.mkdir();(crash/'events').mkdir();(crash/'tasks').mkdir()
        p=subprocess.run([sys.executable,str(ROOT/'iterate.py'),'--rounds','1','--output',str(crash),'--plan-only'],cwd=ROOT,capture_output=True,encoding='utf-8',check=True)
        config=json.loads(p.stdout)['config'];config.pop('config_sha256')
        (crash/'run-intent.json').write_bytes(iterate.canonical(config)+b'\n')
        call('init',crash/'state');key=(crash/'state/public.key').read_bytes()
        iterate.main(['--rounds','1','--resume',str(crash)])
        assert key==(crash/'state/public.key').read_bytes()
        passed('initialization-before-config-window-recovers-identity')
        report['status']='PASS'
    except BaseException as exc:
        report['status']='FAIL';report['error']=repr(exc);raise
    finally:
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Evidence:',root/'report.json',flush=True)


if __name__=='__main__':main()
