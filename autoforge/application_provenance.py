"""Publisher-signed artifact verification using an operator-pinned OpenPGP identity."""
from __future__ import annotations
import hashlib
import tempfile
import re
import subprocess
from pathlib import Path
from typing import Any

class ProvenanceRefused(ValueError):
    pass

_FINGERPRINT = re.compile(r"(?:[0-9A-F]{40}|[0-9A-F]{64})\Z")


def verify_publisher_artifact(artifact: Path, signature: Path, keyring: Path, *,
                              expected_fingerprint: str, source_url: str,
                              signature_url: str, gpgv: str = "gpgv") -> dict[str, Any]:
    """Validate OpenPGP detached signature over exact artifact bytes and pinned signer.

    The isolated keyring must contain operator-approved key material. A successful
    gpgv result alone is insufficient: VALIDSIG must name the pinned fingerprint.
    URLs are recorded as provenance identifiers; callers must enforce official hosts.
    """
    fp = expected_fingerprint.upper().replace(" ", "")
    if not _FINGERPRINT.fullmatch(fp):
        raise ProvenanceRefused("expected publisher fingerprint must be a full 40/64-hex fingerprint")
    for label, value in (("source_url", source_url), ("signature_url", signature_url)):
        if not isinstance(value, str) or not value.startswith("https://"):
            raise ProvenanceRefused(f"{label} must be an HTTPS URL")
    for path in (artifact, signature, keyring):
        if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
            raise ProvenanceRefused("artifact, detached signature and pinned keyring must be regular files")
    try:
        artifact_digest = hashlib.sha256()
        with artifact.open("rb") as source, tempfile.NamedTemporaryFile(prefix="autoforge-publisher-", suffix=artifact.suffix, delete=False) as snapshot:
            snapshot_path = Path(snapshot.name)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                artifact_digest.update(chunk)
                snapshot.write(chunk)
        digest = artifact_digest.hexdigest()
        keyring_arg = str(keyring.resolve()).replace("\\", "/")
        run = subprocess.run([gpgv, "--status-fd", "1", "--keyring", keyring_arg, str(signature), str(snapshot_path)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvenanceRefused(f"gpgv verification unavailable: {exc}") from exc
    finally:
        if 'snapshot_path' in locals():
            snapshot_path.unlink(missing_ok=True)
    valid = []
    for line in run.stdout.splitlines():
        if line.startswith("[GNUPG:] VALIDSIG "):
            fields = line.split()
            if len(fields) >= 3:
                # VALIDSIG's optional final field is the primary fingerprint for subkey signatures.
                valid.append(fields[-1].upper() if len(fields) >= 12 else fields[2].upper())
    if run.returncode != 0 or valid != [fp]:
        raise ProvenanceRefused("detached signature invalid or signer does not exactly match the pinned fingerprint")
    digest = artifact_digest.hexdigest()
    return {"schema": 1, "status": "PASS", "attestation": "publisher OpenPGP signature verified",
            "publisher_fingerprint": fp, "artifact_sha256": digest, "artifact_size": artifact.stat().st_size,
            "source_url": source_url, "signature_url": signature_url,
            "signature_sha256": hashlib.sha256(signature.read_bytes()).hexdigest(),
            "keyring_sha256": hashlib.sha256(keyring.read_bytes()).hexdigest(),
            "verification": "gpgv VALIDSIG exact signer fingerprint; detached signature covers exact artifact bytes",
            "license_authority": "manual explicit operator record required; not verified here"}
