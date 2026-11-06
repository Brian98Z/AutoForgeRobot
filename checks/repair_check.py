"""Real external counterexample repair, immutable holdout, budget and resume checks."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import iterate
import repair_protocol

ROOT=Path(__file__).resolve().parents[1]


def main():
    root=ROOT/'bootstrap-runs'/('repair-check-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    task={'schema':1,'id':'ambiguous','goal':'infer intended arithmetic','public_cases':[{'input':[0,0],'expected':0},{'input':[2,2],'expected':4}], 'holdout_cases':[{'input':[0,0],'expected':0}], 'max_rounds':2}
    taskfile=root/'tasks.json';taskfile.write_text(json.dumps([task]),encoding='utf-8')
    fit=root/'fit.json';fit.write_text(json.dumps([{'input':[3,4],'expected':12},{'input':[5,6],'expected':30}]),encoding='utf-8')
    regression=root/'regression.json';regression.write_text(json.dumps([{'input':[2,2],'expected':4}]),encoding='utf-8')
    command=[sys.executable,str(ROOT / 'autoforge' / 'external_fit.py'),'--fit',str(fit),'--regression',str(regression),'--public-feedback']
    common=['--rounds','1','--tasks',str(taskfile),'--evaluator-command',json.dumps(command)]
    report={'status':'RUNNING','checks':[]}
    def passed(name):report['checks'].append(name);print('PASS:',name,flush=True)
    def check_run(path):
        cp=json.loads(sorted((path/'checkpoints').glob('*.json'))[-1].read_text(encoding='utf-8'))
        assert cp['status']=='COMPLETE' and cp['completed_rounds']==1
        row=cp['rows'][0]
        assert len(row['repair_chain'])==1 and row['effective_task']['holdout_cases']==row['original_task']['holdout_cases']
        assert row['external_evaluation']['response']['metrics']['fit_mae']==0
        return cp
    try:
        run=root/'repaired';iterate.main(common+['--external-repairs','1','--output',str(run)]);check_run(run)
        iterate.main(common+['--external-repairs','1','--resume',str(run)]);check_run(run)
        passed('public-counterexamples-repair-addition-to-multiplication-and-resume')
        denied=root/'zero-budget'
        try:iterate.main(common+['--output',str(denied)])
        except RuntimeError:pass
        else:raise AssertionError('zero budget did not refuse')
        cp=json.loads(sorted((denied/'checkpoints').glob('*.json'))[-1].read_text(encoding='utf-8'))
        assert cp['completed_rounds']==0 and cp['active_release'] is None
        passed('zero-repair-budget-rolls-back-and-stops')
        for feedback in ([{'input':[0,0],'expected':0}],[{'input':[2,2],'expected':99}]):
            try:repair_protocol.validate_feedback(task,task,feedback)
            except ValueError:pass
            else:raise AssertionError('protected or conflicting feedback accepted')
        passed('holdout-overlap-and-public-conflict-refused')
        interrupted=root/'intent-before-rollback'
        real=iterate.Runner.rollback_exact
        def interrupted_rollback(self,rid):raise KeyboardInterrupt('injected after repair intent before rollback')
        iterate.Runner.rollback_exact=interrupted_rollback
        try:
            try:iterate.main(common+['--external-repairs','1','--output',str(interrupted)])
            except KeyboardInterrupt:pass
        finally:iterate.Runner.rollback_exact=real
        assert list((interrupted/'repairs').glob('*.json'))
        iterate.main(common+['--external-repairs','1','--resume',str(interrupted)]);check_run(interrupted)
        passed('repair-intent-before-rollback-crash-resumes-without-duplicate-feedback')
        report['status']='PASS'
    except BaseException as exc:
        report['status']='FAIL';report['error']=repr(exc);raise
    finally:
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print('Evidence:',root/'report.json',flush=True)


if __name__=='__main__':main()
