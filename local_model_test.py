"""Run an explicitly supplied model server and test command with owned cleanup."""
from __future__ import annotations
import argparse,json,math,socket,subprocess,time
from urllib.request import urlopen

def run(server_argv,test_argv,*,port=8082,startup_timeout=90,test_timeout=600):
    for argv in (server_argv,test_argv):
        if not isinstance(argv,list) or not argv or any(not isinstance(x,str) or not x for x in argv):raise ValueError('commands must be nonempty JSON argv arrays')
    if type(port)is not int or not 1<=port<=65535 or any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (startup_timeout,test_timeout)):raise ValueError('invalid port or deadline')
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1',port))==0:raise ValueError('port already occupied; refusing to adopt or stop an existing server')
    server=subprocess.Popen(server_argv,stdin=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+startup_timeout
        while True:
            if server.poll() is not None:raise RuntimeError('owned model server exited before readiness')
            try:
                with urlopen(f'http://127.0.0.1:{port}/v1/models',timeout=1) as response:
                    data=json.loads(response.read(1048577))
                if isinstance(data,dict) and isinstance(data.get('data'),list) and data['data']:break
            except (OSError,ValueError):pass
            if time.monotonic()>=deadline:raise TimeoutError('owned model server readiness deadline exceeded')
            time.sleep(.1)
        test=subprocess.Popen(test_argv,stdin=subprocess.DEVNULL)
        try:return test.wait(timeout=test_timeout)
        finally:
            if test.poll() is None:
                test.terminate()
                try:test.wait(timeout=5)
                except subprocess.TimeoutExpired:test.kill();test.wait(timeout=5)
    finally:
        if server.poll() is None:
            server.terminate()
            try:server.wait(timeout=5)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server-argv',required=True,help='JSON array; server must bind only loopback at --port')
    p.add_argument('--test-argv',required=True,help='JSON array; no shell evaluation')
    p.add_argument('--port',type=int,default=8082)
    p.add_argument('--startup-timeout',type=float,default=90)
    p.add_argument('--test-timeout',type=float,default=600)
    a=p.parse_args()
    try:return run(json.loads(a.server_argv),json.loads(a.test_argv),port=a.port,startup_timeout=a.startup_timeout,test_timeout=a.test_timeout)
    except (ValueError,OSError,RuntimeError,TimeoutError,subprocess.TimeoutExpired) as exc:
        print(json.dumps({'status':'HOLD','reason':str(exc)}));return 2
if __name__=='__main__':raise SystemExit(main())
