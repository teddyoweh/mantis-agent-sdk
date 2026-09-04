"""Extra request headers reach the wire — for proxy-authenticated endpoints.

``mantis deploy … connect`` hands back ``{"model", "backend", "headers"}``
where ``headers`` may be a Modal proxy-auth pair (``Modal-Key`` /
``Modal-Secret``). Three ways in, one result: ``Agent(extra_headers=…)``,
``MantisAgentOptions(extra_headers=…)`` / the dict option, or the
``MANTIS_AGENT_EXTRA_HEADERS`` JSON env var the terminal reads. Every adapter
that takes ``default_headers`` merges them after its own auth header (an
explicit header wins), so a request to the endpoint carries them verbatim.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest
import respx

from mantis_agent import Agent, MantisAgentOptions
from mantis_agent.providers.base import EXTRA_HEADERS_ENV, extra_headers_from_env
from mantis_agent.providers.modal_provider import ModalProvider, ModalProviderError
from mantis_agent.types import UserMessage

_SSE = (
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


def _request_for(agent: Agent, url: str) -> httpx.Request:
    with respx.mock:
        route = respx.post(url).mock(
            return_value=httpx.Response(200, text=_SSE, headers={"content-type": "text/event-stream"}))

        async def go() -> None:
            async for _ in agent.provider.stream(  # type: ignore[union-attr]
                model=agent.model, messages=[UserMessage(content="hi")], max_tokens=20,
            ):
                pass

        anyio.run(go)
        anyio.run(agent.provider.aclose)  # type: ignore[union-attr]
        return route.calls[0].request


# ---------------------------------------------------------------------------
# Agent(extra_headers=…) → wire
# ---------------------------------------------------------------------------


def test_agent_extra_headers_land_on_openai_compat_requests(monkeypatch) -> None:
    monkeypatch.delenv(EXTRA_HEADERS_ENV, raising=False)
    agent = Agent(
        model="qwen3-8b", backend="https://gpu.example/v1", api_key="k",
        extra_headers={"Modal-Key": "wk-abc", "Modal-Secret": "ws-xyz", "X-Trace": "t1"},
    )
    req = _request_for(agent, "https://gpu.example/v1/chat/completions")
    assert req.headers["modal-key"] == "wk-abc"
    assert req.headers["modal-secret"] == "ws-xyz"
    assert req.headers["x-trace"] == "t1"
    assert req.headers["authorization"] == "Bearer k"  # auth still there


def test_agent_extra_headers_win_over_the_auth_header(monkeypatch) -> None:
    """Explicit headers are merged last — a gateway that wants its own
    ``Authorization`` shape gets it."""
    monkeypatch.delenv(EXTRA_HEADERS_ENV, raising=False)
    agent = Agent(
        model="qwen3-8b", backend="https://gw.example/v1", api_key="k",
        extra_headers={"Authorization": "Bearer wk-1.ws-2"},
    )
    req = _request_for(agent, "https://gw.example/v1/chat/completions")
    assert req.headers["authorization"] == "Bearer wk-1.ws-2"


def test_agent_extra_headers_land_on_modal_requests(monkeypatch) -> None:
    """The deploy manager's ``connect()`` shape: a modal.run backend plus the
    proxy-auth pair as headers, no token env vars at all."""
    for var in ("MODAL_PROXY_TOKEN_ID", "MODAL_PROXY_TOKEN_SECRET",
                "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", EXTRA_HEADERS_ENV):
        monkeypatch.delenv(var, raising=False)
    agent = Agent(
        model="Qwen/Qwen3-8B", backend="https://alice--llm-serve.modal.run/v1",
        extra_headers={"Modal-Key": "wk-abc", "Modal-Secret": "ws-xyz"},
    )
    assert isinstance(agent.provider, ModalProvider)
    req = _request_for(agent, "https://alice--llm-serve.modal.run/v1/chat/completions")
    assert req.headers["modal-key"] == "wk-abc"
    assert req.headers["modal-secret"] == "ws-xyz"
    assert "authorization" not in req.headers  # no unrelated key leaked as Bearer


def test_agent_extra_headers_reach_anthropic(monkeypatch) -> None:
    monkeypatch.delenv(EXTRA_HEADERS_ENV, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-k")
    agent = Agent(model="claude-opus-5", extra_headers={"X-Gateway-Tenant": "acme"})
    headers = {k.lower(): v for k, v in agent.provider.client.headers.items()}  # type: ignore[union-attr]
    assert headers["x-gateway-tenant"] == "acme"
    assert headers["x-api-key"] == "sk-ant-k"
    anyio.run(agent.provider.aclose)  # type: ignore[union-attr]


def test_extra_headers_is_not_in_repr() -> None:
    agent = Agent(model="qwen3-8b", backend="https://gpu.example/v1", api_key="",
                  extra_headers={"Modal-Secret": "ws-super-secret"})
    assert "ws-super-secret" not in repr(agent)
    anyio.run(agent.provider.aclose)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Options → Agent
# ---------------------------------------------------------------------------


def test_typed_options_pass_extra_headers_through(monkeypatch) -> None:
    monkeypatch.delenv(EXTRA_HEADERS_ENV, raising=False)
    from mantis_agent.compat_query import _build_agent

    opts = MantisAgentOptions(
        model="qwen3-8b", backend="https://gpu.example/v1", api_key="",
        extra_headers={"Modal-Key": "wk-1", "Modal-Secret": "ws-2"},
    )
    assert opts.to_query_options()["extra_headers"] == {"Modal-Key": "wk-1", "Modal-Secret": "ws-2"}
    assert "ws-2" not in repr(opts)
    agent = _build_agent(opts.to_query_options())
    assert agent.extra_headers == {"Modal-Key": "wk-1", "Modal-Secret": "ws-2"}
    assert agent.extra is None or "extra_headers" not in agent.extra  # consumed, not leaked
    req = _request_for(agent, "https://gpu.example/v1/chat/completions")
    assert req.headers["modal-key"] == "wk-1"


def test_dict_options_pass_extra_headers_through(monkeypatch) -> None:
    monkeypatch.delenv(EXTRA_HEADERS_ENV, raising=False)
    from mantis_agent.query import _agent_from_options

    agent = _agent_from_options({
        "model": "qwen3-8b", "backend": "https://gpu.example/v1", "api_key": "",
        "extra_headers": {"X-Trace": "t9"},
    })
    assert agent.extra_headers == {"X-Trace": "t9"}
    req = _request_for(agent, "https://gpu.example/v1/chat/completions")
    assert req.headers["x-trace"] == "t9"


# ---------------------------------------------------------------------------
# MANTIS_AGENT_EXTRA_HEADERS env (JSON)
# ---------------------------------------------------------------------------


def test_env_json_applies_when_no_explicit_headers(monkeypatch) -> None:
    monkeypatch.setenv(EXTRA_HEADERS_ENV, json.dumps({"Modal-Key": "wk-env", "Modal-Secret": "ws-env"}))
    agent = Agent(model="qwen3-8b", backend="https://gpu.example/v1", api_key="")
    req = _request_for(agent, "https://gpu.example/v1/chat/completions")
    assert req.headers["modal-key"] == "wk-env"
    assert req.headers["modal-secret"] == "ws-env"


def test_explicit_headers_beat_the_env(monkeypatch) -> None:
    monkeypatch.setenv(EXTRA_HEADERS_ENV, json.dumps({"X-From": "env"}))
    agent = Agent(model="qwen3-8b", backend="https://gpu.example/v1", api_key="",
                  extra_headers={"X-From": "code"})
    req = _request_for(agent, "https://gpu.example/v1/chat/completions")
    assert req.headers["x-from"] == "code"


def test_env_parsing_edge_cases(monkeypatch) -> None:
    assert extra_headers_from_env("") is None
    assert extra_headers_from_env("   ") is None
    assert extra_headers_from_env("{}") is None
    assert extra_headers_from_env('{"X-N": 7}') == {"X-N": "7"}  # scalars stringified
    monkeypatch.delenv(EXTRA_HEADERS_ENV, raising=False)
    assert extra_headers_from_env() is None


@pytest.mark.parametrize(
    "raw,needle",
    [
        ("{not json", "not valid JSON"),
        ('["a", "b"]', "JSON object"),
        ('{"X": {"nested": 1}}', "string value"),
        ('{"X": true}', "string value"),
    ],
)
def test_bad_env_json_is_a_clear_error_not_a_crash(monkeypatch, raw: str, needle: str) -> None:
    with pytest.raises(ValueError) as exc:
        extra_headers_from_env(raw)
    assert EXTRA_HEADERS_ENV in str(exc.value) and needle in str(exc.value)
    # And through Agent, the same clear message rather than a JSONDecodeError
    # from deep inside provider construction.
    monkeypatch.setenv(EXTRA_HEADERS_ENV, raw)
    with pytest.raises(ValueError, match=EXTRA_HEADERS_ENV):
        Agent(model="qwen3-8b", backend="https://gpu.example/v1", api_key="")


# ---------------------------------------------------------------------------
# ModalProvider — proxy token precedence and the joined bearer form
# ---------------------------------------------------------------------------


def _modal_headers(**kw: Any) -> dict[str, str]:
    p = ModalProvider(base_url="https://alice--llm-serve.modal.run/v1", **kw)
    headers = {k.lower(): v for k, v in p.inner_provider.client.headers.items()}
    anyio.run(p.aclose)
    return headers


def _clear_modal_env(monkeypatch) -> None:
    for var in ("MODAL_PROXY_TOKEN_ID", "MODAL_PROXY_TOKEN_SECRET",
                "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_modal_proxy_token_pair_is_read_first(monkeypatch) -> None:
    _clear_modal_env(monkeypatch)
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-api")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-api")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "wk-proxy")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-proxy")
    h = _modal_headers()
    assert (h["modal-key"], h["modal-secret"]) == ("wk-proxy", "ws-proxy")
    assert "authorization" not in h


def test_modal_falls_back_to_the_api_token_pair(monkeypatch) -> None:
    _clear_modal_env(monkeypatch)
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-api")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-api")
    h = _modal_headers()
    assert (h["modal-key"], h["modal-secret"]) == ("ak-api", "as-api")


def test_modal_half_a_proxy_pair_is_an_error_not_a_silent_mix(monkeypatch) -> None:
    _clear_modal_env(monkeypatch)
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "wk-proxy")  # no secret
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-api")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-api")
    with pytest.raises(ModalProviderError, match="BOTH halves"):
        _modal_headers()


def test_modal_constructor_args_beat_the_env(monkeypatch) -> None:
    _clear_modal_env(monkeypatch)
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "wk-proxy")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-proxy")
    h = _modal_headers(token_id="wk-arg", token_secret="ws-arg")
    assert (h["modal-key"], h["modal-secret"]) == ("wk-arg", "ws-arg")


def test_modal_single_wk_key_becomes_a_bearer(monkeypatch) -> None:
    """Modal also accepts the pair joined as one bearer token."""
    _clear_modal_env(monkeypatch)
    h = _modal_headers(api_key="wk-abc.ws-xyz")
    assert h["authorization"] == "Bearer wk-abc.ws-xyz"
    assert "modal-key" not in h


def test_modal_no_credentials_sends_no_auth_and_never_leaks_openai_key(monkeypatch) -> None:
    _clear_modal_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stale")
    h = _modal_headers()
    assert "authorization" not in h and "modal-key" not in h


def test_modal_default_headers_still_merge_and_win(monkeypatch) -> None:
    _clear_modal_env(monkeypatch)
    monkeypatch.setenv("MODAL_PROXY_TOKEN_ID", "wk-env")
    monkeypatch.setenv("MODAL_PROXY_TOKEN_SECRET", "ws-env")
    h = _modal_headers(default_headers={"Modal-Key": "wk-explicit", "X-Trace": "t"})
    assert h["modal-key"] == "wk-explicit"
    assert h["modal-secret"] == "ws-env"
    assert h["x-trace"] == "t"


def test_agent_builds_a_modal_provider_from_a_spec(monkeypatch) -> None:
    """``model="modal:ws/app/fn@served"`` reaches the adapter with the URL
    parts and the served-model name — plus any extra headers."""
    _clear_modal_env(monkeypatch)
    monkeypatch.delenv(EXTRA_HEADERS_ENV, raising=False)
    agent = Agent(model="modal:alice/my-llm/serve@Qwen/Qwen3-8B",
                  extra_headers={"Modal-Key": "wk-1", "Modal-Secret": "ws-2"})
    assert isinstance(agent.provider, ModalProvider)
    assert agent.provider.base_url == "https://alice--my-llm-serve.modal.run/v1"
    assert agent.provider.inner_model == "Qwen/Qwen3-8B"
    h = {k.lower(): v for k, v in agent.provider.inner_provider.client.headers.items()}
    assert (h["modal-key"], h["modal-secret"]) == ("wk-1", "ws-2")
    anyio.run(agent.provider.aclose)
