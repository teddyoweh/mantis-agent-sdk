"""Auto-detecting the Claude Code and Codex CLIs' logins.

What this pins: a Codex ChatGPT sign-in enables OpenAI and routes it to the
ChatGPT Codex backend (with the backend's wire quirks), a Codex-stored API key
is reused as a plain OpenAI key, an explicit API key still wins, a token near
expiry is refreshed and written back in Codex's format, and Claude Code's login
is detected but never read.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import respx

from mantis_agent import auth_methods as A
from mantis_agent import catalog, cli_logins
from mantis_agent.events import ContentBlockDelta, MessageDelta
from mantis_agent.providers.openai_compat import OpenAICompatProvider
from mantis_agent.routing import resolve_backend
from mantis_agent.types import UserMessage


def _jwt(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


def _write_codex(home: Path, *, exp_in: float = 3600, api_key: str | None = None,
                 chatgpt: bool = True) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"OPENAI_API_KEY": api_key, "last_refresh": "2026-09-20T00:00:00Z"}
    if chatgpt:
        data["tokens"] = {
            "id_token": "id-old",
            "access_token": _jwt({
                "exp": time.time() + exp_in,
                "client_id": "app_test",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-123", "chatgpt_plan_type": "plus",
                },
            }),
            "refresh_token": "rt-old",
            "account_id": "acct-123",
        }
    path = home / "auth.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated mantis home and no credential env vars."""
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "mantis"))
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("OPENAI_API_KEY", "MANTIS_AGENT_BASE_URL", "MANTIS_AGENT_API_KEY",
                "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    codex = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return codex


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_nothing_detected_without_logins(home: Path) -> None:
    assert cli_logins.detect_codex()["signed_in"] is False
    assert cli_logins.has_chatgpt_login() is False
    assert cli_logins.detect_claude_code()["signed_in"] is False
    assert catalog.is_enabled(catalog.BY_ID["openai"]) is False


def test_codex_chatgpt_login_detected(home: Path) -> None:
    _write_codex(home)
    info = cli_logins.detect_codex()
    assert info["mode"] == "chatgpt" and info["plan"] == "plus"
    assert cli_logins.chatgpt_account_id() == "acct-123"
    assert cli_logins.chatgpt_headers()["chatgpt-account-id"] == "acct-123"


def test_disable_env_turns_the_route_off(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_codex(home)
    monkeypatch.setenv("MANTIS_DISABLE_CODEX_LOGIN", "1")
    assert cli_logins.has_chatgpt_login() is False
    assert catalog.is_enabled(catalog.BY_ID["openai"]) is False


def test_claude_code_detected_but_never_read(home: Path, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "secret"}}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    info = cli_logins.detect_claude_code()
    assert info == {**info, "signed_in": True, "source": "file"}
    assert "secret" not in json.dumps(info)
    oauth = A.method_status("anthropic")["oauth"]
    assert oauth["configured"] is False  # detection only — no token lifted
    assert "Claude Code is signed in" in oauth["hint"]
    assert not os.environ.get("ANTHROPIC_AUTH_TOKEN")


def test_claude_keychain_detection(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_logins, "_claude_keychain_entry", lambda: True)
    assert cli_logins.detect_claude_code()["source"] == "keychain"


# ---------------------------------------------------------------------------
# Auth status + routing
# ---------------------------------------------------------------------------


def test_chatgpt_login_becomes_active_openai_method(home: Path) -> None:
    _write_codex(home)
    status = A.method_status("openai")
    assert status["chatgpt"]["configured"] and status["chatgpt"]["active"]
    assert status["chatgpt"]["source"] == "cli"
    assert "plus plan" in status["chatgpt"]["hint"]
    assert catalog.is_enabled(catalog.BY_ID["openai"])
    assert resolve_backend("gpt-5.5") == cli_logins.CHATGPT_CODEX_URL
    assert catalog.bearer_backend(catalog.BY_ID["openai"]) == cli_logins.CHATGPT_CODEX_URL


def test_explicit_api_key_outranks_chatgpt_login(home: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _write_codex(home)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    status = A.method_status("openai")
    assert status["api_key"]["active"] and not status["chatgpt"]["active"]
    assert resolve_backend("gpt-5.5") == "https://api.openai.com/v1"


def test_codex_api_key_is_reused(home: Path) -> None:
    _write_codex(home, api_key="sk-from-codex", chatgpt=False)
    prov = catalog.BY_ID["openai"]
    assert catalog.api_key_for(prov) == "sk-from-codex"
    status = A.method_status("openai")["api_key"]
    assert status["configured"] and status["source"] == "cli"
    assert "Codex" in status["hint"]


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


@respx.mock
def test_expiring_token_is_refreshed_and_written_back(home: Path) -> None:
    path = _write_codex(home, exp_in=10)
    new_access = _jwt({"exp": time.time() + 3600})
    route = respx.post("https://auth.openai.com/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": new_access, "refresh_token": "rt-new", "id_token": "id-new",
        })
    )
    assert cli_logins.chatgpt_access_token() == new_access
    sent = json.loads(route.calls[0].request.content)
    assert sent["refresh_token"] == "rt-old" and sent["client_id"] == "app_test"
    saved = json.loads(path.read_text())
    assert saved["tokens"]["refresh_token"] == "rt-new"
    assert saved["tokens"]["account_id"] == "acct-123"  # untouched keys kept
    assert saved["last_refresh"] != "2026-09-20T00:00:00Z"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_fresh_token_is_not_refreshed(home: Path) -> None:
    _write_codex(home, exp_in=3600)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://auth.openai.com/oauth/token")
        cli_logins.chatgpt_access_token()
        assert not route.called


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


@respx.mock
def test_chatgpt_backend_wire_and_stream(home: Path) -> None:
    _write_codex(home)
    events = [
        {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "Hel"},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "lo"},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"type": "function_call", "call_id": "call_1", "name": "add",
                  "arguments": "{\"a\":1,\"b\":2}"}},
        # The backend leaves ``output`` empty here; items came above.
        {"type": "response.completed",
         "response": {"output": [], "usage": {"input_tokens": 9, "output_tokens": 4}}},
    ]
    route = respx.post(f"{cli_logins.CHATGPT_CODEX_URL}/responses").mock(
        return_value=httpx.Response(200, content=_sse(events),
                                    headers={"content-type": "text/event-stream"})
    )
    provider = OpenAICompatProvider(base_url=cli_logins.CHATGPT_CODEX_URL)
    tools = [{"name": "add", "description": "add", "input_schema": {
        "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}}]

    async def run() -> list[Any]:
        return [ev async for ev in provider.stream(
            model="gpt-5.5", messages=[UserMessage(content="hi")], system="Be terse.",
            tools=tools, max_tokens=4096,
        )]

    out = anyio.run(run)
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["stream"] is True and body["store"] is False
    assert body["instructions"] == "Be terse."
    assert "max_output_tokens" not in body
    assert req.headers["chatgpt-account-id"] == "acct-123"
    assert req.headers["authorization"].startswith("Bearer eyJ")
    assert req.headers["originator"] == "codex_cli_rs"

    text = "".join(ev.delta.text for ev in out
                   if isinstance(ev, ContentBlockDelta) and hasattr(ev.delta, "text"))
    assert text == "Hello"
    args = "".join(ev.delta.partial_json for ev in out
                   if isinstance(ev, ContentBlockDelta) and hasattr(ev.delta, "partial_json"))
    assert json.loads(args) == {"a": 1, "b": 2}
    final = next(ev for ev in out if isinstance(ev, MessageDelta))
    assert final.stop_reason == "tool_use" and final.usage.output_tokens == 4


@respx.mock
def test_env_openai_key_never_sent_to_chatgpt_backend(home: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    _write_codex(home)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-platform")
    route = respx.post(f"{cli_logins.CHATGPT_CODEX_URL}/responses").mock(
        return_value=httpx.Response(200, content=_sse([
            {"type": "response.completed", "response": {"usage": {}}}]))
    )
    provider = OpenAICompatProvider(base_url=cli_logins.CHATGPT_CODEX_URL)

    async def run() -> None:
        async for _ in provider.stream(model="gpt-5.5", messages=[UserMessage(content="hi")]):
            pass

    anyio.run(run)
    assert "sk-platform" not in route.calls[0].request.headers["authorization"]


@respx.mock
def test_failed_response_raises_provider_error(home: Path) -> None:
    from mantis_agent.errors import ProviderError

    _write_codex(home)
    respx.post(f"{cli_logins.CHATGPT_CODEX_URL}/responses").mock(
        return_value=httpx.Response(200, content=_sse([
            {"type": "response.failed",
             "response": {"error": {"message": "usage limit reached"}}}]))
    )
    provider = OpenAICompatProvider(base_url=cli_logins.CHATGPT_CODEX_URL)

    async def run() -> None:
        async for _ in provider.stream(model="gpt-5.5", messages=[UserMessage(content="hi")]):
            pass

    with pytest.raises(ProviderError, match="usage limit reached"):
        anyio.run(run)


def test_codex_models_skip_hidden(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "models_cache.json").write_text(json.dumps({"client_version": "0.200.0", "models": [
        {"slug": "gpt-reserve", "visibility": "hide"},
        {"slug": "gpt-5.5", "visibility": "list"},
    ]}))
    assert cli_logins.codex_models() == ["gpt-5.5"]
    assert cli_logins.codex_client_version() == "0.200.0"
