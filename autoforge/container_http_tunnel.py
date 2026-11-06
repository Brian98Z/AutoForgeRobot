"""Host-loopback TCP transport to an isolated container through engine exec pipes."""
from __future__ import annotations
import socket, socketserver, subprocess, threading

_CLIENT = '''import socket,sys,threading
s=socket.create_connection(('127.0.0.1',8000),timeout=2)
s.settimeout(5)
def send():
 try:
  while True:
   b=sys.stdin.buffer.read1(65536)
   if not b:break
   s.sendall(b)
 except OSError:pass
 finally:
  try:s.shutdown(socket.SHUT_WR)
  except OSError:pass
t=threading.Thread(target=send,daemon=True);t.start()
try:
 while True:
  b=s.recv(65536)
  if not b:break
  sys.stdout.buffer.write(b);sys.stdout.buffer.flush()
except OSError:pass
finally:s.close()
'''

class Tunnel:
    def __init__(self,engine:str,container:str,env:dict[str,str]):
        self.engine=engine;self.container=container;self.env=env
        self.lock=threading.Lock();self.processes=set();self.closing=False
        owner=self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                with owner.lock:
                    if owner.closing:return
                    proc=subprocess.Popen([engine,'exec','-i',container,'python','-I','-u','-c',_CLIENT],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,bufsize=0,close_fds=True)
                    owner.processes.add(proc)
                def send():
                    try:
                        while True:
                            data=self.request.recv(65536)
                            if not data:break
                            proc.stdin.write(data);proc.stdin.flush()
                    except (OSError,ValueError):pass
                    finally:
                        try:proc.stdin.close()
                        except OSError:pass
                worker=threading.Thread(target=send,daemon=True);worker.start()
                timer=threading.Timer(15,lambda:proc.kill() if proc.poll() is None else None);timer.start()
                try:
                    while True:
                        data=proc.stdout.read(65536)
                        if not data:break
                        self.request.sendall(data)
                except OSError:pass
                finally:
                    timer.cancel()
                    try:self.request.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    if proc.poll() is None:proc.kill()
                    proc.wait(timeout=3);worker.join(timeout=6)
                    proc.stdout.close()
                    with owner.lock:owner.processes.discard(proc)
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads=False
            block_on_close=True
        self.server=Server(('127.0.0.1',0),Handler)
        self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def close(self):
        with self.lock:
            self.closing=True
            for proc in self.processes:
                if proc.poll() is None:proc.kill()
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
