"""Exercise model retry boundaries through real loopback HTTP, never candidate execution."""
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
from unittest.mock import patch
import agent_runtime
import agent_session


class Provider(BaseHTTPRequestHandler):
    mode = ''
    requests = 0
    bodies = []
    def log_message(self, *_): pass
    def do_POST(self):
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        cls.requests += 1; cls.bodies.append(body)
        if cls.mode.startswith('rate') and (cls.requests == 1 or cls.mode == 'rate_always' or cls.mode == 'rate_after_write' and cls.requests >= 3):
            self.send_response(429); self.end_headers(); return
        if cls.mode in ('context', 'bad_request'):
            self.send_response(400); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'error': {'code': 'context_length_exceeded' if cls.mode == 'context' else 'invalid_request_error', 'message': 'private provider diagnostic'}}).encode()); return
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        def send(value):
            self.wfile.write(b'data: ' + (value if isinstance(value, bytes) else json.dumps(value).encode()) + b'\n\n'); self.wfile.flush()
        if cls.mode.startswith('disconnect') and (cls.requests == 1 or cls.mode == 'disconnect_always'):
            args = json.dumps({'path': 'main.py', 'content': 'never committed', 'expected_sha256': None})
            send({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'partial', 'type': 'function', 'function': {'name': 'write_file', 'arguments': args}}]}, 'finish_reason': None}]})
            return
        args = json.dumps({'path': 'main.py', 'content': '# exact single committed write\n', 'expected_sha256': None})
        if any(m['role'] == 'tool' for m in body['messages']):
            delta = {'role': 'assistant', 'content': 'done'}; finish = 'stop'
        else:
            delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'committed', 'type': 'function', 'function': {'name': 'write_file', 'arguments': args}}]}; finish = 'tool_calls'
        send({'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}); send(b'[DONE]')


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    results = {}
    try:
        env = {'AUTOFORGE_API_BASE': f'http://127.0.0.1:{server.server_port}/v1', 'AUTOFORGE_MODEL': 'retry-fixture', 'AUTOFORGE_API_KEY': ''}
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory(prefix='model-retry-') as td:
            for mode, requests, reason in [('rate_once', 3, 'model_stopped_without_acceptance'), ('disconnect_once', 3, 'model_stopped_without_acceptance'), ('rate_always', 2, 'rate_limit'), ('disconnect_always', 2, 'stream_disconnect'), ('context', 1, 'context_window'), ('bad_request', 1, 'transport')]:
                Provider.mode = mode; Provider.requests = 0; Provider.bodies = []
                out = Path(td) / mode
                report = agent_runtime.run({'schema': 1, 'goal': 'bounded model failure check', 'references': []}, [], ['never-dispatch-builder'], out, 6)
                assert report['reason_code'] == reason, (mode, report)
                assert Provider.requests == requests, (mode, Provider.requests)
                state = agent_session.load(out)
                events = [json.loads(line) for line in (out / 'events.jsonl').read_text().splitlines()]
                writes = [e for e in events if e['type'] == 'tool_result']
                if mode.endswith('once'):
                    assert [e['call_id'] for e in writes] == ['committed'], (mode, writes)
                    assert state['candidate']['files'] == [{'path': 'main.py', 'content': '# exact single committed write\n'}]
                    assert Provider.bodies[0]['messages'] == Provider.bodies[1]['messages']
                else:
                    assert not writes and state['candidate']['files'] == []
                assert state['inflight'] is None and not (out / 'revisions').exists()
                assert 'private provider diagnostic' not in (out / 'events.jsonl').read_text()
                results[mode] = {'requests': Provider.requests, 'reason_code': reason, 'committed_writes': len(writes)}
                if mode == 'rate_always':
                    agent_runtime.run({'schema': 1, 'goal': 'bounded model failure check', 'references': []}, [], ['never-dispatch-builder'], out, 6, resume=True)
                    assert Provider.requests == requests + 1, 'resume reset persisted retry allowance'
            Provider.mode = 'rate_after_write'; Provider.requests = 0; Provider.bodies = []
            out = Path(td) / 'rate-after-write'
            report = agent_runtime.run({'schema': 1, 'goal': 'bounded retry across mutation', 'references': []}, [], ['never-dispatch-builder'], out, 6)
            assert Provider.requests == 3 and report['reason_code'] == 'rate_limit', report
            assert agent_session.load(out)['candidate']['files'] == [{'path': 'main.py', 'content': '# exact single committed write\n'}]
            results['rate_after_write'] = {'requests': 3, 'reason_code': report['reason_code'], 'committed_writes': 1}
            for mode in ('rate_always', 'disconnect_always'):
                Provider.mode = mode; Provider.requests = 0; Provider.bodies = []
                out = Path(td) / (mode + '-budget')
                report = agent_runtime.run({'schema': 1, 'goal': 'bounded retry budget check', 'references': []}, [], ['never-dispatch-builder'], out, 1)
                assert Provider.requests == 1 and report['reason_code'] == 'model_call_budget', report
                assert agent_session.load(out)['candidate']['files'] == []
                results[mode + '_budget'] = {'requests': 1, 'reason_code': report['reason_code'], 'committed_writes': 0}
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=2)
    print(json.dumps({'status': 'PASS', 'scope': 'real HTTP fault injection, not model quality or candidate execution', 'checks': results}))


if __name__ == '__main__': main()
