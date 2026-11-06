"""Execute hash-locked Python, Node, TypeScript, or Java builds in exact local Linux images."""
from application_checkpoint import regular_chain
import argparse, hashlib, json, os, re, shlex, shutil, subprocess, sys, tarfile, tempfile, time, uuid, zipfile
from pathlib import Path
from typing import Any
import application_dependencies as dependencies
import application_builder as builder
from email.parser import Parser
import applications

class ToolchainRefused(ValueError): pass
MAX_LOG=1_000_000
MAX_FILES=20_000
MAX_FILE=64*1024*1024
MAX_TOTAL_OUTPUT=128*1024*1024

def _sha(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def _json(x:Any)->bytes:return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def _run(argv:list[str],timeout:int=20)->str:
    try:r=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False,env={'PATH':os.environ.get('PATH','')},close_fds=True)
    except (OSError,subprocess.TimeoutExpired) as e:raise ToolchainRefused(f'engine command unavailable or timed out: {argv[0]}') from e
    if r.returncode:raise ToolchainRefused(f'engine command failed ({r.returncode}): {r.stderr[:1000].decode(errors="replace")}')
    return r.stdout.decode('utf-8','replace').strip()
def _safe_path(raw:str)->str:
    try:p=applications._validate_path(raw)
    except Exception as e:raise ToolchainRefused(f'invalid candidate path: {raw!r}') from e
    return p

def _candidate(root:Path, manifest:dict)->tuple[dict[str,bytes],str]:
    if not isinstance(manifest,dict) or not isinstance(manifest.get('files'),list):raise ToolchainRefused('candidate manifest with files required')
    files={}
    for row in manifest['files']:
        if not isinstance(row,dict) or set(row)!={'path','size','sha256'}:raise ToolchainRefused('malformed candidate file record')
        rel=_safe_path(row['path']);p=root.joinpath(*rel.split('/'))
        try:regular_chain(p)
        except Exception as e:raise ToolchainRefused(f'unsafe/missing candidate file {rel}') from e
        if not p.is_file():raise ToolchainRefused(f'candidate file missing: {rel}')
        data=p.read_bytes()
        if len(data)!=row['size'] or _sha(data)!=row['sha256']:raise ToolchainRefused(f'candidate hash mismatch: {rel}')
        files[rel]=data
    if len(files)!=len(manifest['files']):raise ToolchainRefused('duplicate candidate paths')
    canonical={'schema':1,'goal':manifest.get('goal'),'references':manifest.get('references'),'files':[{'path':p,'size':len(files[p]),'sha256':_sha(files[p])} for p in sorted(files)]}
    digest=_sha(_json(canonical))
    if manifest.get('candidate_sha256')!=digest:raise ToolchainRefused('candidate manifest digest mismatch')
    return files,digest

def _inventory(root:Path)->list[dict]:
    rows=[]
    for base,dirs,names in os.walk(root,followlinks=False):
        for n in dirs:
            if (Path(base)/n).is_symlink():raise ToolchainRefused('build output contains symlink')
        for n in names:
            p=Path(base)/n
            if p.is_symlink() or not p.is_file():raise ToolchainRefused('build output contains non-regular file')
            size=p.stat().st_size
            if size>MAX_FILE or len(rows)>=MAX_FILES:raise ToolchainRefused('build output exceeds inventory limits')
            rows.append({'path':p.relative_to(root).as_posix(),'size':size,'sha256':_sha(p.read_bytes())})
    return sorted(rows,key=lambda x:x['path'])

def _npm_comparator(version:str, requirement:str)->bool:
    import re
    actual=re.fullmatch(r'(\d+)(?:\.(\d+|x|X|\*))?(?:\.(\d+|x|X|\*))?(?:-([0-9A-Za-z.-]+))?',version)
    match=re.fullmatch(r'(\^|~|>=|>|<=|<|=)?(\d+|x|X|\*)(?:\.(\d+|x|X|\*))?(?:\.(\d+|x|X|\*))?(?:-([0-9A-Za-z.-]+))?',requirement)
    if not actual or not match:return version==requirement
    op,major,minor,patch,_pre=match.groups();got=tuple(int(x or 0) for x in actual.groups()[:3]); target=tuple(int(x) if x and x.lower() not in {'x','*'} else None for x in (major,minor,patch))
    count=sum(x is not None for x in target)
    floor=tuple(target[i] if target[i] is not None else 0 for i in range(3))
    if count<3:
        if op in ('>=','>'):return got>=floor if op=='>=' else got>floor
        if op in ('<=','<'):return got<=floor if op=='<=' else got<floor
        if op in ('^','~'):
            upper=(floor[0]+1,0,0) if floor[0] else ((0,floor[1]+1,0) if floor[1] else (0,0,floor[2]+1))
            return got>=floor and got<upper
        return all(target[i] is None or got[i]==target[i] for i in range(3))
    if op=='^':
        upper=(floor[0]+1,0,0) if floor[0] else ((0,floor[1]+1,0) if floor[1] else (0,0,floor[2]+1))
        return got>=floor and got<upper
    if op=='~':return got>=floor and got<(floor[0],floor[1]+1,0)
    if op=='>=':return got>=floor
    if op=='>':return got>floor
    if op=='<=':return got<=floor
    if op=='<':return got<floor
    return got==floor

def _npm_satisfies(version:str, requirement:str)->bool:
    return all(_npm_comparator(version,part) for part in requirement.split() if part)

def _npm_matches_any(version:str, requirement:str)->bool:
    return any(clause.strip() and all(_npm_comparator(version,part) for part in clause.strip().split()) for clause in requirement.split('||'))

def _verify_package_artifacts(manifest:dict,cache:Path,stage:Path)->None:
    eco=manifest['ecosystem'];artifacts=manifest.get('artifacts',[])
    if eco=='python':
        dest=stage/'wheelhouse';dest.mkdir()
        for a in artifacts:
            try:
                with zipfile.ZipFile(cache/a['artifact_sha256']) as z:
                    infos=z.infolist()
                    if len(infos)>MAX_FILES or sum(info.file_size for info in infos)>MAX_FILE*MAX_FILES:raise ValueError('wheel archive exceeds member/unpacked size limits')
                    metadata=[i for i in infos if i.filename.endswith('.dist-info/METADATA')]; wheels=[i for i in infos if i.filename.endswith('.dist-info/WHEEL')]
                    if len(metadata)!=1 or len(wheels)!=1 or metadata[0].file_size>MAX_FILE or wheels[0].file_size>MAX_FILE:raise ValueError('wheel metadata missing/oversized')
                    meta=Parser().parsestr(z.open(metadata[0]).read(MAX_FILE+1).decode()); tags=[x.split(':',1)[1].strip() for x in z.open(wheels[0]).read(MAX_FILE+1).decode().splitlines() if x.startswith('Tag:')]
                    if meta.get('Name','').casefold()!=a['name'].casefold() or meta.get('Version')!=a['version'] or not tags:raise ValueError('wheel identity mismatch')
                    if not re.fullmatch(r'[A-Za-z0-9_.-]+',meta.get('Name','')) or not re.fullmatch(r'[A-Za-z0-9_.+!]+',meta.get('Version','')) or not all(re.fullmatch(r'[A-Za-z0-9_.-]+',tag) for tag in tags):raise ValueError('unsafe wheel filename metadata')
                    filename=meta['Name'].replace('-','_')+'-'+meta['Version']+'-'+tags[0]+'.whl'; target=dest/filename
                    shutil.copyfile(cache/a['artifact_sha256'],target)
                    if _sha(target.read_bytes())!=a['artifact_sha256']:raise ValueError('wheel hash mismatch')
            except (OSError,ValueError,zipfile.BadZipFile,KeyError) as e:raise ToolchainRefused(f"cached artifact is not a matching wheel: {a.get('name')}") from e
        lock='\n'.join(f"{a['name']}=={a['version']} --hash=sha256:{a['artifact_sha256']}" for a in artifacts)+'\n';(stage/'requirements.lock').write_text(lock,encoding='utf8')
    elif eco in {'node','typescript'}:
        modules=stage/'node_modules';modules.mkdir()
        by_name={a['name'].casefold():a for a in artifacts}
        lock={p['name'].casefold():p for p in manifest.get('packages',[])}
        if len(lock)!=len(manifest.get('packages',[])) or set(lock)!=set(by_name):raise ToolchainRefused('npm package artifacts differ from transitive lock')
        for key,pkg in lock.items():
            if not isinstance(pkg['name'],str) or not re.fullmatch(r'(?:@[A-Za-z0-9._~-]+/)?[A-Za-z0-9._~-]+',pkg['name']) or not isinstance(pkg['version'],str) or not re.fullmatch(r'[A-Za-z0-9.+_-]+',pkg['version']):raise ToolchainRefused('unsafe npm package name/version')
            if key!=pkg['name'].casefold():raise ToolchainRefused('npm lock key mismatch')
            artifact=by_name[key]
            if artifact['version']!=pkg['version'] or artifact['artifact_sha256']!=pkg['artifact_sha256']:raise ToolchainRefused('npm artifact does not match exact lock identity')
            if not isinstance(pkg.get('dependencies',{}),dict):raise ToolchainRefused('npm dependency lock must be an exact map')
            for name,version in pkg.get('dependencies',{}).items():
                child=lock.get(name.casefold())
                if child is None or child['version']!=version:raise ToolchainRefused(f'unlocked npm transitive dependency: {name}')
            target=modules/pkg['name']
            try:
                with tarfile.open(cache/artifact['artifact_sha256'],'r:gz') as archive:
                    members=[];total=0
                    for member in archive:
                        if len(members)>=MAX_FILES:raise ValueError('too many npm archive members')
                        parts=Path(member.name).parts
                        if not parts or parts[0]!='package' or '..' in parts or Path(member.name).is_absolute() or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):raise ValueError('unsafe npm tar entry')
                        total+=member.size
                        if total>MAX_FILE*MAX_FILES or member.size>MAX_FILE:raise ValueError('npm archive exceeds unpacked size limits')
                        members.append(member)
                    target.mkdir(parents=True)
                    for member in members:
                        rel=Path(*Path(member.name).parts[1:])
                        if not rel.parts:continue
                        out=target/rel
                        if member.isdir():out.mkdir(parents=True,exist_ok=True)
                        else:
                            out.parent.mkdir(parents=True,exist_ok=True);src=archive.extractfile(member)
                            if src is None:raise ValueError('unreadable npm tar entry')
                            with src,out.open('xb') as f:shutil.copyfileobj(src,f,length=65536)
                    pkg_path=target/'package.json'
                    if pkg_path.stat().st_size>MAX_FILE:raise ValueError('npm metadata exceeds size limit')
                    package=json.loads(pkg_path.read_text(encoding='utf8'))
                    if package.get('name','').casefold()!=pkg['name'].casefold() or package.get('version')!=pkg['version']:raise ValueError('npm package metadata mismatch')
                    archive_dependencies=package.get('dependencies',{})
                    if not isinstance(archive_dependencies,dict) or any(not isinstance(name,str) or not isinstance(requirement,str) or name.casefold() not in lock or not _npm_matches_any(lock[name.casefold()]['version'],requirement) for name,requirement in archive_dependencies.items()):raise ValueError('npm dependency ranges do not match exact locked child versions')
                    for relation in ('optionalDependencies','peerDependencies'):
                        declared=package.get(relation,{})
                        if not isinstance(declared,dict) or any(not isinstance(name,str) or not isinstance(requirement,str) or name.casefold() not in lock or not _npm_matches_any(lock[name.casefold()]['version'],requirement) for name,requirement in declared.items()):raise ValueError(f'npm {relation} ranges do not match exact locked child versions')
                    bin_map=package.get('bin',{})
                    if isinstance(bin_map,str):
                        command=pkg['name'].rsplit('/',1)[-1]
                        bin_map={command:bin_map}
                    if not isinstance(bin_map,dict) or any(not isinstance(command,str) or not re.fullmatch(r'[A-Za-z0-9._-]+',command) or not isinstance(entry,str) or not entry or entry.startswith(('/','\\')) or '\\' in entry or any(part=='..' for part in entry.split('/')) for command,entry in bin_map.items()):
                        raise ValueError('unsafe npm executable metadata')
                    for command,entry in bin_map.items():
                        executable=target.joinpath(*(part for part in entry.split('/') if part!='.'))
                        if not executable.is_file():raise ValueError('npm executable target missing')
                        bin_dir=modules/'.bin';bin_dir.mkdir(exist_ok=True)
                        wrapper=bin_dir/command
                        if wrapper.exists():raise ValueError('duplicate npm executable name')
                        wrapper.write_text('#!/bin/sh\nexec node '+shlex.quote('/build/project/node_modules/'+pkg['name']+'/'+ '/'.join(part for part in entry.split('/') if part!='.'))+' "$@"\n',encoding='utf8',newline='\n')
                        wrapper.chmod(0o555)
            except (OSError,ValueError,tarfile.TarError,KeyError,json.JSONDecodeError) as e:raise ToolchainRefused(f"cached artifact is not a matching npm tgz: {pkg['name']}") from e
        for pkg in lock.values():
            parent=modules/pkg['name']
            for name,version in pkg.get('dependencies',{}).items():
                child=lock[name.casefold()]
                if child['version']!=version:raise ToolchainRefused(f'unlocked npm transitive dependency: {name}')
                nested=parent/'node_modules'/name
                nested.parent.mkdir(parents=True,exist_ok=True)
                if nested.exists() or nested.is_symlink():raise ToolchainRefused('npm archive pre-bundles a dependency path')
                shutil.copytree(modules/child['name'],nested,symlinks=False)
    elif eco=='java':
        jars=stage/'jars';jars.mkdir()
        for a in artifacts:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+',a['name']) or not re.fullmatch(r'[A-Za-z0-9_.+-]+',a['version']):raise ToolchainRefused('unsafe Java artifact name/version')
            try:
                with zipfile.ZipFile(cache/a['artifact_sha256']) as z:
                    infos=z.infolist()
                    if len(infos)>MAX_FILES or sum(info.file_size for info in infos)>MAX_FILE*MAX_FILES:raise ValueError('jar archive exceeds member/unpacked size limits')
                    if not any(info.filename.endswith('.class') for info in infos):raise ValueError('jar contains no classes')
                shutil.copyfile(cache/a['artifact_sha256'],jars/(a['name']+'-'+a['version']+'.jar'))
            except (OSError,ValueError,zipfile.BadZipFile) as e:raise ToolchainRefused(f"cached artifact is not a matching Java jar: {a['name']}") from e

def check(manifest:dict,candidate_root:Path|None,candidate_manifest:dict|None,cache:Path,engine:str,image:str)->dict:
    try:
        if not isinstance(manifest,dict) or manifest.get('schema')!=1:raise ToolchainRefused('validated environment manifest required')
        eco=manifest.get('ecosystem')
        if eco not in {'python','node','typescript','java'}:raise ToolchainRefused('unsupported ecosystem')
        if manifest.get('toolchain',{}).get('container_image')!=image:raise ToolchainRefused('requested image differs from validated manifest image')
        sources=manifest.get('source_allowlist');packages=manifest.get('packages');artifacts=manifest.get('artifacts')
        if not isinstance(sources,dict) or not isinstance(packages,list) or not isinstance(artifacts,list):raise ToolchainRefused('manifest source, package, and artifact inventories required')
        validated=dependencies.validate_lock({'schema':1,'ecosystem':eco,'toolchain':manifest['toolchain'],'packages':packages},sources=sources,candidate_mode=manifest.get('acquisition_mode','offline'))
        if validated['lock_sha256']!=manifest.get('lock_sha256'):raise ToolchainRefused('dependency lock digest mismatch')
        if candidate_root is not None:
            if candidate_manifest is None:raise ToolchainRefused('candidate manifest required')
            candidate_files,_=_candidate(candidate_root,candidate_manifest)
            if eco in {'node','typescript'}:
                package_data=candidate_files.get('package.json')
                if package_data is None:raise ToolchainRefused('Node candidate package.json required')
                package_json=json.loads(package_data)
                if not isinstance(package_json,dict):raise ToolchainRefused('candidate package.json must be an object')
                lock={p['name'].casefold():p for p in packages}
                roots={}
                for category in ('dependencies','devDependencies','optionalDependencies','peerDependencies'):
                    declared=package_json.get(category,{})
                    if not isinstance(declared,dict):raise ToolchainRefused(f'candidate {category} must be an exact locked map')
                    for name,requirement in declared.items():
                        if not isinstance(name,str) or not isinstance(requirement,str) or not name or not requirement:raise ToolchainRefused(f'candidate {category} entries must be nonempty strings')
                        key=name.casefold();pkg=lock.get(key)
                        if pkg is None or not _npm_matches_any(pkg['version'],requirement):raise ToolchainRefused(f'candidate {category} are not bound by the exact transitive lock: {name}')
                        prior=roots.get(key)
                        if prior is not None and prior!=pkg['version']:raise ToolchainRefused(f'candidate root dependency has conflicting requirements: {name}')
                        roots[key]=pkg['version']
                for key in ('bundledDependencies','bundleDependencies'):
                    if package_json.get(key):raise ToolchainRefused(f'candidate {key} requires bundled package acquisition support')
        cacheproof=dependencies.verify_cache(cache,artifacts,expected_sources=set(sources))
        if cacheproof['cache_sha256']!=manifest.get('cache_sha256'):raise ToolchainRefused('verified cache differs from manifest')
        identity=lambda rows:sorted((r['name'].casefold(),r['version'],r['source'],r['artifact_sha256'],r['license']) for r in rows)
        if identity(packages)!=identity(artifacts):raise ToolchainRefused('locked package identities differ from cached artifact identities')
        info=builder.preflight(engine,image)
        return {'status':'READY','engine':engine,'image_id':info['image_id'],'server_os':info['server_os'],'candidate_checked':candidate_root is not None,'candidate_executed':False,'network':'none','ecosystem':eco}
    except (ToolchainRefused,dependencies.DependencyRefused,builder.BuildFailure,builder.MissingPrerequisite,OSError,ValueError,KeyError) as e:return {'status':'BLOCKED','blockers':[str(e)],'candidate_executed':False}

def run(manifest:dict,candidate_manifest:dict,cache:Path,engine:str,output:Path,*,candidate_root:Path,build_argv:list[str],image:str|None=None,repeat:bool=False,timeout:int=300,memory:str='1g',cpus:str='1',pids:int=128)->dict:
    if not isinstance(build_argv,list) or not build_argv or any(not isinstance(x,str) or not x for x in build_argv):raise ToolchainRefused('exact declared build argv required')
    if timeout<1 or timeout>3600 or pids<1 or pids>1024:raise ToolchainRefused('resource limits outside permitted bounds')
    expected_image=manifest.get('toolchain',{}).get('container_image','')
    if image is not None and image!=expected_image:raise ToolchainRefused('requested image differs from validated manifest image')
    readiness=check(manifest,candidate_root,candidate_manifest,cache,engine,expected_image)
    if readiness['status']!='READY':raise ToolchainRefused('; '.join(readiness['blockers']))
    files,digest=_candidate(candidate_root,candidate_manifest);cache=Path(cache).absolute();output=Path(output).absolute();regular_chain(output)
    if output.exists():raise ToolchainRefused('output directory must be absent')
    runs=[]
    try:
        for index in range(2 if repeat else 1):
            name='autoforge-toolchain-'+uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix='autoforge-stage-') as td:
                stage=Path(td);cand=stage/'candidate';cand.mkdir()
                for rel,data in files.items():
                    p=cand.joinpath(*rel.split('/'));p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
                for p in cand.rglob('*'):
                    if p.is_file():p.chmod(0o444)
                _verify_package_artifacts(manifest,cache,stage)
                eco=manifest['ecosystem'];image=expected_image
                mounts=['--mount',f'type=bind,src={cand},dst=/autoforge,readonly']
                if eco=='python':mounts += ['--mount',f'type=bind,src={stage}/wheelhouse,dst=/wheelhouse,readonly','--mount',f'type=bind,src={stage}/requirements.lock,dst=/autoforge-lock/requirements.lock,readonly']
                elif eco in {'node','typescript'}:mounts += ['--mount',f'type=bind,src={stage}/node_modules,dst=/autoforge-deps/node_modules,readonly']
                else:mounts += ['--mount',f'type=bind,src={stage}/jars,dst=/autoforge-deps/jars,readonly']
                cmd=[engine,'run','--rm','--name',name,'--network=none','--read-only','--user=65534:65534','--cap-drop=ALL','--security-opt=no-new-privileges',f'--pids-limit={pids}',f'--memory={memory}',f'--cpus={cpus}',*mounts,'--tmpfs','/tmp:rw,nosuid,nodev,size=512m,mode=1777','--tmpfs','/build:rw,exec,nosuid,nodev,size=2g,mode=1777','--tmpfs','/build/result:rw,nosuid,nodev,size=256m,mode=1777','--workdir','/build','--entrypoint','/bin/sh',image,'-c','']
                if eco=='python':setup='mkdir -p /build/project /build/home /build/result; cp -r /autoforge/. /build/project/; chmod -R u+rwX /build/project; export HOME=/build/home PYTHONPATH=/build/site-packages PYTHONDONTWRITEBYTECODE=1; python --version; python -m pip --version; '+(shlex.join(['python','-m','pip','install','--no-index','--require-hashes','--find-links=/wheelhouse','-r','/autoforge-lock/requirements.lock','--target','/build/site-packages']) if manifest.get('artifacts') else 'true')+'; cd /build/project; '+shlex.join(build_argv)+'; cp -r /build/project/. /build/result/'
                elif eco in {'node','typescript'}:setup='mkdir -p /build/project /build/home /build/result /build/project/node_modules/.bin; cp -r /autoforge/. /build/project/; chmod -R u+rwX /build/project; cp -r /autoforge-deps/node_modules/. /build/project/node_modules/; chmod -R u+rwX /build/project/node_modules; chmod -R a+rx /build/project/node_modules/.bin; if [ -f /build/project/node_modules/preact/package.json ]; then cp /build/project/node_modules/preact/dist/preact.umd.js /build/project/preact.umd.js; fi; export HOME=/build/home npm_config_offline=true npm_config_ignore_scripts=true npm_config_audit=false npm_config_fund=false; export PATH=/build/project/node_modules/.bin:$PATH; node --version; npm --version; cd /build/project; '+shlex.join(build_argv)+'; rm -rf node_modules; cp -r /build/project/. /build/result/'
                else:setup='mkdir -p /build/project /build/home /build/result; cp -r /autoforge/. /build/project/; chmod -R u+rwX /build/project; if [ -n "${JAVA_HOME:-}" ]; then export PATH="$JAVA_HOME/bin:$PATH"; fi; export HOME=/build/home CLASSPATH="/build/project:/autoforge-deps/jars/*"; java -version; javac -version; cd /build/project; '+shlex.join(build_argv)+'; if [ -f Main.class ]; then java -cp ".:/autoforge-deps/jars/*" Main; fi; cp -r /build/project/. /build/result/'
                cmd[-1]='set -eu; ( '+setup+' ) >&2; tar -C /build/result -cf - .'
                try:proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={'PATH':os.environ.get('PATH','')},close_fds=True,bufsize=0)
                except Exception:
                    try:subprocess.run([engine,'rm','-f',name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)
                    except (OSError,subprocess.TimeoutExpired):pass
                    raise
                started=time.monotonic()
                stdout=bytearray();stderr=bytearray();overflow={'archive':False,'log':False};drain_errors=[]
                def drain(stream,target,cap,key):
                    try:
                        while True:
                            chunk=stream.read(65536)
                            if not chunk:break
                            if len(target)+len(chunk)>cap:overflow[key]=True
                            if len(target)<cap:target.extend(chunk[:cap-len(target)])
                    except BaseException as exc:drain_errors.append(exc)
                import threading
                threads=[threading.Thread(target=drain,args=(proc.stdout,stdout,MAX_TOTAL_OUTPUT,'archive'),daemon=True),threading.Thread(target=drain,args=(proc.stderr,stderr,MAX_LOG,'log'),daemon=True)]
                try:
                    for thread in threads:thread.start()
                except BaseException:
                    try:proc.kill();proc.wait(timeout=5)
                    except (OSError,subprocess.TimeoutExpired):pass
                    try:proc.stdout.close();proc.stderr.close()
                    except OSError:pass
                    try:subprocess.run([engine,'rm','-f',name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)
                    except (OSError,subprocess.TimeoutExpired):pass
                    raise
                timed_out=False
                try:
                    deadline=time.monotonic()+timeout
                    while proc.poll() is None and time.monotonic()<deadline and not any(overflow.values()):time.sleep(.05)
                    timed_out=proc.poll() is None and not any(overflow.values())
                    if timed_out or any(overflow.values()):proc.kill()
                    proc.wait(timeout=5)
                finally:
                    try:
                        if proc.poll() is None:proc.kill();proc.wait(timeout=5)
                    except (OSError,subprocess.TimeoutExpired):pass
                    finally:
                        for thread in threads:thread.join(timeout=2)
                        proc.stdout.close();proc.stderr.close()
                        try:subprocess.run([engine,'kill',name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=False)
                        except (OSError,subprocess.TimeoutExpired):pass
                        try:subprocess.run([engine,'rm','-f',name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)
                        except (OSError,subprocess.TimeoutExpired):pass
                if timed_out:raise ToolchainRefused('container execution timed out')
                if overflow['archive']:raise ToolchainRefused('container output archive cap exceeded')
                if overflow['log']:raise ToolchainRefused('container log cap exceeded')
                if drain_errors:raise ToolchainRefused(f'container stream read failed: {type(drain_errors[0]).__name__}')
                if proc.returncode:raise ToolchainRefused(f'container exited {proc.returncode}: {stderr.decode(errors="replace")[:2000]}')
                extract=stage/'extract';extract.mkdir()
                try:
                    with tarfile.open(fileobj=__import__('io').BytesIO(stdout),mode='r:') as archive:
                        members=archive.getmembers()
                        if len(members)>MAX_FILES:raise ValueError('too many output files')
                        total=0;seen_paths=set()
                        for member in members:
                            raw=member.name
                            while raw.startswith('./'):raw=raw[2:]
                            if member.isdir():raw=raw[:-1] if raw.endswith('/') else raw
                            if raw in ('','.'):continue
                            try:rel=applications._validate_path(raw)
                            except Exception as exc:raise ValueError('unsafe or nonportable output path') from exc
                            folded=rel.casefold()
                            if not member.isdir() and (folded in seen_paths or any(folded.startswith(path+'/') or path.startswith(folded+'/') for path in seen_paths)):raise ValueError('colliding output archive paths')
                            if not member.isdir():seen_paths.add(folded)
                            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):raise ValueError('unsafe output archive entry')
                            total+=member.size
                            if total>MAX_TOTAL_OUTPUT or member.size>MAX_FILE:raise ValueError('output exceeds byte limits')
                            dest=extract.joinpath(*rel.split('/'))
                            if member.isdir():dest.mkdir(parents=True,exist_ok=True)
                            else:
                                dest.parent.mkdir(parents=True,exist_ok=True);src=archive.extractfile(member)
                                if src is None:raise ValueError('unreadable output file')
                                with src,dest.open('xb') as f:shutil.copyfileobj(src,f)
                except (OSError,ValueError,tarfile.TarError) as exc:raise ToolchainRefused(f'container output rejected: {exc}') from exc
                inventory=_inventory(extract);invsha=_sha(_json(inventory));runs.append({'elapsed_seconds':round(time.monotonic()-started,3),'inventory':inventory,'inventory_sha256':invsha,'observed_versions':stderr.decode(errors='replace')[:2000]})
                if not output.exists():output.mkdir(parents=True)
                for row in inventory:
                    src=extract/row['path'];dst=output/row['path'];dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
        deterministic=not repeat or runs[0]['inventory_sha256']==runs[1]['inventory_sha256']
        return {'status':'PASS' if deterministic else 'NONDETERMINISTIC','candidate_sha256':digest,'runs':runs,'determinism_checked':repeat,'deterministic':deterministic if repeat else None}
    except Exception:shutil.rmtree(output,ignore_errors=True);raise

def main()->int:
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='action',required=True)
    for action in ('check','execute'):
        q=s.add_parser(action);q.add_argument('--manifest',type=Path,required=True);q.add_argument('--cache',type=Path,required=True);q.add_argument('--engine',choices=('docker','podman'),default='docker');q.add_argument('--image',required=True);q.add_argument('--candidate-root',type=Path);q.add_argument('--candidate-manifest',type=Path);q.add_argument('--output',type=Path);q.add_argument('--build-argv',help='JSON argv array');q.add_argument('--repeat',action='store_true');q.add_argument('--timeout',type=int,default=300)
    a=p.parse_args()
    try:
        manifest=json.loads(a.manifest.read_text(encoding='utf-8'));cm=json.loads(a.candidate_manifest.read_text(encoding='utf-8')) if a.candidate_manifest else None
        if a.action=='check':result=check(manifest,a.candidate_root,cm,a.cache,a.engine,a.image)
        else:
            if a.candidate_root is None or cm is None or a.output is None or not a.build_argv:raise ToolchainRefused('execute requires candidate root/manifest, output and exact --build-argv JSON')
            result=run(manifest,cm,a.cache,a.engine,a.output,candidate_root=a.candidate_root,image=a.image,build_argv=json.loads(a.build_argv),repeat=a.repeat,timeout=a.timeout)
        print(json.dumps(result,sort_keys=True));return 0 if result['status'] in {'READY','PASS'} else 2
    except (OSError,ValueError,KeyError,ToolchainRefused,dependencies.DependencyRefused) as e:print(json.dumps({'status':'BLOCKED','reason':str(e)}));return 2
if __name__=='__main__':raise SystemExit(main())
