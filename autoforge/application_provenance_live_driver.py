#!/usr/bin/env python3
"""Explicitly authorized live retrieval and verification of Julia's official Windows x64 release."""
from __future__ import annotations
import argparse
import hashlib
import json
import tempfile
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
import application_provenance as provenance

VERSION = "1.13.3"
BASE = "https://julialang-s3.julialang.org/bin/linux/x64/1.13/"
ARTIFACT = f"julia-{VERSION}-linux-x86_64.tar.gz"
SIGNATURE = ARTIFACT + ".asc"
KEY_URL = "https://julialang.org/assets/juliareleases.asc"
# The caller must pin the independently confirmed full fingerprint; never trust a downloaded key's identity.
def fetch(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "AutoForge-publisher-provenance/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.geturl() != url:
            raise provenance.ProvenanceRefused("redirect rejected; official source URL must remain exact")
        data = response.read(1024 * 1024 * 1024 + 1)
    if not data or len(data) > 1024 * 1024 * 1024:
        raise provenance.ProvenanceRefused("download is empty or exceeds 1 GiB")
    dest.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorized", action="store_true", help="explicitly authorize HTTPS downloads from official Julia URLs")
    parser.add_argument("--fingerprint", required=True, help="operator-pinned full OpenPGP primary fingerprint")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gpgv", default="gpgv")
    args = parser.parse_args()
    if not args.authorized:
        parser.error("network access requires --authorized")
    try:
        if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
            raise provenance.ProvenanceRefused("output directory must be absent or empty")
        args.out.mkdir(parents=True, exist_ok=True)
        artifact, signature, downloaded_key, keyring = (args.out / ARTIFACT, args.out / SIGNATURE,
                                                          args.out / "juliareleases.asc", args.out / "publisher-keyring.gpg")
        fetch(KEY_URL, downloaded_key)
        fetch(BASE + ARTIFACT, artifact)
        fetch(BASE + SIGNATURE, signature)
        # Import public key material into a fresh isolated keyring, then require the caller's pin at verify time.
        if shutil.which(args.gpgv) is None:
            raise provenance.ProvenanceRefused("gpgv not installed")
        gpg = shutil.which("gpg")
        if gpg is None:
            raise provenance.ProvenanceRefused("gpg not installed to construct isolated keyring")
        fingerprint = args.fingerprint.upper().replace(" ", "")
        listed = subprocess.run([gpg, "--batch", "--show-keys", "--with-colons", "--fingerprint", str(downloaded_key)],
                                capture_output=True, text=True, timeout=60, check=False)
        if listed.returncode or not any(line.split(":")[9].upper() == fingerprint for line in listed.stdout.splitlines() if line.startswith("fpr:")):
            raise provenance.ProvenanceRefused("downloaded official key does not match the operator-pinned fingerprint")
        dearmored = subprocess.run([gpg, "--batch", "--dearmor", "--output", str(keyring), str(downloaded_key)],
                                   capture_output=True, text=True, timeout=60, check=False)
        if dearmored.returncode or not keyring.is_file() or keyring.stat().st_size == 0:
            raise provenance.ProvenanceRefused("cannot convert pinned public key to gpgv keyring")
        result = provenance.verify_publisher_artifact(artifact, signature, keyring,
            expected_fingerprint=args.fingerprint, source_url=BASE + ARTIFACT,
            signature_url=BASE + SIGNATURE, gpgv=args.gpgv)
        result["publisher_key_url"] = KEY_URL
        result["release_version"] = VERSION
        report_bytes = (json.dumps(result, sort_keys=True, indent=2) + "\n").encode("utf-8")
        report_path = args.out / "publisher-verification.json"
        report_path.write_bytes(report_bytes)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, provenance.ProvenanceRefused, subprocess.SubprocessError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
