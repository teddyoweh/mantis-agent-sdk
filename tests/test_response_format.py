"""Structured-output ``response_format`` plumbing.

Coverage map:

* :class:`TestNormalize` — pure validation / canonicalization of user input.
* :class:`TestTranslate` — per-backend translation rules (OpenAI envelope,
  Ollama native ``format``, TGI grammar, mock, anthropic-passthrough rejection).
* :class:`TestAgentWiring` — ``Agent(response_format=...)`` forwards to the
  provider via ``extra``; bad shapes blow up at construct time.
* :class:`TestMantisAgentOptionsWiring` — ``MantisAgentOptions(...)`` →
  ``to_query_options()`` → ``_agent_from_options`` round-trip.
* :class:`TestOpenAIWire` — captures the real httpx request body to assert the
  envelope reaches the wire intact.
* :class:`TestOllamaWire` — same idea for Ollama's ``format`` translation.
* :class:`TestPublicAPI` — exports show up at top level for SemVer guarantee.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mantis_agent import (
    Agent,
    MantisAgentOptions,
    ResponseFormatError,
    normalize_response_format,
    translate_response_format,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.providers.ollama import OllamaProvider
from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.types import UserMessage


# ---------------------------------------------------------------------------
# Test fixtures — small SSE bodies the mock transports replay.
# ---------------------------------------------------------------------------


_OPENAI_SSE_BODY = (
    b'data: {"id":"x","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n'
    b'data: {"id":"x","choices":[{"delta":{"content":"{\\"ok\\":true}"},"index":0}]}\n\n'
    b'data: {"id":"x","choices":[{"delta":{},"index":0,"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":1,"completion_tokens":3,"total_tokens":4}}\n\n'
    b"data: [DONE]\n\n"
)


# Ollama emits NDJSON, one JSON object per line.
_OLLAMA_NDJSON_BODY = (
    b'{"model":"qwen","created_at":"2026-05-17T00:00:00Z",'
    b'"message":{"role":"assistant","content":"{\\"ok\\":true}"},"done":false}\n'
    b'{"model":"qwen","created_at":"2026-05-17T00:00:00Z","message":{"role":"assistant","content":""},'
    b'"done":true,"prompt_eval_count":1,"eval_count":3,'
    b'"total_duration":1,"load_duration":1,"prompt_eval_duration":1,"eval_duration":1}\n'
)


# ---------------------------------------------------------------------------
# Pure normalization
# ---------------------------------------------------------------------------


class TestNormalize:

    def test_json_object(self) -> None:
        kind, payload = normalize_response_format({"type": "json_object"})
        assert kind == "json_object"
        assert payload is None

    def test_json_schema_openai_envelope(self) -> None:
        rf = {
            "type": "json_schema",
            "json_schema": {
                "name": "Color",
                "schema": {"type": "object", "properties": {"hex": {"type": "string"}}},
                "strict": True,
            },
        }
        kind, payload = normalize_response_format(rf)
        assert kind == "json_schema"
        assert payload is not None
        assert payload["name"] == "Color"
        assert payload["strict"] is True
        assert payload["schema"]["properties"]["hex"]["type"] == "string"

    def test_json_schema_flat_shortcut(self) -> None:
        """``{"type":"json_schema","schema":{...}}`` should canonicalize the
        same as the OpenAI nested form. This is the form we want most users
        to type."""

        rf = {"type": "json_schema", "schema": {"type": "object"}}
        kind, payload = normalize_response_format(rf)
        assert kind == "json_schema"
        assert payload is not None
        assert payload["schema"] == {"type": "object"}
        assert payload["name"] is None
        assert payload["strict"] is None

    def test_json_schema_type_inferred_from_schema_key(self) -> None:
        """When ``type`` is omitted but ``schema``/``json_schema`` is present,
        infer ``type='json_schema'`` instead of barking at the user."""

        rf = {"schema": {"type": "object"}}
        kind, _ = normalize_response_format(rf)
        assert kind == "json_schema"

    def test_none_input_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="None"):
            normalize_response_format(None)

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="must be a dict"):
            normalize_response_format("json_object")  # type: ignore[arg-type]

    def test_missing_type_no_schema_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="missing 'type'"):
            normalize_response_format({})

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="unknown response_format type"):
            normalize_response_format({"type": "yaml_blob"})

    def test_json_object_with_extra_keys_rejected(self) -> None:
        """``json_object`` doesn't take extra fields — the user almost
        certainly meant ``json_schema`` if they're putting one there."""

        with pytest.raises(ResponseFormatError, match="json_object accepts no other keys"):
            normalize_response_format(
                {"type": "json_object", "schema": {"type": "object"}}
            )

    def test_json_schema_without_schema_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="requires a 'schema'"):
            normalize_response_format({"type": "json_schema"})

    def test_json_schema_bad_name_type_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="name must be a string"):
            normalize_response_format(
                {"type": "json_schema", "schema": {"type": "object"}, "name": 7}
            )

    def test_json_schema_bad_strict_type_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="strict must be a bool"):
            normalize_response_format(
                {"type": "json_schema", "schema": {"type": "object"}, "strict": "yes"}
            )

    def test_json_schema_nested_not_dict_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="must be a dict"):
            normalize_response_format(
                {"type": "json_schema", "json_schema": "not a dict"}
            )


# ---------------------------------------------------------------------------
# Per-backend translation
# ---------------------------------------------------------------------------


class TestTranslate:

    def test_openai_compat_json_object(self) -> None:
        out = translate_response_format(
            {"type": "json_object"}, "openai_compat"
        )
        assert out == {"response_format": {"type": "json_object"}}

    def test_openai_compat_json_schema_defaults_name(self) -> None:
        """A user who passes a flat ``{"schema": ...}`` should still get a
        non-empty ``name`` on the wire — strict mode rejects empty names."""

        out = translate_response_format(
            {"type": "json_schema", "schema": {"type": "object"}}, "openai_compat"
        )
        rf = out["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "response"
        assert rf["json_schema"]["schema"] == {"type": "object"}
        # ``strict`` only on the wire when the user opted in.
        assert "strict" not in rf["json_schema"]

    def test_openai_compat_json_schema_keeps_name_and_strict(self) -> None:
        out = translate_response_format(
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "Person",
                    "schema": {"type": "object"},
                    "strict": True,
                },
            },
            "openai_compat",
        )
        rf = out["response_format"]["json_schema"]
        assert rf["name"] == "Person"
        assert rf["strict"] is True

    def test_llamacpp_uses_openai_envelope(self) -> None:
        out = translate_response_format({"type": "json_object"}, "llamacpp")
        assert "response_format" in out

    def test_modal_uses_openai_envelope(self) -> None:
        out = translate_response_format({"type": "json_object"}, "modal")
        assert "response_format" in out

    def test_ollama_json_object_to_string(self) -> None:
        out = translate_response_format({"type": "json_object"}, "ollama")
        assert out == {"format": "json"}

    def test_ollama_json_schema_to_raw_schema(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        out = translate_response_format(
            {"type": "json_schema", "schema": schema}, "ollama"
        )
        assert out == {"format": schema}

    def test_tgi_json_object_emits_grammar(self) -> None:
        out = translate_response_format({"type": "json_object"}, "tgi")
        assert out == {"parameters": {"grammar": {"type": "json", "value": {}}}}

    def test_tgi_json_schema_emits_grammar_value(self) -> None:
        schema = {"type": "object"}
        out = translate_response_format(
            {"type": "json_schema", "schema": schema}, "tgi"
        )
        assert out["parameters"]["grammar"]["value"] == schema

    def test_anthropic_passthrough_loud_rejection(self) -> None:
        """Real Anthropic API has no response_format — silently dropping it
        would have the user wondering why their JSON-mode prompts come back
        as prose. Be loud."""

        with pytest.raises(ResponseFormatError, match="anthropic_passthrough"):
            translate_response_format(
                {"type": "json_object"}, "anthropic_passthrough"
            )

    def test_mock_uses_openai_envelope(self) -> None:
        """Tests inspecting Mock's ``last_extra`` should see the canonical
        OpenAI shape — same as a real openai_compat call would."""

        out = translate_response_format(
            {"type": "json_object"}, "mock"
        )
        assert out == {"response_format": {"type": "json_object"}}

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ResponseFormatError, match="does not yet support"):
            translate_response_format({"type": "json_object"}, "fictional")


# ---------------------------------------------------------------------------
# Agent construction + provider plumbing
# ---------------------------------------------------------------------------


class TestAgentWiring:

    def _agent(self, **kw: Any) -> tuple[Agent, MockProvider]:
        p = MockProvider()
        a = Agent(
            model="qwen2.5-7b-instruct",
            provider=p,
            include_memory=False,
            **kw,
        )
        return a, p

    def test_construct_accepts_valid_response_format(self) -> None:
        a, _ = self._agent(response_format={"type": "json_object"})
        assert a.response_format == {"type": "json_object"}

    def test_construct_rejects_invalid_response_format(self) -> None:
        with pytest.raises(ResponseFormatError):
            self._agent(response_format={"type": "bogus"})

    def test_construct_without_response_format_is_a_noop(self) -> None:
        """Default ``None`` must not change extra. Regression guard against
        accidentally forwarding ``response_format=None`` to providers."""

        a, p = self._agent(extra={"some_user_key": 1})

        async def go() -> None:
            async for _ in a._provider_stream([UserMessage(content="hi")]):
                pass

        import anyio
        anyio.run(go)
        assert p.last_extra == {"some_user_key": 1}

    def test_response_format_reaches_mock_provider_as_openai_envelope(self) -> None:
        a, p = self._agent(
            response_format={
                "type": "json_schema",
                "schema": {"type": "object", "properties": {"hex": {"type": "string"}}},
            }
        )

        async def go() -> None:
            async for _ in a._provider_stream([UserMessage(content="pick a color")]):
                pass

        import anyio
        anyio.run(go)

        assert p.last_extra is not None
        rf = p.last_extra["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "response"
        assert rf["json_schema"]["schema"]["properties"]["hex"]["type"] == "string"

    def test_response_format_merged_with_user_extras(self) -> None:
        """Both the user-supplied ``extra={"scenario": ...}`` and the
        translated response_format should reach the provider in one extra dict."""

        a, p = self._agent(
            extra={"scenario": "sunny"},
            response_format={"type": "json_object"},
        )

        async def go() -> None:
            async for _ in a._provider_stream([UserMessage(content="hi")]):
                pass

        import anyio
        anyio.run(go)

        assert p.last_extra is not None
        assert p.last_extra["scenario"] == "sunny"
        assert p.last_extra["response_format"] == {"type": "json_object"}

    def test_explicit_extra_response_format_wins(self) -> None:
        """Power-user escape hatch: if ``Agent.extra`` itself sets a
        ``response_format`` key, that overrides the translated shape so
        weird backends with custom wire forms can be supported."""

        a, p = self._agent(
            extra={"response_format": {"type": "custom_wire_shape"}},
            response_format={"type": "json_object"},
        )

        async def go() -> None:
            async for _ in a._provider_stream([UserMessage(content="hi")]):
                pass

        import anyio
        anyio.run(go)

        assert p.last_extra is not None
        assert p.last_extra["response_format"] == {"type": "custom_wire_shape"}

    def test_extra_options_dict_merges_deeply(self) -> None:
        """Per-key deep merge: the response_format translator may emit
        ``parameters={"grammar":...}``; the user may have set
        ``parameters={"seed": 42}`` — both should survive."""

        from mantis_agent import response_format as rf_mod

        # Use the TGI translation directly to exercise the parameters path
        # without spinning up a TGI provider.
        out = rf_mod.translate_response_format({"type": "json_object"}, "tgi")
        assert "parameters" in out

        # Now exercise the same merge logic via Agent on a mock provider —
        # we simulate by manually injecting an extra with "parameters":
        a, p = self._agent(
            extra={"parameters": {"seed": 42}},
            response_format={"type": "json_object"},
        )

        # Patch the Agent's provider to claim "tgi" for the merge path test.
        p.name = "tgi"  # type: ignore[attr-defined]

        async def go() -> None:
            async for _ in a._provider_stream([UserMessage(content="hi")]):
                pass

        import anyio
        anyio.run(go)

        assert p.last_extra is not None
        # Both seed and grammar should be present after the deep merge.
        params = p.last_extra["parameters"]
        assert params["seed"] == 42
        assert params["grammar"]["type"] == "json"


# ---------------------------------------------------------------------------
# MantisAgentOptions → query() round-trip
# ---------------------------------------------------------------------------


class TestMantisAgentOptionsWiring:

    def test_dataclass_roundtrip(self) -> None:
        opts = MantisAgentOptions(
            model="qwen2.5-7b-instruct",
            response_format={"type": "json_object"},
        )
        d = opts.to_query_options()
        assert d["response_format"] == {"type": "json_object"}

    def test_dataclass_default_is_none(self) -> None:
        opts = MantisAgentOptions(model="qwen2.5-7b-instruct")
        d = opts.to_query_options()
        assert "response_format" not in d

    def test_response_format_routes_to_agent_field_not_extra(self) -> None:
        """Regression guard: this would silently break if we forgot to add
        ``response_format`` to ``_agent_from_options``'s recognized-keys
        loop. Without it the value would end up on ``Agent.extra`` and
        never reach the translator."""

        from mantis_agent.query import _agent_from_options

        # Inject a mock provider via the ``provider`` opt so the agent
        # doesn't try to dial a real backend.
        p = MockProvider()
        opts = {
            "model": "qwen2.5-7b-instruct",
            "response_format": {"type": "json_object"},
            "provider": p,
        }
        a = _agent_from_options(opts)
        assert a.response_format == {"type": "json_object"}
        # Confirm it's NOT in extra.
        assert (a.extra or {}).get("response_format") is None

    def test_options_with_existing_extra_dont_get_clobbered(self) -> None:
        opts = MantisAgentOptions(
            model="qwen2.5-7b-instruct",
            response_format={"type": "json_object"},
            extra={"telemetry_id": "abc"},
        )
        d = opts.to_query_options()
        assert d["response_format"] == {"type": "json_object"}
        assert d["extra"]["telemetry_id"] == "abc"


# ---------------------------------------------------------------------------
# Wire-level — OpenAI-compat
# ---------------------------------------------------------------------------


def _record_handler(out: list[httpx.Request], body: bytes, ctype: str):
    def handler(request: httpx.Request) -> httpx.Response:
        out.append(request)
        return httpx.Response(
            200,
            headers={"content-type": ctype},
            content=body,
        )

    return handler


class TestOpenAIWire:

    @pytest.mark.anyio
    async def test_response_format_lands_in_chat_completions_payload(self) -> None:
        seen: list[httpx.Request] = []
        handler = _record_handler(seen, _OPENAI_SSE_BODY, "text/event-stream")

        p = OpenAICompatProvider(base_url="http://stub/v1")
        await p.client.aclose()
        p.client = httpx.AsyncClient(
            base_url="http://stub/v1",
            transport=httpx.MockTransport(handler),
            headers={"content-type": "application/json"},
        )

        a = Agent(
            model="qwen2.5-7b-instruct",
            provider=p,
            include_memory=False,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "Color",
                    "schema": {
                        "type": "object",
                        "properties": {"hex": {"type": "string"}},
                        "required": ["hex"],
                    },
                    "strict": True,
                },
            },
        )

        async for _ in a._provider_stream([UserMessage(content="pick a color")]):
            pass
        await p.client.aclose()

        assert len(seen) == 1
        body = json.loads(seen[0].content.decode("utf-8"))
        rf = body["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "Color"
        assert rf["json_schema"]["schema"]["required"] == ["hex"]
        assert rf["json_schema"]["strict"] is True

    @pytest.mark.anyio
    async def test_json_object_lands_on_wire(self) -> None:
        seen: list[httpx.Request] = []
        handler = _record_handler(seen, _OPENAI_SSE_BODY, "text/event-stream")

        p = OpenAICompatProvider(base_url="http://stub/v1")
        await p.client.aclose()
        p.client = httpx.AsyncClient(
            base_url="http://stub/v1",
            transport=httpx.MockTransport(handler),
            headers={"content-type": "application/json"},
        )

        a = Agent(
            model="qwen2.5-7b-instruct",
            provider=p,
            include_memory=False,
            response_format={"type": "json_object"},
        )

        async for _ in a._provider_stream([UserMessage(content="hi")]):
            pass
        await p.client.aclose()

        body = json.loads(seen[0].content.decode("utf-8"))
        assert body["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Wire-level — Ollama
# ---------------------------------------------------------------------------


class TestOllamaWire:

    @pytest.mark.anyio
    async def test_json_object_becomes_format_string(self) -> None:
        seen: list[httpx.Request] = []
        handler = _record_handler(seen, _OLLAMA_NDJSON_BODY, "application/x-ndjson")

        p = OllamaProvider(base_url="http://stub:11434")
        await p.client.aclose()
        p.client = httpx.AsyncClient(
            base_url="http://stub:11434",
            transport=httpx.MockTransport(handler),
            headers={
                "content-type": "application/json",
                "accept": "application/x-ndjson",
            },
        )

        a = Agent(
            model="qwen2.5:7b",
            provider=p,
            include_memory=False,
            response_format={"type": "json_object"},
        )

        async for _ in a._provider_stream([UserMessage(content="hi")]):
            pass
        await p.client.aclose()

        body = json.loads(seen[0].content.decode("utf-8"))
        # Top-level ``format`` is Ollama-native — the string "json" is the
        # spelling that flips JSON-mode on.
        assert body["format"] == "json"

    @pytest.mark.anyio
    async def test_json_schema_becomes_raw_schema(self) -> None:
        seen: list[httpx.Request] = []
        handler = _record_handler(seen, _OLLAMA_NDJSON_BODY, "application/x-ndjson")

        p = OllamaProvider(base_url="http://stub:11434")
        await p.client.aclose()
        p.client = httpx.AsyncClient(
            base_url="http://stub:11434",
            transport=httpx.MockTransport(handler),
            headers={
                "content-type": "application/json",
                "accept": "application/x-ndjson",
            },
        )

        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        }
        a = Agent(
            model="qwen2.5:7b",
            provider=p,
            include_memory=False,
            response_format={"type": "json_schema", "schema": schema},
        )

        async for _ in a._provider_stream([UserMessage(content="hi")]):
            pass
        await p.client.aclose()

        body = json.loads(seen[0].content.decode("utf-8"))
        # Ollama wants the schema dict directly, not the OpenAI envelope.
        assert body["format"] == schema


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


class TestPublicAPI:

    def test_exports_are_importable(self) -> None:
        import mantis_agent as a

        assert hasattr(a, "ResponseFormatError")
        assert hasattr(a, "normalize_response_format")
        assert hasattr(a, "translate_response_format")
        assert a.ResponseFormatError.__name__ == "ResponseFormatError"

    def test_exports_listed_in___all__(self) -> None:
        from mantis_agent import __all__

        for n in (
            "ResponseFormatError",
            "normalize_response_format",
            "translate_response_format",
        ):
            assert n in __all__, f"{n} missing from __all__"

    def test_response_format_error_subclasses_value_error(self) -> None:
        """A user catching ``ValueError`` (the most common generic
        exception type for config errors) should still see our errors."""

        assert issubclass(ResponseFormatError, ValueError)
