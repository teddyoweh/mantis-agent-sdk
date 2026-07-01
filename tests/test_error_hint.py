"""``error_hint`` turns a runtime failure into a one-line recovery hint for the
three selection failures users hit most — bad key, unavailable model, backend
down. These lock the routing so a mis-selection always points to the fix.
"""

from __future__ import annotations

from mantis_agent.errors import AuthError, RateLimitError
from mantis_agent.tui import error_hint


def test_rate_limit_suggests_wait_or_switch() -> None:
    # Free tiers (Groq/Gemini) 429 aggressively — the hint must guide recovery.
    h = error_hint(RateLimitError("Rate limit reached", status_code=429), None)
    assert "rate limited" in h.lower()
    assert "/models" in h
    assert "rate limited" in error_hint(Exception("429 Too Many Requests"), None).lower()


def test_rate_limit_surfaces_retry_after_when_known() -> None:
    h = error_hint(RateLimitError("slow down", status_code=429, retry_after_s=30), None)
    assert "30s" in h
    # No retry-after → generic wording, no bogus number.
    h2 = error_hint(RateLimitError("slow down", status_code=429), None)
    assert "retry in" not in h2 and "wait a moment" in h2


def test_bad_key_points_to_setup() -> None:
    assert "mantis setup" in error_hint(AuthError("Incorrect API key", status_code=401), None)
    assert "mantis setup" in error_hint(Exception("Incorrect API key provided"), None)


def test_unavailable_model_points_to_models_picker() -> None:
    assert "/models" in error_hint(Exception("The model `nope` does not exist"), None)
    assert "/models" in error_hint(Exception("not supported in the v1/chat/completions endpoint"), None)


def test_unavailable_model_is_local_aware() -> None:
    # On a local Ollama backend, a missing model should suggest `ollama pull`.
    local = error_hint(Exception("model `x` not found"), "http://localhost:11434")
    assert "ollama pull" in local.lower()
    remote = error_hint(Exception("model `x` not found"), "https://api.openai.com/v1")
    assert "ollama pull" not in remote.lower()


def test_connection_hint_is_context_aware() -> None:
    local = error_hint(Exception("Connection refused"), "http://localhost:11434")
    assert "ollama serve" in local.lower()
    remote = error_hint(Exception("All connection attempts failed"), "https://api.openai.com/v1")
    assert "backend url" in remote.lower() or "network" in remote.lower()
    assert "ollama serve" not in remote.lower()


def test_no_hint_for_unrecognized_error() -> None:
    assert error_hint(Exception("some internal parse glitch"), None) is None
