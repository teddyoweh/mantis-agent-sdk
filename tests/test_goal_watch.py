"""/goal autopilot prompts + contract, /watch parsing reuse."""

from __future__ import annotations

from mantis_agent.tui import (
    GOAL_BLOCKED_MARKER,
    GOAL_COMPLETE_MARKER,
    GOAL_MAX_CYCLES,
    goal_continue_prompt,
    goal_kickoff_prompt,
    goal_reflect_prompt,
    goal_verify_prompt,
)


def test_goal_prompts_carry_the_contract() -> None:
    g = "ship the /export command"
    k = goal_kickoff_prompt(g)
    assert g in k and "todo_write" in k and GOAL_BLOCKED_MARKER in k
    c = goal_continue_prompt(g, 3, GOAL_MAX_CYCLES)
    assert g in c and "3/" in c and GOAL_BLOCKED_MARKER in c
    v = goal_verify_prompt(g)
    assert g in v and "ADVERSARIALLY" in v and GOAL_COMPLETE_MARKER in v
    r = goal_reflect_prompt(g)
    assert g in r and "remember" in r and "NON-OBVIOUS" in r


def test_goal_markers_are_distinct_and_uppercase() -> None:
    # the engine detects these via substring — they must never collide or
    # accidentally appear in normal prose
    assert GOAL_COMPLETE_MARKER != GOAL_BLOCKED_MARKER
    assert GOAL_COMPLETE_MARKER.isupper() and GOAL_BLOCKED_MARKER.isupper()
    # verify prompt must not contain the BLOCKED marker (false-positive risk)
    assert GOAL_BLOCKED_MARKER not in goal_verify_prompt("x")
    # reflect prompt must not re-trigger completion detection... (engine moves
    # to reflect phase BEFORE firing, so containing the marker would be fine —
    # but keep it out anyway for greppability)
    assert GOAL_COMPLETE_MARKER not in goal_reflect_prompt("x")


def test_watch_arg_parsing_via_loop_parser() -> None:
    from mantis_agent.tui import parse_loop_command
    # explicit interval
    assert parse_loop_command("10s pytest -q") == (10.0, "pytest -q")
    # no interval → parser rejects; the /watch handler falls back to 30s
    assert isinstance(parse_loop_command("pytest -q tests/"), str)


# -- small-model robustness ---------------------------------------------------------


def test_unknown_tool_message_suggests_and_guides() -> None:
    from mantis_agent.builtin_tools import CODING_TOOLS
    from mantis_agent.tools import ToolRegistry, unknown_tool_message
    reg = ToolRegistry()
    reg.add(*CODING_TOOLS)
    msg = unknown_tool_message("bahs", reg)
    assert "does not exist" in msg and "'bash'" in msg          # close match named
    assert "answer directly in text" in msg                      # the escape hatch
    msg2 = unknown_tool_message("zzzzz", reg)
    assert "Did you mean" not in msg2 and "read_file" in msg2    # full list shown


def test_dispatch_unknown_tool_returns_guided_error() -> None:
    import anyio
    from mantis_agent.tools import ToolRegistry, dispatch_tool_calls
    from mantis_agent.types import ToolUseBlock
    from mantis_agent.builtin_tools import CODING_TOOLS
    reg = ToolRegistry()
    reg.add(*CODING_TOOLS)
    res = anyio.run(lambda: dispatch_tool_calls(
        reg, [ToolUseBlock(id="x", name="model", input={})]))
    assert res[0].is_error and "does not exist" in res[0].content


def test_env_block_carries_model_identity() -> None:
    from mantis_agent.system_reminder import build_env_context_block
    block = build_env_context_block(model="qwen2.5-coder:7b", backend="ollama",
                                    dir_entries=[], is_git=False)
    assert "You are the model: qwen2.5-coder:7b (served via ollama)" in block
    plain = build_env_context_block(dir_entries=[], is_git=False)
    assert "You are the model" not in plain     # only when known


def test_small_model_classification() -> None:
    from mantis_agent.tui import is_small_model
    assert is_small_model("qwen2.5-coder:7b")
    assert is_small_model("llama3.2:3b")
    assert is_small_model("qwen2.5:1.5b")
    assert is_small_model("mistral:7b")
    assert not is_small_model("gpt-5.4")
    assert not is_small_model("claude-opus-4-8")
    assert not is_small_model("gpt-oss:20b")            # 20B > 14B threshold
    assert not is_small_model("deepseek-ai/DeepSeek-V3")


def test_small_model_gets_slim_tool_belt() -> None:
    from mantis_agent.tui import MantisTUI
    t = MantisTUI(model="qwen2.5-coder:7b", backend="http://localhost:11434",
                  api_key=None, system=None, max_tokens=1, temperature=None, max_turns=1)
    names = {x.name for x in t._build_agent().tools}
    assert names == {"bash", "read_file", "write_file", "edit_file", "ls", "glob",
                     "grep", "web_search", "web_fetch", "todo_write"}
    # big model keeps the full belt
    t2 = MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                   system=None, max_tokens=1, temperature=None, max_turns=1)
    names2 = {x.name for x in t2._build_agent().tools}
    assert {"task", "pair", "ask_user_question", "monitor", "lsp"} <= names2
