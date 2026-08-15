"""``response_model`` — ask for a type, get an instance back.

Structured output worked before this, but the caller did all the work::

    SCHEMA = {"type": "json_schema", "json_schema": {"name": "invoice",
              "schema": {"type": "object", "properties": {...},
                         "required": [...], "additionalProperties": False},
              "strict": True}}
    ...
    data = json.loads(final_text)          # and hope it parsed

That is the provider's envelope, hand-written, plus an unchecked ``json.loads``
of whatever the model emitted. Every field name exists twice — once in your
schema and once in the class you decode into — so they drift.

With this module you hand over the type you already have::

    @dataclass
    class Invoice:
        vendor: str
        total_usd: float
        due_date: str

    options = {"model": ..., "response_model": Invoice}
    ...
    result.parsed        # -> Invoice(vendor='Northwind Traders', ...)

Supported types: anything ``msgspec`` understands (dataclasses,
``msgspec.Struct``, ``TypedDict``, ``NamedTuple``, and plain
``list``/``dict`` generics of those) and pydantic ``BaseModel`` subclasses.

Two deliberate behaviors, both learned from watching small models:

* **Fences are stripped.** A 7B asked for JSON frequently answers with a
  ```` ```json ```` block. Refusing to parse that is technically correct and
  practically useless.
* **A parse failure is a run failure.** You asked for an ``Invoice``; ending
  with ``parsed=None`` and a success flag would be the same silent-failure
  trap that makes provider errors so hard to spot. It lands in
  ``result.errors``, and ``raise_on_error=True`` turns it into an exception.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["build_response_format", "is_supported", "parse_response", "schema_for"]


def _is_pydantic(model: Any) -> bool:
    return hasattr(model, "model_json_schema") and hasattr(model, "model_validate_json")


def is_supported(model: Any) -> bool:
    """Can we derive a schema from — and decode into — this type?"""

    if model is None or not isinstance(model, type):
        return False
    if _is_pydantic(model):
        return True
    try:
        schema = schema_for(model)
    except Exception:  # noqa: BLE001 — "no" is the only answer that matters
        return False
    # Must describe an object. ``str`` and friends produce a valid schema that
    # no provider will accept as a strict response format, and "your scalar
    # silently did nothing" is the failure mode this whole feature exists to
    # avoid.
    return schema.get("type") == "object"


def _inline_root(schema: dict[str, Any]) -> dict[str, Any]:
    """msgspec returns ``{"$ref": "#/$defs/X", "$defs": {...}}``.

    Providers in strict mode want the object schema itself at the root, so
    hoist the referenced definition and keep any *other* definitions behind for
    nested types to resolve against.
    """

    ref = schema.get("$ref")
    defs = dict(schema.get("$defs") or {})
    if not ref or not ref.startswith("#/$defs/"):
        return schema
    name = ref.split("/")[-1]
    root = dict(defs.pop(name, {}))
    if defs:
        root["$defs"] = defs
    return root


def _strictify(node: Any) -> Any:
    """Set ``additionalProperties: false`` on every object.

    OpenAI's strict json_schema mode rejects a schema without it, and models
    that aren't strict-checked still hallucinate fewer stray keys with it
    present.
    """

    if isinstance(node, dict):
        out = {k: _strictify(v) for k, v in node.items()}
        if out.get("type") == "object" and "additionalProperties" not in out:
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    return node


def schema_for(model: Any) -> dict[str, Any]:
    """JSON Schema for ``model``, inlined and strict-friendly."""

    if _is_pydantic(model):
        return _strictify(model.model_json_schema())

    import msgspec  # local: keeps import cost off the module import path

    return _strictify(_inline_root(msgspec.json.schema(model)))


def build_response_format(model: Any) -> dict[str, Any]:
    """The provider-facing ``response_format`` for ``model``."""

    name = getattr(model, "__name__", "response")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": schema_for(model),
            "strict": True,
        },
    }


_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _unfence(text: str) -> str:
    m = _FENCE.match(text or "")
    return m.group(1) if m else (text or "")


def parse_response(model: Any, text: str) -> Any:
    """Decode ``text`` into ``model``. Raises ``ValueError`` on bad output.

    The error message carries the head of what the model actually said —
    debugging a schema mismatch without seeing the payload is guesswork.
    """

    raw = _unfence(text).strip()
    if not raw:
        raise ValueError("response_model: the model returned no text to parse")

    if _is_pydantic(model):
        try:
            return model.model_validate_json(raw)
        except Exception as e:  # noqa: BLE001 — pydantic's own error type varies
            raise ValueError(
                f"response_model: could not parse into {model.__name__}: {e}; "
                f"model said: {raw[:200]!r}"
            ) from None

    import msgspec

    try:
        return msgspec.json.decode(raw.encode("utf-8"), type=model)
    except (msgspec.ValidationError, msgspec.DecodeError, json.JSONDecodeError) as e:
        raise ValueError(
            f"response_model: could not parse into "
            f"{getattr(model, '__name__', model)}: {e}; model said: {raw[:200]!r}"
        ) from None
