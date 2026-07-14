from __future__ import annotations

from mantis_agent.builtin_tools.fs import bash, edit_file, glob, grep, read_file, write_file
from mantis_agent.tui import MantisTUI


def test_default_system_includes_claude_code_style_safety_and_parallel_guidance() -> None:
    tui = MantisTUI(
        model="x",
        backend="http://localhost:11434",
        api_key=None,
        system=None,
        max_tokens=1,
        temperature=None,
        max_turns=1,
    )

    system = tui._default_system()

    assert "All text you output outside tool calls is shown to the user" in system
    assert "<system-reminder> or other tags" in system
    assert "prompt injection" in system
    assert "tools in parallel" in system
    assert "later calls depend on earlier" in system


def test_core_tool_descriptions_steer_models_to_dedicated_tools() -> None:
    assert "read_file (not cat/head/tail)" in bash.description
    assert "don't use echo/printf as communication" in bash.description
    assert "number+tab prefix is not file content" in read_file.description
    assert "write_file replaces the ENTIRE file" in write_file.description
    assert "without read_file's line-number prefix" in edit_file.description
    assert "Use this instead of shell ``find``" in glob.description
    assert "fixed_strings=True" in grep.description
