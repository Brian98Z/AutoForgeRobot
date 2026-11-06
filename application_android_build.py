#!/usr/bin/env python3
"""Provision official user-local Android tools and build manifest-bound Java APKs.

Official package metadata supplies checksums (currently SHA-1); downloaded archive
SHA-256 values are also recorded as local integrity evidence, not Google signatures.
License acceptance is explicit and never performed automatically.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, shlex, shutil, subprocess, sys, time, urllib.request, uuid, xml.etree.ElementTree as ET, zipfile
from pathlib import Path, PurePosixPath

REPO = 'https://dl.google.com/android/repository/repository2-1.xml'
USER_AGENT = 'AutoForge-AndroidBuild/1.0'
MAX_XML = 32 * 1024 * 1024
MAX_ARCHIVE = 2 * 1024 * 1024 * 1024
MAX_FILES = 100000
class AndroidBuildError(ValueError): pass

def sha_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def fetch(url: str, limit: int) -> bytes:
    if not (url.startswith('https://dl.google.com/android/repository/') or url.startswith('https://dl.google.com/android/repository/sys-img/')):
        raise AndroidBuildError('refusing non-Google Android repository URL')
    req=urllib.request.Request(url,headers={'User-Agent':USER_AGENT})
    with urllib.request.urlopen(req,timeout=60) as response:
        if response.geturl()!=url or response.status!=200: raise AndroidBuildError('official repository response URL/status mismatch')
        data=response.read(limit+1)
    if len(data)>limit: raise AndroidBuildError('download exceeds configured size limit')
    return data
def packages(xml: bytes, host_os: str|None=None) -> list[dict]:
    try: root=ET.fromstring(xml)
    except ET.ParseError as e: raise AndroidBuildError('official repository XML is malformed') from e
    def child(node, name): return next((x for x in node if x.tag.rsplit('}',1)[-1]==name),None)
    def text(node,name):
        x=child(node,name)
        return (x.text or '').strip() if x is not None else ''
    rows=[]
    for node in root.iter():
        if node.tag.rsplit('}',1)[-1]!='remotePackage': continue
        path=node.get('path',''); revnode=child(node,'revision'); rev=text(revnode,'major') if revnode is not None else ''
        archives=child(node,'archives'); archive_rows=[x for x in archives if x.tag.rsplit('}',1)[-1]=='archive'] if archives is not None else []
        if host_os:
            matching=[a for a in archive_rows if text(a,'host-os')==host_os]
            archive_rows=matching if matching else [a for a in archive_rows if not text(a,'host-os')]
        for archive in archive_rows:
            complete=child(archive,'complete')
            if complete is None: continue
            url=text(complete,'url'); checksum=text(complete,'checksum'); kind=text(complete,'checksumType') or 'sha1'
            try: size=int(text(complete,'size') or '0')
            except ValueError: continue
            if not url or kind.lower() not in {'sha1','sha256'} or not re.fullmatch(r'[0-9a-fA-F]{40}' if kind.lower()=='sha1' else r'[0-9a-fA-F]{64}',checksum or ''): continue
            if not re.fullmatch(r'[A-Za-z0-9._;/-]+',path) or '..' in path.split('/'): continue
            rows.append({'path':path,'revision':rev,'url':url if url.startswith('https://') else 'https://dl.google.com/android/repository/'+url,'checksum':checksum.lower(),'checksum_type':kind.lower(),'size':size})
    return rows

def official_inventory() -> dict:
    raw=fetch(REPO,MAX_XML); rows=packages(raw)
    if not rows: raise AndroidBuildError('official XML contains no supported checksum-bound packages')
    return {'schema':1,'repository':REPO,'repository_xml_sha256':hashlib.sha256(raw).hexdigest(),'packages':rows}

def select(rows: list[dict], name: str) -> dict:
    found=[r for r in rows if r['path']==name]
    if name.endswith(';latest'):
        prefix=name[:-7]; found=[r for r in rows if r['path']==prefix or r['path'].startswith(prefix+';')]
    if not found: raise AndroidBuildError(f'official repository package unavailable: {name}')
    return found[0]
def system_image_inventory(api: str, tag: str='google_apis', abi: str='x86_64') -> dict:
    if not re.fullmatch(r'\d{2,3}',api) or tag not in {'google_apis','google_apis_playstore','default'} or abi not in {'x86_64','arm64-v8a'}:
        raise AndroidBuildError('unsupported emulator API, image tag, or ABI')
    url='https://dl.google.com/android/repository/sys-img/'+tag+'/sys-img2-3.xml'
    raw=fetch(url,MAX_XML); rows=[]
    for row in packages(raw):
        if row['path']==f'system-images;android-{api};{tag};{abi}':
            row['url']='https://dl.google.com/android/repository/sys-img/'+tag+'/'+row['url'].rsplit('/',1)[-1]
            rows.append(row)
    if not rows: raise AndroidBuildError('official system-image metadata lacks requested package')
    return {'repository':url,'repository_xml_sha256':hashlib.sha256(raw).hexdigest(),'packages':rows}

def emulator_inventory() -> dict:
    raw=fetch(REPO,MAX_XML); rows=packages(raw,host_os='windows')
    selected=[row for row in rows if row['path']=='emulator']
    if not selected: raise AndroidBuildError('official repository has no Windows x64 emulator package')
    return {'repository_xml_sha256':hashlib.sha256(raw).hexdigest(),'packages':selected}


def provision_emulator(sdk: Path, avd_root: Path, api: str, accept_license: bool, tag: str='google_apis', abi: str='x86_64', hardware_acceleration: bool=False) -> dict:
    if not accept_license: raise AndroidBuildError('Google SDK license acknowledgment required before emulator provisioning')
    sdk=sdk.absolute(); avd_root=avd_root.absolute()
    if sdk.exists() and any(sdk.iterdir()): raise AndroidBuildError('SDK provisioning requires an empty fresh user-local SDK directory')
    if avd_root.exists() and any(avd_root.iterdir()): raise AndroidBuildError('AVD root must be empty and exclusively owned')
    sdk.mkdir(parents=True,exist_ok=True); avd_root.mkdir(parents=True,exist_ok=True)
    repo=official_inventory(); image=system_image_inventory(api,tag,abi)
    emulator=emulator_inventory()['packages'][0]
    packages_to_install=[select(packages(fetch(REPO,MAX_XML),host_os='windows'),'platform-tools;latest'),select(repo['packages'],'cmdline-tools;latest'),select(repo['packages'],'platforms;android-'+api),select(repo['packages'],'build-tools;36.0.0'),emulator,image['packages'][0]]
    cache=sdk.parent/'.autoforge-android-downloads'; receipts=[]
    for pkg in packages_to_install:
        archive=_download_package(pkg,cache); staging=sdk/'.staging'/pkg['checksum']
        if staging.exists(): shutil.rmtree(staging)
        staging.mkdir(parents=True); _safe_extract(archive.read_bytes(),staging)
        package=pkg['path']
        rel=Path('cmdline-tools/latest') if package.startswith('cmdline-tools;') else (Path('emulator') if package=='emulator' else (Path('platform-tools') if package.startswith('platform-tools;') else Path(package.replace(';','/'))))
        target=sdk/rel
        if target.exists(): raise AndroidBuildError('SDK package destination collision')
        target.parent.mkdir(parents=True,exist_ok=True); target.mkdir()
        roots=[p for p in staging.iterdir() if p.is_dir()]
        source=roots[0] if package.startswith('cmdline-tools;') and len(roots)==1 else staging
        for item in list(source.iterdir()): item.replace(target/item.name)
        shutil.rmtree(staging)
        receipts.append({'path':package,'publisher_checksum':pkg['checksum'],'checksum_type':pkg['checksum_type'],'download_sha256':sha_file(archive),'size':pkg['size']})
    avdmanager=sdk/'cmdline-tools'/'latest'/'bin'/'avdmanager.bat'
    image_path=f'system-images;android-{api};{tag};{abi}'
    name='autoforge-'+uuid.uuid4().hex[:12]
    avd_path=avd_root/name; avd_path.mkdir(parents=True,exist_ok=False)
    avdmanager_env=os.environ.copy(); avdmanager_env['ANDROID_AVD_HOME']=str(avd_root)
    p=subprocess.run([str(avdmanager),'create','avd','--name',name,'--package',image_path,'--path',str(avd_path),'--force'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,check=False,env=avdmanager_env,close_fds=True)
    if p.returncode: raise AndroidBuildError('avdmanager failed: '+p.stderr[-3000:].decode('utf8','replace'))
    emulator=sdk/'emulator'/'emulator.exe'; adb=sdk/'platform-tools'/'adb.exe'
    serial='emulator-5580'
    devices=_run([str(adb),'devices','-l']).splitlines()
    if any(line.split() and line.split()[0]==serial for line in devices[1:]): raise AndroidBuildError('refusing emulator serial already owned by another process')
    proc=subprocess.Popen([str(emulator),'-avd',name,'-no-snapshot','-no-audio','-no-boot-anim','-port','5580','-accel','on' if hardware_acceleration else 'off'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,env={**os.environ,'ANDROID_AVD_HOME':str(avd_root)})
    serial='emulator-5580'; deadline=time.monotonic()+180
    try:
        while time.monotonic()<deadline:
            if proc.poll() is not None: raise AndroidBuildError('owned emulator exited before boot completion')
            try:
                state=_run([str(adb),'-s',serial,'get-state'],timeout=5).strip()
                boot=_run([str(adb),'-s',serial,'shell','getprop','sys.boot_completed'],timeout=10).strip() if state=='device' else ''
                if boot=='1': break
            except AndroidBuildError: pass
            time.sleep(2)
        identity=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',f"Get-CimInstance Win32_Process -Filter 'ProcessId = {proc.pid}' | Select-Object ProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress"],capture_output=True,text=True,timeout=15,check=True)
        process=json.loads(identity.stdout)
        if not isinstance(process,dict) or int(process.get('ProcessId',0))!=proc.pid or Path(process.get('ExecutablePath','')).resolve()!=emulator.resolve() or name not in process.get('CommandLine','') or '-port 5580' not in process.get('CommandLine',''): raise AndroidBuildError('cannot establish exact spawned emulator process identity')
        ownership={'schema':1,'serial':serial,'avd_name':name,'emulator_pid':proc.pid,'process_creation_date':process['CreationDate'],'process_executable':str(emulator.resolve()),'process_command_line':process['CommandLine'],'emulator_executable_sha256':sha_file(emulator),'adb_executable_sha256':sha_file(adb),'sdk':str(sdk),'avd_root':str(avd_root)}
        owner_record=avd_root/'autoforge-emulator-owner.json'
        owner_record.write_text(json.dumps(ownership,sort_keys=True)+'\n',encoding='utf8')
        return {'status':'EMULATOR_READY',**ownership,'owner_record':str(owner_record),'hardware_acceleration':hardware_acceleration,'packages':receipts,'repository_xml_sha256':repo['repository_xml_sha256'],'system_image_xml_sha256':image['repository_xml_sha256'],'license_acknowledged':True,'limitations':['Only this fresh named AVD and exact spawned emulator process are owned; physical adb devices are never selected.','Official SHA-1 and locally computed download SHA-256 are recorded; no Google signature verification is claimed.']}
    except Exception:
        proc.terminate()
        try: proc.wait(timeout=15)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
        raise

def stop_emulator(sdk: Path, serial: str, pid: int, owner_record: Path) -> dict:
    record=json.loads(owner_record.read_text(encoding='utf8'))
    if record.get('serial')!=serial or record.get('emulator_pid')!=pid or not re.fullmatch(r'emulator-\d+',serial):
        raise AndroidBuildError('stop identity does not match exact saved AutoForge emulator ownership record')
    adb=sdk/'platform-tools'/'adb.exe'; emulator=sdk/'emulator'/'emulator.exe'
    if sha_file(adb)!=record.get('adb_executable_sha256') or sha_file(emulator)!=record.get('emulator_executable_sha256'):
        raise AndroidBuildError('SDK executable hashes differ from ownership record; refusing stop')
    query=f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' | Select-Object ProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    proc=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',query],capture_output=True,text=True,timeout=15,check=True)
    if not proc.stdout.strip(): return {'status':'ALREADY_STOPPED','serial':serial,'pid':pid}
    live=json.loads(proc.stdout)
    if int(live.get('ProcessId',0))!=pid or live.get('CreationDate')!=record.get('process_creation_date') or Path(live.get('ExecutablePath','')).resolve()!=Path(record.get('process_executable','')).resolve() or live.get('CommandLine')!=record.get('process_command_line'):
        raise AndroidBuildError('PID was reused or emulator command identity changed; refusing shutdown')
    name=record.get('avd_name')
    if not isinstance(name,str) or not re.fullmatch(r'autoforge-[a-f0-9]{12}',name): raise AndroidBuildError('invalid recorded AVD identity')
    observed=_run([str(adb),'-s',serial,'shell','getprop','ro.boot.qemu.avd_name'],timeout=10).strip()
    if observed!=name: raise AndroidBuildError('ADB serial does not report recorded owned AVD name; refusing shutdown')
    _run([str(adb),'-s',serial,'emu','kill'],timeout=15)
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        check=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',query],capture_output=True,text=True,timeout=15,check=True)
        if not check.stdout.strip(): return {'status':'STOPPED','serial':serial,'pid':pid}
        time.sleep(.5)
    raise AndroidBuildError('owned emulator process was not observed stopped')
def _safe_extract(blob: bytes|Path, dest: Path) -> None:
    dest.mkdir(parents=True,exist_ok=True)
    count=0; total=0
    archive=zipfile.ZipFile(blob if isinstance(blob,Path) else __import__('io').BytesIO(blob))
    with archive as z:
        for info in z.infolist():
            count+=1; total+=info.file_size
            raw=info.filename.replace('\\\\','/')
            p=PurePosixPath(raw)
            mode=(info.external_attr >> 16) & 0xffff
            if count>MAX_FILES or total>MAX_ARCHIVE or info.file_size>MAX_ARCHIVE or p.is_absolute() or not p.parts or any(x in ('','.','..') for x in p.parts) or (mode & 0o170000)==0o120000:
                raise AndroidBuildError('unsafe or oversized SDK archive entry')
            out=dest.joinpath(*p.parts)
            resolved=out.resolve()
            if dest.resolve() not in resolved.parents and resolved!=dest.resolve(): raise AndroidBuildError('SDK archive traversal rejected')
            if info.is_dir(): out.mkdir(parents=True,exist_ok=True); continue
            out.parent.mkdir(parents=True,exist_ok=True)
            with z.open(info) as src, out.open('xb') as dst: shutil.copyfileobj(src,dst,1024*1024)
            if os.name!='nt':
                mode=(info.external_attr >> 16) & 0o777
                out.chmod(mode & 0o755 if mode else 0o644)

def _download_package(pkg: dict, cache: Path) -> Path:
    cache.mkdir(parents=True,exist_ok=True); target=cache/pkg['checksum']
    def valid(path):
        h=hashlib.new(pkg['checksum_type']); sha=hashlib.sha256(); size=0
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''): size+=len(block); h.update(block); sha.update(block)
        return size==pkg['size'] and h.hexdigest()==pkg['checksum']
    if target.exists():
        if not valid(target): raise AndroidBuildError('cached SDK archive checksum mismatch')
        return target
    req=urllib.request.Request(pkg['url'],headers={'User-Agent':USER_AGENT})
    tmp=target.with_suffix('.part'); h=hashlib.new(pkg['checksum_type']); size=0
    try:
        with urllib.request.urlopen(req,timeout=60) as response, tmp.open('wb') as dst:
            if response.status!=200 or not response.geturl().startswith('https://dl.google.com/android/repository/'): raise AndroidBuildError('SDK download redirected away from Google HTTPS')
            while True:
                block=response.read(1024*1024)
                if not block: break
                size+=len(block)
                if size>pkg['size'] or size>MAX_ARCHIVE: raise AndroidBuildError('SDK archive exceeds official size')
                h.update(block); dst.write(block)
        if size!=pkg['size'] or h.hexdigest()!=pkg['checksum']: raise AndroidBuildError('official SDK archive size/checksum mismatch')
        tmp.replace(target); return target
    except Exception:
        tmp.unlink(missing_ok=True); raise

def provision(sdk: Path, api: str, build_tools: str, java_zip: str|None, accept_license: bool, host_os: str='linux') -> dict:
    if not accept_license: raise AndroidBuildError('Google SDK license acknowledgment required: pass --accept-sdk-license only after operator review')
    if not sdk.is_absolute() or not re.fullmatch(r'\d{2,3}',api) or not re.fullmatch(r'\d+\.\d+\.\d+',build_tools): raise AndroidBuildError('absolute SDK path, API integer and exact build-tools version required')
    if host_os not in {'windows','linux','macosx'}: raise AndroidBuildError('unsupported SDK host OS')
    inv=official_inventory(); rows=packages(fetch(REPO,MAX_XML),host_os=host_os); cache=sdk.parent/'.autoforge-android-downloads'
    wanted=[f'platforms;android-{api}',f'build-tools;{build_tools}','platform-tools;latest','cmdline-tools;latest','emulator']
    if java_zip: wanted.append(java_zip)
    receipts=[]
    for name in wanted:
        pkg=select(rows,name); archive=_download_package(pkg,cache); staging=sdk/'.staging'/pkg['checksum']
        if staging.exists(): shutil.rmtree(staging)
        staging.mkdir(parents=True); _safe_extract(archive,staging)
        if name.startswith('cmdline-tools;'):
            source=next((x for x in staging.iterdir() if x.is_dir()),None)
            if source is None: raise AndroidBuildError('command-line tools archive layout invalid')
            rel=Path('cmdline-tools/latest')
        elif name=='emulator':
            source=next((x for x in staging.iterdir() if x.is_dir()),None)
            if source is None: raise AndroidBuildError('emulator archive layout invalid')
            rel=Path('emulator')
        elif name.startswith('platform-tools;'):
            source=next((x for x in staging.iterdir() if x.is_dir()),None)
            if source is None: raise AndroidBuildError('platform-tools archive layout invalid')
            rel=Path('platform-tools')
        elif name.startswith('system-images;'):
            source=staging; rel=Path(name.replace(';','/'))
        else:
            expected=Path(name.replace(';','/')).name
            candidates=[x for x in staging.rglob(expected) if x.is_dir()]
            source=candidates[0] if len(candidates)==1 else staging
            rel=Path(name.replace(';','/'))
        target=sdk/rel
        if target.exists(): raise AndroidBuildError(f'refusing to replace SDK package: {name}')
        target.parent.mkdir(parents=True,exist_ok=True); target.mkdir()
        for item in list(source.iterdir()): item.replace(target/item.name)
        shutil.rmtree(staging)
        receipts.append({'path':name,'publisher_checksum':pkg['checksum'],'checksum_type':pkg['checksum_type'],'download_sha256':sha_file(archive),'size':pkg['size']})
    return {'status':'PROVISIONED','sdk':str(sdk),'repository_xml_sha256':inv['repository_xml_sha256'],'packages':receipts,'license_acknowledged':True,'limitations':['Official package SHA-1 is recorded with independent local download SHA-256; no Google signature verification is claimed.','User-local SDK only; no global PATH or system configuration changed.']}

def _run(argv:list[str],timeout:int=120)->str:
    try:r=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False,close_fds=True)
    except (OSError,subprocess.TimeoutExpired) as e: raise AndroidBuildError(f'tool unavailable or timed out: {argv[0]}') from e
    if r.returncode: raise AndroidBuildError(f'tool failed ({argv[0]}): {r.stderr[-3000:].decode("utf-8","replace")}')
    return r.stdout.decode('utf-8','replace')

def build(manifest_path: Path, sdk: Path, out: Path, engine: str, image: str, build_tools_version: str='36.0.0') -> dict:
    manifest=json.loads(manifest_path.read_text(encoding='utf8'))
    if not isinstance(manifest,dict) or not isinstance(manifest.get('workspace'),str) or not isinstance(manifest.get('files'),list): raise AndroidBuildError('workspace manifest requires workspace and file inventory')
    root=Path(manifest['workspace']).resolve(strict=True); files={}; rows_by_path={}
    for row in manifest['files']:
        if not isinstance(row,dict) or set(row)!={'path','size','sha256'}: raise AndroidBuildError('malformed workspace file record')
        rel=row['path']
        if not isinstance(rel,str) or not rel or '\\' in rel or rel.startswith('/') or any(x in ('','.','..') for x in rel.split('/')) or rel in files: raise AndroidBuildError('unsafe or duplicate manifest path')
        p=root.joinpath(*rel.split('/'))
        if p.is_symlink() or not p.is_file() or p.stat().st_size!=row['size'] or sha_file(p)!=row['sha256']: raise AndroidBuildError(f'manifest file mismatch: {rel}')
        files[rel]=p; rows_by_path[rel]=row
    canonical={'schema':1,'goal':manifest['goal'],'references':manifest['references'],'files':[{'path':r['path'],'size':r['size'],'sha256':r['sha256']} for r in manifest['files']]}
    digest=hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    if manifest.get('candidate_sha256')!=digest: raise AndroidBuildError('workspace manifest digest mismatch')
    if 'AndroidManifest.xml' not in files or not any(name.endswith('.java') for name in files): raise AndroidBuildError('manifest-bound AndroidManifest.xml and Java sources required')
    if out.exists(): raise AndroidBuildError('output path already exists')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image): raise AndroidBuildError('exact sha256:<64 lowercase hex> Linux JDK image required')
    info=__import__('application_builder').preflight(engine,image)
    if info['server_os']!='linux': raise AndroidBuildError('Android compiler must run in Linux container')
    sdk=sdk.resolve(strict=True); out=out.absolute(); out.parent.mkdir(parents=True,exist_ok=True)
    if out.exists(): raise AndroidBuildError('output path already exists')
    scratch=Path(__import__('tempfile').mkdtemp(prefix='.android-build-',dir=out.parent))
    output=scratch/'output'; output.mkdir(); output.chmod(0o777)
    candidate_root=scratch/'candidate'; candidate_root.mkdir()
    for rel,source in files.items():
        dest=candidate_root.joinpath(*rel.split('/')); dest.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(source,dest)
        if sha_file(dest)!=rows_by_path[rel]['sha256']: raise AndroidBuildError('staged candidate bytes changed')
    java_args=' '.join(shlex.quote('/candidate/'+rel) for rel in sorted(files) if rel.endswith('.java'))
    bt=sdk/'build-tools'/build_tools_version; platform=sdk/'platforms'/'android-35'
    if not (platform/'android.jar').is_file() or not (bt/'aapt2').exists() or not (bt/'lib'/'d8.jar').is_file():
        shutil.rmtree(scratch,ignore_errors=True); raise AndroidBuildError('API 35 and matching Linux build-tools SDK packages required')
    sdk_tools=f'/sdk/build-tools/{build_tools_version}:/sdk/platform-tools:/sdk/cmdline-tools/latest/bin'
    code=f"set -eu; mkdir -p /tmp/work/classes /tmp/work/dex /tmp/work/app-unpack; aapt2 link -o /tmp/work/app.apk --manifest /candidate/AndroidManifest.xml --min-sdk-version 23 -I /sdk/platforms/android-35/android.jar; javac -proc:none -source 8 -target 8 -bootclasspath /sdk/platforms/android-35/android.jar -d /tmp/work/classes {java_args}; jar cf /tmp/work/classes.jar -C /tmp/work/classes .; d8 --lib /sdk/platforms/android-35/android.jar --output /tmp/work/dex /tmp/work/classes.jar; cd /tmp/work/app-unpack; unzip -q /tmp/work/app.apk; cp /tmp/work/dex/classes*.dex .; zip -qr /tmp/work/unsigned.apk .; zip -q -0 /tmp/work/unsigned.apk resources.arsc; zipalign -f 4 /tmp/work/unsigned.apk /tmp/work/aligned.apk; keytool -genkeypair -keystore /tmp/work/debug.keystore -storepass android -keypass android -alias androiddebugkey -dname 'CN=Android Debug,O=AutoForge,C=US' -keyalg RSA -keysize 2048 -validity 10000 -noprompt; apksigner sign --ks /tmp/work/debug.keystore --ks-pass pass:android --key-pass pass:android --out /output/app.apk /tmp/work/aligned.apk"
    code='export PATH='+shlex.quote(sdk_tools)+':"${JAVA_HOME:+$JAVA_HOME/bin:}$PATH"; '+code
    name='autoforge-android-'+uuid.uuid4().hex
    owner='autoforge-android-build-'+uuid.uuid4().hex
    cmd=[engine,'run','--name',name,'--label',f'autoforge.owner={owner}','--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','--memory=2g','--cpus=2','--mount',f'type=bind,src={candidate_root},dst=/candidate,readonly','--mount',f'type=bind,src={sdk},dst=/sdk,readonly','--mount',f'type=bind,src={output},dst=/output','--tmpfs','/tmp:rw,nosuid,nodev,size=1g,mode=1777','--workdir','/tmp','--entrypoint','/bin/sh',image,'-c',code]
    try:
        _run(cmd,timeout=300)
        artifact=Path(output)/'app.apk'
        if not artifact.is_file() or artifact.stat().st_size==0: raise AndroidBuildError('isolated container produced no APK')
        if out.exists(): raise AndroidBuildError('output path collision')
        tmp=out.with_name(out.name+'.'+uuid.uuid4().hex+'.part'); shutil.copyfile(artifact,tmp)
        try: os.link(tmp,out)
        except FileExistsError as e: raise AndroidBuildError('output path collision') from e
        finally: tmp.unlink(missing_ok=True)
    finally:
        inspect=subprocess.run([engine,'inspect','--format','{{ index .Config.Labels "autoforge.owner" }}',name],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=False)
        if inspect.returncode==0 and inspect.stdout.decode('utf8','replace').strip()==owner:
            subprocess.run([engine,'rm','-f',name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20,check=False)
            remaining=subprocess.run([engine,'inspect','--format','{{ index .Config.Labels "autoforge.owner" }}',name],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=False)
            if remaining.returncode==0:
                raise AndroidBuildError('owned Android build container remains after cleanup; preserving scratch evidence')
            presence=subprocess.run([engine,'ps','-aq','--filter',f'name=^{name}$'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=False)
            if presence.returncode or presence.stdout.strip(): raise AndroidBuildError('owned container removal is unconfirmed; preserving scratch evidence')
        elif inspect.returncode==0:
            raise AndroidBuildError('container name is not owned by this build; refusing cleanup and preserving scratch evidence')
        else:
            presence=subprocess.run([engine,'ps','-aq','--filter',f'name=^{name}$'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=False)
            if presence.returncode or presence.stdout.strip(): raise AndroidBuildError('container state is uncertain after failed inspect; preserving scratch evidence')
        shutil.rmtree(scratch,ignore_errors=True)
    return {'status':'BUILT','candidate_sha256':digest,'apk':str(out),'apk_sha256':sha_file(out),'container_image':image,'container_network':'none','sdk_mount':'read-only','candidate_mount':'read-only manifest-staged only','candidate_executed':False,'build_tools_version':build_tools_version,'signing':'ephemeral debug test key; not trusted/release','limitations':['Build uses only exact manifest files staged into a readonly candidate mount.','No emulator install, launch, UI assertion or native behavior proof performed.']}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('metadata',help='fetch official repository XML and print checksum-bound package inventory')
    q=sub.add_parser('provision'); q.add_argument('--sdk',type=Path,required=True); q.add_argument('--api',required=True); q.add_argument('--build-tools',required=True); q.add_argument('--java-zip',help='optional official package path'); q.add_argument('--host-os',choices=('windows','linux','macosx'),default='windows'); q.add_argument('--accept-sdk-license',action='store_true')
    q=sub.add_parser('build'); q.add_argument('--manifest',type=Path,required=True); q.add_argument('--sdk',type=Path,required=True); q.add_argument('--output',type=Path,required=True); q.add_argument('--engine',choices=('docker','podman'),default='docker'); q.add_argument('--image',required=True,help='exact locally present Linux JDK image sha256 ID'); q.add_argument('--build-tools',default='36.0.0')
    q=sub.add_parser('emulator'); q.add_argument('--sdk',type=Path,required=True); q.add_argument('--avd-root',type=Path,required=True); q.add_argument('--api',default='35'); q.add_argument('--tag',choices=('google_apis','google_apis_playstore','default'),default='google_apis'); q.add_argument('--abi',choices=('x86_64','arm64-v8a'),default='x86_64'); q.add_argument('--accept-sdk-license',action='store_true'); q.add_argument('--hardware-acceleration',action='store_true',help='request WHPX acceleration; default is software emulation')
    q=sub.add_parser('stop-emulator'); q.add_argument('--sdk',type=Path,required=True); q.add_argument('--serial',required=True); q.add_argument('--pid',type=int,required=True); q.add_argument('--owner-record',type=Path,required=True)
    a=p.parse_args(argv)
    try:
        if a.cmd=='metadata': result=official_inventory()
        elif a.cmd=='provision': result=provision(a.sdk,a.api,a.build_tools,a.java_zip,a.accept_sdk_license,a.host_os)
        elif a.cmd=='build': result=build(a.manifest,a.sdk,a.output,a.engine,a.image,a.build_tools)
        elif a.cmd=='stop-emulator': result=stop_emulator(a.sdk,a.serial,a.pid,a.owner_record)
        else: result=provision_emulator(a.sdk,a.avd_root,a.api,a.accept_sdk_license,a.tag,a.abi,a.hardware_acceleration)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True)); return 0
    except (AndroidBuildError,OSError,KeyError,ValueError,zipfile.BadZipFile) as e:
        print(json.dumps({'status':'BLOCKED','reason':str(e),'candidate_executed':False}),file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())
