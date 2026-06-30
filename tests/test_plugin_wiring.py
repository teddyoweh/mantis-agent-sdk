"""``ClaudeAgentOptions(plugins=[Plugin(...)])`` actually wires plugins in.

Previously: ``plugins`` was an accepted field but ignored. Now: each
plugin's ``tools`` merge into the registry, ``system_prompt_addition``
appends to the system prompt, and ``hooks`` (if any) join the active
Hooks instance.
"""

from __future__ import annotations

from mantis_agent import ClaudeAgentOptions, Plugin, tool


@tool
async def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


@tool
async def multiply(a: int, b: int) -> str:
    """Multiply two integers."""
    return str(a * b)


def test_plugin_tools_merge_into_registry() -> None:
    """Tools from each plugin appear in the agent's resolved tool list."""

    opts = ClaudeAgentOptions(
        plugins=[
            Plugin(name="math-pack", tools=[add, multiply]),
        ],
    ).to_query_options()

    tool_names = [t.name for t in opts["tools"]]
    assert "add" in tool_names
    assert "multiply" in tool_names


def test_plugin_tools_combine_with_user_tools() -> None:
    """Plugin tools join the user's own tools — both end up in the registry."""

    @tool
    async def divide(a: int, b: int) -> str:
        return str(a / b)

    opts = ClaudeAgentOptions(
        tools=[divide],
        plugins=[
            Plugin(name="math-pack", tools=[add, multiply]),
        ],
    ).to_query_options()

    tool_names = {t.name for t in opts["tools"]}
    assert tool_names == {"add", "multiply", "divide"}


def test_plugin_system_prompt_addition_appends() -> None:
    """system_prompt_addition appends to the user's system_prompt with a blank line."""

    opts = ClaudeAgentOptions(
        system_prompt="You are an agent.",
        plugins=[
            Plugin(name="terse", system_prompt_addition="Reply in one sentence."),
        ],
    ).to_query_options()

    system = opts["system"]
    assert "You are an agent." in system
    assert "Reply in one sentence." in system
    # The user's prompt comes first; plugin addition follows.
    assert system.index("You are an agent.") < system.index("Reply in one sentence.")


def test_plugin_system_prompt_addition_without_user_system() -> None:
    """When the user passes no system_prompt, plugin additions become the system."""

    opts = ClaudeAgentOptions(
        plugins=[Plugin(name="terse", system_prompt_addition="Be brief.")],
    ).to_query_options()

    assert opts["system"] == "Be brief."


def test_multiple_plugin_additions_join_with_blank_lines() -> None:
    """Two plugins each adding text → both appended, separated by blank lines."""

    opts = ClaudeAgentOptions(
        system_prompt="root",
        plugins=[
            Plugin(name="a", system_prompt_addition="from A"),
            Plugin(name="b", system_prompt_addition="from B"),
        ],
    ).to_query_options()

    assert "root" in opts["system"]
    assert "from A" in opts["system"]
    assert "from B" in opts["system"]


def test_empty_plugins_list_is_noop() -> None:
    """plugins=[] leaves tools / system_prompt untouched."""

    opts = ClaudeAgentOptions(
        system_prompt="hi",
        plugins=[],
    ).to_query_options()

    assert opts["system"] == "hi"
    # No tools key at all when nothing real was passed.
    assert "tools" not in opts


def test_plugin_hooks_merge_into_combined_hooks() -> None:
    """A plugin's hooks dict joins the user's hooks dict, with user hooks
    winning on per-event collision."""

    from mantis_agent import HookMatcher

    async def plugin_pre(*_a, **_kw):
        return {}

    async def user_pre(*_a, **_kw):
        return {}

    opts = ClaudeAgentOptions(
        hooks={"PostToolUse": [HookMatcher(hooks=[user_pre])]},
        plugins=[
            Plugin(
                name="audit",
                hooks={"PreToolUse": [HookMatcher(hooks=[plugin_pre])]},
            ),
        ],
    ).to_query_options()

    hooks = opts["hooks"]
    # Both event handlers wired.
    assert hooks.pre_tool_use is plugin_pre
    assert hooks.post_tool_use is user_pre
