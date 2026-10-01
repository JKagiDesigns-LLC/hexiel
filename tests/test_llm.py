

# ---------------------------------------------------------------- provider quirks

def _captured_payload(base_url, **kw):
    """Run OpenAICompatProvider.stream until it sends, capturing the JSON body."""
    import httpx
    from hexiel.llm import OpenAICompatProvider
    seen = {}

    def fake_send(self, request, stream=False):
        import json as _json
        seen["body"] = _json.loads(request.content)
        raise httpx.ConnectError("captured", request=request)
    orig = httpx.Client.send
    httpx.Client.send = fake_send
    try:
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "", "reasoning": "I should grep.",
                 "tool_calls": [{"id": "c1", "name": "grep", "arguments": {"pattern": "x"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "hit"}]
        try:
            list(OpenAICompatProvider("k", base_url).stream(
                msgs, "sys", [], model="m", max_output_tokens=123, extra_body={}, timeout=5, **kw))
        except Exception:  # noqa: BLE001 - the fake transport always fails after capturing
            pass
    finally:
        httpx.Client.send = orig
    return seen["body"]


def test_openai_hosts_get_max_completion_tokens():
    assert _captured_payload("https://api.openai.com/v1")["max_completion_tokens"] == 123
    assert "max_tokens" not in _captured_payload("https://api.openai.com/v1")
    assert _captured_payload("https://my.openai.azure.com/openai/v1")["max_completion_tokens"] == 123
    assert _captured_payload("http://localhost:11434/v1")["max_tokens"] == 123
    assert _captured_payload("http://x/v1", max_tokens_param="max_completion_tokens")["max_completion_tokens"] == 123


def test_reasoning_echoed_only_when_enabled():
    plain = _captured_payload("https://api.deepseek.com")
    assert all("reasoning_content" not in m and "reasoning" not in m for m in plain["messages"])
    echoed = _captured_payload("https://api.deepseek.com", echo_reasoning=True)
    asst = [m for m in echoed["messages"] if m["role"] == "assistant"][0]
    assert asst["reasoning_content"] == "I should grep." and asst["tool_calls"]
