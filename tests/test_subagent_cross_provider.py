"""Agent types pinned to another provider's model (a Grok or Claude scout under
a GPT lead) run on that provider, not the parent's endpoint."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from mantis_agent import catalog
from mantis_agent.providers.mock import MockProvider
from mantis_agent.subagent import AgentType, make_task_tool
from mantis_agent.tui import MantisTUI

SCOUT = AgentType(name="scout", description="", system_prompt="be brief",
                  tools="read-only", model="grok-4")
PLAIN = AgentType(name="plain", description="", system_prompt="be brief", tools="read-only")


def _task(route: Any, *types: AgentType) -> Any:
    return make_task_tool(model="gpt-6-astra", provider=MockProvider(default_text="parent"),
                          tools=[], agent_types=list(types) or [SCOUT], route_model=route)


def _run(t: Any, kind: str = "scout") -> str:
    return anyio.run(lambda: t.fn(prompt="look", subagent_type=kind))


def test_routed_provider_runs_the_child() -> None:
    seen: list[str] = []

    def route(model: str) -> Any:
        seen.append(model)
        return MockProvider(default_text="routed")

    assert _run(_task(route)) == "routed"
    assert seen == ["grok-4"]


def test_none_keeps_the_parents_provider() -> None:
    assert _run(_task(lambda m: None)) == "parent"


def test_a_string_is_returned_as_the_reason() -> None:
    out = _run(_task(lambda m: "Grok (xAI) isn't enabled — /enable xai"))
    assert "can't run on grok-4" in out and "/enable xai" in out


def test_a_raising_router_is_reported_not_raised() -> None:
    def route(model: str) -> Any:
        raise RuntimeError("boom")
    assert "RuntimeError: boom" in _run(_task(route))


def test_types_without_a_model_never_consult_the_router() -> None:
    def route(model: str) -> Any:
        raise AssertionError("called")
    assert _run(_task(route, PLAIN), "plain") == "parent"


# -- the terminal's router -----------------------------------------------------

@pytest.fixture
def tui(monkeypatch: pytest.MonkeyPatch) -> Any:
    keys = {"openai", "xai"}
    monkeypatch.setattr(catalog, "api_key_for",
                        lambda p: f"sk-{p.id}" if p.id in keys else None)
    monkeypatch.setattr(catalog, "bearer_backend", lambda p, cur=None: None)
    t = MantisTUI.__new__(MantisTUI)
    t.backend = "https://api.openai.com/v1"
    return t


def test_same_provider_stays_on_the_parent(tui: Any) -> None:
    assert tui._route_child_model("gpt-5.4-mini") is None


def test_another_enabled_family_gets_its_own_provider(tui: Any) -> None:
    p = tui._route_child_model("grok-4")
    assert p is not None and not isinstance(p, str)
    assert "x.ai" in str(p.client.base_url)
    assert tui._route_child_model("grok-4") is p   # built once per session


def test_a_locked_family_under_a_lab_parent_says_how_to_enable(tui: Any) -> None:
    out = tui._route_child_model("claude-sonnet-5")
    assert isinstance(out, str) and "/enable anthropic" in out


def test_open_models_under_a_local_parent_stay_local(tui: Any) -> None:
    tui.backend = "http://localhost:11434/v1"
    assert tui._route_child_model("qwen-3.8-27b") is None
    assert tui._route_child_model("grok-4") is not None   # a lab model can't be local


def test_a_gateway_parent_keeps_models_it_may_serve(tui: Any) -> None:
    gateway = next(p for p in catalog.CATALOG if p.id == "openrouter")
    tui.backend = gateway.base_url
    assert tui._route_child_model("claude-sonnet-5") is None


def test_unknown_ids_stay_on_the_parent(tui: Any) -> None:
    assert tui._route_child_model("my-finetune-v3") is None


def test_the_start_event_says_whether_the_type_pins_its_model() -> None:
    events: list[dict] = []
    t = make_task_tool(model="gpt-6-astra", provider=MockProvider(default_text="ok"), tools=[],
                       agent_types=[SCOUT, PLAIN], on_progress=events.append)
    _run(t, "scout")
    _run(t, "plain")
    starts = [e for e in events if e.get("phase") == "start"]
    assert [(e["type"], e["model"], e["pinned"]) for e in starts] == [
        ("scout", "grok-4", True), ("plain", "gpt-6-astra", False)]
