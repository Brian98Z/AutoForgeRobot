"""Run the pinned Lean checker and record exact theorem/axiom evidence.

This checks formal model theorems, not Python source refinement or all of PROOF.md.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

HERE = Path(__file__).resolve().parent
DEFAULT_LEAN = HERE.parent / 'runtime/lean-4.35.0-windows/bin/lean.exe'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lean', type=Path, default=DEFAULT_LEAN)
    args=parser.parse_args()
    output=HERE/'results'/('kernel-'+uuid.uuid4().hex[:12]);output.mkdir(parents=True)
    source=HERE/'formal/Simulation.lean'
    report={'status':'RUNNING','scope':'Lean formal tape simulation only; Python refinement and complete universality encoding not certified',
            'commands':[],'axioms':[],'errors':[]}
    start=time.monotonic()

    def command(argv):
        result=subprocess.run(list(map(str,argv)),cwd=source.parent,capture_output=True,
                              encoding='utf-8',timeout=120)
        report['commands'].append({'argv':list(map(str,argv)),'returncode':result.returncode,
                                   'stdout':result.stdout,'stderr':result.stderr})
        return result

    try:
        if not args.lean.is_file():
            raise ValueError(f'Lean executable missing: {args.lean}')
        report.update(lean_sha256=sha(args.lean),source_sha256=sha(source),source=str(source))
        version=command([args.lean,'--version'])
        if version.returncode or 'version 4.35.0' not in version.stdout:
            raise ValueError('expected pinned Lean 4.35.0')
        report['version']=version.stdout.strip()
        # Reject bypass constructs conservatively even inside comments.
        text=source.read_text(encoding='utf-8')
        forbidden=re.findall(r'\b(?:sorry|admit|axiom|unsafe|native_decide)\b',text)
        if forbidden:
            raise ValueError('forbidden proof bypass token: '+','.join(sorted(set(forbidden))))
        checked=command([args.lean,'-DwarningAsError=true',source,'-o',output/'Simulation.olean'])
        if checked.returncode:
            raise ValueError('Lean rejected proof; see checker output')
        transcript=checked.stdout+'\n'+checked.stderr
        if 'sorryAx' in transcript:
            raise ValueError('unsound admitted theorem dependency')
        dependency_lines=[line for line in transcript.splitlines() if 'depends on axioms' in line or 'does not depend on any axioms' in line]
        required = ['decode_write','decode_left','decode_right','decode_stay','one_step','step_simulation','initial_related','fuel_simulation','input_simulation']
        for theorem in required:
            if not any(f"'TapeSimulation.{theorem}'" in line for line in dependency_lines):
                raise ValueError(f'missing theorem audit: {theorem}')
        report['checked_theorems']=required
        report['axioms']=dependency_lines
        # Negative control ensures this checker refuses a false arithmetic theorem.
        negative=output/'RejectFalse.lean'
        negative.write_text('theorem false_control : (0 : Nat) = 1 := by rfl\n',encoding='utf-8')
        refused=command([args.lean,negative])
        if refused.returncode==0:
            raise ValueError('checker accepted deliberately false theorem')
        report['negative_control']='REJECTED'
        report['status']='PASS'
    except Exception as exc:
        report['status']='FAIL';report['errors'].append(f'{type(exc).__name__}: {exc}')
    finally:
        report['elapsed_seconds']=round(time.monotonic()-start,4)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'status':report['status'],'report':str(output/'report.json'),'errors':report['errors'],'axioms':report['axioms']},ensure_ascii=False,indent=2))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':
    raise SystemExit(main())
