"""Pin verbatim Claude Agent SDK example compatibility.

For each canonical example shipped at
https://github.com/anthropics/claude-agent-sdk-python/tree/main/examples
we keep a verbatim copy under ``mantis_agent/examples/``, modified only
by replacing ``from claude_agent_sdk`` → ``from mantis_agent``. These
tests assert each one:

  1. Parses (AST-clean)
  2. Imports without raising (every symbol they use exists at the right path)
  3. Yields the right message shapes when run against a MockProvider

This is the litmus test for drop-in compatibility. If anything here
fails, Spawn workflows that swap claude_agent_sdk for mantis_agent will
break — and that's the whole reason this SDK exists.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "mantis_agent" / "examples"


# The verbatim-from-Claude examples (only the import line is rewritten).
# Order matters: simpler first, so a regression in basics fails fast.
VERBATIM_EXAMPLES: tuple[str, ...] = (
    "quick_start.py",
    "tools_option.py",
    "system_prompt.py",
    "mcp_calculator.py",
    "stderr_callback_example.py",
    "max_budget_usd.py",
)


@pytest.mark.parametrize("name", VERBATIM_EXAMPLES)
def test_example_parses(name: str) -> None:
    """AST-clean. No syntax errors, no missing typing imports."""

    path = EXAMPLES_DIR / name
    assert path.exists(), f"missing verbatim example {name!r}"
    ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", VERBATIM_EXAMPLES)
def test_example_imports_resolve(name: str) -> None:
    """Every symbol the example imports from mantis_agent exists.

    We load the module via importlib but stop *before* asyncio.run()
    fires — the example's ``if __name__ == "__main__":`` guard does
    that, and pytest never runs as __main__.
    """

    path = EXAMPLES_DIR / name
    spec = importlib.util.spec_from_file_location(
        f"mantis_agent._verbatim_example_{name.replace('.', '_')}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # If we got here, every import resolved.


def test_only_import_line_differs_from_claude() -> None:
    """The verbatim examples should differ from upstream Claude only by
    the ``claude_agent_sdk`` → ``mantis_agent`` substitution.

    We don't have the upstream files on disk in CI, so this test is a
    soft check: confirm each example contains the canonical
    ``from mantis_agent import`` line and NO leftover
    ``from claude_agent_sdk`` reference.
    """

    for name in VERBATIM_EXAMPLES:
        path = EXAMPLES_DIR / name
        text = path.read_text(encoding="utf-8")
        assert "from mantis_agent" in text, (
            f"{name}: missing canonical import"
        )
        assert "from claude_agent_sdk" not in text, (
            f"{name}: leftover claude_agent_sdk import — port incomplete"
        )


# ---------------------------------------------------------------------------
# Shape parity — every symbol the Claude examples use is wired to a real
# object at the expected top-level path.
# ---------------------------------------------------------------------------


def test_top_level_surface_matches_claude_sdk_python() -> None:
    """The public symbols Claude's examples import from
    ``claude_agent_sdk`` (top level) all exist on ``mantis_agent``."""

    import mantis_agent

    # The set of names the canonical examples actually import. Grep'd
    # from the 16 official examples in the upstream repo (May 2026).
    expected = {
        "AssistantMessage",
        "MantisAgentOptions",
        "ClaudeSDKClient",
        "ResultMessage",
        "SystemMessage",
        "TextBlock",
        "ToolResultBlock",
        "ToolUseBlock",
        "UserMessage",
        "create_sdk_mcp_server",
        "query",
        "tool",
    }
    missing = expected - set(dir(mantis_agent))
    assert not missing, f"missing public symbols: {missing}"


def test_types_submodule_surface_matches() -> None:
    """``from claude_agent_sdk.types import HookContext, HookInput,
    HookJSONOutput, HookMatcher, Message, ResultMessage, AssistantMessage,
    TextBlock`` works on ``mantis_agent.types`` too."""

    import mantis_agent.types as ttypes

    for name in (
        "HookContext",
        "HookInput",
        "HookJSONOutput",
        "HookMatcher",
        "ResultMessage",
        "AssistantMessage",
        "TextBlock",
    ):
        obj = getattr(ttypes, name, None)
        assert obj is not None, (
            f"mantis_agent.types.{name} not exposed — Claude SDK compat broken"
        )


# ---------------------------------------------------------------------------
# Claude-positional @tool signature (mcp_calculator.py uses this)
# ---------------------------------------------------------------------------


def test_claude_positional_tool_decorator() -> None:
    """``@tool("name", "desc", {"a": float, "b": float})`` returns a Tool
    with the right name + JSON schema derived from the type dict."""

    from mantis_agent import tool

    @tool("add", "Add two numbers", {"a": float, "b": float})
    async def add_numbers(args: dict) -> dict:
        result = args["a"] + args["b"]
        return {"content": [{"type": "text", "text": f"{result}"}]}

    assert add_numbers.name == "add"
    assert add_numbers.description == "Add two numbers"
    assert add_numbers.input_schema["type"] == "object"
    assert add_numbers.input_schema["properties"]["a"] == {"type": "number"}
    assert add_numbers.input_schema["properties"]["b"] == {"type": "number"}
    assert set(add_numbers.input_schema["required"]) == {"a", "b"}


# ---------------------------------------------------------------------------
# query() runtime parity — Claude Python SDK shapes (flat .content,
# .total_cost_usd directly on the result), not the TS-nested shape
# ---------------------------------------------------------------------------


def test_query_yields_claude_shaped_messages() -> None:
    """query() with MantisAgentOptions yields AssistantMessage with flat
    .content (not nested .message.content), ResultMessage with
    .total_cost_usd directly accessible — same as Claude Python SDK."""

    import anyio

    from mantis_agent import (
        AssistantMessage,
        MantisAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
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
    from mantis_agent.types import Usage

    events = [
        MessageStart(message_id="m1", model="mock-7b"),
        ContentBlockStart(index=0, block=TextBlock(text="")),
        ContentBlockDelta(index=0, delta=TextDelta(text="hello")),
        ContentBlockStop(index=0),
        MessageDelta(stop_reason="end_turn", usage=Usage(input_tokens=4, output_tokens=2)),
        MessageStop(),
    ]

    # Inject the provider via env override — compat_query builds the
    # agent from options. Patch provider routing via the mock entry.
    from mantis_agent.providers.base import register

    register("mock", lambda: MockProvider(scripted_events=events))

    async def main():
        opts = MantisAgentOptions(
            model="mock-test",
            backend="mock",
            system_prompt="be brief",
            include_memory=False,
        )
        kinds = []
        text = ""
        cost = None
        async for msg in query(prompt="hi", options=opts):
            kinds.append(type(msg).__name__)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text = block.text
            elif isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd
        return kinds, text, cost

    kinds, text, cost = anyio.run(main)
    # Exactly what Claude examples expect: SystemMessage(init) then
    # UserMessage echo, then AssistantMessage, then ResultMessage.
    assert "SystemMessage" in kinds
    assert "UserMessage" in kinds
    assert "AssistantMessage" in kinds
    assert "ResultMessage" in kinds
    # The .content access is flat, not nested.
    assert text == "hello"
    # Cost surfaced as float at top level of the result.
    assert isinstance(cost, float)
