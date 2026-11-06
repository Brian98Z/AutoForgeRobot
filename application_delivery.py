"""Authorized local application packaging and immutable deployment lifecycle.

Candidate code is data only. The only executed argv is an explicitly trusted
operator health adapter; running candidates requires an external verified
container controller and is intentionally not implemented here.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, stat, subprocess, sys, tempfile, time, uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable
import applications
import application_checkpoint as checkpoint

class DeliveryRefused(ValueError): pass


def _json(value: Any) -> bytes: return checkpoint.canonical(value)
def _sha(data: bytes) -> str: return checkpoint.digest(data)
def _atomic(path: Path, data: bytes) -> None: checkpoint._atomic(path, data)
def _chain(path: Path) -> None:
    try: checkpoint.regular_chain(path)
    except Exception as exc: raise DeliveryRefused(str(exc)) from exc

def _load(path: Path, limit: int = 8*1024*1024) -> Any:
    _chain(path)
    try:
        with path.open('rb') as f: raw=f.read(limit+1)
        if len(raw)>limit: raise DeliveryRefused('input exceeds size limit')
        return json.loads(raw.decode('utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise DeliveryRefused(f'invalid JSON input: {exc}') from exc

def _write_json(path: Path, value: Any) -> None: _atomic(path, _json(value)+b'\n')

def _tree(root: Path) -> list[dict[str, Any]]:
    _chain(root)
    if not root.is_dir(): raise DeliveryRefused('pack root must be a directory')
    records=[]
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        here=Path(current)
        for name in list(dirs)+names:
            p=here/name; st=p.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400: raise DeliveryRefused('pack contains link/reparse point')
        dirs.sort()
        for name in sorted(names):
            p=here/name
            if p.parent==root and p.name in {'delivery.json','inventory.json'}: continue
            st=p.lstat()
            if not stat.S_ISREG(st.st_mode): raise DeliveryRefused('pack contains non-regular file')
            rel=p.relative_to(root).as_posix()
            try: applications._validate_path(rel)
            except applications.Refused as exc: raise DeliveryRefused(str(exc)) from exc
            b=p.read_bytes(); records.append({'path':rel,'size':len(b),'sha256':_sha(b)})
    return records

def _inventory(records: list[dict[str,Any]]) -> dict[str,Any]:
    ordered=sorted(records,key=lambda x:x['path'])
    return {'schema':1,'files':ordered,'inventory_sha256':_sha(_json(ordered))}

def _read_candidate(root: Path) -> tuple[dict[str,Any],dict[str,bytes],str]:
    manifest=_load(root/'manifest.json')
    if not isinstance(manifest,dict) or manifest.get('schema')!=1 or not isinstance(manifest.get('files'),list): raise DeliveryRefused('schema-1 candidate manifest required')
    files={}
    for row in manifest['files']:
        if not isinstance(row,dict) or set(row)!={'path','size','sha256'}: raise DeliveryRefused('candidate file record malformed')
        rel=applications._validate_path(row['path']); p=root.joinpath(*rel.split('/')); _chain(p)
        try: b=p.read_bytes()
        except OSError as exc: raise DeliveryRefused(f'candidate file unavailable: {rel}') from exc
        if len(b)!=row['size'] or _sha(b)!=row['sha256']: raise DeliveryRefused(f'candidate tamper detected: {rel}')
        try: text=b.decode('utf-8')
        except UnicodeDecodeError as exc: raise DeliveryRefused(f'candidate file is not UTF-8: {rel}') from exc
        files[rel]=b
    candidate={'schema':1,'goal':manifest.get('goal'),'references':manifest.get('references'),'files':[{'path':p,'content':files[p].decode('utf-8')} for p in sorted(files)]}
    clean,_=applications.validate_candidate(candidate)
    expected={'schema':1,'goal':clean['goal'],'references':clean['references'],'files':[{'path':p,'size':len(files[p]),'sha256':_sha(files[p])} for p in sorted(files)]}
    digest=_sha(_json(expected))
    if manifest.get('candidate_sha256')!=digest: raise DeliveryRefused('candidate digest mismatch')
    return clean,files,digest

def validate_pack(pack: Path) -> dict[str,Any]:
    """Independently re-read all bytes, candidate and evidence bindings."""
    pack=Path(pack).absolute(); _chain(pack)
    metadata=_load(pack/'delivery.json')
    if not isinstance(metadata, dict) or type(metadata.get('schema')) is not int or metadata['schema'] != 1: raise DeliveryRefused('delivery metadata malformed')
    candidate,files,candidate_digest=_read_candidate(pack/'candidate')
    _candidate_members(pack/'candidate',files)
    if metadata.get('candidate_sha256')!=candidate_digest: raise DeliveryRefused('delivery candidate binding mismatch')
    evidence=_load(pack/'evidence.json')
    if not isinstance(evidence,dict) or evidence.get('schema')!=1: raise DeliveryRefused('validation evidence malformed')
    if evidence.get('candidate_sha256')!=candidate_digest or evidence.get('status')!='PASS' or not isinstance(evidence.get('validation_sha256'),str): raise DeliveryRefused('passing validation evidence must bind exact candidate digest')
    unsigned={k:v for k,v in evidence.items() if k!='validation_sha256'}
    if evidence['validation_sha256']!=_sha(_json(unsigned)): raise DeliveryRefused('validation evidence digest mismatch')
    inventory=_inventory(_tree(pack))
    if metadata.get('inventory')!=inventory: raise DeliveryRefused('package inventory/tamper mismatch')
    unsigned_metadata={k:v for k,v in metadata.items() if k!='metadata_sha256'}
    if metadata.get('metadata_sha256')!=_sha(_json(unsigned_metadata)): raise DeliveryRefused('package metadata digest mismatch')
    package_schema = metadata.get('maintenance_package_schema', 1)
    if type(package_schema) is not int or package_schema not in (1, 2):
        raise DeliveryRefused('unsupported maintenance package schema')
    if package_schema == 1:
        if 'upgrade_provenance_sha256' in metadata or (pack / 'maintenance' / 'UPGRADE.json').exists():
            raise DeliveryRefused('legacy package contains unversioned upgrade data')
    else:
        if metadata.get('maintenance_bundle_required') is not True:
            raise DeliveryRefused('maintenance format 2 requires a bundled contract')
        provenance = _load(pack / 'maintenance' / 'UPGRADE.json')
        required_provenance = {'schema', 'source_delivery_schema', 'source_metadata_sha256', 'source_inventory_sha256', 'candidate_sha256', 'transformed_documentation'}
        if not isinstance(provenance, dict) or set(provenance) != required_provenance or type(provenance.get('schema')) is not int or provenance.get('schema') != 2 or type(provenance.get('source_delivery_schema')) is not int or provenance.get('source_delivery_schema') != 1:
            raise DeliveryRefused('upgrade provenance malformed')
        for key in ('source_metadata_sha256', 'source_inventory_sha256', 'candidate_sha256'):
            value = provenance.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise DeliveryRefused('upgrade provenance digest malformed')
        if provenance['candidate_sha256'] != candidate_digest or metadata.get('upgrade_provenance_sha256') != _sha(_json(provenance)):
            raise DeliveryRefused('upgrade provenance binding mismatch')
        source_path = pack / 'maintenance' / 'UPGRADE-SOURCE-delivery.json'
        source_metadata = _load(source_path)
        if _sha(source_path.read_bytes()) != provenance['source_metadata_sha256']:
            raise DeliveryRefused('upgrade source metadata backup digest mismatch')
        old_unsigned = {k: v for k, v in source_metadata.items() if k != 'metadata_sha256'}
        if type(source_metadata.get('schema')) is not int or source_metadata['schema'] != 1 or source_metadata.get('metadata_sha256') != _sha(_json(old_unsigned)):
            raise DeliveryRefused('upgrade source metadata backup is not valid schema 1')
        if source_metadata.get('inventory', {}).get('inventory_sha256') != provenance['source_inventory_sha256'] or source_metadata.get('candidate_sha256') != candidate_digest:
            raise DeliveryRefused('upgrade source inventory/candidate provenance mismatch')
        expected_files = {'README.md': 'maintenance/v2/archive-v1/docs/README.md', 'API.md': 'maintenance/v2/archive-v1/docs/API.md', 'CONFIG.example.json': 'maintenance/v2/archive-v1/config/CONFIG.example.json', 'LICENSES.md': 'maintenance/v2/archive-v1/docs/LICENSES.md', 'LIMITATIONS.md': 'maintenance/v2/archive-v1/docs/LIMITATIONS.md', 'CONTRACT.json': 'maintenance/v2/contracts/jsonl-v1.json', 'requirements.lock': 'maintenance/v2/archive-v1/runtime/requirements.lock'}
        source_rows = {row['path']: row for row in source_metadata.get('inventory', {}).get('files', []) if isinstance(row, dict) and isinstance(row.get('path'), str)}
        for old_name, new_name in expected_files.items():
            row = source_rows.get(f'maintenance/{old_name}')
            target = pack / new_name
            if row is None or row.get('size') != target.stat().st_size or row.get('sha256') != _sha(target.read_bytes()):
                raise DeliveryRefused('maintenance format-2 content is not bound to schema-1 source inventory')
        contract = _load(pack / 'maintenance' / 'v2' / 'contracts' / 'jsonl-v1.json', 2 * 1024 * 1024)
        old_inventory = source_metadata.get('inventory')
        if not isinstance(old_inventory, dict) or set(old_inventory) != {'schema', 'files', 'inventory_sha256'} or old_inventory.get('schema') != 1 or not isinstance(old_inventory.get('files'), list):
            raise DeliveryRefused('upgrade source inventory malformed')
        old_rows = old_inventory['files']
        if any(not isinstance(row, dict) or set(row) != {'path', 'size', 'sha256'} or not isinstance(row.get('path'), str) or type(row.get('size')) is not int or row['size'] < 0 or not isinstance(row.get('sha256'), str) or len(row['sha256']) != 64 for row in old_rows):
            raise DeliveryRefused('upgrade source inventory rows malformed')
        if len({row['path'] for row in old_rows}) != len(old_rows) or old_inventory.get('inventory_sha256') != _sha(_json(sorted(old_rows, key=lambda row: row['path']))):
            raise DeliveryRefused('upgrade source inventory digest/uniqueness invalid')
        migrated = {old: new for old, new in expected_files.items()}
        current_by_path = {row['path']: row for row in inventory['files']}
        for row in old_rows:
            old_path = row['path']
            new_path = migrated.get(old_path.removeprefix('maintenance/')) if old_path.startswith('maintenance/') else old_path
            current = current_by_path.get(new_path)
            if current is None or current['size'] != row['size'] or current['sha256'] != row['sha256']:
                raise DeliveryRefused('schema-2 payload differs from complete schema-1 source inventory')
        if not isinstance(contract, dict) or type(contract.get('schema')) is not int or contract.get('schema') != 1 or contract.get('entrypoint') != 'main.py' or not isinstance(contract.get('scenarios'), list):
            raise DeliveryRefused('maintenance format-2 JSONL contract malformed')
        format_doc = _load(pack / 'maintenance' / 'v2' / 'format.json')
        if format_doc != {'schema': 2, 'contract_schema': 1, 'files': expected_files, 'operator_entrypoint': 'maintenance/v2/README.md', 'archived_v1_docs_are_non_authoritative': True} or any(not (pack / target).is_file() for target in expected_files.values()):
            raise DeliveryRefused('maintenance format-2 layout declaration/payload malformed')
        doc_info = provenance['transformed_documentation']
        if not isinstance(doc_info, dict) or set(doc_info) != {'archive_root', 'archive_bytes_preserved', 'entrypoint', 'entrypoint_sha256'} or doc_info.get('archive_root') != 'maintenance/v2/archive-v1/' or doc_info.get('archive_bytes_preserved') is not True or doc_info.get('entrypoint') != 'maintenance/v2/README.md':
            raise DeliveryRefused('maintenance documentation migration provenance malformed')
        entrypoint = pack / 'maintenance' / 'v2' / 'README.md'
        if _sha(entrypoint.read_bytes()) != doc_info.get('entrypoint_sha256') or b'archive-v1/' not in entrypoint.read_bytes() or b'contracts/jsonl-v1.json' not in entrypoint.read_bytes():
            raise DeliveryRefused('maintenance format-2 operator entrypoint invalid')
        if any((pack / 'maintenance' / name).exists() for name in expected_files) or metadata.get('maintenance_bundle_required') is not True:
            raise DeliveryRefused('maintenance format-2 has legacy layout or no bundled contract')
    required_maintenance={'README.md','API.md','CONFIG.example.json','LICENSES.md','LIMITATIONS.md','CONTRACT.json','requirements.lock'}
    if metadata.get('maintenance_bundle_required') is True and package_schema == 1 and any(not (pack/'maintenance'/name).is_file() for name in required_maintenance):raise DeliveryRefused('self-contained maintenance bundle is incomplete')
    if metadata.get('dependency_metadata') is None or metadata.get('engine_metadata') is None: raise DeliveryRefused('dependency and engine metadata required')
    return {'status':'VALID','schema':1,'maintenance_package_schema':package_schema,'candidate_sha256':candidate_digest,'validation_sha256':evidence['validation_sha256'],'inventory':inventory,'files':len(files)}

def _candidate_payload(candidate:dict[str,Any], files:dict[str,bytes], digest:str)->dict[str,Any]:
    return {'schema':1,'goal':candidate['goal'],'references':candidate['references'],'files':[{'path':p,'size':len(files[p]),'sha256':_sha(files[p])} for p in sorted(files)],'candidate_sha256':digest}

def _candidate_members(root:Path, files:dict[str,bytes])->None:
    allowed={'manifest.json',*files.keys()}
    actual=set()
    for current,dirs,names in os.walk(root,topdown=True,followlinks=False):
        here=Path(current)
        for name in list(dirs)+names:
            p=here/name; st=p.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400: raise DeliveryRefused('candidate contains link/reparse point')
        for name in names:
            actual.add((here/name).relative_to(root).as_posix())
    if actual!=allowed: raise DeliveryRefused('candidate has unlisted or missing files')

def create_pack(candidate_dir: Path, validation: dict[str,Any], dependency_metadata: dict[str,Any], engine_metadata: dict[str,Any], output: Path, *, maintenance_files:dict[str,str]|None=None) -> dict[str,Any]:
    """Create deterministic package from a staged schema-1 candidate plus exact PASS evidence."""
    candidate_dir=Path(candidate_dir).absolute(); output=Path(output).absolute()
    candidate,files,digest=_read_candidate(candidate_dir)
    if not isinstance(validation,dict) or validation.get('status')!='PASS' or validation.get('candidate_sha256')!=digest: raise DeliveryRefused('validation must be PASS for exact candidate digest')
    if 'schema' in validation and validation['schema']!=1: raise DeliveryRefused('validation evidence schema must be 1')
    # Preserve and seal the full report; schema is the explicit package envelope.
    evidence={**validation,'schema':1}
    evidence.pop('validation_sha256',None)
    evidence['validation_sha256']=_sha(_json(evidence))
    if not isinstance(dependency_metadata,dict) or not isinstance(engine_metadata,dict): raise DeliveryRefused('dependency and engine metadata must be objects')
    _chain(output)
    if output.exists(): raise DeliveryRefused('package destination must be absent')
    output.parent.mkdir(parents=True,exist_ok=True)
    temp=Path(tempfile.mkdtemp(prefix=f'.{output.name}.pack-',dir=output.parent))
    try:
        _candidate_members(candidate_dir,files)
        candidate_root=temp/'candidate'; candidate_root.mkdir()
        for rel,data in files.items():
            target=candidate_root.joinpath(*rel.split('/')); target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
        _write_json(candidate_root/'manifest.json',_candidate_payload(candidate,files,digest))

        _write_json(temp/'evidence.json',evidence)
        if maintenance_files is not None:
            if not isinstance(maintenance_files,dict) or set(maintenance_files)!={'README.md','API.md','CONFIG.example.json','LICENSES.md','LIMITATIONS.md','CONTRACT.json','requirements.lock'}:raise DeliveryRefused('maintenance bundle requires README/API/config/license/limits/contract/lock files')
            for rel,text in maintenance_files.items():
                try:rel=applications._validate_path('maintenance/'+rel)
                except applications.Refused as exc:raise DeliveryRefused(str(exc)) from exc
                if not isinstance(text,str) or len(text.encode('utf-8'))>131072:raise DeliveryRefused('maintenance text invalid or exceeds 128 KiB')
                target=temp.joinpath(*rel.split('/'));target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text,encoding='utf-8',newline='\n')
        inventory=_inventory(_tree(temp))
        metadata={'schema':1,'candidate_sha256':digest,'dependency_metadata':dependency_metadata,'engine_metadata':engine_metadata,'maintenance_bundle_required':maintenance_files is not None,'inventory':inventory}
        metadata['metadata_sha256']=_sha(_json(metadata))
        _write_json(temp/'delivery.json',metadata)
        # inventory excludes control files, and records every package payload byte.
        if output.exists(): raise DeliveryRefused('package destination appeared during creation')
        temp.rename(output)
    except BaseException:
        shutil.rmtree(temp,ignore_errors=True); raise
    return validate_pack(output)
def _request_id(request_id:str)->str:
    if not isinstance(request_id,str) or not request_id or len(request_id)>128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in request_id): raise DeliveryRefused('invalid request ID')
    return request_id

def _approval(approval: dict[str,Any], deployment_root: Path, candidate_type: str) -> None:
    required={'schema','approved','deployment_root','candidate_types','scopes'}
    if not isinstance(approval,dict) or set(approval)!=required or approval.get('schema')!=1 or approval.get('approved') is not True: raise DeliveryRefused('explicit deployment authorization is required')
    root=str(deployment_root.resolve())
    if approval['deployment_root']!=root: raise DeliveryRefused('authorization is not bound to exact deployment root')
    if candidate_type not in approval['candidate_types'] or not {'deploy','health_check','pointer_switch','rollback'}<=set(approval['scopes']): raise DeliveryRefused('authorization lacks candidate type or required scopes')

def _record(path: Path, request_id: str, value: dict[str,Any]) -> None:
    request_id=_request_id(request_id)
    _chain(path); path.mkdir(parents=True,exist_ok=True)
    target=path/f'{request_id}.json'
    if target.exists(): raise DeliveryRefused('request ID already exists; reconcile instead of replay')
    _write_json(target,value)

def _ledger(path: Path, request_id: str) -> dict[str,Any]:
    request_id=_request_id(request_id)
    value=_load(path/f'{request_id}.json')
    if not isinstance(value,dict) or value.get('request_id')!=request_id: raise DeliveryRefused('ledger record mismatch')
    return value

def reconcile(deployment_root:Path,request_id:str,evidence:dict[str,Any])->dict[str,Any]:
    root=Path(deployment_root).absolute(); _chain(root); root.mkdir(parents=True,exist_ok=True)
    try:
        with checkpoint.ExclusiveLock(root/'.delivery.lock'):
            return _reconcile_locked(root,request_id,evidence)
    except checkpoint.ResumeRefused as exc: raise DeliveryRefused(str(exc)) from exc

def _reconcile_locked(deployment_root: Path, request_id: str, evidence: dict[str,Any]) -> dict[str,Any]:
    """Resolve an ambiguous request from explicit operator evidence; never replay."""
    request_id=_request_id(request_id)
    root=Path(deployment_root).absolute(); ledger=root/'.delivery-ledger'; current=_ledger(ledger,request_id)
    if current.get('state') not in {'unknown','prepare'}: raise DeliveryRefused('only incomplete ambiguous requests require reconciliation')
    if not isinstance(evidence,dict) or evidence.get('request_id')!=request_id or evidence.get('observed_state') not in {'complete','not_executed'} or not isinstance(evidence.get('evidence_id'),str) or not evidence['evidence_id'].strip(): raise DeliveryRefused('explicit reconciliation evidence required')
    current.update({'state':'complete' if evidence['observed_state']=='complete' else 'reconciled_not_executed','reconciliation':evidence,'updated_at':time.time()})
    _write_json(ledger/f'{request_id}.json',current)
    return current

def install(pack: Path, deployment_root: Path, **kwargs: Any) -> dict[str,Any]:
    root=Path(deployment_root).absolute(); _chain(root)
    _approval(kwargs.get('approval'),root,kwargs.get('candidate_type'))
    _request_id(kwargs.get('request_id'))
    if kwargs.get('operator_adapter') is not True: raise DeliveryRefused('trusted operator health adapter must be explicitly enabled')
    root.mkdir(parents=True,exist_ok=True)
    try:
        with checkpoint.ExclusiveLock(root/'.delivery.lock'):
            return _install_locked(pack,root,**kwargs)
    except checkpoint.ResumeRefused as exc: raise DeliveryRefused(str(exc)) from exc

def _install_locked(pack: Path, deployment_root: Path, *, approval: dict[str,Any], request_id: str, candidate_type: str,
            health_argv: list[str], operator_adapter: bool, health_failure: bool=False,
            stop: Callable[[],bool]|None=None, fault: Callable[[str],None]|None=None) -> dict[str,Any]:
    """Install immutable bytes then atomically switch active pointer; failure restores prior pointer.

    The operator adapter is trusted and external; generated candidate is never run here.
    """
    root=Path(deployment_root).absolute(); _chain(root)
    _approval(approval,root,candidate_type)
    if not operator_adapter: raise DeliveryRefused('trusted operator health adapter must be explicitly enabled')
    if not isinstance(health_argv,list) or not health_argv or any(not isinstance(x,str) or not x or '\x00' in x for x in health_argv): raise DeliveryRefused('health adapter must be explicit argv')
    request_id=_request_id(request_id)
    pack_info=validate_pack(Path(pack)); root.mkdir(parents=True,exist_ok=True)
    versions=root/'deployments'; versions.mkdir(exist_ok=True)
    ledger=root/'.delivery-ledger'; ledger.mkdir(exist_ok=True)
    pointer=root/'active.json'; prior=_load(pointer) if pointer.exists() else None
    _record(ledger,request_id,{'schema':1,'request_id':request_id,'state':'prepare','effect':'deploy_and_switch','candidate_sha256':pack_info['candidate_sha256'],'created_at':time.time(),'prior':prior})
    rev=f"{pack_info['candidate_sha256']}-{request_id}"
    destination=versions/rev
    try:
        if stop and stop(): raise DeliveryRefused('operator STOP requested before deployment')
        if destination.exists(): raise DeliveryRefused('immutable deployment revision already exists')
        shutil.copytree(pack,destination)
        validate_pack(destination)
        if fault: fault('after_install_before_pointer')
        new={'schema':1,'revision':rev,'candidate_sha256':pack_info['candidate_sha256'],'previous':prior.get('revision') if prior else None,'request_id':request_id}
        _write_json(pointer,new)
        if fault: fault('after_pointer_switch')
        if stop and stop(): raise DeliveryRefused('operator STOP requested; reverting deployment')
        argv=[x.replace('{deployment}',str(destination)).replace('{candidate_sha256}',pack_info['candidate_sha256']) for x in health_argv]
        completed=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,check=False,shell=False)
        healthy=completed.returncode==0 and not health_failure
        if not healthy:
            if prior is None: pointer.unlink(missing_ok=True)
            else: _write_json(pointer,prior)
            result={'status':'ROLLED_BACK','revision':prior.get('revision') if prior else None,'candidate_sha256':pack_info['candidate_sha256'],'health_returncode':completed.returncode}
        else: result={'status':'PROMOTED','revision':rev,'candidate_sha256':pack_info['candidate_sha256'],'health_returncode':0}
        rec=_ledger(ledger,request_id); rec.update({'state':'complete','result':result,'updated_at':time.time()}); _write_json(ledger/f'{request_id}.json',rec)
        return result
    except BaseException as exc:
        # Exception after pointer mutation is ambiguous to a restarted caller; this live process
        # knows it can restore the prior pointer, then records whether that succeeded.
        try:
            if pointer.exists():
                now=_load(pointer)
                if now.get('request_id')==request_id:
                    if prior is None: pointer.unlink(missing_ok=True)
                    else: _write_json(pointer,prior)
            rec=_ledger(ledger,request_id); rec.update({'state':'complete','result':{'status':'STOPPED_OR_FAILED','error':str(exc),'rollback_attempted':True},'updated_at':time.time()}); _write_json(ledger/f'{request_id}.json',rec)
        except BaseException:
            try:
                rec=_ledger(ledger,request_id); rec.update({'state':'unknown','updated_at':time.time()}); _write_json(ledger/f'{request_id}.json',rec)
            except BaseException: pass
        raise

def promote(deployment_root: Path, *, approval:dict[str,Any], request_id:str, health_argv:list[str], operator_adapter:bool, stop:Callable[[],bool]|None=None) -> dict[str,Any]:
    raise DeliveryRefused('pointer-only promotion is disabled; use application_operations container lifecycle')

def rollback(deployment_root:Path, *, approval:dict[str,Any], request_id:str)->dict[str,Any]:
    raise DeliveryRefused('pointer-only rollback is disabled; use application_operations container lifecycle')

def _cli() -> int:
    ap=argparse.ArgumentParser(description=__doc__); sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('reconcile'); p.add_argument('root',type=Path); p.add_argument('request_id'); p.add_argument('evidence',type=Path)
    p=sub.add_parser('validate-pack'); p.add_argument('pack',type=Path)
    p=sub.add_parser('pack'); p.add_argument('candidate',type=Path); p.add_argument('evidence',type=Path); p.add_argument('dependency_metadata',type=Path); p.add_argument('engine_metadata',type=Path); p.add_argument('output',type=Path)
    p=sub.add_parser('install'); p.add_argument('pack',type=Path); p.add_argument('root',type=Path); p.add_argument('authorization',type=Path); p.add_argument('--request-id',required=True); p.add_argument('--candidate-type',default='application'); p.add_argument('--health-argv',required=True,help='JSON argv vector; trusted operator adapter only'); p.add_argument('--trusted-operator-adapter',action='store_true')
    args=ap.parse_args()
    try:
        if args.cmd=='reconcile': out=reconcile(args.root,args.request_id,_load(args.evidence))
        elif args.cmd=='validate-pack': out=validate_pack(args.pack)
        elif args.cmd=='pack': out=create_pack(args.candidate,_load(args.evidence),_load(args.dependency_metadata),_load(args.engine_metadata),args.output)
        else: out=install(args.pack,args.root,approval=_load(args.authorization),request_id=args.request_id,candidate_type=args.candidate_type,health_argv=json.loads(args.health_argv),operator_adapter=args.trusted_operator_adapter)
        print(json.dumps(out,sort_keys=True)); return 0
    except (DeliveryRefused,applications.Refused,OSError,ValueError) as exc: print(f'REFUSED: {exc}',file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(_cli())
