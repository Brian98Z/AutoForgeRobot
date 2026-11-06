"""Real cumulative multi-goal endurance with forced process-boundary resume.
No mocked providers; does not turn finite execution into a general reliability proof.
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds',type=int,default=100)
    parser.add_argument('--segment',type=int,default=25)
    args=parser.parse_args()
    if args.rounds<1 or args.segment<1:parser.error('positive rounds and segment required')
    root=ROOT/'bootstrap-runs'/('endurance-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    run=root/'run';started=time.monotonic()
    report={'status':'RUNNING','target_rounds':args.rounds,'segments':[],'completed_rounds':0,'scope':'real cumulative offline i64 evolution, not weight training or universal synthesis'}
    def save():
        report['elapsed_seconds']=time.monotonic()-started
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        for index in range((args.rounds+args.segment-1)//args.segment):
            command=[sys.executable,str(ROOT / 'autoforge' / 'iterate.py'),'--rounds',str(args.rounds),'--checkpoint-every',str(args.segment),
                     '--max-new-rounds',str(args.segment),'--timeout','600','--output' if index==0 else '--resume',str(run)]
            begin=time.monotonic()
            with (root/f'{index:04}.stdout.txt').open('wb') as out,(root/f'{index:04}.stderr.txt').open('wb') as err:
                p=subprocess.run(command,cwd=ROOT,stdout=out,stderr=err,timeout=7200)
            if p.returncode:raise RuntimeError(f'segment {index} failed: {p.returncode}; see logs')
            cp=json.loads(sorted((run/'checkpoints').glob('*.json'))[-1].read_text(encoding='utf-8'))
            expected=min((index+1)*args.segment,args.rounds)
            assert cp['completed_rounds']==expected and len(cp['rows'])==expected
            assert len({r['task_id'] for r in cp['rows']})==expected
            assert all(set(r['line_gate_evidence'])=={'python','julia'} for r in cp['rows'])
            report['segments'].append({'segment':index,'completed_rounds':expected,'seconds':time.monotonic()-begin,'checkpoint':str(sorted((run/'checkpoints').glob('*.json'))[-1])})
            report['completed_rounds']=expected;save()
            print(f'PASS: actual cumulative rounds {expected}/{args.rounds}; process-boundary resumes {index}',flush=True)
        assert cp['status']=='COMPLETE'
        p=subprocess.run([str(ROOT/'target/release/autoforge.exe'),'replay',str(run/'state')],capture_output=True,encoding='utf-8',timeout=600)
        assert p.returncode==0,p.stderr
        report['final_replay']=json.loads(p.stdout)
        assert report['final_replay']['verified_releases']==args.rounds
        report['status']='PASS';save()
    except BaseException as exc:
        report['status']='FAIL';report['error']=repr(exc);save();raise
    finally:print('Evidence:',root/'report.json',flush=True)


if __name__=='__main__':main()
