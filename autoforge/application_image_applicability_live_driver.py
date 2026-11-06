#!/usr/bin/env python3
"""Fetch Debian Security Tracker evidence for exact Debian image package builds.

This is an evidence recorder, not a vulnerability filter or image gate. It only
records vendor fixed/not-affected claims; it never changes Grype findings.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from urllib.request import HTTPRedirectHandler
from pathlib import Path

TRACKER = "https://security-tracker.debian.org/tracker/"
USER_AGENT = "AutoForge-Debian-applicability-evidence/1"
MAX_PAGE_BYTES = 2 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(cve: str, timeout: int) -> tuple[bytes, str]:
    url = TRACKER + cve
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        if response.geturl() != url:
            raise ValueError(f"unexpected redirect for {cve}")
        data = response.read(MAX_PAGE_BYTES + 1)
    if not data or len(data) > MAX_PAGE_BYTES:
        raise ValueError(f"empty or oversized Debian tracker page for {cve}")
    return data, url


def extract_trixie_status(html: str, package: str, installed: str) -> dict[str, object]:
    from html.parser import HTMLParser

    class Tables(HTMLParser):
        def __init__(self) -> None:
            super().__init__(); self.tables = []; self.table = None; self.row = None; self.cell = None
        def handle_starttag(self, tag, attrs):
            if tag == "table": self.table = []
            elif tag == "tr" and self.table is not None: self.row = []
            elif tag in {"td", "th"} and self.row is not None: self.cell = []
        def handle_data(self, data):
            if self.cell is not None: self.cell.append(data)
        def handle_endtag(self, tag):
            if tag in {"td", "th"} and self.cell is not None and self.row is not None:
                self.row.append(" ".join("".join(self.cell).split())); self.cell = None
            elif tag == "tr" and self.row is not None and self.table is not None:
                self.table.append(self.row); self.row = None
            elif tag == "table" and self.table is not None:
                self.tables.append(self.table); self.table = None

    parsed = Tables(); parsed.feed(html)
    rows = []
    for table in parsed.tables:
        owner = None
        for cells in table:
            if not cells: continue
            first = cells[0].strip()
            if first:
                # The tracker uses a blank first cell for package-row continuation.
                # Nonblank cells start a new package block; linked labels may add PTS text.
                match = re.match(r"([a-z0-9][a-z0-9+.-]*)", first, re.I)
                owner = match.group(1).casefold() if match else None
            if owner != package.casefold(): continue
            if len(cells) < 3: continue
            release, version, status = cells[-3:]
            if not re.fullmatch(r"trixie(?:\s+\(security\))?", release, re.I): continue
            normalized_status = status.strip().casefold()
            if normalized_status in {"fixed", "not affected", "vulnerable"}:
                rows.append({"release": release, "version": version.strip(), "status": normalized_status})
    exact = [r for r in rows if r["version"] == installed and r["status"] in {"fixed", "not affected"}]
    return {"package": package, "installed_version": installed, "trixie_rows": rows,
            "exact_installed_version_rows": exact,
            "status": "OBSERVED_VENDOR_FIXED" if exact else "UNRESOLVED"}




def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorized", action="store_true", help="authorize unauthenticated HTTPS reads from Debian Security Tracker")
    parser.add_argument("--out", type=Path, required=True, help="new, absent JSON evidence path")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if not args.authorized:
        parser.error("network reads require --authorized")
    if not 1 <= args.timeout <= 120:
        parser.error("timeout must be 1..120 seconds")
    if args.out.exists():
        parser.error("output already exists; refusing overwrite")
    cves = ("CVE-2026-63073", "CVE-2026-75803")
    result: dict[str, object] = {"schema": 1, "status": "HOLD", "source": TRACKER,
        "scope": {"image": "public.ecr.aws/docker/library/python@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893",
                  "image_identity_source": "retained prior scanner evidence; driver does not inspect image",
                  "package": "openssl", "installed_debian_version": "3.5.7-1~deb13u3",
                  "installed_version_source": "retained prior runtime evidence; driver does not inspect image"},
        "entries": [], "scanner_policy": "unchanged: every Grype match remains a gate finding",
        "candidate_execution_authorized": False}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = args.out.parent / (args.out.stem + "-source-pages")
    if raw_dir.exists(): parser.error("raw source evidence directory already exists; refusing overwrite")
    raw_dir.mkdir()
    try:
        entries = []
        for cve in cves:
            raw, url = fetch(cve, args.timeout)
            html = raw.decode("utf-8", "strict")
            raw_path = raw_dir / f"{cve}.html"
            raw_path.write_bytes(raw)
            vendor = extract_trixie_status(html, "openssl", "3.5.7-1~deb13u3")
            entries.append({"cve": cve, "url": url, "response_sha256": hashlib.sha256(raw).hexdigest(),
                            "response_path": str(raw_path), **vendor})
        result["entries"] = entries
        result["status"] = "OBSERVED_VENDOR_FIXED" if all(x["status"] == "OBSERVED_VENDOR_FIXED" for x in entries) else "HOLD"
        args.out.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "OBSERVED_VENDOR_FIXED" else 1
    except (OSError, UnicodeError, ValueError, urllib.error.URLError) as exc:
        print(f"HOLD: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
