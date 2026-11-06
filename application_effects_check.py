"""Real localhost HTTP peer effect lifecycle checks; no provider or payment call."""
from __future__ import annotations
import json, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import application_effects as E

class Peer:
    def __init__(self):
        self.rows={};self.fail_after_commit=False;outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))));rid=body["request_id"]
                outer.rows.setdefault(rid,{"request_id":rid,"state":"SUCCEEDED","payload":body["payload"]})
                if outer.fail_after_commit:
                    outer.fail_after_commit=False;self.close_connection=True;return
                self.respond(200,outer.rows[rid])
            def do_GET(self):
                rid=self.path.removeprefix("/effects/");self.respond(200,outer.rows.get(rid,{"request_id":rid,"state":"NOT_FOUND"}))
            def respond(self,status,obj):
                data=json.dumps(obj).encode();self.send_response(status);self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.endpoint=f"http://127.0.0.1:{self.server.server_port}"
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

def main():
    peer=Peer()
    try:
        with tempfile.TemporaryDirectory() as td:
            ledger=Path(td)/"effects.json";deadline=int(time.time())+30
            E.prepare(ledger,request_id="lost-response",effect_class="idempotent",endpoint=peer.endpoint,method="POST",resource="/items",payload={"quantity":2},scope="test-session",deadline=deadline)
            peer.fail_after_commit=True
            assert E.execute(ledger,"lost-response")["state"]=="UNKNOWN"
            assert E.execute(ledger,"lost-response")["attempts"]==1
            observation=E.query_peer_state(ledger,"lost-response");assert observation["state"]=="SUCCEEDED"
            crash=E.prepare(ledger,request_id="crashed",effect_class="idempotent",endpoint=peer.endpoint,method="POST",resource="/items",payload={"quantity":3},scope="test-session",deadline=deadline)
            state=E._load(ledger);state["requests"]["crashed"]["state"]="INFLIGHT";E._save(ledger,state)
            assert E.recover_inflight(ledger)["recovered_unknown"]==["crashed"] and E.execute(ledger,"crashed")["attempts"]==0
            assert E.reconcile_from_peer(ledger,"lost-response",observation=observation,operator="operator",source="peer query")["state"]=="SUCCEEDED"
            assert len(peer.rows)==1
            try:E.prepare(ledger,request_id="lost-response",effect_class="idempotent",endpoint=peer.endpoint,method="POST",resource="/items",payload={"quantity":9},scope="test-session",deadline=deadline)
            except E.EffectRefused:pass
            else:raise AssertionError("request ID rebound to a different effect")
            try:E.prepare(ledger,request_id="never",effect_class="irreversible",endpoint=peer.endpoint,method="POST",resource="/items",payload={},scope="s",deadline=deadline)
            except E.EffectRefused:pass
            else:raise AssertionError("irreversible effect accepted")
        print("application_effects_check: PASS (real loopback peer, lost response, state query, no replay, durable recovery, irreversible refusal)")
    finally:peer.close()
if __name__=="__main__":main()
