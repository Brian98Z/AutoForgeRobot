"""Exercise real idle HTTP stop, post-stop capability denial and terminal resume."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
import agent_runtime
import agent_session
import application_policy


class IdleProvider(BaseHTTPRequestHandler):
    arrived = threading.Event()
    release = threading.Event()
    requests = 0
    def log_message(self, *_): pass
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        type(self).requests += 1
        self.arrived.set(); self.release.wait(5)
        try:
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            call = {'index': 0, 'id': 'late-write', 'type': 'function', 'function': {'name': 'create_file', 'arguments': json.dumps({'path': 'main.py', 'content': '# must not commit\n'})}}
            response = {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [call]}, 'finish_reason': 'tool_calls'}]}
            self.wfile.write(b'data: ' + json.dumps(response).encode() + b'\n\ndata: [DONE]\n\n'); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), IdleProvider)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    captured = []; failures = []; timings = {}
    original_grant = application_policy.grant
    def grant(*args, **kwargs):
        cap = original_grant(*args, **kwargs); captured.append(cap); return cap
    try:
        with tempfile.TemporaryDirectory(prefix='stop-runtime-') as td, patch.dict(os.environ, {'AUTOFORGE_API_BASE': f'http://127.0.0.1:{server.server_port}/v1', 'AUTOFORGE_MODEL': 'idle-provider', 'AUTOFORGE_API_KEY': ''}), patch.object(application_policy, 'grant', side_effect=grant):
            out = Path(td) / 'session'
            def stop():
                try:
                    if not IdleProvider.arrived.wait(5): raise AssertionError('HTTP request not observed')
                    token = json.loads((out / '.control.json').read_text())['token']
                    timings['start'] = time.monotonic()
                    agent_session.enqueue_control(out, token, 'stop', request_id='actual-stop')
                except BaseException as exc: failures.append(str(exc))
                finally: IdleProvider.release.set()
            stopper = threading.Thread(target=stop); stopper.start()
            request = {'schema': 1, 'goal': 'do not execute late tool after stop', 'references': []}
            report = agent_runtime.run(request, [], ['never-builder'], out, 3)
            stopper.join(timeout=6)
            assert not failures and not stopper.is_alive(), failures
            timings['stop_return_seconds'] = time.monotonic() - timings['start']
            assert timings['stop_return_seconds'] < 3, timings
            assert report['task_status'] == 'CANCELLED' and report['tool_calls'] == 0, report
            state = agent_session.load(out)
            assert not state['candidate']['files'] and not state['inflight']
            for cap in captured:
                try: application_policy.authorize(cap, cap['session_id'], 'create_file')
                except application_policy.PolicyRefused: pass
                else: raise AssertionError('capability usable after stop')
            before = IdleProvider.requests; started = time.monotonic()
            resumed = agent_runtime.run(request, [], ['never-builder'], out, 3, resume=True)
            timings['terminal_resume_seconds'] = time.monotonic() - started
            assert resumed['task_status'] == 'CANCELLED' and IdleProvider.requests == before
            # Deterministic expiry boundary, no timing race or sleep.
            with patch.object(application_policy.time, 'monotonic', return_value=10):
                expiring = original_grant('expiry-session', ['read_file'], ttl_seconds=1)
            with patch.object(application_policy.time, 'monotonic', return_value=11):
                try: application_policy.authorize(expiring, 'expiry-session', 'read_file')
                except application_policy.PolicyRefused: pass
                else: raise AssertionError('expired dispatch accepted')
            application_policy.revoke(expiring)
    finally:
        IdleProvider.release.set(); server.shutdown(); server.server_close(); worker.join(timeout=2)
    timings.pop('start', None)
    print(json.dumps({'status': 'PASS', 'scope': 'real loopback HTTP/controller stop; local regression deadline 3 seconds, not production worst-case bound; no candidate executed', 'timings': timings, 'checks': ['late streamed write not committed', 'capability revoked after stop', 'terminal resume makes no request', 'expiry at exact boundary denied'], 'state_loss': 'no committed tool state lost; uncommitted model response discarded'}))


if __name__ == '__main__': main()
