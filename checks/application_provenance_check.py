#!/usr/bin/env python3
"""Offline verification of a previously live-acquired Julia artifact and tamper rejection."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse
import shutil
import tempfile
import json
import hashlib
from pathlib import Path
import application_provenance as provenance
from application_provenance_live_driver import ARTIFACT, BASE, SIGNATURE


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--gpgv", default="gpgv")
    a = p.parse_args()
    artifact = a.directory / ARTIFACT
    signature = a.directory / SIGNATURE
    keyring = a.directory / "publisher-keyring.gpg"
    result = provenance.verify_publisher_artifact(artifact, signature, keyring,
        expected_fingerprint=a.fingerprint, source_url=BASE + ARTIFACT,
        signature_url=BASE + SIGNATURE, gpgv=a.gpgv)
    with tempfile.TemporaryDirectory(prefix="autoforge-provenance-tamper-") as tmp:
        changed = Path(tmp) / ARTIFACT
        shutil.copyfile(artifact, changed)
        with changed.open("r+b") as f:
            byte = f.read(1)
            if not byte:
                raise AssertionError("verified artifact unexpectedly empty")
            f.seek(0)
            f.write(bytes([byte[0] ^ 1]))
        try:
            provenance.verify_publisher_artifact(changed, signature, keyring,
                expected_fingerprint=a.fingerprint, source_url=BASE + ARTIFACT,
                signature_url=BASE + SIGNATURE, gpgv=a.gpgv)
        except provenance.ProvenanceRefused:
            pass
        else:
            raise AssertionError("tampered exact artifact bytes were accepted")
    print("application_provenance_check: PASS (official detached signature, pinned publisher fingerprint, exact-byte tamper rejection)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
