"""Pure validation and immutable revision helpers for bounded public-feedback repair."""
from __future__ import annotations
import hashlib
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def validate_feedback(original, effective, feedback):
    """Reject malformed, conflicting, duplicate, or holdout-overlapping public labels."""
    if not isinstance(feedback, list) or not feedback:
        raise ValueError("repair requires nonempty public feedback")
    holdout = {tuple(row["input"]): row["expected"] for row in original["holdout_cases"]}
    public = {tuple(row["input"]): row["expected"] for row in effective["public_cases"]}
    additions = {}
    for row in feedback:
        if not isinstance(row, dict) or set(row) != {"input", "expected"}:
            raise ValueError("invalid public feedback row")
        inp, expected = row["input"], row["expected"]
        if not isinstance(inp, list) or len(inp) != 2 or type(expected) is not int or any(type(v) is not int or not -(1<<63) <= v < (1<<63) for v in inp+[expected]):
            raise ValueError("public feedback requires exact i64 input and expected values")
        key = tuple(inp)
        if key in holdout:
            raise ValueError("feedback overlaps protected holdout; refusing promotion")
        if key in public and public[key] != expected:
            raise ValueError("public feedback contradicts existing public label")
        if key in additions and additions[key] != expected:
            raise ValueError("conflicting duplicate public feedback")
        additions[key] = expected
    return [{"input": list(key), "expected": value} for key, value in sorted(additions.items()) if key not in public]


def revision_task(original, effective, additions, revision):
    """Create a linked effective contract without changing original holdout data."""
    if type(revision) is not int or revision < 1:
        raise ValueError("revision must be a positive integer")
    task = dict(effective)
    task["public_cases"] = list(effective["public_cases"]) + list(additions)
    task["holdout_cases"] = list(original["holdout_cases"])
    task['id'] = f"{original['id'][:90]}-repair-{revision:02d}-{sha256(canonical(additions))[:12]}"
    if len(task['public_cases'])+len(task['holdout_cases'])>256:
        raise ValueError('repaired contract exceeds controller case budget')
    return task
