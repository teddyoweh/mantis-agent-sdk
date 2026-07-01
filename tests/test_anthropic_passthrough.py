"""Anthropic passthrough — offline tests.

The adapter ships with one purpose: A/B parity testing of an
mantis-agent-sdk run against real Claude. These tests verify the parts
that don't need live API contact:

* Construction + auth header derivation (env-var fallback, missing-key
  refusal).
* Outbound request building (system field hoisted, message+block
  shape, tool-shape normalization OpenAI→Anthropic).
* Routing — ``detect_provider``, the ``"anthropic"`` sentinel,
  ``hosted_profile_from_url`` for ``api.anthropic.com``.
* SSE → ``StreamEvent`` normalization for every Anthropic event type
  (``message_start``, ``ping``, four block kinds, ``content_block_delta``
  variants for text / thinking / input_json, ``message_delta``,
  ``message_stop``, ``error``).
* HTTP error mapping (401 → AuthError, 5xx → ProviderError).
* Agent-level integration: ``backend="https://api.anthropic.com/v1"``
  builds an ``AnthropicPassthroughProvider`` instance.

The companion ``test_real_anthropic.py`` (skipped by default) would
exercise a live ``ANTHROPIC_API_KEY``; we don't ship that here.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest

from mantis_agent.capabilities import HOSTED_PROFILES, hosted_profile_from_url
from mantis_agent.errors import AuthError, ProviderError
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    ErrorEvent,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
    ThinkingDelta,
)
from mantis_agent.providers.anthropic_passthrough import (
    ANTHROPIC_DEFAULT_BASE_URL,
    ANTHROPIC_DEFAULT_VERSION,
    AnthropicPassthroughProvider,
    _encode_block,
    _encode_message,
    _normalize_base_url,
    _normalize_tools,
    _split_system,
)
from mantis_agent.providers.base import detect_provider, resolve
from mantis_agent.types import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    Usage,
)


# ---------------------------------------------------------------------------
# Routing + registry
# ---------------------------------------------------------------------------


class TestAnthropicRouting:
    def test_detect_provider_routes_api_url_to_passthrough(self) -> None:
        assert (
            detect_provider("https://api.anthropic.com/v1")
            == "anthropic_passthrough"
        )

    def test_detect_provider_routes_sentinel_to_passthrough(self) -> None:
        assert detect_provider("anthropic") == "anthropic_passthrough"

    def test_detect_provider_case_insensitive(self) -> None:
        assert (
            detect_provider("HTTPS://API.ANTHROPIC.COM/v1")
            == "anthropic_passthrough"
        )

    def test_bare_claude_model_does_not_route_here(self) -> None:
        """We don't auto-route claude-* model names to this adapter —
        that would undermine the "don't proxy Anthropic" stance."""
        # claude-* falls through to the bare-name openai_compat default.
        assert detect_provider("claude-sonnet-4-5") == "openai_compat"

    def test_registry_resolves_passthrough(self) -> None:
        cls = resolve("anthropic_passthrough")
        assert cls is AnthropicPassthroughProvider

    def test_hosted_profile_for_anthropic_url(self) -> None:
        cap = hosted_profile_from_url("https://api.anthropic.com/v1")
        assert cap is not None
        assert cap is HOSTED_PROFILES["anthropic"]
        assert cap.kind == "anthropic"
        assert cap.provider_hint == "anthropic"
        assert cap.supports_native_tools is True
        assert cap.supports_grammar is False  # Anthropic has no grammar mode
        assert cap.supports_prefix_caching is True


# ---------------------------------------------------------------------------
# Construction + auth
# ---------------------------------------------------------------------------


class TestProviderInit:
    def _headers(self, p: AnthropicPassthroughProvider) -> dict[str, str]:
        return {k.lower(): v for k, v in p.client.headers.items()}

    def test_construct_with_explicit_key(self) -> None:
        p = AnthropicPassthroughProvider(api_key="sk-ant-test")
        h = self._headers(p)
        assert h["x-api-key"] == "sk-ant-test"
        assert h["anthropic-version"] == ANTHROPIC_DEFAULT_VERSION
        assert h["content-type"] == "application/json"
        assert p.base_url == ANTHROPIC_DEFAULT_BASE_URL
        assert p.backend_capability.provider_hint == "anthropic"
        anyio.run(p.aclose)

    def test_construct_from_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
        p = AnthropicPassthroughProvider()
        assert self._headers(p)["x-api-key"] == "sk-ant-env"
        anyio.run(p.aclose)

    def test_missing_key_raises_auth_error(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(AuthError, match="API key"):
            AnthropicPassthroughProvider()

    def test_anthropic_version_override(self) -> None:
        p = AnthropicPassthroughProvider(
            api_key="sk-ant-test", anthropic_version="2024-10-01"
        )
        assert self._headers(p)["anthropic-version"] == "2024-10-01"
        anyio.run(p.aclose)

    def test_anthropic_beta_header(self) -> None:
        p = AnthropicPassthroughProvider(
            api_key="sk-ant-test",
            anthropic_beta="prompt-caching-2024-07-31,tools-2024-04-04",
        )
        h = self._headers(p)
        assert h["anthropic-beta"].startswith("prompt-caching")
        anyio.run(p.aclose)

    def test_base_url_normalization_strips_messages_suffix(self) -> None:
        p = AnthropicPassthroughProvider(
            api_key="sk-ant-test",
            base_url="https://api.anthropic.com/v1/messages/",
        )
        assert p.base_url == "https://api.anthropic.com/v1"
        anyio.run(p.aclose)

    def test_base_url_normalization_strips_trailing_slash(self) -> None:
        assert _normalize_base_url("https://api.anthropic.com/v1/") == (
            "https://api.anthropic.com/v1"
        )

    def test_capability_override(self) -> None:
        from mantis_agent.capabilities import BackendCapability

        custom = BackendCapability(
            kind="anthropic",
            supports_native_tools=True,
            supports_grammar=False,
            provider_hint="my-anthropic",
        )
        p = AnthropicPassthroughProvider(
            api_key="sk-ant-test", backend_capability=custom
        )
        assert p.backend_capability is custom
        anyio.run(p.aclose)


# ---------------------------------------------------------------------------
# Outbound shape
# ---------------------------------------------------------------------------


class TestSystemHoisting:
    def test_explicit_system_wins(self) -> None:
        msgs = [
            SystemMessage(content="ignored-because-explicit"),
            UserMessage(content="hi"),
        ]
        sys_text, body = _split_system("from-arg", msgs)
        assert sys_text == "from-arg"
        # Explicit system also strips any SystemMessage from the body —
        # don't double-spend the budget.
        assert all(not isinstance(m, SystemMessage) for m in body)

    def test_system_messages_hoisted_when_no_explicit(self) -> None:
        msgs = [
            SystemMessage(content="be brief"),
            UserMessage(content="hi"),
        ]
        sys_text, body = _split_system(None, msgs)
        assert sys_text == "be brief"
        assert len(body) == 1 and isinstance(body[0], UserMessage)

    def test_multiple_system_messages_concatenated(self) -> None:
        msgs = [
            SystemMessage(content="rule A"),
            SystemMessage(content="rule B"),
            UserMessage(content="hi"),
        ]
        sys_text, _ = _split_system(None, msgs)
        assert sys_text == "rule A\n\nrule B"

    def test_system_message_textblock_content(self) -> None:
        msgs = [
            SystemMessage(content=[TextBlock(text="rule A")]),
            UserMessage(content="hi"),
        ]
        sys_text, _ = _split_system(None, msgs)
        assert sys_text == "rule A"

    def test_no_system_at_all(self) -> None:
        msgs = [UserMessage(content="hi")]
        sys_text, body = _split_system(None, msgs)
        assert sys_text is None
        assert len(body) == 1


class TestMessageEncoding:
    def test_user_string_content(self) -> None:
        out = _encode_message(UserMessage(content="hi"))
        assert out == {"role": "user", "content": "hi"}

    def test_user_blocks(self) -> None:
        msg = UserMessage(content=[TextBlock(text="hi")])
        assert _encode_message(msg) == {
            "role": "user",
            "content": [{"type": "text", "text": "hi"}],
        }

    def test_assistant_with_tool_use(self) -> None:
        msg = AssistantMessage(
            content=[
                TextBlock(text="let me check"),
                ToolUseBlock(id="toolu_1", name="lookup", input={"q": "x"}),
            ]
        )
        out = _encode_message(msg)
        assert out["role"] == "assistant"
        assert out["content"][0] == {"type": "text", "text": "let me check"}
        assert out["content"][1] == {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "lookup",
            "input": {"q": "x"},
        }

    def test_tool_result_string(self) -> None:
        block = ToolResultBlock(tool_use_id="toolu_1", content="42")
        out = _encode_block(block)
        assert out == {"type": "tool_result", "tool_use_id": "toolu_1", "content": "42"}

    def test_tool_result_with_blocks(self) -> None:
        block = ToolResultBlock(
            tool_use_id="toolu_1",
            content=[TextBlock(text="some result")],
            is_error=False,
        )
        out = _encode_block(block)
        assert out == {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": [{"type": "text", "text": "some result"}],
        }

    def test_tool_result_error_flag(self) -> None:
        block = ToolResultBlock(
            tool_use_id="toolu_1", content="oops", is_error=True
        )
        out = _encode_block(block)
        assert out["is_error"] is True

    def test_thinking_block_encoded(self) -> None:
        block = ThinkingBlock(thinking="hmm", signature="sig-abc")
        out = _encode_block(block)
        assert out == {"type": "thinking", "thinking": "hmm", "signature": "sig-abc"}

    def test_text_block_with_cache_control(self) -> None:
        block = TextBlock(text="big chunk", cache_control={"type": "ephemeral"})
        out = _encode_block(block)
        assert out["cache_control"] == {"type": "ephemeral"}

    def test_system_message_raises_in_body_encoder(self) -> None:
        with pytest.raises(ProviderError, match="system messages"):
            _encode_message(SystemMessage(content="not allowed here"))


class TestToolNormalization:
    def test_openai_shape_to_anthropic_shape(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "add",
                    "description": "Add two numbers",
                    "parameters": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                    },
                },
            }
        ]
        out = _normalize_tools(tools)
        assert len(out) == 1
        assert out[0]["name"] == "add"
        assert out[0]["description"] == "Add two numbers"
        assert out[0]["input_schema"]["properties"]["a"]["type"] == "number"
        assert "function" not in out[0]
        assert "type" not in out[0]

    def test_anthropic_shape_passthrough(self) -> None:
        tools = [
            {
                "name": "lookup",
                "description": "look something up",
                "input_schema": {"type": "object"},
            }
        ]
        out = _normalize_tools(tools)
        assert out == tools  # unchanged

    def test_parameters_field_accepted_as_input_schema(self) -> None:
        tools = [
            {
                "name": "lookup",
                "description": "look",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ]
        out = _normalize_tools(tools)
        assert out[0]["input_schema"]["properties"]["q"]["type"] == "string"


# ---------------------------------------------------------------------------
# SSE → StreamEvent normalization
# ---------------------------------------------------------------------------


def _sse(event_name: str, payload: dict) -> bytes:
    return (
        f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode()
    )


_CANONICAL_STREAM = (
    _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 0},
            },
        },
    )
    + _sse("ping", {"type": "ping"})  # heartbeat — ignored
    + _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    + _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello, "},
        },
    )
    + _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Claude."},
        },
    )
    + _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    + _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "add",
                "input": {},
            },
        },
    )
    + _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": "{\"a\":1,",
            },
        },
    )
    + _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": "\"b\":2}",
            },
        },
    )
    + _sse("content_block_stop", {"type": "content_block_stop", "index": 1})
    + _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 23},
        },
    )
    + _sse("message_stop", {"type": "message_stop"})
)


def _record_request(
    requests_out: list[httpx.Request], stream_body: bytes
):
    def handler(request: httpx.Request) -> httpx.Response:
        requests_out.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream_body,
        )

    return handler


async def _provider_with_handler(handler) -> AnthropicPassthroughProvider:
    """Construct a provider and swap its inner client for one wired to a
    MockTransport handler. Must be awaited from inside the event loop
    so we can ``await`` the original-client close — anyio.run inside
    anyio.run blows up."""

    p = AnthropicPassthroughProvider(api_key="sk-ant-test")
    original_headers = dict(p.client.headers)
    await p.client.aclose()
    p.client = httpx.AsyncClient(
        base_url=p.base_url,
        transport=httpx.MockTransport(handler),
        headers=original_headers,
    )
    return p


class TestStreaming:

    def test_canonical_stream_normalizes_to_internal_events(self) -> None:
        async def go() -> None:
            seen: list[httpx.Request] = []
            handler = _record_request(seen, _CANONICAL_STREAM)
            p = await _provider_with_handler(handler)

            events: list[Any] = []
            async for ev in p.stream(
                model="claude-sonnet-4-5",
                messages=[UserMessage(content="add 1 and 2")],
                max_tokens=64,
            ):
                events.append(ev)
            await p.aclose()

            # Sanity check the request shape.
            assert len(seen) == 1
            req = seen[0]
            assert req.url.path.endswith("/messages")
            assert req.headers["x-api-key"] == "sk-ant-test"
            assert "anthropic-version" in req.headers
            body = json.loads(req.content.decode("utf-8"))
            assert body["model"] == "claude-sonnet-4-5"
            assert body["max_tokens"] == 64
            assert body["stream"] is True
            assert body["messages"][0]["role"] == "user"

            # Event-level expectations.
            kinds = [type(e).__name__ for e in events]
            assert kinds[0] == "MessageStart"
            assert kinds[-1] == "MessageStop"

            # ping must NOT appear as a yielded event.
            assert all(type(e).__name__ != "Ping" for e in events)

            # The MessageStart should carry the message id + model.
            assert events[0].message_id == "msg_01"
            assert events[0].model == "claude-sonnet-4-5"

            # Text deltas concatenated should produce "Hello, Claude.".
            text_pieces = [
                e.delta.text
                for e in events
                if isinstance(e, ContentBlockDelta)
                and isinstance(e.delta, TextDelta)
            ]
            assert "".join(text_pieces) == "Hello, Claude."

            # input_json deltas land verbatim, in order.
            json_pieces = [
                e.delta.partial_json
                for e in events
                if isinstance(e, ContentBlockDelta)
                and isinstance(e.delta, InputJsonDelta)
            ]
            assert "".join(json_pieces) == '{"a":1,"b":2}'

            # Both content blocks open + close.
            starts = [e for e in events if isinstance(e, ContentBlockStart)]
            stops = [e for e in events if isinstance(e, ContentBlockStop)]
            assert [s.index for s in starts] == [0, 1]
            assert [s.index for s in stops] == [0, 1]

            # message_delta with stop_reason + usage should land.
            mds = [e for e in events if isinstance(e, MessageDelta)]
            assert any(md.stop_reason == "tool_use" for md in mds)
            assert any(
                md.usage is not None and md.usage.output_tokens == 23
                for md in mds
            )

        anyio.run(go)

    def test_thinking_delta_event(self) -> None:
        stream = (
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_th",
                        "role": "assistant",
                        "model": "claude-opus-4",
                        "usage": {"input_tokens": 4, "output_tokens": 0},
                    },
                },
            )
            + _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
            + _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "First I will...",
                    },
                },
            )
            + _sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            )
            + _sse("message_stop", {"type": "message_stop"})
        )

        async def go() -> None:
            handler = _record_request([], stream)
            p = await _provider_with_handler(handler)
            events = []
            async for ev in p.stream(
                model="claude-opus-4",
                messages=[UserMessage(content="think hard")],
            ):
                events.append(ev)
            await p.aclose()

            thinking = [
                e
                for e in events
                if isinstance(e, ContentBlockDelta)
                and isinstance(e.delta, ThinkingDelta)
            ]
            assert len(thinking) == 1
            assert thinking[0].delta.thinking == "First I will..."

        anyio.run(go)

    def test_error_event_in_stream(self) -> None:
        stream = _sse(
            "error",
            {
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": "we are full",
                },
            },
        )

        async def go() -> None:
            handler = _record_request([], stream)
            p = await _provider_with_handler(handler)
            events = []
            async for ev in p.stream(
                model="claude-sonnet-4-5",
                messages=[UserMessage(content="hi")],
            ):
                events.append(ev)
            await p.aclose()

            err_events = [e for e in events if isinstance(e, ErrorEvent)]
            assert len(err_events) == 1
            assert err_events[0].error_type == "overloaded_error"
            assert err_events[0].message == "we are full"

        anyio.run(go)

    def test_system_field_hoisted_in_request_body(self) -> None:
        async def go() -> None:
            seen: list[httpx.Request] = []
            handler = _record_request(seen, _CANONICAL_STREAM)
            p = await _provider_with_handler(handler)
            events = []
            async for ev in p.stream(
                model="claude-sonnet-4-5",
                messages=[
                    SystemMessage(content="be brief"),
                    UserMessage(content="hi"),
                ],
            ):
                events.append(ev)
            await p.aclose()

            body = json.loads(seen[0].content.decode("utf-8"))
            # System is hoisted; with prompt caching on (default) it's a content
            # array carrying an ephemeral cache breakpoint.
            assert body["system"] == [
                {"type": "text", "text": "be brief",
                 "cache_control": {"type": "ephemeral"}}
            ]
            assert all(m["role"] != "system" for m in body["messages"])

        anyio.run(go)

    def test_tools_field_normalized_in_request(self) -> None:
        async def go() -> None:
            seen: list[httpx.Request] = []
            handler = _record_request(seen, _CANONICAL_STREAM)
            p = await _provider_with_handler(handler)
            events = []
            async for ev in p.stream(
                model="claude-sonnet-4-5",
                messages=[UserMessage(content="hi")],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "description": "look",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            ):
                events.append(ev)
            await p.aclose()

            body = json.loads(seen[0].content.decode("utf-8"))
            assert body["tools"][0]["name"] == "lookup"
            assert body["tools"][0]["input_schema"] == {"type": "object"}
            assert "function" not in body["tools"][0]

        anyio.run(go)

    def test_empty_model_raises(self) -> None:
        async def go() -> None:
            p = AnthropicPassthroughProvider(api_key="sk-ant-test")
            with pytest.raises(ProviderError, match="model name"):
                async for _ in p.stream(model="", messages=[UserMessage(content="hi")]):
                    pass
            await p.aclose()

        anyio.run(go)


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


class TestHttpErrors:
    def _provider(self, handler) -> AnthropicPassthroughProvider:
        p = AnthropicPassthroughProvider(api_key="sk-ant-test")
        original_headers = dict(p.client.headers)
        anyio.run(p.client.aclose)
        p.client = httpx.AsyncClient(
            base_url=p.base_url,
            transport=httpx.MockTransport(handler),
            headers=original_headers,
        )
        return p

    def test_401_raises_auth_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": {"type": "authentication_error", "message": "bad key"}},
            )

        async def go() -> None:
            p = await _provider_with_handler(handler)
            with pytest.raises(AuthError, match="bad key"):
                async for _ in p.stream(
                    model="claude-sonnet-4-5",
                    messages=[UserMessage(content="hi")],
                ):
                    pass
            await p.aclose()

        anyio.run(go)

    def test_500_raises_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500,
                json={"error": {"type": "api_error", "message": "boom"}},
            )

        async def go() -> None:
            p = await _provider_with_handler(handler)
            with pytest.raises(ProviderError, match="boom"):
                async for _ in p.stream(
                    model="claude-sonnet-4-5",
                    messages=[UserMessage(content="hi")],
                ):
                    pass
            await p.aclose()

        anyio.run(go)

    def test_429_raises_provider_error_with_text_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=b"slow down")

        async def go() -> None:
            p = await _provider_with_handler(handler)
            with pytest.raises(ProviderError, match="slow down"):
                async for _ in p.stream(
                    model="claude-sonnet-4-5",
                    messages=[UserMessage(content="hi")],
                ):
                    pass
            await p.aclose()

        anyio.run(go)


# ---------------------------------------------------------------------------
# Agent-level wiring
# ---------------------------------------------------------------------------


class TestAgentBuildsProvider:
    def test_anthropic_api_url_builds_passthrough_provider(
        self, monkeypatch
    ) -> None:
        from mantis_agent import Agent

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        agent = Agent(
            model="claude-sonnet-4-5",
            backend="https://api.anthropic.com/v1",
        )
        assert isinstance(agent.provider, AnthropicPassthroughProvider)
        assert agent.provider.base_url == "https://api.anthropic.com/v1"
        anyio.run(agent.provider.aclose)

    def test_sentinel_backend_builds_passthrough_provider(
        self, monkeypatch
    ) -> None:
        from mantis_agent import Agent

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        agent = Agent(
            model="claude-sonnet-4-5",
            backend="anthropic",
        )
        assert isinstance(agent.provider, AnthropicPassthroughProvider)
        # Sentinel uses the provider's default base URL.
        assert agent.provider.base_url == ANTHROPIC_DEFAULT_BASE_URL
        anyio.run(agent.provider.aclose)
