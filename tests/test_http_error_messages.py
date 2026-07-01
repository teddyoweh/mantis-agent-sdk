"""When a picked model/key fails at runtime, the user must see the *provider's*
error message (not a bare "401 Unauthorized" or a traceback). ``raise_for_status``
extracts the API message (OpenAI/Anthropic/Gemini shapes) and maps to a typed
error; these lock that so a bad selection is always self-explanatory.
"""

from __future__ import annotations

import httpx

from mantis_agent.errors import AuthError, ProviderError, RateLimitError
from mantis_agent.http import raise_for_status


def _resp(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://x/v1/chat/completions"))


def test_401_becomes_auth_error_with_api_message() -> None:
    body = {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}}
    try:
        raise_for_status(_resp(401, body))
        raise AssertionError("should have raised")
    except AuthError as e:
        assert "Incorrect API key" in str(e)
        assert e.status_code == 401


def test_404_model_not_found_becomes_provider_error_with_message() -> None:
    body = {"error": {"message": "The model `nope` does not exist", "type": "invalid_request_error"}}
    try:
        raise_for_status(_resp(404, body))
        raise AssertionError("should have raised")
    except ProviderError as e:
        assert "does not exist" in str(e)


def test_429_becomes_rate_limit_error() -> None:
    try:
        raise_for_status(_resp(429, {"error": {"message": "Rate limit reached"}}))
        raise AssertionError("should have raised")
    except RateLimitError as e:
        assert "Rate limit" in str(e)


def test_anthropic_error_shape_message_is_extracted() -> None:
    # Anthropic buries it the same way ({"error": {"type", "message"}}).
    body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    try:
        raise_for_status(_resp(401, body))
        raise AssertionError("should have raised")
    except AuthError as e:
        assert "invalid x-api-key" in str(e)
