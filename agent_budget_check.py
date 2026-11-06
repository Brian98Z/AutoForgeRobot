"""Check pre-dispatch resource reservation, not provider billing guarantees."""
from dataclasses import replace
import json
import agent_budget as b


def refused(fn, code):
    try:
        fn()
    except b.Exhausted as exc:
        assert str(exc) == code, (code, str(exc))
    else:
        raise AssertionError(code)


def main():
    messages = [{'role': 'user', 'content': '中文'}]
    limits = b.Limits(model_calls=1, tool_calls=1)
    budget = b.Budget(limits)
    reservation = budget.reserve_model(messages, [])
    assert reservation['input_token_bound'] > len('中文'.encode())
    budget.record_usage({'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9}, reservation)
    assert budget.snapshot()['server_tokens'] == 9
    refused(lambda: budget.reserve_model(messages, []), 'model_call_budget')
    budget.reserve_tool()
    refused(budget.reserve_tool, 'tool_call_budget')
    refused(lambda: b.Budget(replace(limits, tokens=1)).reserve_model(messages, []), 'token_budget')
    refused(lambda: b.Budget(replace(limits, input_microunits_per_million=1_000_000)).reserve_model(messages, []), 'cost_budget')
    restored = b.Budget(limits, budget.snapshot())
    refused(lambda: restored.reserve_model(messages, []), 'model_call_budget')
    over = b.Budget(limits)
    res = over.reserve_model(messages, [])
    refused(lambda: over.record_usage({'prompt_tokens': res['input_token_bound'] + 1, 'completion_tokens': 0, 'total_tokens': res['input_token_bound'] + 1}, res), 'provider_exceeded_reservation')
    expired = b.Budget(limits, {**budget.snapshot(), 'elapsed_seconds': limits.seconds})
    refused(expired.reserve_tool, 'wall_time_budget')
    print(json.dumps({'status': 'PASS', 'scope': 'conservative resource reservations and checkpoint preservation; not billing proof'}))


if __name__ == '__main__':
    main()
