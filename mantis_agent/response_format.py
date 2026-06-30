"""Structured-output ``response_format`` plumbing.

The user-facing API is the OpenAI shape — it's the most-deployed, and Ollama,
vLLM, Together, Fireworks, llama.cpp, TGI all either accept it natively or
need a tiny rewrite::

    # Free-form JSON object (the model must emit valid JSON; no schema check)
    response_format = {"type": "json_object"}

    # Schema-constrained JSON (model output is validated against ``schema``)
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "Color",
            "schema": {
                "type": "object",
                "properties": {"hex": {"type": "string"}},
                "required": ["hex"],
            },
            "strict": True,
        },
    }

Convenience shortcut accepted on input — when callers don't care about the
OpenAI envelope::

    response_format = {"type": "json_schema", "schema": {...}}        # flat
    response_format = {"json_schema": {"schema": {...}, "name": ...}} # OpenAI

We normalize both into the OpenAI-canonical shape so every downstream branch
only deals with one form.

Per-backend translation rules — see :func:`translate_response_format`:

  * ``openai_compat`` / ``llamacpp`` / ``modal`` → pass the OpenAI envelope
    through verbatim. Servers handle the rest (vLLM uses ``guided_json``,
    llama.cpp uses ``--jinja`` JSON mode, Modal proxies to whichever).
  * ``ollama`` → its native API uses a top-level ``format`` field; we map
    ``json_object`` to the string ``"json"`` and ``json_schema`` to the raw
    schema dict.
  * ``tgi`` → uses ``parameters.grammar = {"type": "json", "value": <schema>}``
    in chat-completions mode; we emit that under ``parameters``.
  * ``anthropic_passthrough`` → real Anthropic API has no ``response_format``;
    users get a clear ``ValueError`` instead of silent ignoring.
  * ``mock`` → stores under ``extra["response_format"]`` so tests can assert
    on the value as-presented (no translation).

Validation is intentionally strict — silent fallthrough is the worst outcome
(the model just spits free text and a downstream JSON parse blows up much
later, far from the bug).
"""

from __future__ import annotations

from typing import Any

# Per-backend translation registrations live below; this lookup makes the
# Agent plumbing one line instead of an if/elif tree.
__all__ = [
    "ResponseFormatError",
    "normalize_response_format",
    "translate_response_format",
]


class ResponseFormatError(ValueError):
    """Raised when ``response_format`` is malformed or unsupported on this
    backend. Subclass of ``ValueError`` so callers wanting a generic catch
    still work, but distinct enough for surgical handling."""


# Canonicalized shapes the rest of the module deals with:
#   ("json_object", None)
#   ("json_schema", {"name": str | None, "schema": dict, "strict": bool | None})
_CanonicalRF = tuple[str, dict[str, Any] | None]


def normalize_response_format(rf: Any) -> _CanonicalRF:
    """Validate + canonicalize the user-supplied ``response_format``.

    Returns a ``(kind, payload)`` tuple where ``kind`` is one of
    ``"json_object"`` or ``"json_schema"``. ``payload`` is ``None`` for
    json_object; for json_schema it's ``{"name": ..., "schema": ...,
    "strict": ...}``.

    Raises :class:`ResponseFormatError` on anything malformed. The error
    messages are intentionally long-form — this is the first place a user
    types a bad value, and a cryptic ``KeyError`` deep in a provider is the
    failure mode we're trying to avoid.
    """

    if rf is None:
        raise ResponseFormatError(
            "response_format=None passed to normalize_response_format — "
            "callers should drop the field entirely when unset."
        )
    if not isinstance(rf, dict):
        raise ResponseFormatError(
            f"response_format must be a dict, got {type(rf).__name__}. "
            "Example: response_format={'type': 'json_object'}."
        )

    rf_type = rf.get("type")
    # Allow the flat shortcut: response_format={"schema": {...}} implies
    # json_schema. Be explicit about it so downstream sees the canonical
    # shape and a future reader doesn't have to guess what we silently did.
    if rf_type is None:
        if "json_schema" in rf or "schema" in rf:
            rf_type = "json_schema"
        else:
            raise ResponseFormatError(
                "response_format is missing 'type'. Use "
                "{'type': 'json_object'} or {'type': 'json_schema', "
                "'json_schema': {...}}."
            )

    if rf_type == "json_object":
        # OpenAI semantics: json_object accepts no extra fields (it's just a
        # signal). Extra keys probably indicate the user meant json_schema
        # but forgot the type — point them there.
        leftover = {k for k in rf if k != "type"}
        if leftover:
            raise ResponseFormatError(
                f"response_format type=json_object accepts no other keys, "
                f"got: {sorted(leftover)}. Did you mean type='json_schema'?"
            )
        return "json_object", None

    if rf_type == "json_schema":
        # Accept either OpenAI's nested envelope (response_format =
        # {"type":"json_schema","json_schema":{"name":..., "schema":...}})
        # OR the flat shortcut (response_format =
        # {"type":"json_schema","schema":{...}}).
        nested = rf.get("json_schema")
        if nested is not None:
            if not isinstance(nested, dict):
                raise ResponseFormatError(
                    "response_format.json_schema must be a dict."
                )
            schema = nested.get("schema")
            name = nested.get("name")
            strict = nested.get("strict")
        else:
            schema = rf.get("schema")
            name = rf.get("name")
            strict = rf.get("strict")

        if not isinstance(schema, dict):
            raise ResponseFormatError(
                "response_format type=json_schema requires a 'schema' dict "
                "(either nested under 'json_schema' OpenAI-style, or flat)."
            )
        if name is not None and not isinstance(name, str):
            raise ResponseFormatError(
                "response_format json_schema.name must be a string."
            )
        if strict is not None and not isinstance(strict, bool):
            raise ResponseFormatError(
                "response_format json_schema.strict must be a bool."
            )
        return "json_schema", {"name": name, "schema": schema, "strict": strict}

    raise ResponseFormatError(
        f"unknown response_format type {rf_type!r}. Supported: "
        "'json_object', 'json_schema'."
    )


def _to_openai_envelope(kind: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Build the OpenAI-canonical request body fragment for ``response_format``."""

    if kind == "json_object":
        return {"type": "json_object"}
    assert kind == "json_schema" and payload is not None
    inner: dict[str, Any] = {"schema": payload["schema"]}
    # Default ``name`` to "response" — OpenAI strict mode requires a name,
    # and "response" is the convention the OpenAI cookbook uses when the
    # user doesn't care. Naming this explicitly avoids a 400 from servers
    # that mirror OpenAI's strict check.
    inner["name"] = payload["name"] or "response"
    if payload["strict"] is not None:
        inner["strict"] = payload["strict"]
    return {"type": "json_schema", "json_schema": inner}


def translate_response_format(
    rf: Any, provider_name: str
) -> dict[str, Any]:
    """Translate ``rf`` to the extra-dict fragment for ``provider_name``.

    The return value is shallow-merged into the provider's ``extra=`` kwarg
    by the Agent layer; an empty dict means "this backend doesn't need
    anything beyond what the system prompt already conveys."

    Raises :class:`ResponseFormatError` for malformed input or backends
    that don't support structured output at all.
    """

    kind, payload = normalize_response_format(rf)

    if provider_name in ("openai_compat", "llamacpp", "modal"):
        return {"response_format": _to_openai_envelope(kind, payload)}

    if provider_name == "ollama":
        # Ollama's native API: ``format="json"`` for free-form, or
        # ``format=<schema dict>`` for schema-constrained generation.
        # https://github.com/ollama/ollama/blob/main/docs/api.md#request-with-json-mode
        if kind == "json_object":
            return {"format": "json"}
        assert payload is not None
        return {"format": payload["schema"]}

    if provider_name == "tgi":
        # text-generation-inference uses ``parameters.grammar`` for
        # constrained generation. ``json_object`` falls back to a permissive
        # JSON grammar (TGI doesn't have a built-in unconstrained-JSON
        # toggle; users who want strict shape should pass a schema).
        if kind == "json_object":
            return {"parameters": {"grammar": {"type": "json", "value": {}}}}
        assert payload is not None
        return {
            "parameters": {
                "grammar": {"type": "json", "value": payload["schema"]}
            }
        }

    if provider_name == "anthropic_passthrough":
        # Real Anthropic API doesn't have response_format. The canonical
        # workaround is tool-use with a single forced tool whose
        # input_schema is the desired shape — not something we want to
        # silently rewrite (it'd shadow the user's actual tools). Be loud.
        raise ResponseFormatError(
            "response_format is not supported on the anthropic_passthrough "
            "backend. Anthropic's recommended structured-output pattern is a "
            "forced tool call — define a Tool whose input_schema matches your "
            "shape and use tool_choice={'type':'tool','name':'<tool>'} "
            "(handled at the agent level, not response_format)."
        )

    if provider_name == "mock":
        # No translation — surface the *canonicalized* form so tests can
        # assert on the normalized shape regardless of which shortcut the
        # caller used. Keep the OpenAI envelope as the canonical form here
        # too (matches what real providers see for openai_compat).
        return {"response_format": _to_openai_envelope(kind, payload)}

    raise ResponseFormatError(
        f"provider {provider_name!r} does not yet support response_format. "
        "Open an issue if you need it on this backend."
    )
