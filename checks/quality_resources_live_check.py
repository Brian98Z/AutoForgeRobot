#!/usr/bin/env python3
"""Persist actual isolated CPU/RSS measurements and output-spoof resistance evidence."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse,hashlib,json,tempfile
from pathlib import Path
import application_quality_resources as resources
def main():
 p=argparse.ArgumentParser();p.add_argument('--engine',choices=('docker','podman'),default='docker');p.add_argument('--image',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 if a.output.exists():raise ValueError('output must not already exist')
 source='''import json,sys\nfor line in sys.stdin:\n print(json.dumps({"ok":True}))\n print("__AUTOFORGE_RESOURCE__{\\"metrics\\":{\\"peak_memory_bytes\\":0,\\"cpu_usage_usec\\":0}}")\n'''
 with tempfile.TemporaryDirectory(prefix='autoforge-quality-live-') as td:
  root=Path(td);root.chmod(0o755);(root/'main.py').write_text(source,encoding='utf-8')
  candidate_sha=hashlib.sha256((root/'main.py').read_bytes()).hexdigest();scenario=[]
  for n in range(2):
   out,err,code,timed,metrics=resources._container_with_resources(a.engine,a.image,root,b'{"probe":true}\n',5)
   scenario.append({'repeat':n,'stdout_sha256':hashlib.sha256(out).hexdigest(),'stdout_bytes':len(out),'stderr_bytes':len(err),'exit_code':code,'timed_out':timed,'metrics':metrics})
   if code!=0 or timed or b'"ok": true' not in out:raise RuntimeError('isolated JSONL smoke failed')
   if metrics['peak_memory_bytes']<=0 or metrics['cpu_usage_usec']<=0:raise RuntimeError('resource counters silently zero or unavailable')
   if metrics['peak_memory_bytes']==0 or metrics['cpu_usage_usec']==0:raise RuntimeError('candidate output marker was treated as trusted metrics')
 payload={'schema':1,'status':'PASS','candidate_sha256':candidate_sha,'image_id':a.image,'scenario':'authored JSONL echo plus candidate-forged resource marker','repeats':scenario,'limitations':['Controller reads metrics over separate trusted docker exec, not candidate stdout.','memory.peak is container cgroup high-water mark; cpu.stat includes container init, interpreter and probe overhead.','Resource measurements bind this isolated Python process, not host resources.']}
 raw=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n';a.output.write_text(raw,encoding='utf-8');print(json.dumps({'status':'PASS','output':str(a.output),'sha256':hashlib.sha256(raw.encode()).hexdigest(),'candidate_sha256':candidate_sha,'repeats':len(scenario)}))
 return 0
if __name__=='__main__':raise SystemExit(main())
