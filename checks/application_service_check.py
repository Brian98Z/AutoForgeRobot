#!/usr/bin/env python3
"""Check HTTP service contract logic locally; run real-container acceptance when configured."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse, json, socket, threading
from pathlib import Path
import application_service as service

def host_guard_check():
    listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(1); port=listener.getsockname()[1]
    secret='fixture-secret-must-not-leak'
    def respond():
        conn,_=listener.accept()
        with conn:
            conn.recv(4096); conn.sendall(('HTTP/1.1 not-a-status '+secret+'\r\nContent-Length: 0\r\n\r\n').encode())
        listener.close()
    worker=threading.Thread(target=respond,daemon=True); worker.start()
    try: service._request(port,'GET','/',None,{'Authorization':'Bearer '+secret})
    except service.ServiceFailure as exc:
        if secret in str(exc): raise AssertionError('untrusted HTTP response leaked secret') from None
    else: raise AssertionError('malformed HTTP status was accepted')
    finally: listener.close(); worker.join(timeout=2)
    return 'malformed HTTP response errors are non-echoing'
def invalid_json_guard_check():
    secret='private-body-must-not-echo'
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);port=listener.getsockname()[1]
    def respond():
        conn,_=listener.accept()
        with conn:
            conn.recv(4096)
            body=('<html>'+secret+'</html>').encode()
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
        listener.close()
    worker=threading.Thread(target=respond,daemon=True);worker.start()
    try:service._request(port,'GET','/json',None)
    except service.ServiceFailure as exc:
        assert str(exc)=='service returned invalid JSON' and secret not in str(exc)
    else:raise AssertionError('HTML response accepted as JSON')
    finally:listener.close();worker.join(timeout=2)
    return 'invalid JSON response retains safe actionable classification without body echo'
def secret_mount_guard_check():
    import hashlib, tempfile
    with tempfile.TemporaryDirectory(prefix='service-secret-check-') as td:
        root=Path(td); secret=root/'TOKEN'; secret.write_text('private-token-value',encoding='utf-8')
        mounts=[{'source':str(secret),'target':'/run/autoforge/secrets/TOKEN','read_only':True,'name':'TOKEN','sha256':hashlib.sha256(secret.read_bytes()).hexdigest()}]
        secret.write_text('changed-token-value',encoding='utf-8')
        try:
            service.run({}, {'schema':1,'health_path':'/','scenarios':[{'name':'x','requests':[{'method':'GET','path':'/','body':None,'expected':{'status':200,'body':None}}]}]}, 'docker','sha256:'+'0'*64,['python','-c','pass'],secret_mounts=mounts)
        except service.ServiceFailure as exc:
            assert 'missing or changed' in str(exc)
        else: raise AssertionError('mutated private secret file accepted')
    return 'private secret mount descriptor is integrity-bound and absent secret refs are refused'
import application_builder

def missing_entrypoint_check():
    import tempfile
    import applications
    with tempfile.TemporaryDirectory(prefix='http-entrypoint-check-') as td:
        destination=Path(td)/'candidate'
        applications.stage_candidate({'schema':1,'goal':'unfinished HTTP modules','references':[],
                                      'files':[{'path':'db.py','content':'value = 1\n'}]},destination)
        manifest=json.loads((destination/'manifest.json').read_text(encoding='utf-8'))
        report=service.run(manifest,{},'docker','not-a-present-image',['python','/candidate/main.py'])
        assert report['candidate_sha256']==manifest['candidate_sha256']
        assert report['build_pass'] is False and report['test_pass'] is False
        assert report['http_results']==[] and report['browser'] is None
        (destination/'db.py').write_text('tampered\n',encoding='utf-8')
        try: service.run(manifest,{},'docker','not-a-present-image',['python','/candidate/main.py'])
        except application_builder.BuildFailure: pass
        else: raise AssertionError('missing entrypoint bypassed candidate integrity')
    return 'missing HTTP entrypoint is bound FAIL before dispatch; tampering still rejected'

def main() -> int:
    result={'schema':1,'host_logic':{'status':'PASS','checks':[host_guard_check(),invalid_json_guard_check(),secret_mount_guard_check(),missing_entrypoint_check()],'limitations':['Host logic only; no service container or HTTP CRUD behavior was exercised.']},'container_acceptance':'NOT_RUN'}
    print(json.dumps(result,ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
