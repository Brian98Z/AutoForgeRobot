#!/usr/bin/env python3
"""Check database lifecycle API; real-container acceptance is explicitly separate."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse,json,sqlite3,sys
from pathlib import Path
import application_builder, application_database as db
def main()->int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--sqlite-test-db',type=Path); p.add_argument('--migration',type=Path); p.add_argument('--rollback',type=Path); p.add_argument('--self-check-db',type=Path); p.add_argument('--engine',choices=('docker','podman')); p.add_argument('--image'); p.add_argument('--expected-version',type=int); p.add_argument('--target-version',type=int); a=p.parse_args()
    versioned=a.expected_version is not None or a.target_version is not None
    if versioned and ((a.expected_version is None or a.target_version is None) or not a.migration or a.rollback or (bool(a.sqlite_test_db)==bool(a.engine and a.image))):
        print(json.dumps({'status':'BLOCKED','reason':'paired schema versions require migration and exactly one SQLite or PostgreSQL target; no rollback acceptance mode'}));return 2
    result={'schema':1,'host_logic':{'status':'IMPLEMENTED_NOT_EXERCISED','limitations':['No database is created unless an explicit test DB path and operation are provided.']},'container_acceptance':'NOT_RUN'}
    if a.self_check_db:
        try: result['host_logic']=db.sqlite_self_check(a.self_check_db)
        except (OSError,ValueError,sqlite3.Error) as e:
            print(json.dumps({'schema':1,'status':'FAIL','reason':str(e)},ensure_ascii=False)); return 2
    if a.sqlite_test_db or (a.migration and not (a.engine or a.image)):
        if a.rollback:
            print(json.dumps({'schema':1,'status':'BLOCKED','reason':'--rollback is for PostgreSQL acceptance only; SQLite restores the pre-migration backup on failure'})); return 2
        if not a.sqlite_test_db or not a.migration:
            print(json.dumps({'schema':1,'status':'BLOCKED','reason':'both --sqlite-test-db and --migration are required'})); return 2
        try:
            out=db.sqlite_lifecycle(a.sqlite_test_db,a.migration.read_text(encoding='utf-8'),expected_version=a.expected_version,target_version=a.target_version); result['host_logic']=out
        except (OSError,ValueError,sqlite3.Error) as e:
            print(json.dumps({'schema':1,'status':'FAIL','reason':str(e)},ensure_ascii=False)); return 2
    if a.engine or a.image:
        if not a.engine or not a.image:
            print(json.dumps({'schema':1,'status':'BLOCKED','reason':'PostgreSQL container check requires engine and exact local image ID'})); return 2
        handle=None
        try:
            handle=db.postgres_start(a.engine,a.image)
            migration=a.migration.read_text(encoding='utf-8') if a.migration else 'CREATE TABLE IF NOT EXISTS app_schema_migrations(version integer PRIMARY KEY); INSERT INTO app_schema_migrations VALUES (1) ON CONFLICT DO NOTHING;'
            rollback=a.rollback.read_text(encoding='utf-8') if a.rollback else 'DROP TABLE IF EXISTS app_schema_migrations;'
            result['container_acceptance']=(db.postgres_migrate(handle,migration,expected_version=a.expected_version,target_version=a.target_version) if versioned else db.postgres_accept(a.engine,handle,migration,rollback))
            db.postgres_stop(a.engine,handle['container'],handle['network'],handle['volume'])
        except (OSError,ValueError,application_builder.BuildFailure,application_builder.MissingPrerequisite) as e:
            if handle:
                try: db.postgres_stop(a.engine,handle['container'],handle['network'],handle['volume'])
                except Exception: pass
            print(json.dumps({'schema':1,'status':'BLOCKED','reason':str(e)},ensure_ascii=False)); return 2
    print(json.dumps(result,ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
