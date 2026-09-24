"""Ollama's context window (``options.num_ctx``) and residency (``keep_alive``).

Without ``num_ctx`` Ollama runs its 2k-4k default and silently truncates the
prompt front — system prompt and tools first — while the engine compacts
against the capability window. These pin what goes on the wire.
"""

from __future__ import annotations

import json

import anyio
import httpx
import pytest

from mantis_agent import context_limits as cl
from mantis_agent.capabilities import lookup_model
from mantis_agent.providers.ollama import DEFAULT_MAX_NUM_CTX, OllamaProvider
from mantis_agent.types import UserMessage

_DONE = (
    '{"message":{"role":"assistant","content":"ok"},"done":false}\n'
    '{"message":{"role":"assistant","content":""},"done":true,'
    '"prompt_eval_count":1,"eval_count":1}\n'
)

_ENV = (
    "MANTIS_OLLAMA_NUM_CTX",
    "MANTIS_OLLAMA_MAX_NUM_CTX",
    "MANTIS_OLLAMA_KEEP_ALIVE",
    "OLLAMA_CONTEXT_LENGTH",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    cl._reset_cache_for_tests()
    yield
    cl._reset_cache_for_tests()


def _provider(show: dict | None = None, *, status: int | list[int] = 200,
              ps: dict | None = None, base: str = "http://gpu:11434", **kw):
    """Provider on a mock transport. ``show`` = the /api/show body; ``None``
    disables the probes. ``status`` may be a list consumed per /api/show call.
    ``ps`` = the /api/ps body (default: nothing loaded).
    Returns (provider, chat bodies, show request count)."""

    chats: list[dict] = []
    shows: list[int] = []
    statuses = list(status) if isinstance(status, list) else None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            shows.append(1)
            code = statuses.pop(0) if statuses else (
                status if isinstance(status, int) else 200)
            return httpx.Response(code, json=show or {})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json=ps or {"models": []})
        chats.append(json.loads(request.content))
        return httpx.Response(200, text=_DONE,
                              headers={"content-type": "application/x-ndjson"})

    p = OllamaProvider(base_url=base, probe_model_info=show is not None, **kw)
    p.client = httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler))
    return p, chats, shows


def _run(p: OllamaProvider, model: str, times: int = 1) -> None:
    async def go() -> None:
        for _ in range(times):
            async for _ev in p.stream(model=model, max_tokens=64,
                                      messages=[UserMessage(content="hi")],
                                      model_capability=lookup_model(model)):
                pass

    anyio.run(go)


def test_defaults_send_capped_num_ctx_and_keep_alive() -> None:
    model = "llama3.1:8b"
    p, chats, _ = _provider()
    _run(p, model)
    assert lookup_model(model).context_window > DEFAULT_MAX_NUM_CTX, "fixture assumption"
    assert chats[0]["options"]["num_ctx"] == DEFAULT_MAX_NUM_CTX
    assert chats[0]["options"]["num_predict"] == 64
    assert chats[0]["keep_alive"] == "30m"


def test_small_window_is_not_inflated_to_the_cap() -> None:
    model = "llama3:8b"
    declared = lookup_model(model).context_window
    assert declared < DEFAULT_MAX_NUM_CTX, "fixture assumption"
    p, chats, _ = _provider()
    _run(p, model)
    assert chats[0]["options"]["num_ctx"] == declared


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_OLLAMA_NUM_CTX", "12288")
    monkeypatch.setenv("MANTIS_OLLAMA_KEEP_ALIVE", "-1")
    p, chats, _ = _provider()
    _run(p, "llama3.1:8b")
    assert chats[0]["options"]["num_ctx"] == 12288
    # A bare number must go out as an int: Ollama reads strings as Go
    # durations and rejects "-1" for having no unit.
    assert chats[0]["keep_alive"] == -1


def test_max_cap_env_raises_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    model = "llama3.1:8b"
    declared = lookup_model(model).context_window
    monkeypatch.setenv("MANTIS_OLLAMA_MAX_NUM_CTX", str(declared * 4))
    p, chats, _ = _provider()
    _run(p, model)
    assert chats[0]["options"]["num_ctx"] == declared


def test_constructor_args_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_OLLAMA_NUM_CTX", "12288")
    monkeypatch.setenv("MANTIS_OLLAMA_KEEP_ALIVE", "5m")
    p, chats, _ = _provider(num_ctx=4096, keep_alive=0)
    _run(p, "llama3.1:8b")
    assert chats[0]["options"]["num_ctx"] == 4096
    assert chats[0]["keep_alive"] == 0


def test_zero_and_off_omit_the_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_OLLAMA_NUM_CTX", "0")
    monkeypatch.setenv("MANTIS_OLLAMA_KEEP_ALIVE", "off")
    p, chats, _ = _provider()
    _run(p, "llama3.1:8b")
    assert "num_ctx" not in chats[0]["options"]
    assert "keep_alive" not in chats[0]


def test_server_side_context_length_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "16384")
    p, chats, _ = _provider()
    _run(p, "llama3.1:8b")
    assert "num_ctx" not in chats[0]["options"]
    # ... but the engine still plans against what the daemon runs.
    assert cl.effective_window("llama3.1:8b", 131072, "http://gpu:11434") == 16384


def test_num_ctx_is_stable_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    p, chats, _ = _provider()
    _run(p, "llama3.1:8b")
    # Changing the env mid-session must not change the window: a different
    # num_ctx makes Ollama reload the model.
    monkeypatch.setenv("MANTIS_OLLAMA_NUM_CTX", "4096")
    _run(p, "llama3.1:8b", times=2)
    assert len({c["options"]["num_ctx"] for c in chats}) == 1


def test_api_show_clamps_to_trained_length_and_is_cached() -> None:
    show = {"model_info": {"general.architecture": "llama",
                           "llama.context_length": 8192}}
    p, chats, shows = _provider(show)
    _run(p, "llama3.1:8b", times=3)
    assert all(c["options"]["num_ctx"] == 8192 for c in chats)
    assert len(shows) == 1
    assert p.model_context_length("llama3.1:8b") == 8192
    assert p.num_ctx_for("llama3.1:8b") == 8192


def test_api_show_failure_is_tolerated() -> None:
    p, chats, shows = _provider({"error": "nope"}, status=404)
    _run(p, "llama3.1:8b", times=3)
    # A failed probe is retried once, then the guess is pinned.
    assert len(shows) == 2
    assert len({c["options"]["num_ctx"] for c in chats}) == 1
    assert chats[0]["options"]["num_ctx"] == min(
        lookup_model("llama3.1:8b").context_window, DEFAULT_MAX_NUM_CTX)
    assert p.model_context_length("llama3.1:8b") is None


def test_engine_plans_against_the_sent_window() -> None:
    p, chats, _ = _provider()
    _run(p, "llama3.1:8b")
    sent = chats[0]["options"]["num_ctx"]
    # Host-scoped and bare (the TUI leaves Agent.backend unset).
    assert cl.effective_window("llama3.1:8b", 131072, "http://gpu:11434") == sent
    assert cl.effective_window("llama3.1:8b", 131072) == sent
    # Process-local only: nothing written for later sessions.
    assert cl.learned_limit("llama3.1:8b") is None


def test_extra_options_still_win() -> None:
    p, chats, _ = _provider()

    async def go() -> None:
        async for _ev in p.stream(model="llama3.1:8b", max_tokens=8,
                                  messages=[UserMessage(content="hi")],
                                  extra={"options": {"num_ctx": 2048},
                                         "keep_alive": "1h"}):
            pass

    anyio.run(go)
    assert chats[0]["options"]["num_ctx"] == 2048
    assert chats[0]["keep_alive"] == "1h"


def test_probe_off_under_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_AGENT_MOCK", "1")
    assert OllamaProvider(base_url="http://gpu:11434")._probe_model_info is False


# --- review fixes ------------------------------------------------------------


def test_window_is_planned_before_the_first_request() -> None:
    """The agent sizes compaction / max_tokens before stream() runs."""
    from mantis_agent import Agent

    p, chats, _ = _provider()
    agent = Agent(model="llama3.1:8b", provider=p)
    assert not chats
    assert agent._effective_context_window() == DEFAULT_MAX_NUM_CTX
    _run(p, "llama3.1:8b")
    assert chats[0]["options"]["num_ctx"] == DEFAULT_MAX_NUM_CTX


def test_probe_refines_the_early_plan() -> None:
    show = {"model_info": {"general.architecture": "llama",
                           "llama.context_length": 8192}}
    p, _, _ = _provider(show)
    assert p.planned_context_window("llama3.1:8b") == DEFAULT_MAX_NUM_CTX
    _run(p, "llama3.1:8b")
    assert cl.effective_window("llama3.1:8b", 131072) == 8192
    # Once resolved, the early hook must not re-announce the stale guess.
    assert p.planned_context_window("llama3.1:8b") == 8192
    assert cl.effective_window("llama3.1:8b", 131072) == 8192


@pytest.mark.parametrize("model", ["gpt-oss:120b-cloud", "qwen3-coder:480b-cloud",
                                   "deepseek-v3.1:cloud"])
def test_cloud_models_send_no_num_ctx_and_have_no_vram_ceiling(model: str) -> None:
    p, chats, _ = _provider()
    declared = lookup_model(model).context_window
    assert p.planned_context_window(model) == declared
    _run(p, model)
    assert "num_ctx" not in chats[0]["options"]
    assert chats[0]["keep_alive"] == "30m"
    assert cl.effective_window(model, declared) == declared
    if declared > DEFAULT_MAX_NUM_CTX:
        assert cl.effective_window(model, declared) > DEFAULT_MAX_NUM_CTX


def test_cloud_plans_against_the_probed_window() -> None:
    show = {"model_info": {"general.architecture": "gptoss",
                           "gptoss.context_length": 131072}}
    p, chats, _ = _provider(show)
    _run(p, "gpt-oss:120b-cloud")
    assert "num_ctx" not in chats[0]["options"]
    assert cl.effective_window("gpt-oss:120b-cloud", 10**6) == 131072


def test_ollama_com_host_is_cloud() -> None:
    p, chats, _ = _provider(base="https://ollama.com")
    _run(p, "gpt-oss:120b")
    assert "num_ctx" not in chats[0]["options"]


def test_loaded_model_with_a_larger_server_window_is_not_overridden() -> None:
    """OLLAMA_CONTEXT_LENGTH is usually set on the daemon, not our shell;
    /api/ps shows what the server actually loaded."""
    show = {"model_info": {"general.architecture": "llama",
                           "llama.context_length": 131072}}
    ps = {"models": [{"name": "llama3.1:8b", "model": "llama3.1:8b",
                      "context_length": 65536}]}
    p, chats, _ = _provider(show, ps=ps)
    _run(p, "llama3.1:8b")
    assert chats[0]["options"]["num_ctx"] == 65536
    assert cl.effective_window("llama3.1:8b", 131072) == 65536


def test_loaded_model_with_a_small_default_window_is_raised() -> None:
    show = {"model_info": {"general.architecture": "llama",
                           "llama.context_length": 131072}}
    ps = {"models": [{"name": "llama3.1:8b", "context_length": 4096}]}
    p, chats, _ = _provider(show, ps=ps)
    _run(p, "llama3.1:8b")
    # A 4k load is Ollama's default, not a choice — truncation is the bug.
    assert chats[0]["options"]["num_ctx"] == DEFAULT_MAX_NUM_CTX


def test_num_ctx_zero_respects_server_and_plans_against_loaded(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_OLLAMA_NUM_CTX", "0")
    show = {"model_info": {"general.architecture": "llama",
                           "llama.context_length": 131072}}
    ps = {"models": [{"name": "llama3", "context_length": 16384}]}
    p, chats, _ = _provider(show, ps=ps)
    _run(p, "llama3:latest")
    assert "num_ctx" not in chats[0]["options"]
    assert cl.effective_window("llama3:latest", 131072) == 16384


def test_unknown_model_uses_probed_trained_length() -> None:
    model = "mystery-coder:7b"
    assert lookup_model(model).family == "unknown", "fixture assumption"
    show = {"model_info": {"general.architecture": "mystery",
                           "mystery.context_length": 131072}}
    p, chats, _ = _provider(show)
    _run(p, model)
    assert chats[0]["options"]["num_ctx"] == DEFAULT_MAX_NUM_CTX

    small = {"model_info": {"general.architecture": "mystery",
                            "mystery.context_length": 16384}}
    p, chats, _ = _provider(small)
    _run(p, "mystery-coder:3b")
    assert chats[0]["options"]["num_ctx"] == 16384


def test_unknown_model_without_probe_keeps_the_table_default() -> None:
    model = "mystery-coder:7b"
    p, chats, _ = _provider()
    _run(p, model)
    assert chats[0]["options"]["num_ctx"] == lookup_model(model).context_window


def test_context_length_key_follows_general_architecture() -> None:
    # The vision tower's key comes first; it must not win.
    show = {"model_info": {"clip.context_length": 2048,
                           "general.architecture": "llama",
                           "llama.context_length": 16384}}
    p, chats, _ = _provider(show)
    _run(p, "llama3.1:8b")
    assert p.model_context_length("llama3.1:8b") == 16384
    assert chats[0]["options"]["num_ctx"] == 16384


def test_context_length_suffix_scan_is_the_fallback() -> None:
    show = {"model_info": {"weird.context_length": 16384}}
    p, _, _ = _provider(show)
    _run(p, "llama3.1:8b")
    assert p.model_context_length("llama3.1:8b") == 16384


def test_failed_probe_is_retried_and_can_still_clamp() -> None:
    show = {"model_info": {"general.architecture": "llama",
                           "llama.context_length": 8192}}
    p, chats, shows = _provider(show, status=[503, 200])
    _run(p, "llama3.1:8b", times=3)
    assert len(shows) == 2
    assert chats[0]["options"]["num_ctx"] == DEFAULT_MAX_NUM_CTX
    assert [c["options"]["num_ctx"] for c in chats[1:]] == [8192, 8192]
    assert p.model_context_length("llama3.1:8b") == 8192


def test_forget_clears_the_runtime_window() -> None:
    cl.note_runtime_limit("m", 8192, "http://gpu:11434")
    cl.forget("m", "http://gpu:11434")
    assert cl.runtime_limit("m") is None
    assert cl.runtime_limit("m", "http://gpu:11434") is None
