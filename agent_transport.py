#!/usr/bin/env python3
"""OpenAI-compatible transport for model-driven agent tool selection."""
from __future__ import annotations

import json
from typing import Any

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


def complete(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Request one complete model turn, returning its assistant message without dispatching tools."""
    clean_messages = _messages(messages)
    clean_tools = _tools(tools)
    _base, _url, model, _key = application_proposer._endpoint()
    envelope = application_proposer.completion({
        "model": model,
        "messages": clean_messages,
        "tools": clean_tools,
        "tool_choice": "auto",
        "max_tokens": MAX_TOKENS,
    })
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise Refused("API response must contain exactly one choice")
    choice = choices[0]
    reason = choice.get("finish_reason")
    message = choice.get("message")
    if reason not in ("stop", "tool_calls") or not isinstance(message, dict):
        raise Refused("API response has missing, truncated, or invalid finish reason/message")
    if message.get("refusal"):
        raise Refused("API refused the request")
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
