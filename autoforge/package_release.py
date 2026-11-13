#!/usr/bin/env python3
"""Assemble release artefacts and their checksum list from the built binaries.

Run ``autoforge/build_exe.py`` for the target platform first; this script only copies,
archives and hashes. Archives carry explicit POSIX modes because a Windows host cannot
report Linux executable bits.

    python autoforge/package_release.py --platform windows --version 0.1.1
    python autoforge/package_release.py --platform linux   --version 0.1.1
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add(tf: tarfile.TarFile, path: Path, arcname: str, executable: bool = False) -> None:
    info = tf.gettarinfo(str(path), arcname=arcname)
    info.mode = 0o755 if info.isdir() or executable or path.name == "autoforge" else 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if info.isreg():
        with path.open("rb") as handle:
            tf.addfile(info, handle)
    else:
        tf.addfile(info)


def tar_tree(folder: Path, target: Path) -> None:
    with tarfile.open(target, "w:gz", compresslevel=6) as tf:
        for path in sorted(folder.rglob("*")):
            _add(tf, path, str(path.relative_to(folder.parent)))


def tar_binary(path: Path, target: Path) -> None:
    with tarfile.open(target, "w:gz", compresslevel=6) as tf:
        _add(tf, path, path.name, executable=True)


def zip_tree(folder: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder.parent))


def windows(version: str) -> list[Path]:
    release = ROOT / "release" / "windows"
    release.mkdir(parents=True, exist_ok=True)
    exe = release / f"autoforge-{version}-windows-x64.exe"
    shutil.copy2(ROOT / "dist" / "autoforge.exe", exe)
    bundle = release / f"autoforge-{version}-windows-x64-onedir.zip"
    zip_tree(ROOT / "dist-onedir" / "autoforge", bundle)
    return [exe, bundle]


def linux(version: str) -> list[Path]:
    release = ROOT / "release" / "linux"
    release.mkdir(parents=True, exist_ok=True)
    binary = release / f"autoforge-{version}-linux-x64"
    shutil.copy2(ROOT / "dist-linux" / "autoforge", binary)
    single = release / f"autoforge-{version}-linux-x64.tar.gz"
    tar_binary(binary, single)
    binary.unlink()
    bundle = release / f"autoforge-{version}-linux-x64-onedir.tar.gz"
    tar_tree(ROOT / "dist-linux-onedir" / "autoforge", bundle)
    return [single, bundle]


PLATFORMS = {"windows": windows, "linux": linux}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    release = ROOT / "release" / args.platform
    for stale in release.glob(f"autoforge-{args.version}-*"):
        stale.unlink()

    artefacts = PLATFORMS[args.platform](args.version)
    previous = sorted(release.glob("autoforge-*-windows-x64.exe")) + sorted(release.glob("autoforge-*-linux-x64"))
    lines = [f"{sha256(path)}  {path.name}" for path in [*artefacts, *(p for p in previous if p not in artefacts)]]
    (release / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for path in artefacts:
        print(f"{path.stat().st_size:>12,}  {path.name}")
    print(f"checksums -> {release / 'SHA256SUMS.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
