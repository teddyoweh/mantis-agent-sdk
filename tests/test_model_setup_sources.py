"""`mantis setup` must cover every model source — local, hosted API, free
hosted, self-host, and Anthropic/Claude — and route each correctly. These lock
in the wiring the /loop built so a regression can't silently drop a source.
"""

from __future__ import annotations

import mantis_agent.catalog as catalog
from mantis_agent.setup_wizard import (
    FREE_PROVIDER_IDS,
    _arrow_select,
    _ping_chat_model,
    _probe_openai_models,
)


# -- Anthropic / Claude ------------------------------------------------------


def test_anthropic_is_a_catalog_provider() -> None:
    prov = catalog.BY_ID.get("anthropic")
    assert prov is not None
    assert prov.base_url == "https://api.anthropic.com/v1"
    assert prov.api_key_env == "ANTHROPIC_API_KEY"


def test_claude_models_route_to_anthropic() -> None:
    for m in ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"):
        prov = catalog.provider_for_model(m)
        assert prov is not None and prov.id == "anthropic", m


def test_anthropic_enabled_via_auth_token(monkeypatch) -> None:
    # A Claude OAuth / gateway Bearer token (ANTHROPIC_AUTH_TOKEN) must count as
    # "enabled" even with no x-api-key in the store.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    assert catalog.is_enabled(catalog.BY_ID["anthropic"]) is True


def test_anthropic_bearer_ping_unreachable_does_not_block() -> None:
    from mantis_agent.setup_wizard import _ping_anthropic_bearer
    ok, _detail = _ping_anthropic_bearer("http://127.0.0.1:59999/v1", "claude-x", "tok")
    assert ok is True


class _Resp:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


def test_anthropic_ping_rejects_only_real_auth_errors(monkeypatch) -> None:
    # A valid key that lacks access to the flagship (404/permission) must NOT be
    # reported as an invalid credential — only 401 / authentication_error does.
    import httpx

    from mantis_agent import setup_wizard as sw

    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _Resp(401, {"error": {"type": "authentication_error", "message": "bad key"}}))
    assert sw._ping_anthropic_model("https://api.anthropic.com/v1", "claude-opus-4-8", "k")[0] is False

    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _Resp(404, {"error": {"type": "not_found_error", "message": "model"}}))
    assert sw._ping_anthropic_model("https://api.anthropic.com/v1", "claude-opus-4-8", "k")[0] is True

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(200, {}))
    assert sw._ping_anthropic_model("https://api.anthropic.com/v1", "claude-opus-4-8", "k")[0] is True


def test_anthropic_uses_x_api_key_not_bearer() -> None:
    anth = catalog.BY_ID["anthropic"]
    h = catalog._models_headers(anth, "sk-secret")
    assert h["x-api-key"] == "sk-secret"
    assert "anthropic-version" in h
    assert "Authorization" not in h


def test_openai_compat_providers_use_bearer() -> None:
    h = catalog._models_headers(catalog.BY_ID["openai"], "sk-secret")
    assert h == {"Authorization": "Bearer sk-secret"}


def test_no_key_means_no_auth_headers() -> None:
    assert catalog._models_headers(catalog.BY_ID["anthropic"], None) == {}


# -- Free hosted filter ------------------------------------------------------


def test_free_provider_ids_are_all_real_catalog_ids() -> None:
    for pid in FREE_PROVIDER_IDS:
        assert pid in catalog.BY_ID, pid


# -- Self-host probe ---------------------------------------------------------


def test_selfhost_probe_unreachable_returns_none() -> None:
    # A closed port must degrade to None (→ manual model entry), never raise.
    assert _probe_openai_models("http://127.0.0.1:59999/v1", "") is None


# -- Model ping (validate-before-save) ---------------------------------------


def test_hosted_flow_end_to_end_saves_model(monkeypatch, tmp_path) -> None:
    # Drive the WHOLE hosted setup orchestration (not just helpers): pick a
    # provider → paste key → validate → pick a model → confirm → save. Mocks the
    # network + I/O; asserts the model is persisted as the default.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from mantis_agent import setup_wizard as sw

    inputs = iter(["1", "1"])  # provider #1 (DeepSeek), then model #1
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr("getpass.getpass", lambda *a: "sk-test-key")
    monkeypatch.setattr(catalog, "validate_provider", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(catalog, "refresh_live_models", lambda *a, **k: ["deepseek-chat", "deepseek-reasoner"])
    monkeypatch.setattr(sw, "_confirm_model", lambda *a, **k: True)

    try:
        rc = sw._run_hosted(_NullConsole(), free_only=False)
        assert rc == 0
        last = catalog.get_last_model()
        assert last and last["model"] == "deepseek-chat"
        assert last["backend"] == catalog.BY_ID["deepseek"].base_url
    finally:
        catalog.clear_key("deepseek")


def test_hosted_flow_aborts_when_key_invalid(monkeypatch, tmp_path) -> None:
    # A rejected key must NOT save anything and must clear the bad key.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from mantis_agent import setup_wizard as sw

    monkeypatch.setattr("builtins.input", lambda *a: "1")
    monkeypatch.setattr("getpass.getpass", lambda *a: "bad-key")
    monkeypatch.setattr(catalog, "validate_provider", lambda *a, **k: (False, "invalid API key"))

    rc = sw._run_hosted(_NullConsole(), free_only=False)
    assert rc == 1
    assert catalog.saved_key("deepseek") is None


def test_selfhost_flow_end_to_end_saves_model(monkeypatch, tmp_path) -> None:
    # URL → probe /v1/models → pick → confirm → save backend+model.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    from mantis_agent import setup_wizard as sw

    inputs = iter(["http://localhost:9911/v1", "1"])  # base URL, then model #1
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr("getpass.getpass", lambda *a: "")  # local server, no key
    monkeypatch.setattr(sw, "_probe_openai_models", lambda *a, **k: ["local-coder"])
    monkeypatch.setattr(sw, "_confirm_model", lambda *a, **k: True)

    rc = sw._run_selfhost(_NullConsole())
    assert rc == 0
    last = catalog.get_last_model()
    assert last and last["model"] == "local-coder"
    assert last["backend"] == "http://localhost:9911/v1"


def test_anthropic_apikey_flow_end_to_end(monkeypatch, tmp_path) -> None:
    # Claude auth chooser → API key → validate → pick model → save.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from mantis_agent import setup_wizard as sw

    inputs = iter(["1", "1"])  # auth method #1 (API key), then model #1
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr("getpass.getpass", lambda *a: "sk-ant-key")
    monkeypatch.setattr(sw, "_ping_anthropic_model", lambda *a, **k: (True, "ok"))

    try:
        rc = sw._run_anthropic(_NullConsole(), catalog.BY_ID["anthropic"])
        assert rc == 0
        last = catalog.get_last_model()
        assert last and last["model"].startswith("claude-")
        assert catalog.saved_key("anthropic") == "sk-ant-key"
    finally:
        catalog.clear_key("anthropic")


def test_local_flow_end_to_end_saves_model(monkeypatch, tmp_path) -> None:
    # Local Ollama flow: ensure server → pull → verify → save as default.
    # Mocks the ollama subprocess/daemon; asserts the tag is persisted @ 11434.
    import subprocess
    import types

    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    from mantis_agent import setup_local
    from mantis_agent import setup_wizard as sw

    monkeypatch.setattr(setup_local, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(setup_local, "start_ollama_server", lambda: (True, ""))
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: 0)  # the `ollama pull`
    monkeypatch.setattr(sw, "_ollama_has", lambda tag: True)

    args = types.SimpleNamespace(model="qwen2.5-coder:7b", list_only=False, auto=False)
    rc = sw._run_local(_NullConsole(), args)
    assert rc == 0
    last = catalog.get_last_model()
    assert last and last["model"] == "qwen2.5-coder:7b"
    assert "11434" in (last["backend"] or "")


def test_local_flow_aborts_when_pull_fails(monkeypatch, tmp_path) -> None:
    import subprocess
    import types

    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    from mantis_agent import setup_local
    from mantis_agent import setup_wizard as sw

    monkeypatch.setattr(setup_local, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(setup_local, "start_ollama_server", lambda: (True, ""))
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: 1)  # pull fails
    args = types.SimpleNamespace(model="qwen2.5-coder:7b", list_only=False, auto=False)
    assert sw._run_local(_NullConsole(), args) == 1


def test_run_setup_entry_points_exit_cleanly_on_cancel(monkeypatch, tmp_path) -> None:
    # Every `mantis setup [flag]` entry point must exit cleanly (0 or 1) even when
    # the user cancels at the first prompt — never propagate an exception. This
    # codifies the live-binary smoke test as a regression guard.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    from mantis_agent.setup_wizard import run_setup

    def _eof(*_a: object) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    monkeypatch.setattr("getpass.getpass", _eof)
    for argv in ([], ["--status"], ["--list"], ["--hosted"], ["--free"], ["--selfhost"]):
        rc = run_setup(argv)
        assert rc in (0, 1), f"{argv} returned {rc!r}"


def test_hosted_provider_round_trip_wires_backend_and_key(monkeypatch, tmp_path) -> None:
    # The common case: an OpenAI-compat hosted provider (DeepSeek/OpenAI/Groq/…)
    # set up with a key must restore its model + backend + key and build a
    # NON-anthropic provider pointed at that backend.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MANTIS_AGENT_MODEL", raising=False)
    catalog.set_key("deepseek", "sk-deepseek")
    catalog.set_last_model("deepseek-chat", "https://api.deepseek.com/v1")

    from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider
    from mantis_agent.tui import MantisTUI

    try:
        t = MantisTUI(model="qwen2.5-7b-instruct", backend="http://localhost:11434",
                      api_key=None, system=None, max_tokens=1, temperature=None, max_turns=1)
        t._restore_last_model()
        assert t.model == "deepseek-chat"
        assert t.backend == "https://api.deepseek.com/v1"
        assert t.api_key == "sk-deepseek"
        agent = t._build_agent()
        assert not isinstance(agent.provider, AnthropicPassthroughProvider)
    finally:
        catalog.clear_key("deepseek")


def test_oauth_token_round_trip_native_anthropic(monkeypatch, tmp_path) -> None:
    # Native OAuth-token path: a claude-* model + ANTHROPIC_AUTH_TOKEN (no api
    # key) must restore via provider_for_model + the token branch, build the
    # passthrough with Bearer, AND carry the required oauth-beta header.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-tok")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MANTIS_AGENT_MODEL", raising=False)
    catalog.set_last_model("claude-opus-4-8", "https://api.anthropic.com/v1")

    from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider
    from mantis_agent.tui import MantisTUI

    t = MantisTUI(model="qwen2.5-7b-instruct", backend="http://localhost:11434",
                  api_key=None, system=None, max_tokens=1, temperature=None, max_turns=1)
    t._restore_last_model()
    assert t.model == "claude-opus-4-8"
    assert "anthropic.com" in (t.backend or "")

    agent = t._build_agent()
    assert isinstance(agent.provider, AnthropicPassthroughProvider)
    h = {k.lower(): v for k, v in dict(agent.provider.client.headers).items()}
    assert h.get("authorization") == "Bearer oauth-tok"
    assert h.get("anthropic-beta") == "oauth-2025-04-20"  # native → oauth beta required
    assert "x-api-key" not in h


def test_gateway_round_trip_builds_passthrough_with_bearer(monkeypatch, tmp_path) -> None:
    # The full gateway round-trip: a saved gateway model+URL + ANTHROPIC_AUTH_TOKEN
    # must restore and build an anthropic_passthrough provider that authenticates
    # with Bearer (not x-api-key). This is what makes Bedrock/Vertex/Azure work.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gw-token")
    monkeypatch.delenv("MANTIS_AGENT_MODEL", raising=False)
    catalog.set_last_model("anthropic.claude-sonnet-4-5-v1:0", "https://gw.example.com/anthropic/v1")

    from mantis_agent.providers.anthropic_passthrough import AnthropicPassthroughProvider
    from mantis_agent.tui import MantisTUI

    t = MantisTUI(model="qwen2.5-7b-instruct", backend="http://localhost:11434",
                  api_key=None, system=None, max_tokens=1, temperature=None, max_turns=1)
    t._restore_last_model()
    assert t.model == "anthropic.claude-sonnet-4-5-v1:0"
    assert t.backend == "https://gw.example.com/anthropic/v1"

    agent = t._build_agent()
    assert isinstance(agent.provider, AnthropicPassthroughProvider)
    headers = {k.lower(): v for k, v in dict(agent.provider.client.headers).items()}
    assert headers.get("authorization") == "Bearer gw-token"
    assert "x-api-key" not in headers


def test_anthropic_gateway_urls_route_to_passthrough() -> None:
    # Bedrock/Vertex/Azure Anthropic-Messages gateways (the /anthropic path
    # convention) must use the passthrough (/v1/messages), not OpenAI-compat.
    from mantis_agent.providers.base import detect_provider
    assert detect_provider("https://gw.example.com/anthropic/v1") == "anthropic_passthrough"
    assert detect_provider("https://foundry.services.ai.azure.com/anthropic") == "anthropic_passthrough"
    assert detect_provider("https://api.anthropic.com/v1") == "anthropic_passthrough"
    # No false positive: an OpenAI-compat path that merely contains 'anthropic'.
    assert detect_provider("https://api.example.com/anthropic-compat/v1") == "openai_compat"


def test_anthropic_gateway_flow_uses_typed_model_id(monkeypatch, tmp_path) -> None:
    # A Bedrock/Vertex/Azure gateway uses provider-specific model ids, so the
    # gateway path must take a TYPED id (not the direct-Anthropic flagship pick).
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from mantis_agent import setup_wizard as sw

    inputs = iter(["3", "https://gw.example.com/anthropic/v1", "anthropic.claude-sonnet-4-5-v1:0"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr("getpass.getpass", lambda *a: "gw-token")
    monkeypatch.setattr(sw, "_ping_anthropic_bearer", lambda *a, **k: (True, "ok"))

    rc = sw._run_anthropic(_NullConsole(), catalog.BY_ID["anthropic"])
    assert rc == 0
    last = catalog.get_last_model()
    assert last["model"] == "anthropic.claude-sonnet-4-5-v1:0"
    assert last["backend"] == "https://gw.example.com/anthropic/v1"


def test_print_status_never_crashes() -> None:
    # `mantis setup --status` must render whatever the config is (or nothing)
    # without raising — it runs before any provider is even set up.
    from mantis_agent.setup_wizard import _print_status
    _print_status(_NullConsole())


def test_grouped_provider_models_enabled_before_disabled() -> None:
    # The unified /models picker relies on this ordering: every enabled provider
    # group must come before every disabled one (active/usable models on top).
    groups = catalog.grouped_provider_models()
    seen_disabled = False
    for g in groups:
        if not g["enabled"]:
            seen_disabled = True
        else:
            assert not seen_disabled, "an enabled group appeared after a disabled one"


def test_setting_a_key_enables_and_regroups_provider(monkeypatch, tmp_path) -> None:
    # The inline-enable path relies on this: set_key → is_enabled True → the
    # provider is grouped under "enabled". clear_key reverses it.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    prov = catalog.BY_ID["deepseek"]
    assert catalog.is_enabled(prov) is False
    catalog.set_key("deepseek", "sk-test-key")
    try:
        assert catalog.is_enabled(prov) is True
        enabled_ids = [g["provider_id"] for g in catalog.grouped_provider_models() if g["enabled"]]
        assert "deepseek" in enabled_ids
    finally:
        catalog.clear_key("deepseek")
    assert catalog.is_enabled(prov) is False


def test_grouped_provider_models_are_real_providers_with_lists() -> None:
    for g in catalog.grouped_provider_models():
        assert g["provider_id"] in catalog.BY_ID
        assert isinstance(g["models"], list)
        assert "enabled" in g


def test_model_ping_unreachable_does_not_block_save() -> None:
    # A network flake must NOT block a save — returns (True, "") so setup is
    # usable offline / behind a proxy. (A real 4xx from the model is separate.)
    ok, _detail = _ping_chat_model("http://127.0.0.1:59999/v1", "some-model", "")
    assert ok is True


def test_confirm_model_routes_anthropic_to_messages_api(monkeypatch) -> None:
    from mantis_agent import setup_wizard as sw
    hit = {}
    monkeypatch.setattr(sw, "_ping_anthropic_model", lambda *a, **k: (hit.__setitem__("anthropic", 1), (True, "ok"))[1])
    monkeypatch.setattr(sw, "_ping_chat_model", lambda *a, **k: (hit.__setitem__("chat", 1), (True, "ok"))[1])
    sw._confirm_model(_NullConsole(), "https://api.anthropic.com/v1", "claude-opus-4-8", "k")
    assert hit.get("anthropic") and not hit.get("chat")


def test_confirm_model_routes_openai_to_chat_completions(monkeypatch) -> None:
    from mantis_agent import setup_wizard as sw
    hit = {}
    monkeypatch.setattr(sw, "_ping_anthropic_model", lambda *a, **k: (hit.__setitem__("anthropic", 1), (True, "ok"))[1])
    monkeypatch.setattr(sw, "_ping_chat_model", lambda *a, **k: (hit.__setitem__("chat", 1), (True, "ok"))[1])
    sw._confirm_model(_NullConsole(), "https://api.openai.com/v1", "gpt-4o", "k")
    assert hit.get("chat") and not hit.get("anthropic")


# -- Arrow selector ----------------------------------------------------------


def test_arrow_select_non_tty_returns_sentinel() -> None:
    # Under pytest stdin/stdout aren't TTYs — the selector must return -1 so
    # callers fall back to numeric input() instead of hanging on app.run().
    assert _arrow_select("pick", [("a", "x"), ("b", "y")]) == -1


class _NullConsole:
    def print(self, *a: object, **k: object) -> None:  # noqa: D102
        pass


def test_pick_model_id_empty_list_returns_none() -> None:
    # A provider that returned no models must not crash the picker (was IndexError
    # on the "Enter=<first>" prompt) — it returns None so the caller can bail.
    from mantis_agent import setup_wizard as sw
    assert sw._pick_model_id(_NullConsole(), []) is None


def test_pick_model_id_numeric_fallback(monkeypatch) -> None:
    from mantis_agent import setup_wizard as sw
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert sw._pick_model_id(_NullConsole(), ["m-a", "m-b", "m-c"]) == "m-b"


def test_pick_model_id_enter_takes_first(monkeypatch) -> None:
    from mantis_agent import setup_wizard as sw
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert sw._pick_model_id(_NullConsole(), ["first", "second"]) == "first"


def test_pick_model_id_accepts_typed_exact_id(monkeypatch) -> None:
    from mantis_agent import setup_wizard as sw
    monkeypatch.setattr("builtins.input", lambda *a: "org/my-custom-model")
    assert sw._pick_model_id(_NullConsole(), ["m-a"]) == "org/my-custom-model"
