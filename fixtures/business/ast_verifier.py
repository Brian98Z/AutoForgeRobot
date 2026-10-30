"""Trusted syntax-only verifier. Does not execute candidate source or claim business correctness."""
import ast
import json
from pathlib import Path
import sys

p=json.load(sys.stdin);m=p['workspace_manifest'];issues=[]
for item in m['files']:
    if item['path'].endswith('.py'):
        try:ast.parse(Path(m['workspace'],item['path']).read_text(encoding='utf-8'),filename=item['path'])
        except SyntaxError as e:issues.append(f'SyntaxError in {item["path"]} line {e.lineno}: {e.msg}')
if not any(x['path']=='main.py' for x in m['files']):issues.append('main.py required')
print(json.dumps({'schema':1,'candidate_sha256':p['candidate_sha256'],'build_pass':not issues,'test_pass':not issues,'observations':issues or ['Python syntax parsed successfully.'],'limitations':['AST syntax only. Candidate source NOT executed; no business correctness or container validation.']}))
