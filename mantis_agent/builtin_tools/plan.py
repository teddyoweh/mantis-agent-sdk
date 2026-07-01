"""``exit_plan_mode`` — the plan-mode approval handoff (Claude Code's
ExitPlanMode). In plan mode the agent researches read-only; when it has a plan it
calls this tool to present the plan and ask the user to approve. On approval the
harness lifts plan mode so the agent can start editing; otherwise the agent keeps
refining. The interactive presentation is provided by the terminal via a bound
``presenter``; headless runs just proceed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..tools import Tool, tool

# presenter(plan) -> a result string the model reads (approved / keep-planning).
Presenter = Callable[[str], Awaitable[str]]


def make_exit_plan_mode(presenter: Presenter | None) -> Tool:
    """Build the ``exit_plan_mode`` tool bound to an interactive ``presenter``."""

    @tool(name="exit_plan_mode", is_read_only=True)
    async def exit_plan_mode(plan: str) -> str:
        """Present your implementation plan and ask the user to approve it, then
        (on approval) leave plan mode and start making the changes.

        Call this ONLY when you are in plan mode and have finished
        researching — pass the FULL plan as markdown (concise steps, not prose).
        Do not ask about the plan with other tools; this is the approval path.
        If the user approves, plan mode is lifted and you may edit; if not, stay
        in plan mode and revise based on their feedback."""
        if not (plan or "").strip():
            return "No plan provided. Write the plan first, then call exit_plan_mode."
        if presenter is None:
            return "Non-interactive session — no approval needed; proceed with the plan."
        return await presenter(plan)

    return exit_plan_mode
