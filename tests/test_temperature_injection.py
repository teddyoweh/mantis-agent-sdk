"""An unrequested temperature must not break a request.

Observed against a live Anthropic session:

    error: Anthropic API error (400): `temperature` is deprecated for this model.

Nobody had set a temperature. ``Agent`` fills ``recommended_temperature`` from
the capability table when the caller leaves it None — a value we invent — and
Anthropic's newer models reject an explicit temperature outright. So a default
of our own making turned every request into a 400.
"""

from __future__ import annotations

import pytest

from mantis_agent.agent import Agent
from mantis_agent.tools import Tool

ANTHROPIC = "https://api.anthropic.com/v1"


@pytest.fixture()
def _key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "A" * 40)


def _tool() -> Tool:
    return Tool(name="x", description="d", input_schema={"type": "object"},
                fn=lambda **k: "")


def test_anthropic_gets_no_temperature_unless_asked(_key) -> None:
    a = Agent(model="claude-opus-5", backend=ANTHROPIC, tools=[_tool()])
    assert a.temperature is None, "injected a temperature nobody requested"


def test_an_explicit_temperature_is_still_honoured(_key) -> None:
    # The user then owns the outcome, including a 400 on a model that rejects it.
    a = Agent(model="claude-opus-5", backend=ANTHROPIC, tools=[_tool()],
              temperature=0.3)
    assert a.temperature == 0.3


def test_anthropic_without_tools_is_also_left_alone(_key) -> None:
    a = Agent(model="claude-opus-5", backend=ANTHROPIC)
    assert a.temperature is None


def test_other_providers_keep_their_tuned_default() -> None:
    # The capability default exists for a reason — weak local models emit far
    # more malformed tool-call JSON at 0.7 — so this must stay for them.
    a = Agent(model="qwen3:8b", backend="http://localhost:11434/v1", tools=[_tool()])
    assert isinstance(a.temperature, float)
    assert a.temperature <= 0.2  # clamped for tool reliability
