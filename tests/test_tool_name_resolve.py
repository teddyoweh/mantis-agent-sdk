"""ToolRegistry.resolve — case/underscore drift + Claude-Code-name aliases."""

from __future__ import annotations

import pytest

from mantis_agent.tools import ToolRegistry, tool


@tool
async def read_file(path: str) -> str:
    return "r"


@tool
async def bash(command: str) -> str:
    return "b"


@tool
async def edit_file(path: str, old_string: str = "", new_string: str = "") -> str:
    return "e"


@tool
async def grep(pattern: str) -> str:
    return "g"


def _reg() -> ToolRegistry:
    r = ToolRegistry()
    r.add(read_file, bash, edit_file, grep)
    return r


def test_exact_match() -> None:
    assert _reg().resolve("read_file").name == "read_file"


@pytest.mark.parametrize("name,expected", [
    ("Read", "read_file"), ("READ", "read_file"), ("read", "read_file"), ("view", "read_file"),
    ("Bash", "bash"), ("shell", "bash"), ("execute", "bash"), ("run_command", "bash"),
    ("Edit", "edit_file"), ("str_replace", "edit_file"), ("StrReplaceEditor", "edit_file"),
    ("Grep", "grep"), ("search", "grep"), ("ripgrep", "grep"),
])
def test_aliases_and_case(name: str, expected: str) -> None:
    assert _reg().resolve(name).name == expected


def test_unknown_returns_none() -> None:
    assert _reg().resolve("totally_made_up_tool") is None
    assert _reg().resolve("") is None


def test_get_stays_exact() -> None:
    # get() must NOT fuzzy-match (internal exact checks rely on it)
    r = _reg()
    assert r.get("Read") is None
    assert r.get("read_file").name == "read_file"


def test_alias_only_if_target_registered() -> None:
    # "search" aliases to grep — but if grep isn't registered, no match
    r = ToolRegistry()
    r.add(read_file, bash)
    assert r.resolve("search") is None            # grep not present
    assert r.resolve("shell").name == "bash"      # bash present


def test_custom_tool_case_insensitive() -> None:
    @tool
    async def my_custom(x: int) -> str:
        return "c"
    r = ToolRegistry()
    r.add(my_custom)
    assert r.resolve("MyCustom").name == "my_custom"    # case/underscore drift
    assert r.resolve("mycustom").name == "my_custom"
