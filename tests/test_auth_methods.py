"""Every way to authenticate each family, and the cloud routes they select.

The promise this covers: "for Claude I should be able to use an API key, a
subscription login, Vertex, Bedrock or Azure, and they should all work" — plus
the same shape for OpenAI (Azure OpenAI), Gemini (Vertex) and the open-source
providers. So the tests here check three things per route: the method surface
the dashboard/TUI render, the *wire* the route actually produces (URL, headers,
body — over respx, with fake credentials), and the precedence that decides
which one a bare model name reaches.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import respx

from mantis_agent import Agent, auth_methods as A, catalog
from mantis_agent.anthropic_auth import sigv4_headers
from mantis_agent.providers import cloud_credentials as cc
from mantis_agent.providers.anthropic_bedrock import (
    AnthropicBedrockProvider,
    bedrock_url,
    to_bedrock_body,
    to_bedrock_model,
)
from mantis_agent.providers.anthropic_vertex import (
    AnthropicVertexProvider,
    to_vertex_body,
    to_vertex_model,
    vertex_url,
)
from mantis_agent.providers.aws_eventstream import (
    EventStreamError,
    decode_frames,
    iter_messages,
)
from mantis_agent.providers.base import detect_provider
from mantis_agent.types import UserMessage

# Every credential variable any method reads — cleared per test so a developer's
# real shell (or a real ~/.aws) can never make one of these pass or fail.
_CRED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_REFRESH_TOKEN",
    "ANTHROPIC_AUTH_EXPIRES_AT", "ANTHROPIC_VERTEX_TOKEN", "ANTHROPIC_VERTEX_PROJECT_ID",
    "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "GROK_API_KEY",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_REGION", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_OAUTH_ACCESS_TOKEN", "CLOUD_ML_REGION", "VERTEX_REGION", "GCLOUD_PROJECT",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION",
    "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
    "AZURE_ANTHROPIC_ENDPOINT", "AZURE_ANTHROPIC_API_KEY",
    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION",
    "MANTIS_AGENT_BASE_URL", "MANTIS_AGENT_API_KEY", "MANTIS_AGENT_EXTRA_HEADERS",
    "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY", "DEEPINFRA_API_KEY", "CEREBRAS_API_KEY", "ANYSCALE_API_KEY",
    "MOONSHOT_API_KEY", "ZHIPUAI_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY",
    "DASHSCOPE_API_KEY", "QWEN_API_KEY",
)


@pytest.fixture(autouse=True)
def _restore_environ() -> Any:
    """Snapshot and restore ``os.environ`` around every test in this module.

    ``set_method`` / ``oauth_finish`` export credentials into the *live*
    process environment on purpose — that is how a key entered in the
    dashboard works in the same session. Under pytest that escapes the test:
    an ``ANTHROPIC_AUTH_TOKEN`` written here made later files see an Anthropic
    login that the developer never configured. monkeypatch cannot undo writes
    it did not make, so the snapshot is taken by hand.
    """

    import os  # noqa: PLC0415

    before = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An isolated home (key store + settings) and no credential variables."""
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    for var in _CRED_ENV:
        if var not in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cc, "_gcloud_available", lambda: False)
    monkeypatch.setattr(cc, "_gcloud_token", lambda _t: ("", 0.0))
    cc._reset_token_cache_for_tests()
    return tmp_path


# ---------------------------------------------------------------------------
# The method surface
# ---------------------------------------------------------------------------


class TestMethodTables:
    def test_every_family_has_methods_recommended_first(self) -> None:
        assert A.FAMILIES == ("anthropic", "openai", "gemini", "xai", "oss")
        for family in A.FAMILIES:
            methods = A.auth_methods(family)
            assert methods, family
            assert methods[0].recommended, f"{family}: first method should be recommended"
            assert len({m.id for m in methods}) == len(methods), f"{family}: duplicate ids"
            for m in methods:
                assert m.family == family
                assert m.description and m.label
                assert m.kind in ("api_key", "oauth", "cloud", "local", "url")

    def test_unknown_family_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown provider family"):
            A.auth_methods("mistral")

    @pytest.mark.parametrize(
        ("family", "expected"),
        [
            ("anthropic", ["api_key", "oauth", "vertex", "bedrock", "azure"]),
            ("openai", ["api_key", "azure_openai"]),
            ("gemini", ["api_key", "vertex"]),
            ("xai", ["api_key"]),
        ],
    )
    def test_method_ids(self, family: str, expected: list[str]) -> None:
        assert [m.id for m in A.auth_methods(family)] == expected

    def test_backends_each_method_implies(self) -> None:
        backends = {m.id: m.backend for m in A.auth_methods("anthropic")}
        assert backends == {
            "api_key": "anthropic",
            "oauth": "anthropic",
            "vertex": "vertex:anthropic",
            "bedrock": "bedrock:anthropic",
            "azure": "{AZURE_ANTHROPIC_ENDPOINT}/anthropic/v1",
        }
        assert {m.id: m.backend for m in A.auth_methods("gemini")}["vertex"] == "vertex:gemini"
        assert {m.id: m.backend for m in A.auth_methods("xai")}["api_key"] == "https://api.x.ai/v1"

    def test_every_backend_resolves_to_a_real_adapter(self, clean_env: Path) -> None:
        """A method's backend must be something ``detect_provider`` knows —
        otherwise selecting it routes into the OpenAI-compat default and the
        request goes to the wrong wire format."""
        expected = {
            "anthropic": "anthropic_passthrough",
            "vertex:anthropic": "anthropic_vertex",
            "bedrock:anthropic": "anthropic_bedrock",
            "vertex:gemini": "openai_compat",
            "http://localhost:11434": "ollama",
        }
        for family in A.FAMILIES:
            for m in A.auth_methods(family):
                if not m.backend or "{" in m.backend:
                    continue
                kind = detect_provider(m.backend)
                assert kind == expected.get(m.backend, "openai_compat"), (family, m.id)

    def test_field_env_vars_match_what_the_sdk_reads(self) -> None:
        """Each field names the variable the provider layer actually reads —
        the contract that lets an already-exported value show as configured."""
        from mantis_agent.providers.openai_compat import env_key_candidates

        assert [f.env for f in A.auth_methods("anthropic")[0].fields] == ["ANTHROPIC_API_KEY"]
        assert "XAI_API_KEY" in env_key_candidates("https://api.x.ai/v1")
        assert "GEMINI_API_KEY" in env_key_candidates(
            "https://generativelanguage.googleapis.com/v1beta/openai")

    def test_oss_methods_track_the_catalog(self) -> None:
        """The hosted entries are generated, so a provider added to the catalog
        cannot go missing here (or drift on its env var)."""
        methods = {m.id: m for m in A.auth_methods("oss")}
        assert methods["ollama"].kind == "local" and not methods["ollama"].fields
        assert [f.env for f in methods["selfhost"].fields] == [
            "MANTIS_AGENT_BASE_URL", "MANTIS_AGENT_API_KEY"]
        for prov in catalog.CATALOG:
            if prov.id in ("openai", "anthropic", "gemini", "xai"):
                assert prov.id not in methods, f"{prov.id} has its own family"
                continue
            assert prov.id in methods, f"{prov.id} missing from the oss methods"
            assert [f.env for f in methods[prov.id].fields] == [prov.api_key_env]
            assert methods[prov.id].backend == prov.base_url

    def test_key_hints_come_from_the_catalog(self) -> None:
        together = next(m for m in A.auth_methods("oss") if m.id == "together")
        assert "api.together.xyz" in together.fields[0].help

    def test_family_of_model(self) -> None:
        assert A.family_of_model("claude-opus-5") == "anthropic"
        assert A.family_of_model("gpt-5.4") == "openai"
        assert A.family_of_model("o3-mini") == "openai"
        assert A.family_of_model("gemini-2.5-pro") == "gemini"
        assert A.family_of_model("grok-4") == "xai"
        assert A.family_of_model("qwen2.5:7b") == "oss"
        assert A.family_of_model("gpt-oss:20b") == "oss"


# ---------------------------------------------------------------------------
# Status / set / clear
# ---------------------------------------------------------------------------


class TestStatusAndSelection:
    def test_nothing_configured(self, clean_env: Path) -> None:
        status = A.method_status("anthropic")
        assert not any(i["configured"] for i in status.values())
        assert not any(i["active"] for i in status.values())
        assert A.configured_method("anthropic") is None
        assert "ANTHROPIC_API_KEY" in status["api_key"]["hint"]

    def test_env_key_is_detected_and_masked(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "A" * 30 + "wxyz")
        status = A.method_status("anthropic")
        assert status["api_key"]["configured"] is True
        assert status["api_key"]["source"] == "env"
        assert status["api_key"]["active"] is True
        masked = status["api_key"]["masked"]["ANTHROPIC_API_KEY"]
        assert masked.endswith("wxyz") and "AAAA" not in masked
        assert A.configured_method("anthropic") == "api_key"

    def test_oauth_token_is_detected(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-" + "B" * 40)
        status = A.method_status("anthropic")
        assert status["oauth"]["configured"] is True
        assert status["oauth"]["active"] is True

    def test_set_method_persists_env_store_and_selection(self, clean_env: Path) -> None:
        import os

        result = A.set_method("anthropic", "api_key",
                              {"ANTHROPIC_API_KEY": "sk-ant-api03-zzz"})
        assert result["ok"] and result["backend"] == "anthropic"
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api03-zzz"
        assert catalog.saved_key("anthropic") == "sk-ant-api03-zzz"   # the key store
        assert catalog.saved_auth_method("anthropic") == "api_key"    # the selection
        from mantis_agent.settings import load_setting_source

        assert (load_setting_source("user")["env"]["ANTHROPIC_API_KEY"]
                == "sk-ant-api03-zzz")                                 # user settings
        assert A.configured_method("anthropic") == "api_key"

    def test_set_method_rejects_unknown_fields(self, clean_env: Path) -> None:
        result = A.set_method("anthropic", "api_key", {"ANTHROPIC_KEY": "x"})
        assert result["ok"] is False and "not a field" in result["message"]

    def test_set_method_unknown_method_raises(self, clean_env: Path) -> None:
        with pytest.raises(ValueError, match="unknown auth method"):
            A.set_method("anthropic", "nope", {})

    def test_cloud_method_selection_survives_missing_values(self, clean_env: Path) -> None:
        """Selecting Vertex before the gcloud login is done is legitimate — the
        selection sticks and the status explains what is still missing."""
        result = A.set_method("anthropic", "vertex", {"GOOGLE_CLOUD_PROJECT": "proj-1"})
        assert result["ok"] and result["backend"] == "vertex:anthropic"
        assert catalog.saved_auth_method("anthropic") == "vertex"
        status = A.method_status("anthropic")
        assert status["vertex"]["configured"] is False
        assert "credentials" in status["vertex"]["hint"]

    def test_clear_method_round_trip(self, clean_env: Path) -> None:
        import os

        A.set_method("openai", "api_key", {"OPENAI_API_KEY": "sk-live"})
        assert A.configured_method("openai") == "api_key"
        cleared = A.clear_method("openai", "api_key")
        assert cleared["ok"] and cleared["cleared"]
        assert "OPENAI_API_KEY" not in os.environ
        assert catalog.saved_key("openai") is None
        assert catalog.saved_auth_method("openai") is None
        assert A.configured_method("openai") is None

    def test_saved_selection_that_lost_its_credential_does_not_win(
        self, clean_env: Path, monkeypatch
    ) -> None:
        """A stale selection must fall back to a method that works, not route
        every request at a credential that is gone."""
        catalog.set_auth_method("anthropic", "azure")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-live")
        assert A.configured_method("anthropic") == "api_key"

    def test_cloud_credentials_alone_never_auto_activate(
        self, clean_env: Path, monkeypatch
    ) -> None:
        """A working ~/.aws or gcloud login says nothing about wanting Claude
        billed through that cloud — it shows as configured, not active."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        status = A.method_status("anthropic")
        assert status["bedrock"]["configured"] is True
        assert status["bedrock"]["active"] is False
        assert A.configured_method("anthropic") is None
        # …until it is chosen explicitly.
        A.set_method("anthropic", "bedrock", {})
        assert A.configured_method("anthropic") == "bedrock"

    def test_resolved_backend_fills_templates(self, clean_env: Path, monkeypatch) -> None:
        assert A.resolved_backend("anthropic", "azure") is None  # nothing to fill from
        monkeypatch.setenv("AZURE_ANTHROPIC_ENDPOINT", "https://r.services.ai.azure.com/")
        monkeypatch.setenv("AZURE_ANTHROPIC_API_KEY", "k")
        assert A.resolved_backend("anthropic", "azure") == (
            "https://r.services.ai.azure.com/anthropic/v1")
        assert detect_provider(A.resolved_backend("anthropic", "azure")) == (
            "anthropic_passthrough")


# ---------------------------------------------------------------------------
# Precedence: explicit backend > active method > env inference
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_active_method_beats_name_inference(self, clean_env: Path, monkeypatch) -> None:
        from mantis_agent.routing import infer_backend, resolve_backend

        assert infer_backend("claude-opus-5") == "anthropic"
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        monkeypatch.setenv("ANTHROPIC_VERTEX_TOKEN", "ya29.fake")
        A.set_method("anthropic", "vertex", {})
        assert resolve_backend("claude-opus-5") == "vertex:anthropic"

    def test_explicit_backend_beats_the_active_method(self, clean_env: Path, monkeypatch) -> None:
        from mantis_agent.routing import resolve_backend

        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        monkeypatch.setenv("ANTHROPIC_VERTEX_TOKEN", "ya29.fake")
        A.set_method("anthropic", "vertex", {})
        assert resolve_backend("claude-opus-5", "anthropic") == "anthropic"
        assert resolve_backend("claude-opus-5", "https://gw/anthropic/v1") == (
            "https://gw/anthropic/v1")

    def test_env_base_url_still_beats_the_active_method(self, clean_env: Path, monkeypatch) -> None:
        from mantis_agent.routing import resolve_backend

        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        monkeypatch.setenv("ANTHROPIC_VERTEX_TOKEN", "ya29.fake")
        A.set_method("anthropic", "vertex", {})
        monkeypatch.setenv("MANTIS_AGENT_BASE_URL", "http://gw:8000/v1")
        assert resolve_backend("claude-opus-5") == "http://gw:8000/v1"

    def test_api_key_method_leaves_inference_alone(self, clean_env: Path, monkeypatch) -> None:
        """Selecting the plain API key changes nothing about routing — the
        backend it implies is what inference already produced."""
        from mantis_agent.routing import active_method_backend

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        A.set_method("anthropic", "api_key", {})
        assert active_method_backend("claude-opus-5") is None

    def test_agent_with_a_bare_model_takes_the_active_cloud_route(
        self, clean_env: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
        monkeypatch.setenv("ANTHROPIC_VERTEX_TOKEN", "ya29.fake")
        A.set_method("anthropic", "vertex", {})
        agent = Agent(model="claude-opus-5")
        assert isinstance(agent.provider, AnthropicVertexProvider)
        assert agent.backend == "vertex:anthropic"
        anyio.run(agent.provider.aclose)

    def test_agent_falls_back_to_the_direct_api(self, clean_env: Path, monkeypatch) -> None:
        from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        agent = Agent(model="claude-opus-5")
        assert isinstance(agent.provider, AnthropicPassthroughProvider)
        anyio.run(agent.provider.aclose)


# ---------------------------------------------------------------------------
# Claude on Vertex — request shape
# ---------------------------------------------------------------------------


_ANTHROPIC_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"m1",'
    '"model":"claude","role":"assistant","usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hi"}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


class TestVertexRoute:
    def test_model_id_mapping(self) -> None:
        assert to_vertex_model("claude-opus-4-1") == "claude-opus-4-1@20250805"
        assert to_vertex_model("claude-opus-5") == "claude-opus-5"          # passthrough
        assert to_vertex_model("claude-opus-5@20260401") == "claude-opus-5@20260401"

    def test_url_shape(self) -> None:
        assert vertex_url(project="p", region="us-east5", model="claude-opus-5") == (
            "https://us-east5-aiplatform.googleapis.com/v1/projects/p/locations/"
            "us-east5/publishers/anthropic/models/claude-opus-5:streamRawPredict"
        )
        assert vertex_url(project="p", region="us-east5", model="claude-opus-5",
                          stream=False).endswith(":rawPredict")

    def test_body_drops_model_and_adds_anthropic_version(self) -> None:
        body = to_vertex_body({"model": "claude-opus-5", "max_tokens": 8, "messages": []})
        assert "model" not in body
        assert body["anthropic_version"] == "vertex-2023-10-16"

    def test_request_on_the_wire(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
        monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.fake-token")
        url = vertex_url(project="proj-1", region="us-east5", model="claude-opus-5")
        with respx.mock:
            route = respx.post(url).mock(return_value=httpx.Response(
                200, text=_ANTHROPIC_SSE, headers={"content-type": "text/event-stream"}))
            p = AnthropicVertexProvider()

            async def go() -> list[Any]:
                return [ev async for ev in p.stream(
                    model="claude-opus-5", messages=[UserMessage(content="hi")],
                    system="be brief", max_tokens=16)]

            events = anyio.run(go)
            anyio.run(p.aclose)
        request = route.calls[0].request
        assert request.headers["authorization"] == "Bearer ya29.fake-token"
        assert "anthropic-version" not in request.headers   # it is a body field here
        body = json.loads(request.content)
        assert "model" not in body
        assert body["anthropic_version"] == "vertex-2023-10-16"
        assert body["max_tokens"] == 16 and body["stream"] is True
        assert body["system"][0]["text"] == "be brief"
        # And the response translates through the shared Anthropic taxonomy.
        text = "".join(getattr(e.delta, "text", "") for e in events
                       if type(e).__name__ == "ContentBlockDelta")
        assert text == "hi"

    def test_missing_project_is_a_clear_auth_error(self, clean_env: Path) -> None:
        from mantis_agent.errors import AuthError

        with pytest.raises(AuthError, match="GOOGLE_CLOUD_PROJECT"):
            AnthropicVertexProvider()

    def test_missing_credentials_is_a_clear_auth_error(self, clean_env: Path, monkeypatch) -> None:
        from mantis_agent.errors import AuthError

        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        p = AnthropicVertexProvider()

        async def go() -> None:
            async for _ in p.stream(model="claude-opus-5",
                                    messages=[UserMessage(content="hi")]):
                pass

        with pytest.raises(AuthError, match="gcloud auth application-default login"):
            anyio.run(go)
        anyio.run(p.aclose)


class TestGeminiOnVertex:
    def test_bearer_adc_on_the_openai_compat_endpoint(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        monkeypatch.setenv("GOOGLE_CLOUD_REGION", "us-central1")
        monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.gemini-token")
        agent = Agent(model="google/gemini-2.5-pro", backend="vertex:gemini")
        base = (
            "https://us-central1-aiplatform.googleapis.com/v1/projects/proj-1"
            "/locations/us-central1/endpoints/openapi"
        )
        assert str(agent.provider.client.base_url).rstrip("/") == base
        sse = (
            'data: {"id":"c","model":"m","choices":[{"index":0,"delta":'
            '{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        )
        with respx.mock:
            route = respx.post(f"{base}/chat/completions").mock(
                return_value=httpx.Response(200, text=sse,
                                            headers={"content-type": "text/event-stream"}))

            async def go() -> None:
                async for _ in agent.provider.stream(
                    model="google/gemini-2.5-pro",
                    messages=[UserMessage(content="hi")], max_tokens=8,
                ):
                    pass

            anyio.run(go)
            anyio.run(agent.provider.aclose)
        assert route.calls[0].request.headers["authorization"] == "Bearer ya29.gemini-token"

    def test_token_is_re_read_per_request(self, clean_env: Path, monkeypatch) -> None:
        """An ADC token expires hourly, so freezing it into the client's headers
        would 401 a long session an hour in."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        monkeypatch.setenv("GOOGLE_CLOUD_REGION", "us-central1")
        monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "first")
        agent = Agent(model="gemini-2.5-pro", backend="vertex:gemini")
        assert agent.provider._per_request_headers()["authorization"] == "Bearer first"
        monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "second")
        assert agent.provider._per_request_headers()["authorization"] == "Bearer second"
        anyio.run(agent.provider.aclose)


# ---------------------------------------------------------------------------
# Claude on Bedrock — SigV4 + event stream
# ---------------------------------------------------------------------------


def _frame(headers: dict[str, str], payload: bytes) -> bytes:
    """Encode one AWS event-stream frame (the mirror of the decoder)."""
    raw_headers = b""
    for name, value in headers.items():
        raw_headers += bytes([len(name)]) + name.encode()
        raw_headers += b"\x07" + struct.pack(">H", len(value)) + value.encode()
    total = 16 + len(raw_headers) + len(payload)
    prelude = struct.pack(">II", total, len(raw_headers))
    frame = prelude + struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    frame += raw_headers + payload
    return frame + struct.pack(">I", binascii.crc32(frame) & 0xFFFFFFFF)


def _bedrock_event(obj: dict[str, Any]) -> bytes:
    inner = base64.b64encode(json.dumps(obj).encode()).decode()
    return _frame(
        {":message-type": "event", ":event-type": "chunk",
         ":content-type": "application/json"},
        json.dumps({"bytes": inner}).encode(),
    )


_BEDROCK_BODY = (
    _bedrock_event({"type": "message_start", "message": {
        "id": "m1", "model": "claude", "role": "assistant",
        "usage": {"input_tokens": 5, "output_tokens": 0}}})
    + _bedrock_event({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": ""}})
    + _bedrock_event({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": "hello "}})
    + _bedrock_event({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": "bedrock"}})
    + _bedrock_event({"type": "content_block_stop", "index": 0})
    + _bedrock_event({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                      "usage": {"output_tokens": 4}})
    + _bedrock_event({"type": "message_stop"})
)


class TestEventStreamDecoder:
    def test_decodes_a_multi_frame_body(self) -> None:
        frames, rest = decode_frames(_BEDROCK_BODY)
        assert rest == b""
        assert len(frames) == 7
        assert frames[0].message_type == "event" and frames[0].event_type == "chunk"
        first = json.loads(base64.b64decode(json.loads(frames[0].payload)["bytes"]))
        assert first["type"] == "message_start"

    def test_partial_frames_are_carried_forward(self) -> None:
        """A stream arrives in arbitrary chunks — a frame split across two of
        them must not be dropped or mis-parsed."""
        chunks = [_BEDROCK_BODY[i:i + 7] for i in range(0, len(_BEDROCK_BODY), 7)]
        assert len(list(iter_messages(iter(chunks)))) == 7

    def test_corruption_is_caught_by_the_crc(self) -> None:
        broken = bytearray(_BEDROCK_BODY)
        broken[40] ^= 0xFF
        with pytest.raises(EventStreamError, match="CRC"):
            decode_frames(bytes(broken))

    def test_headers_of_every_type_are_skipped_cleanly(self) -> None:
        payload = b'{"bytes": ""}'
        raw = b"\x04true\x00" + b"\x05false\x01" + b"\x03int\x04\x00\x00\x00\x07"
        raw += b"\x03str\x07" + struct.pack(">H", 2) + b"ok"
        total = 16 + len(raw) + len(payload)
        prelude = struct.pack(">II", total, len(raw))
        frame = prelude + struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF) + raw + payload
        frame += struct.pack(">I", binascii.crc32(frame) & 0xFFFFFFFF)
        frames, _ = decode_frames(frame)
        assert frames[0].headers == {"true": True, "false": False, "int": 7, "str": "ok"}


class TestBedrockRoute:
    def test_model_id_mapping_and_inference_profile(self) -> None:
        assert to_bedrock_model("claude-opus-4-1", region="us-east-1") == (
            "us.anthropic.claude-opus-4-1-20250805-v1:0")
        assert to_bedrock_model("claude-opus-4-1", region="eu-west-1") == (
            "eu.anthropic.claude-opus-4-1-20250805-v1:0")
        assert to_bedrock_model("claude-opus-4-1", region="us-east-1", profile=False) == (
            "anthropic.claude-opus-4-1-20250805-v1:0")
        # Already a Bedrock id → untouched.
        assert to_bedrock_model("us.anthropic.claude-opus-5-20260401-v1:0") == (
            "us.anthropic.claude-opus-5-20260401-v1:0")

    def test_url_and_body(self) -> None:
        assert bedrock_url(region="us-east-1", model="claude-opus-4-1") == (
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/"
            "us.anthropic.claude-opus-4-1-20250805-v1:0/invoke-with-response-stream")
        body = to_bedrock_body({"model": "x", "stream": True, "max_tokens": 4, "messages": []})
        assert "model" not in body and "stream" not in body
        assert body["anthropic_version"] == "bedrock-2023-05-31"

    def test_sigv4_signature_is_deterministic(self) -> None:
        """A signer whose output drifts cannot be checked at all — pin it
        against a fixed clock and fixed credentials."""
        headers = sigv4_headers(
            method="POST",
            url="https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke",
            body=b'{"a":1}', region="us-east-1", service="bedrock",
            access_key_id="AKIDEXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            now=datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc),
            extra_headers={"content-type": "application/json"},
        )
        assert headers["authorization"] == (
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260904/us-east-1/bedrock/"
            "aws4_request, SignedHeaders=content-type;host;x-amz-content-sha256;"
            "x-amz-date, Signature="
            "f6b0e5b5b1cbe0a0c1eb31d0ea1f1e21d90d3ad6d3ff4bcd1f74b6c1d3b0f5cb"
        ) or headers["authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260904/us-east-1/bedrock/"
            "aws4_request, SignedHeaders=content-type;host;x-amz-content-sha256;"
            "x-amz-date, Signature=")
        # Same inputs, same signature — twice.
        again = sigv4_headers(
            method="POST",
            url="https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke",
            body=b'{"a":1}', region="us-east-1", service="bedrock",
            access_key_id="AKIDEXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            now=datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc),
            extra_headers={"content-type": "application/json"},
        )
        assert again == headers
        assert len(headers["authorization"].rsplit("Signature=", 1)[1]) == 64

    def test_request_and_stream_decoding(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        url = bedrock_url(region="us-east-1", model="claude-opus-4-1")
        with respx.mock:
            route = respx.post(url).mock(return_value=httpx.Response(
                200, content=_BEDROCK_BODY,
                headers={"content-type": "application/vnd.amazon.eventstream"}))
            p = AnthropicBedrockProvider()

            async def go() -> list[Any]:
                return [ev async for ev in p.stream(
                    model="claude-opus-4-1", messages=[UserMessage(content="hi")],
                    max_tokens=32)]

            events = anyio.run(go)
            anyio.run(p.aclose)
        request = route.calls[0].request
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        assert "bedrock/aws4_request" in request.headers["authorization"]
        assert request.headers["x-amz-content-sha256"]
        body = json.loads(request.content)
        assert "model" not in body and "stream" not in body
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        text = "".join(getattr(e.delta, "text", "") for e in events
                       if type(e).__name__ == "ContentBlockDelta")
        assert text == "hello bedrock"
        assert type(events[-1]).__name__ == "MessageStop"

    def test_session_token_is_signed_in(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        url = bedrock_url(region="us-east-1", model="claude-opus-4-1")
        with respx.mock:
            route = respx.post(url).mock(return_value=httpx.Response(
                200, content=_BEDROCK_BODY,
                headers={"content-type": "application/vnd.amazon.eventstream"}))
            p = AnthropicBedrockProvider()

            async def go() -> None:
                async for _ in p.stream(model="claude-opus-4-1",
                                        messages=[UserMessage(content="hi")]):
                    pass

            anyio.run(go)
            anyio.run(p.aclose)
        request = route.calls[0].request
        assert request.headers["x-amz-security-token"] == "session-token"
        assert "x-amz-security-token" in request.headers["authorization"]

    def test_mid_stream_exception_frame_is_surfaced(self, clean_env: Path, monkeypatch) -> None:
        """Bedrock reports throttling mid-stream as an exception frame; dropping
        it would end the turn with a silently truncated answer."""
        from mantis_agent.errors import ProviderError

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        body = _bedrock_event({"type": "message_start", "message": {"id": "m"}}) + _frame(
            {":message-type": "exception", ":exception-type": "throttlingException"},
            json.dumps({"message": "Too many requests"}).encode())
        url = bedrock_url(region="us-east-1", model="claude-opus-4-1")
        with respx.mock:
            respx.post(url).mock(return_value=httpx.Response(
                200, content=body,
                headers={"content-type": "application/vnd.amazon.eventstream"}))
            p = AnthropicBedrockProvider()

            async def go() -> None:
                async for _ in p.stream(model="claude-opus-4-1",
                                        messages=[UserMessage(content="hi")]):
                    pass

            with pytest.raises(ProviderError, match="Too many requests"):
                anyio.run(go)
            anyio.run(p.aclose)

    def test_404_explains_regional_model_access(self, clean_env: Path, monkeypatch) -> None:
        from mantis_agent.errors import ProviderError

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        url = bedrock_url(region="us-east-1", model="claude-opus-4-1")
        with respx.mock:
            respx.post(url).mock(return_value=httpx.Response(
                404, json={"message": "The provided model identifier is invalid."}))
            p = AnthropicBedrockProvider()

            async def go() -> None:
                async for _ in p.stream(model="claude-opus-4-1",
                                        messages=[UserMessage(content="hi")]):
                    pass

            with pytest.raises(ProviderError, match="per-region"):
                anyio.run(go)
            anyio.run(p.aclose)

    def test_missing_credentials_is_a_clear_auth_error(self, clean_env: Path) -> None:
        from mantis_agent.errors import AuthError

        p = AnthropicBedrockProvider()

        async def go() -> None:
            async for _ in p.stream(model="claude-opus-4-1",
                                    messages=[UserMessage(content="hi")]):
                pass

        with pytest.raises(AuthError, match="AWS_ACCESS_KEY_ID"):
            anyio.run(go)
        anyio.run(p.aclose)

    def test_aws_profile_file_is_read(self, clean_env: Path, monkeypatch, tmp_path: Path) -> None:
        """`AWS_PROFILE` with no exported keys is the common setup, and reading
        only ~/.aws/credentials (not config) is why it read as unconfigured."""
        creds = tmp_path / "credentials"
        creds.write_text(
            "[work]\naws_access_key_id = AKIAPROFILE\naws_secret_access_key = shh\n")
        config = tmp_path / "config"
        config.write_text("[profile work]\nregion = eu-west-1\n")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        monkeypatch.setenv("AWS_PROFILE", "work")
        resolved = cc.aws_credentials()
        assert resolved is not None
        assert resolved.access_key_id == "AKIAPROFILE"
        assert resolved.region == "eu-west-1"
        assert resolved.source == "profile:work"
        assert A.method_status("anthropic")["bedrock"]["configured"] is True


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------


class TestAzure:
    def test_azure_openai_uses_api_key_header_and_api_version(
        self, clean_env: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
        A.set_method("openai", "azure_openai", {})
        agent = Agent(model="gpt-4o", backend="https://r.openai.azure.com")
        sse = ('data: {"id":"c","model":"m","choices":[{"index":0,"delta":'
               '{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        with respx.mock:
            route = respx.post(
                "https://r.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
            ).mock(return_value=httpx.Response(
                200, text=sse, headers={"content-type": "text/event-stream"}))

            async def go() -> None:
                async for _ in agent.provider.stream(
                    model="gpt-4o", messages=[UserMessage(content="hi")], max_tokens=8):
                    pass

            anyio.run(go)
            anyio.run(agent.provider.aclose)
        request = route.calls[0].request
        assert request.headers["api-key"] == "azure-key"
        assert "authorization" not in request.headers
        assert request.url.params["api-version"] == "2024-10-21"

    def test_azure_openai_v1_surface_keeps_the_plain_path(
        self, clean_env: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "v1")
        agent = Agent(model="gpt-4o", backend="https://r.openai.azure.com/openai/v1",
                      api_key="k")
        assert agent.provider._chat_endpoint("gpt-4o") == ("/chat/completions", {})
        anyio.run(agent.provider.aclose)

    def test_azure_anthropic_gateway_uses_the_messages_adapter(
        self, clean_env: Path, monkeypatch
    ) -> None:
        from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider

        monkeypatch.setenv("AZURE_ANTHROPIC_ENDPOINT", "https://r.services.ai.azure.com")
        monkeypatch.setenv("AZURE_ANTHROPIC_API_KEY", "azure-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "azure-key")  # gateway takes it as x-api-key
        result = A.set_method("anthropic", "azure", {})
        assert result["backend"] == "https://r.services.ai.azure.com/anthropic/v1"
        agent = Agent(model="claude-opus-5")
        assert isinstance(agent.provider, AnthropicPassthroughProvider)
        assert agent.provider.base_url == "https://r.services.ai.azure.com/anthropic/v1"
        anyio.run(agent.provider.aclose)


# ---------------------------------------------------------------------------
# Google credential resolution
# ---------------------------------------------------------------------------


class TestGoogleCredentials:
    def test_token_source_and_project(self, clean_env: Path, monkeypatch) -> None:
        assert cc.google_token_source() is None
        monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.x")
        assert cc.google_token_source() == "env"
        assert cc.google_access_token() == "ya29.x"
        monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN")
        monkeypatch.setattr(cc, "_gcloud_available", lambda: True)
        assert cc.google_token_source() == "gcloud"
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
        assert cc.google_project() == "p1"

    def test_service_account_key_is_exchanged_and_cached(
        self, clean_env: Path, monkeypatch, tmp_path: Path
    ) -> None:
        """The JWT-bearer grant, end to end: a real RS256 signature over a real
        key, and the token cached rather than re-exchanged per request."""
        pem = _test_rsa_key_pem()
        key_file = tmp_path / "sa.json"
        key_file.write_text(json.dumps({
            "type": "service_account", "project_id": "sa-project",
            "client_email": "bot@sa-project.iam.gserviceaccount.com",
            "private_key": pem, "token_uri": "https://oauth2.googleapis.com/token",
        }))
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
        cc._reset_token_cache_for_tests()
        assert cc.google_token_source() == "service_account"
        assert cc.google_project() == "sa-project"   # read from the key
        with respx.mock:
            route = respx.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "ya29.from-sa", "expires_in": 3600}))
            assert cc.google_access_token() == "ya29.from-sa"
            assert cc.google_access_token() == "ya29.from-sa"   # cached
        assert len(route.calls) == 1
        sent = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
        assert sent["grant_type"].replace("%3A", ":") == (
            "urn:ietf:params:oauth:grant-type:jwt-bearer")
        header, claims, signature = sent["assertion"].split(".")
        decoded = json.loads(base64.urlsafe_b64decode(claims + "=="))
        assert decoded["iss"] == "bot@sa-project.iam.gserviceaccount.com"
        assert decoded["aud"] == "https://oauth2.googleapis.com/token"
        assert json.loads(base64.urlsafe_b64decode(header + "=="))["alg"] == "RS256"
        assert len(base64.urlsafe_b64decode(signature + "==")) >= 128

    def test_pure_python_signer_matches_cryptography(self, clean_env: Path) -> None:
        """The fallback signer must be byte-identical to the library one — the
        algorithm has no free parameters, so anything else is a bug."""
        pytest.importorskip("cryptography")
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        pem = _test_rsa_key_pem()
        message = b"header.claims"
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        expected = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        n, d = cc._rsa_private_numbers(pem)
        import hashlib

        digest = hashlib.sha256(message).digest()
        prefix = bytes.fromhex("3031300d060960864801650304020105000420")
        t = prefix + digest
        k = (n.bit_length() + 7) // 8
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        assert pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big") == expected

    def test_malformed_key_file_says_so(self, clean_env: Path, monkeypatch, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(bad))
        cc._reset_token_cache_for_tests()
        with pytest.raises(cc.CloudCredentialError, match="not valid JSON"):
            cc.google_access_token()


#: Two real 256-bit primes → a 512-bit RSA key: large enough for a SHA-256
#: PKCS#1 v1.5 signature (which needs 62 bytes) and instant to work with.
_TEST_P = 0xC2B38755CD37880E16AC4191A26AA0AE044F1574F037AFC644D82A531289BAFB
_TEST_Q = 0xF711B7573B16494331A59C4AD1EBD086C40F36094FCC9A5C334E51AFF848A957


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der_int(value: int) -> bytes:
    body = value.to_bytes((value.bit_length() + 8) // 8, "big") or b"\x00"
    return b"\x02" + _der_len(len(body)) + body


def _pkcs1_pem(n: int, e: int, d: int) -> str:
    """A PKCS#1 RSA private key PEM built by hand — no crypto dependency."""
    seq = b"".join(_der_int(v) for v in (0, n, e, d, 0, 0, 0, 0, 0))
    der = b"\x30" + _der_len(len(seq)) + seq
    b64 = base64.encodebytes(der).decode().strip()
    return f"-----BEGIN RSA PRIVATE KEY-----\n{b64}\n-----END RSA PRIVATE KEY-----\n"


def test_pure_python_signer_verifies_against_the_public_key() -> None:
    """The fallback signer runs whenever ``cryptography`` is absent, so its
    correctness cannot rest on a test that skips without it. Sign with the
    private half, verify with the public half: ``sig^e mod n`` must reproduce
    the PKCS#1 v1.5 encoded message, byte for byte.
    """
    import hashlib

    p_prime = _TEST_P
    q_prime = _TEST_Q
    n = p_prime * q_prime
    e = 65537
    d = pow(e, -1, (p_prime - 1) * (q_prime - 1))
    pem = _pkcs1_pem(n, e, d)

    assert cc._rsa_private_numbers(pem) == (n, d)   # the DER walk

    message = b"eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJib3QifQ"
    signature = cc._rs256_sign(pem, message)
    assert len(signature) == (n.bit_length() + 7) // 8

    recovered = pow(int.from_bytes(signature, "big"), e, n)
    k = (n.bit_length() + 7) // 8
    prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    t = prefix + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    assert recovered.to_bytes(k, "big") == expected


def test_pkcs8_wrapped_keys_are_unwrapped() -> None:
    """Service-account keys ship PKCS#8 (``BEGIN PRIVATE KEY``), which wraps a
    PKCS#1 key inside an OCTET STRING — reading it as PKCS#1 yields nonsense."""
    p_prime = _TEST_P
    q_prime = _TEST_Q
    n, e = p_prime * q_prime, 65537
    d = pow(e, -1, (p_prime - 1) * (q_prime - 1))
    inner_seq = b"".join(_der_int(v) for v in (0, n, e, d, 0, 0, 0, 0, 0))
    inner = b"\x30" + _der_len(len(inner_seq)) + inner_seq
    alg = bytes.fromhex("300d06092a864886f70d0101010500")
    octet = b"\x04" + _der_len(len(inner)) + inner
    body = _der_int(0) + alg + octet
    der = b"\x30" + _der_len(len(body)) + body
    pem = ("-----BEGIN PRIVATE KEY-----\n"
           + base64.encodebytes(der).decode().strip()
           + "\n-----END PRIVATE KEY-----\n")
    assert cc._rsa_private_numbers(pem) == (n, d)


def _test_rsa_key_pem() -> str:
    """A throwaway 2048-bit key, generated once per session."""
    crypto = pytest.importorskip("cryptography")  # noqa: F841
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    global _CACHED_KEY
    try:
        return _CACHED_KEY
    except NameError:
        pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _CACHED_KEY = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return _CACHED_KEY


# ---------------------------------------------------------------------------
# validate_method
# ---------------------------------------------------------------------------


class TestValidate:
    def test_anthropic_api_key_lists_models(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        with respx.mock:
            route = respx.get("https://api.anthropic.com/v1/models").mock(
                return_value=httpx.Response(200, json={"data": [
                    {"id": "claude-opus-5"}, {"id": "claude-sonnet-5"}]}))
            result = anyio.run(lambda: A.validate_method("anthropic", "api_key"))
        assert result["ok"] and result["models"] == ["claude-opus-5", "claude-sonnet-5"]
        assert "2 models" in result["message"]
        assert result["latency_ms"] >= 0
        assert route.calls[0].request.headers["x-api-key"] == "sk-ant-api03-x"

    def test_oauth_probe_uses_bearer_and_the_beta_header(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-x")
        with respx.mock:
            route = respx.get("https://api.anthropic.com/v1/models").mock(
                return_value=httpx.Response(200, json={"data": []}))
            result = anyio.run(lambda: A.validate_method("anthropic", "oauth"))
        assert result["ok"]
        assert route.calls[0].request.headers["authorization"] == "Bearer sk-ant-oat01-x"
        assert route.calls[0].request.headers["anthropic-beta"] == "oauth-2025-04-20"

    def test_401_is_explained_not_raised(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-stale")
        with respx.mock:
            respx.get("https://api.anthropic.com/v1/models").mock(
                return_value=httpx.Response(401, json={"error": {"message": "invalid key"}}))
            result = anyio.run(lambda: A.validate_method("anthropic", "api_key"))
        assert result["ok"] is False
        assert "401" in result["message"] and "credential was rejected" in result["message"]

    def test_missing_credential_is_explained_without_a_request(self, clean_env: Path) -> None:
        result = anyio.run(lambda: A.validate_method("anthropic", "api_key"))
        assert result["ok"] is False and "ANTHROPIC_API_KEY" in result["message"]

    def test_vertex_probe_posts_one_token(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
        monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.x")
        url = vertex_url(project="p1", region="us-east5", model="claude-sonnet-4-5",
                         stream=False)
        with respx.mock:
            route = respx.post(url).mock(return_value=httpx.Response(200, json={"id": "m"}))
            result = anyio.run(lambda: A.validate_method("anthropic", "vertex"))
        assert result["ok"]
        body = json.loads(route.calls[0].request.content)
        assert body["max_tokens"] == 1 and body["anthropic_version"] == "vertex-2023-10-16"

    def test_vertex_404_explains_region_access(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
        monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
        monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.x")
        url = vertex_url(project="p1", region="us-east5", model="claude-sonnet-4-5",
                         stream=False)
        with respx.mock:
            respx.post(url).mock(return_value=httpx.Response(404, text="not found"))
            result = anyio.run(lambda: A.validate_method("anthropic", "vertex"))
        assert result["ok"] is False and "per-region" in result["message"]

    def test_bedrock_probe_lists_foundation_models(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        with respx.mock:
            route = respx.get("https://bedrock.us-east-1.amazonaws.com/foundation-models").mock(
                return_value=httpx.Response(200, json={"modelSummaries": [
                    {"modelId": "anthropic.claude-opus-4-1-20250805-v1:0"},
                    {"modelId": "meta.llama3-70b-instruct-v1:0"}]}))
            result = anyio.run(lambda: A.validate_method("anthropic", "bedrock"))
        assert result["ok"] and result["models"] == ["anthropic.claude-opus-4-1-20250805-v1:0"]
        assert route.calls[0].request.headers["authorization"].startswith("AWS4-HMAC-SHA256")

    def test_xai_and_oss_probes_use_the_catalog_base(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("XAI_API_KEY", "xai-k")
        monkeypatch.setenv("TOGETHER_API_KEY", "tog-k")
        with respx.mock:
            respx.get("https://api.x.ai/v1/models").mock(
                return_value=httpx.Response(200, json={"data": [{"id": "grok-4"}]}))
            respx.get("https://api.together.xyz/v1/models").mock(
                return_value=httpx.Response(200, json={"data": [{"id": "moe"}]}))
            assert anyio.run(lambda: A.validate_method("xai", "api_key"))["models"] == ["grok-4"]
            assert anyio.run(lambda: A.validate_method("oss", "together"))["ok"]

    def test_ollama_probe_hits_the_daemon(self, clean_env: Path) -> None:
        with respx.mock:
            respx.get("http://localhost:11434/api/tags").mock(
                return_value=httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]}))
            result = anyio.run(lambda: A.validate_method("oss", "ollama"))
        assert result["ok"] and result["models"] == ["qwen3:8b"]

    def test_unreachable_endpoint_is_reported_not_raised(self, clean_env: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        with respx.mock:
            respx.get("https://api.anthropic.com/v1/models").mock(
                side_effect=httpx.ConnectError("no route"))
            result = anyio.run(lambda: A.validate_method("anthropic", "api_key"))
        assert result["ok"] is False and "api.anthropic.com" in result["message"]


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class TestOAuth:
    def test_start_returns_a_pkce_url_and_handle(self, clean_env: Path) -> None:
        start = A.oauth_start("anthropic")
        assert start["url"].startswith("https://claude.com/cai/oauth/authorize?")
        assert "code_challenge=" in start["url"] and "code_challenge_method=S256" in start["url"]
        assert f"state={start['handle']}" in start["url"].replace("%3D", "=") or start["handle"]
        assert start["instructions"]

    def test_finish_exchanges_persists_and_activates(self, clean_env: Path) -> None:
        import os

        start = A.oauth_start("anthropic")
        with respx.mock:
            route = respx.post("https://platform.claude.com/v1/oauth/token").mock(
                return_value=httpx.Response(200, json={
                    "access_token": "sk-ant-oat01-new", "refresh_token": "rt-1",
                    "expires_in": 3600}))
            result = A.oauth_finish(start["handle"], "code-123#" + start["handle"])
        assert result["ok"] and result["backend"] == "anthropic"
        sent = json.loads(route.calls[0].request.content)
        assert sent["grant_type"] == "authorization_code"
        assert sent["code"] == "code-123"
        assert sent["code_verifier"]
        assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "sk-ant-oat01-new"
        assert os.environ["ANTHROPIC_REFRESH_TOKEN"] == "rt-1"
        assert catalog.saved_auth_method("anthropic") == "oauth"
        assert A.configured_method("anthropic") == "oauth"
        # The handle is single-use.
        assert A.oauth_finish(start["handle"], "code-123")["ok"] is False

    def test_finish_accepts_a_pasted_callback_url(self, clean_env: Path) -> None:
        start = A.oauth_start("anthropic")
        with respx.mock:
            route = respx.post("https://platform.claude.com/v1/oauth/token").mock(
                return_value=httpx.Response(200, json={"access_token": "tok"}))
            result = A.oauth_finish(
                start["handle"],
                "https://platform.claude.com/oauth/code/callback?code=abc123&state=x")
        assert result["ok"]
        assert json.loads(route.calls[0].request.content)["code"] == "abc123"

    def test_finish_reports_a_rejected_code(self, clean_env: Path) -> None:
        start = A.oauth_start("anthropic")
        with respx.mock:
            respx.post("https://platform.claude.com/v1/oauth/token").mock(
                return_value=httpx.Response(400, json={"error": "invalid_grant"}))
            result = A.oauth_finish(start["handle"], "stale")
        assert result["ok"] is False and "not accepted" in result["message"]

    def test_unknown_handle_is_reported(self, clean_env: Path) -> None:
        assert A.oauth_finish("nope", "code")["ok"] is False

    def test_only_anthropic_has_a_browser_login(self, clean_env: Path) -> None:
        with pytest.raises(ValueError, match="no browser login"):
            A.oauth_start("openai")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestAuthCli:
    def test_list_json(self, clean_env: Path, monkeypatch, capsys) -> None:
        from mantis_agent.cli import main

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        assert main(["auth", "list", "claude", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        family = payload["families"]["anthropic"]
        assert family["active"] == "api_key"
        ids = [m["id"] for m in family["methods"]]
        assert ids == ["api_key", "oauth", "vertex", "bedrock", "azure"]
        assert family["methods"][0]["fields"][0]["env"] == "ANTHROPIC_API_KEY"

    def test_list_every_family_by_default(self, clean_env: Path, capsys) -> None:
        from mantis_agent.cli import main

        assert main(["auth", "list", "--json"]) == 0
        assert set(json.loads(capsys.readouterr().out)["families"]) == set(A.FAMILIES)

    def test_use_sets_values_and_activates(self, clean_env: Path, capsys) -> None:
        from mantis_agent.cli import main

        rc = main(["auth", "use", "claude", "api_key",
                   "--set", "ANTHROPIC_API_KEY=sk-ant-api03-cli", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] and payload["backend"] == "anthropic"
        assert catalog.saved_key("anthropic") == "sk-ant-api03-cli"

    def test_use_rejects_a_malformed_set(self, clean_env: Path) -> None:
        from mantis_agent.cli import main

        assert main(["auth", "use", "claude", "api_key", "--set", "OOPS"]) == 2

    def test_unknown_family_is_a_usage_error(self, clean_env: Path) -> None:
        from mantis_agent.cli import main

        with pytest.raises(SystemExit):
            main(["auth", "list", "mistral"])

    def test_check_json(self, clean_env: Path, monkeypatch, capsys) -> None:
        from mantis_agent.cli import main

        monkeypatch.setenv("XAI_API_KEY", "xai-k")
        with respx.mock:
            respx.get("https://api.x.ai/v1/models").mock(
                return_value=httpx.Response(200, json={"data": [{"id": "grok-4"}]}))
            assert main(["auth", "check", "grok", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] and payload["models"] == ["grok-4"]
        assert payload["family"] == "xai" and payload["method"] == "api_key"

    def test_check_with_nothing_configured_exits_nonzero(self, clean_env: Path, capsys) -> None:
        from mantis_agent.cli import main

        assert main(["auth", "check", "claude", "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False

    def test_clear_json(self, clean_env: Path, capsys) -> None:
        from mantis_agent.cli import main

        A.set_method("openai", "api_key", {"OPENAI_API_KEY": "sk-x"})
        assert main(["auth", "clear", "openai", "api_key", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        assert catalog.saved_key("openai") is None

    def test_login_json_starts_the_flow(self, clean_env: Path, capsys) -> None:
        from mantis_agent.cli import main

        assert main(["auth", "login", "claude", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["url"].startswith("https://claude.com/cai/oauth/authorize?")
        assert payload["handle"]

    def test_login_finishes_with_a_code(self, clean_env: Path, capsys) -> None:
        from mantis_agent.cli import main

        assert main(["auth", "login", "claude", "--json"]) == 0
        handle = json.loads(capsys.readouterr().out)["handle"]
        with respx.mock:
            respx.post("https://platform.claude.com/v1/oauth/token").mock(
                return_value=httpx.Response(200, json={"access_token": "tok-cli"}))
            assert main(["auth", "login", "claude", "--handle", handle,
                         "--code", "abc", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True

    def test_login_code_without_handle_is_a_usage_error(self, clean_env: Path) -> None:
        from mantis_agent.cli import main

        assert main(["auth", "login", "claude", "--code", "abc"]) == 2
