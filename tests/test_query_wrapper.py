"""Tests for the ``query()`` drop-in wrapper.

These tests use the sibling-built :class:`MockProvider`. The whole file is
skipped if the mock module hasn't landed yet — the rest of the test suite
stays green even while M0/M1 are in progress.
"""

from __future__ import annotations

import pytest

# The mock provider may not be ready in every checkout. importorskip means
# the whole file no-ops cleanly when it isn't.
mock_module = pytest.importorskip("mantis_agent.providers.mock")

from mantis_agent import tool  # noqa: E402
from mantis_agent.providers import base as provider_base  # noqa: E402
from mantis_agent.query import (  # noqa: E402
    SDKAssistantMessage,
    SDKResultMessage,
    SDKSystemMessage,
    SDKUserMessage,
    _to_snake,
    query,
)


# ---------------------------------------------------------------------------
# _to_snake — option normalization
# ---------------------------------------------------------------------------


class TestToSnake:
    @pytest.mark.parametrize(
        "given,want",
        [
            ("maxTurns", "max_turns"),
            ("max_turns", "max_turns"),
            ("apiKey", "api_key"),
            ("permissionMode", "permission_mode"),
            ("MCPServers", "m_c_p_servers"),  # all-caps is unusual but consistent
            ("model", "model"),
        ],
    )
    def test_cases(self, given: str, want: str) -> None:
        assert _to_snake(given) == want


# ---------------------------------------------------------------------------
# Wrapper round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_mock_as_default(monkeypatch: pytest.MonkeyPatch):
    """Force ``detect_provider`` to return ``"mock"`` so the wrapper picks the
    mock provider regardless of the model/backend strings we use.

    This lets us assert on the wrapper's behavior without standing up a real
    server. Restored automatically by ``monkeypatch``.
    """

    original = provider_base.detect_provider

    def _detect(model_or_url: str, *, backend_hint: str | None = None) -> str:
        return "mock"

    monkeypatch.setattr(provider_base, "detect_provider", _detect)
    yield
    monkeypatch.setattr(provider_base, "detect_provider", original)


@pytest.mark.anyio
async def test_query_emits_system_user_assistant_result(
    _patch_mock_as_default,
) -> None:
    """The wrapper should emit, in order: system, user, assistant, result."""

    seen: list[type] = []
    async for msg in query(
        prompt="hello",
        options={"model": "mock-model", "system": "be brief", "max_turns": 1},
    ):
        seen.append(type(msg))

    # System and user are always emitted up front.
    assert seen[0] is SDKSystemMessage
    assert seen[1] is SDKUserMessage
    # An assistant turn might be present depending on the mock's scripted
    # behavior, but the final message is always a result.
    assert seen[-1] is SDKResultMessage
    if SDKAssistantMessage in seen:
        # If the mock produced an assistant turn, it should be between user
        # and result — never before user or after result.
        idx = seen.index(SDKAssistantMessage)
        assert idx > seen.index(SDKUserMessage)
        assert idx < seen.index(SDKResultMessage)


@pytest.mark.anyio
async def test_query_accepts_camelcase_options(_patch_mock_as_default) -> None:
    # The wrapper should treat ``maxTurns`` and ``max_turns`` interchangeably.
    saw_system = False
    async for msg in query(
        prompt="hi",
        options={"model": "mock-model", "maxTurns": 1, "system": "be brief"},
    ):
        if isinstance(msg, SDKSystemMessage):
            saw_system = True
    assert saw_system


@pytest.mark.anyio
async def test_query_with_tool_option(_patch_mock_as_default) -> None:
    @tool
    async def echo(text: str) -> str:
        """Echo."""

        return text

    # We don't assert the tool actually fires (depends on mock behavior); we
    # just confirm passing a tools list doesn't blow up wrapper plumbing.
    seen_result = False
    async for msg in query(
        prompt="say hi",
        options={"model": "mock-model", "tools": [echo], "max_turns": 1},
    ):
        if isinstance(msg, SDKResultMessage):
            seen_result = True
    assert seen_result


@pytest.mark.anyio
async def test_query_requires_model() -> None:
    # No ``model`` key → wrapper raises before yielding anything.
    with pytest.raises(ValueError, match="model"):
        async for _ in query(prompt="hi", options={"backend": "http://x"}):
            pass


@pytest.fixture
def anyio_backend() -> str:
    """Default to asyncio for pytest-anyio."""

    return "asyncio"
