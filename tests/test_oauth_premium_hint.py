"""A 429 on a Claude subscription token carries no detail worth showing.

Anthropic answers with a bare `{"error":{"type":"rate_limit_error","message":
"Error"}}` — no retry-after, no message. A generic "wait a moment and retry"
against an unstated duration is not useful, so the hint names the credential
and the ways out instead.

This originally read as an *entitlement* boundary: opus/sonnet/fable 429'd while
haiku returned 200, so the hint told people to switch to Haiku. That diagnosis
was wrong. The premium models were rejecting mantis's request shape, not the
token — a subscription token must lead with the Claude Code identity system
block (`CLAUDE_CODE_IDENTITY`, covered in test_anthropic_passthrough.py) and
mantis wasn't sending one. With that fixed every model answers, so a 429 that
still arrives here is a genuinely spent usage window.
"""

from __future__ import annotations

import pytest

from mantis_agent.errors import RateLimitError
from mantis_agent.tui import error_hint

ANTHROPIC = "https://api.anthropic.com/v1"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # setenv first: delenv on an absent var records nothing to restore, so a
    # value leaking in from the real environment would survive the test.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")


def _err() -> RateLimitError:
    return RateLimitError("Anthropic API error (429): Error")


def test_oauth_only_429_names_the_real_fix(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-" + "A" * 40)
    hint = error_hint(_err(), ANTHROPIC)
    assert hint is not None
    assert "usage limit" in hint
    # Must not send people back to Haiku as though the model were unavailable —
    # that was the old misdiagnosis, and the premium models work now.
    assert "isn't available" not in hint
    assert "wait a moment" not in hint


def test_api_key_429_is_a_real_quota_limit(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "A" * 40)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-" + "A" * 40)
    hint = error_hint(_err(), ANTHROPIC)
    assert hint is not None
    assert "retry" in hint
    assert "subscription token" not in hint


def test_other_providers_are_untouched(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-" + "A" * 40)
    hint = error_hint(_err(), "https://api.openai.com/v1")
    assert hint is not None
    assert "subscription token" not in hint


def test_no_credential_at_all_falls_back(monkeypatch) -> None:
    hint = error_hint(_err(), ANTHROPIC)
    assert hint is not None
    assert "subscription token" not in hint
