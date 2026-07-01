"""Salvage Llama-style <function=NAME>{json}</function> tool calls, with name
resolution for Claude-name/case drift."""

from __future__ import annotations

from mantis_agent.agent import _salvage_text_tool_calls
from mantis_agent.tools import ToolRegistry, tool


@tool
async def read_file(path: str) -> str:
    return "r"


@tool
async def bash(command: str) -> str:
    return "b"


def _reg() -> ToolRegistry:
    r = ToolRegistry()
    r.add(read_file, bash)
    return r


def test_function_equals_tag() -> None:
    c = _salvage_text_tool_calls('<function=read_file>{"path": "config.py"}</function>', _reg())
    assert len(c) == 1 and c[0].name == "read_file" and c[0].input == {"path": "config.py"}


def test_function_tag_with_drifted_name() -> None:
    c = _salvage_text_tool_calls('<function=Read>{"path": "x.py"}</function>', _reg())
    assert c[0].name == "read_file"                 # Read → read_file


def test_function_call_name_attr() -> None:
    c = _salvage_text_tool_calls('<function_call name="bash">{"command": "ls"}</function_call>', _reg())
    assert c[0].name == "bash" and c[0].input == {"command": "ls"}


def test_json_path_also_resolves_names() -> None:
    c = _salvage_text_tool_calls('{"name": "Bash", "arguments": {"command": "pwd"}}', _reg())
    assert c[0].name == "bash"


def test_unknown_tool_in_tag_ignored() -> None:
    assert _salvage_text_tool_calls('<function=made_up>{}</function>', _reg()) == []


def test_multiple_function_tags() -> None:
    text = ('<function=bash>{"command": "ls"}</function>\n'
            '<function=read_file>{"path": "a.py"}</function>')
    c = _salvage_text_tool_calls(text, _reg())
    assert [x.name for x in c] == ["bash", "read_file"]


def test_plain_prose_salvages_nothing() -> None:
    assert _salvage_text_tool_calls("I'll read the config file next.", _reg()) == []
