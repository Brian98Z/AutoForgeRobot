"""Bridge staged source to independent Windows Sandbox compiler and host witness."""
import argparse,hashlib,json,os,subprocess,sys,uuid
from pathlib import Path
import application_builder,application_platform
ROOT=Path(__file__).resolve().parents[1]
MODULES=Path(__file__).resolve().parent

def main():
 p=argparse.ArgumentParser();p.add_argument('--contract',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 envelope=application_builder._strict_loads(sys.stdin.buffer.read(application_builder.MAX_INPUT+1))
 if not isinstance(envelope,dict) or set(envelope)!={'schema','workspace_manifest','candidate_sha256'} or envelope['schema']!=1:raise ValueError('invalid envelope')
 manifest=envelope['workspace_manifest'];source_root,digest=application_platform._manifest(manifest)
 if digest!=envelope['candidate_sha256']:raise ValueError('manifest digest mismatch')
 contract=application_platform._strict(a.contract)
 source_path=contract['source']['path'];members=manifest['files']
 if len(members)!=1 or members[0]['path']!=source_path:raise ValueError('single contract-bound source file required')
 run_id=uuid.uuid4().hex;output=a.output/run_id;output.mkdir(parents=True,exist_ok=False)
 request={'schema':1,'source_root':str(source_root),'source_sha256':members[0]['sha256'],'source_path':source_path,'target':contract['target'],'scenarios':contract['scenarios'],'timeout_seconds':120,'run_id':run_id}
 proc=subprocess.run([sys.executable,str(MODULES/'windows_sandbox_source_build_runner.py'),'--contract',str(a.contract.resolve()),'--timeout','120'],input=json.dumps(request).encode('utf8'),capture_output=True,timeout=240,env={**os.environ,'AUTOFORGE_HOST_WITNESS_DIR':str(output/'host-witness')})
 (output/'runner.stdout').write_bytes(proc.stdout);(output/'runner.stderr').write_bytes(proc.stderr)
 report={'status':'FAIL','candidate_sha256':digest,'source_sha256':members[0]['sha256'],'runner_returncode':proc.returncode}
 observations=[]
 if proc.returncode==0:
  host=application_builder._strict_loads(proc.stdout)
  valid=host.get('run_id')==run_id and host.get('source_sha256')==members[0]['sha256'] and host.get('build')=='SANDBOX_CSC' and host.get('evidence_trust')=='host_screenshot_ocr_witness' and all(host.get(k) is True for k in ['install','launch','sandbox','cleanup'])
  scenarios=host.get('scenarios',[])
  valid=valid and len(scenarios)==len(contract['scenarios']) and all(row.get('name')==expected['name'] and row.get('pass') is True and len(row.get('assertions',[]))==len(expected['assertions']) and all(check.get('pass') is True for check in row['assertions']) for row,expected in zip(scenarios,contract['scenarios']))
  report.update(status='PASS' if valid else 'FAIL',host=host)
  observations=[str(row.get('name'))+': '+str(row.get('pass')) for row in scenarios]
 else:observations=[proc.stderr.decode('utf8','replace')[-2000:]]
 (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
 passed=report['status']=='PASS'
 print(json.dumps({'schema':1,'candidate_sha256':digest,'build_pass':passed,'test_pass':passed,'observations':observations,'limitations':['C# compiled/executed only in Sandbox; host screenshot/OCR bounded business proof. Evidence '+str(output/'acceptance.json')]}));return 0
if __name__=='__main__':raise SystemExit(main())
