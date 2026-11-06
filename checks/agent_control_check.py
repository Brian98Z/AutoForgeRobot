"""Authenticated control/event API regression checks for isolation and revocation."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json, os, subprocess, sys, tempfile, time
from pathlib import Path
import agent_session as S

def reject(fn):
    try:fn()
    except (ValueError, OSError):return
    raise AssertionError("expected authorization refusal")

def main():
  with tempfile.TemporaryDirectory(prefix="agent-control-") as td:
    root=Path(td);a=root/"a";b=root/"b";a.mkdir();b.mkdir()
    ta=S.create_control(a,"session-A",ttl_seconds=30);tb=S.create_control(b,"session-B",ttl_seconds=30)
    with S.EventJournal(a) as journal:
      journal.append({"type":"started","session_id":"session-A"})
      assert S.read_events(a,ta,after=0,limit=1)[0]["type"]=="started"
      reject(lambda:S.read_events(a,tb))
      reject(lambda:S.enqueue_control(a,tb,"stop"))
      reject(lambda:S.read_events(a,"wrong"))
      reject(lambda:S.read_events(a,ta,after=-1))
    state={"schema":1,"session_id":"session-A","task_status":"SUCCEEDED"}
    (a/"session.json").write_text("{}",encoding="utf-8")
    original=S.load
    try:
      S.load=lambda output:state
      S.authenticate(a,ta)
      reject(lambda:S.authenticate(a,ta,require_active=True))
      assert S.read_events(a,ta,after=0,limit=10)
      for terminal in ("FAILED","CANCELLED"):
        state["task_status"]=terminal
        reject(lambda:S.enqueue_control(a,ta,"pause"))
    finally:S.load=original
    cap=S._control_record(a);cap["expires_at"]=int(time.time())-1
    from application_checkpoint import _atomic,canonical
    _atomic(a/".control.json",canonical(cap)+b"\n")
    reject(lambda:S.read_events(a,ta))
    # Actual CLI event-read route must reject missing/invalid token and accept exact token.
    cap["expires_at"]=int(time.time())+30;_atomic(a/".control.json",canonical(cap)+b"\n")
    (a/"session.json").unlink()
    env=dict(os.environ);env["AGENT_SESSION_CONTROL_TOKEN"]=tb
    cmd=[sys.executable,str(Path(__file__).resolve().parents[1] / "autoforge" / "agent_session.py"),"events",str(a)]
    bad=subprocess.run(cmd,capture_output=True,text=True,env=env,timeout=5)
    assert bad.returncode!=0
    env["AGENT_SESSION_CONTROL_TOKEN"]=ta
    good=subprocess.run(cmd,capture_output=True,text=True,env=env,timeout=5)
    assert good.returncode==0 and json.loads(good.stdout)[0]["seq"]==1
  print("agent_control_check: PASS (live journal read, cross-session, invalid, expiry, terminal write revocation, CLI token gate)")
if __name__=="__main__":main()
