"""Trusted data-only builder for runtime-led specification iteration; never executes candidate files."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import application_builder as builder
import application_spec as spec


def validate(envelope, expected_goal, obligations=None):
    root,digest=builder._manifest(envelope['workspace_manifest'],require_entrypoint=False)
    if envelope.get('schema')!=1 or envelope.get('candidate_sha256')!=digest:
        raise ValueError('candidate envelope identity mismatch')
    observations=[]
    try:
        raw=builder._load_json(root/'spec.json',builder.MAX_INPUT)
        draft=spec.validate_spec(raw)
        if draft['goal']!=expected_goal: raise spec.SpecRefused('authoritative goal changed')
        if draft['approval']['status']!='draft': raise spec.SpecRefused('candidate cannot approve its own specification')
        if draft['oracle']['hidden_oracle_ids']: raise spec.SpecRefused('candidate must not supply hidden oracle metadata')
        if obligations is not None:
            if not isinstance(obligations,dict) or set(obligations)!={'schema','requirements'} or obligations['schema']!=1 or not isinstance(obligations['requirements'],list) or not obligations['requirements']:
                raise ValueError('invalid controller obligation contract')
            actual={row['id']:row for row in draft['requirements']}
            required_ids=set()
            for required in obligations['requirements']:
                if not isinstance(required,dict) or set(required)!=(spec.REQUIRED_REQUIREMENT|{'statement'}) or not isinstance(required.get('id'),str):
                    raise ValueError('invalid obligation identity or fields')
                if required['id'] in required_ids: raise ValueError('duplicate controller obligation identity')
                required_ids.add(required['id'])
            actual={row['id']:row for row in draft['requirements']}
            for required in obligations['requirements']:
                if not isinstance(required,dict) or set(required)!=(spec.REQUIRED_REQUIREMENT|{'statement'}) or not isinstance(required.get('id'),str):
                    raise ValueError('invalid obligation identity or fields')
                row=actual.get(required['id'])
                if row is None: raise spec.SpecRefused('required obligation removed: '+required['id'])
                for key,value in required.items():
                    if row[key]!=value: raise spec.SpecRefused('controller obligation changed: '+required['id']+'.'+key)
        observations.append('Structured draft validated; semantic approval and business acceptance remain external.')
        passed=True
    except (OSError,ValueError,UnicodeError) as exc:
        observations.append(str(exc)[:2000]);passed=False
    return {'schema':1,'candidate_sha256':digest,'build_pass':passed,'test_pass':passed,'observations':observations,'limitations':['Data-only structural specification validation; no candidate code executed.','PASS is not semantic approval, executable business acceptance, or release authorization.']}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--goal-file',type=Path,required=True);p.add_argument('--obligations',type=Path);a=p.parse_args()
    envelope=builder._strict_loads(sys.stdin.buffer.read(builder.MAX_INPUT+1))
    obligations=builder._load_json(a.obligations,builder.MAX_INPUT) if a.obligations else None
    print(json.dumps(validate(envelope,a.goal_file.read_text(encoding='utf-8'),obligations),ensure_ascii=True))
if __name__=='__main__':main()
