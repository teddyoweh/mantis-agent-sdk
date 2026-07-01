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


def test_lookup_pricing_unknown_degrades_to_none_not_crash() -> None:
    # Cost tracking must skip USD (return None) for models with no pricing entry
    # rather than crash or invent a number — wrong cost figures are worse than $0.
    from mantis_agent.budget import lookup_pricing
    assert lookup_pricing("totally-made-up-model-xyz-9000", "openai") is None
    assert lookup_pricing("totally-made-up-model-xyz-9000", None) is None


def test_real_registry_tools_normalize_to_anthropic_shape() -> None:
    # Claude selection is only useful if mantis's ACTUAL tools work: every tool
    # the registry emits (OpenAI shape) must convert to Anthropic's
    # {name, description, input_schema} shape — a mismatch silently breaks tools.
    from mantis_agent.providers.anthropic_passthrough import _normalize_tools
    from mantis_agent.tui import MantisTUI

    t = MantisTUI(model="claude-opus-4-8", backend="https://api.anthropic.com/v1",
                  api_key="x", system=None, max_tokens=1, temperature=None, max_turns=1)
    specs = t._build_agent().tools.to_wire()
    assert specs, "registry should produce tool specs"
    norm = _normalize_tools(specs)
    assert len(norm) == len(specs)
    for x in norm:
        assert "name" in x and isinstance(x.get("input_schema"), dict), x.get("name")


def test_agent_resolves_correct_capabilities_for_openai_flagship() -> None:
    # End-to-end runtime propagation: an Agent on OpenAI must resolve gpt-4o to
    # native tools + 128k context AND the openai backend profile — proving the
    # capability/profile fixes actually reach the agent (not just the tables).
    from mantis_agent.agent import Agent
    a = Agent(model="gpt-4o", backend="https://api.openai.com/v1")
    assert a.model_capability.supports_native_tools is True
    assert a.model_capability.context_window == 128000
    assert a.backend_capability is not None
    assert a.backend_capability.provider_hint == "openai"
    assert a.backend_capability.supports_native_tools is True


def test_openai_compat_chat_url_preserves_versioned_base() -> None:
    # Gemini/GLM chat correctness relies on httpx keeping the base path when a
    # leading-slash "/chat/completions" is joined — lock that behavior so a
    # provider refactor or httpx change can't silently drop "/v1beta/openai".
    from mantis_agent.providers.openai_compat import OpenAICompatProvider
    base = "https://generativelanguage.googleapis.com/v1beta/openai"
    p = OpenAICompatProvider(base_url=base, api_key="k")
    url = str(p.client.build_request("POST", "/chat/completions").url)
    assert url == f"{base}/chat/completions"


def test_available_models_sorts_newest_first(monkeypatch) -> None:
    # The in-app picker's active group (_available_models) must also surface
    # recent models first — separate code path from refresh_live_models.
    import httpx

    import respx as _respx

    from mantis_agent.tui import MantisTUI

    base = "https://api.openai.com/v1"
    with _respx.mock:
        _respx.get(f"{base}/models").mock(return_value=httpx.Response(200, json={"data": [
            {"id": "gpt-old", "created": 100},
            {"id": "gpt-newest", "created": 300},
            {"id": "gpt-mid", "created": 200},
        ]}))
        t = MantisTUI(model="gpt-newest", backend=base, api_key="k",
                      system=None, max_tokens=1, temperature=None, max_turns=1)
        models, ok = t._available_models()
    assert ok
    assert models == ["gpt-newest", "gpt-mid", "gpt-old"]


def test_available_models_endpoint_for_versioned_base(monkeypatch) -> None:
    # Gemini's base already carries a version path (…/v1beta/openai), so the
    # models endpoint is a direct child ({base}/models) — NOT {base}/v1/models,
    # which 404s. Verify _available_models hits the right URL and parses it.
    import httpx

    import respx as _respx

    from mantis_agent.tui import MantisTUI

    base = "https://generativelanguage.googleapis.com/v1beta/openai"
    with _respx.mock:
        route = _respx.get(f"{base}/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "gemini-2.5-pro"}]}))
        t = MantisTUI(model="gemini-2.5-pro", backend=base, api_key="k",
                      system=None, max_tokens=1, temperature=None, max_turns=1)
        models, ok = t._available_models()
    assert ok and "gemini-2.5-pro" in models
    assert route.called


def test_refresh_live_models_sorts_newest_first(monkeypatch) -> None:
    # Providers (OpenAI) return models oldest-first; we sort by "created" desc so
    # the recent flagships surface at the top of the picker, not buried.
    import httpx

    import respx as _respx

    prov = catalog.BY_ID["openai"]
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with _respx.mock:
        _respx.get(f"{prov.base_url.rstrip('/')}/models").mock(return_value=httpx.Response(200, json={"data": [
            {"id": "old", "created": 100},
            {"id": "newest", "created": 300},
            {"id": "mid", "created": 200},
        ]}))
        ids = catalog.refresh_live_models(prov)
    assert ids == ["newest", "mid", "old"]


def test_catalog_validate_hits_direct_models_child_for_gemini(monkeypatch) -> None:
    # Setup-side probe must also hit {base}/models (a direct child) for Gemini's
    # versioned base — not an extra /v1. Guards the setup validation URL.
    import httpx

    import respx as _respx

    prov = catalog.BY_ID["gemini"]  # base = …/v1beta/openai
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    with _respx.mock:
        route = _respx.get(f"{prov.base_url.rstrip('/')}/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "gemini-2.5-pro"}]}))
        ok, _detail = catalog.validate_provider(prov)
    assert ok
    assert route.called


def test_hosted_backend_profiles_have_correct_provider_hints() -> None:
    # Without these, OpenAI/Gemini/GLM/Qwen fell to the generic vLLM profile
    # (hint="vllm"), so cost/pricing tracking used the wrong provider.
    from mantis_agent.capabilities import hosted_profile_from_url
    cases = {
        "https://api.openai.com/v1": "openai",
        "https://generativelanguage.googleapis.com/v1beta/openai": "gemini",
        "https://api.z.ai/api/paas/v4": "glm",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1": "qwen",
    }
    for url, hint in cases.items():
        p = hosted_profile_from_url(url)
        assert p is not None, url
        assert p.provider_hint == hint, (url, p.provider_hint)
        assert p.supports_native_tools is True, url


def test_family_capability_snapshot() -> None:
    # A single guard for every family's (native_tools, context_window) tuned this
    # session — a regression in any one (like the o-series 128k→200k) fails here.
    from mantis_agent.capabilities import lookup_model
    expected = {
        "gpt-4o": (True, 128000), "gpt-5.4": (True, 128000),
        "o1": (True, 200000), "o3-mini": (True, 200000), "o4-mini": (True, 200000),
        "claude-opus-4-8": (True, 200000), "gemini-2.5-pro": (True, 1000000),
        "glm-4.7": (True, 128000), "deepseek-chat": (True, 65536),
        "kimi-latest": (True, 131072), "qwen2.5-coder:7b": (True, 32768),
        "llama3.1:8b": (True, 131072),
    }
    for m, (nt, ctx) in expected.items():
        c = lookup_model(m)
        assert (c.supports_native_tools, c.context_window) == (nt, ctx), (m, c.supports_native_tools, c.context_window)


def test_hosted_flagships_have_native_tools_and_real_context() -> None:
    # Regression: the popular hosted models must resolve to native tool-calling
    # (path A) + their true context windows — not the 8192 / no-tools generic
    # default, which silently degrades tool routing and triggers early compaction.
    from mantis_agent.capabilities import lookup_model
    for m in ("gpt-4o", "gpt-5.4", "gpt-5.5", "o1", "o3", "glm-4.7"):
        c = lookup_model(m)
        assert c.supports_native_tools is True, m
        assert c.context_window >= 128000, (m, c.context_window)
    for m in ("claude-opus-4-8", "claude-sonnet-5"):
        c = lookup_model(m)
        assert c.supports_native_tools is True, m
        assert c.context_window >= 200000, (m, c.context_window)
    assert lookup_model("gemini-2.5-pro").context_window >= 1000000
    # o-series reasoning models have a 200k window (bigger than the 128k gpt-4o).
    for m in ("o1", "o3", "o4-mini"):
        assert lookup_model(m).context_window == 200000, m
    # And the local families stayed correct.
    assert lookup_model("qwen2.5-coder:7b").context_window == 32768


def test_ollama_base_respects_ollama_host(monkeypatch) -> None:
    from mantis_agent.setup_wizard import _ollama_base
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert _ollama_base() == "http://localhost:11434"
    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.5:11435")
    assert _ollama_base() == "http://192.168.1.5:11435"
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box")  # no port → default appended
    assert _ollama_base() == "http://gpu-box:11434"
    monkeypatch.setenv("OLLAMA_HOST", "https://remote.example.com:443")
    assert _ollama_base() == "https://remote.example.com:443"
    # 0.0.0.0 (bind-all) must be rewritten to loopback for the client connection.
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    assert _ollama_base() == "http://127.0.0.1:11434"


def test_no_legit_chat_model_is_over_filtered() -> None:
    # Guard against over-broad _NONCHAT_MARKERS (like the -pro bug): every real
    # chat model across every provider must survive the picker's chat filter.
    from mantis_agent.tui import _is_chat_model
    legit = [
        "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-5.4", "gpt-5.4-mini",
        "gpt-5.5", "o1", "o1-mini", "o3", "o3-mini", "o4-mini", "chatgpt-4o-latest",
        "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001",
        "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash",
        "deepseek-chat", "deepseek-reasoner", "deepseek-v3",
        "qwen-max", "qwen3-235b-a22b", "qwen3-coder-plus", "glm-4.7", "glm-4-plus",
        "kimi-latest", "moonshotai/kimi-k2-instruct-0905",
        "llama-3.3-70b-versatile", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "mixtral-8x7b-instruct", "mistral-large-2", "openai/gpt-oss-120b", "zai-org/GLM-4.7",
    ]
    for m in legit:
        assert _is_chat_model(m) is True, m


def test_pro_filter_excludes_only_openai_responses_models() -> None:
    # The -pro exclusion must be OpenAI-specific: OpenAI's o1-pro/gpt-5-pro are
    # responses-only, but gemini-2.5-pro (and -plus tiers) are normal chat models.
    from mantis_agent.tui import _is_chat_model
    for m in ("gemini-2.5-pro", "gemini-1.5-pro", "qwen3-coder-plus", "glm-4-plus"):
        assert _is_chat_model(m) is True, m
    for m in ("gpt-5.4-pro", "gpt-5-pro", "o1-pro", "o3-pro", "o4-pro"):
        assert _is_chat_model(m) is False, m


def test_catalog_providers_are_wellformed() -> None:
    # Guard the setup fallback data: every hosted provider must have a usable id,
    # https base URL, key env var, and at least one non-empty flagship model id.
    seen_ids = set()
    for p in catalog.CATALOG:
        assert p.id and p.id not in seen_ids, f"duplicate/blank id: {p.id!r}"
        seen_ids.add(p.id)
        assert p.label
        assert p.base_url.startswith("https://"), p.id
        assert p.api_key_env, p.id
        assert len(p.models) >= 1 and all(p.models), p.id


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


def test_free_hosted_openrouter_offers_only_free_models(monkeypatch, tmp_path) -> None:
    # "Free hosted → OpenRouter" must surface only :free models, not paid ones.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from mantis_agent import setup_wizard as sw

    free_ids = [p.id for p in catalog.CATALOG if p.id in sw.FREE_PROVIDER_IDS]
    idx = free_ids.index("openrouter") + 1  # 1-based numeric pick in the free list
    inputs = iter([str(idx), "1"])  # pick openrouter, then model #1
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    monkeypatch.setattr("getpass.getpass", lambda *a: "sk-or-key")
    monkeypatch.setattr(catalog, "validate_provider", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(catalog, "refresh_live_models",
                        lambda *a, **k: ["openai/gpt-4o", "meta-llama/llama-3.3-70b:free", "z-ai/glm:free"])
    monkeypatch.setattr(sw, "_confirm_model", lambda *a, **k: True)

    try:
        rc = sw._run_hosted(_NullConsole(), free_only=True)
        assert rc == 0
        saved = catalog.get_last_model()["model"]
        assert saved.endswith(":free"), saved
    finally:
        catalog.clear_key("openrouter")


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


def test_local_auto_flow_pulls_recommended_model(monkeypatch, tmp_path) -> None:
    # `mantis setup --auto`: no prompts, pull the hardware-recommended model.
    import subprocess
    import types

    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    from mantis_agent import setup_local
    from mantis_agent import setup_wizard as sw

    monkeypatch.setattr(setup_local, "is_ollama_installed", lambda: True)
    monkeypatch.setattr(setup_local, "start_ollama_server", lambda: (True, ""))
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "_ollama_has", lambda tag: True)

    budget, _label = sw.detect_hardware()
    expected = sw.recommend(budget).tag
    args = types.SimpleNamespace(model=None, list_only=False, auto=True)
    rc = sw._run_local(_NullConsole(), args)
    assert rc == 0
    last = catalog.get_last_model()
    assert last["model"] == expected
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


def test_openrouter_free_model_restores_key_via_backend_match(monkeypatch, tmp_path) -> None:
    # An OpenRouter ":free" model id isn't in provider_for_model's prefix map, so
    # restore must fall back to matching the saved backend URL to wire the key —
    # else the model comes back with no auth and 401s.
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("MANTIS_AGENT_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Sanity: the id really does miss the heuristic.
    assert catalog.provider_for_model("meta-llama/llama-3.3-70b:free") is None
    or_url = catalog.BY_ID["openrouter"].base_url
    catalog.set_key("openrouter", "sk-or-key")
    catalog.set_last_model("meta-llama/llama-3.3-70b:free", or_url)

    from mantis_agent.tui import MantisTUI
    try:
        t = MantisTUI(model="qwen2.5-7b-instruct", backend="http://localhost:11434",
                      api_key=None, system=None, max_tokens=1, temperature=None, max_turns=1)
        t._restore_last_model()
        assert t.model == "meta-llama/llama-3.3-70b:free"
        assert t.backend.rstrip("/") == or_url.rstrip("/")
        assert t.api_key == "sk-or-key"  # wired via backend-URL match
    finally:
        catalog.clear_key("openrouter")


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


class _R:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._b = body

    def json(self) -> dict:
        return self._b


def test_ping_chat_model_uses_max_completion_tokens_for_new_openai(monkeypatch) -> None:
    # gpt-5.x / o-series reject max_tokens — the setup ping must send
    # max_completion_tokens for them (else validation 400s on a valid model).
    import httpx

    from mantis_agent import setup_wizard as sw
    captured: dict = {}

    def _post(url, headers, json, timeout):  # noqa: ANN001
        captured["payload"] = json
        return _R(200, {})

    monkeypatch.setattr(httpx, "post", _post)
    sw._ping_chat_model("https://api.openai.com/v1", "gpt-5.5", "k")
    assert "max_completion_tokens" in captured["payload"] and "max_tokens" not in captured["payload"]
    sw._ping_chat_model("https://api.openai.com/v1", "gpt-4o", "k")
    assert "max_tokens" in captured["payload"] and "max_completion_tokens" not in captured["payload"]


def test_ping_chat_model_truncation_is_pass_but_wrong_endpoint_fails(monkeypatch) -> None:
    import httpx

    from mantis_agent import setup_wizard as sw
    # Reasoning model truncated by our 1-token cap → the model+key WORK → pass.
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _R(
        400, {"error": {"message": "Could not finish the message because max_tokens was reached"}}))
    assert sw._ping_chat_model("https://api.openai.com/v1", "gpt-5.5", "k")[0] is True
    # Responses-only (codex) model → still correctly rejected.
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _R(
        400, {"error": {"message": "This model is not supported in the v1/chat/completions endpoint"}}))
    assert sw._ping_chat_model("https://api.openai.com/v1", "gpt-5.2-codex", "k")[0] is False


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


def test_pick_model_id_handles_large_lists_without_capping(monkeypatch) -> None:
    # OpenAI returns 50+ chat models with gpt-5.x at the END — a model past the
    # old 30-item cap must still be reachable (the arrow picker now scrolls).
    from mantis_agent import setup_wizard as sw
    models = [f"model-{i}" for i in range(47)] + ["gpt-5.5"] + [f"tail-{i}" for i in range(8)]
    assert len(models) > 30
    monkeypatch.setattr("builtins.input", lambda *a: "gpt-5.5")
    assert sw._pick_model_id(_NullConsole(), models) == "gpt-5.5"
    # And the cap is now generous (was 30) — a 200-long list is accepted whole.
    big = [f"m{i}" for i in range(200)]
    monkeypatch.setattr("builtins.input", lambda *a: "m150")
    assert sw._pick_model_id(_NullConsole(), big) == "m150"


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
