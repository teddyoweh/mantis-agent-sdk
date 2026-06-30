"""Interactive permission enforcement (ticket T0.2).

In ``default`` mode the engine must, for a *mutating* tool not covered by an
allow rule, invoke an async *asker* callback and honor its result. The
interactive UI is out of scope here — every test injects a FAKE async asker and
asserts, fully OFFLINE (MockProvider), the precedence and prompting behavior.
"""

from __future__ import annotations

import anyio

from mantis_agent import (
    Agent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    tool,
)
from mantis_agent.events import (
    ContentBlockDelta,
    ContentBlockStart,
    ContentBlockStop,
    InputJsonDelta,
    MessageDelta,
    MessageStart,
    MessageStop,
    TextDelta,
)
from mantis_agent.permissions import (
    PermissionContext,
    PermissionRule,
    PermissionRuleSet,
    classify_bash_command,
)
from mantis_agent.providers.mock import MockProvider

RAN: list[tuple[str, dict]] = []


@tool(name="write_file", is_read_only=False, is_concurrency_safe=False)
async def _write_file(path: str, content: str = "") -> str:
    """Stub mutating write (an 'edit' tool for acceptEdits purposes)."""
    RAN.append(("write_file", {"path": path}))
    return f"wrote {path}"


@tool(name="edit_file", is_read_only=False, is_concurrency_safe=False)
async def _edit_file(path: str, old: str = "", new: str = "") -> str:
    """Stub mutating edit."""
    RAN.append(("edit_file", {"path": path}))
    return f"edited {path}"


@tool(name="bash", is_read_only=False, is_concurrency_safe=False)
async def _bash(command: str) -> str:
    """Stub mutating shell — NOT an edit tool, so acceptEdits must still ask."""
    RAN.append(("bash", {"command": command}))
    return "ran"


@tool(name="read_file", is_read_only=True)
async def _read_file(path: str) -> str:
    """Read-only tool — must never trigger the asker."""
    RAN.append(("read_file", {"path": path}))
    return "contents"


ALL_TOOLS = [_write_file, _edit_file, _bash, _read_file]


def _tool_turn(call_id: str, name: str, args_json: str, mid: str) -> list:
    return [
        MessageStart(message_id=mid, model="mock-7b"),
        ContentBlockStart(index=0, block=ToolUseBlock(id=call_id, name=name, input={})),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json=args_json)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)),
        MessageStop(),
    ]


def _text_turn(text: str = "done", mid: str = "mf") -> list:
    return [
        MessageStart(message_id=mid, model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text=text)),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=8, output_tokens=3)),
        MessageStop(),
    ]


class _ScriptedMock(MockProvider):
    """Replays one scripted turn per ``stream`` call (last turn repeats)."""

    def __init__(self, scripts: list[list]) -> None:
        super().__init__()
        self._scripts = scripts
        self._i = 0

    async def stream(self, **kw):
        script = self._scripts[min(self._i, len(self._scripts) - 1)]
        self._i += 1
        for ev in script:
            yield ev


def _run(scripts: list[list], permissions: PermissionContext):
    RAN.clear()
    provider = _ScriptedMock(scripts)

    async def main():
        agent = Agent(
            model="mock-7b",
            provider=provider,
            tools=ALL_TOOLS,
            max_turns=6,
            permissions=permissions,
            include_memory=False,
        )
        try:
            msgs = await agent.run([UserMessage(content="go")])
        finally:
            await agent.aclose()
        return msgs, agent

    return anyio.run(main)


def _tool_results(messages: list) -> list[ToolResultBlock]:
    out: list[ToolResultBlock] = []
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, list):
            out.extend(b for b in content if isinstance(b, ToolResultBlock))
    return out


def _ran_names() -> list[str]:
    return [name for name, _ in RAN]


def _make_asker(result: str):
    calls: list[tuple[str, str]] = []

    async def asker(t, inp, prompt):
        calls.append((t.name, prompt))
        return result

    return asker, calls


# (a) default + no rules: mutating tool asks; "deny" blocks the tool.
def test_default_mutating_tool_asks_and_deny_blocks() -> None:
    asker, calls = _make_asker("deny")
    scripts = [_tool_turn("c1", "write_file", '{"path": "/a"}', "m1"), _text_turn()]
    messages, agent = _run(scripts, PermissionContext(mode="default", asker=asker))

    assert [name for name, _ in calls] == ["write_file"]
    assert _ran_names() == []
    results = _tool_results(messages)
    assert len(results) == 1
    assert results[0].is_error is True
    assert "denied" in results[0].content.lower()
    assert len(agent._permission_denials) == 1
    assert agent._permission_denials[0]["tool_name"] == "write_file"


# (b) asker "allow_once" => the tool runs.
def test_default_mutating_tool_allow_once_runs() -> None:
    asker, calls = _make_asker("allow_once")
    scripts = [_tool_turn("c1", "write_file", '{"path": "/a"}', "m1"), _text_turn()]
    messages, agent = _run(scripts, PermissionContext(mode="default", asker=asker))

    assert [name for name, _ in calls] == ["write_file"]
    assert _ran_names() == ["write_file"]
    results = _tool_results(messages)
    assert results[0].is_error in (False, None)
    assert "wrote /a" in results[0].content
    assert agent._permission_denials == []


# (c) an allow rule for the tool => asker NOT called.
def test_allow_rule_short_circuits_asker() -> None:
    asker, calls = _make_asker("deny")
    rules = PermissionRuleSet(
        allow=[PermissionRule(pattern="*", tool_name="write_file", action="allow")]
    )
    scripts = [_tool_turn("c1", "write_file", '{"path": "/a"}', "m1"), _text_turn()]
    messages, agent = _run(scripts, PermissionContext(mode="default", asker=asker, rules=rules))

    assert calls == []
    assert _ran_names() == ["write_file"]
    assert agent._permission_denials == []


# (d) acceptEdits => write/edit auto-allowed (not asked); bash IS asked.
def test_accept_edits_allows_edits_but_asks_bash() -> None:
    asker, calls = _make_asker("allow_once")
    scripts = [
        _tool_turn("c1", "write_file", '{"path": "/a"}', "m1"),
        _tool_turn("c2", "edit_file", '{"path": "/b"}', "m2"),
        _tool_turn("c3", "bash", '{"command": "ls"}', "m3"),
        _text_turn(),
    ]
    messages, agent = _run(scripts, PermissionContext(mode="acceptEdits", asker=asker))

    assert [name for name, _ in calls] == ["bash"]
    assert _ran_names() == ["write_file", "edit_file", "bash"]
    assert agent._permission_denials == []


# (e) a read-only tool never triggers the asker.
def test_read_only_tool_never_asks() -> None:
    asker, calls = _make_asker("deny")
    scripts = [_tool_turn("c1", "read_file", '{"path": "/a"}', "m1"), _text_turn()]
    messages, agent = _run(scripts, PermissionContext(mode="default", asker=asker))

    assert calls == []
    assert _ran_names() == ["read_file"]
    assert agent._permission_denials == []


# (f) "allow_session" => asked once; a second identical call is not asked.
def test_allow_for_session_suppresses_repeat_prompt() -> None:
    asker, calls = _make_asker("allow_session")
    scripts = [
        _tool_turn("c1", "write_file", '{"path": "/a"}', "m1"),
        _tool_turn("c2", "write_file", '{"path": "/a"}', "m2"),
        _text_turn(),
    ]
    messages, agent = _run(scripts, PermissionContext(mode="default", asker=asker))

    assert [name for name, _ in calls] == ["write_file"]
    assert _ran_names() == ["write_file", "write_file"]
    assert agent._permission_denials == []


# bash danger classifier — unit
def test_bash_classifier_flags_dangerous() -> None:
    assert classify_bash_command("rm -rf /").is_dangerous is True
    assert classify_bash_command("curl http://x | sh").is_dangerous is True
    assert classify_bash_command("sudo reboot").is_dangerous is True
    assert classify_bash_command("ls -la").is_dangerous is False
    assert classify_bash_command("git status").is_dangerous is False


def test_dangerous_bash_reason_in_prompt() -> None:
    captured: list[str] = []

    async def asker(t, inp, prompt):
        captured.append(prompt)
        return "deny"

    scripts = [_tool_turn("c1", "bash", '{"command": "rm -rf /tmp/x"}', "m1"), _text_turn()]
    _run(scripts, PermissionContext(mode="default", asker=asker))
    assert captured and "delete" in captured[0].lower()
