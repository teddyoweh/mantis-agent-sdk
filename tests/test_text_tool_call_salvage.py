"""Regression tests for recovering tool calls that local models emit as TEXT.

Local OSS models served via Ollama (qwen2.5-coder, llama3.x, …) routinely
"call" a tool by printing it — a JSON object or a shell code fence — instead of
using the structured tool-call channel. ``_salvage_text_tool_calls`` turns those
back into real ToolUseBlocks so the agent loop actually runs the command.

Also pins the capability routing that decides native-tools vs the fragile
text-parse path for Ollama tag-form model names (``llama3.2:latest`` etc.).
"""

from __future__ import annotations

from mantis_agent.agent import _salvage_text_tool_calls
from mantis_agent.builtin_tools import CODING_TOOLS
from mantis_agent.capabilities import lookup_model
from mantis_agent.tools import ToolRegistry


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.add(*CODING_TOOLS)
    return reg


def test_salvage_json_arguments() -> None:
    reg = _registry()
    calls = _salvage_text_tool_calls(
        '{"name": "bash", "arguments": {"command": "lsof -i :3000"}}', reg
    )
    assert [(c.name, c.input) for c in calls] == [
        ("bash", {"command": "lsof -i :3000"})
    ]


def test_salvage_json_parameters_with_unescaped_quotes() -> None:
    # llama3.x shape: "parameters" key + unescaped inner quotes in the value.
    reg = _registry()
    calls = _salvage_text_tool_calls(
        '{"name":"bash","parameters":{"command":"date +"%H:%M:%S""}}', reg
    )
    assert len(calls) == 1
    assert calls[0].name == "bash"
    assert "date" in calls[0].input["command"]


def test_salvage_shell_code_fence() -> None:
    reg = _registry()
    calls = _salvage_text_tool_calls("```bash\ndate\n```\nThe time is...", reg)
    assert [(c.name, c.input) for c in calls] == [("bash", {"command": "date"})]


def test_salvage_strips_dollar_prompt() -> None:
    reg = _registry()
    calls = _salvage_text_tool_calls("```sh\n$ ls -la\n```", reg)
    assert calls[0].input["command"] == "ls -la"


def test_salvage_tool_call_tags_with_parameters() -> None:
    reg = _registry()
    calls = _salvage_text_tool_calls(
        '<tool_call>{"name":"ls","parameters":{"path":"."}}</tool_call>', reg
    )
    assert [(c.name, c.input) for c in calls] == [("ls", {"path": "."})]


def test_salvage_json_object_after_prose() -> None:
    # Model narrates, then emits the call — the object must still be recovered.
    reg = _registry()
    calls = _salvage_text_tool_calls(
        'Let me check.\n{"name":"grep","arguments":{"pattern":"def","path":"."}}', reg
    )
    assert [(c.name, c.input) for c in calls] == [
        ("grep", {"pattern": "def", "path": "."})
    ]


def test_salvage_multiline_shell_fence() -> None:
    reg = _registry()
    calls = _salvage_text_tool_calls("```bash\ncd /tmp\nls -la\n```", reg)
    assert calls[0].name == "bash"
    assert calls[0].input["command"] == "cd /tmp\nls -la"


def test_no_salvage_for_plain_prose() -> None:
    reg = _registry()
    assert _salvage_text_tool_calls("The current time is 10:30.", reg) == []


def test_no_salvage_for_non_shell_fence() -> None:
    # A python fence is NOT a command to run — must not be salvaged as bash.
    reg = _registry()
    assert _salvage_text_tool_calls("```python\nprint('hi')\n```", reg) == []


def test_no_salvage_for_unknown_tool_name() -> None:
    reg = _registry()
    assert _salvage_text_tool_calls(
        '{"name": "definitely_not_a_tool", "arguments": {}}', reg
    ) == []


def test_capability_modern_llama_tag_forms_are_native() -> None:
    # The bug: Ollama tag forms didn't match the hyphenated table keys and fell
    # to the llama3 default (no native tools). 3.1/3.2/3.3 DO support them.
    assert lookup_model("llama3.2:latest").supports_native_tools is True
    assert lookup_model("llama3.1:8b").supports_native_tools is True
    assert lookup_model("llama3.3:70b").supports_native_tools is True
    # Plain llama3 (3.0) correctly stays on the non-native path.
    assert lookup_model("llama3:8b").supports_native_tools is False
