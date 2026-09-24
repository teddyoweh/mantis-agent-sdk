"""The per-turn tool-schema token estimate is cached (it used to re-serialize
every schema 2-3x per turn) and invalidates whenever the wire would change:
defer / surface, add, a reassigned description, the compact switch."""

from __future__ import annotations

import json

from mantis_agent import Agent, tool


def _mk(name: str, doc: str = "A tool."):
    @tool(name=name, input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
    async def f(x: str = "") -> str:
        return x
    f.description = doc
    return f


def _truth(agent: Agent) -> int:
    return len(json.dumps(agent.tools.to_wire(compact=agent._compact_tool_wire()))) // 4


def test_cache_hits_without_reserializing(monkeypatch) -> None:
    a = Agent(model="qwen3:8b", backend="http://localhost:11434",
              tools=[_mk("alpha"), _mk("beta")])
    first = a._tool_wire_tokens()
    assert first == _truth(a)
    calls = {"n": 0}
    real = type(a.tools).to_wire

    def counting(self, **kw):
        calls["n"] += 1
        return real(self, **kw)

    monkeypatch.setattr(type(a.tools), "to_wire", counting)
    for _ in range(5):
        assert a._tool_wire_tokens() == first
    assert calls["n"] == 0


def test_cache_invalidates_on_defer_surface_add_and_description() -> None:
    a = Agent(model="qwen3:8b", backend="http://localhost:11434",
              tools=[_mk("alpha", "short"), _mk("beta", "x" * 400)])
    full = a._tool_wire_tokens()

    assert a.tools.defer("beta") == 1
    deferred = a._tool_wire_tokens()
    assert deferred < full and deferred == _truth(a)

    assert a.tools.surface("beta")
    assert a._tool_wire_tokens() == full == _truth(a)

    a.tools.add(_mk("gamma", "y" * 800))
    grown = a._tool_wire_tokens()
    assert grown > full and grown == _truth(a)

    a.tools.get("alpha").description = "z" * 1200
    assert a._tool_wire_tokens() == _truth(a) > grown


def test_overhead_still_counts_system_and_tools() -> None:
    a = Agent(model="qwen3:8b", backend="http://localhost:11434",
              tools=[_mk("alpha")], system="s" * 400)
    assert a._prompt_overhead_tokens() == 100 + _truth(a)
