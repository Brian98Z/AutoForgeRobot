"""Exercise real staged data validation and candidate authority boundaries."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import copy,json,tempfile
from pathlib import Path
import applications
import application_spec as spec
import application_spec_check
import specification_adapter as adapter


def main():
    with tempfile.TemporaryDirectory(prefix='spec-adapter-') as tmp:
        root=Path(tmp)
        def run(draft,index,obligations=None):
            candidate={'schema':1,'goal':'iterate specification','references':[],'files':[{'path':'spec.json','content':json.dumps(draft)}]}
            stage=applications.stage_candidate(candidate,root/str(index))
            manifest=json.loads(Path(stage['manifest']).read_text(encoding='utf-8'))
            return adapter.validate({'schema':1,'candidate_sha256':manifest['candidate_sha256'],'workspace_manifest':manifest},'Manage inventory items',obligations)
        baseline=application_spec_check.fixture();baseline['oracle']['hidden_oracle_ids']=[]
        broken=copy.deepcopy(baseline);broken['tasks'][1]['depends_on']=['absent']
        assert run(broken,1)['test_pass'] is False
        assert run(baseline,2)['test_pass'] is True
        changed=copy.deepcopy(baseline);changed['goal']='different goal'
        assert run(changed,3)['test_pass'] is False
        approved=spec.approve_spec(baseline,operator='candidate',authorization_source='self')
        assert run(approved,4)['test_pass'] is False
        hidden=copy.deepcopy(baseline);hidden['oracle']['hidden_oracle_ids']=['private']
        assert run(hidden,5)['test_pass'] is False
        obligations={'schema':1,'requirements':copy.deepcopy(baseline['requirements'])}
        assert run(baseline,6,obligations)['test_pass'] is True
        weakened=copy.deepcopy(baseline);weakened['requirements'][0]['invariants']=[]
        assert run(weakened,7,obligations)['test_pass'] is False
        relabeled=copy.deepcopy(baseline);relabeled['requirements'][0]['provenance']='design'
        assert run(relabeled,8,obligations)['test_pass'] is False
        missing_requirement=copy.deepcopy(baseline);missing_requirement['tasks'][0]['requirement_ids']=['UNKNOWN']
        assert run(missing_requirement,9)['test_pass'] is False
        incomplete=copy.deepcopy(baseline);incomplete['requirements'][0]['invariants']=[]
        malformed={'schema':1,'requirements':[{'id':'R-ADD','acceptance_ids':['AC-ADD']}]}
        assert run(baseline,10,malformed)['test_pass'] is False
    print('specification_adapter_check: PASS (dependency failure/repair, exact goal, self-approval and hidden metadata refusal; data only)')
if __name__=='__main__':main()
