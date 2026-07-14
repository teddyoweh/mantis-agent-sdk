"""Tests for the ``vllm_self_hosted`` example.

The example has two run modes:

  * **Real vLLM** — requires a reachable OpenAI-compat server (default
    ``http://localhost:8000/v1``, override with ``VLLM_BASE_URL``).
  * **Offline mock** — guarded by ``MANTIS_AGENT_MOCK=1``; uses a scripted
    :class:`MockProvider` to exercise the tool-call → tool-result →
    final-answer path with zero network.

These tests cover the mock path (which is also what CI / contributors run)
and the live-mode preflight (so a missing server fails fast with an
actionable hint instead of an httpx connection-refused stack trace).

We intentionally do **not** hit a real vLLM endpoint here — that's what
the example's live mode is for. Tests must stay hermetic; we patch
``_vllm_is_reachable`` to control the preflight outcome.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import anyio
import pytest

from mantis_agent.examples import vllm_self_hosted as example


# ---------------------------------------------------------------------------
# Tool unit-tests
# ---------------------------------------------------------------------------


def test_search_docs_matches_retries():
    """The canned dict matches on substring → the tool returns the
    expected retries snippet."""

    async def go() -> str:
        return await example.search_docs.fn(query_str="retries")

    out = anyio.run(go)
    assert "exponential backoff" in out
    assert "30s" in out


def test_search_docs_case_insensitive():
    """Tool matches must ignore case/whitespace — model quirks
    (\"Rate Limits\" vs \"rate limits\") shouldn't break the demo."""

    async def go() -> str:
        return await example.search_docs.fn(query_str="  RATE LIMITS  ")

    out = anyio.run(go)
    assert "Rate limits" in out
    assert "60 req/min" in out


def test_search_docs_unknown_returns_no_match_line():
    """Unknown queries still return a one-line string (no exception)."""

    async def go() -> str:
        return await example.search_docs.fn(query_str="nonsense-topic")

    out = anyio.run(go)
    assert out.startswith("No matching docs")
    assert "nonsense-topic" in out


def test_search_docs_empty_query_safe():
    """Empty queries must not crash — `.strip().lower()` on None or ''
    needs to be defensive enough to fall through to the no-match line."""

    async def go() -> str:
        return await example.search_docs.fn(query_str="")

    out = anyio.run(go)
    assert "No matching docs" in out


# ---------------------------------------------------------------------------
# Reachability preflight
# ---------------------------------------------------------------------------


def test_reachability_false_when_unreachable(monkeypatch):
    """An unreachable port must return False, not raise."""

    def boom(*_a, **_kw):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(example.urllib.request, "urlopen", boom)
    assert example._vllm_is_reachable("http://127.0.0.1:1") is False


def test_reachability_true_for_2xx(monkeypatch):
    """Any 2xx → True. We don't parse the body — just the status."""

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return None
    monkeypatch.setattr(example.urllib.request, "urlopen", lambda *_a, **_kw: _Resp())
    assert example._vllm_is_reachable("http://localhost:8000/v1") is True


def test_reachability_false_for_500(monkeypatch):
    """A 5xx server response shouldn't qualify as reachable — even if
    the socket opens, the chat endpoint is unlikely to succeed."""

    class _Resp:
        status = 503
        def __enter__(self): return self
        def __exit__(self, *_): return None
    monkeypatch.setattr(example.urllib.request, "urlopen", lambda *_a, **_kw: _Resp())
    assert example._vllm_is_reachable("http://localhost:8000/v1") is False


def test_reachability_url_appends_models_segment(monkeypatch):
    """The preflight must hit ``{base}/models`` regardless of whether
    the user passed a trailing slash. Otherwise some setups GET
    ``/v1//models`` and 404 spuriously."""

    seen = []
    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return None
    def fake_urlopen(url, *_a, **_kw):
        seen.append(url)
        return _Resp()
    monkeypatch.setattr(example.urllib.request, "urlopen", fake_urlopen)
    example._vllm_is_reachable("http://localhost:8000/v1/")
    example._vllm_is_reachable("http://localhost:8000/v1")
    assert seen == [
        "http://localhost:8000/v1/models",
        "http://localhost:8000/v1/models",
    ]


# ---------------------------------------------------------------------------
# Mock provider — script structure is the contract with Agent loop
# ---------------------------------------------------------------------------


def test_mock_provider_yields_two_turns_in_order():
    """First stream() must emit a tool_use → stop; second must emit a
    text answer → stop. Flipping order deadlocks the demo."""

    from mantis_agent.events import (
        ContentBlockStart,
        MessageStart,
        MessageStop,
    )
    from mantis_agent.types import TextBlock, ToolUseBlock

    provider = example._build_mock_provider()

    async def collect_turn() -> list:
        out = []
        async for ev in provider.stream():
            out.append(ev)
        return out

    turn_1 = anyio.run(collect_turn)
    turn_2 = anyio.run(collect_turn)

    assert isinstance(turn_1[0], MessageStart)
    starts_1 = [e for e in turn_1 if isinstance(e, ContentBlockStart)]
    assert len(starts_1) == 1
    assert isinstance(starts_1[0].block, ToolUseBlock)
    assert starts_1[0].block.name == "search_docs"
    assert isinstance(turn_1[-1], MessageStop)

    assert isinstance(turn_2[0], MessageStart)
    starts_2 = [e for e in turn_2 if isinstance(e, ContentBlockStart)]
    assert len(starts_2) == 1
    assert isinstance(starts_2[0].block, TextBlock)
    assert isinstance(turn_2[-1], MessageStop)


def test_mock_provider_replays_last_script_on_overflow():
    """Defensive: a stray extra ``stream()`` call must not IndexError —
    it should replay the last script."""

    provider = example._build_mock_provider()

    async def consume(n: int) -> list[list]:
        rounds: list[list] = []
        for _ in range(n):
            evs = []
            async for ev in provider.stream():
                evs.append(ev)
            rounds.append(evs)
        return rounds

    rounds = anyio.run(consume, 4)
    assert len(rounds) == 4
    assert [type(e).__name__ for e in rounds[2]] == [
        type(e).__name__ for e in rounds[1]
    ]
    assert [type(e).__name__ for e in rounds[3]] == [
        type(e).__name__ for e in rounds[1]
    ]


# ---------------------------------------------------------------------------
# main() — end-to-end through query() + Agent loop in mock mode
# ---------------------------------------------------------------------------


def test_main_mock_mode_exercises_tool_path(capsys, monkeypatch, tmp_path):
    """main() in mock mode must dispatch ``search_docs``, thread the
    result back, and print the success marker."""

    monkeypatch.setenv("MANTIS_AGENT_MOCK", "1")
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "mantis_agent_home"))
    # Mock mode must skip the preflight entirely — assert that by making
    # _vllm_is_reachable explode if called.
    def boom(*_a, **_kw):
        raise AssertionError("_vllm_is_reachable must not run in mock mode")
    monkeypatch.setattr(example, "_vllm_is_reachable", boom)

    anyio.run(example.main)

    out = capsys.readouterr().out
    assert "[assistant → search_docs] dispatching" in out
    assert "backoff" in out  # the canned doc result
    assert "[ok] mock-mode smoke test passed." in out


def test_main_live_mode_unreachable_server_raises_systemexit(monkeypatch, tmp_path):
    """Live mode must fail fast with an actionable hint when the server
    isn't up — not silently call httpx and connection-refused."""

    monkeypatch.delenv("MANTIS_AGENT_MOCK", raising=False)
    monkeypatch.delenv("MANTIS_AGENT_NO_PREFLIGHT", raising=False)
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "mantis_agent_home"))
    monkeypatch.setattr(example, "_vllm_is_reachable", lambda *_a, **_kw: False)

    with pytest.raises(SystemExit) as excinfo:
        anyio.run(example.main)

    msg = str(excinfo.value)
    assert "vLLM not reachable" in msg
    assert "MANTIS_AGENT_MOCK=1" in msg
    # And the hint must include the exact start command so a user can
    # copy-paste their way out.
    assert "vllm.entrypoints.openai.api_server" in msg


def test_main_live_mode_preflight_can_be_disabled(monkeypatch, tmp_path):
    """``MANTIS_AGENT_NO_PREFLIGHT=1`` should skip the reachability check
    even when the server is down — for users whose preflight URL is
    blocked but who know the chat endpoint works."""

    monkeypatch.delenv("MANTIS_AGENT_MOCK", raising=False)
    monkeypatch.setenv("MANTIS_AGENT_NO_PREFLIGHT", "1")
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "mantis_agent_home"))
    def boom(*_a, **_kw):
        raise AssertionError("preflight must be skipped when NO_PREFLIGHT=1")
    monkeypatch.setattr(example, "_vllm_is_reachable", boom)
    # Stub query() so we don't actually try to talk to vLLM — that's
    # not what this test is verifying. We just want to confirm we got
    # past the preflight guard.

    async def fake_query(*, prompt, options):
        if False:
            yield None  # make it an async iterator
        return
    monkeypatch.setattr(example, "query", fake_query)

    # Should not raise SystemExit — the guard was bypassed.
    anyio.run(example.main)


def test_main_live_mode_uses_env_overrides(monkeypatch, tmp_path):
    """``VLLM_BASE_URL`` and ``VLLM_MODEL`` env vars must flow into the
    query options. A regression here would silently send requests to
    the default URL even when the user pointed elsewhere."""

    monkeypatch.delenv("MANTIS_AGENT_MOCK", raising=False)
    monkeypatch.delenv("MANTIS_AGENT_NO_PREFLIGHT", raising=False)
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "mantis_agent_home"))
    monkeypatch.setenv("VLLM_BASE_URL", "https://gpu.example/v1")
    monkeypatch.setenv("VLLM_MODEL", "Qwen/Qwen2.5-72B-Instruct")

    seen = {}
    def fake_reachable(base_url, *, timeout=2.0):
        seen["base_url"] = base_url
        return True
    monkeypatch.setattr(example, "_vllm_is_reachable", fake_reachable)

    captured = {}
    async def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        if False:
            yield None
        return
    monkeypatch.setattr(example, "query", fake_query)

    anyio.run(example.main)

    assert seen["base_url"] == "https://gpu.example/v1"
    assert captured["options"]["backend"] == "https://gpu.example/v1"
    assert captured["options"]["model"] == "Qwen/Qwen2.5-72B-Instruct"


# ---------------------------------------------------------------------------
# Module exports / shape — keep the example self-contained
# ---------------------------------------------------------------------------


def test_example_module_exposes_expected_top_level_names():
    """``main``, ``search_docs``, ``_build_mock_provider``,
    ``_vllm_is_reachable`` must remain top-level — they're the seams
    tests and downstream code can rely on."""

    importlib.reload(example)
    for name in ("main", "search_docs", "_build_mock_provider", "_vllm_is_reachable"):
        assert hasattr(example, name), f"missing top-level name: {name}"


# ---------------------------------------------------------------------------
# Subprocess smoke
# ---------------------------------------------------------------------------


def test_example_runs_in_mock_mode_via_subprocess(tmp_path):
    """Run ``python -m mantis_agent.examples.vllm_self_hosted`` in mock
    mode as a subprocess. Catches regressions where in-process tests
    pass but the example's ``if __name__ == '__main__'`` path breaks."""

    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["MANTIS_AGENT_MOCK"] = "1"
    env["MANTIS_AGENT_HOME"] = str(tmp_path / "mantis_agent_home")
    Path(env["MANTIS_AGENT_HOME"]).mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mantis_agent.examples.vllm_self_hosted",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        print("---- stdout ----")
        print(result.stdout)
        print("---- stderr ----")
        print(result.stderr)

    assert result.returncode == 0, "example exited non-zero in mock mode"
    assert "mock-mode smoke test passed" in result.stdout
    assert "backoff" in result.stdout, (
        "tool result didn't flow through to the parent's final text"
    )


def test_example_subprocess_unreachable_server_exits_nonzero(tmp_path, monkeypatch):
    """Live mode with no server up must exit non-zero with hint —
    protects users from a confusing httpx stack trace."""

    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.pop("MANTIS_AGENT_MOCK", None)
    env.pop("MANTIS_AGENT_NO_PREFLIGHT", None)
    # Point at a port nothing will ever listen on so the preflight is
    # guaranteed to fail in <2s on every CI box.
    env["VLLM_BASE_URL"] = "http://127.0.0.1:1/v1"
    env["MANTIS_AGENT_HOME"] = str(tmp_path / "mantis_agent_home")
    Path(env["MANTIS_AGENT_HOME"]).mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mantis_agent.examples.vllm_self_hosted",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0, (
        "example exited 0 with unreachable server — should have raised SystemExit"
    )
    combined = result.stdout + result.stderr
    assert "vLLM not reachable" in combined
    assert "MANTIS_AGENT_MOCK=1" in combined
