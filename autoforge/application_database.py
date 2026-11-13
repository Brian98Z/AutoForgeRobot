#!/usr/bin/env python3
"""Dedicated disposable test-database lifecycle tools; never reads external credentials."""
from __future__ import annotations
import hashlib, json, os, re, sqlite3, subprocess, tempfile, time, uuid
from pathlib import Path
from typing import Any
import application_builder
import container_env

class DatabaseFailure(ValueError): pass
def _bounded_command(argv:list[str],data:bytes|None=None,timeout:int=60,limit:int=1048576,env:dict[str,str]|None=None)->tuple[int,bytes,bytes]:
    with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
        try: proc=subprocess.Popen(argv,stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,stdout=out,stderr=err,env=env or container_env.engine_env(),close_fds=True)
        except OSError as exc: raise DatabaseFailure(f'command unavailable: {argv[0]}') from exc
        try:
            if data is not None:
                assert proc.stdin is not None; proc.stdin.write(data); proc.stdin.close()
            deadline=time.monotonic()+timeout
            while proc.poll() is None and time.monotonic()<deadline:
                if out.tell()>limit or err.tell()>limit: proc.kill(); raise DatabaseFailure('command output exceeded limit')
                time.sleep(.05)
            if proc.poll() is None: proc.kill(); raise DatabaseFailure('command timed out')
            if out.tell()>limit or err.tell()>limit: raise DatabaseFailure('command output exceeded limit')
            out.seek(0); err.seek(0); return proc.returncode,out.read(limit),err.read(limit)
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()

def _migration_statements(script:str)->list[str]:
    statements=[]; current=''
    for char in script:
        current+=char
        if char==';' and sqlite3.complete_statement(current):
            statement=current.strip(); current=''
            if statement: statements.append(statement)
    if current.strip():
        if not sqlite3.complete_statement(current): raise DatabaseFailure('migration contains an incomplete SQL statement')
        statements.append(current.strip())
    if not statements: raise DatabaseFailure('migration contains no SQL statements')
    return statements

def _migration_authorizer(action:int,arg1:str|None,arg2:str|None,db:str|None,trigger:str|None)->int:
    if action in {sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH,sqlite3.SQLITE_TRANSACTION,sqlite3.SQLITE_SAVEPOINT,sqlite3.SQLITE_ALTER_TABLE}: return sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_PRAGMA and str(arg1).lower() in {'writable_schema','journal_mode','locking_mode'}: return sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_FUNCTION and str(arg2 or arg1).lower() in {'load_extension','writefile'}: return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

def _exec_migration(con:sqlite3.Connection,script:str,expected_version:int|None=None,target_version:int|None=None)->None:
    statements=_migration_statements(script)
    con.execute('BEGIN IMMEDIATE')
    con.set_authorizer(_migration_authorizer)
    try:
        if expected_version is not None:
            actual=con.execute('PRAGMA user_version').fetchone()[0]
            if actual!=expected_version:raise DatabaseFailure(f'incompatible schema version: expected {expected_version}, observed {actual}')
        for statement in statements: con.execute(statement)
    except Exception:
        con.set_authorizer(None)
        if con.in_transaction: con.rollback()
        raise
    con.set_authorizer(None)
    if target_version is not None:con.execute(f'PRAGMA user_version={target_version}')
    con.commit()

def sqlite_self_check(db_path:Path)->dict[str,Any]:
    """Exercise isolated SQLite constraints, transaction rollback, concurrency, backup and restore."""
    if db_path.exists(): raise DatabaseFailure('self-check target must not already exist')
    db_path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(db_path,timeout=3,isolation_level=None,check_same_thread=False); con.execute('PRAGMA journal_mode=WAL'); con.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, value INTEGER NOT NULL UNIQUE)'); con.execute('INSERT INTO records(value) VALUES(10)')
    try:
        try: con.execute('INSERT INTO records(value) VALUES(10)')
        except sqlite3.IntegrityError: pass
        else: raise DatabaseFailure('unique constraint did not reject duplicate')
        try:
            con.execute('BEGIN IMMEDIATE'); con.execute('INSERT INTO records(value) VALUES(20)'); con.rollback()
        except Exception:
            if con.in_transaction: con.rollback()
            raise
        import concurrent.futures
        def update(value:int)->None:
            c=sqlite3.connect(db_path,timeout=5); c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE records SET value=? WHERE id=1',(value,)); c.commit(); c.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(update,[11,12,13,14]))
        if con.execute('SELECT count(*) FROM records').fetchone()[0]!=1: raise DatabaseFailure('concurrent update invariant failed')
        expected=con.execute('SELECT value FROM records WHERE id=1').fetchone()[0]
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        backup=db_path.with_suffix('.backup.sqlite'); b=sqlite3.connect(backup); con.backup(b); b.close()
        _exec_migration(con,'CREATE TABLE schema_version(version INTEGER NOT NULL); INSERT INTO schema_version VALUES (2);')
        con.close(); con=sqlite3.connect(db_path); persisted=con.execute('SELECT value FROM records WHERE id=1').fetchone()[0]
        if persisted!=expected: raise DatabaseFailure('reopen persistence check failed')
        try: _exec_migration(con,'INSERT INTO records(value) VALUES (21); SELECT * FROM missing_table;')
        except sqlite3.Error as failure:
            if con.in_transaction: con.rollback()
            con.close(); con=None
            restored=sqlite3.connect(db_path); source=sqlite3.connect(backup); source.backup(restored); restored.close(); source.close()
            verify=sqlite3.connect(db_path); count=verify.execute('SELECT count(*) FROM records').fetchone()[0]; verify.close()
            if count!=1: raise DatabaseFailure('backup restore did not preserve test row') from failure
        else: raise DatabaseFailure('deliberately invalid migration unexpectedly succeeded')
    finally:
        if con: con.close()
    try: sqlite_lifecycle(db_path,'INSERT INTO records(value) VALUES (21); SELECT * FROM missing_table;')
    except DatabaseFailure: pass
    else: raise DatabaseFailure('invalid lifecycle migration unexpectedly succeeded')
    verify=sqlite3.connect(db_path)
    try:
        if verify.execute('SELECT value FROM records WHERE id=1').fetchone()!=(expected,) or verify.execute('SELECT count(*) FROM records').fetchone()!=(1,):raise DatabaseFailure('lifecycle failure changed pre-migration records')
    finally: verify.close()
    return {'schema':1,'backend':'sqlite','status':'PASS','checks':['unique constraint','transaction rollback','concurrent serialized updates','database backup','reopen persistence','failed migration rollback/backup restore'],'database':str(db_path),'backup':str(db_path.with_suffix('.backup.sqlite'))}

def sqlite_lifecycle(db_path:Path, migration_sql:str, *, busy_timeout_ms:int=3000,expected_version:int|None=None,target_version:int|None=None)->dict[str,Any]:
    """Operate only on an explicitly named dedicated test DB; migration failure restores backup."""
    if not isinstance(db_path,Path) or db_path.exists() and (db_path.is_symlink() or not db_path.is_file()): raise DatabaseFailure('database target must be absent or a regular dedicated test file')
    if not isinstance(migration_sql,str) or not migration_sql.strip() or len(migration_sql.encode())>262144: raise DatabaseFailure('migration SQL must be nonempty and bounded')
    _migration_statements(migration_sql)
    if (expected_version is None)!=(target_version is None):raise DatabaseFailure('expected_version and target_version must be supplied together')
    if expected_version is not None and (type(expected_version)is not int or type(target_version)is not int or not 0<=expected_version<target_version<=2147483647):raise DatabaseFailure('schema versions must be increasing nonnegative 31-bit integers')
    db_path.parent.mkdir(parents=True,exist_ok=True)
    backup=db_path.with_name(db_path.name+'.pre-migration-'+uuid.uuid4().hex+'.sqlite')
    existed=db_path.exists(); con=None
    try:
        if existed:
            src=sqlite3.connect(f'file:{db_path.resolve()}?mode=ro',uri=True,timeout=3)
            dst=sqlite3.connect(backup); src.backup(dst); dst.close(); src.close()
        con=sqlite3.connect(db_path,timeout=busy_timeout_ms/1000,isolation_level=None)
        con.execute(f'PRAGMA busy_timeout={busy_timeout_ms}')
        con.execute('PRAGMA foreign_keys=ON')
        _exec_migration(con,migration_sql,expected_version,target_version)
        integrity=con.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity!='ok': raise DatabaseFailure('SQLite integrity check failed')
        con.close(); con=None
        # Reopen checks persisted schema access, not an actual process restart.
        check=sqlite3.connect(f'file:{db_path.resolve()}?mode=ro',uri=True); check.execute('PRAGMA schema_version').fetchone(); check.close()
        return {'schema':1,'backend':'sqlite','status':'PASS','backup':str(backup) if existed else None,'database_sha256':hashlib.sha256(db_path.read_bytes()).hexdigest(),'integrity':'ok','limitations':['Host SQLite lifecycle only; it does not exercise generated code or a container.','Failed migrations restore the prior database; no compensating SQL runs after restoration.']}
    except Exception as exc:
        if con is not None:
            try: con.close()
            except sqlite3.Error: pass
        if existed and backup.exists():
            restore=sqlite3.connect(db_path); source=sqlite3.connect(f'file:{backup.resolve()}?mode=ro',uri=True); source.backup(restore); restore.close(); source.close()
        elif not existed:
            try: db_path.unlink()
            except FileNotFoundError: pass
        raise DatabaseFailure(f'migration failed and prior test database restored: {exc}') from exc

def postgres_preflight(engine:str,image:str)->dict[str,str]:
    return application_builder.preflight(engine,image)
def postgres_migrate(handle:dict[str,Any],migration_sql:str,*,expected_version:int,target_version:int,timeout:int=45)->dict[str,Any]:
    """Apply operator-authored SQL and version in one serialized test-DB transaction."""
    if type(expected_version)is not int or type(target_version)is not int or not 0<=expected_version<target_version<=2147483647:raise DatabaseFailure('schema versions must be increasing nonnegative 31-bit integers')
    if not isinstance(handle,dict) or handle.get('status')!='READY' or not all(isinstance(handle.get(k),str) and handle[k] for k in ('engine','container','username','database')):raise DatabaseFailure('invalid PostgreSQL lifecycle handle')
    if type(timeout)is not int or not 3<=timeout<=180:raise DatabaseFailure('invalid migration timeout')
    if not isinstance(migration_sql,str) or not migration_sql.strip() or len(migration_sql.encode())>262144:raise DatabaseFailure('bounded migration SQL required')
    if any(line.lstrip().startswith('\\') for line in migration_sql.splitlines()) or re.search(r'(?im)^\s*(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b',migration_sql):raise DatabaseFailure('transaction control and psql meta-commands forbidden')
    script=("BEGIN;\nSELECT pg_advisory_xact_lock(1096111183);\n"
            "CREATE TABLE IF NOT EXISTS public.autoforge_schema_version (singleton boolean PRIMARY KEY CHECK(singleton), version integer NOT NULL);\n"
            "INSERT INTO public.autoforge_schema_version VALUES(true,0) ON CONFLICT DO NOTHING;\n"
            f"DO $autoforge$ BEGIN IF (SELECT version FROM public.autoforge_schema_version WHERE singleton) <> {expected_version} THEN RAISE EXCEPTION 'incompatible schema version'; END IF; END $autoforge$;\n"
            +migration_sql+f"\n;UPDATE public.autoforge_schema_version SET version={target_version} WHERE singleton;\nCOMMIT;\n")
    code,out,err=_bounded_command([handle['engine'],'exec','-i',handle['container'],'psql','-X','-v','ON_ERROR_STOP=1','-U',handle['username'],'-d',handle['database']],script.encode(),timeout=timeout,limit=65536)
    if code:raise DatabaseFailure('versioned PostgreSQL migration rejected: '+err[:2048].decode('utf-8','replace'))
    return {'schema':1,'backend':'postgresql','status':'PASS','previous_version':expected_version,'version':target_version,'migration_sha256':hashlib.sha256(migration_sql.encode()).hexdigest(),'scope':'operator-authored SQL in dedicated test database; transaction rollback on failure'}

def postgres_accept(engine:str,handle:dict[str,Any],migration_sql:str,rollback_sql:str,timeout:int=45)->dict[str,Any]:
    """Apply caller migrations and run isolated unique, rollback, concurrency, backup, restart checks."""
    if type(timeout) is not int or not 3<=timeout<=180: raise DatabaseFailure('invalid PostgreSQL acceptance timeout')
    if not isinstance(handle,dict) or handle.get('status')!='READY' or not all(isinstance(handle.get(k),str) for k in ('engine','container','username','database','password')): raise DatabaseFailure('invalid PostgreSQL lifecycle handle')
    engine=handle['engine']; cid=handle['container']; user=handle['username']; database=handle['database']; password=handle['password']
    def psql(sql:str,limit:int=timeout,ok_fail:bool=False)->str:
        if len(sql.encode())>262144 or any(line.lstrip().startswith('\\') for line in sql.splitlines()): raise DatabaseFailure('bounded SQL only; psql meta-commands forbidden')
        code,out,err=_bounded_command([engine,'exec','-i','-e','PGPASSWORD='+password,cid,'psql','-X','-v','ON_ERROR_STOP=1','-U',user,'-d',database],sql.encode(),timeout=limit,limit=65536)
        if code and not ok_fail: raise DatabaseFailure('PostgreSQL operation failed: '+err[:2048].decode('utf-8','replace'))
        return (out+err)[:65536].decode('utf-8','replace')
    if not isinstance(migration_sql,str) or not migration_sql.strip() or len(migration_sql.encode())>262144 or not isinstance(rollback_sql,str) or not rollback_sql.strip() or len(rollback_sql.encode())>262144: raise DatabaseFailure('bounded migration and rollback SQL are required')
    if any(line.lstrip().startswith('\\') for line in migration_sql.splitlines()+rollback_sql.splitlines()) or re.search(r'(?im)^\s*(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b',migration_sql+'\n'+rollback_sql): raise DatabaseFailure('transaction control and psql meta-commands forbidden in migration scripts')
    backup_code,pre_migration_backup,backup_err=_bounded_command([engine,'exec','-e','PGPASSWORD='+password,cid,'pg_dump','-Fc','-U',user,'-d',database],timeout=timeout,limit=16777216)
    if backup_code: raise DatabaseFailure('pre-migration pg_dump failed: '+backup_err[:2048].decode('utf-8','replace'))
    try: psql('BEGIN;\n'+migration_sql+'\nCOMMIT;')
    except DatabaseFailure as migration_error:
        restore_code,_,restore_err=_bounded_command([engine,'exec','-i','-e','PGPASSWORD='+password,cid,'pg_restore','--clean','--if-exists','-U',user,'-d',database],pre_migration_backup,timeout=timeout,limit=65536)
        if restore_code: raise DatabaseFailure('migration failed and pre-migration database backup restore failed: '+restore_err[:2048].decode('utf-8','replace')) from migration_error
        raise DatabaseFailure('migration failed; pre-migration database backup restored') from migration_error
    psql('CREATE TABLE IF NOT EXISTS autoforge_lifecycle_probe (id integer PRIMARY KEY, value integer NOT NULL UNIQUE); INSERT INTO autoforge_lifecycle_probe VALUES (1,10) ON CONFLICT (id) DO UPDATE SET value=EXCLUDED.value;')
    duplicate=psql('INSERT INTO autoforge_lifecycle_probe VALUES (2,10);',ok_fail=True)
    if 'duplicate key value violates unique constraint' not in duplicate: raise DatabaseFailure('PostgreSQL unique constraint was not enforced')
    psql('BEGIN; INSERT INTO autoforge_lifecycle_probe VALUES (3,30); ROLLBACK;')
    rollback_result=psql("SELECT count(*) FROM autoforge_lifecycle_probe WHERE id=3;")
    if not re.search(r'(?m)^\s*0\s*$',rollback_result): raise DatabaseFailure('PostgreSQL rollback did not restore row count')
    psql('CREATE TABLE autoforge_request_probe (request_id text PRIMARY KEY); CREATE TABLE autoforge_effect_probe (id integer PRIMARY KEY,value integer NOT NULL); INSERT INTO autoforge_effect_probe VALUES(1,0);')
    replay_sql="WITH accepted AS (INSERT INTO autoforge_request_probe VALUES ('request-1') ON CONFLICT DO NOTHING RETURNING request_id) UPDATE autoforge_effect_probe SET value=value+1 WHERE id=1 AND EXISTS(SELECT 1 FROM accepted);"
    psql(replay_sql);psql(replay_sql)
    if not re.search(r'(?m)^\s*1\s*$',psql('SELECT value FROM autoforge_effect_probe WHERE id=1;')):raise DatabaseFailure('duplicate database request applied more than once')
    psql('CREATE TABLE IF NOT EXISTS autoforge_lifecycle_backup (id integer PRIMARY KEY,value integer); INSERT INTO autoforge_lifecycle_backup VALUES (1,10) ON CONFLICT (id) DO NOTHING;')
    backup_code,backup_data,backup_err=_bounded_command([engine,'exec','-e','PGPASSWORD='+password,cid,'pg_dump','-Fc','-U',user,'-d',database],timeout=timeout,limit=16777216)
    if backup_code: raise DatabaseFailure('pg_dump backup failed: '+backup_err[:2048].decode('utf-8','replace'))
    restore_code,_,restore_err=_bounded_command([engine,'exec','-i','-e','PGPASSWORD='+password,cid,'pg_restore','--clean','--if-exists','-U',user,'-d',database],backup_data,timeout=timeout,limit=65536)
    if restore_code: raise DatabaseFailure('pg_restore backup recovery failed: '+restore_err[:2048].decode('utf-8','replace'))
    subprocess.run([engine,'restart','--time','10',cid],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=15,check=True,env=container_env.engine_env(),close_fds=True)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        ready=subprocess.run([engine,'exec',cid,'pg_isready','-U',user,'-d',database],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=5,check=False,env=container_env.engine_env(),close_fds=True)
        if b'accepting connections' in ready.stdout: break
        time.sleep(.3)
    else: raise DatabaseFailure('PostgreSQL did not become ready after container restart')
    persisted=psql('SELECT value FROM autoforge_lifecycle_probe WHERE id=1;')
    if not re.search(r'(?m)^\s*10\s*$',persisted): raise DatabaseFailure('PostgreSQL restart persistence check failed')
    psql(replay_sql)
    if not re.search(r'(?m)^\s*1\s*$',psql('SELECT value FROM autoforge_effect_probe WHERE id=1;')):raise DatabaseFailure('request replay after restart applied duplicate effect')
    def update()->None: psql('BEGIN; UPDATE autoforge_lifecycle_probe SET value=value+1 WHERE id=1; COMMIT;')
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(lambda _:update(),range(2)))
    final=psql('SELECT value FROM autoforge_lifecycle_probe WHERE id=1;')
    if not re.search(r'(?m)^\s*12\s*$',final): raise DatabaseFailure('PostgreSQL concurrent update check failed')
    psql('BEGIN;\n'+rollback_sql+'\nCOMMIT;')
    return {'schema':1,'backend':'postgresql','status':'PASS','checks':['schema migration','unique constraint','transaction rollback','concurrent serialized updates','database request replay applies one effect, including after restart','pg_dump backup/pg_restore recovery','volume-backed container restart persistence','rollback migration'],'limitations':['Storage uses a dedicated disposable named container volume deleted at cleanup; durable production backup retention is not claimed.','Replay probe checks a database transaction pattern, not an application HTTP idempotency implementation.']}

def postgres_start(engine:str,image:str,timeout:int=30)->dict[str,Any]:
    """Start isolated ephemeral PostgreSQL with generated disposable credentials; no host mounts."""
    if type(timeout) is not int or not 3<=timeout<=120: raise DatabaseFailure('timeout must be 3..120 seconds')
    info=postgres_preflight(engine,image); suffix=uuid.uuid4().hex; network='autoforge-db-net-'+suffix; name='autoforge-db-'+suffix; volume='autoforge-db-vol-'+suffix
    secret=uuid.uuid4().hex+uuid.uuid4().hex
    def run(argv:list[str],limit:int=15)->bytes:
        code,out,err=_bounded_command(argv,timeout=limit,limit=65536)
        if code: raise DatabaseFailure(f'engine command failed: {err[:2048].decode("utf-8","replace")}')
        return out
    network_created=False; volume_created=False; container_created=False
    try:
        run([engine,'network','create','--internal',network]); network_created=True
        run([engine,'volume','create',volume]); volume_created=True
        cid=run([engine,'run','-d','--name',name,'--network',network,'--user=postgres','--read-only','--mount',f'type=volume,src={volume},dst=/var/lib/postgresql/data','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','--memory=512m','--cpus=1','--tmpfs','/var/run/postgresql:rw,nosuid,nodev,size=8m,mode=1777','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m','-e','POSTGRES_PASSWORD='+secret,'-e','POSTGRES_DB=autoforge_test','-e','POSTGRES_USER=autoforge_test','--entrypoint','docker-entrypoint.sh',image,'postgres','-c','listen_addresses=localhost']).decode().strip(); container_created=True
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            code,out,err=_bounded_command([engine,'exec',cid,'sh','-c','test "$(head -n 1 /proc/1/comm)" = postgres && pg_isready -U autoforge_test -d autoforge_test'],timeout=5,limit=65536)
            if code==0 and b'accepting connections' in out:return {'schema':1,'backend':'postgresql','status':'READY','engine':engine,'image_id':info['image_id'],'container':name,'network':network,'volume':volume,'database':'autoforge_test','username':'autoforge_test','password':secret,'limitations':['Dedicated disposable named Docker volume; temporary test data is removed by postgres_stop.','Database uses container-local exec only; no host port is published.']}
            time.sleep(.3)
        raise DatabaseFailure('PostgreSQL readiness timeout')
    except Exception:
        if container_created:
            try: run([engine,'rm','-f',name],10)
            except Exception: pass
        if volume_created:
            try: run([engine,'volume','rm',volume],10)
            except Exception: pass
        if network_created:
            try: run([engine,'network','rm',network],10)
            except Exception: pass
        raise

def postgres_stop(engine:str,container:str,network:str|None=None,volume:str|None=None)->None:
    if not isinstance(container,str) or not re.fullmatch(r'autoforge-db-[0-9a-f]{32}',container): raise DatabaseFailure('refusing non-AutoForge database container identity')
    subprocess.run([engine,'rm','-f',container],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False,env=container_env.engine_env(),close_fds=True)
    if volume is not None and re.fullmatch(r'autoforge-db-vol-[0-9a-f]{32}',volume): subprocess.run([engine,'volume','rm',volume],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False,env=container_env.engine_env(),close_fds=True)
    if network is not None and re.fullmatch(r'autoforge-db-net-[0-9a-f]{32}',network): subprocess.run([engine,'network','rm',network],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False,env=container_env.engine_env(),close_fds=True)
