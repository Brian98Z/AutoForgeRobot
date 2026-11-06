import json
import application_operations as ops
from pathlib import Path
root=Path(r'F:\OMP\projects\项目与插件\autoforge\bootstrap-runs\stateful-delivery-20261104')
auth=json.loads((root.parent/'stateful-delivery-20261104-authorization.json').read_text())
image=auth['image_ids'][0]
try:
 print(ops._stateful_database('docker',image,'af-state-diagnostic-20261104','CREATE TABLE IF NOT EXISTS migration_probe(value TEXT);',0,1))
except Exception as e:
 print(type(e).__name__+': '+str(e))
