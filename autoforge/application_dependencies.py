"""Hash-bound dependency locks and explicitly authorized artifact acquisition."""
from __future__ import annotations
import argparse, base64, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from typing import Any
import application_checkpoint as checkpoint

class DependencyRefused(ValueError): pass

def _canonical(value: Any)->bytes: return checkpoint.canonical(value)
def _sha(data:bytes)->str: return checkpoint.digest(data)
def _chain(path:Path)->None:
    try: checkpoint.regular_chain(path)
    except Exception as exc: raise DependencyRefused(str(exc)) from exc
def _atomic(path:Path,data:bytes)->None: checkpoint._atomic(path,data)


def validate_lock(lock:Any, *, sources:dict[str,str], candidate_mode:str='offline')->dict[str,Any]:
    if not isinstance(lock,dict) or set(lock)!={'schema','ecosystem','toolchain','packages'} or lock.get('schema')!=1: raise DependencyRefused('lock must contain schema, ecosystem, toolchain, packages')
    if lock['ecosystem'] not in {'python','node','typescript','java'}: raise DependencyRefused('ecosystem must be python, node, typescript, or java')
    tool=lock['toolchain']
    if not isinstance(tool,dict) or not all(isinstance(tool.get(k),str) and tool[k] for k in ('name','version','container_image')): raise DependencyRefused('runnable toolchain metadata and exact container image required')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',tool['container_image']): raise DependencyRefused('container_image must be an exact sha256 image ID')
    if not isinstance(sources,dict) or any(not isinstance(k,str) or not k or not isinstance(v,str) or not v for k,v in sources.items()): raise DependencyRefused('source allowlist must map nonempty source IDs to nonempty origins')
    packages=lock['packages']
    if not isinstance(packages,list): raise DependencyRefused('packages must be a list')
    seen=set(); checked=[]
    for package in packages:
        required={'name','version','source','artifact_sha256','license'}
        optional={'dependencies'} if lock['ecosystem'] in {'node','typescript'} else set()
        if not isinstance(package,dict) or set(package)-required-optional or not required<=set(package): raise DependencyRefused('package record must include name/version/source/artifact_sha256/license and optional dependencies')
        if not all(isinstance(package[k],str) and package[k] for k in required): raise DependencyRefused('package fields must be nonempty strings')
        key=package['name'].casefold()
        if key in seen: raise DependencyRefused('duplicate package name')
        seen.add(key)
        if package['source'] not in sources: raise DependencyRefused(f"package source not explicitly allowlisted: {package['source']}")
        if not re.fullmatch(r'[0-9a-f]{64}',package['artifact_sha256']): raise DependencyRefused('artifact_sha256 must be lowercase SHA-256')
        if 'dependencies' in package:
            deps=package['dependencies']
            if not isinstance(deps,dict) or any(not isinstance(k,str) or not k or not isinstance(v,str) or not v for k,v in deps.items()): raise DependencyRefused('node dependency map must bind exact package versions')
        checked.append(dict(package))
    if candidate_mode not in {'offline','authorized_acquisition'}: raise DependencyRefused('candidate mode must be offline or authorized_acquisition')
    return {'schema':1,'ecosystem':lock['ecosystem'],'toolchain':dict(tool),'packages':checked,'sources':dict(sorted(sources.items())),'mode':candidate_mode,'lock_sha256':_sha(_canonical(lock))}

_RANGE_VERSION = re.compile(r'[<>=!~^*|,\[\]\s]')
def audit_advisories(lock: Any, snapshot: Any | None, *, sources: dict[str, str],
                     snapshot_bytes: bytes | None = None, now: str | None = None) -> dict[str, Any]:
    """Audit exact versions in a caller-trusted snapshot; provenance is not authenticated here.

    Missing, expired, malformed, or range-based data yields HOLD. A PASS means only
    that this bounded supplied snapshot contains no exact match.
    """
    checked = validate_lock(lock, sources=sources)
    lock_sha = checked['lock_sha256']
    if snapshot is None:
        return {'schema': 1, 'status': 'HOLD', 'reason': 'advisory snapshot missing',
                'lock_sha256': lock_sha, 'advisory_snapshot_sha256': None,
                'advisory_provenance': None, 'generated_at': None, 'expires_at': None,
                'matches': []}
    if snapshot_bytes is not None:
        try:
            byte_snapshot=json.loads(snapshot_bytes.decode('utf-8'))
            if _canonical(byte_snapshot)!=_canonical(snapshot): raise DependencyRefused('snapshot bytes do not match advisory data')
        except (UnicodeError,ValueError) as exc:
            raise DependencyRefused('snapshot bytes do not match advisory data') from exc
    raw = snapshot_bytes if snapshot_bytes is not None else _canonical(snapshot)
    snapshot_sha = _sha(raw)
    required = {'schema', 'ecosystem', 'generated_at', 'expires_at', 'provenance', 'advisories'}
    if not isinstance(snapshot, dict) or set(snapshot) != required or snapshot.get('schema') != 1:
        return {'schema':1,'status':'HOLD','reason':'unsupported advisory snapshot schema',
                'lock_sha256':lock_sha,'advisory_snapshot_sha256':snapshot_sha,
                'advisory_provenance':None,'generated_at':None,'expires_at':None,'matches':[]}
    provenance = snapshot.get('provenance')
    generated, expires = snapshot.get('generated_at'), snapshot.get('expires_at')
    def instant(value):
        if not isinstance(value, str) or not value.endswith('Z'): raise ValueError
        from datetime import datetime
        return datetime.fromisoformat(value[:-1] + '+00:00')
    try:
        gen, exp = instant(generated), instant(expires)
        from datetime import datetime, timezone
        current = instant(now) if now is not None else datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return {'schema':1,'status':'HOLD','reason':'invalid UTC snapshot timestamps',
                'lock_sha256':lock_sha,'advisory_snapshot_sha256':snapshot_sha,
                'advisory_provenance':provenance,'generated_at':generated,'expires_at':expires,'matches':[]}
    base = {'schema':1,'lock_sha256':lock_sha,'advisory_snapshot_sha256':snapshot_sha,
            'advisory_provenance':provenance,'generated_at':generated,'expires_at':expires}
    if not isinstance(provenance, str) or not provenance.strip() or exp <= gen or current < gen or current >= exp:
        return {**base,'status':'HOLD','reason':'snapshot provenance invalid or snapshot stale/not yet valid','matches':[]}
    if snapshot.get('ecosystem') != checked['ecosystem'] or not isinstance(snapshot.get('advisories'), list):
        return {**base,'status':'HOLD','reason':'snapshot ecosystem mismatch or advisories invalid','matches':[]}
    locked = {(p['name'].casefold(), p['version']) for p in checked['packages']}
    if any(version.casefold() in {'latest','master','main','*'} for _, version in locked):
        return {**base,'status':'HOLD','reason':'lock contains floating version selector','matches':[]}
    if any(_RANGE_VERSION.search(version) for _, version in locked):
        return {**base,'status':'HOLD','reason':'lock contains unsupported non-exact version constraint','matches':[]}
    matches, unsupported = [], []
    for row in snapshot['advisories']:
        if not isinstance(row, dict) or set(row) != {'id','name','versions'} or not isinstance(row.get('id'), str) or not row['id'] or not isinstance(row.get('name'), str) or not row['name'] or not isinstance(row.get('versions'), list) or not row['versions']:
            unsupported.append('malformed advisory record'); continue
        versions = row['versions']
        if any(not isinstance(v, str) or not v or _RANGE_VERSION.search(v) for v in versions):
            unsupported.append(f"unsupported version range for advisory {row['id']}"); continue
        for name, version in locked:
            if row['name'].casefold() == name and version in versions:
                matches.append({'id':row['id'],'name':row['name'],'version':version})
    if matches: return {**base,'status':'FAIL','reason':'locked vulnerable version matched','matches':matches,'unsupported':unsupported}
    if unsupported: return {**base,'status':'HOLD','reason':'snapshot contains unsupported advisory data','matches':[],'unsupported':unsupported}
    return {**base,'status':'PASS','reason':'no exact-version matches in supplied snapshot only','matches':[]}

def audit_advisories_file(lock_path: Path, snapshot_path: Path | None, *, sources: dict[str, str], now: str | None = None) -> dict[str, Any]:
    lock_path = Path(lock_path); _chain(lock_path)
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    snapshot = None; raw = None
    if snapshot_path is not None:
        snapshot_path = Path(snapshot_path); _chain(snapshot_path)
        raw = snapshot_path.read_bytes()
        try: snapshot = json.loads(raw.decode('utf-8'))
        except (UnicodeError,ValueError):
            base=audit_advisories(lock,None,sources=sources,now=now)
            return {**base,'reason':'malformed advisory snapshot JSON','advisory_snapshot_sha256':_sha(raw)}
    return audit_advisories(lock, snapshot, sources=sources, snapshot_bytes=raw, now=now)

_OSV_URL='https://api.osv.dev/v1/query'
_OSV_ECOSYSTEM={'python':'PyPI','node':'npm','typescript':'npm','java':'Maven'}

def query_osv(lock: Any, *, sources: dict[str,str], timeout: int = 20,
              max_response_bytes: int = 4*1024*1024) -> dict[str,Any]:
    """Query OSV exact package versions over TLS; responses are not publisher-signed."""
    checked=validate_lock(lock,sources=sources)
    if checked['ecosystem'] not in _OSV_ECOSYSTEM:
        return {'schema':1,'status':'HOLD','reason':'lock ecosystem has no explicit OSV mapping','lock_sha256':checked['lock_sha256'],'matches':[]}
    if type(timeout) is not int or not 1<=timeout<=60 or type(max_response_bytes) is not int or not 1024<=max_response_bytes<=16*1024*1024:
        raise DependencyRefused('OSV timeout or response size limit out of range')
    from datetime import datetime,timezone,timedelta
    import time
    fetched=datetime.now(timezone.utc); started=time.monotonic(); aggregate_bytes=0
    queries=[]; advisories=[]; raw_hashes=[]; raw_responses=[]
    def partial(reason):
        known=bool(advisories)
        return {'schema':1,'status':'FAIL' if known else 'HOLD','reason':reason,'lock_sha256':checked['lock_sha256'],
                'source':_OSV_URL,'transport_authentication':'HTTPS/TLS server authentication; response is not publisher-signed',
                'fetched_at':fetched.isoformat().replace('+00:00','Z'),'queries':queries,
                'response_sha256':_sha(_canonical(raw_hashes)) if raw_hashes else None,
                'raw_responses_base64':[base64.b64encode(raw).decode('ascii') for raw in raw_responses],
                'matches':[{'id':row['id'],'name':row['name'],'version':version} for row in advisories for version in row['versions']]}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
    if len(checked['packages'])>128: return partial('OSV package query count exceeds 128')
    for package in checked['packages']:
        remaining=60-(time.monotonic()-started)
        if remaining<=0: return partial('OSV aggregate query exceeded 60 seconds')
        version=package['version']
        if not version or _RANGE_VERSION.search(version) or version.casefold() in {'latest','master','main','*'}:
            return partial('OSV requires exact locked package versions')
        payload=_canonical({'package':{'name':package['name'],'ecosystem':_OSV_ECOSYSTEM[checked['ecosystem']]},'version':version})
        request=urllib.request.Request(_OSV_URL,data=payload,headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'AutoForge-OSV-query/1'},method='POST')
        try:
            with opener.open(request,timeout=min(timeout,remaining)) as response:
                if response.status!=200 or response.geturl()!=_OSV_URL: raise DependencyRefused('OSV response was not direct HTTP 200 from the fixed endpoint')
                remaining_bytes=8*1024*1024-aggregate_bytes
                raw=response.read(min(max_response_bytes,remaining_bytes)+1)
        except urllib.error.HTTPError as exc:
            if 300<=exc.code<400: raise DependencyRefused('OSV redirect refused') from exc
            return partial(f'OSV query failed with HTTP {exc.code}')
        except (OSError,urllib.error.URLError,TimeoutError): return partial('OSV TLS query failed')
        if len(raw)>max_response_bytes: return partial('OSV response exceeds byte limit')
        aggregate_bytes+=len(raw)
        if aggregate_bytes>8*1024*1024: return partial('OSV aggregate response bytes exceed 8 MiB')
        raw_hash=_sha(raw); raw_hashes.append(raw_hash); raw_responses.append(raw)
        query={'name':package['name'],'version':version,'ecosystem':_OSV_ECOSYSTEM[checked['ecosystem']],
               'request_sha256':_sha(payload),'request_base64':base64.b64encode(payload).decode('ascii'),'response_sha256':raw_hash}
        try: result=json.loads(raw.decode('utf-8'))
        except (UnicodeError,ValueError): queries.append(query); return partial('OSV response was not valid JSON')
        if not isinstance(result,dict) or set(result)-{'vulns','next_page_token'} or not isinstance(result.get('vulns',[]),list):
            queries.append(query); return partial('OSV response schema unsupported')
        for vuln in result.get('vulns',[]):
            if not isinstance(vuln,dict) or not isinstance(vuln.get('id'),str) or not vuln['id']:
                queries.append(query); return partial('OSV vulnerability record missing id')
            advisories.append({'id':vuln['id'],'name':package['name'],'versions':[version]})
        queries.append(query)
        if result.get('next_page_token'): return partial('OSV response pagination requires additional pages')
    snapshot={'schema':1,'ecosystem':checked['ecosystem'],'generated_at':fetched.isoformat().replace('+00:00','Z'),
              'expires_at':(fetched+timedelta(hours=24)).isoformat().replace('+00:00','Z'),
              'provenance':'https://api.osv.dev/v1/query; HTTPS/TLS server authentication; no provider signature claimed',
              'advisories':advisories}
    snapshot_bytes=_canonical(snapshot)
    audit=audit_advisories(lock,snapshot,sources=sources,snapshot_bytes=snapshot_bytes,now=fetched.isoformat().replace('+00:00','Z'))
    return {'schema':1,'status':audit['status'],'reason':audit['reason'],'lock_sha256':checked['lock_sha256'],
            'source':_OSV_URL,'transport_authentication':'HTTPS/TLS server authentication; response is not publisher-signed',
            'fetched_at':snapshot['generated_at'],'expires_at':snapshot['expires_at'],'query_count':len(queries),
            'queries':queries,'response_sha256':_sha(_canonical(raw_hashes)),
            'raw_responses_base64':[__import__('base64').b64encode(raw).decode('ascii') for raw in raw_responses],
            'snapshot':snapshot,'snapshot_sha256':_sha(snapshot_bytes),'matches':audit['matches']}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def acquire_https(url:str, cache:Path, expected_sha256:str, *, source_id:str, sources:dict[str,str], license_record:str, package_name:str, package_version:str, authorized:bool=False, timeout:int=30, max_bytes:int=256*1024*1024)->dict[str,Any]:
    """Download one allowlisted HTTPS artifact; never follows redirects or sends caller credentials."""
    if not authorized: raise DependencyRefused('explicit dependency-acquisition authorization required')
    if source_id not in sources: raise DependencyRefused('artifact source is not allowlisted')
    parsed=urllib.parse.urlsplit(url); approved=urllib.parse.urlsplit(sources[source_id])
    try: approved_port=approved.port; requested_port=parsed.port
    except ValueError as exc: raise DependencyRefused('artifact URL has invalid port') from exc
    if approved.scheme!='https' or not approved.hostname or approved.username or approved.password or approved.path not in ('','/') or approved.query or approved.fragment or parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.hostname.casefold()!=approved.hostname.casefold() or requested_port!=approved_port:
        raise DependencyRefused('artifact URL must use the exact allowlisted HTTPS origin without credentials')
    if not re.fullmatch(r'[0-9a-f]{64}',expected_sha256): raise DependencyRefused('expected SHA-256 required')
    request=urllib.request.Request(url,headers={'User-Agent':'AutoForge-artifact-acquirer/1','Accept-Encoding':'identity'},method='GET')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
    try:
        with opener.open(request,timeout=timeout) as response:
            if response.status!=200 or response.geturl()!=url: raise DependencyRefused('artifact server response is not direct HTTP 200')
            data=response.read(max_bytes+1)
    except urllib.error.HTTPError as exc:
        if 300<=exc.code<400: raise DependencyRefused('artifact redirect refused') from exc
        raise DependencyRefused(f'artifact HTTP request failed ({exc.code})') from exc
    except (OSError,urllib.error.URLError,TimeoutError) as exc: raise DependencyRefused('artifact HTTPS request failed') from exc
    if len(data)>max_bytes: raise DependencyRefused('artifact exceeds acquisition size limit')
    if _sha(data)!=expected_sha256: raise DependencyRefused('artifact SHA-256 mismatch')
    cache=Path(cache).absolute(); _chain(cache); cache.mkdir(parents=True,exist_ok=True)
    return _store_bytes(data,cache,expected_sha256,source_id=source_id,license_record=license_record,package_name=package_name,package_version=package_version)

def _store_bytes(data:bytes,cache:Path,expected_sha256:str,*,source_id:str,license_record:str,package_name:str,package_version:str)->dict[str,Any]:
    actual=_sha(data)
    if actual!=expected_sha256: raise DependencyRefused('artifact SHA-256 mismatch')
    cache=Path(cache).absolute(); _chain(cache); cache.mkdir(parents=True,exist_ok=True); _chain(cache)
    target=cache/actual; _chain(target)
    if target.exists():
        if not target.is_file() or _sha(target.read_bytes())!=actual: raise DependencyRefused('existing cache entry failed immutable digest check')
    else:
        temp=cache/f'.{actual}.{os.getpid()}.tmp'
        try:
            with temp.open('xb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
            try: os.link(temp,target)
            except FileExistsError: pass
        finally: temp.unlink(missing_ok=True)
        if _sha(target.read_bytes())!=actual: raise DependencyRefused('cached artifact verification failed')
        os.chmod(target,stat.S_IREAD)
    record={'schema':1,'name':package_name,'version':package_version,'source':source_id,'artifact_sha256':actual,'size':len(data),'license':license_record,'cache_file':str(target)}
    manifest=cache/(actual+'.json')
    if manifest.exists():
        old=json.loads(manifest.read_text(encoding='utf-8'))
        if old!=record: raise DependencyRefused('cache provenance conflict')
    else: _atomic(manifest,_canonical(record)+b'\n')
    return record

def import_artifact(source:Path, cache:Path, expected_sha256:str, *, source_id:str, license_record:str, package_name:str, package_version:str, authorized:bool=False)->dict[str,Any]:
    """Import one existing local artifact; no download, execution, or overwrite."""
    if not authorized: raise DependencyRefused('explicit dependency-acquisition authorization required')
    if not package_name or not package_version: raise DependencyRefused('package name and version required')
    if not re.fullmatch(r'[0-9a-f]{64}',expected_sha256): raise DependencyRefused('expected SHA-256 required')
    source=Path(source).absolute(); cache=Path(cache).absolute(); _chain(source); _chain(cache)
    st=source.lstat()
    if not stat.S_ISREG(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400: raise DependencyRefused('artifact must be a regular local file')
    return _store_bytes(source.read_bytes(),cache,expected_sha256,source_id=source_id,license_record=license_record,package_name=package_name,package_version=package_version)

def verify_cache(cache:Path, records:list[dict[str,Any]], *, expected_sources:set[str])->dict[str,Any]:
    cache=Path(cache).absolute(); _chain(cache); verified=[]
    for record in records:
        if not isinstance(record,dict) or record.get('source') not in expected_sources: raise DependencyRefused('cache artifact source is not allowlisted')
        sha=record.get('artifact_sha256')
        if not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{64}',sha): raise DependencyRefused('invalid artifact hash')
        path=cache/sha; _chain(path)
        if not path.is_file() or _sha(path.read_bytes())!=sha: raise DependencyRefused('cache artifact missing or tampered')
        proof=json.loads((cache/(sha+'.json')).read_text(encoding='utf-8'))
        if proof.get('artifact_sha256')!=sha or any(proof.get(k)!=record.get(k) for k in ('source','license','name','version','size')): raise DependencyRefused('cache provenance mismatch')
        if not all(isinstance(record.get(k),str) and record[k] for k in ('name','version')): raise DependencyRefused('cache record must bind package name/version')
        verified.append({'name':record['name'],'version':record['version'],'source':record['source'],'artifact_sha256':sha,'license':proof['license'],'size':path.stat().st_size})
    ordered=sorted(verified,key=lambda r:(r['name'].casefold(),r['artifact_sha256']))
    return {'schema':1,'artifacts':ordered,'cache_sha256':_sha(_canonical(ordered))}

def _publisher_url_origin(url: str, *, allow_path: bool) -> tuple[str, int]:
    if not isinstance(url,str): raise DependencyRefused('publisher URL must be text')
    try: parsed=urllib.parse.urlsplit(url); port=parsed.port
    except ValueError as exc: raise DependencyRefused('publisher URL has invalid authority') from exc
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise DependencyRefused('publisher URL must be credential-free HTTPS without fragment')
    if not allow_path and parsed.path not in ('','/'):
        raise DependencyRefused('publisher source authority must not contain a path')
    return parsed.hostname.casefold(),port or 443

_LOCAL_ARTIFACT_KINDS = {'image', 'reference', 'plugin'}

def verify_local_artifacts(root: Path, records: list[dict[str, Any]], *,
                           expected_sources: set[str]) -> dict[str, Any]:
    """Verify bounded local image/reference/plugin identities against actual file bytes.

    Source and license are operator declarations; this does not verify signatures,
    advisories, licenses, or trust in the declaring party.
    """
    if not isinstance(expected_sources, set) or any(not isinstance(x, str) or not x for x in expected_sources):
        raise DependencyRefused('local artifact source allowlist must be a set of nonempty IDs')
    if not isinstance(records, list) or len(records) > 256:
        raise DependencyRefused('local artifact records must be a bounded list')
    root = Path(root).absolute(); _chain(root)
    if not root.is_dir(): raise DependencyRefused('local artifact root must be a directory')
    verified=[]; seen=set()
    required={'kind','name','version','source','license','path','artifact_sha256'}
    for record in records:
        if not isinstance(record, dict) or set(record) != required:
            raise DependencyRefused('local artifact record fields mismatch')
        if any(not isinstance(record[k],str) or not record[k].strip() for k in required):
            raise DependencyRefused('local artifact record fields must be nonempty text')
        if record['kind'] not in _LOCAL_ARTIFACT_KINDS:
            raise DependencyRefused('unsupported local artifact kind')
        if record['source'] not in expected_sources:
            raise DependencyRefused('local artifact source is not allowlisted')
        rel=Path(record['path'])
        if rel.is_absolute() or '\\' in record['path'] or any(p in {'', '.', '..'} for p in record['path'].split('/')):
            raise DependencyRefused('local artifact path must be a safe relative POSIX path')
        if not re.fullmatch(r'[0-9a-f]{64}',record['artifact_sha256']):
            raise DependencyRefused('local artifact hash must be lowercase SHA-256')
        identity=(record['kind'],record['name'].casefold(),record['version'])
        if identity in seen: raise DependencyRefused('duplicate local artifact identity')
        seen.add(identity)
        path=root.joinpath(*record['path'].split('/')); _chain(path)
        if not path.is_file() or _sha(path.read_bytes()) != record['artifact_sha256']:
            raise DependencyRefused('local artifact missing or tampered')
        verified.append({k:record[k] for k in ('kind','name','version','source','license','path','artifact_sha256')})
    ordered=sorted(verified,key=lambda r:(r['kind'],r['name'].casefold(),r['version'],r['path']))
    return {'schema':1,'artifacts':ordered,'artifacts_sha256':_sha(_canonical(ordered)),
            'signature_verification':'unsupported','advisory_verification':'unsupported'}
def verify_publisher_attestation(artifact: Path, signature: Path, keyring: Path, *,
                                 expected_fingerprint: str, source_url: str,
                                 signature_url: str, expected_version: str,
                                 expected_sha256: str | None = None,
                                 gpgv: str = 'gpgv') -> dict[str, Any]:
    """Reverify publisher signature against exact local bytes and explicit release binding."""
    if not isinstance(expected_version, str) or not expected_version.strip():
        raise DependencyRefused('publisher release version binding required')
    try:
        import application_provenance as provenance
        verified = provenance.verify_publisher_artifact(artifact, signature, keyring,
            expected_fingerprint=expected_fingerprint, source_url=source_url,
            signature_url=signature_url, gpgv=gpgv)
    except (ImportError, ValueError, OSError) as exc:
        raise DependencyRefused(f'publisher signature verification failed: {exc}') from exc
    if expected_sha256 is not None and verified['artifact_sha256'] != expected_sha256:
        raise DependencyRefused('publisher artifact digest does not match expected digest')
    return {**verified, 'release_version': expected_version}


def verify_signed_json(data: bytes, signature: bytes, public_key_pem: bytes) -> dict[str, Any]:
    """Verify an exact canonical JSON document using an operator-pinned Ed25519 key."""
    if not isinstance(data, bytes) or not isinstance(signature, bytes) or not isinstance(public_key_pem, bytes):
        raise DependencyRefused('signed document, detached signature and pinned public key bytes required')
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise DependencyRefused('Ed25519 verification unavailable: install the cryptography runtime dependency explicitly') from exc
    try:
        key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(key, Ed25519PublicKey): raise DependencyRefused('pinned key must be Ed25519')
        key.verify(signature, data)
        value = json.loads(data.decode('utf-8'))
    except DependencyRefused: raise
    except Exception as exc: raise DependencyRefused('detached signature or signed JSON verification failed') from exc
    if _canonical(value) != data: raise DependencyRefused('signed JSON must use canonical encoding')
    return value

def verify_local_artifact_bundle(root: Path, bundle_bytes: bytes, signature: bytes,
                                pinned_public_key_pem: bytes, *, expected_sources: set[str]) -> dict[str, Any]:
    """Verify operator-attested local artifact identities; this is not publisher provenance."""
    bundle=verify_signed_json(bundle_bytes,signature,pinned_public_key_pem)
    if not isinstance(bundle,dict) or set(bundle)!={'schema','artifacts'} or type(bundle.get('schema')) is not int or bundle['schema']!=1:
        raise DependencyRefused('signed local artifact bundle must be schema 1 with artifacts')
    inventory=verify_local_artifacts(root,bundle['artifacts'],expected_sources=expected_sources)
    try:
        from cryptography.hazmat.primitives import serialization
        key=serialization.load_pem_public_key(pinned_public_key_pem)
        fingerprint=hashlib.sha256(key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    except Exception as exc: raise DependencyRefused('pinned Ed25519 public key fingerprint unavailable') from exc
    return {'schema':1,'status':'PASS','attestation':'operator-attested; not publisher-signed',
            'signer_public_key_sha256':fingerprint,'bundle_sha256':_sha(bundle_bytes),
            'artifacts':inventory['artifacts'],'artifacts_sha256':inventory['artifacts_sha256']}

def verify_license_policy(lock: dict[str, Any], policy: Any, *, sources: dict[str, str]) -> dict[str, Any]:
    """Check declared package licenses against an authenticated operator policy; not legal advice."""
    checked = validate_lock(lock, sources=sources)
    if not isinstance(policy, dict) or set(policy) != {'schema', 'packages'} or type(policy.get('schema')) is not int or policy['schema'] != 1 or not isinstance(policy['packages'], dict):
        raise DependencyRefused('license policy must be schema 1 with package license allowlists')
    results=[]
    for package in checked['packages']:
        allowed=policy['packages'].get(package['name'].casefold())
        if not isinstance(allowed, list) or not allowed or any(not isinstance(item, str) or not item for item in allowed):
            results.append({'name':package['name'],'license':package['license'],'status':'UNRESOLVED'})
        else:
            results.append({'name':package['name'],'license':package['license'],'status':'PASS' if package['license'] in allowed else 'FAIL'})
    status='FAIL' if any(item['status']=='FAIL' for item in results) else ('PASS' if all(item['status']=='PASS' for item in results) else 'HOLD')
    return {'schema':1,'status':status,'lock_sha256':checked['lock_sha256'],'policy_sha256':_sha(_canonical(policy)),'results':results,
            'limitation':'Checks operator-declared license identifiers against this policy only; does not determine legal license obligations.'}


def environment_manifest(lock:dict[str,Any], *, sources:dict[str,str], artifacts:list[dict[str,Any]], cache:Path, candidate_mode:str='offline')->dict[str,Any]:
    checked=validate_lock(lock,sources=sources,candidate_mode=candidate_mode); inventory=verify_cache(cache,artifacts,expected_sources=set(sources))
    by_hash={a['artifact_sha256']:a for a in inventory['artifacts']}
    if len(by_hash)!=len(inventory['artifacts']): raise DependencyRefused('artifact digest reused by multiple lock entries')
    locked=sorted((p['name'].casefold(),p['version'],p['source'],p['artifact_sha256'],p['license']) for p in checked['packages'])
    available=sorted((p['name'].casefold(),p['version'],p['source'],p['artifact_sha256'],p['license']) for p in inventory['artifacts'])
    if locked!=available: raise DependencyRefused('lock package provenance multiplicity differs from verified cache inventory')
    return {'schema':1,'lock_sha256':checked['lock_sha256'],'ecosystem':checked['ecosystem'],'toolchain':checked['toolchain'],'packages':checked['packages'],'source_allowlist':checked['sources'],'artifacts':inventory['artifacts'],'cache_sha256':inventory['cache_sha256'],'candidate_run_network':'disabled','acquisition_mode':candidate_mode,'install_argv':locked_install_argv(checked)}

def locked_install_argv(validated_lock:dict[str,Any])->list[str]:
    eco=validated_lock['ecosystem']
    if eco=='python': return ['python','-m','pip','install','--no-index','--require-hashes','--find-links','/wheelhouse','-r','/autoforge-lock/requirements.lock']
    if eco in {'node','typescript'}: return ['npm','install','--offline','--ignore-scripts','--no-audit','--no-fund','--package-lock=false']
    return ['javac','-proc:none','-cp','/autoforge-jars/*']

def container_plan(manifest:dict[str,Any], *, build_argv:list[str])->dict[str,Any]:
    if not isinstance(manifest,dict) or not isinstance(build_argv,list) or not build_argv or any(not isinstance(x,str) or not x or '\x00' in x for x in build_argv): raise DependencyRefused('validated manifest and nonempty build argv required')
    sources=manifest.get('source_allowlist')
    lock={'schema':1,'ecosystem':manifest.get('ecosystem'),'toolchain':manifest.get('toolchain'),'packages':manifest.get('packages')}
    checked=validate_lock(lock,sources=sources,candidate_mode=manifest.get('acquisition_mode','offline'))
    if manifest.get('schema')!=1 or manifest.get('lock_sha256')!=checked['lock_sha256'] or manifest.get('candidate_run_network')!='disabled': raise DependencyRefused('manifest integrity or offline execution policy invalid')
    install_argv=locked_install_argv(checked)
    if manifest.get('install_argv')!=install_argv: raise DependencyRefused('install command differs from locked isolated installer')
    return {'container_image':checked['toolchain']['container_image'],'network':'none','candidate_mount':'read-only','cache_mount':'read-only','lock_sha256':checked['lock_sha256'],'install_argv':install_argv,'build_argv':build_argv,'execute_on_host':False}

def supply_chain_gate(lock:dict[str,Any], *, sources:dict[str,str], artifacts:list[dict[str,Any]],
                      cache:Path, snapshot:Any|None, authorized:bool,
                      snapshot_bytes:bytes|None=None, now:str|None=None,
                      local_artifacts:list[dict[str,Any]]|None=None,
                      local_artifact_root:Path|None=None,
                      advisory_signature:bytes|None=None, license_policy:Any|None=None,
                      license_policy_bytes:bytes|None=None, license_signature:bytes|None=None,
                      pinned_public_key_pem:bytes|None=None,
                      local_bundle_bytes:bytes|None=None, local_bundle_signature:bytes|None=None,
                      refresh_osv:bool=False, engine:str|None=None,
                      image_scanner:Path|None=None, image_scanner_sha256:str|None=None,
                      image_db_cache:Path|None=None, image_report_path:Path|None=None,
                      image_repo_digest:str|None=None,
                      publisher_required:bool=False, publisher_artifact:Path|None=None,
                      publisher_signature:Path|None=None, publisher_keyring:Path|None=None,
                      publisher_fingerprint:str|None=None, publisher_source_url:str|None=None,
                      publisher_signature_url:str|None=None, publisher_version:str|None=None,
                      publisher_package_name:str|None=None)->dict[str,Any]:
    """Run scoped dependency checks; live OSV refresh is opt-in and never authorizes execution."""
    if type(refresh_osv) is not bool: raise DependencyRefused('refresh_osv must be an explicit boolean')
    if engine is not None and (not isinstance(engine,str) or engine not in {'docker','podman'}): raise DependencyRefused('engine must be docker or podman')
    if engine is not None and not refresh_osv: raise DependencyRefused('runtime image inspection requires explicit live OSV refresh mode')
    if refresh_osv and now is not None: raise DependencyRefused('live OSV timestamp is assigned by the live query; caller-supplied now is unsupported')
    if authorized is not True:
        raise DependencyRefused('explicit caller authorization required for supply-chain gate')
    if any(x is not None for x in (image_scanner,image_scanner_sha256,image_db_cache,image_report_path,image_repo_digest)):
        if not all(x is not None for x in (image_scanner,image_scanner_sha256,image_db_cache,image_report_path,image_repo_digest)):
            raise DependencyRefused('image scanner path/hash, DB cache, report path and repository digest must be supplied together')
        if engine not in (None,'docker'): raise DependencyRefused('Grype image scan requires the Docker image engine')
    if type(publisher_required) is not bool: raise DependencyRefused('publisher_required must be boolean')
    publisher_inputs=(publisher_artifact,publisher_signature,publisher_keyring,publisher_fingerprint,publisher_source_url,publisher_signature_url,publisher_version,publisher_package_name)
    if not publisher_required and any(value is not None for value in publisher_inputs):
        raise DependencyRefused('publisher signature inputs require publisher_required=True')
    publisher_attestation=None
    if publisher_required:
        try:
            if any(value is None for value in publisher_inputs):
                raise DependencyRefused('required publisher attestation inputs missing')
            if not isinstance(publisher_package_name,str) or not publisher_package_name:
                raise DependencyRefused('publisher package name required for lock binding')
            checked_lock=validate_lock(lock,sources=sources)
            matches=[p for p in checked_lock['packages'] if p['name'].casefold()==publisher_package_name.casefold()]
            if len(matches)!=1 or matches[0]['version']!=publisher_version:
                raise DependencyRefused('publisher package identity/version does not uniquely match dependency lock')
            package=matches[0]
            source_host,source_port=_publisher_url_origin(sources[package['source']],allow_path=False)
            artifact_host,artifact_port=_publisher_url_origin(publisher_source_url,allow_path=True)
            signature_host,signature_port=_publisher_url_origin(publisher_signature_url,allow_path=True)
            artifact_url=urllib.parse.urlsplit(publisher_source_url)
            if (artifact_host,artifact_port)!=(source_host,source_port) or (signature_host,signature_port)!=(source_host,source_port):
                raise DependencyRefused('publisher artifact and signature URL origins must match locked package source authority')
            if publisher_version not in artifact_url.path:
                raise DependencyRefused('publisher artifact URL does not encode the locked package version')
            publisher_attestation=verify_publisher_attestation(publisher_artifact,publisher_signature,publisher_keyring,
                expected_fingerprint=publisher_fingerprint,source_url=publisher_source_url,
                signature_url=publisher_signature_url,expected_version=publisher_version,
                expected_sha256=package['artifact_sha256'])
            publisher_attestation['package_name']=package['name']
            publisher_attestation['package_source']=package['source']
        except (ValueError, OSError) as exc:
            publisher_attestation={'status':'HOLD' if 'missing' in str(exc) else 'FAIL',
                                   'reason':'required publisher attestation missing' if 'missing' in str(exc) else 'publisher signature/pin/artifact binding mismatch'}
    if publisher_attestation is not None and publisher_attestation['status']=='FAIL':
        return {'schema':1,'status':'FAIL','reason':'publisher signature or lock identity binding failed',
                'publisher_required':True,'publisher_attestation':publisher_attestation,
                'candidate_execution_authorized':False}
    environment=environment_manifest(lock,sources=sources,artifacts=artifacts,cache=cache)
    image_security=None
    if image_scanner is not None:
        import application_builder as builder
        try:
            inspected=json.loads(builder._run(['docker','image','inspect','--format','{{json .RepoDigests}}',environment['toolchain']['container_image']],timeout=15).decode('utf-8'))
        except (builder.BuildFailure,builder.MissingPrerequisite,ValueError,UnicodeError) as exc:
            raise DependencyRefused(f'cannot bind scanner image to dependency lock: {exc}') from exc
        if not isinstance(inspected,list) or image_repo_digest not in inspected:
            raise DependencyRefused('scanner repository digest is not the locked local image')
        image_security=grype_image_gate(image_repo_digest,scanner=image_scanner,expected_scanner_sha256=image_scanner_sha256,
            db_cache=image_db_cache,expected_repo_digest=image_repo_digest,report_path=image_report_path,authorized=authorized)
        if image_security['status']=='FAIL':
            return {'schema':1,'status':'FAIL','reason':'locally scanned runtime image contains vulnerability findings','image_security':image_security,'candidate_execution_authorized':False}
    runtime_image=None
    if refresh_osv:
        if engine is None: raise DependencyRefused('live supply-chain refresh requires an explicit docker or podman engine')
        import application_builder as builder
        try:
            runtime_image=builder.preflight(engine,environment['toolchain']['container_image'])
            raw=builder._run([engine,'image','inspect','--format','{{json .}}',environment['toolchain']['container_image']],timeout=15)
            inspected=json.loads(raw.decode('utf-8'))
        except (builder.MissingPrerequisite,builder.BuildFailure,UnicodeError,ValueError) as exc: raise DependencyRefused(f'runtime image inspection failed: {exc}') from exc
        image_id=environment['toolchain']['container_image']
        if not isinstance(inspected,dict) or inspected.get('Id')!=image_id or not isinstance(inspected.get('RepoDigests'),list) or not isinstance(inspected.get('Os'),str) or inspected.get('Os')!='linux' or not isinstance(inspected.get('Architecture'),str) or not inspected.get('Architecture') or not isinstance(inspected.get('RootFS'),dict):
            raise DependencyRefused('engine image inspection returned incomplete or mismatched runtime identity')
        runtime_image={**runtime_image,'repo_digests':inspected['RepoDigests'],'os':inspected['Os'],'architecture':inspected['Architecture'],'rootfs':inspected['RootFS']}
    live_osv=query_osv(lock,sources=sources) if refresh_osv else None
    local_inventory=None
    if local_bundle_bytes is not None or local_bundle_signature is not None:
        if local_bundle_bytes is None or local_bundle_signature is None or pinned_public_key_pem is None or local_artifact_root is None:
            raise DependencyRefused('signed local bundle, detached signature, pinned key, and artifact root are all required')
        local_inventory=verify_local_artifact_bundle(local_artifact_root,local_bundle_bytes,local_bundle_signature,pinned_public_key_pem,expected_sources=set(sources))
        if local_artifacts is not None and _canonical(local_artifacts)!=_canonical(json.loads(local_bundle_bytes.decode('utf-8'))['artifacts']):
            raise DependencyRefused('signed local artifact bundle must exactly match the caller-requested artifact records')
    elif local_artifacts is not None:
        if local_artifact_root is None: raise DependencyRefused('local artifact root required when local artifact records are supplied')
        local_inventory=verify_local_artifacts(local_artifact_root,local_artifacts,expected_sources=set(sources))
    if live_osv is None:
        advisory=audit_advisories(lock,snapshot,sources=sources,snapshot_bytes=snapshot_bytes,now=now)
    elif 'snapshot' in live_osv:
        advisory=audit_advisories(lock,live_osv['snapshot'],sources=sources,snapshot_bytes=_canonical(live_osv['snapshot']),now=now)
        advisory={**advisory,'source':'live OSV HTTPS query','transport_authentication':live_osv.get('transport_authentication'),'query_count':live_osv.get('query_count'),'response_sha256':live_osv.get('response_sha256'),'queries':live_osv.get('queries'),'raw_responses_base64':live_osv.get('raw_responses_base64')}
        if advisory['status']=='PASS' and live_osv['status']!='PASS': advisory={**advisory,'status':'HOLD','reason':'live OSV query did not complete with PASS'}
    else:
        advisory={**audit_advisories(lock,None,sources=sources,now=now),'status':live_osv['status'],'reason':live_osv['reason'],'source':live_osv.get('source'),'transport_authentication':live_osv.get('transport_authentication'),'query_count':len(live_osv.get('queries',[])),'response_sha256':live_osv.get('response_sha256'),'queries':live_osv.get('queries',[]),'raw_responses_base64':live_osv.get('raw_responses_base64',[])}
    signed_advisory=False
    if advisory_signature is not None and pinned_public_key_pem is not None and snapshot_bytes is not None:
        signed_value=verify_signed_json(snapshot_bytes,advisory_signature,pinned_public_key_pem)
        if signed_value != snapshot: raise DependencyRefused('signed advisory payload differs from audited snapshot')
        signed_advisory=True
    license_result={'status':'HOLD','reason':'signed operator license policy missing'}
    if license_signature is not None and pinned_public_key_pem is not None and license_policy_bytes is not None:
        signed_policy=verify_signed_json(license_policy_bytes,license_signature,pinned_public_key_pem)
        if signed_policy != license_policy: raise DependencyRefused('signed license policy differs from supplied policy')
        license_result=verify_license_policy(lock,license_policy,sources=sources)
    common={'lock_sha256':environment['lock_sha256'],'cache_sha256':environment['cache_sha256'],
            'container_image':environment['toolchain']['container_image'],'ecosystem':environment['ecosystem'],
            'image_security':image_security,
            'artifact_count':len(environment['artifacts']),'local_artifacts':local_inventory,
            'advisory':advisory,'advisory_signature_verified':signed_advisory,'license_policy':license_result,
            'scoped_checks_status':'PASS' if advisory['status']=='PASS' and (signed_advisory or refresh_osv and advisory.get('source')=='live OSV HTTPS query') and license_result['status']=='PASS' and (image_security is None or image_security['status']=='PASS') and (publisher_attestation is None or publisher_attestation['status']=='PASS') else 'HOLD',
            'image_digest_bound_to_lock':True,'image_runtime_identity_verified':runtime_image is not None,'runtime_image_preflight':runtime_image,
            'publisher_required':publisher_required,'publisher_attestation':publisher_attestation,
            'live_osv_refresh':refresh_osv,'supported_scope':['python','node','typescript','java'],
            'locally_hash_verified_scope':['images','references','plugins'],
            'installer_controls':['Python wheels install with --no-index --require-hashes into ephemeral /build/site-packages','Node/TypeScript artifacts are cached and extracted without npm lifecycle invocation; npm_config_ignore_scripts=true and offline; candidate execution network=none'],
            'unresolved_required_scope':['publisher authenticity for runtime image','advisory database completeness beyond queried exact lock versions'],
            'license_policy_scope':'Optional operator allowlist check on declared identifiers; not legal advice or a license grant.'}
    if advisory['status']=='FAIL':
        return {'schema':1,'status':'FAIL','reason':'locked artifact matches supplied advisory data',**common}
    if license_result['status']=='FAIL':
        return {'schema':1,'status':'FAIL','reason':'declared package license is not allowed by authenticated operator policy',**common}
    if advisory['status']=='PASS' and not signed_advisory and not (refresh_osv and advisory.get('source')=='live OSV HTTPS query'):
        advisory={**advisory,'status':'HOLD','reason':'advisory snapshot is unsigned; caller-trusted data only'}
        common['advisory']=advisory
    if advisory['status']=='PASS' and license_result['status']=='PASS' and (publisher_attestation is None or publisher_attestation['status']=='PASS'):
        reason='scoped checks passed; publisher authenticity and advisory database completeness remain unverified'
        if publisher_attestation is not None:
            reason='scoped checks passed; publisher signature verified for the selected artifact, advisory database completeness remains unverified'
        return {'schema':1,'status':'HOLD','reason':reason,**common,'candidate_execution_authorized':False}
    return {'schema':1,'status':'HOLD','reason':'dependency evidence is incomplete or contains unresolved obligations',
            **common,'candidate_execution_authorized':False}

def grype_image_gate(image: str, *, scanner: Path, expected_scanner_sha256: str,
                     db_cache: Path, expected_repo_digest: str, report_path: Path,
                     authorized: bool = False, timeout: int = 300) -> dict[str, Any]:
    """Scan one already-local Docker image with a hash-pinned Grype and DB cache."""
    base={'schema':1,'scanner':'grype','image':image,'expected_repo_digest':expected_repo_digest,
          'candidate_execution_authorized':False}
    def hold(reason: str, **extra: Any) -> dict[str, Any]: return {**base,'status':'HOLD','reason':reason,**extra}
    if authorized is not True: return hold('explicit caller authorization required')
    if not isinstance(timeout,int) or isinstance(timeout,bool) or timeout < 1 or timeout > 1800:
        return hold('timeout must be from 1 to 1800 seconds')
    if not re.fullmatch(r'[0-9a-f]{64}',expected_scanner_sha256): return hold('expected scanner SHA-256 is invalid')
    if not isinstance(expected_repo_digest,str) or not re.fullmatch(r'.+@sha256:[0-9a-f]{64}',expected_repo_digest): return hold('expected image repository digest is invalid')
    scanner=Path(scanner).absolute(); db_cache=Path(db_cache).absolute(); report_path=Path(report_path).absolute()
    config_path=None
    try:
        _chain(scanner); _chain(db_cache)
        if not scanner.is_file() or _sha(scanner.read_bytes()) != expected_scanner_sha256: return hold('scanner missing or SHA-256 mismatch')
        if not db_cache.is_dir(): return hold('Grype DB cache is not a directory')
        env={k:v for k,v in os.environ.items() if not k.upper().startswith(('GRYPE_','SYFT_'))}
        env.update({'GRYPE_DB_CACHE_DIR':str(db_cache),'GRYPE_DB_AUTO_UPDATE':'false','GRYPE_CHECK_FOR_APP_UPDATE':'false'})
        with tempfile.NamedTemporaryFile(prefix='grype-policy-',suffix='.yaml',delete=False,mode='w',encoding='utf-8') as config:
            config.write('ignore: []\nexclude: []\nonly-fixed: false\nonly-notfixed: false\n')
            config_path=Path(config.name)
        status=subprocess.run([str(scanner),'--config',str(config_path),'db','status','-o','json'],capture_output=True,timeout=min(timeout,30),env=env,check=False)
        try: db=json.loads(status.stdout.decode('utf-8'))
        except (UnicodeError,ValueError): db={}
        if status.returncode != 0 or db.get('valid') is not True or not db.get('schemaVersion') or not isinstance(db.get('built'),str) or not isinstance(db.get('path'),str):
            return hold('Grype vulnerability database is invalid or lacks build/path identity',db_status=db)
        db_path=Path(db['path']).resolve(strict=True)
        if not db_path.is_relative_to(db_cache.resolve(strict=True)):
            return hold('Grype database path escapes the pinned cache',db_status=db)
        _chain(db_path)
        with db_path.open('rb') as db_stream:
            db_checksum=hashlib.file_digest(db_stream,'sha256').hexdigest()
        if not image or '\x00' in image or image != expected_repo_digest:
            return hold('image reference must exactly equal the caller-bound repository digest')
        _chain(report_path.parent)
        if report_path.exists(): return hold('report output already exists; refusing overwrite')
        with tempfile.NamedTemporaryFile(prefix='grype-image-',suffix='.json',delete=False,dir=report_path.parent) as tmp: temp=Path(tmp.name)
        try:
            try: run=subprocess.run([str(scanner),'--config',str(config_path),'--output','json','--file',str(temp),'--from','docker',image],capture_output=True,timeout=timeout,env=env,check=False)
            except subprocess.TimeoutExpired: return hold('Grype image scan timed out')
            if run.returncode not in (0,1): return hold('Grype image scan failed; no registry fallback permitted',scanner_exit_code=run.returncode,stderr=run.stderr[-2048:].decode('utf-8','replace'))
            size=temp.stat().st_size
            if size>64*1024*1024: return hold('Grype report exceeds 64 MiB bound')
            with temp.open('rb') as stream: raw=stream.read(64*1024*1024+1)
            if len(raw)>64*1024*1024: return hold('Grype report exceeds 64 MiB bound')
            report=json.loads(raw.decode('utf-8'))
            if not isinstance(report,dict) or not isinstance(report.get('matches'),list) or not isinstance(report.get('source'),dict): return hold('Grype report has unsupported structure')
            source=report['source']; target=source.get('target') if isinstance(source.get('target'),dict) else {}
            manifest_digest=target.get('manifestDigest'); supplied=target.get('userInput')
            repo_digests=target.get('repoDigests')
            if supplied != image or source.get('type') != 'image' or not isinstance(repo_digests,list) or expected_repo_digest not in repo_digests or not isinstance(manifest_digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',manifest_digest): return hold('Grype report source does not bind to exact requested local image and repository digest',report_source={'type':source.get('type'),'userInput':supplied,'manifestDigest':manifest_digest,'imageID':target.get('imageID'),'repoDigests':repo_digests})
            matches=report['matches']
            if any(not isinstance(x,dict) or not isinstance(x.get('vulnerability'),dict) or not isinstance(x['vulnerability'].get('severity'),str) for x in matches): return hold('Grype report contains malformed findings')
            digest=_sha(raw); critical=sum(1 for x in matches if x['vulnerability']['severity']=='Critical'); high=sum(1 for x in matches if x['vulnerability']['severity']=='High')
            _atomic(report_path,raw)
            db_identity={'schemaVersion':db.get('schemaVersion'),'built':db.get('built'),'checksum':db_checksum,'checksum_scope':'actual validated database file bytes'}
            return {**base,'status':'FAIL' if matches else 'PASS','reason':'vulnerabilities found' if matches else 'no vulnerabilities reported for bound image','db_valid':True,'db_schema_version':db.get('schemaVersion'),'db_identity':db_identity,'db_built_at':db.get('built'),'scanner_sha256':expected_scanner_sha256,'report_path':str(report_path),'report_sha256':digest,'finding_count':len(matches),'critical_count':critical,'high_count':high,'source_config_id':target.get('imageID'), 'source_manifest_digest':manifest_digest,'source_repo_digest':expected_repo_digest,'source_identity':supplied,'scanner_exit_code':run.returncode}
        finally:
            try: temp.unlink(missing_ok=True)
            except OSError: pass
    except subprocess.TimeoutExpired: return hold('Grype database status timed out')
    except (OSError,ValueError,UnicodeError,DependencyRefused) as exc: return hold(f'Grype gate could not establish evidence: {exc}')
    finally:
        if config_path is not None: config_path.unlink(missing_ok=True)

def image_match_key(match:dict[str,Any])->str:
    """Stable identity for one scanner match, including package version and every location."""
    vuln=match.get('vulnerability'); artifact=match.get('artifact')
    if not isinstance(vuln,dict) or not isinstance(artifact,dict): raise DependencyRefused('scanner match lacks vulnerability or package artifact identity')
    locations=artifact.get('locations')
    if not isinstance(locations,list) or not locations: raise DependencyRefused('scanner match has no package locations')
    identity={'vulnerability_id':vuln.get('id'),'namespace':vuln.get('namespace'),'package':artifact.get('name'),'version':artifact.get('version'),
              'purl':artifact.get('purl'),'locations':locations}
    if any(not isinstance(identity[k],str) or not identity[k] for k in ('vulnerability_id','namespace','package','version')):
        raise DependencyRefused('scanner match identity is incomplete')
    return _sha(_canonical(identity))


def verify_image_applicability(report_path:Path, scan_gate_path:Path, signed_contract:bytes,
                               signature:bytes, pinned_public_key_pem:bytes, source_root:Path,
                               regression_path:Path, *, pinned_key_sha256:str, reviewer_signature:bytes,
                               reviewer_public_key_pem:bytes, reviewer_key_sha256:str, now:str|None=None)->dict[str,Any]:
    """Verify a separately signed, exact-scan applicability decision; never alters the raw scan."""
    if not re.fullmatch(r'[0-9a-f]{64}',pinned_key_sha256) or _sha(pinned_public_key_pem)!=pinned_key_sha256: raise DependencyRefused('operator-pinned contract signing key fingerprint missing or mismatched')
    if not re.fullmatch(r'[0-9a-f]{64}',reviewer_key_sha256) or _sha(reviewer_public_key_pem)!=reviewer_key_sha256 or reviewer_key_sha256==pinned_key_sha256: raise DependencyRefused('reviewer public key must be separately operator-pinned and distinct from contract signer')
    try:
        from datetime import datetime,timezone
        for original in (report_path,scan_gate_path,source_root,regression_path):
            _chain(Path(original))
        report_path=Path(report_path).resolve(strict=True); scan_gate_path=Path(scan_gate_path).resolve(strict=True)
        source_root=Path(source_root).resolve(strict=True); regression_path=Path(regression_path).resolve(strict=True)
        for path in (report_path,scan_gate_path,regression_path):
            if not path.is_file(): raise DependencyRefused('scan, gate, and regression evidence must be regular files')
        report_bytes=report_path.read_bytes(); gate=json.loads(scan_gate_path.read_text(encoding='utf-8'))
        report=json.loads(report_bytes.decode('utf-8'))
        if gate.get('status')!='FAIL' or gate.get('report_sha256')!=_sha(report_bytes) or gate.get('report_path')!=str(report_path):
            raise DependencyRefused('original scan gate must be FAIL and bind the exact unchanged report bytes/path')
        if not isinstance(report,dict) or not isinstance(report.get('matches'),list) or not report['matches']: raise DependencyRefused('scanner report must contain at least one finding')
        source=report.get('source'); target=source.get('target') if isinstance(source,dict) else None
        if not isinstance(target,dict) or source.get('type')!='image': raise DependencyRefused('scan report image identity missing')
        image=target.get('userInput'); repo_digests=target.get('repoDigests'); manifest=target.get('manifestDigest'); config=target.get('imageID')
        if not isinstance(image,str) or not re.fullmatch(r'.+@sha256:[0-9a-f]{64}',image) or not isinstance(repo_digests,list) or any(not isinstance(x,str) for x in repo_digests) or image not in repo_digests or not isinstance(manifest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',manifest) or not isinstance(config,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',config):
            raise DependencyRefused('scan report RepoDigest/config/manifest binding incomplete')
        if gate.get('source_repo_digest')!=image or gate.get('source_manifest_digest')!=manifest or gate.get('source_config_id')!=config:
            raise DependencyRefused('scan gate identity differs from exact raw report image/config/manifest')
        if gate.get('finding_count')!=len(report['matches']) or not isinstance(gate.get('scanner_sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',gate['scanner_sha256']): raise DependencyRefused('scan gate finding count or scanner identity mismatch')
        actual_now=datetime.fromisoformat((now or datetime.now(timezone.utc).isoformat()).replace('Z','+00:00'))
        if actual_now.tzinfo is None: raise DependencyRefused('verification time must be timezone-aware UTC')
        actual_now=actual_now.astimezone(timezone.utc)
        contract=verify_signed_json(signed_contract,signature,pinned_public_key_pem)
        if not isinstance(contract,dict) or type(contract.get('schema')) is not int or contract['schema']!=1: raise DependencyRefused('signed applicability contract schema invalid')
        verify_signed_json(signed_contract,reviewer_signature,reviewer_public_key_pem)
        required={'schema','scan_report_sha256','image_repo_digest','image_config_id','image_manifest_digest','scanner_sha256','db_identity','db_built_at','findings','behavior_regression_sha256','reviewer_identity','reviewed_at','expires_at'}
        if set(contract)!=required: raise DependencyRefused('signed applicability contract fields invalid')
        if not isinstance(contract,dict) or set(contract)!=required or type(contract.get('schema')) is not int or contract['schema']!=1: raise DependencyRefused('signed applicability contract schema/fields invalid')
        exact={'scan_report_sha256':_sha(report_bytes),'image_repo_digest':image,'image_config_id':config,'image_manifest_digest':manifest,
               'scanner_sha256':gate.get('scanner_sha256'),'behavior_regression_sha256':_sha(regression_path.read_bytes())}
        for key,value in exact.items():
            if contract.get(key)!=value: raise DependencyRefused(f'applicability contract {key} does not bind observed evidence')
        if not isinstance(contract['reviewer_identity'],str) or contract['reviewer_identity']!=reviewer_key_sha256: raise DependencyRefused('reviewer_identity must equal separately operator-pinned reviewer public-key SHA-256')
        def timestamp(value):
            if not isinstance(value,str): raise DependencyRefused('signed timestamps must be strings')
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None: raise DependencyRefused('signed timestamps must be timezone-aware')
            return parsed.astimezone(timezone.utc)
        if timestamp(contract['expires_at'])<=actual_now: raise DependencyRefused('applicability contract has expired')
        timestamp(contract['db_built_at']); timestamp(contract['reviewed_at'])
        if timestamp(contract['reviewed_at'])>actual_now: raise DependencyRefused('review timestamp is in the future')
        if not isinstance(contract['db_identity'],dict) or gate.get('db_identity')!=contract['db_identity'] or gate.get('db_built_at')!=contract['db_built_at']:
            raise DependencyRefused('contract database identity/time differs from scanner gate evidence')
        matches=report['matches']; match_keys=[image_match_key(x) for x in matches]
        if len(set(match_keys))!=len(match_keys): raise DependencyRefused('duplicate indistinguishable scanner matches require explicit manual reconciliation')
        expected=set(match_keys)
        entries=contract['findings']
        if not isinstance(entries,list) or len(entries)!=len(expected): raise DependencyRefused('every scanner match requires exactly one signed applicability decision')
        by_key={}
        for entry in entries:
            keys={'match_key','decision','source_type','source_url','source_file','source_sha256','behavior_regression_sha256'}
            if not isinstance(entry,dict) or set(entry)!=keys or entry['decision'] not in {'fixed','not_affected'} or entry['source_type'] not in {'upstream','vendor'}:
                raise DependencyRefused('finding decision/source record malformed or unknown')
            key=entry['match_key']
            if not isinstance(key,str) or key in by_key: raise DependencyRefused('duplicate or invalid match key')
            by_key[key]=entry
        if set(by_key)!=expected: raise DependencyRefused('signed applicability coverage does not exactly match original findings')
        for key,entry in by_key.items():
            match=next(x for x in matches if image_match_key(x)==key)
            url=entry['source_url']; filename=entry['source_file']; digest=entry['source_sha256']
            vuln=match['vulnerability']; declared=set(vuln.get('urls',[])) if isinstance(vuln.get('urls',[]),list) else set()
            if isinstance(vuln.get('dataSource'),str): declared.add(vuln['dataSource'])
            if not isinstance(url,str) or not url.startswith('https://') or url not in declared or not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9._-]+',filename) or filename in {'.','..'} or not re.fullmatch(r'[0-9a-f]{64}',digest):
                raise DependencyRefused('source must be an HTTPS upstream/vendor URL declared by this exact vulnerability match; basename and SHA-256 required')
            source_candidate=source_root/filename
            _chain(source_candidate)
            source_file=source_candidate.resolve(strict=True)
            if source_file.parent!=source_root or not source_file.is_file() or _sha(source_file.read_bytes())!=digest:
                raise DependencyRefused('source evidence path or SHA-256 differs from signed finding')
            if entry['behavior_regression_sha256']!=exact['behavior_regression_sha256']: raise DependencyRefused('per-finding regression digest mismatch')
        fingerprint=hashlib.sha256(pinned_public_key_pem).hexdigest()
        return {'schema':1,'status':'PASS_APPLICABILITY','original_scan_status':'FAIL','report_sha256':_sha(report_bytes),
                'image_repo_digest':image,'image_config_id':config,'image_manifest_digest':manifest,'scanner_sha256':exact['scanner_sha256'],
                'signer_public_key_pem_sha256':fingerprint,'signer_role':'operator-pinned Ed25519 key; reviewer identity is a signed declaration, not third-party authentication','contract_sha256':_sha(signed_contract),
                'candidate_execution_authorized':False,'raw_scan_modified':False,
                'limitation':'Signed reviewer applicability assessment only; original scanner FAIL and findings are retained. This is not publisher acceptance or vulnerability remediation.'}
    except DependencyRefused: raise
    except (OSError,ValueError,KeyError,UnicodeError,TypeError) as exc: raise DependencyRefused(f'applicability evidence invalid: {exc}') from exc
 
 
def _cli()->int:
    ap=argparse.ArgumentParser(description=__doc__); sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('import-artifact'); p.add_argument('source',type=Path); p.add_argument('cache',type=Path); p.add_argument('sha256'); p.add_argument('--source-id',required=True); p.add_argument('--license',required=True); p.add_argument('--package-name',required=True); p.add_argument('--package-version',required=True); p.add_argument('--authorized',action='store_true')
    p=sub.add_parser('acquire-https'); p.add_argument('url'); p.add_argument('cache',type=Path); p.add_argument('sha256'); p.add_argument('--sources',type=Path,required=True); p.add_argument('--source-id',required=True); p.add_argument('--license',required=True); p.add_argument('--package-name',required=True); p.add_argument('--package-version',required=True); p.add_argument('--authorized',action='store_true')
    p=sub.add_parser('verify-cache'); p.add_argument('cache',type=Path); p.add_argument('records',type=Path); p.add_argument('--sources',required=True)
    p=sub.add_parser('audit-advisories'); p.add_argument('lock',type=Path); p.add_argument('--snapshot',type=Path); p.add_argument('--sources',type=Path,required=True); p.add_argument('--now')
    p=sub.add_parser('verify-signed-json'); p.add_argument('document',type=Path); p.add_argument('signature',type=Path); p.add_argument('pinned_public_key',type=Path)
    p=sub.add_parser('verify-image-applicability'); p.add_argument('report',type=Path); p.add_argument('scan_gate',type=Path); p.add_argument('contract',type=Path); p.add_argument('signature',type=Path); p.add_argument('pinned_public_key',type=Path); p.add_argument('source_root',type=Path); p.add_argument('behavior_regression',type=Path); p.add_argument('--pinned-key-sha256',required=True); p.add_argument('--reviewer-signature',type=Path,required=True); p.add_argument('--reviewer-public-key',type=Path,required=True); p.add_argument('--reviewer-key-sha256',required=True); p.add_argument('--now')
    p=sub.add_parser('supply-chain-gate'); p.add_argument('lock',type=Path); p.add_argument('cache',type=Path); p.add_argument('records',type=Path); p.add_argument('sources',type=Path); p.add_argument('snapshot',type=Path,nargs='?'); p.add_argument('advisory_signature',type=Path,nargs='?'); p.add_argument('license_policy',type=Path,nargs='?'); p.add_argument('license_signature',type=Path,nargs='?'); p.add_argument('pinned_public_key',type=Path,nargs='?'); p.add_argument('--license-policy-file',type=Path); p.add_argument('--license-signature-file',type=Path); p.add_argument('--pinned-key-file',type=Path); p.add_argument('--local-root',type=Path); p.add_argument('--local-bundle',type=Path); p.add_argument('--local-signature',type=Path); p.add_argument('--now'); p.add_argument('--authorized',action='store_true'); p.add_argument('--refresh-osv',action='store_true'); p.add_argument('--engine',choices=('docker','podman')); p.add_argument('--image-scanner',type=Path); p.add_argument('--image-scanner-sha256'); p.add_argument('--image-db-cache',type=Path); p.add_argument('--image-report',type=Path); p.add_argument('--image-repo-digest')
    p=sub.choices['supply-chain-gate']; p.add_argument('--publisher-required',action='store_true'); p.add_argument('--publisher-artifact',type=Path); p.add_argument('--publisher-signature',type=Path); p.add_argument('--publisher-keyring',type=Path); p.add_argument('--publisher-fingerprint'); p.add_argument('--publisher-source-url'); p.add_argument('--publisher-signature-url'); p.add_argument('--publisher-version'); p.add_argument('--publisher-package-name')
    p=sub.add_parser('verify-publisher'); p.add_argument('artifact',type=Path); p.add_argument('signature',type=Path); p.add_argument('keyring',type=Path); p.add_argument('--fingerprint',required=True); p.add_argument('--source-url',required=True); p.add_argument('--signature-url',required=True); p.add_argument('--version',required=True); p.add_argument('--sha256'); p.add_argument('--gpgv',default='gpgv')
    p=sub.add_parser('refresh-osv'); p.add_argument('lock',type=Path); p.add_argument('--sources',type=Path,required=True); p.add_argument('--snapshot-out',type=Path,required=True); p.add_argument('--output',type=Path); p.add_argument('--timeout',type=int,default=20)
    p=sub.add_parser('verify-local-bundle'); p.add_argument('root',type=Path); p.add_argument('bundle',type=Path); p.add_argument('signature',type=Path); p.add_argument('pinned_public_key',type=Path); p.add_argument('--sources',required=True)
    a=ap.parse_args()
    try:
        if a.cmd=='import-artifact': result=import_artifact(a.source,a.cache,a.sha256,source_id=a.source_id,license_record=a.license,package_name=a.package_name,package_version=a.package_version,authorized=a.authorized)
        elif a.cmd=='acquire-https': result=acquire_https(a.url,a.cache,a.sha256,source_id=a.source_id,sources=json.loads(a.sources.read_text(encoding='utf-8')),license_record=a.license,package_name=a.package_name,package_version=a.package_version,authorized=a.authorized)
        elif a.cmd=='verify-image-applicability': result=verify_image_applicability(a.report,a.scan_gate,a.contract.read_bytes(),a.signature.read_bytes(),a.pinned_public_key.read_bytes(),a.source_root,a.behavior_regression,pinned_key_sha256=a.pinned_key_sha256,reviewer_signature=a.reviewer_signature.read_bytes(),reviewer_public_key_pem=a.reviewer_public_key.read_bytes(),reviewer_key_sha256=a.reviewer_key_sha256,now=a.now)
        elif a.cmd=='verify-cache': result=verify_cache(a.cache,json.loads(a.records.read_text(encoding='utf-8')),expected_sources=set(a.sources.split(',')))
        elif a.cmd=='verify-signed-json': result={'status':'PASS','document':verify_signed_json(a.document.read_bytes(),a.signature.read_bytes(),a.pinned_public_key.read_bytes())}
        elif a.cmd=='verify-publisher': result=verify_publisher_attestation(a.artifact,a.signature,a.keyring,expected_fingerprint=a.fingerprint,source_url=a.source_url,signature_url=a.signature_url,expected_version=a.version,expected_sha256=a.sha256,gpgv=a.gpgv)
        elif a.cmd=='supply-chain-gate':
            if a.refresh_osv:
                if not a.authorized or not a.engine or not a.license_policy_file or not a.license_signature_file or not a.pinned_key_file:
                    raise DependencyRefused('--refresh-osv requires --authorized, --engine, and signed license policy/key files')
                snapshot_bytes=None; snapshot=None; advisory_sig=None
                license_path=a.license_policy_file; license_sig_path=a.license_signature_file; key_path=a.pinned_key_file
            else:
                if not all((a.snapshot,a.advisory_signature,a.license_policy,a.license_signature,a.pinned_public_key)):
                    raise DependencyRefused('offline gate requires snapshot, advisory signature, license policy/signature and pinned key')
                snapshot_bytes=a.snapshot.read_bytes(); snapshot=json.loads(snapshot_bytes.decode('utf-8')); advisory_sig=a.advisory_signature.read_bytes()
                license_path=a.license_policy; license_sig_path=a.license_signature; key_path=a.pinned_public_key
            lock=json.loads(a.lock.read_text(encoding='utf-8')); records=json.loads(a.records.read_text(encoding='utf-8')); sources=json.loads(a.sources.read_text(encoding='utf-8'))
            license_bytes=license_path.read_bytes(); license_policy=json.loads(license_bytes.decode('utf-8'))
            result=supply_chain_gate(lock,sources=sources,artifacts=records,cache=a.cache,snapshot=snapshot,snapshot_bytes=snapshot_bytes,
                advisory_signature=advisory_sig,license_policy=license_policy,license_policy_bytes=license_bytes,
                license_signature=license_sig_path.read_bytes(),pinned_public_key_pem=key_path.read_bytes(),
                local_artifact_root=a.local_root,local_bundle_bytes=a.local_bundle.read_bytes() if a.local_bundle else None,
                local_bundle_signature=a.local_signature.read_bytes() if a.local_signature else None,
                authorized=a.authorized,now=a.now,refresh_osv=a.refresh_osv,engine=a.engine,image_scanner=a.image_scanner,image_scanner_sha256=a.image_scanner_sha256,image_db_cache=a.image_db_cache,image_report_path=a.image_report,image_repo_digest=a.image_repo_digest,
                publisher_required=a.publisher_required,publisher_artifact=a.publisher_artifact,publisher_signature=a.publisher_signature,publisher_keyring=a.publisher_keyring,publisher_fingerprint=a.publisher_fingerprint,publisher_source_url=a.publisher_source_url,publisher_signature_url=a.publisher_signature_url,publisher_version=a.publisher_version,publisher_package_name=a.publisher_package_name)
        elif a.cmd=='audit-advisories':
            result=audit_advisories_file(a.lock,a.snapshot,sources=json.loads(a.sources.read_text(encoding='utf-8')),now=a.now)
        elif a.cmd=='refresh-osv':
            lock=json.loads(a.lock.read_text(encoding='utf-8')); sources=json.loads(a.sources.read_text(encoding='utf-8'))
            result=query_osv(lock,sources=sources,timeout=a.timeout)
            if result['status'] in {'PASS','FAIL'} and 'snapshot' in result: _atomic(a.snapshot_out,_canonical(result['snapshot'])+b'\n')
            if a.output is not None: _atomic(a.output,_canonical(result)+b'\n')
        elif a.cmd=='verify-local-bundle':
            result=verify_local_artifact_bundle(a.root,a.bundle.read_bytes(),a.signature.read_bytes(),a.pinned_public_key.read_bytes(),expected_sources=set(a.sources.split(',')))
        print(json.dumps(result,sort_keys=True)); return 1 if result.get('status') in {'FAIL','HOLD'} else 0
    except (DependencyRefused,OSError,ValueError,KeyError) as exc: print(f'REFUSED: {exc}',file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(_cli())
