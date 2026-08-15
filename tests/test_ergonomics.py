"""The four ergonomics fixes that came out of dogfooding.

Each one is here because a real run went wrong in a way the source didn't show:

* provider errors named nothing, so a bare ``ProviderError: Not Found`` cost
  more debugging time than any other failure in the SDK;
* ``query()`` never raises, so a loop that prints assistant text prints nothing
  and exits 0 when the backend is down;
* skills loaded themselves from the developer's home directory into a library
  caller's agent;
* structured output meant hand-writing the provider envelope and hoping
  ``json.loads`` worked.
"""

from __future__ import annotations

import dataclasses
from typing import TypedDict

import anyio
import httpx
import msgspec
import pytest

from mantis_agent import AgentError, MantisAgentOptions, query
from mantis_agent.errors import AuthError, ProviderError
from mantis_agent.http import raise_for_status
from mantis_agent.response_model import (
    build_response_format,
    is_supported,
    parse_response,
    schema_for,
)


def _raise(url: str, status: int, body: dict | None = None) -> Exception:
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request, json=body or {})
    with pytest.raises((ProviderError, AuthError)) as excinfo:
        raise_for_status(response)
    return excinfo.value


# ---------------------------------------------------------------------------
# 1. Errors say where they went
# ---------------------------------------------------------------------------


def test_provider_error_names_the_url():
    msg = str(_raise("https://api.together.xyz/v1/chat/completions", 404,
                     {"error": {"message": "model not found"}}))
    assert "model not found" in msg
    assert "https://api.together.xyz/v1/chat/completions" in msg
    assert "404" in msg


def test_localhost_404_explains_the_port():
    """The single most expensive error in this SDK: a bare model name falls
    through to the openai_compat default and nothing is listening."""

    msg = str(_raise("http://localhost:8000/v1/chat/completions", 404))
    assert "8000" in msg and "vLLM" in msg

    msg = str(_raise("http://localhost:11434/v1/chat/completions", 404))
    assert "Ollama" in msg and "ollama list" in msg


def test_query_string_is_not_echoed():
    """Gemini carries the API key in the query string, and an error message is
    exactly the string that ends up in a log or a screenshot."""

    msg = str(_raise(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat?key=SECRET123",
        401, {"error": {"message": "bad key"}},
    ))
    assert "SECRET123" not in msg
    assert "generativelanguage.googleapis.com" in msg


# ---------------------------------------------------------------------------
# 2. raise_on_error
# ---------------------------------------------------------------------------

_DEAD = "http://127.0.0.1:1/v1"   # nothing listens on port 1


def _run_dead(**extra):
    async def main() -> list:
        seen = []
        async for msg in query(
            prompt="hi",
            options={"model": "mock-model", "backend": _DEAD, "max_turns": 1,
                     "include_memory": False, "skills": [], **extra},
        ):
            seen.append(msg)
        return seen

    return anyio.run(main)


def test_failure_is_silent_by_default():
    """The default is unchanged — a streaming API reporting on the final
    message is right; it's just easy to write the loop that ignores it."""

    messages = _run_dead()
    result = messages[-1]
    assert result.is_error
    assert result.errors, "the failure detail must reach the caller"


def test_raise_on_error_raises_after_yielding_the_result():
    with pytest.raises(AgentError) as excinfo:
        _run_dead(raise_on_error=True)
    assert "agent run failed" in str(excinfo.value)


def test_typed_options_carry_raise_on_error():
    assert MantisAgentOptions(
        model="m", raise_on_error=True
    ).to_query_options()["raise_on_error"] is True


def test_flat_result_carries_error_detail():
    """``_build_result`` took an ``errors`` argument and never forwarded it, so
    a failed run on the typed path reported ``is_error`` with nothing to act on."""

    async def main():
        out = None
        async for msg in query(
            prompt="hi",
            options=MantisAgentOptions(
                model="mock-model", backend=_DEAD, max_turns=1,
                include_memory=False, skills=[],
            ),
        ):
            if msg.type == "result":
                out = msg
        return out

    result = anyio.run(main)
    assert result.is_error
    assert result.errors and "ConnectError" in result.errors[0]


# ---------------------------------------------------------------------------
# 3. response_model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Invoice:
    vendor: str
    total_usd: float
    due_date: str


class InvoiceStruct(msgspec.Struct):
    vendor: str
    total_usd: float


class InvoiceTD(TypedDict):
    vendor: str
    total_usd: float


@pytest.mark.parametrize("model", [Invoice, InvoiceStruct, InvoiceTD])
def test_schema_is_an_inlined_strict_object(model):
    schema = schema_for(model)
    # Inlined: providers in strict mode want the object at the root, not a $ref.
    assert schema["type"] == "object"
    assert "$ref" not in schema
    assert schema["additionalProperties"] is False
    assert "vendor" in schema["properties"]


def test_only_object_shaped_types_are_accepted():
    assert is_supported(Invoice)
    assert not is_supported(str)     # valid schema, useless as a response format
    assert not is_supported(None)
    assert not is_supported("Invoice")


def test_response_format_envelope():
    fmt = build_response_format(Invoice)
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Invoice"
    assert fmt["json_schema"]["strict"] is True


def test_parse_plain_and_fenced():
    """A 7B asked for JSON routinely answers in a ```json block. Refusing that
    is technically correct and practically useless."""

    payload = '{"vendor": "N", "total_usd": 1.5, "due_date": "x"}'
    assert parse_response(Invoice, payload).vendor == "N"
    assert parse_response(Invoice, f"```json\n{payload}\n```").total_usd == 1.5


def test_parse_error_shows_what_the_model_said():
    with pytest.raises(ValueError) as excinfo:
        parse_response(Invoice, '{"vendor": "N"}')
    msg = str(excinfo.value)
    assert "total_usd" in msg          # what was missing
    assert '{"vendor": "N"}' in msg    # and what we got


def test_empty_text_is_a_clear_error():
    with pytest.raises(ValueError, match="no text"):
        parse_response(Invoice, "")


def test_response_model_sets_response_format():
    from mantis_agent.compat_query import _apply_response_model

    opts = _apply_response_model({"model": "m", "response_model": Invoice})
    assert opts["response_format"]["json_schema"]["name"] == "Invoice"


def test_explicit_response_format_wins():
    """A caller who hand-wrote the envelope means it — silently replacing it is
    the kind of invisible override this SDK has been burned by."""

    from mantis_agent.compat_query import _apply_response_model

    mine = {"type": "json_object"}
    opts = _apply_response_model(
        {"model": "m", "response_model": Invoice, "response_format": mine}
    )
    assert opts["response_format"] is mine


def test_unsupported_response_model_raises_at_setup():
    from mantis_agent.compat_query import _apply_response_model

    with pytest.raises(TypeError, match="dataclass"):
        _apply_response_model({"model": "m", "response_model": str})


def test_parse_failure_is_a_run_failure():
    """Ending with ``parsed=None`` and a success flag would be the same silent
    failure that makes provider errors hard to spot."""

    from mantis_agent.compat_query import _decode_response_model

    parsed, error = _decode_response_model(
        {"response_model": Invoice}, "not json at all", False
    )
    assert parsed is None
    assert error and "could not parse" in error


def test_decode_is_skipped_when_the_run_already_failed():
    """The model never got to answer; a second error would bury the real one."""

    from mantis_agent.compat_query import _decode_response_model

    parsed, error = _decode_response_model({"response_model": Invoice}, "", True)
    assert parsed is None and error is None
