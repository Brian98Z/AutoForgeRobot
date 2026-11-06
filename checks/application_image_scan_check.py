"""Offline regressions for the explicitly authorized Grype image gate."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import application_dependencies as d

from application_image_applicability_live_driver import extract_trixie_status


def check_vendor_backport_parser() -> None:
    page = '''<table><tr><td>openssl</td><td>trixie</td><td>3.5.7-1~deb13u2</td><td>fixed</td></tr>
    <tr><td></td><td>trixie (security)</td><td>3.5.7-1~deb13u3</td><td>fixed</td></tr>
    <tr><td>openssh</td><td>trixie</td><td>3.5.7-1~deb13u3</td><td>not affected</td></tr></table>'''
    exact = extract_trixie_status(page, 'openssl', '3.5.7-1~deb13u3')
    assert exact['status'] == 'OBSERVED_VENDOR_FIXED'
    assert exact['exact_installed_version_rows'] == [{'release': 'trixie (security)', 'version': '3.5.7-1~deb13u3', 'status': 'fixed'}]
    unrelated = extract_trixie_status(page, 'openssl', '3.5.7-1~deb13u9')
    assert unrelated['status'] == 'UNRESOLVED'
    mixed = '''<table><tr><td>openssl</td><td>trixie</td><td>3.5.7-1~deb13u2</td><td>vulnerable</td></tr>
    <tr><td>openssl</td><td>forky</td><td>3.6.5-1</td><td>fixed</td></tr>
    <tr><td>otherpkg</td><td>trixie</td><td>3.5.7-1~deb13u3</td><td>fixed</td></tr></table>'''
    assert extract_trixie_status(mixed, 'openssl', '3.5.7-1~deb13u3')['status'] == 'UNRESOLVED'
    not_affected = '''<table><tr><td>openssl</td><td>trixie</td><td>(not affected)</td><td>not affected</td></tr></table>'''
    unaffected = extract_trixie_status(not_affected, 'openssl', '(not affected)')
    assert unaffected['status'] == 'OBSERVED_VENDOR_FIXED'
    assert unaffected['exact_installed_version_rows'] == [{'release': 'trixie', 'version': '(not affected)', 'status': 'not affected'}]


def check_signed_applicability(root: Path) -> None:
    from datetime import datetime,timezone,timedelta
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private=Ed25519PrivateKey.generate(); public=private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    reviewer=Ed25519PrivateKey.generate(); reviewer_public=reviewer.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    report={'matches':[{'vulnerability':{'id':'CVE-TEST-1','namespace':'vendor:test','urls':['https://upstream.example/security/advisory']},'artifact':{'name':'pkg','version':'1.0','purl':'pkg:generic/pkg@1.0','locations':[{'path':'/usr/lib/pkg'}]}}],
            'source':{'type':'image','target':{'userInput':'example.invalid/test@sha256:'+'a'*64,'repoDigests':['example.invalid/test@sha256:'+'a'*64],'imageID':'sha256:'+'b'*64,'manifestDigest':'sha256:'+'c'*64}}}
    report_path=root/'app-report.json'; report_path.write_bytes(json.dumps(report,separators=(',',':')).encode())
    built='2026-11-01T00:00:00Z'; valid='2027-01-01T00:00:00Z'; now='2026-11-05T00:00:00Z'
    gate={'status':'FAIL','report_sha256':hashlib.sha256(report_path.read_bytes()).hexdigest(),'report_path':str(report_path.resolve()),
          'source_repo_digest':report['source']['target']['userInput'],'source_config_id':'sha256:'+'b'*64,'source_manifest_digest':'sha256:'+'c'*64,
          'finding_count':1,'scanner_sha256':'d'*64,'db_identity':{'schemaVersion':'6','built':built,'checksum':'e'*64},'db_built_at':built}
    gate_path=root/'app-gate.json'; gate_path.write_text(json.dumps(gate),encoding='utf-8')
    regression=root/'behavior-regression.bin'; regression.write_bytes(b'independently authored regression evidence')
    sources=root/'upstream-source'; sources.mkdir(); source=sources/'upstream.txt'; source.write_bytes(b'test source evidence')
    contract={'schema':1,'scan_report_sha256':gate['report_sha256'],'image_repo_digest':gate['source_repo_digest'],'image_config_id':gate['source_config_id'],
      'image_manifest_digest':gate['source_manifest_digest'],'scanner_sha256':gate['scanner_sha256'],'db_identity':gate['db_identity'],'db_built_at':built,
      'findings':[{'match_key':d.image_match_key(report['matches'][0]),'decision':'fixed','source_type':'upstream','source_url':'https://upstream.example/security/advisory',
        'source_file':source.name,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'behavior_regression_sha256':hashlib.sha256(regression.read_bytes()).hexdigest()}],
      'behavior_regression_sha256':hashlib.sha256(regression.read_bytes()).hexdigest(),'reviewer_identity':hashlib.sha256(reviewer_public).hexdigest(),'reviewed_at':now,'expires_at':valid}
    signed=d._canonical(contract); sig=private.sign(signed); reviewer_sig=reviewer.sign(signed)
    contract_path=root/'app-contract.json'; signature_path=root/'app-contract.sig'; key_path=root/'app-key.pem'; reviewer_sig_path=root/'reviewer.sig'; reviewer_key_path=root/'reviewer.pem'
    contract_path.write_bytes(signed); signature_path.write_bytes(sig); key_path.write_bytes(public); reviewer_sig_path.write_bytes(reviewer_sig); reviewer_key_path.write_bytes(reviewer_public)
    import subprocess,sys
    key_digest=hashlib.sha256(public).hexdigest(); reviewer_digest=hashlib.sha256(reviewer_public).hexdigest()
    cli=subprocess.run([sys.executable,str(Path(d.__file__).resolve()),'verify-image-applicability',str(report_path),str(gate_path),str(contract_path),str(signature_path),str(key_path),str(sources),str(regression),'--pinned-key-sha256',key_digest,'--reviewer-signature',str(reviewer_sig_path),'--reviewer-public-key',str(reviewer_key_path),'--reviewer-key-sha256',reviewer_digest,'--now',now],capture_output=True,text=True,check=False)
    assert cli.returncode==0 and json.loads(cli.stdout)['status']=='PASS_APPLICABILITY',cli.stderr
    source_link=sources/'linked-source.txt'
    try:
        source_link.symlink_to(source)
    except (OSError, NotImplementedError):
        pass
    else:
        linked_contract={**contract,'findings':[dict(contract['findings'][0],source_file=source_link.name)]}
        linked_bytes=d._canonical(linked_contract)
        try:
            d.verify_image_applicability(report_path,gate_path,linked_bytes,private.sign(linked_bytes),public,sources,regression,
                pinned_key_sha256=key_digest,reviewer_signature=reviewer.sign(linked_bytes),reviewer_public_key_pem=reviewer_public,
                reviewer_key_sha256=reviewer_digest,now=now)
        except d.DependencyRefused:
            pass
        else:
            raise AssertionError('applicability verifier followed a symlinked source evidence path')
        source_link.unlink()
    linked_root=root/'linked-evidence'
    linked_root.mkdir()
    linked_report=linked_root/'report.json'
    try:
        linked_report.symlink_to(report_path)
    except (OSError, NotImplementedError):
        pass
    else:
        try:
            d.verify_image_applicability(linked_report,gate_path,signed,sig,public,sources,regression,
                pinned_key_sha256=key_digest,reviewer_signature=reviewer_sig,reviewer_public_key_pem=reviewer_public,
                reviewer_key_sha256=reviewer_digest,now=now)
        except d.DependencyRefused:
            pass
        else:
            raise AssertionError('applicability verifier followed a symlinked report path')
    import application_image_review_packet as packet
    packet_root=root/'packet'
    packet_root.mkdir()
    packet_file=packet_root/'evidence.bin'
    packet_file.write_bytes(b'packet evidence')
    packet_link=packet_root/'link.bin'
    try:
        packet_link.symlink_to(packet_file)
    except (OSError, NotImplementedError):
        pass
    else:
        try:
            packet._safe_regular(packet_link)
        except packet.PacketRefused:
            pass
        else:
            raise AssertionError('review packet reader followed a symlinked evidence file')
    from unittest.mock import patch
    reparse = type('Stat', (), {'st_mode': packet.stat.S_IFREG, 'st_file_attributes': 0x400})()
    with patch.object(packet.Path, 'lstat', return_value=reparse):
        try:
            packet._safe_regular(packet_root/'reparse-file')
        except packet.PacketRefused:
            pass
        else:
            raise AssertionError('review packet reader accepted a Windows reparse point')
    for bad_contract,bad_sig in ((signed,sig[:-1]+bytes([sig[-1]^1]),),(d._canonical({**contract,'findings':[]}),private.sign(d._canonical({**contract,'findings':[]})) ),
      (d._canonical({**contract,'expires_at':'2026-11-04T00:00:00Z'}),private.sign(d._canonical({**contract,'expires_at':'2026-11-04T00:00:00Z'}))),
      (d._canonical({**contract,'findings':[dict(contract['findings'][0],source_sha256='0'*64)]}),private.sign(d._canonical({**contract,'findings':[dict(contract['findings'][0],source_sha256='0'*64)]})))):
        try:d.verify_image_applicability(report_path,gate_path,bad_contract,bad_sig,public,sources,regression,pinned_key_sha256=key_digest,reviewer_signature=reviewer.sign(bad_contract),reviewer_public_key_pem=reviewer_public,reviewer_key_sha256=reviewer_digest,now=now)
        except d.DependencyRefused:pass
        else:raise AssertionError('bad signature, incomplete findings, or expired applicability accepted')
    altered=gate_path.read_text(encoding='utf-8'); gate_path.write_text(altered.replace('FAIL','PASS'),encoding='utf-8')
    try:d.verify_image_applicability(report_path,gate_path,signed,sig,public,sources,regression,pinned_key_sha256=key_digest,reviewer_signature=reviewer_sig,reviewer_public_key_pem=reviewer_public,reviewer_key_sha256=reviewer_digest,now=now)
    except d.DependencyRefused:pass
    else:raise AssertionError('applicability gate accepted when raw scan was not FAIL')


def main() -> None:
    with tempfile.TemporaryDirectory(prefix='image-applicability-check-') as td:
        check_vendor_backport_parser(); check_signed_applicability(Path(td))
    with tempfile.TemporaryDirectory(prefix='image-scan-check-') as td:
        root=Path(td); scanner=root/'grype.exe'; scanner.write_bytes(b'pinned scanner'); db=root/'db'; db.mkdir(); report_path=root/'report.json'
        db_file=db/'vulnerability.db'; db_file.write_bytes(b'validated database fixture')
        digest=hashlib.sha256(scanner.read_bytes()).hexdigest(); image='example.invalid/python@sha256:'+'a'*64
        common=dict(scanner=scanner,expected_scanner_sha256=digest,db_cache=db,expected_repo_digest=image,report_path=report_path,authorized=True)
        assert d.grype_image_gate(image,**{**common,'authorized':False})['status']=='HOLD'
        assert d.grype_image_gate('python:latest',**common)['status']=='HOLD'
        assert d.grype_image_gate(image,**{**common,'expected_scanner_sha256':'0'*64})['status']=='HOLD'
        class Result:
            returncode=0; stdout=json.dumps({'valid':True,'schemaVersion':'6','built':'2026-11-01T00:00:00Z','path':str(db_file)}).encode(); stderr=b''
        with patch.object(d.subprocess,'run',return_value=Result()):
            assert d.grype_image_gate(image,**common)['status']=='HOLD'
        class BadDB(Result):
            stdout=b'{"valid":false}'
        with patch.object(d.subprocess,'run',return_value=BadDB()):
            assert d.grype_image_gate(image,**common)['status']=='HOLD'
        payload=json.dumps({'matches':[{'vulnerability':{'severity':'Critical'}},{'vulnerability':{'severity':'High'}}], 'source':{'type':'image','target':{'userInput':image,'manifestDigest':'sha256:'+'a'*64,'repoDigests':[image],'imageID':'sha256:'+'b'*64}}}).encode()
        calls=iter([Result(),type('Scan',(Result,),{'stdout':b'','returncode':1})()])
        def run(*args,**kwargs):
            if 'db' in args[0]: return Result()
            Path(args[0][args[0].index('--file')+1]).write_bytes(payload)
            return type('Scan',(Result,),{'stdout':b'','returncode':1 if payload.find(b'Critical')>=0 else 0})()
        with patch.object(d.subprocess,'run',side_effect=run):
            out=d.grype_image_gate(image,**common)
        assert out['status']=='FAIL' and out['critical_count']==1 and out['high_count']==1
        assert hashlib.sha256(report_path.read_bytes()).hexdigest()==out['report_sha256']
        report_path=root/'clean-report.json'; common['report_path']=report_path
        payload=json.dumps({'matches':[], 'source':{'type':'image','target':{'userInput':image,'manifestDigest':'sha256:'+'a'*64,'repoDigests':[image],'imageID':'sha256:'+'b'*64}}}).encode()
        calls=iter([Result(),type('Scan',(Result,),{'stdout':b'','returncode':0})()])
        with patch.object(d.subprocess,'run',side_effect=run): out=d.grype_image_gate(image,**common)
        assert out['status']=='PASS' and out['finding_count']==0
        common['report_path']=root/'wrong-source-report.json'
        payload=json.dumps({'matches':[], 'source':{'type':'image','target':{'userInput':'other-image','manifestDigest':'sha256:'+'c'*64,'repoDigests':[image],'imageID':'sha256:'+'b'*64}}}).encode()
        calls=iter([Result(),type('Scan',(Result,),{'stdout':b'','returncode':0})()])
        with patch.object(d.subprocess,'run',side_effect=run):
            assert d.grype_image_gate(image,**common)['status']=='HOLD'
        common['report_path']=root/'wrong-repo-report.json'
        payload=json.dumps({'matches':[], 'source':{'type':'image','target':{'userInput':image,'manifestDigest':'sha256:'+'a'*64,'repoDigests':['example.invalid/other@sha256:'+'c'*64],'imageID':'sha256:'+'b'*64}}}).encode()
        calls=iter([Result(),type('Scan',(Result,),{'stdout':b'','returncode':0})()])
        with patch.object(d.subprocess,'run',side_effect=run):
            assert d.grype_image_gate(image,**common)['status']=='HOLD'
        report_path=root/'final-clean-report.json'; common['report_path']=report_path
        payload=json.dumps({'matches':[], 'source':{'type':'image','target':{'userInput':image,'manifestDigest':'sha256:'+'a'*64,'repoDigests':[image],'imageID':'sha256:'+'b'*64}}}).encode()
        with patch.object(d.subprocess,'run',side_effect=run): out=d.grype_image_gate(image,**common)
        assert out['status']=='PASS' and out['finding_count']==0, out
    print('{"status":"PASS","checks":["authorization","exact digest input","pinned scanner hash","invalid database HOLD","vulnerable image FAIL","clean image PASS","manifest mismatch HOLD","report digest","Debian backport exact-version parser","signed exact-match applicability CLI PASS","forged/incomplete/expired/source-tamper/scan-status negatives refused"]}')

if __name__=='__main__': main()
