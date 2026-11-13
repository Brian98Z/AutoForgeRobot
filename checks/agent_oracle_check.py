"""Exercise runtime HOLD/unknown semantics for the trusted coarse oracle adapter."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import agent_runtime as runtime
import agent_session
def main():
    request = {'schema': 1, 'goal': 'coarse oracle controller contract', 'references': []}
    candidate = {**request, 'files': [{'path': 'main.py', 'content': '# never executed\n'}]}
    outcomes = {}
    with tempfile.TemporaryDirectory(prefix='agent-oracle-') as td:
        root = Path(td)
        nested = root / 'trusted.py'; nested.write_text('# trusted adapter identity\n')
        contract = root / 'contract.json'; contract.write_text('{}')
        ledger = root / 'ledger.json'
        argv = [sys.executable, str(Path(runtime.__file__).with_name('application_oracle.py')), '--ledger', str(ledger), '--contract-sha256', 'a' * 64, '--max-queries', '1', '--builder-argv', json.dumps([sys.executable, str(nested), '--contract', str(contract)])]
        for effect in ('not_dispatched', 'unknown'):
            def answer(*args, **kwargs):
                return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'heldout', 'type': 'function', 'function': {'name': 'validate', 'arguments': '{}'}}]}
            def adapter(command, payload):
                digest = payload['candidate_sha256']
                return {'schema': 1, 'candidate_sha256': digest, 'build_pass': False, 'test_pass': False, 'oracle_status': 'HOLD', 'reason_codes': ['query_limit' if effect == 'not_dispatched' else 'unknown_outcome'], 'artifact_sha256': digest, 'external_effect': effect, 'observations': ['Heldout validation HOLD.'], 'limitations': ['coarse result only']}
            out = root / effect
            with patch.object(runtime.agent_transport, 'complete', side_effect=answer), patch.object(runtime.applications, '_run_json_command', side_effect=adapter):
                report = runtime.run(request, [], argv, out, initial_candidate=candidate)
            assert report['status'] == 'HOLD' and report['task_status'] == 'BLOCKED', report
            state = agent_session.load(out)
            assert bool(state['inflight']) == (effect == 'unknown'), state
            assert report['reason_code'] == ('unknown_tool_side_effect' if effect == 'unknown' else 'oracle_validation_hold')
            validations = list((out / 'revisions').glob('*/validation.json'))
            assert json.loads(validations[0].read_text())['status'] == 'HOLD'
            assert state['validated_candidate'] is None
            if effect == 'unknown':
                with patch.object(runtime.agent_transport, 'complete', side_effect=AssertionError('unknown replayed')):
                    resumed = runtime.run(request, [], argv, out, resume=True)
                assert resumed['reason_code'] == 'unknown_tool_side_effect'
            outcomes[effect] = report['reason_code']
        limits = runtime.agent_budget.Limits()
        before = runtime._configuration(request, [], argv, limits, ['validate'])
        ledger.write_text('changing mutable allowance must not alter command identity')
        assert runtime._configuration(request, [], argv, limits, ['validate']) == before
        nested.write_text('# changed trusted source\n')
        assert runtime._configuration(request, [], argv, limits, ['validate']) != before
        nested.write_text('# trusted adapter identity\n')
        contract.write_text('{"changed":true}')
        assert runtime._configuration(request, [], argv, limits, ['validate']) != before
    print(json.dumps({'status': 'PASS', 'scope': 'controller outcome regression, no candidate execution', 'checks': outcomes, 'identity': 'nested trusted adapter/contract bound; mutable ledger bytes excluded'}))
if __name__ == '__main__': main()
