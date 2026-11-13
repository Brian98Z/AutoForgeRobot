"""Opt-in Linux Docker lifecycle exercise using authored local container fixtures only.
Requires an explicitly authorized local image ID, deployment root, and acceptance evidence.
No network/paid service is used and the authored service source is never run on the host.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse,hashlib,json,tempfile
from pathlib import Path
import applications
import application_delivery as delivery
import application_operations as ops
ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'/'container_acceptance'
def canon(v):return json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--engine',choices=['docker'],required=True);p.add_argument('--image-id',required=True);p.add_argument('--deployment-root',type=Path,required=True);p.add_argument('--authorization',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--run-id',default='live');a=p.parse_args(argv)
 if not a.output.is_absolute() or not a.deployment_root.is_absolute():raise SystemExit('output/deployment-root must be absolute')
 auth=delivery._load(a.authorization); image=a.image_id
 if auth.get('deployment_root')!=str(a.deployment_root.resolve()) or image not in auth.get('image_ids',[]):raise SystemExit('authorization must bind supplied root and exact image ID')
 with tempfile.TemporaryDirectory(prefix='autoforge-release-fixture-') as td:
  source='''import json\nfrom http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\nclass H(BaseHTTPRequestHandler):\n def log_message(self,*a):pass\n def do_GET(self):\n  body={"ok":True} if self.path=="/health" else ({"value":7} if self.path=="/value" else {"error":"not_found"})\n  data=json.dumps(body).encode();self.send_response(200 if self.path in ("/health","/value") else 404);self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)\nThreadingHTTPServer(("0.0.0.0",8000),H).serve_forever()\n'''
  base=Path(td);candidate=base/'candidate';applications.stage_candidate({'schema':1,'goal':'trusted authored lifecycle fixture','references':[],'files':[{'path':'main.py','content':source}]},candidate)
  digest=json.loads((candidate/'manifest.json').read_text())['candidate_sha256'];validation={'schema':1,'status':'PASS','candidate_sha256':digest,'validator':'authored-runtime-fixture','scenarios':['health and immutable business invariant']};validation['validation_sha256']=hashlib.sha256(canon(validation)).hexdigest()
  pack=base/'sealed-pack';delivery.create_pack(candidate,validation,{'schema':1,'dependencies':[],'lock_sha256':hashlib.sha256(b'').hexdigest()},{'engine':'docker','image_id':image,'source':'explicit operator image ID'},pack)
  contract={'schema':1,'health_path':'/health','scenarios':[{'name':'invariant','requests':[{'method':'GET','path':'/value','body':None,'expected':{'status':200,'body':{'value':7}}}]}]}
  result=ops.deploy(pack,a.deployment_root,authorization=auth,request_id=f'{a.run_id}-shadow',image_id=image,engine=a.engine,command=['python','-I','-u','/candidate/main.py'],contract=contract,config={},phase='shadow')
  assert result['status']=='SHADOW' and result['measurements']['business_pass']
  first_promoted=ops.transition(a.deployment_root,authorization=auth,image_id=image,engine=a.engine,action='promote',request_id=f'{a.run_id}-promote-baseline')
  assert first_promoted['revision']==result['revision']
  second=ops.deploy(pack,a.deployment_root,authorization=auth,request_id=f'{a.run_id}-canary',image_id=image,engine=a.engine,command=['python','-I','-u','/candidate/main.py'],contract=contract,config={},phase='canary')
  assert second['status']=='CANARY' and second['measurements']['business_pass']
  second_promoted=ops.transition(a.deployment_root,authorization=auth,image_id=image,engine=a.engine,action='promote',request_id=f'{a.run_id}-promote')
  assert second_promoted['status']=='PROMOTED'
  try:ops.deploy(pack,a.deployment_root,authorization=auth,request_id=f'{a.run_id}-fault',image_id=image,engine=a.engine,command=['python','-I','-u','/candidate/main.py'],contract=contract,config={},phase='canary',fault_after=1)
  except ops.OperationsRefused as e:
   assert 'configured release stop condition triggered' in str(e),str(e)
  else:raise AssertionError('real post-readiness container fault did not trigger failure handling')
  rolled=ops.transition(a.deployment_root,authorization=auth,image_id=image,engine=a.engine,action='rollback',request_id=f'{a.run_id}-rollback')
  report={'schema':1,'status':'PASS','image_id':image,'shadow':result,'promote':second_promoted,'fault':'post-readiness SIGKILL; business probe observes failure and candidate is removed','rollback':rolled,'authored_fixture_only':True,'limitations':['Authored HTTP fixture has no credentials or migrations; it proves only this exact local runtime lifecycle.','Disposable deployment root is operator-supplied.','Rollback covers code/config pointer only; no database migration is executed.']}
  a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_bytes(canon(report)+b'\n');print(json.dumps({'status':'PASS','evidence':str(a.output),'revision':result['revision'],'fault':'observed','rollback':rolled['status']}));return 0
if __name__=='__main__':raise SystemExit(main())
