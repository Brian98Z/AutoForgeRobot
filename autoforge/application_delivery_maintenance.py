"""Create and maintain an integrity-bound package for a checked JSON-lines candidate."""
from __future__ import annotations

import shutil
import json
import sys
from pathlib import Path
from typing import Any

import argparse
import application_builder as builder
import application_checkpoint as checkpoint
import application_delivery as delivery

LICENSES = """# License and redistribution status\n\nThe generated candidate has no license grant in the available evidence. Authorship and redistribution rights are unknown; obtain a rights-holder decision before redistribution. The AutoForge repository license does not grant rights to candidate files. Operators must review the exact base-image license and notices.\n"""
LIMITATIONS = """# Known limits\n\n- Evidence covers only the exact JSON-lines contract; it is not general correctness, security, compatibility, accessibility, production, or performance certification.\n- Runtime requires the exact local Linux Docker image ID; no remote image pull is performed.\n- The runner uses a network-disabled, read-only, non-root, resource-limited container.\n- The application_operations HTTP service lifecycle is not applicable to this JSON-lines candidate.\n- No database migration was executed. No data rollback is claimed.\n- Generated candidate licensing is unknown; do not redistribute without authorization.\n"""


def diagnose(checkpoint_path: Path) -> dict[str, Any]:
    """Read-only format and in-flight diagnosis; makes no compatibility or resume claim."""
    path = Path(checkpoint_path)
    if path.is_dir():
        path = path / 'session.json'
    envelope = delivery._load(path, 32 * 1024 * 1024)
    if not isinstance(envelope, dict) or set(envelope) != {'schema', 'state', 'sha256'} or type(envelope['schema']) is not int or envelope['schema'] != 1 or not isinstance(envelope['state'], dict):
        raise delivery.DeliveryRefused('checkpoint envelope/state malformed')
    state = envelope['state']
    expected = checkpoint.digest(checkpoint.canonical(state))
    if envelope.get('sha256') != expected:
        raise delivery.DeliveryRefused('checkpoint digest mismatch')
    if path.name == 'session.json':
        if type(state.get('schema')) is not int or state['schema'] != 1:
            return {'status': 'REFUSED', 'kind': 'agent-session', 'reason': 'unsupported session schema; no migration is defined'}
        if state.get('inflight') is not None:
            return {'status': 'REFUSED', 'kind': 'agent-session', 'reason': 'external call was in flight; side effects are ambiguous; do not replay or upgrade; inspect agent_reconcile.py for its narrowly eligible validate case'}
        return {'status': 'VALID_ENVELOPE_READ_ONLY', 'kind': 'agent-session', 'schema': 1, 'compatibility': 'not assessed; no source or runtime configuration compatibility check'}
    if state.get('schema') != 1:
        return {'status': 'REFUSED', 'kind': 'cycle-checkpoint', 'reason': 'unsupported checkpoint schema; no migration is defined'}
    if state.get('inflight') is not None:
        return {'status': 'REFUSED', 'kind': 'cycle-checkpoint', 'reason': 'external call was in flight; side effects are ambiguous; do not replay or upgrade'}
    return {'status': 'VALID_ENVELOPE_READ_ONLY', 'kind': 'cycle-checkpoint', 'schema': 1, 'compatibility': 'digest/schema/inflight inspected read-only; no source compatibility check'}


def replay(pack: Path, engine: str, image_id: str) -> dict[str, Any]:
    valid = delivery.validate_pack(pack)
    package = Path(pack).absolute()
    metadata = delivery._load(package / 'delivery.json')
    recorded = metadata.get('engine_metadata', {})
    if not isinstance(recorded, dict) or recorded.get('engine') != engine or recorded.get('image_id') != image_id:
        raise delivery.DeliveryRefused('replay engine/image must match the sealed package metadata')
    contract_path = package / 'maintenance' / ('CONTRACT.json' if valid['maintenance_package_schema'] == 1 else 'v2/contracts/jsonl-v1.json')
    contract = delivery._load(contract_path, builder.MAX_INPUT)
    source = package / 'candidate'
    raw = delivery._load(source / 'manifest.json')
    manifest = {'schema': 1, 'goal': raw['goal'], 'references': raw['references'], 'files': raw['files'], 'candidate_sha256': raw['candidate_sha256'], 'workspace': str(source)}
    info = builder.preflight(engine, image_id)
    report = builder.build({'schema': 1, 'workspace_manifest': manifest, 'candidate_sha256': raw['candidate_sha256']}, contract, engine, image_id, info)
    report.update({'package_status': valid['status'], 'image_id': info['image_id'], 'candidate_executed_on_host': False})
    return report

MAINTENANCE_PACKAGE_SCHEMA = 2
MAINTENANCE_V2_FILES = {
    'README.md': 'maintenance/v2/archive-v1/docs/README.md',
    'API.md': 'maintenance/v2/archive-v1/docs/API.md',
    'CONFIG.example.json': 'maintenance/v2/archive-v1/config/CONFIG.example.json',
    'LICENSES.md': 'maintenance/v2/archive-v1/docs/LICENSES.md',
    'LIMITATIONS.md': 'maintenance/v2/archive-v1/docs/LIMITATIONS.md',
    'CONTRACT.json': 'maintenance/v2/contracts/jsonl-v1.json',
    'requirements.lock': 'maintenance/v2/archive-v1/runtime/requirements.lock',
}


def upgrade_package(source: Path, output: Path) -> dict[str, Any]:
    """Migrate bundled maintenance format 1 to the directory-structured format 2.

    Candidate and evidence bytes are preserved; delivery envelope schema remains 1.
    Publication is to a new absent destination; the source is never rewritten.
    """
    source, output = Path(source).absolute(), Path(output).absolute()
    if output == source or source in output.parents:
        raise delivery.DeliveryRefused('upgrade destination must not be inside its source package')
    delivery._chain(output)
    if output.exists():
        raise delivery.DeliveryRefused('upgrade destination must be absent')
    validated = delivery.validate_pack(source)
    old_metadata_bytes = (source / 'delivery.json').read_bytes()
    old_metadata = delivery._load(source / 'delivery.json')
    if validated['maintenance_package_schema'] != 1:
        raise delivery.DeliveryRefused('upgrade accepts maintenance format 1 only')
    if old_metadata.get('maintenance_bundle_required') is not True:
        raise delivery.DeliveryRefused('upgrade requires a bundled maintenance package')
    if type(old_metadata.get('schema')) is not int or old_metadata['schema'] != 1:
        raise delivery.DeliveryRefused('only validated schema-1 delivery packages can be upgraded')
    backup_digest = checkpoint.digest(old_metadata_bytes)
    staging = output.with_name(output.name + f'.upgrade-{__import__("uuid").uuid4().hex}')
    delivery._chain(staging)
    if staging.exists():
        raise delivery.DeliveryRefused('upgrade staging path already exists')
    try:
        shutil.copytree(source, staging, copy_function=shutil.copy2)
        old_contract = delivery._load(staging / 'maintenance' / 'CONTRACT.json', builder.MAX_INPUT)
        if not isinstance(old_contract, dict) or old_contract.get('schema') != 1 or old_contract.get('entrypoint') != 'main.py':
            raise delivery.DeliveryRefused('schema-1 maintenance JSONL contract is not supported')
        for old_name, new_name in MAINTENANCE_V2_FILES.items():
            old_path = staging / 'maintenance' / old_name
            target = staging.joinpath(*new_name.split('/'))
            target.parent.mkdir(parents=True, exist_ok=True)
            old_path.replace(target)
        delivery._write_json(staging / 'maintenance' / 'v2' / 'format.json', {
            'schema': 2, 'contract_schema': 1, 'files': MAINTENANCE_V2_FILES,
            'operator_entrypoint': 'maintenance/v2/README.md',
            'archived_v1_docs_are_non_authoritative': True,
        })
        v2_readme = '''# Maintenance package format 2

This is the authoritative operator entrypoint. `archive-v1/` contains original schema-1 documentation verbatim for provenance only; do not use its paths or command examples. The v2 contract is `contracts/jsonl-v1.json` (JSON-lines contract schema 1). Candidate and evidence are unchanged by migration.

Run from the AutoForge source directory; destinations must be absent:

```text
python autoforge/application_delivery_maintenance.py validate <package>
python autoforge/application_delivery_maintenance.py selfcheck --engine docker --image <sealed-sha256-image-id>
python autoforge/application_delivery_maintenance.py replay <package> --engine docker --image <sealed-sha256-image-id> --report <new-report.json>
```

`validate` accepts maintenance formats 1 and 2. `replay` validates the sealed runtime identity and consumes the version-appropriate contract. Export copies an already verified package to a new absent directory. Upgrade accepts only a validated bundled format-1 package with the supported schema-1 JSONL contract; it refuses format 2 and non-maintenance packages. Diagnose is read-only; ambiguous in-flight state must not be replayed or upgraded.
'''
        (staging / 'maintenance' / 'v2' / 'README.md').write_text(v2_readme, encoding='utf-8', newline='\n')
        provenance = {'schema': MAINTENANCE_PACKAGE_SCHEMA, 'source_delivery_schema': 1,
                      'source_metadata_sha256': backup_digest,
                      'source_inventory_sha256': validated['inventory']['inventory_sha256'],
                      'candidate_sha256': validated['candidate_sha256'],
                      'transformed_documentation': {'archive_root': 'maintenance/v2/archive-v1/',
                          'archive_bytes_preserved': True, 'entrypoint': 'maintenance/v2/README.md',
                          'entrypoint_sha256': checkpoint.digest(v2_readme.encode('utf-8'))}}
        (staging / 'maintenance' / 'UPGRADE-SOURCE-delivery.json').write_bytes(old_metadata_bytes)
        delivery._write_json(staging / 'maintenance' / 'UPGRADE.json', provenance)
        metadata = delivery._load(staging / 'delivery.json')
        metadata['inventory'] = delivery._inventory(delivery._tree(staging))
        metadata['maintenance_package_schema'] = MAINTENANCE_PACKAGE_SCHEMA
        metadata['upgrade_provenance_sha256'] = checkpoint.digest(checkpoint.canonical(provenance))
        metadata.pop('metadata_sha256', None)
        metadata['metadata_sha256'] = checkpoint.digest(checkpoint.canonical(metadata))
        delivery._write_json(staging / 'delivery.json', metadata)
        copied = delivery.validate_pack(staging)
        if copied['candidate_sha256'] != validated['candidate_sha256']:
            raise delivery.DeliveryRefused('upgraded package candidate binding changed')
        if output.exists():
            raise delivery.DeliveryRefused('upgrade destination appeared during publication')
        staging.rename(output)
        return {'status': 'UPGRADED', 'source_maintenance_format': 1,
                'maintenance_package_schema': MAINTENANCE_PACKAGE_SCHEMA,
                'candidate_sha256': copied['candidate_sha256'], 'source_mutated': False,
                'source_metadata_backup_sha256': backup_digest, 'inventory': copied['inventory']}
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
def export_package(source: Path, output: Path) -> dict[str, Any]:
    """Copy a validated package to an absent destination and verify its exact inventory."""
    before = delivery.validate_pack(source)
    source, output = Path(source).absolute(), Path(output).absolute()
    if output == source or source in output.parents:
        raise delivery.DeliveryRefused('export destination must not be inside its source package')
    delivery._chain(output)
    if output.exists():
        raise delivery.DeliveryRefused('export destination must be absent')
    shutil.copytree(source, output, copy_function=shutil.copy2)
    copied = delivery.validate_pack(output)
    if (source / 'delivery.json').read_bytes() != (output / 'delivery.json').read_bytes():
        raise delivery.DeliveryRefused('exported package metadata differs from source')
    if before['candidate_sha256'] != copied['candidate_sha256'] or before['inventory'] != copied['inventory']:
        raise delivery.DeliveryRefused('exported package bytes differ from validated source')
    return {'status': 'EXPORTED', 'candidate_sha256': copied['candidate_sha256'], 'files': copied['files'], 'inventory': copied['inventory'], 'source_mutated': False}


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('pack')
    p.add_argument('candidate', type=Path); p.add_argument('evidence', type=Path)
    p.add_argument('dependency_metadata', type=Path); p.add_argument('engine_metadata', type=Path)
    p.add_argument('contract', type=Path); p.add_argument('image_id'); p.add_argument('output', type=Path)
    p = sub.add_parser('validate'); p.add_argument('pack', type=Path)
    p = sub.add_parser('export'); p.add_argument('pack', type=Path); p.add_argument('output', type=Path)
    p = sub.add_parser('selfcheck'); p.add_argument('--engine', default='docker'); p.add_argument('--image', required=True)
    p = sub.add_parser('replay'); p.add_argument('pack', type=Path); p.add_argument('--engine', default='docker'); p.add_argument('--image', required=True); p.add_argument('--report', type=Path)
    p = sub.add_parser('diagnose'); p.add_argument('checkpoint', type=Path)
    p = sub.add_parser('upgrade'); p.add_argument('package', type=Path); p.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'pack':
            digest = delivery._read_candidate(args.candidate.absolute())[2]
            validation = delivery._load(args.evidence)
            if validation.get('candidate_sha256') != digest:
                raise delivery.DeliveryRefused('validation does not match candidate source digest')
            result = bundle(args.candidate, validation, delivery._load(args.dependency_metadata), delivery._load(args.engine_metadata), engine='docker', image_id=args.image_id, contract=delivery._load(args.contract), output=args.output)
            if delivery._read_candidate(args.candidate.absolute())[2] != digest:
                raise delivery.DeliveryRefused('candidate changed during package creation')
        elif args.action == 'export':
            result = export_package(args.pack, args.output)
        elif args.action == 'validate':
            result = delivery.validate_pack(args.pack)
        elif args.action == 'selfcheck':
            result = {'status': 'READY', **builder.preflight(args.engine, args.image)}
        elif args.action == 'replay':
            result = replay(args.pack, args.engine, args.image)
            if args.report:
                encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + '\n'
                if len(encoded.encode('utf-8')) > builder.MAX_REPORT:
                    raise delivery.DeliveryRefused('diagnostic report exceeds size limit')
                args.report.write_text(encoded, encoding='utf-8')
        elif args.action == 'diagnose':
            result = diagnose(args.checkpoint)
        elif args.action == 'upgrade':
            result = upgrade_package(args.package, args.output)
        else:
            result = {'status': 'REFUSED', 'reason': 'unsupported action'}
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0 if result.get('status') not in {'REFUSED', 'BLOCKED'} and result.get('build_pass') is not False and result.get('test_pass') is not False else 2
    except (delivery.DeliveryRefused, builder.MissingPrerequisite, builder.BuildFailure, checkpoint.ResumeRefused, OSError, ValueError, KeyError) as exc:
        missing = isinstance(exc, builder.MissingPrerequisite)
        print(json.dumps({'status': 'BLOCKED' if missing else 'REFUSED', 'reason': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3 if missing else 2


def bundle(candidate_dir: Path, validation: dict[str, Any], dependency_metadata: dict[str, Any], engine_metadata: dict[str, Any], *, engine: str, image_id: str, contract: dict[str, Any], output: Path) -> dict[str, Any]:
    candidate, _, digest = delivery._read_candidate(Path(candidate_dir).absolute())
    if validation.get('status') != 'PASS' or validation.get('candidate_sha256') != digest:
        raise delivery.DeliveryRefused('maintenance bundle requires exact passing validation')
    if engine != 'docker' or not isinstance(image_id, str) or not image_id.startswith('sha256:'):
        raise delivery.DeliveryRefused('maintenance instructions require Docker and an exact image ID')
    if not isinstance(engine_metadata, dict) or engine_metadata.get('engine') != engine or engine_metadata.get('image_id') != image_id:
        raise delivery.DeliveryRefused('maintenance runtime must match engine metadata')
    if not isinstance(contract, dict) or contract.get('schema') != 1 or contract.get('entrypoint') != 'main.py':
        raise delivery.DeliveryRefused('versioned JSONL contract required')
    contract_json = json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    api = '''# Public interface\n\nThis candidate is a JSON-lines process: read one UTF-8 JSON object per stdin line and emit one JSON object per stdout line. It does not expose an HTTP listener. Exact request/response behavior is recorded in `CONTRACT.json`. Implementation source is `candidate/main.py`.\n'''
    config = json.dumps({'environment': 'example-only; JSONL candidate does not consume runtime config'}, ensure_ascii=False, indent=2) + '\n'
    lock = '''# Offline Python dependency lock\n# No third-party Python packages are required by this candidate.\n# The pinned interpreter image ID is recorded in delivery.json.\n'''
    readme = f'''# Offline JSON-lines release package

Goal: {candidate['goal']}
Candidate SHA-256: `{digest}`

## Contents

- `candidate/`: exact candidate source and manifest.
- `evidence.json`: validation evidence bound to this candidate digest.
- `delivery.json`: integrity-checked inventory, dependencies, and engine metadata.
- `maintenance/CONTRACT.json`: exact JSON-lines request/expected-response contract.

## Offline preflight

Linux Docker must already contain exact image `{image_id}`. Commands pass its immutable ID directly; no remote pull/network occurs. Host orchestration requires Python 3.11.

## Maintainer commands

Run from the AutoForge source directory. Use an absent output path for each new immutable package.

```text
python autoforge/application_delivery_maintenance.py upgrade <validated-schema-1-package> <new-absent-package-dir>
docker run --rm --network=none --read-only --user=65534:65534 --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=64 --memory=256m --cpus=1 --mount type=bind,src=<absolute-package>/candidate,dst=/candidate,readonly --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777 --workdir /candidate --interactive --entrypoint python {image_id} -I -u /candidate/main.py
```

This direct command accepts JSON lines on stdin; to check all declared requests and exact responses, prefer the full CLI `export` then `replay` sequence below.

```text
python autoforge/application_delivery_maintenance.py export <verified-source-package> <new-absent-export-dir>
python autoforge/application_delivery_maintenance.py validate <new-absent-export-dir>
python autoforge/application_delivery_maintenance.py selfcheck --engine docker --image {image_id}
python autoforge/application_delivery_maintenance.py replay <new-absent-export-dir> --engine docker --image {image_id} --report <bounded-report.json>
```
Rollback means selecting a prior verified immutable package and rerunning that package's exact `replay` contract. No data rollback is claimed; this is not production deployment.

`replay` verifies package integrity then invokes the existing isolated builder against packaged JSONL scenarios. Candidate runs only in Docker, never on host. Missing exact image or non-Linux engine blocks before execution. Exit 0 requires exact response matches; the bounded report includes observations and limitations. This is contract evidence, not production deployment.

`diagnose` checks checkpoint/session envelopes read-only. Session/checkpoint migration is not offered: the old state cannot prove current source and executor identity. `upgrade` migrates only a fully validated bundled maintenance-format-1 package with a schema-1 JSONL contract. It moves original docs, config, license/limits and lock files byte-for-byte under `maintenance/v2/archive-v1/`; those old instructions and paths are explicitly non-authoritative. The new operator entrypoint is `maintenance/v2/README.md` and its digest and transformation classification are sealed in `UPGRADE.json` and checked by validate. Contract moves to `maintenance/v2/contracts/jsonl-v1.json`. Candidate/evidence bytes stay identical; every source-inventory payload row is checked against its mapped v2 or unchanged path. Unknown versions, unbundled packages, and unsupported contract shapes are refused. `validate` and `replay` independently validate both layouts; replay reads the versioned contract. Unknown/in-flight checkpoint effects are refused read-only and never migrated or replayed.
'''
    maintenance = {'README.md': readme, 'API.md': api, 'CONTRACT.json': contract_json,
                   'CONFIG.example.json': config, 'LICENSES.md': LICENSES,
                   'LIMITATIONS.md': LIMITATIONS, 'requirements.lock': lock}
    return delivery.create_pack(candidate_dir, validation, dependency_metadata, engine_metadata, output, maintenance_files=maintenance)


if __name__ == '__main__':
    raise SystemExit(_cli())
