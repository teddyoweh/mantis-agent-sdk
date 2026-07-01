"""``error_hint`` turns a runtime failure into a one-line recovery hint for the
three selection failures users hit most — bad key, unavailable model, backend
down. These lock the routing so a mis-selection always points to the fix.
"""

from __future__ import annotations

from mantis_agent.errors import AuthError
from mantis_agent.tui import error_hint


def test_bad_key_points_to_setup() -> None:
    assert "mantis setup" in error_hint(AuthError("Incorrect API key", status_code=401), None)
    assert "mantis setup" in error_hint(Exception("Incorrect API key provided"), None)


def test_unavailable_model_points_to_models_picker() -> None:
    assert "/models" in error_hint(Exception("The model `nope` does not exist"), None)
    assert "/models" in error_hint(Exception("not supported in the v1/chat/completions endpoint"), None)


def test_connection_hint_is_context_aware() -> None:
    local = error_hint(Exception("Connection refused"), "http://localhost:11434")
    assert "ollama serve" in local.lower()
    remote = error_hint(Exception("All connection attempts failed"), "https://api.openai.com/v1")
    assert "backend url" in remote.lower() or "network" in remote.lower()
    assert "ollama serve" not in remote.lower()


def test_no_hint_for_unrecognized_error() -> None:
    assert error_hint(Exception("some internal parse glitch"), None) is None
