"""Conservative pre-dispatch reservations; server counters are not billing evidence."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import time


class Exhausted(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    model_calls: int = 12
    tool_calls: int = 96
    tokens: int = 1_000_000
    seconds: float = 600
    cost_microunits: int = 0
    input_microunits_per_million: int = 0
    output_microunits_per_million: int = 0
    output_tokens: int = 4096
    repeated_failures: int = 3

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == 'seconds':
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError('seconds must be finite and positive')
            elif type(value) is not int or value < (0 if 'microunits' in name else 1):
                raise ValueError(f'invalid budget field: {name}')
        if self.output_tokens > 4096 or self.model_calls > 1000 or self.tool_calls > 10000:
            raise ValueError('budget exceeds controller limits')


class Budget:
    def __init__(self, limits: Limits, state: dict | None = None):
        self.limits = limits
        self.started = time.monotonic()
        self.state = dict(state or {'model_calls': 0, 'tool_calls': 0, 'reserved_tokens': 0,
                                    'reserved_cost_microunits': 0, 'server_tokens': 0,
                                    'elapsed_seconds': 0.0, 'usage_source': 'conservative_reservation_not_bill'})
        for key in ('model_calls', 'tool_calls', 'reserved_tokens', 'reserved_cost_microunits', 'server_tokens'):
            if type(self.state.get(key)) is not int or self.state[key] < 0:
                raise ValueError('budget checkpoint has invalid counters')
        elapsed = self.state.get('elapsed_seconds')
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError('budget checkpoint has invalid elapsed time')
        self.elapsed_prior = elapsed

    def expired(self):
        return self.elapsed_prior + time.monotonic() - self.started >= self.limits.seconds

    def check_time(self):
        if self.expired():
            raise Exhausted('wall_time_budget')

    def reserve_model(self, messages, tools):
        self.check_time()
        if self.state['model_calls'] >= self.limits.model_calls:
            raise Exhausted('model_call_budget')
        # UTF-8 byte fallback is deliberately conservative for byte-level tokenizers.
        # Fixed framing allowance covers role/function wrappers. Not a billing estimate.
        encoded = json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        input_tokens = len(encoded) + 4096 + 32 * len(messages)
        output_tokens = self.limits.output_tokens
        tokens = input_tokens + output_tokens
        cost = (input_tokens * self.limits.input_microunits_per_million + output_tokens * self.limits.output_microunits_per_million + 999999) // 1000000
        if self.state['reserved_tokens'] + tokens > self.limits.tokens:
            raise Exhausted('token_budget')
        if self.state['reserved_cost_microunits'] + cost > self.limits.cost_microunits:
            raise Exhausted('cost_budget')
        self.state['model_calls'] += 1
        self.state['reserved_tokens'] += tokens
        self.state['reserved_cost_microunits'] += cost
        return {'input_token_bound': input_tokens, 'output_token_limit': output_tokens, 'reserved_tokens': tokens, 'reserved_cost_microunits': cost}

    def record_usage(self, usage, reservation):
        prompt, completion, total = (usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
        if any(type(x) is not int or x < 0 for x in (prompt, completion, total)) or total != prompt + completion:
            raise Exhausted('invalid_server_usage')
        self.state['server_tokens'] += total
        if prompt > reservation['input_token_bound'] or completion > reservation['output_token_limit']:
            raise Exhausted('provider_exceeded_reservation')

    def reserve_tool(self):
        self.check_time()
        if self.state['tool_calls'] >= self.limits.tool_calls:
            raise Exhausted('tool_call_budget')
        self.state['tool_calls'] += 1

    def snapshot(self):
        return {**self.state, 'elapsed_seconds': self.elapsed_prior + time.monotonic() - self.started}
