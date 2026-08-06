"""A too-large context guess must not be able to wedge a session.

Reported against Cerebras `zai-glm-4.7`:

    error: Please reduce the length of the messages or completion.
    Current length is 14789 while limit is 8192
      -> the conversation is too long ... automatic compaction could not recover

Our table claims 128000 tokens for that model, and Cerebras' own public catalog
advertises 131072 — but the free tier enforces 8192. The compactor only fires
at a fraction of the window it is TOLD about, so at 128k it never fired, the
prompt was refused, and the emergency retry re-compacted against the same wrong
budget. Every later message then overflowed too, including a manual /compact.

The fix is not a table entry for one model — it is to believe the endpoint.
"""

from __future__ import annotations

import pytest

from mantis_agent import context_limits as cl

CEREBRAS = "https://api.cerebras.ai/v1"
REAL_ERROR = ("Please reduce the length of the messages or completion. "
              "Current length is 14789 while limit is 8192")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    cl._reset_cache_for_tests()
    yield
    cl._reset_cache_for_tests()


def test_the_reported_error_yields_the_enforced_ceiling() -> None:
    assert cl.parse_limit(REAL_ERROR) == 8192


def test_it_never_mistakes_the_current_length_for_the_limit() -> None:
    # Both numbers sit in one sentence; picking 14789 would RAISE the ceiling
    # and guarantee the next turn fails the same way.
    assert cl.parse_limit(REAL_ERROR) != 14789


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("This model's maximum context length is 8192 tokens. However, your "
         "messages resulted in 14789 tokens", 8192),
        ("prompt is too long: 210000 tokens > 200000 maximum", 200000),
        ("Input validation error: `inputs` tokens + `max_new_tokens` must be <= 8192", 8192),
        ("input tokens exceed the configured limit of 32768", 32768),
        ("rate limited — retry in 30s", None),
        ("Anthropic API error (429): Error", None),
        ("", None),
    ],
)
def test_parses_every_provider_phrasing_and_nothing_else(text: str, want: int | None) -> None:
    assert cl.parse_limit(text) == want


def test_implausibly_small_numbers_are_rejected() -> None:
    # A max_tokens or a rate-limit count must never become a context window:
    # clamping to it would fail every request instead of fixing anything.
    assert cl.parse_limit("while limit is 512") is None
    assert cl.record_limit("m", 512, CEREBRAS) is False


def test_a_learned_limit_lowers_the_planning_window() -> None:
    assert cl.effective_window("zai-glm-4.7", 128000, CEREBRAS) == 128000
    cl.record_limit("zai-glm-4.7", 8192, CEREBRAS)
    assert cl.effective_window("zai-glm-4.7", 128000, CEREBRAS) == 8192


def test_it_never_raises_a_window_above_what_we_declared() -> None:
    cl.record_limit("m", 200000, CEREBRAS)
    assert cl.effective_window("m", 8192, CEREBRAS) == 8192


def test_the_smallest_observed_ceiling_wins() -> None:
    cl.record_limit("m", 8192, CEREBRAS)
    assert cl.record_limit("m", 65536, CEREBRAS) is False
    assert cl.learned_limit("m", CEREBRAS) == 8192


def test_limits_are_scoped_per_endpoint_not_per_model_id() -> None:
    # The same model id is served at different tiers by different providers;
    # an 8k cap on one says nothing about another.
    cl.record_limit("zai-glm-4.7", 8192, CEREBRAS)
    assert cl.learned_limit("zai-glm-4.7", "https://api.z.ai/api/paas/v4") is None
    assert cl.effective_window("zai-glm-4.7", 128000,
                               "https://api.z.ai/api/paas/v4") == 128000


def test_a_learned_limit_survives_the_session(monkeypatch, tmp_path) -> None:
    # The point is that the NEXT session plans correctly from the first turn,
    # rather than re-learning by failing again.
    cl.record_limit("zai-glm-4.7", 8192, CEREBRAS)
    cl._reset_cache_for_tests()
    assert cl.learned_limit("zai-glm-4.7", CEREBRAS) == 8192


def test_the_store_is_not_world_readable(tmp_path) -> None:
    import os
    import stat

    cl.record_limit("m", 8192, CEREBRAS)
    assert stat.S_IMODE(os.stat(cl._path()).st_mode) == 0o600
