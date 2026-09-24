"""Shared pytest fixtures.

These fixtures stay deliberately small so each test stays readable on its
own. The heavy lifting is in three places:

* ``mock_backend`` — a :class:`MockProvider` instance preloaded with a
  scripted response. Tests get to assert on what *messages* the agent
  produced, not on HTTP wire shapes.
* ``simple_tools`` — a couple of trivial ``@tool``-decorated functions that
  exercise the common shapes (string input, int input, returning JSON).
* ``recorded_fixture`` — loader for ``tests/recorded/<name>.json`` event
  streams. We don't ship any recorded fixtures yet (that's a sibling agent's
  job); the loader skips the test gracefully if the file is missing.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_env_context(monkeypatch):
    """Disable the session-start <env>/git context head for the whole suite so
    Agents don't shell out to git and don't inject an extra isMeta message that
    would shift the yielded-message indices tests assert on. Mirrors Claude
    Code skipping context injection under NODE_ENV=test. Tests that specifically
    exercise env injection re-enable it with ``monkeypatch.delenv(...)``."""
    monkeypatch.setenv("MANTIS_AGENT_NO_CONTEXT", "1")


@pytest.fixture(autouse=True)
def _isolate_cli_logins(monkeypatch, tmp_path_factory):
    """Hide the developer's real Codex / Claude Code logins from the suite.

    ``cli_logins`` reads ``~/.codex/auth.json`` and the macOS Keychain; without
    this a machine with ``codex login`` done would enable OpenAI (and reroute
    it to the ChatGPT backend) in tests that expect no credential at all.
    Tests that exercise detection point ``CODEX_HOME`` / ``CLAUDE_CONFIG_DIR``
    at their own fixtures."""
    empty = tmp_path_factory.mktemp("no-cli-logins")
    monkeypatch.setenv("CODEX_HOME", str(empty / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty / "claude"))
    monkeypatch.delenv("MANTIS_DISABLE_CODEX_LOGIN", raising=False)
    from mantis_agent import cli_logins as _cl  # noqa: PLC0415
    monkeypatch.setattr(_cl, "_claude_keychain_entry", lambda: False)


@pytest.fixture(autouse=True)
def _clear_runtime_context_limits():
    """Forget process-local context windows a provider announced (Ollama's
    ``num_ctx``) so one test's window never shrinks another's planning."""
    from mantis_agent import context_limits as _cl  # noqa: PLC0415
    _cl._runtime.clear()
    yield
    _cl._runtime.clear()


@pytest.fixture(autouse=True)
def _isolate_deploy_registry():
    """Restore ``mantis_agent.deploy.base.DEPLOY_PROVIDERS`` after every test.

    Several suites register a throwaway adapter (``@register_provider`` with
    id ``fake``) to drive the manager / dashboard / CLI without a network.
    Without this snapshot the fake leaks into later tests that iterate the
    registry (e.g. "every deploy provider has a key guide"), making the
    outcome depend on file ordering."""
    try:
        from mantis_agent.deploy import base as _deploy_base  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — deploy package absent in some layouts
        yield
        return
    before = dict(_deploy_base.DEPLOY_PROVIDERS)
    yield
    _deploy_base.DEPLOY_PROVIDERS.clear()
    _deploy_base.DEPLOY_PROVIDERS.update(before)


# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_backend() -> Any:
    """Return a MockProvider instance if the sibling module is present.

    The mock provider is built by another agent. Tests that depend on it
    should ``pytest.importorskip("mantis_agent.providers.mock")`` at the
    top of the test or use this fixture and let the skip-on-missing kick in.
    """

    mock_module = pytest.importorskip("mantis_agent.providers.mock")
    cls = getattr(mock_module, "MockProvider", None)
    if cls is None:
        pytest.skip("MockProvider not exported from mantis_agent.providers.mock")
    return cls()


# ---------------------------------------------------------------------------
# Tool fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_tools() -> list[Any]:
    """A trio of toy tools covering string / numeric / dict-returning shapes."""

    from mantis_agent import tool

    @tool
    async def echo(text: str) -> str:
        """Return ``text`` verbatim."""

        return text

    @tool
    async def add(a: int, b: int) -> int:
        """Add two integers."""

        return a + b

    @tool
    async def make_record(name: str, age: int) -> dict[str, Any]:
        """Build a small dict — exercises JSON return value coercion."""

        return {"name": name, "age": age}

    return [echo, add, make_record]


# ---------------------------------------------------------------------------
# Recorded fixtures
# ---------------------------------------------------------------------------


_RECORDED_DIR = pathlib.Path(__file__).parent / "recorded"


@pytest.fixture
def recorded_fixture():
    """Return a loader callable: ``recorded_fixture("name") -> list[dict]``.

    Loads ``tests/recorded/<name>.json`` and parses it as a list of dicts that
    a mock provider can replay as a stream. Skips the test gracefully if the
    file is missing — recorded fixtures are owned by a sibling agent.
    """

    def _load(name: str) -> list[dict[str, Any]]:
        path = _RECORDED_DIR / f"{name}.json"
        if not path.exists():
            pytest.skip(f"recorded fixture missing: tests/recorded/{name}.json")
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            pytest.skip(f"recorded fixture {name} is not a list of events")
        return data

    return _load
