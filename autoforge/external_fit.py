"""Reference external evaluator: execute active WASM against operator-held datasets.

Datasets are arrays of {input:[i64,i64],expected:i64}. Public counterexamples are
emitted only when explicitly enabled, and only for failed fitting examples.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys


def dataset(path):
    rows=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows,list) or not 1<=len(rows)<=256:raise ValueError('dataset requires 1..256 cases')
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'input','expected'} or not isinstance(row['input'],list) or len(row['input'])!=2:raise ValueError('invalid evaluation case')
        if any(type(v) is not int or not -(1<<63)<=v<(1<<63) for v in row['input']+[row['expected']]):raise ValueError('exact i64 evaluation cases required')
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fit',type=Path,required=True)
    parser.add_argument('--regression',type=Path,required=True)
    parser.add_argument('--max-mae',type=float,default=0)
    parser.add_argument('--public-feedback',action='store_true',help='return failed fit rows as public counterexamples (never regression rows)')
    args=parser.parse_args()
    if not math.isfinite(args.max_mae) or args.max_mae<0:parser.error('max-mae must be finite and nonnegative')
    request=json.loads(sys.stdin.buffer.read(1048577))
    def measure(rows):
        errors=[]
        for row in rows:
            result=subprocess.run([request['execution']['controller'],'execute',request['execution']['state'],*map(str,row['input'])],capture_output=True,encoding='utf-8',timeout=30)
            if result.returncode:raise ValueError('candidate execution failed: '+result.stderr)
            result=json.loads(result.stdout)
            if result['release']!=request['release_id']:raise ValueError('active candidate changed during evaluation')
            errors.append(abs(result['output']-row['expected']))
        return errors
    fit_rows=dataset(args.fit); regression_rows=dataset(args.regression)
    fit=measure(fit_rows);regression=measure(regression_rows)
    response={key:request[key] for key in ('schema','request_id','task_sha256','release_id','wasm_sha256','evaluator_id')}
    response.update(metrics={'fit_mae':sum(fit)/len(fit),'fit_max_error':max(fit),'fit_exact_rate':sum(e==0 for e in fit)/len(fit),
                             'regression_failures':sum(e!=0 for e in regression),'fit_cases':len(fit),'regression_cases':len(regression)},
                    fit_pass=sum(fit)/len(fit)<=args.max_mae,regression_pass=all(e==0 for e in regression),
                    feedback=([row for row,error in zip(fit_rows,fit) if error!=0] if args.public_feedback else []))
    print(json.dumps(response,separators=(',',':')))


if __name__=='__main__':main()
