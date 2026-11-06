"""Offline, digest-verified engineering snapshots; restore only into an absent directory.
Snapshots contain state signing keys: keep them private. Manifest digest must be retained separately.
"""
import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tomllib
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def regular(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError(f'link/reparse point refused: {path}')
    return info


def tree(path, excluded=()):
    regular(path)
    if path.is_file():
        yield path, Path(path.name)
        return
    for folder, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in excluded]
        for d in dirs:
            regular(Path(folder) / d)
        for name in files:
            if name in excluded:
                continue
            source = Path(folder) / name
            if not stat.S_ISREG(regular(source).st_mode):
                raise ValueError(f'nonregular file refused: {source}')
            yield source, source.relative_to(path)


@contextmanager
def lock(path):
    with path.open('a+b') as f:
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def publish_directory(stage, target):
    if os.name != 'nt':
        for folder, _, _ in os.walk(stage, topdown=False):
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move.restype = ctypes.c_int
        if not move(str(stage), str(target), 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    elif sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        if rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1):
            raise OSError(ctypes.get_errno(), 'no-replace directory publication failed')
        try:
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            print(json.dumps({'warning': 'directory committed; parent durability unconfirmed', 'error': str(exc)}), file=sys.stderr)
    else:
        raise OSError('durable no-replace directory publication unsupported on this platform')


def synced_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def copied_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    regular(source)
    with source.open('rb') as src, destination.open('xb') as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    shutil.copymode(source, destination)
    sha = digest(destination)
    if digest(source) != sha:
        raise ValueError(f'source changed during snapshot: {source}')
    return sha


def command(argv, env=None):
    p = subprocess.run(list(map(str, argv)), capture_output=True, encoding='utf-8', env=env, timeout=240)
    if p.returncode:
        raise RuntimeError(f'{argv}: {p.stderr}')
    return p.stdout


def discard_stage(stage):
    def writable_retry(function, path, exc_info):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)
    try:
        shutil.rmtree(stage, onerror=writable_retry)
    except OSError as exc:
        print(json.dumps({'warning': 'unpublished stage cleanup failed', 'stage': str(stage), 'error': str(exc)}), file=sys.stderr)


def capture(state, destination):
    state, destination = state.resolve(), destination.absolute()
    if destination.exists():
        raise FileExistsError(destination)
    if destination.is_relative_to(state):
        raise ValueError('snapshot destination must be outside live state')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + destination.name + '-', dir=destination.parent))
    records, originals = {}, {}
    def add(source, rel):
        if rel.as_posix() in records:
            raise ValueError(f'duplicate bundle path: {rel}')
        sha = copied_file(source, stage / rel)
        records[rel.as_posix()] = {'sha256': sha, 'size': (stage / rel).stat().st_size}
        originals[str(source)] = sha
    def add_tree(source, prefix, excluded=()):
        for file, rel in tree(source, excluded):
            add(file, prefix / rel)
    try:
        # Serialize state copy with controller and kill; long runtime copying occurs unlocked.
        with lock(state / 'control.lock'), lock(state / 'commit.lock'):
            for source, rel in tree(state, ('control.lock', 'commit.lock', 'drills')):
                if source.suffix != '.tmp':
                    add(source, Path('state') / rel)
        # Source and tests, but not generated runs, unrelated states or compiler build caches.
        for name in ('src', 'providers', 'examples', 'tests', 'fixtures', 'universal'):
            add_tree(ROOT / name, Path(name), ('results', '__pycache__'))
        for file in ROOT.iterdir():
            if file.is_file() and (file.suffix in ('.py', '.md', '.toml', '.lock', '.cmd') or file.name == '.gitignore'):
                add(file, Path(file.name))
        add(ROOT / 'target/release/autoforge.exe', Path('target/release/autoforge.exe'))
        add_tree(ROOT / 'runtime', Path('runtime'))
        # Current Python workloads use stdlib only. Exclude unrelated installed application packages.
        pybase = Path(sys.base_prefix)
        for folder in ('Lib', 'DLLs', 'libs'):
            add_tree(pybase / folder, Path('runtime/python') / folder, ('site-packages', '__pycache__'))
        for file in pybase.iterdir():
            if file.is_file() and file.suffix in ('.exe', '.dll', '.txt'):
                add(file, Path('runtime/python') / file.name)
        julia = ROOT / 'runtime/julia-1.13.3/bin/julia.exe'
        dependency_code = 'using Pkg, JSON3; println(JSON3.write([Dict("name"=>d.name,"source"=>d.source) for d in values(Pkg.dependencies()) if d.is_tracking_registry]))'
        deps = json.loads(command([julia, '--startup-file=no', f'--project={ROOT / "providers"}', '-e', dependency_code]))
        for dep in deps:
            source = Path(dep['source'])
            if source.resolve().is_relative_to((ROOT / 'runtime').resolve()):
                continue
            add_tree(source, Path('runtime/julia-depot/packages') / dep['name'] / source.name, ('__pycache__',))
        # Preserve actual compiler and cached dependency inputs, not only Cargo.lock fingerprints.
        toolchain = Path(command(['rustc', '--print', 'sysroot']).strip())
        add_tree(toolchain, Path('runtime/rust-toolchain'))
        cargo_home = Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo'))
        packages = tomllib.loads((ROOT / 'Cargo.lock').read_text(encoding='utf-8'))['package']
        registry = cargo_home / 'registry'
        add_tree(registry / 'index', Path('runtime/cargo-home/registry/index'))
        for package in packages:
            if not package.get('source', '').startswith('registry+'):
                continue
            crate = package['name'] + '-' + package['version']
            matches = list((registry / 'cache').glob('*/' + crate + '.crate'))
            if not matches:
                raise ValueError(f'missing cached build dependency: {crate}')
            for file in matches:
                add(file, Path('runtime/cargo-home/registry/cache') / file.relative_to(registry / 'cache'))
        for filename in ('config', 'config.toml'):
            if (cargo_home / filename).exists():
                raise ValueError('custom Cargo configuration requires explicit portable review; not copied silently')
        # State may legitimately advance after unlocking; it was captured under both locks.
        for name, expected in originals.items():
            source = Path(name)
            if not source.is_relative_to(state) and digest(source) != expected:
                raise ValueError(f'engineering input changed during snapshot: {source}')
        manifest = {'schema': 1, 'platform': sys.platform, 'python': sys.version,
                    'scope': 'offline source, state, execution runtimes, Julia packages, Rust toolchain and Cargo registry',
                    'limits': ['not an OS image; Windows SDK/MSVC linker remains a host build prerequisite', 'no external service rollback', 'no hardware power-loss certification', 'Python stdlib-only workloads'],
                    'files': records}
        synced_write(stage / 'snapshot-manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode())
        sha = digest(stage / 'snapshot-manifest.json')
        publish_directory(stage, destination)
        print(json.dumps({'snapshot': str(destination), 'manifest_sha256': sha, 'files': len(records), 'bytes': sum(r['size'] for r in records.values()), 'contains_signing_keys': True}))
    except BaseException:
        if stage.exists():
            discard_stage(stage)
        raise


def verify(bundle, expected):
    regular(bundle)
    manifest_path = bundle / 'snapshot-manifest.json'
    regular(manifest_path)
    if digest(manifest_path) != expected:
        raise ValueError('manifest checksum mismatch; use independently retained digest')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['schema'] != 1 or manifest['platform'] != sys.platform:
        raise ValueError('snapshot schema/platform mismatch')
    files = manifest['files']
    actual = {rel.as_posix() for _, rel in tree(bundle)}
    if actual != set(files) | {'snapshot-manifest.json'}:
        raise ValueError('snapshot inventory mismatch')
    for name, info in files.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or ':' in name or '\\' in name:
            raise ValueError('unsafe manifest path')
        file = bundle / path
        if file.stat().st_size != info['size'] or digest(file) != info['sha256']:
            raise ValueError(f'snapshot file mismatch: {name}')
    return manifest


def restore(bundle, destination, expected):
    bundle, destination = bundle.resolve(), destination.absolute()
    manifest = verify(bundle, expected)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + destination.name + '-', dir=destination.parent))
    try:
        for name, info in manifest['files'].items():
            if copied_file(bundle / name, stage / name) != info['sha256']:
                raise ValueError(f'snapshot changed during restore: {name}')
        synced_write(stage / 'snapshot-manifest.json', (bundle / 'snapshot-manifest.json').read_bytes())
        publish_directory(stage, destination)
    except BaseException:
        if stage.exists():
            discard_stage(stage)
        raise
    print(json.dumps({'restored': str(destination), 'manifest_sha256': expected, 'files': len(manifest['files'])}))


def run_restored(project, args):
    project = project.resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AUTOFORGE_', 'JULIA_', 'PYTHON'))}
    env.update(AUTOFORGE_PROJECT=str(project), AUTOFORGE_PYTHON=str(project/'runtime/python/python.exe'),
               AUTOFORGE_JULIA=str(project/'runtime/julia-1.13.3/bin/julia.exe'), AUTOFORGE_PROVIDER='offline',
               JULIA_DEPOT_PATH=str(project/'runtime/julia-depot'), JULIA_PKG_OFFLINE='true',
               CARGO_HOME=str(project/'runtime/cargo-home'), CARGO_NET_OFFLINE='true',
               PATH=str(project/'runtime/rust-toolchain/bin')+os.pathsep+os.environ.get('PATH',''))
    if not args:
        raise ValueError('controller arguments required')
    return subprocess.call([str(project/'target/release/autoforge.exe'), *args], cwd=project, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    cap = sub.add_parser('snapshot');cap.add_argument('--state', type=Path, required=True);cap.add_argument('--output', type=Path, required=True)
    for name in ('verify', 'restore'):
        cmd = sub.add_parser(name);cmd.add_argument('bundle', type=Path);cmd.add_argument('--sha256', required=True)
        if name == 'restore':cmd.add_argument('--output', type=Path, required=True)
    run = sub.add_parser('run');run.add_argument('project', type=Path);run.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == 'snapshot':capture(args.state,args.output)
    elif args.command == 'restore':restore(args.bundle,args.output,args.sha256)
    elif args.command == 'verify':
        manifest=verify(args.bundle,args.sha256);print(json.dumps({'status':'VERIFIED','files':len(manifest['files'])}))
    else:return run_restored(args.project,args.args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
