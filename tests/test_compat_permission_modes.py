"""claude-agent-sdk's permission-mode names must mean what they mean there.
``bypassPermissions`` used to reach the engine unmapped, matching no check:
headless, the danger list still denied ``rm -f`` in a scratch dir. ``plan``
matched nothing either and fell through to Allow — writes included."""

from __future__ import annotations

import anyio

from mantis_agent import MantisAgentOptions
from mantis_agent.builtin_tools import bash, read_file, write_file
from mantis_agent.permissions import Allow, Deny, PermissionContext, check_permission


def _ctx(mode: str) -> PermissionContext:
    opts = MantisAgentOptions(model="mock", permission_mode=mode).to_query_options()
    return opts["permissions"]


def _decide(ctx: PermissionContext, tool, inp: dict):
    return anyio.run(check_permission, tool, inp, ctx)


def test_bypass_permissions_is_bypass() -> None:
    ctx = _ctx("bypassPermissions")
    assert ctx.mode == "bypass"
    assert isinstance(_decide(ctx, bash, {"command": "rm -f chk-*.png"}), Allow)


def test_plan_mode_reads_but_never_writes() -> None:
    ctx = _ctx("plan")
    assert isinstance(_decide(ctx, read_file, {"path": "a.py"}), Allow)
    assert isinstance(_decide(ctx, bash, {"command": "ls"}), Allow)
    assert isinstance(_decide(ctx, write_file, {"path": "a.py", "content": "x"}), Deny)
    assert isinstance(_decide(ctx, bash, {"command": "touch a.py"}), Deny)


def test_mantis_names_still_work() -> None:
    assert _ctx("bypass").mode == "bypass"
    assert _ctx("acceptEdits").mode == "acceptEdits"
