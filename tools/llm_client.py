"""Shared Together configuration and streaming support for text workflows.

Qwen3.8 Flash requires streaming. Callers that need a complete string or JSON
document receive a buffered completion; browser callers can consume live chunks.
"""

from __future__ import annotations

import json as json_module
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEFAULT_MODEL = "Qwen/Qwen3.8-Flash"
TOGETHER_MODEL = os.environ.get("TOGETHER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
TOGETHER_REASONING_EFFORT = os.environ.get("TOGETHER_REASONING_EFFORT", "low")
TOGETHER_BASE_URL = "https://api.together.ai/v1"
TOGETHER_CHAT_URL = f"{TOGETHER_BASE_URL}/chat/completions"


def _parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    result = dict(parameters)
    result["model"] = result.get("model") or TOGETHER_MODEL
    result["stream"] = True
    if result["model"] == DEFAULT_MODEL:
        result.setdefault("reasoning_effort", TOGETHER_REASONING_EFFORT)
        # Reasoning shares the output budget. Small extraction/judge requests
        # still need enough space to reach their final answer.
        if result.get("max_tokens") is not None:
            result["max_tokens"] = max(result["max_tokens"], 1024)
    return result


def _collect_chunks(chunks: Iterable[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"model": TOGETHER_MODEL, "choices": [], "usage": None}
    choices: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        event = chunk if isinstance(chunk, dict) else chunk.model_dump(exclude_none=True)
        if event.get("error"):
            error = event["error"]
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise RuntimeError(f"Together stream failed: {message}")
        for key in ("id", "model", "created"):
            if event.get(key) is not None:
                result[key] = event[key]
        if event.get("usage"):
            result["usage"] = event["usage"]
        for part in event.get("choices") or []:
            index = part.get("index", 0)
            choice = choices.setdefault(index, {
                "index": index, "content": [], "tool_calls": {}, "finish_reason": None,
            })
            delta = part.get("delta") or {}
            if delta.get("content"):
                choice["content"].append(delta["content"])
            for tool in delta.get("tool_calls") or []:
                call = choice["tool_calls"].setdefault(tool.get("index", 0), {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                })
                if tool.get("id"):
                    call["id"] = tool["id"]
                if tool.get("type"):
                    call["type"] = tool["type"]
                for key in ("name", "arguments"):
                    call["function"][key] += (tool.get("function") or {}).get(key) or ""
            if part.get("finish_reason"):
                choice["finish_reason"] = part["finish_reason"]

    if not choices:
        raise RuntimeError("Together returned an empty completion stream.")
    for index, choice in sorted(choices.items()):
        content = "".join(choice["content"])
        tool_calls = [call for _, call in sorted(choice["tool_calls"].items())]
        if not content and not tool_calls:
            raise RuntimeError("The model returned no answer. Increase the output token budget and try again.")
        result["choices"].append({
            "index": index,
            "message": {"role": "assistant", "content": content or None, "tool_calls": tool_calls or None},
            "finish_reason": choice["finish_reason"],
        })
    return result


def _namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


def create_completion(client: Any, **parameters: Any) -> Any:
    """Use a Together/OpenAI SDK client, buffering unless live output is requested."""
    live = parameters.get("stream", False)
    request = _parameters(parameters)
    extra_body = dict(request.get("extra_body") or {})
    extra_body.setdefault("stream_options", {"include_usage": True})
    request["extra_body"] = extra_body
    stream = client.chat.completions.create(**request)
    if live:
        return stream
    try:
        return _namespace(_collect_chunks(stream))
    finally:
        stream.close()


@dataclass
class BufferedCompletionResponse:
    """HTTP-style result for existing requests-based generators and validators."""

    status_code: int
    headers: Any
    text: str
    _response: requests.Response
    _data: dict[str, Any] | None = None

    def json(self) -> dict[str, Any]:
        return self._data if self._data is not None else json_module.loads(self.text)

    def raise_for_status(self) -> None:
        self._response.raise_for_status()


def request_completion(
    url: str = TOGETHER_CHAT_URL,
    *,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    timeout: Any = 120,
) -> BufferedCompletionResponse:
    """Collect SSE events into the standard completion JSON response shape."""
    payload = _parameters(json or {})
    payload.setdefault("stream_options", {"include_usage": True})
    response = requests.post(url, headers=headers, json=payload, timeout=timeout, stream=True)
    try:
        if response.status_code >= 400:
            return BufferedCompletionResponse(response.status_code, response.headers, response.text, response)

        def events():
            for line in response.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                if data:
                    yield json_module.loads(data)

        data = _collect_chunks(events())
        return BufferedCompletionResponse(response.status_code, response.headers, json_module.dumps(data), response, data)
    finally:
        response.close()
