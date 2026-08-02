"""OpenAI's GPT-5 / o-series reject the legacy ``max_tokens`` (they require
``max_completion_tokens``) and only accept the default temperature. mantis's
openai_compat provider handles this in ``_build_payload`` — these lock that in so
a user picking gpt-5.x / o1 / o3 doesn't hit a 400 on the first turn.
"""

from __future__ import annotations

from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.types import UserMessage


def _payload(model: str, *, tools: list[dict] | None = None) -> dict:
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    return p._build_payload(
        model=model,
        messages=[UserMessage(content="hi")],
        system=None,
        tools=tools,
        max_tokens=100,
        temperature=0.7,
        extra=None,
        path="A",
    )


def test_gpt5_and_o_series_use_max_completion_tokens_no_temperature() -> None:
    for model in ("gpt-5.4", "gpt-5.5", "o1", "o1-mini", "o3", "o4-mini"):
        pl = _payload(model)
        assert pl.get("max_completion_tokens") == 100, model
        assert "max_tokens" not in pl, model
        assert "temperature" not in pl, model  # these models reject non-default temp


def test_legacy_openai_models_keep_max_tokens_and_temperature() -> None:
    pl = _payload("gpt-4o")
    assert pl.get("max_tokens") == 100
    assert "max_completion_tokens" not in pl
    assert pl.get("temperature") == 0.7


def test_gpt56_tools_set_reasoning_effort_none() -> None:
    tool = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}
    pl = _payload("gpt-5.6-sol", tools=[tool])
    assert pl.get("reasoning_effort") == "none"


def test_stream_retries_with_max_completion_tokens_on_param_error() -> None:
    # A recent model our name-detection misses (e.g. the bare "chat-latest" alias)
    # 400s on max_tokens; the provider must swap the field and retry, not fail.
    import anyio
    import httpx

    from mantis_agent.providers.openai_compat import OpenAICompatProvider
    from mantis_agent.types import UserMessage

    calls: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        # A gpt-5-era model rejects BOTH legacy params. The token-field error is
        # reported first; if the retry keeps temperature it trips this second 400.
        if b'"max_tokens"' in request.content:
            return httpx.Response(400, json={"error": {"message":
                "Unsupported parameter: 'max_tokens' is not supported with this model. "
                "Use 'max_completion_tokens' instead."}})
        if b'"temperature"' in request.content:
            return httpx.Response(400, json={"error": {"message":
                "Unsupported value: 'temperature' only the default (1) is supported."}})
        sse = ('data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
               'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
               'data: [DONE]\n\n')
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    p.client = httpx.AsyncClient(base_url="https://api.openai.com/v1",
                                 transport=httpx.MockTransport(handler))

    async def go() -> list:
        return [ev async for ev in p.stream(model="chat-latest", temperature=0.7,
                                            messages=[UserMessage(content="hi")], max_tokens=10)]

    anyio.run(go)
    # The one retry must fix BOTH params at once (swap token field + drop temp),
    # so it succeeds on attempt 2 rather than tripping the temperature error.
    assert len(calls) == 2
    assert b"max_completion_tokens" in calls[1] and b'"max_tokens"' not in calls[1]
    assert b'"temperature"' not in calls[1]


def test_stream_retries_setting_reasoning_effort_none_on_tools_error() -> None:
    import anyio
    import httpx

    from mantis_agent.providers.openai_compat import OpenAICompatProvider
    from mantis_agent.types import UserMessage

    calls: list[bytes] = []
    tool = {"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        if b'"reasoning_effort":"none"' not in request.content:
            return httpx.Response(400, json={"error": {"message":
                "Function tools with reasoning_effort are not supported for gpt-5.6-sol in /v1/chat/completions. "
                "To use function tools, use /v1/responses or set reasoning_effort to 'none'."}})
        sse = ('data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
               'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
               'data: [DONE]\n\n')
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    p.client = httpx.AsyncClient(base_url="https://api.openai.com/v1",
                                 transport=httpx.MockTransport(handler))

    async def go() -> list:
        return [ev async for ev in p.stream(model="some-new-model", messages=[UserMessage(content="hi")],
                                            tools=[tool], max_tokens=10)]

    anyio.run(go)
    assert len(calls) == 2
    assert b'"reasoning_effort":"none"' in calls[1]


def test_stream_retries_lowering_max_tokens_on_context_error() -> None:
    import json

    import anyio
    import httpx

    from mantis_agent.providers.openai_compat import OpenAICompatProvider
    from mantis_agent.types import UserMessage

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["max_tokens"] + 12289 > 16384:
            return httpx.Response(400, json={"error": {"message":
                "This model's maximum context length is 16384 tokens. However, "
                f"you requested {payload['max_tokens']} output tokens and your prompt contains at least "
                f"12289 input tokens, for a total of at least {payload['max_tokens'] + 12289} tokens."}})
        sse = ('data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
               'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
               'data: [DONE]\n\n')
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider(base_url="https://modal.example/v1", api_key="x")
    p.client = httpx.AsyncClient(base_url="https://modal.example/v1",
                                 transport=httpx.MockTransport(handler))

    async def go() -> list:
        return [ev async for ev in p.stream(model="zai-org/GLM-4-9B-0414",
                                            messages=[UserMessage(content="hi")],
                                            max_tokens=4096)]

    anyio.run(go)
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 3583


def test_stream_retries_dropping_temperature_when_reported_first() -> None:
    # If OpenAI reports the temperature rejection first (before the token-field
    # one), the retry must still fire — drop temperature and retry.
    import anyio
    import httpx

    from mantis_agent.providers.openai_compat import OpenAICompatProvider
    from mantis_agent.types import UserMessage

    calls: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        if b'"temperature"' in request.content:
            return httpx.Response(400, json={"error": {"message":
                "Unsupported value: 'temperature' does not support 0.7 — only the default (1) is supported."}})
        sse = ('data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
               'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
               'data: [DONE]\n\n')
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    p.client = httpx.AsyncClient(base_url="https://api.openai.com/v1",
                                 transport=httpx.MockTransport(handler))

    async def go() -> list:
        return [ev async for ev in p.stream(model="some-new-model", messages=[UserMessage(content="hi")],
                                            max_tokens=10, temperature=0.7)]

    anyio.run(go)
    assert len(calls) == 2
    assert b'"temperature"' not in calls[1]


def test_openai_reasoning_effort_and_verbosity_extra() -> None:
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    pl = p._build_payload(
        model="gpt-5.5",
        messages=[UserMessage(content="hi")],
        system=None,
        tools=None,
        max_tokens=100,
        temperature=0.7,
        extra={"effort": "high", "verbosity": "high"},
        path="A",
    )
    assert pl["reasoning_effort"] == "high"
    assert pl["verbosity"] == "high"
    assert "effort" not in pl


def test_openai_thinking_aliases_map_to_payload() -> None:
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    pl = p._build_payload(
        model="gpt-5.5",
        messages=[UserMessage(content="hi")],
        system=None,
        tools=None,
        max_tokens=100,
        temperature=0.7,
        extra={"thinking": {"effort": "minimal", "budget_tokens": 2048}},
        path="A",
    )
    assert pl["reasoning_effort"] == "low"
    # The alias maps to effort only; the budget has no Chat Completions field
    # and must not be invented (`400 Unknown parameter: 'max_thinking_tokens'`).
    assert "max_thinking_tokens" not in pl
    assert "thinking" not in pl


def test_gpt56_xhigh_effort_is_allowed_for_chat_payload() -> None:
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    pl = p._build_payload(
        model="gpt-5.6-sol",
        messages=[UserMessage(content="hi")],
        system=None,
        tools=None,
        max_tokens=100,
        temperature=0.7,
        extra={"effort": "xhigh"},
        path="A",
    )
    assert pl["reasoning_effort"] == "xhigh"


def test_gpt56_minimal_aliases_to_low_for_chat_payload() -> None:
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    pl = p._build_payload(
        model="gpt-5.6-sol",
        messages=[UserMessage(content="hi")],
        system=None,
        tools=None,
        max_tokens=100,
        temperature=0.7,
        extra={"effort": "minimal"},
        path="A",
    )
    assert pl["reasoning_effort"] == "low"
