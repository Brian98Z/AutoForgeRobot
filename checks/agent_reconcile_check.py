"""Behavioral reconciliation guards; no real model or candidate code execution."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib,json,sys,tempfile
from pathlib import Path
import agent_reconcile as R
import agent_session as S
import application_checkpoint as C
def refuse(fn):
    try:fn()
    except (ValueError,OSError):return
    raise AssertionError('unsafe reconciliation accepted')
def main():
    with tempfile.TemporaryDirectory(prefix='reconcile-check-') as td:
        out=Path(td);token=S.create_control(out,'check')
        candidate={'schema':1,'goal':'guard','references':[],'files':[{'path':'main.py','content':'# not executed\n'}]}
        call={'kind':'tool','name':'validate','call_id':'v','candidate_sha256':hashlib.sha256(C.canonical(candidate)).hexdigest()}
        state={'session_id':'check','task_status':'BLOCKED','status':'HOLD','reason_code':'unknown_tool_side_effect','inflight':call,'candidate':candidate,'messages':[],'observations':[],'config':{'builder_argv':[],'builder_command_sha256':'0'*64}}
        S.save(out,state)
        args={'operator':'test-operator','source':'guard smoke','builder_argv':[sys.executable,'arbitrary.py']}
        refuse(lambda:R.reconcile(out,'wrong',**args))
        before=(out/'session.json').read_bytes()
        refuse(lambda:R.reconcile(out,token,**args))
        assert (out/'session.json').read_bytes()==before
        state['messages']=[{'role':'tool','tool_call_id':'v','content':json.dumps({'error':'interrupted'})}];S.save(out,state)
        before=(out/'session.json').read_bytes();refuse(lambda:R.reconcile(out,token,**args));assert (out/'session.json').read_bytes()==before
        state['inflight']['candidate_sha256']='0'*64;S.save(out,state)
        refuse(lambda:R.reconcile(out,token,**args))
        assert not list(out.glob('reconciliation-*.json'))
    print(json.dumps({'status':'PASS','checks':['invalid control token denied','uncommitted external call retained','arbitrary adapter denied','changed candidate denied','no receipt or checkpoint mutation on rejection'],'scope':'Host guard behavior only; real Docker pause/reconcile/resume recorded separately'}))
if __name__=='__main__':main()
