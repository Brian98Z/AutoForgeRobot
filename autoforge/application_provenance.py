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
    snapshots: list[Path] = []
    try:
        def snapshot(source: Path, suffix: str) -> tuple[Path, str, int]:
            digest = hashlib.sha256(); size = 0
            with source.open("rb") as stream, tempfile.NamedTemporaryFile(prefix="autoforge-publisher-", suffix=suffix, delete=False) as target:
                path = Path(target.name); snapshots.append(path)
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > 1024 * 1024 * 1024: raise ProvenanceRefused("publisher input exceeds byte limit")
                    digest.update(chunk); target.write(chunk)
            return path, digest.hexdigest(), size
        snapshot_path, digest, artifact_size = snapshot(artifact, artifact.suffix)
        signature_path, signature_sha, _ = snapshot(signature, ".sig")
        keyring_path, keyring_sha, _ = snapshot(keyring, ".gpg")
        keyring_arg = str(keyring_path).replace("\\", "/")
        run = subprocess.run([gpgv, "--status-fd", "1", "--keyring", keyring_arg, str(signature_path), str(snapshot_path)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvenanceRefused(f"gpgv verification unavailable: {exc}") from exc
    finally:
        for path in snapshots: path.unlink(missing_ok=True)
    valid = []
    for line in run.stdout.splitlines():
        if line.startswith("[GNUPG:] VALIDSIG "):
            fields = line.split()
            if len(fields) >= 3:
                # VALIDSIG's optional final field is the primary fingerprint for subkey signatures.
                valid.append(fields[-1].upper() if len(fields) >= 12 else fields[2].upper())
    if run.returncode != 0 or valid != [fp]:
        raise ProvenanceRefused("detached signature invalid or signer does not exactly match the pinned fingerprint")
    return {"schema": 1, "status": "PASS", "attestation": "publisher OpenPGP signature verified",
            "publisher_fingerprint": fp, "artifact_sha256": digest, "artifact_size": artifact_size,
            "source_url": source_url, "signature_url": signature_url,
            "signature_sha256": signature_sha,
            "keyring_sha256": keyring_sha,
            "verification": "gpgv VALIDSIG exact signer fingerprint; detached signature covers exact artifact bytes",
            "license_authority": "manual explicit operator record required; not verified here"}
