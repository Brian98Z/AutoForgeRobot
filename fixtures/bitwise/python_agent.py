"""Operator-installed example extension; not generated or approved by AutoForge."""
import json
import sys


def main():
    request = json.load(sys.stdin)
    if request['schema'] != 1:
        raise ValueError('unsupported protocol')
    task = request['task']
    if set(task) != {'id', 'goal', 'public_cases'}:
        raise ValueError('unexpected fields exposed to proposer')
    cases = task['public_cases']
    for instruction, operation in [('xor', lambda a,b:a^b), ('and', lambda a,b:a&b), ('or', lambda a,b:a|b)]:
        if all(operation(*c['input']) == c['expected'] for c in cases):
            body = f'(i64.{instruction} (local.get 0) (local.get 1))'
            print(json.dumps({'wat': '(module (func (export "solve") (param i64 i64) (result i64) '+body+'))',
                              'strategy': 'operator-extension:bitwise:'+instruction}))
            return
    raise ValueError('bitwise grammar cannot satisfy public cases')


if __name__ == '__main__':
    main()
