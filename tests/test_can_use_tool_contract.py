"""``can_use_tool`` must receive the tool NAME, and must be wired on both paths.

Found by dogfooding. A policy written exactly as the permissions guide (and the
Claude Agent SDK) documents it::

    async def can_use_tool(tool_name, tool_input, ctx):
        if tool_name == "delete_account":
            return PermissionResultDeny(message="needs a human")
        return PermissionResultAllow()

...let the deletion through. Two independent reasons, both fail-open:

1. The resolver passed the whole ``Tool`` object as the first argument, so
   ``tool_name == "delete_account"`` was never true and the policy fell through
   to allow.
2. On the dict-options path, ``can_use_tool`` was swept into ``Agent.extra`` and
   ``permission_mode`` was merely "consumed" — neither built a
   ``PermissionContext``, so the agent ran with ``permissions=None`` and no
   gating at all. ``"bypass"`` appeared to work only because *nothing* was
   guarded.

A guardrail that looks configured and isn't is worse than no guardrail, so both
are pinned here.
"""

from __future__ import annotations

import anyio

from mantis_agent import (
    Agent,
    MantisAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    Usage,
    UserMessage,
    query,
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
from mantis_agent.providers.mock import MockProvider
from mantis_agent.types import TextBlock, ToolUseBlock

RAN: list[str] = []


@tool
async def delete_account(user_id: str) -> str:
    """Permanently delete a user account."""
    RAN.append(user_id)          # must stay empty when the policy denies
    return f"deleted {user_id}"


def _script() -> tuple[list, list]:
    """Turn 1 asks for delete_account; turn 2 gives up in words."""

    turn1 = [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(index=0, block=ToolUseBlock(id="c1", name="delete_account", input={})),
        ContentBlockDelta(index=0, delta=InputJsonDelta(partial_json='{"user_id": "u-42"}')),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=5)),
        MessageStop(),
    ]
    turn2 = [
        MessageStart(message_id="m2", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="I can't do that.")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=12, output_tokens=4)),
        MessageStop(),
    ]
    return turn1, turn2


class _TwoTurnMock(MockProvider):
    def __init__(self):
        super().__init__()
        self._turn = 0

    async def stream(self, **kw):
        turn1, turn2 = _script()
        script = turn1 if self._turn == 0 else turn2
        self._turn += 1
        for ev in script:
            yield ev


def _policy(seen: list):
    async def can_use_tool(tool_name, tool_input, ctx):
        seen.append(tool_name)
        if tool_name == "delete_account":
            return PermissionResultDeny(message="Deleting accounts requires a human.")
        return PermissionResultAllow()

    return can_use_tool


# ---------------------------------------------------------------------------
# The contract: first argument is a name string
# ---------------------------------------------------------------------------


def test_policy_receives_the_tool_name_not_the_tool_object() -> None:
    seen: list = []
    RAN.clear()

    async def main() -> None:
        agent = Agent(
            model="mock-7b",
            provider=_TwoTurnMock(),
            tools=[delete_account],
            max_steps=3,
            include_memory=False,
        )
        from mantis_agent.claude_compat import _name_first_can_use_tool
        from mantis_agent.permissions import PermissionContext

        agent.permissions = PermissionContext(
            mode="default", can_use_tool=_name_first_can_use_tool(_policy(seen))
        )
        await agent.run([UserMessage(content="delete u-42")])
        await agent.aclose()

    anyio.run(main)

    assert seen, "policy was never consulted"
    assert all(isinstance(n, str) for n in seen), f"got non-strings: {seen}"
    assert "delete_account" in seen
    assert RAN == [], "denied tool still executed — guardrail failed open"


# ---------------------------------------------------------------------------
# Both option shapes must wire it up
# ---------------------------------------------------------------------------


def test_typed_options_enforce_the_denial() -> None:
    seen: list = []
    RAN.clear()

    async def main() -> None:
        options = MantisAgentOptions(
            model="mock-7b",
            tools=[delete_account],
            can_use_tool=_policy(seen),
            max_turns=3,
            include_memory=False,
        )
        wire = options.to_query_options()
        wire["provider"] = _TwoTurnMock()
        async for _ in query(prompt="delete u-42", options=wire):
            pass

    anyio.run(main)

    assert all(isinstance(n, str) for n in seen), f"got non-strings: {seen}"
    assert RAN == [], "denied tool still executed on the typed path"


def test_dict_options_enforce_the_denial() -> None:
    """The dict path built no PermissionContext at all — every call was allowed."""

    seen: list = []
    RAN.clear()

    async def main() -> None:
        async for _ in query(
            prompt="delete u-42",
            options={
                "model": "mock-7b",
                "provider": _TwoTurnMock(),
                "tools": [delete_account],
                "can_use_tool": _policy(seen),
                "max_turns": 3,
                "include_memory": False,
            },
        ):
            pass

    anyio.run(main)

    assert seen, "dict-path policy was never consulted"
    assert all(isinstance(n, str) for n in seen), f"got non-strings: {seen}"
    assert RAN == [], "denied tool still executed on the dict path"


def test_dict_options_permission_mode_builds_a_context() -> None:
    """``permission_mode`` alone must produce a real PermissionContext, so
    "default" actually gates instead of silently allowing everything."""

    from mantis_agent.query import _agent_from_options

    agent = _agent_from_options(
        {"model": "mock", "backend": "mock", "permission_mode": "default"}
    )
    assert agent.permissions is not None
    assert agent.permissions.mode == "default"


def test_no_permission_settings_leaves_permissions_unset() -> None:
    """Additive change: an agent that asked for nothing keeps the old shape."""

    from mantis_agent.query import _agent_from_options

    agent = _agent_from_options({"model": "mock", "backend": "mock"})
    assert agent.permissions is None
