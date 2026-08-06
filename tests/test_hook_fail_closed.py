"""A crashing *blocking* hook must be able to deny rather than fail open.

``HookDispatcher.dispatch`` swallows every hook exception. For observability
events (``PostToolUse`` & friends) that is exactly right — a broken dashboard
hook must never take the agent down. But for ``PreToolUse`` and the other
veto-carrying events the same behaviour is a security hole: a guard hook whose
entire job is to *deny* a dangerous tool call fails OPEN the moment it trips
over an input it did not anticipate, and the unguarded call proceeds.

These tests pin the two-mode contract:
  * ``fail_closed=True``  → a raising blocking hook denies the operation.
  * ``fail_closed=False`` → today's behaviour (proceed) plus a loud WARNING.
  * non-blocking events   → never denied, in either mode.

The second half drives the SHIPPED path — ``Agent(...)`` → ``_preflight_call``
— because a dispatcher flag nobody can reach from the public API fixes nothing.
It also pins that a *sync* hook (a plain ``def`` returning a ``HookResult``)
is honoured rather than being converted into a denial by the fail-closed path.
"""

from __future__ import annotations

import logging

import anyio
import pytest

from mantis_agent.agent import Agent
from mantis_agent.hooks import (
    BLOCKING_EVENTS,
    HookContext,
    HookDispatcher,
    HookResult,
    Hooks,
)
from mantis_agent.providers.mock import MockProvider
from mantis_agent.tools import ToolRegistry, tool
from mantis_agent.types import ToolResultBlock, ToolUseBlock


# --- helpers ---------------------------------------------------------------


async def _boom(ctx: HookContext) -> HookResult | None:
    """A guard hook that crashes on unexpected input."""
    raise ValueError("unexpected tool input")


async def _deny(ctx: HookContext) -> HookResult | None:
    return HookResult(block=True, note="nope")


async def _add_x(ctx: HookContext) -> HookResult | None:
    return HookResult(mutated_input={**(ctx.input or {}), "x": 1})


async def _add_y(ctx: HookContext) -> HookResult | None:
    return HookResult(mutated_input={**(ctx.input or {}), "y": 2})


def _sync_deny(ctx: HookContext) -> HookResult | None:
    """A *sync* guard — a plain ``def``, not ``async def``."""
    return HookResult(block=True, note="sync nope")


def _sync_noop(ctx: HookContext) -> HookResult | None:
    return None


def _sync_boom(ctx: HookContext) -> HookResult | None:
    raise ValueError("unexpected tool input")


def _run(dispatcher: HookDispatcher, event: str, ctx: HookContext) -> HookResult:
    return anyio.run(lambda: dispatcher.dispatch(event, ctx))


# --- the defect ------------------------------------------------------------


def test_crashing_pre_tool_use_blocks_when_fail_closed() -> None:
    d = HookDispatcher(Hooks(pre_tool_use=_boom), fail_closed=True)
    res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
    assert res.block is True
    assert res.note == "hook PreToolUse failed: ValueError; failing closed"


def test_crashing_pre_tool_use_proceeds_but_warns_when_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = HookDispatcher(Hooks(pre_tool_use=_boom))
    assert d.fail_closed is False  # default preserved for back-compat
    with caplog.at_level(logging.WARNING, logger="mantis_agent.hooks"):
        res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
    assert res.block is False  # unchanged behaviour this release
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a failing blocking hook must warn loudly"
    text = warnings[0].getMessage()
    assert "_boom" in text  # names the offending hook
    assert "fail" in text.lower()


def test_crashing_post_tool_use_never_blocks(caplog: pytest.LogCaptureFixture) -> None:
    for fail_closed in (False, True):
        d = HookDispatcher(Hooks(post_tool_use=_boom), fail_closed=fail_closed)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="mantis_agent.hooks"):
            res = _run(d, "PostToolUse", HookContext(event="PostToolUse"))
        assert res.block is False, f"observability event denied (fail_closed={fail_closed})"
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_env_override_forces_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_HOOKS_FAIL_CLOSED", "1")
    d = HookDispatcher(Hooks(pre_tool_use=_boom))  # constructor says open…
    res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
    assert res.block is True  # …env wins


def test_env_override_off_leaves_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_HOOKS_FAIL_CLOSED", "0")
    d = HookDispatcher(Hooks(pre_tool_use=_boom))
    res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
    assert res.block is False


def test_env_override_does_not_downgrade_explicit_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env var is an escalation switch only — it never turns safety off."""
    monkeypatch.setenv("MANTIS_HOOKS_FAIL_CLOSED", "0")
    d = HookDispatcher(Hooks(pre_tool_use=_boom), fail_closed=True)
    res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
    assert res.block is True


# --- regressions: healthy hooks unchanged ----------------------------------


def test_healthy_blocking_hook_still_blocks() -> None:
    for fail_closed in (False, True):
        d = HookDispatcher(Hooks(pre_tool_use=_deny), fail_closed=fail_closed)
        res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
        assert res.block is True
        assert res.note == "nope"


def test_mutation_chaining_still_works() -> None:
    d = HookDispatcher(Hooks(pre_tool_use=[_add_x, _add_y]), fail_closed=True)
    res = _run(d, "PreToolUse", HookContext(event="PreToolUse", input={"a": 0}))
    assert res.block is False
    assert res.mutated_input == {"a": 0, "x": 1, "y": 2}


def test_crash_mid_chain_fails_closed_before_later_hooks_run() -> None:
    """A crash denies immediately; later hooks in the chain never see it."""
    seen: list[str] = []

    async def later(ctx: HookContext) -> HookResult | None:
        seen.append("later")
        return None

    d = HookDispatcher(Hooks(pre_tool_use=[_boom, later]), fail_closed=True)
    res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
    assert res.block is True
    assert seen == []


def test_sync_blocking_hook_still_blocks_in_both_modes() -> None:
    """A plain ``def`` hook is plausible user code: honour its verdict.

    Under fail-closed, ``await fn(ctx)`` on a sync hook raises TypeError; if
    that counts as a hook failure the *result* is coincidentally right here
    (block) but for the wrong reason — and the note would be wrong. Pin both.
    """
    for fail_closed in (False, True):
        d = HookDispatcher(Hooks(pre_tool_use=_sync_deny), fail_closed=fail_closed)
        res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
        assert res.block is True, f"sync hook ignored (fail_closed={fail_closed})"
        assert res.note == "sync nope", f"denied by TypeError, not by the hook: {res.note}"


def test_sync_noop_hook_is_a_noop_in_both_modes() -> None:
    """The regression: a sync hook returning ``None`` must not become a denial."""
    for fail_closed in (False, True):
        seen: list[str] = []

        def noop(ctx: HookContext) -> HookResult | None:
            seen.append("ran")
            return None

        d = HookDispatcher(Hooks(pre_tool_use=noop), fail_closed=fail_closed)
        res = _run(d, "PreToolUse", HookContext(event="PreToolUse"))
        assert seen == ["ran"]
        assert res.block is False, f"sync no-op hook DENIED (fail_closed={fail_closed})"


def test_sync_mutating_hook_chains_in_both_modes() -> None:
    def add_z(ctx: HookContext) -> HookResult | None:
        return HookResult(mutated_input={**(ctx.input or {}), "z": 3})

    for fail_closed in (False, True):
        d = HookDispatcher(Hooks(pre_tool_use=[add_z, _add_x]), fail_closed=fail_closed)
        res = _run(d, "PreToolUse", HookContext(event="PreToolUse", input={"a": 0}))
        assert res.block is False
        assert res.mutated_input == {"a": 0, "z": 3, "x": 1}


def test_blocking_events_membership() -> None:
    assert "PreToolUse" in BLOCKING_EVENTS
    assert "UserPromptSubmit" in BLOCKING_EVENTS
    assert "PermissionRequest" in BLOCKING_EVENTS
    assert "SubagentStop" in BLOCKING_EVENTS
    assert "Stop" in BLOCKING_EVENTS
    assert "PreCompact" in BLOCKING_EVENTS
    assert "InstructionsLoaded" in BLOCKING_EVENTS
    # Observability-only events must stay out of the set.
    assert "PostToolUse" not in BLOCKING_EVENTS
    assert "PostToolUseFailure" not in BLOCKING_EVENTS
    assert "Notification" not in BLOCKING_EVENTS


# --- end to end: the SHIPPED path ------------------------------------------
#
# Everything above exercises ``HookDispatcher`` directly. That is not the API
# users have. These tests go through ``Agent(...)`` and the real
# ``_preflight_call`` gate the agent loop runs before every tool dispatch, so a
# flag that exists but is unreachable from ``Agent`` fails here.


def _agent(hooks: Hooks, **kw) -> Agent:
    @tool(name="spin")
    async def spin(x: int) -> str:  # pragma: no cover - never actually runs
        return "ok"

    reg = ToolRegistry()
    reg.add(spin)
    return Agent(
        model="mock",
        provider=MockProvider(),
        tools=reg,
        hooks=hooks,
        include_memory=False,
        include_env=False,
        **kw,
    )


def _preflight(ag: Agent) -> ToolResultBlock | None:
    """Run the real pre-dispatch gate for one ``spin`` call."""

    async def go() -> ToolResultBlock | None:
        call = ToolUseBlock(id="c1", name="spin", input={"x": 1})
        _, sc = await ag._preflight_call(call, [])
        await ag.aclose()
        return sc

    return anyio.run(go)


def test_agent_defaults_to_fail_open_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Documented current behaviour: default config lets the call through…"""
    ag = _agent(Hooks(pre_tool_use=_boom))
    assert ag.hooks_fail_closed is False
    with caplog.at_level(logging.WARNING, logger="mantis_agent.hooks"):
        sc = _preflight(ag)
    assert sc is None, "crashing PreToolUse hook should fail OPEN by default"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "…but it must say so loudly"
    assert "_boom" in warnings[0].getMessage()


def test_agent_hooks_fail_closed_blocks_crashing_pre_tool_use() -> None:
    """The opt-in the previous fix never wired: reachable from Agent()."""
    ag = _agent(Hooks(pre_tool_use=_boom), hooks_fail_closed=True)
    sc = _preflight(ag)
    assert sc is not None, "crashing PreToolUse hook FAILED OPEN through the real API"
    assert sc.is_error is True
    assert "blocked by hook" in sc.content
    assert "failing closed" in sc.content


def test_agent_env_override_blocks_with_zero_code_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MANTIS_HOOKS_FAIL_CLOSED", "1")
    ag = _agent(Hooks(pre_tool_use=_boom))  # nothing opted in in code…
    assert ag.hooks_fail_closed is True  # …env promotes it at construction
    sc = _preflight(ag)
    assert sc is not None and sc.is_error is True


def test_agent_env_override_does_not_downgrade_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MANTIS_HOOKS_FAIL_CLOSED", "0")
    ag = _agent(Hooks(pre_tool_use=_boom), hooks_fail_closed=True)
    sc = _preflight(ag)
    assert sc is not None and sc.is_error is True


def test_agent_sync_hook_blocks_in_both_modes() -> None:
    for fail_closed in (False, True):
        ag = _agent(Hooks(pre_tool_use=_sync_deny), hooks_fail_closed=fail_closed)
        sc = _preflight(ag)
        assert sc is not None, f"sync hook's block ignored (fail_closed={fail_closed})"
        assert "sync nope" in sc.content, (
            f"denied by an await TypeError, not by the hook (fail_closed={fail_closed}): "
            f"{sc.content}"
        )


def test_agent_sync_noop_hook_does_not_block_in_either_mode() -> None:
    for fail_closed in (False, True):
        ag = _agent(Hooks(pre_tool_use=_sync_noop), hooks_fail_closed=fail_closed)
        sc = _preflight(ag)
        assert sc is None, f"sync no-op hook blocked the call (fail_closed={fail_closed})"


def test_agent_sync_crashing_hook_follows_the_mode() -> None:
    """Sync-ness must not change the *policy*: a real crash still fails
    open by default and closed when opted in."""
    assert _preflight(_agent(Hooks(pre_tool_use=_sync_boom))) is None
    sc = _preflight(_agent(Hooks(pre_tool_use=_sync_boom), hooks_fail_closed=True))
    assert sc is not None and "failing closed" in sc.content


def test_agent_crashing_post_tool_use_never_blocks() -> None:
    """Observability events stay fail-open under the agent's dispatcher too."""
    for fail_closed in (False, True):
        ag = _agent(Hooks(post_tool_use=_boom), hooks_fail_closed=fail_closed)
        assert ag._dispatcher is not None

        async def go() -> HookResult:
            res = await ag._dispatcher.dispatch(
                "PostToolUse", HookContext(event="PostToolUse")
            )
            await ag.aclose()
            return res

        assert anyio.run(go).block is False, (
            f"observability event denied (fail_closed={fail_closed})"
        )


def test_agent_sync_post_tool_use_hook_runs_and_never_blocks() -> None:
    for fail_closed in (False, True):
        seen: list[str] = []

        def observer(ctx: HookContext) -> HookResult | None:
            seen.append(ctx.event)
            return None

        ag = _agent(Hooks(post_tool_use=observer), hooks_fail_closed=fail_closed)
        assert ag._dispatcher is not None

        async def go() -> HookResult:
            res = await ag._dispatcher.dispatch(
                "PostToolUse", HookContext(event="PostToolUse")
            )
            await ag.aclose()
            return res

        assert anyio.run(go).block is False
        assert seen == ["PostToolUse"]
