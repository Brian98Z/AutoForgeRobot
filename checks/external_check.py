"""Exercise external evaluation against real candidates plus receipt rejection controls."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import external_evaluation as external
import external_fit
ROOT=Path(__file__).resolve().parents[1]
def main():
    root=ROOT/'bootstrap-runs'/('external-check-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    state=root/'state';exe=ROOT/'target/release/autoforge.exe'
    env=dict(os.environ,AUTOFORGE_PROJECT=str(ROOT),AUTOFORGE_PROVIDER='offline')
    report={'status':'RUNNING','checks':[]}
    def cli(*args):
        p=subprocess.run([str(exe),*map(str,args)],capture_output=True,encoding='utf-8',env=env,cwd=ROOT,timeout=120)
        if p.returncode:raise RuntimeError(p.stderr)
        return json.loads(p.stdout)
    def passed(name):report['checks'].append(name);print('PASS:',name,flush=True)
    def rejected(fn):
        try:fn()
        except ValueError:return
        raise AssertionError('invalid external response accepted')
    try:
        cli('init',state);release=cli('evolve',state,ROOT/'examples/add.json')['release']
        for name,rows in [('fit',[{'input':[3,4],'expected':7},{'input':[-2,1],'expected':-1}]),('regression',[{'input':[0,0],'expected':0}]),('wrong',[{'input':[0,0],'expected':1}])]:
            (root/(name+'.json')).write_text(json.dumps(rows),encoding='utf-8')
        command=[sys.executable,str(Path(external_fit.__file__)),'--fit',str(root/'fit.json'),'--regression',str(root/'regression.json')]
        good=external.evaluate(ROOT,state,ROOT/'examples/add.json',release,root/'receipts',command,30)
        assert good['status']=='PASS' and good['response']['metrics']['fit_mae']==0,good
        passed('actual-candidate-fitting-and-regression-pass')
        bad=external.evaluate(ROOT,state,ROOT/'examples/add.json',release,root/'receipts',command[:-1]+[str(root/'wrong.json')],30)
        assert bad['status']=='REJECT' and bad['response']['metrics']['regression_failures']==1,bad
        passed('wrong-regression-label-veto')
        for field in ('request_id','task_sha256','release_id','wasm_sha256','evaluator_id'):
            mutation=copy.deepcopy(good['response']);mutation[field]='stale'
            rejected(lambda:external.parse_response(json.dumps(mutation),good['request']))
        mutation=copy.deepcopy(good['response']);mutation['metrics']['fit_mae']=float('nan')
        rejected(lambda:external.parse_response(json.dumps(mutation),good['request']))
        mutation=copy.deepcopy(good['response']);mutation['fit_pass']=1
        rejected(lambda:external.parse_response(json.dumps(mutation),good['request']))
        passed('stale-identities-nan-and-nonboolean-gates-rejected')
        identity=external.command_identity(command,ROOT)
        (root/'regression.json').write_text('[{"input":[0,0],"expected":1}]')
        assert external.command_identity(command,ROOT)!=identity
        passed('dataset-change-alters-evaluator-identity')
        slow=[sys.executable,'-c','import time; time.sleep(5)']
        timeout=external.evaluate(ROOT,state,ROOT/'examples/add.json',release,root/'receipts',slow,1)
        assert timeout['status']=='ERROR' and 'deadline' in timeout['error']
        cli('kill',state)
        stop=external.evaluate(ROOT,state,ROOT/'examples/add.json',release,root/'receipts',slow,10)
        assert stop['status']=='ERROR' and 'stop latched' in stop['error']
        assert cli('status',state)['state']=='KILLED'
        passed('timeout-and-operator-stop-fail-closed')
        report['status']='PASS'
    finally:
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Evidence:',root/'report.json',flush=True)
if __name__=='__main__':main()
