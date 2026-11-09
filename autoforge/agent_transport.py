#!/usr/bin/env python3
"""OpenAI-compatible transport for model-driven agent tool selection."""
from __future__ import annotations

import json
import os
from typing import Any, Callable

import application_proposer

Refused = application_proposer.Refused

MAX_TOKENS = 4096
def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Refused(f"{label} must be an object")
    return value


def _messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise Refused("messages must be a non-empty array")
    allowed = {"role", "content", "name", "tool_call_id", "tool_calls"}
    result: list[dict[str, Any]] = []
    for item in value:
        message = _object(item, "message")
        if set(message) - allowed:
            message = {key: val for key, val in message.items() if key in allowed}
        role = message.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise Refused("message has invalid role")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise Refused("message content must be a string or null")
        clean: dict[str, Any] = {"role": role, "content": content}
        if "name" in message:
            if not isinstance(message["name"], str):
                raise Refused("message name must be a string")
            clean["name"] = message["name"]
        if "tool_call_id" in message:
            if not isinstance(message["tool_call_id"], str):
                raise Refused("tool_call_id must be a string")
            clean["tool_call_id"] = message["tool_call_id"]
        if "tool_calls" in message:
            if role != "assistant" or not isinstance(message["tool_calls"], list):
                raise Refused("tool_calls is valid only as an assistant array")
            clean["tool_calls"] = message["tool_calls"]
        result.append(clean)
    return result


def _tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise Refused("tools must be an array")
    result = []
    names = set()
    for item in value:
        tool = _object(item, "tool")
        fn = _object(tool.get("function"), "tool function")
        name = fn.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise Refused("tool function names must be non-empty and unique")
        names.add(name)
        if set(tool) != {"type", "function"} or tool.get("type") != "function":
            raise Refused("tool must be a function definition")
        if not isinstance(fn.get("parameters"), dict):
            raise Refused("tool function parameters must be a JSON schema object")
        result.append(tool)
    return result


def _usage(value: Any) -> dict[str, int]:
    usage = _object(value, "usage")
    required = {"prompt_tokens", "completion_tokens", "total_tokens"}
    if not required <= set(usage):
        raise Refused("API usage counters are malformed")
    if any(type(usage[key]) is not int or usage[key] < 0 for key in required):
        raise Refused("API usage counters are malformed")
    if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        raise Refused("API usage counters are inconsistent")
    return {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


def _stream(body: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
            should_stop: Callable[[], bool] | None) -> dict[str, Any]:
    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish = None
    role_seen = False
    usage = None
    usage_seen = False
    for chunk in application_proposer.interruptible_stream_completion(
            body, should_stop, application_proposer.MAX_RESPONSE_BYTES):
        if chunk.get("error") is not None:
            raise Refused("API stream returned an error")
        if "usage" in chunk and chunk["usage"] is not None:
            if usage_seen:
                raise Refused("API stream contains duplicate usage")
            usage = _usage(chunk["usage"])
            usage_seen = True
        choices = chunk.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise Refused("API stream must contain one choice")
        if not choices:
            if finish is None or set(chunk) - {"choices", "usage", "id", "object", "created", "model", "system_fingerprint", "service_tier", "timings"}:
                raise Refused("API stream has an unexpected empty choice")
            continue
        choice = _object(choices[0], "stream choice")
        if type(choice.get("index")) is not int or choice["index"] != 0 or finish is not None:
            raise Refused("API stream has an invalid choice index or data after finish")
        delta = _object(choice.get("delta"), "stream delta")
        if set(delta) - {"role", "content", "tool_calls", "refusal", "reasoning_content"}:
            raise Refused("API stream uses unsupported delta fields")
        if delta.get("refusal"):
            raise application_proposer.TransportError("API refused the request", "refusal")
        if "role" in delta:
            if delta["role"] != "assistant":
                raise Refused("API stream role is not assistant")
            role_seen = True
        text = delta.get("content")
        if text is not None:
            if not isinstance(text, str):
                raise Refused("API stream content must be text")
            content.append(text)
            if text:
                on_event({"type": "model_text_delta", "text": text})
        fragments = delta.get("tool_calls", [])
        if not isinstance(fragments, list):
            raise Refused("API stream tool_calls must be an array")
        for fragment in fragments:
            fragment = _object(fragment, "stream tool call")
            index = fragment.get("index")
            if type(index) is not int or not 0 <= index < 96 or set(fragment) - {"index", "id", "type", "function"}:
                raise Refused("API stream tool call has invalid fields/index")
            call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if "type" in fragment and fragment["type"] != "function":
                raise Refused("API stream tool call must be a function")
            fn = _object(fragment.get("function", {}), "stream tool function")
            if set(fn) - {"name", "arguments"}:
                raise Refused("API stream tool function has invalid fields")
            for key, target, source in (("id", call, fragment), ("name", call["function"], fn), ("arguments", call["function"], fn)):
                if key in source:
                    if not isinstance(source[key], str):
                        raise Refused("API stream tool fragments must be strings")
                    target[key] += source[key]
            on_event({"type": "model_tool_delta", "index": index,
                      "id_delta": fragment.get("id", ""), "name_delta": fn.get("name", ""),
                      "arguments_delta": fn.get("arguments", "")})
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason == "length":
                raise application_proposer.TransportError("API response was truncated", "truncation")
            if reason == "content_filter":
                raise application_proposer.TransportError("API refused the request", "refusal")
            if reason != "stop" and reason != "tool_calls":
                raise Refused("API stream has unsupported finish reason")
            finish = reason
    if not role_seen or finish is None or sorted(calls) != list(range(len(calls))):
        raise application_proposer.TransportError("API stream ended without a complete assistant turn", "truncation")
    if usage is not None:
        if on_event is not None:
            on_event({"type": "model_usage", "usage": usage, "source": "server"})
    elif on_event is not None:
        on_event({"type": "model_usage_unavailable", "source": "unavailable"})
    message = {"role": "assistant", "content": "".join(content) if content else None}
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {"choices": [{"finish_reason": finish, "message": message}]}


def complete(messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
             on_event: Callable[[dict[str, Any]], None] | None = None,
             should_stop: Callable[[], bool] | None = None,
             max_tokens: int = MAX_TOKENS) -> dict[str, Any]:
    """Return one validated assistant turn; streaming deltas are observation only."""
    clean_messages = _messages(messages)
    clean_tools = _tools(tools)
    _base, _url, model, _key = application_proposer._endpoint()
    tool_choice = os.environ.get('AUTOFORGE_TOOL_CHOICE', 'auto')
    if tool_choice not in ('auto', 'required'):
        raise Refused('AUTOFORGE_TOOL_CHOICE must be auto or required')
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS:
        raise Refused(f"max_tokens must be an integer from 1 to {MAX_TOKENS}")
    if should_stop is not None and should_stop():
        raise application_proposer.TransportError("API request cancelled", "cancellation")
    thinking = os.environ.get('AUTOFORGE_ENABLE_THINKING', '')
    if thinking not in ('', 'true', 'false'):
        raise Refused('AUTOFORGE_ENABLE_THINKING must be unset, true, or false')
    body = {"model": model, "messages": clean_messages, "tools": clean_tools,
            "tool_choice": tool_choice, "parallel_tool_calls": False, "max_tokens": max_tokens,
            "stream_options": {"include_usage": True}}
    if thinking:
        body['chat_template_kwargs'] = {'enable_thinking': thinking == 'true'}
    if on_event is not None or should_stop is not None:
        envelope = _stream(body, on_event or (lambda _event: None), should_stop)
    else:
        envelope = application_proposer.completion(body)
    if not isinstance(envelope, dict):
        raise Refused("API response must be an object")
    choices = envelope.get("choices")
    usage = _usage(envelope["usage"]) if envelope.get("usage") is not None else None
    if usage is not None and on_event is not None and not envelope.get("stream_usage_emitted"):
        on_event({"type": "model_usage", "usage": usage, "source": "server"})
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise Refused("API response must contain exactly one choice")
    choice = choices[0]
    reason = choice.get("finish_reason")
    message = choice.get("message")
    if reason == "length":
        raise application_proposer.TransportError("API response was truncated", "truncation")
    if reason == "content_filter":
        raise application_proposer.TransportError("API refused the request", "refusal")
    if reason not in ("stop", "tool_calls") or not isinstance(message, dict):
        raise Refused("API response has missing or invalid finish reason/message")
    if message.get("refusal"):
        raise application_proposer.TransportError("API refused the request", "refusal")
    # Some compatible providers attach private reasoning metadata; never return it to the agent loop.
    if set(message) - {"role", "content", "tool_calls", "refusal", "function_call", "audio", "annotations", "reasoning_content"}:
        raise Refused("API response has an invalid assistant message shape")
    if message.get("role") != "assistant":
        raise Refused("API response message role is not assistant")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise Refused("API response content must be a string or null")
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list):
        raise Refused("API tool_calls must be an array")
    if reason == "tool_calls" and not calls:
        raise Refused("tool_calls finish reason requires tool calls")
    if reason == "stop" and calls:
        raise Refused("stop finish reason cannot contain tool calls")
    if message.get("function_call") is not None or message.get("audio") is not None:
        raise Refused("API response uses an unsupported message shape")
    known_names = {tool["function"]["name"] for tool in clean_tools}
    result_calls = []
    ids = set()
    for call in calls:
        call = _object(call, "tool call")
        if set(call) != {"id", "type", "function"} or call.get("type") != "function":
            raise Refused("tool call has an invalid shape")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in ids:
            raise Refused("tool call IDs must be non-empty and unique")
        ids.add(call_id)
        fn = _object(call.get("function"), "tool call function")
        if set(fn) != {"name", "arguments"} or fn.get("name") not in known_names:
            raise Refused("tool call names an unknown function or has invalid fields")
        arguments = fn.get("arguments")
        if not isinstance(arguments, str):
            raise Refused("tool arguments must be a JSON string")
        parsed = application_proposer._json_loads(arguments, "tool arguments")
        if not isinstance(parsed, dict):
            raise Refused("tool arguments must decode to a JSON object")
        result_calls.append({"id": call_id, "type": "function",
                             "function": {"name": fn["name"], "arguments": arguments}})
    if reason == "stop" and content is None:
        raise Refused("stop response has no textual content")
    return {"role": "assistant", "content": content, **({"tool_calls": result_calls} if result_calls else {})}
