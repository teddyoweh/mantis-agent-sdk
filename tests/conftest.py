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
