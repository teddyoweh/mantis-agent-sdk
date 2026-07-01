"""OpenAI's GPT-5 / o-series reject the legacy ``max_tokens`` (they require
``max_completion_tokens``) and only accept the default temperature. mantis's
openai_compat provider handles this in ``_build_payload`` — these lock that in so
a user picking gpt-5.x / o1 / o3 doesn't hit a 400 on the first turn.
"""

from __future__ import annotations

from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.types import UserMessage


def _payload(model: str) -> dict:
    p = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="x")
    return p._build_payload(
        model=model,
        messages=[UserMessage(content="hi")],
        system=None,
        tools=None,
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
