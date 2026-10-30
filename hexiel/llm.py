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
import re
import os
from collections.abc import Iterator

import httpx
import httpcore
import socket
import threading


class _TrackingBackend(httpcore.SyncBackend):
    """Remembers the sockets it opens so a blocked request can be cut from another thread."""

    def __init__(self, sockets: set, lock: threading.Lock):
        super().__init__()
        self._sockets, self._lock = sockets, lock

    def connect_tcp(self, *args, **kwargs):
        stream = super().connect_tcp(*args, **kwargs)
        sock = stream.get_extra_info("socket")
        if sock is not None:
            with self._lock:
                self._sockets.add(sock)
        return stream


class AbortableHTTP:
    """Mixin for providers: `self.client(timeout)` gives an httpx client whose in-flight request can be
    aborted with `self.abort()` (a read blocked on a silent server — e.g. a long prompt being processed —
    can't otherwise be interrupted). Closing the socket also tells the model server to stop working."""

    def _tracked(self):
        if not hasattr(self, "_socks"):
            self._socks, self._socks_lock = set(), threading.Lock()
        return self._socks, self._socks_lock

    def client(self, timeout: float) -> httpx.Client:
        transport = httpx.HTTPTransport()
        try:
            socks, lock = self._tracked()
            transport._pool._network_backend = _TrackingBackend(socks, lock)
        except AttributeError:        # httpx/httpcore internals changed: still works, just not abortable
            pass
        return httpx.Client(timeout=timeout, transport=transport)

    billing = ""                       # the account this provider's requests are paid from (spend.py); "" = free

    def meter(self, model: str, usage: dict) -> None:
        if self.billing:
            from . import spend
            spend.record(self.billing, model, usage)

    def abort(self) -> None:
        socks, lock = self._tracked()
        with lock:
            live = list(socks)
            socks.clear()
        for sock in live:
            try:
                sock.shutdown(socket.SHUT_RDWR)      # wakes the thread blocked in recv()
            except OSError:
                pass


class ProviderError(RuntimeError):
    pass


class RateLimitError(ProviderError):
    """429 / overloaded. retry_after: seconds until the limit resets (None = unknown).
    long_term: a quota that won't reset soon (monthly cap, billing) — don't sit and wait."""

    def __init__(self, message: str, retry_after: float | None = None, long_term: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.long_term = long_term


class TransientError(RateLimitError):
    """The connection to the model server failed (timeout, refused, dropped):
    retried by the same auto-resume path as a rate limit, after a short pause."""

    def __init__(self, message: str, retry_after: float = 5.0, timed_out: bool = False):
        super().__init__(message, retry_after=retry_after)
        self.timed_out = timed_out


RETRYABLE_STATUS = {429, 503, 529}          # rate limited, unavailable, Anthropic "overloaded"
_LONG_TERM = re.compile(r"monthly|weekly|daily (usage )?limit|insufficient_quota|billing|credit balance|"
                        r"usage limit|quota exceeded|add (usage )?credits", re.I)


def _duration(text: str) -> float | None:
    """'1h2m3.5s' / '6m0s' / '20ms' / '30s' → seconds."""
    parts = re.findall(r"([\d.]+)\s*(ms|h|m|s)", text)
    if not parts:
        return None
    unit = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(n) * unit[u] for n, u in parts)


def retry_after_from(headers, text: str) -> float | None:
    """Seconds until a rate limit resets, from the standard and provider headers or
    the error text. None when nothing says."""
    import datetime
    import email.utils
    now = datetime.datetime.now(datetime.timezone.utc)
    waits: list[float] = []
    raw = (headers.get("retry-after") or "").strip()
    if raw:
        try:
            waits.append(float(raw))
        except ValueError:
            try:
                waits.append((email.utils.parsedate_to_datetime(raw) - now).total_seconds())
            except (TypeError, ValueError):
                pass
    if not waits:
        for name, value in headers.items():
            name = name.lower()
            if name.startswith("anthropic-ratelimit-") and name.endswith("-reset"):
                try:
                    when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    waits.append((when - now).total_seconds())
                except ValueError:
                    pass
            elif name.startswith("x-ratelimit-reset"):
                d = _duration(value)
                if d is not None:
                    waits.append(d)
    if not waits:
        m = re.search(r"(?:try again|retry|reset[s]?)\s+(?:in|after)\s+([\dhms. ]+)", text, re.I)
        if m:
            d = _duration(m.group(1))
            if d is not None:
                waits.append(d)
        m = re.search(r"reset[s]?\s+(?:at|on)\s+(\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:?\d\d)?)", text, re.I)
        if m:
            try:
                when = datetime.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=datetime.timezone.utc)
                waits.append((when - now).total_seconds())
            except ValueError:
                pass
    waits = [w for w in waits if w > 0]
    return max(waits) if waits else None


def raise_for_status(status: int, headers, text: str, who: str) -> None:
    if status in RETRYABLE_STATUS:
        raise RateLimitError(f"{who} {status}: {text[:300]}", retry_after_from(headers, text),
                             long_term=status == 429 and bool(_LONG_TERM.search(text)))
    raise ProviderError(f"{who} {status}: {text[:500]}")


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
            if not call["name"] and not call["id"]:      # a gap left by a sparse index: not a real call
                continue
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


def _mark_cache_tail(messages: list[dict]) -> list[dict]:
    """Put a cache breakpoint on the last content block of the last message."""
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return messages
        content = [{"type": "text", "text": content}]
    if not content:
        return messages
    content = [dict(b) for b in content]
    content[-1]["cache_control"] = {"type": "ephemeral"}
    return messages[:-1] + [{**last, "content": content}]


class AnthropicProvider(AbortableHTTP):
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
            if role == "graphic":
                continue                 # a UI-only card; never a provider message
            if role == "tool":
                blocks = cls._tool_result_blocks(msg)
                prev = converted[-1] if converted else None
                if prev and prev["role"] == "user" and isinstance(prev["content"], list) \
                        and prev["content"] and prev["content"][0]["type"] == "tool_result":
                    # parallel tool calls: every result must ride in the ONE user message that
                    # follows the assistant turn, or Anthropic rejects the earlier tool_use ids
                    # (tool_result blocks first, then any images)
                    at = next((i for i, b in enumerate(prev["content"]) if b["type"] != "tool_result"),
                              len(prev["content"]))
                    prev["content"][at:at] = blocks[:1]
                    prev["content"].extend(blocks[1:])
                else:
                    converted.append({"role": "user", "content": blocks})
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
               timeout: float, **_openai_only) -> Iterator[tuple]:
        body = {
            "model": model,
            "max_tokens": max_output_tokens,
            # prompt caching: tools + system are cached at the system breakpoint, and the
            # conversation up to the newest message at the second — each turn re-reads the
            # history at ~10% of the input price instead of paying for it again
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": _mark_cache_tail(self._convert_messages(messages)),
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
        with self.client(timeout) as client:
            with client.stream("POST", f"{self.base_url}/v1/messages",
                               json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise_for_status(resp.status_code, resp.headers, resp.read().decode(), "anthropic")
                usage = {}
                slots: dict[int, int] = {}      # content-block index -> position in the tool-call list
                for sse in _iter_sse(resp.iter_lines()):
                    data = json.loads(sse)
                    mtype = data.get("type", "")
                    if mtype == "message_start":
                        u = data["message"]["usage"]
                        cached = u.get("cache_read_input_tokens") or 0
                        # with caching, input_tokens only counts the uncached part
                        usage["prompt_tokens"] = (u.get("input_tokens", 0) + cached
                                                  + (u.get("cache_creation_input_tokens") or 0))
                        usage["cached_tokens"] = cached
                        usage["cache_write_tokens"] = u.get("cache_creation_input_tokens") or 0
                    elif mtype == "content_block_start":
                        block = data["content_block"]
                        if block["type"] == "tool_use":
                            # block indexes count text and thinking blocks too: a reply that says something and THEN calls
                            # a tool has the tool at index 1, which must still be the FIRST tool call, not a second one
                            slots[data["index"]] = len(buf.tool_calls)
                            buf.start_call(slots[data["index"]], block["id"], block["name"])
                    elif mtype == "content_block_delta":
                        delta = data["delta"]
                        if delta["type"] == "text_delta":
                            buf.add_text(delta["text"])
                            yield ("text", delta["text"])
                        elif delta["type"] == "input_json_delta" and data.get("index") in slots:
                            buf.add_args(slots[data["index"]], delta["partial_json"])
                    elif mtype == "message_delta":
                        out = data.get("usage", {}).get("output_tokens")
                        if out:
                            usage["completion_tokens"] = out
                self.meter(model, usage)
                yield ("done", usage)
        yield ("assistant", buf.finalize())


class OpenAICompatProvider(AbortableHTTP):
    """Any /chat/completions endpoint: Ollama, vLLM, OpenRouter, OpenAI itself."""

    def __init__(self, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    @classmethod
    def _convert_messages(cls, messages: list[dict], echo_reasoning: bool = False) -> list[dict]:
        converted = []
        for msg in messages:
            if echo_reasoning and msg["role"] == "assistant" and msg.get("reasoning"):
                # providers like DeepSeek/Together/Kimi require prior reasoning on tool turns
                converted.extend(cls._convert_messages([{k: v for k, v in msg.items() if k != "reasoning"}]))
                converted[-1]["reasoning_content"] = msg["reasoning"]
                continue
            if msg["role"] == "graphic":
                continue                 # a UI-only card; never a provider message
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
               timeout: float, echo_reasoning: bool = False,
               max_tokens_param: str = "") -> Iterator[tuple]:
        if not max_tokens_param:   # OpenAI's newer models reject max_tokens
            host = self.base_url.lower()
            openai_style = "api.openai.com" in host or ".openai.azure.com" in host or ".azure.com/openai" in host
            max_tokens_param = "max_completion_tokens" if openai_style else "max_tokens"
        base = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
                        + self._convert_messages(messages, echo_reasoning),
            "stream": True,
            max_tokens_param: max_output_tokens,
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
        with self.client(timeout) as client:
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
                raise_for_status(response.status_code, response.headers, text, "provider")
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
                self.meter(model, usage)
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
    from . import spend
    provider.billing = spend.account_for(model_config)
    return provider