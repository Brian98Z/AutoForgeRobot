"""Check immutable export and maintenance-package upgrade without candidate execution."""
from __future__ import annotations
import shutil
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import application_checkpoint as checkpoint
import application_delivery as delivery
import application_delivery_maintenance as maintenance
import applications


def reject(fn):
    try: fn()
    except delivery.DeliveryRefused: return
    raise AssertionError('unsafe maintenance action accepted')


def main():
    with tempfile.TemporaryDirectory(prefix='maintenance-check-') as td:
        root = Path(td); candidate = root / 'candidate'
        applications.stage_candidate({'schema': 1, 'goal': 'maintenance boundary check', 'references': [], 'files': [{'path': 'main.py', 'content': '# never executed\n'}]}, candidate)
        digest = json.loads((candidate / 'manifest.json').read_text())['candidate_sha256']
        image = 'sha256:' + 'a' * 64
        evidence = {'status': 'PASS', 'candidate_sha256': digest}
        contract = {'schema': 1, 'entrypoint': 'main.py', 'scenarios': [{'name': 'unused', 'requests': [{}], 'expected': [{}]}]}
        pack = root / 'pack'
        maintenance.bundle(candidate, evidence, {'packages': []}, {'engine': 'docker', 'image_id': image}, engine='docker', image_id=image, contract=contract, output=pack)
        original = {p.relative_to(pack).as_posix(): p.read_bytes() for p in pack.rglob('*') if p.is_file()}
        reject(lambda: maintenance.export_package(pack, pack / 'recursive-copy'))
        assert not (pack / 'recursive-copy').exists()
        exported = root / 'exported'
        maintenance.export_package(pack, exported)
        assert original == {p.relative_to(exported).as_posix(): p.read_bytes() for p in exported.rglob('*') if p.is_file()}
        reject(lambda: maintenance.export_package(pack, exported))
        reject(lambda: maintenance.replay(pack, 'docker', 'sha256:' + 'b' * 64))
        reject(lambda: maintenance.bundle(candidate, evidence, {}, {'engine': 'docker', 'image_id': image}, engine='docker', image_id='sha256:' + 'b' * 64, contract=contract, output=root / 'bad-runtime'))
        source_before = {p.relative_to(pack).as_posix(): p.read_bytes() for p in pack.rglob('*') if p.is_file()}
        metadata_path = pack / 'delivery.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        metadata['maintenance_package_schema'] = 99
        unsigned = {k: v for k, v in metadata.items() if k != 'metadata_sha256'}
        metadata['metadata_sha256'] = checkpoint.digest(checkpoint.canonical(unsigned))
        metadata_path.write_bytes(checkpoint.canonical(metadata) + b'\n')
        unsupported_before = {p.relative_to(pack).as_posix(): p.read_bytes() for p in pack.rglob('*') if p.is_file()}
        reject(lambda: maintenance.upgrade_package(pack, root / 'failed-upgrade'))
        assert not (root / 'failed-upgrade').exists()
        assert unsupported_before == {p.relative_to(pack).as_posix(): p.read_bytes() for p in pack.rglob('*') if p.is_file()}
        metadata_path.write_bytes(source_before['delivery.json'])
        unrelated = root / 'unrelated.txt'; unrelated.write_text('keep', encoding='utf-8')
        upgraded = root / 'upgraded'
        completed = subprocess.run([sys.executable, str(Path(maintenance.__file__).resolve()), 'upgrade', str(pack), str(upgraded)], check=True, capture_output=True, text=True)
        assert json.loads(completed.stdout)['status'] == 'UPGRADED'
        validation = subprocess.run([sys.executable, str(Path(maintenance.__file__).resolve()), 'validate', str(upgraded)], check=True, capture_output=True, text=True)
        assert not (upgraded / 'maintenance' / 'CONTRACT.json').exists()
        assert (upgraded / 'maintenance' / 'v2' / 'contracts' / 'jsonl-v1.json').read_bytes() == source_before['maintenance/CONTRACT.json']
        assert (upgraded / 'candidate' / 'manifest.json').read_bytes() == source_before['candidate/manifest.json']
        assert (upgraded / 'evidence.json').read_bytes() == source_before['evidence.json']
        assert json.loads(validation.stdout)['maintenance_package_schema'] == 2
        assert delivery._read_candidate(upgraded / 'candidate')[2] == digest
        reject(lambda: maintenance.upgrade_package(upgraded, root / 'upgrade-v2-refused'))
        assert not (root / 'upgrade-v2-refused').exists()
        nonmaintenance = root / 'nonmaintenance'
        shutil.copytree(pack, nonmaintenance)
        nonmetadata_path = nonmaintenance / 'delivery.json'
        nonmetadata = json.loads(nonmetadata_path.read_text(encoding='utf-8'))
        nonmetadata['maintenance_bundle_required'] = False
        nonmetadata['inventory'] = delivery._inventory(delivery._tree(nonmaintenance))
        nonmetadata.pop('metadata_sha256', None)
        nonmetadata['metadata_sha256'] = checkpoint.digest(checkpoint.canonical(nonmetadata))
        delivery._write_json(nonmetadata_path, nonmetadata)
        delivery.validate_pack(nonmaintenance)
        nonmaintenance_before = {p.relative_to(nonmaintenance).as_posix(): p.read_bytes() for p in nonmaintenance.rglob('*') if p.is_file()}
        reject(lambda: maintenance.upgrade_package(nonmaintenance, root / 'upgrade-nonmaintenance-refused'))
        assert not (root / 'upgrade-nonmaintenance-refused').exists()
        assert nonmaintenance_before == {p.relative_to(nonmaintenance).as_posix(): p.read_bytes() for p in nonmaintenance.rglob('*') if p.is_file()}
        tampered = root / 'tampered-evidence'
        shutil.copytree(upgraded, tampered)
        alternate = dict(json.loads((tampered / 'evidence.json').read_text(encoding='utf-8')))
        alternate['observations'] = ['replacement passing evidence']
        alternate.pop('validation_sha256', None)
        alternate['validation_sha256'] = checkpoint.digest(checkpoint.canonical(alternate))
        delivery._write_json(tampered / 'evidence.json', alternate)
        tampered_meta_path = tampered / 'delivery.json'
        tampered_meta = json.loads(tampered_meta_path.read_text(encoding='utf-8'))
        tampered_meta['inventory'] = delivery._inventory(delivery._tree(tampered))
        tampered_meta.pop('metadata_sha256', None)
        tampered_meta['metadata_sha256'] = checkpoint.digest(checkpoint.canonical(tampered_meta))
        delivery._write_json(tampered_meta_path, tampered_meta)
        reject(lambda: delivery.validate_pack(tampered))
        v2_readme = (upgraded / 'maintenance' / 'v2' / 'README.md').read_bytes()
        assert b'archive-v1/' in v2_readme and b'contracts/jsonl-v1.json' in v2_readme
        assert b'archive-v1/' in (upgraded / 'maintenance' / 'v2' / 'format.json').read_bytes() or b'archive-v1/' in (upgraded / 'maintenance' / 'UPGRADE.json').read_bytes()
        assert (upgraded / 'maintenance' / 'UPGRADE-SOURCE-delivery.json').read_bytes() == source_before['delivery.json']
        assert source_before == {p.relative_to(pack).as_posix(): p.read_bytes() for p in pack.rglob('*') if p.is_file()}
        assert unrelated.read_text(encoding='utf-8') == 'keep'
        session = root / 'session'; session.mkdir()
        pending = session / '.mirror-pending.json'; pending.write_bytes(b'pending transaction must not be repaired')
        mirror = session / 'candidate.json'; mirror.write_bytes(b'previous mirror must remain')
        path = session / 'session.json'
        def save_state(state):
            path.write_bytes(checkpoint.canonical({'schema': 1, 'state': state, 'sha256': checkpoint.digest(checkpoint.canonical(state))}) + b'\n')
        save_state({'schema': 1, 'inflight': None})
        before = {p.name: p.read_bytes() for p in session.iterdir()}
        assert maintenance.diagnose(session)['status'] == 'VALID_ENVELOPE_READ_ONLY'
        assert before == {p.name: p.read_bytes() for p in session.iterdir()}
        save_state({'schema': 1, 'inflight': {'kind': 'tool', 'name': 'validate'}})
        pending_before = {p.name: p.read_bytes() for p in session.iterdir()}
        reject(lambda: maintenance.upgrade_package(session, root / 'inflight-refused'))
        assert not (root / 'inflight-refused').exists()
        assert pending_before == {p.name: p.read_bytes() for p in session.iterdir()}
        assert maintenance.diagnose(session)['status'] == 'REFUSED'
        save_state({'schema': 2, 'inflight': None})
        assert maintenance.diagnose(session)['status'] == 'REFUSED'
        raw = json.loads(path.read_text()); raw['state']['schema'] = 1; path.write_text(json.dumps(raw))
        reject(lambda: maintenance.diagnose(session))
        assert pending.read_bytes() == before[pending.name] and mirror.read_bytes() == before[mirror.name]
    print(json.dumps({'status': 'PASS', 'scope': 'real filesystem export/diagnosis; no candidate execution', 'checks': ['byte-exact export', 'recursive/existing export refused', 'sealed runtime binding', 'diagnosis never repairs pending mirror', 'inflight/unknown version/digest tamper denied']}))


if __name__ == '__main__': main()
