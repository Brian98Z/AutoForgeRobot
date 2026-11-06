"""Host-side toolchain contract and denial checks; never installs or pulls images."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
from pathlib import Path
import application_toolchain as t


def main()->None:
    image='sha256:'+'a'*64
    manifest={'schema':1,'toolchain':{'container_image':image},'ecosystem':'python','install_argv':['python','-m','pip','install','--no-index','--require-hashes','--find-links','/autoforge-cache','-r','/autoforge/requirements.lock'],'artifacts':[],'source_allowlist':{}}
    blocked=t.check(manifest,None,None,Path('missing-cache'),'docker','sha256:'+'b'*64)
    assert blocked['status']=='BLOCKED' and blocked['candidate_executed'] is False
    denied=t.check({**manifest,'ecosystem':'maven'},None,None,Path('missing-cache'),'docker',image)
    assert denied['status']=='BLOCKED'
    try:
        t._candidate(Path('.'),{'schema':1,'goal':'x','references':[],'files':[{'path':'../escape','size':0,'sha256':hashlib.sha256(b'').hexdigest()}],'candidate_sha256':'0'*64})
    except t.ToolchainRefused:pass
    for eco in ('python','node','typescript','java'):
        lock={'schema':1,'ecosystem':eco,'toolchain':{'name':eco,'version':'1','container_image':image},'packages':[]}
        validated=__import__('application_dependencies').validate_lock(lock,sources={})
        assert validated['ecosystem']==eco
        assert __import__('application_dependencies').locked_install_argv({'ecosystem':eco})
    assert t._npm_satisfies('5.9.3','^5.8.0')
    assert t._npm_matches_any('10.26.4','>=10')
    assert t._npm_matches_any('10.26.4','^10')
    assert not t._npm_matches_any('11.0.0','^10')

if __name__=='__main__':main()
