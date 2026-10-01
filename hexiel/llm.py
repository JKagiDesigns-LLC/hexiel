"""Provider plumbing: streaming chat over any OpenAI-compatible endpoint or Anthropic.

Internal message format (provider-agnostic):
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments": dict}]}
    {"role": "tool", "content": str, "tool_call_id": id}

Streaming providers yield ("text", delta) events and ("call", index, {"id"/"name"} deltas),
then a final ("done", usage) event. Callers accumulate the assistant message themselves
via the AssistantBuffer returned by AssistantBuffer.from_stream().
"""

import json
import os
from collections.abc import Iterator

import httpx


class ProviderError(RuntimeError):
    pass


def est_tokens(text: str) -> int:
    """Cheap heuristic; exact usage from the API is preferred whenever available."""
    return (len(text) + 3) // 4


class AssistantBuffer:
    """Accumulates streamed assistant content and tool calls into the internal format."""

    def __init__(self) -> None:
        self.text = ""
        self.tool_calls: list[dict] = []
        self._args_chunks: dict[int, list[str]] = {}

    def add_text(self, delta: str) -> None:
        self.text += delta

    def start_call(self, index: int, call_id: str, name: str) -> None:
        self.ensure_call(index)
        self.tool_calls[index]["id"] = call_id or f"call_{index}"
        if name:
            self.tool_calls[index]["name"] = name

    def ensure_call(self, index: int) -> None:
        while len(self.tool_calls) <= index:
            self.tool_calls.append({"id": "", "name": "", "arguments": {}})

    def add_args(self, index: int, chunk: str) -> None:
        self._args_chunks.setdefault(index, []).append(chunk)

    def finalize(self) -> dict:
        calls = []
        for i, call in enumerate(self.tool_calls):
            raw = "".join(self._args_chunks.get(i, []))
            try:
                args = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw_arguments": raw}
            calls.append({"id": call["id"] or f"call_{i}", "name": call["name"],
                          "arguments": args})
        msg = {"role": "assistant", "content": self.text}
        if calls:
            msg["tool_calls"] = calls
        return msg


class AnthropicProvider:
    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @classmethod
    def _convert_tools(cls, tools: list[dict]) -> list[dict]:
        return [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]

    @classmethod
    def _convert_messages(cls, messages: list[dict]) -> list[dict]:
        converted = []
        for msg in messages:
            role = msg["role"]
            if role == "tool":
                converted.append({
                    "role": "user",
                    "content": cls._tool_result_blocks(msg),
                })
            elif role == "assistant":
                blocks = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for call in msg.get("tool_calls", []):
                    blocks.append({
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    })
                converted.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif msg.get("images"):
                converted.append({"role": "user", "content": [
                    *({"type": "image", "source": {"type": "base64",
                                                   "media_type": img["media_type"],
                                                   "data": img["data"]}}
                      for img in msg["images"]),
                    {"type": "text", "text": msg.get("content", "")},
                ]})
            else:
                converted.append({"role": "user", "content": msg.get("content", "")})
        return converted

    @classmethod
    def _tool_result_blocks(cls, msg: dict) -> list[dict]:
        blocks = [{"type": "tool_result", "tool_use_id": msg["tool_call_id"],
                   "content": msg.get("content", "")}]
        for image in msg.get("images", []):
            blocks.append({
                "type": "image",
                "source": {"type": "base64",
                           "media_type": image["media_type"],
                           "data": image["data"]},
            })
        return blocks

    def stream(self, messages: list[dict], system: str, tools: list[dict], *,
               model: str, max_output_tokens: int, extra_body: dict,
               timeout: float) -> Iterator[tuple]:
        body = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": self._convert_messages(messages),
            "stream": True,
            **extra_body,
        }
        if tools:
            body["tools"] = self._convert_tools(tools)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        buf = AssistantBuffer()
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", f"{self.base_url}/v1/messages",
                               json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise ProviderError(f"anthropic {resp.status_code}: {resp.read().decode()[:500]}")
                usage = {}
                kind, idx = "", 0
                for sse in _iter_sse(resp.iter_lines()):
                    data = json.loads(sse)
                    mtype = data.get("type", "")
                    if mtype == "message_start":
                        usage["prompt_tokens"] = data["message"]["usage"]["input_tokens"]
                    elif mtype == "content_block_start":
                        block = data["content_block"]
                        if block["type"] == "tool_use":
                            buf.start_call(data["index"], block["id"], block["name"])
                            kind = "tool"
                            idx = data["index"]
                    elif mtype == "content_block_delta":
                        delta = data["delta"]
                        if delta["type"] == "text_delta":
                            buf.add_text(delta["text"])
                            yield ("text", delta["text"])
                        elif delta["type"] == "input_json_delta":
                            buf.add_args(idx, delta["partial_json"])
                    elif mtype == "message_delta":
                        out = data.get("usage", {}).get("output_tokens")
                        if out:
                            usage["completion_tokens"] = out
                yield ("done", usage)
        yield ("assistant", buf.finalize())


class OpenAICompatProvider:
    """Any /chat/completions endpoint: Ollama, vLLM, OpenRouter, OpenAI itself."""

    def __init__(self, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @classmethod
    def _convert_messages(cls, messages: list[dict]) -> list[dict]:
        converted = []
        for msg in messages:
            if msg["role"] == "tool":
                converted.append({
                    "role": "tool",
                    "tool_call_id": msg["tool_call_id"],
                    "content": msg.get("content", ""),
                })
                # OpenAI protocol carries images in user messages, not tool messages
                for image in msg.get("images", []):
                    converted.append({
                        "role": "user",
                        "content": [
                            {"type": "image_url",
                             "image_url": {"url":
                                           f"data:{image['media_type']};base64,{image['data']}"}},
                            {"type": "text",
                             "text": f"(image attached from tool result {msg['tool_call_id']})"},
                        ],
                    })
            elif msg.get("tool_calls"):
                converted.append({
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c["arguments"]),
                            },
                        }
                        for c in msg["tool_calls"]
                    ],
                })
            elif msg.get("images"):
                converted.append({"role": msg["role"], "content": [
                    {"type": "text", "text": msg.get("content", "")},
                    *({"type": "image_url", "image_url": {
                        "url": f"data:{img['media_type']};base64,{img['data']}"}}
                      for img in msg["images"]),
                ]})
            else:
                converted.append({"role": msg["role"], "content": msg.get("content", "")})
        return converted

    def stream(self, messages: list[dict], system: str, tools: list[dict], *,
               model: str, max_output_tokens: int, extra_body: dict,
               timeout: float) -> Iterator[tuple]:
        base = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + self._convert_messages(messages),
            "stream": True,
            "max_tokens": max_output_tokens,
            **extra_body,
        }
        if tools:
            base["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["parameters"]}}
                for t in tools
            ]
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        buf = AssistantBuffer()
        with httpx.Client(timeout=timeout) as client:
            response = None
            # try include_usage first; servers that reject stream_options get one retry
            for attempt in (1, 2):
                payload = dict(base)
                if attempt == 1:
                    payload["stream_options"] = {"include_usage": True}
                response = client.send(client.build_request(
                    "POST", f"{self.base_url}/chat/completions",
                    json=payload, headers=headers), stream=True)
                if response.status_code < 400:
                    break
                text = response.read().decode()[:500]
                response.close()
                if attempt == 1 and "stream_options" in text:
                    continue
                raise ProviderError(f"provider {response.status_code}: {text}")
            try:
                usage = {}
                for sse in _iter_sse(response.iter_lines()):
                    data = json.loads(sse)
                    for choice in data.get("choices", []):
                        delta = choice.get("delta", {})
                        if delta.get("reasoning"):
                            yield ("thinking", delta["reasoning"])
                        if delta.get("content"):
                            buf.add_text(delta["content"])
                            yield ("text", delta["content"])
                        for tc in delta.get("tool_calls", []):
                            idx = tc.get("index", 0)
                            fn = tc.get("function", {})
                            buf.ensure_call(idx)
                            if tc.get("id") or fn.get("id"):
                                buf.tool_calls[idx]["id"] = tc.get("id") or fn.get("id", "")
                            if fn.get("name"):
                                buf.tool_calls[idx]["name"] = fn["name"]
                            if fn.get("arguments"):
                                buf.add_args(idx, fn["arguments"])
                    if data.get("usage"):
                        usage = data["usage"]
                yield ("done", usage)
            finally:
                response.close()
        yield ("assistant", buf.finalize())


def _iter_sse(lines: Iterator[str]) -> Iterator[str]:
    """Yield the data payload of each SSE event, skipping comments and keep-alives."""
    for line in lines:
        line = line.decode() if isinstance(line, bytes) else line
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                yield data


def make_provider(model_config, settings) -> tuple[object, str]:
    """Return (provider, system_prompt_is_separate=True) for a model profile."""
    api_key = os.environ.get(model_config.api_key_env, "") if model_config.api_key_env else ""
    if model_config.provider == "anthropic":
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise SystemExit(f"model '{model_config.name}': set {model_config.api_key_env or 'ANTHROPIC_API_KEY'}")
        provider = AnthropicProvider(api_key, base_url=model_config.base_url or "https://api.anthropic.com")
    else:
        if not model_config.base_url:
            raise SystemExit(f"model '{model_config.name}': openai provider needs base_url")
        provider = OpenAICompatProvider(api_key, base_url=model_config.base_url)
    return provider