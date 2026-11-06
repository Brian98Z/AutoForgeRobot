#!/usr/bin/env python3
"""Windows Sandbox controller for manifest-bound portable EXE UI checks.

Reads one request from stdin. Candidate files are exposed read-only; only the exact
hash-bound portable EXE is copied into the disposable guest for launch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import threading
MAX_REQUEST = 4_194_304
MAX_REPORT = 1_048_576


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


class _SandboxTemp:
    """Controller temp dir whose cleanup cannot recurse on locked Sandbox mappings."""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="autoforge-wsb-")
        return self.path

    def __exit__(self, *_exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message):
    raise ValueError(message)


def verify_request(req):
    required = {"schema", "operation", "candidate_root", "candidate_sha256", "target", "artifact", "scenarios", "timeout_seconds", "run_id"}
    if not isinstance(req, dict) or set(req) != required or req["schema"] != 1 or req["operation"] != "build-install-launch-interact":
        fail("invalid Windows Sandbox request")
    if not isinstance(req["run_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", req["run_id"]):
        fail("invalid run nonce")
    root = Path(req["candidate_root"]).resolve(strict=True)
    if not root.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", req["candidate_sha256"]):
        fail("invalid candidate identity")
    artifact = req["artifact"]
    if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256", "args"}:
        fail("portable EXE artifact declaration is required")
    rel = artifact["path"]
    if not isinstance(rel, str) or not rel or "\\" in rel or rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")):
        fail("unsafe artifact relative path")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) or not isinstance(artifact["args"], list) or len(artifact["args"]) > 32 or any(not isinstance(x, str) or "\x00" in x or len(x) > 4096 for x in artifact["args"]):
        fail("invalid artifact hash or argument vector")
    binary = root.joinpath(*rel.split("/"))
    if binary.is_symlink() or not binary.is_file() or root not in binary.resolve(strict=True).parents or sha256(binary) != artifact["sha256"]:
        fail("portable EXE is missing, linked, outside candidate root, or hash-mismatched")
    if binary.suffix.lower() != ".exe":
        fail("supported artifact class is portable Windows .exe only")
    if type(req["timeout_seconds"]) is not int or not 1 <= req["timeout_seconds"] <= 300:
        fail("timeout_seconds must be 1..300")
    if not isinstance(req["target"], str) or not req["target"] or len(req["target"]) > 200:
        fail("named target required")
    return root, binary.resolve(strict=True)


def main():
    if os.name != "nt":
        fail("Windows Sandbox is available only on Windows")
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        fail("request exceeds size limit")
    req = json.loads(raw.decode("utf-8"))
    root, binary = verify_request(req)
    sandbox = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsSandbox.exe"
    if not sandbox.is_file():
        fail("Windows Sandbox client is absent; enable Windows Sandbox feature with administrator elevation and reboot, then retry")
    with _SandboxTemp() as temp_name:
        temp = Path(temp_name).resolve()
        keep_dir = os.environ.get("AUTOFORGE_KEEP_SANDBOX_DIR")
        evidence = temp / "report.json"
        guest = temp / "guest.ps1"
        wsb = temp / "run.wsb"
        guest_source_path = Path(__file__).with_name("windows_sandbox_guest.ps1")
        if sha256(guest_source_path) != "4616c203464def4ea6273f507be8adbad7258ad4f5228027ff5987c2c737a9cc":
            fail("trusted guest runner digest mismatch")
        guest_source = guest_source_path.read_text(encoding="utf-8")
        guest.write_text(guest_source, encoding="utf-8", newline="\r\n")
        guest_path = r"C:\host\guest.ps1"
        guest_cmd = f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{guest_path}"'
        config = ET.Element("Configuration")
        ET.SubElement(config, "VGpu").text = "Disable"
        ET.SubElement(config, "Networking").text = "Disable"
        ET.SubElement(config, "ClipboardRedirection").text = "Disable"
        mappings = ET.SubElement(config, "MappedFolders")
        for host_path, guest_path, readonly in ((str(root), r"C:\candidate", True), (str(temp), r"C:\host", False)):
            mapping = ET.SubElement(mappings, "MappedFolder")
            ET.SubElement(mapping, "HostFolder").text = host_path
            ET.SubElement(mapping, "SandboxFolder").text = guest_path
            ET.SubElement(mapping, "ReadOnly").text = "true" if readonly else "false"
        logon = ET.SubElement(config, "LogonCommand")
        ET.SubElement(logon, "Command").text = guest_cmd
        wsb.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(config, encoding="utf-8"))
        payload = {"schema": 1, "run_id": req["run_id"], "target": req["target"], "candidate_sha256": req["candidate_sha256"], "artifact_path": req["artifact"]["path"], "artifact_sha256": req["artifact"]["sha256"], "artifact_args": req["artifact"]["args"], "scenarios": req["scenarios"], "timeout_seconds": req["timeout_seconds"], "report_path": r"C:\host\report.json"}
        (temp / "request.json").write_bytes(canonical(payload))
        # Guest script and request use a writable controller-owned directory; candidate mapping is read-only.
        process = subprocess.Popen([str(sandbox), str(wsb)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, close_fds=True)
        deadline = time.monotonic() + req["timeout_seconds"] + 30
        try:
            while time.monotonic() < deadline:
                if evidence.is_file() and evidence.stat().st_size <= MAX_REPORT:
                    report = json.loads(evidence.read_text(encoding="utf-8"))
                    expected = {"schema": 1, "run_id": req["run_id"], "target": req["target"], "candidate_sha256": req["candidate_sha256"], "artifact_sha256": req["artifact"]["sha256"]}
                    if any(report.get(k) != v for k, v in expected.items()):
                        fail("guest report is not bound to this run, target, candidate and artifact")
                    if report.get("sandbox") is not True or report.get("cleanup") is not True:
                        fail("guest omitted sandbox or cleanup evidence")
                    process.wait(timeout=max(1, deadline - time.monotonic()))
                    if process.poll() is None:
                        fail("Windows Sandbox process cleanup could not be confirmed")
                    report["evidence_trust"] = "guest_writable_untrusted"
                    return report
                if process.poll() is not None:
                    (temp / "launcher-exit.json").write_text(
                        json.dumps({"returncode": process.returncode, "launcher": str(sandbox), "wsb": str(wsb)}, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    time.sleep(0.2)
                    continue
                time.sleep(0.2)
            fail("Windows Sandbox timed out or closed without nonce-bound guest evidence")
        finally:
            if keep_dir:
                try:
                    shutil.copytree(temp, keep_dir, dirs_exist_ok=True)
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.poll() is None:
                fail("Windows Sandbox process cleanup could not be confirmed")

if __name__ == "__main__":
    try:
        result = main()
        encoded = canonical(result) + b"\n"
        if len(encoded) > MAX_REPORT:
            raise ValueError("guest report exceeds limit")
        sys.stdout.buffer.write(encoded)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        sys.stderr.write(f"BLOCKED: {exc}\n")
        raise SystemExit(2)
