"""Real local controller failure/recovery checks; isolated state only."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[1]


def main():
    root=ROOT/'bootstrap-runs'/('transactions-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    exe=ROOT/'target/release/autoforge.exe'
    env=dict(os.environ,AUTOFORGE_PROJECT=str(ROOT),AUTOFORGE_PROVIDER='offline')
    report={'status':'RUNNING','checks':[],'commands':[]}
    def call(*args,code=0):
        p=subprocess.run([str(exe),*map(str,args)],capture_output=True,encoding='utf-8',env=env,cwd=ROOT,timeout=180)
        report['commands'].append({'args':list(map(str,args)),'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
        assert p.returncode==code,(args,p.stdout,p.stderr)
        return json.loads(p.stdout or p.stderr)
    def passed(name):report['checks'].append(name);print('PASS:',name,flush=True)
    try:
        state=root/'state';call('init',state);call('kill',state)
        # A directory at the next journal filename makes publication fail before authorization.
        obstruction=state/'ledger/00000001.json';obstruction.mkdir()
        call('resume',state,code=1)
        assert (state/'STOP').exists()
        obstruction.rmdir();assert call('status',state)['state']=='KILLED'
        call('resume',state);assert call('status',state)['state']=='READY'
        passed('failed-resume-journal-publication-keeps-stop-and-retry-recovers')
        call('kill',state);(state/'STOP').unlink();(state/'STOP').mkdir()
        call('resume',state,code=1)
        assert call('status',state)['state']=='KILLED'
        events=[json.loads(p.read_text())['payload']['event'] for p in sorted((state/'ledger').glob('*.json'))]
        assert events[-1]=='OPERATOR_RESUME'
        (state/'STOP').rmdir();(state/'STOP').write_bytes(b'operator kill')
        call('resume',state);assert call('status',state)['state']=='READY'
        passed('failed-stop-removal-retains-stop-and-committed-authorization')
        # Existing legacy partial blob requires explicit quarantine; no silent overwrite.
        task=json.loads((ROOT/'examples/add.json').read_text())
        raw=json.dumps({k:task[k] for k in ('schema','id','goal','public_cases','holdout_cases','max_rounds')},ensure_ascii=False,separators=(',',':')).encode()
        address=hashlib.sha256(raw).hexdigest();(state/'blobs'/address).write_bytes(raw[:11])
        call('evolve',state,ROOT/'examples/add.json',code=1)
        recovered=call('recover',state)
        assert address in recovered['quarantined_unreferenced_damaged_blobs']
        assert call('evolve',state,ROOT/'examples/add.json')['decision']=='PROMOTE'
        passed('partial-unreferenced-blob-explicitly-quarantined-and-retried')
        release=call('status',state)['active']
        payload=json.loads((state/'blobs'/release).read_text())['payload']
        wasm=state/'blobs'/payload['wasm'];original=wasm.read_bytes();wasm.write_bytes(b'corruption')
        call('recover',state,code=1);assert wasm.read_bytes()==b'corruption'
        wasm.write_bytes(original)
        assert call('execute',state,-7,3)['output']==-4
        passed('transitively-referenced-corruption-is-not-quarantined')
        assert call('rollback',state)['active'] is None
        assert call('status',state)['active'] is None
        call('execute',state,1,2,code=1)
        count=call('status',state)['ledger_entries']
        assert call('rollback',state)['changed'] is False
        assert call('status',state)['ledger_entries']==count
        passed('first-release-rollback-to-empty-and-idempotent-repeat')
        partial=root/'partial';(partial/'blobs').mkdir(parents=True);(partial/'ledger').mkdir()
        shutil.copy2(state/'private.key',partial/'private.key')
        old=(partial/'private.key').read_bytes();call('recover',partial)
        assert (partial/'private.key').read_bytes()==old
        assert (partial/'public.key').read_bytes()==(state/'public.key').read_bytes()
        assert call('status',partial)['ledger_entries']==1
        passed('private-only-initialization-recovers-with-identical-identity')
        fresh=root/'absent';call('recover',fresh);assert call('status',fresh)['active'] is None
        passed('absent-state-recovery-initializes')
        unrelated=root/'unrelated';unrelated.mkdir();(unrelated/'important.txt').write_text('preserve')
        call('recover',unrelated,code=1);assert sorted(p.name for p in unrelated.iterdir())==['important.txt']
        passed('unrelated-directory-recovery-refused-without-mutation')
        before={p.name:p.read_bytes() for p in (partial/'ledger').glob('*.json')}
        call('init',partial,code=1)
        assert before=={p.name:p.read_bytes() for p in (partial/'ledger').glob('*.json')}
        passed('init-never-overwrites-existing-state')
        report['status']='PASS'
    except Exception as exc:
        report['status']='FAIL';report['error']=repr(exc);raise
    finally:
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Evidence:',root/'report.json',flush=True)


if __name__=='__main__':main()
