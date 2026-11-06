"""Build, upgrade, and container-replay a fresh package against its exact local runtime."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import application_builder as builder
import application_delivery as delivery
import application_delivery_maintenance as maintenance
import applications

ROOT = Path(__file__).resolve().parent
HISTORICAL = ROOT / 'bootstrap-runs' / 'maintenance-upgrade-real-20261104-rerun3' / 'package'
IMAGE = 'sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60'
OUT = ROOT / 'bootstrap-runs' / 'maintenance-upgrade-real-20261104-rerun5'

def main() -> None:
    if OUT.exists():
        raise RuntimeError(f'smoke output must not exist: {OUT}')
    manifest_path = HISTORICAL / 'candidate' / 'manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if type(manifest.get('schema')) is not int or manifest['schema'] != 1:
        raise RuntimeError('historical candidate manifest is not schema 1')
    source_files = {}
    for record in manifest['files']:
        if set(record) != {'path', 'size', 'sha256'}:
            raise RuntimeError('historical candidate member record malformed')
        data = (HISTORICAL / 'candidate' / record['path']).read_bytes()
        if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise RuntimeError(f'historical listed candidate member failed digest: {record["path"]}')
        source_files[record['path']] = data.decode('utf-8')
    candidate = {'schema': 1, 'goal': manifest['goal'], 'references': manifest['references'],
                 'files': [{'path': path, 'content': source_files[path]} for path in sorted(source_files)]}
    source = HISTORICAL
    source_before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    source_validation = delivery.validate_pack(source)
    if source_validation['maintenance_package_schema'] != 1 or source_validation['candidate_sha256'] != manifest['candidate_sha256']:
        raise RuntimeError('historical source is not a validated maintenance-format-1 package')
    OUT.mkdir()
    image_info = builder.preflight('docker', IMAGE)
    upgraded = OUT / 'upgraded'
    upgraded_result = maintenance.upgrade_package(source, upgraded)
    upgraded_validation = delivery.validate_pack(upgraded)
    if upgraded_validation['maintenance_package_schema'] != 2:
        raise RuntimeError('upgrade did not produce maintenance format 2')
    selfcheck = builder.preflight('docker', IMAGE)
    if selfcheck != image_info:
        raise RuntimeError('selfcheck identity differs from replay runtime')
    replay = maintenance.replay(upgraded, 'docker', IMAGE)
    if replay.get('build_pass') is not True or replay.get('test_pass') is not True or replay.get('candidate_sha256') != manifest['candidate_sha256']:
        raise RuntimeError(f'upgraded package replay did not pass: {replay}')
    source_after = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    if source_before != source_after:
        raise RuntimeError('historical source package changed during migration')
    delivery._write_json(OUT / 'smoke-report.json', {
        'status': 'PASS', 'delivery_schema': 1, 'source_maintenance_package_schema': 1, 'maintenance_package_schema': 2,
        'candidate_sha256': manifest['candidate_sha256'], 'image_id': IMAGE,
        'source_validation': source_validation, 'upgraded_validation': upgraded_validation,
        'selfcheck': {'status': 'READY', **selfcheck}, 'upgrade': upgraded_result, 'replay': replay,
        'historical_candidate_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'source_package_bytes_unchanged': True,
    })
    print(json.dumps({'status': 'PASS', 'candidate_sha256': manifest['candidate_sha256'],
                      'delivery_schema': 1, 'source_maintenance_package_schema': 1, 'maintenance_package_schema': 2,
                      'image_id': IMAGE, 'upgrade': upgraded_result['status'],
                      'source_validation': source_validation['status'], 'upgraded_validation': upgraded_validation['status'],
                      'selfcheck': 'READY', 'source_package_bytes_unchanged': True,
                      'replay': replay}, sort_keys=True, ensure_ascii=False))

if __name__ == '__main__':
    main()
