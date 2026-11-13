"""Operator-trusted external fitting/regression subprocess protocol; never a sandbox.
External acceptance is an extra gate, not proof or permission to override internal gates.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

import paths

from universal.machine import _atomic_create


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command_identity(command, project):
    if not isinstance(command,list) or not command or any(not isinstance(x,str) or not x for x in command):
        raise ValueError('evaluator command must be a nonempty JSON string array; no shell')
    resolved=[]
    for index,arg in enumerate(command):
        candidate=Path(arg.split('=',1)[1] if index and arg.startswith('--') and '=' in arg else arg)
        if not candidate.is_absolute():candidate=Path(project)/candidate
        if index==0 and not candidate.is_file():
            found=shutil.which(arg)
            if not found:raise ValueError('evaluator executable not found')
            candidate=Path(found)
        resolved.append({'argument':arg,'sha256':sha(candidate)} if candidate.is_file() else {'argument':arg})
    return hashlib.sha256(json.dumps(resolved,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def parse_response(raw,request):
    def unique(pairs):
        obj={}
        for key,value in pairs:
            if key in obj:raise ValueError('duplicate response key')
            obj[key]=value
        return obj
    def invalid(value):raise ValueError('nonfinite JSON constant')
    result=json.loads(raw,object_pairs_hook=unique,parse_constant=invalid)
    fields={'schema','request_id','task_sha256','release_id','wasm_sha256','evaluator_id','metrics','fit_pass','regression_pass','feedback'}
    if not isinstance(result,dict) or set(result)!=fields:raise ValueError('external response fields mismatch')
    for key in ('schema','request_id','task_sha256','release_id','wasm_sha256','evaluator_id'):
        if type(result[key]) is not type(request[key]) or result[key]!=request[key]:raise ValueError('external response identity mismatch: '+key)
    if type(result['fit_pass']) is not bool or type(result['regression_pass']) is not bool:raise ValueError('gate values must be booleans')
    metrics=result['metrics']
    if not isinstance(metrics,dict) or not metrics or len(metrics)>64:raise ValueError('1..64 fitting metrics required')
    for key,value in metrics.items():
        if not isinstance(key,str) or not key or len(key)>128 or type(value) not in (int,float):raise ValueError('invalid numeric metric')
        try:finite=math.isfinite(value)
        except OverflowError:finite=False
        if not finite:raise ValueError('metric outside finite numeric range')
    feedback=result['feedback']
    if not isinstance(feedback,list) or len(feedback)>256:raise ValueError('feedback must be bounded array')
    for item in feedback:
        if not isinstance(item,dict) or set(item)!={'input','expected'}:raise ValueError('feedback must contain only public input/expected')
        if not isinstance(item['input'],list) or len(item['input'])!=2:raise ValueError('invalid feedback input')
        if any(type(v) is not int or not -(1<<63)<=v<(1<<63) for v in item['input']+[item['expected']]):raise ValueError('feedback requires exact i64')
    return result


def evaluate(project,state,task_path,release_id,output_dir,command,timeout):
    project,state,task_path,output_dir=map(Path,(project,state,task_path,output_dir))
    if type(timeout) is not int or timeout<1:raise ValueError('positive integer evaluation timeout required')
    output_dir.mkdir(parents=True,exist_ok=True)
    request_id=uuid.uuid4().hex
    receipt={'status':'ERROR','request_id':request_id,'response':None}
    try:
        release_bytes=(state/'blobs'/release_id).read_bytes()
        if hashlib.sha256(release_bytes).hexdigest()!=release_id:raise ValueError('release hash mismatch')
        release=json.loads(release_bytes)['payload']
        task_blob=state/'blobs'/release['task']
        if sha(task_blob)!=release['task']:raise ValueError('task blob mismatch')
        task=json.loads(task_blob.read_text(encoding='utf-8'))
        if task!=json.loads(task_path.read_text(encoding='utf-8')):raise ValueError('task does not match release')
        wasm=state/'blobs'/release['wasm'];wat=state/'blobs'/release['wat']
        if sha(wasm)!=release['wasm'] or sha(wat)!=release['wat']:raise ValueError('candidate hash mismatch')
        request={'schema':1,'request_id':request_id,'task_sha256':release['task'],'release_id':release_id,
                 'wasm_sha256':release['wasm'],'evaluator_id':command_identity(command,project),
                 'task':{key:task[key] for key in ('id','goal','public_cases')},
                 'candidate':{'wat':wat.read_text(encoding='utf-8'),'wasm_path':str(wasm.resolve())},
                 'execution':{'controller':str(paths.release_binary(project).resolve()),'state':str(state.resolve())}}
        receipt['request']=request
        with tempfile.TemporaryFile() as stdin,tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
            stdin.write(json.dumps(request,ensure_ascii=False).encode());stdin.seek(0)
            proc=subprocess.Popen(command,cwd=project,stdin=stdin,stdout=stdout,stderr=stderr,shell=False,start_new_session=os.name!='nt')
            started=time.monotonic()
            try:
                while True:
                    if (state/'STOP').exists():raise RuntimeError('operator stop latched during external evaluation')
                    if os.fstat(stdout.fileno()).st_size>1048576 or os.fstat(stderr.fileno()).st_size>1048576:raise ValueError('evaluator output budget exceeded')
                    remaining=timeout-(time.monotonic()-started)
                    if remaining<=0:raise TimeoutError('external evaluator deadline exceeded')
                    try:proc.wait(timeout=min(.05,remaining));break
                    except subprocess.TimeoutExpired:pass
                stdout.seek(0);stderr.seek(0)
                raw=stdout.read(1048577);error=stderr.read(1048577)
                receipt.update(exit_code=proc.returncode,stderr=error.decode('utf-8',errors='replace'),elapsed_seconds=time.monotonic()-started)
                if len(raw)>1048576 or len(error)>1048576:raise ValueError('evaluator output budget exceeded')
                if proc.returncode!=0:raise ValueError('external evaluator exited unsuccessfully')
                if (state/'STOP').exists():raise RuntimeError('operator stop latched before evaluation acceptance')
                if command_identity(command,project)!=request['evaluator_id']:raise ValueError('evaluator or dataset changed during execution')
                response=parse_response(raw,request)
                receipt['response']=response
                receipt['status']='PASS' if response['fit_pass'] and response['regression_pass'] else 'REJECT'
            finally:
                if proc.poll() is None:
                    if os.name=='nt':
                        subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,timeout=10)
                    else:
                        try:os.killpg(proc.pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                    if proc.poll() is None:proc.kill()
                    proc.wait()
    except Exception as exc:
        receipt['error']=f'{type(exc).__name__}: {exc}'
    path=output_dir/(request_id+'.json')
    warnings=_atomic_create(path,(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode())
    return {**receipt,'receipt_path':str(path),'receipt_sha256':sha(path),'warnings':warnings}
