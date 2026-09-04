"""Structured output across provider families.

The ``json_schema`` envelope is only sent where the backend can enforce it.
Elsewhere the engine falls back to instruct-and-parse: the schema rides in
the system prompt and ``response_model`` decoding (which already strips
fences) parses the text. A backend that only knows ``json_object`` gets that
mode plus the schema instruction.

The gate reads ``structured_output`` off the backend capability when present
(``"json_schema"`` | ``"json_object"`` | ``"none"``); without the flag it
falls back to the provider-name rule (Anthropic passthrough has no
``response_format`` at all).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anyio

from mantis_agent import Agent
from mantis_agent.capabilities import HOSTED_PROFILES
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import TextBlock, Usage, UserMessage

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "Color",
        "schema": {"type": "object", "properties": {"hex": {"type": "string"}}, "required": ["hex"]},
        "strict": True,
    },
}


def _events(text: str = '```json\n{"hex": "#fff"}\n```') -> list:
    return [
        MessageStart(message_id="m1", model="mock"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=1, output_tokens=1)),
        MessageStop(),
    ]


class _Named(MockProvider):
    def __init__(self, name: str, backend_capability: Any = None) -> None:
        super().__init__(scripted_events=_events())
        self.name = name
        if backend_capability is not None:
            self.backend_capability = backend_capability


def _run(provider: Any, **kw: Any) -> dict[str, Any]:
    agent = Agent(model="mock", provider=provider, response_format=SCHEMA, auto_compact=False,
                  include_recall=False, include_env=False, include_memory=False,
                  system="Be terse.", **kw)
    anyio.run(agent.run, [UserMessage(content="a color")])
    return provider.last_call_kwargs


def test_capable_backend_gets_the_native_envelope_and_no_prompt_instruction() -> None:
    kw = _run(_Named("mock"))
    assert kw["extra"]["response_format"]["type"] == "json_schema"
    assert "JSON Schema" not in (kw["system"] or "")


def test_anthropic_passthrough_falls_back_to_instruct_and_parse() -> None:
    kw = _run(_Named("anthropic_passthrough", HOSTED_PROFILES["anthropic"]))
    assert not (kw["extra"] or {}).get("response_format")
    system = kw["system"] or ""
    assert system.startswith("Be terse.")
    assert '"hex"' in system and "JSON" in system


def test_backend_flag_none_falls_back_to_instruct() -> None:
    cap = SimpleNamespace(**{**vars(HOSTED_PROFILES["mock"]), "structured_output": "none"})
    kw = _run(_Named("mock", cap))
    assert not (kw["extra"] or {}).get("response_format")
    assert '"hex"' in (kw["system"] or "")


def test_backend_flag_json_object_downgrades_schema_and_instructs() -> None:
    cap = SimpleNamespace(**{**vars(HOSTED_PROFILES["mock"]), "structured_output": "json_object"})
    kw = _run(_Named("mock", cap))
    assert kw["extra"]["response_format"] == {"type": "json_object"}
    assert '"hex"' in (kw["system"] or "")


def test_backend_flag_json_schema_keeps_native_path() -> None:
    cap = SimpleNamespace(**{**vars(HOSTED_PROFILES["mock"]), "structured_output": "json_schema"})
    kw = _run(_Named("mock", cap))
    assert kw["extra"]["response_format"]["type"] == "json_schema"
    assert "JSON Schema" not in (kw["system"] or "")


def test_instruct_fallback_output_decodes_via_response_model() -> None:
    """The fenced JSON a model emits under the instruct fallback decodes into
    the requested ``response_model`` type — the same decode ``query()`` runs."""
    from dataclasses import dataclass

    from mantis_agent.response_model import build_response_format, parse_response

    @dataclass
    class Color:
        hex: str

    prov = _Named("anthropic_passthrough", HOSTED_PROFILES["anthropic"])
    agent = Agent(model="mock", provider=prov, response_format=build_response_format(Color),
                  auto_compact=False, include_recall=False, include_env=False,
                  include_memory=False)
    msgs: list = [UserMessage(content="a color")]
    anyio.run(agent.run, msgs)
    assert '"hex"' in (prov.last_call_kwargs["system"] or "")
    final_text = "".join(b.text for b in msgs[-1].content if isinstance(b, TextBlock))
    assert parse_response(Color, final_text) == Color(hex="#fff")
