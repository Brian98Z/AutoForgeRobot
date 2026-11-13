#!/usr/bin/env python3
"""Run isolated Python JSONL and read resource counters through trusted engine exec."""
from __future__ import annotations
import json, os, subprocess, uuid
from pathlib import Path
import application_builder as builder
import container_env

class ResourceProbeUnavailable(RuntimeError): pass

_CANDIDATE = r'''import runpy,signal,sys,traceback
signal.alarm(int(sys.argv[1]));sys.path.insert(0,"/candidate")
try:runpy.run_path("/candidate/main.py",run_name="__main__");code=0
except SystemExit as e:code=e.code if isinstance(e.code,int) else 1
except BaseException:traceback.print_exc(file=sys.stderr);code=1
sys.exit(code if isinstance(code,int) else 1)
'''
_METRICS = r'''import json
base="/sys/fs/cgroup/"
def read(name):
 with open(base+name,encoding="ascii") as f:return f.read()
peak=int(read("memory.peak").strip())
stat={k:int(v) for line in read("cpu.stat").splitlines() for k,v in [line.split()] if k in ("usage_usec","user_usec","system_usec")}
print(json.dumps({"schema":1,"peak_memory_bytes":peak,"cpu_usage_usec":stat["usage_usec"],"user_cpu_usec":stat.get("user_usec"),"system_cpu_usec":stat.get("system_usec")},sort_keys=True,separators=(",",":")))
'''
_IDLE = 'import time;time.sleep(120)'

def _run(argv, *, timeout, data=None, limit=builder.MAX_OUT):
    import threading
    proc=subprocess.Popen(argv,stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=container_env.engine_env(),close_fds=True,bufsize=0)
    out=bytearray();err=bytearray();overflow=[False]
    def drain(stream,target,cap):
        while True:
            chunk=stream.read(65536)
            if not chunk:break
            if len(target)+len(chunk)>cap:overflow[0]=True
            if len(target)<cap:target.extend(chunk[:cap-len(target)])
    threads=[threading.Thread(target=drain,args=(proc.stdout,out,limit),daemon=True),threading.Thread(target=drain,args=(proc.stderr,err,builder.MAX_ERR),daemon=True)]
    for thread in threads:thread.start()
    try:
        if data is not None:
            import threading
            def send():
                try:proc.stdin.write(data);proc.stdin.close()
                except (BrokenPipeError,OSError):pass
            writer=threading.Thread(target=send,daemon=True);writer.start()
        try:proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait(timeout=5);raise ResourceProbeUnavailable('trusted isolated command timed out')
        if data is not None:
            writer.join(timeout=2)
            if writer.is_alive():raise ResourceProbeUnavailable('candidate input writer did not complete')
        for thread in threads:thread.join(timeout=2)
        if overflow[0] or any(thread.is_alive() for thread in threads):raise ResourceProbeUnavailable('resource runner output bound exceeded')
        return subprocess.CompletedProcess(argv,proc.returncode,bytes(out),bytes(err))
    finally:
        if proc.poll() is None:proc.kill();proc.wait(timeout=5)
def _container_with_resources(engine:str,image:str,root:Path,payload:bytes,timeout:int,*,runtime='python'):
    if runtime!='python':raise ResourceProbeUnavailable('resource probe supports Python candidates only')
    if len(payload)>builder.MAX_INPUT:raise builder.BuildFailure('container request payload exceeds byte limit')
    name='autoforge-quality-'+uuid.uuid4().hex
    base=[engine,'run','-d','--name',name,'--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=64','--memory=256m','--cpus=1','--mount',f'type=bind,src={root},dst=/candidate,readonly','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777','--workdir','/candidate','--entrypoint','python',image,'-I','-c',_IDLE]
    started=subprocess.run(base,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=False,env=container_env.engine_env(),close_fds=True)
    if started.returncode!=0:raise ResourceProbeUnavailable('isolated resource container failed to start')
    cid=started.stdout.decode('ascii','strict').strip()
    if not cid:raise ResourceProbeUnavailable('container engine returned no resource container identity')
    try:
        cmd=[engine,'exec','-i',cid,'python','-I','-c',_CANDIDATE,str(timeout)]
        try: candidate=_run(cmd,timeout=timeout+5,data=payload)
        except ResourceProbeUnavailable as exc:raise ResourceProbeUnavailable(f'candidate resource execution unavailable: {exc}') from exc
        except subprocess.TimeoutExpired as exc:raise ResourceProbeUnavailable('candidate resource execution exceeded bounded timeout') from exc
        metrics_proc=_run([engine,'exec',cid,'python','-I','-c',_METRICS],timeout=5,limit=65536)
        if metrics_proc.returncode!=0:raise ResourceProbeUnavailable('controller could not read cgroup-v2 resource counters')
        try:metrics=json.loads(metrics_proc.stdout)
        except (ValueError,UnicodeError) as exc:raise ResourceProbeUnavailable('malformed controller cgroup metrics') from exc
        if not isinstance(metrics,dict) or set(metrics)!={'schema','peak_memory_bytes','cpu_usage_usec','user_cpu_usec','system_cpu_usec'} or metrics['schema']!=1 or any(type(metrics[k]) is not int or metrics[k]<0 for k in ('peak_memory_bytes','cpu_usage_usec')):
            raise ResourceProbeUnavailable('invalid controller cgroup counters')
        return candidate.stdout,candidate.stderr,candidate.returncode,False,{**metrics,'source':'trusted engine exec reading cgroup-v2 memory.peak and cpu.stat; includes idle init and measurement commands'}
    finally:
        subprocess.run([engine,'rm','-f',cid],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8,check=False,env=container_env.engine_env(),close_fds=True)
