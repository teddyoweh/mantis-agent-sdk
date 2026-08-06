"""A tool call cut off by the output cap must not be reported as bad syntax.

When a model writes a large file, the ``content`` argument can run past
``max_tokens`` and the arguments JSON ends mid-string. Mantis used to answer
that with "not valid JSON — re-issue a well-formed object", which is the wrong
diagnosis and the wrong fix: the model's syntax was fine, and re-issuing the
same oversized call truncates at exactly the same place. Each retry costs a
full generation, so the turn stalls for minutes and never lands the write.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import pytest

from mantis_agent.agent import _looks_truncated
from mantis_agent.types import AssistantMessage, ToolResultBlock, UserMessage


# -- telling "cut off" from "written wrong" ---------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"path": "a.md", "content": "# Title\\n\\nlots of text that runs',
        '{"path": "a.md", "content": "done"',      # string closed, object open
        '{"items": [1, 2,',
        '{"a": {"b": ',
        '{"s": "he said \\"hi',                    # cut inside an escape
        '{"code": "if (x) { y }"',                 # brace inside a string
    ],
)
def test_truncated_prefixes_are_recognised(raw: str) -> None:
    assert _looks_truncated(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1,}',                 # trailing comma — balanced but wrong
        "{a: 1}",                    # unquoted key
        "{'a': 1}",                  # single quotes
        '{"a": 1} extra',            # trailing garbage
        "}}",                        # closes more than it opens
        "not json at all",
        "",
        "   ",
    ],
)
def test_genuine_syntax_errors_are_not_called_truncation(raw: str) -> None:
    assert _looks_truncated(raw) is False


def test_a_brace_inside_a_string_is_not_structure() -> None:
    """The walk has to track string state, or any JS/JSON payload in a value
    would look like an unclosed object."""
    assert _looks_truncated('{"code": "if (x) { y }"}') is False
    assert _looks_truncated('{"code": "}}}}"}') is False


def test_non_strings_are_handled() -> None:
    assert _looks_truncated(None) is False
    assert _looks_truncated(123) is False


# -- what the model is actually told ----------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Turn 1: a write_file cut off by the cap. Turn 2: a real syntax error."""

    protocol_version = "HTTP/1.1"
    turns = 0

    def log_message(self, *a):  # noqa: D102
        return

    def do_GET(self):  # noqa: N802
        self._send(json.dumps({"data": [{"id": "m"}]}).encode(), "application/json")

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).turns += 1
        base = {"id": "1", "object": "chat.completion.chunk", "model": "m"}
        out: list[str] = []

        def chunk(delta, finish=None):
            out.append("data: " + json.dumps(
                {**base, "choices": [{"index": 0, "delta": delta,
                                      "finish_reason": finish}]}) + "\n\n")

        if type(self).turns == 1:
            chunk({"role": "assistant", "tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "write_file", "arguments": ""}}]})
            chunk({"tool_calls": [{"index": 0, "function": {"arguments":
                '{"path": "doc.md", "content": "# Title\\n\\ntext that runs'}}]})
            chunk({}, "length")                      # the output cap
        elif type(self).turns == 2:
            chunk({"role": "assistant", "tool_calls": [{
                "index": 0, "id": "c2", "type": "function",
                "function": {"name": "write_file", "arguments": '{"path": "a.md",}'}}]})
            chunk({}, "tool_calls")                  # balanced, but malformed
        else:
            chunk({"role": "assistant", "content": "ok"})
            chunk({}, "stop")
        out.append("data: " + json.dumps({**base, "choices": [], "usage": {
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}) + "\n\n")
        out.append("data: [DONE]\n\n")
        self._send("".join(out).encode(), "text/event-stream")

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


@pytest.fixture()
def stub_backend(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    _Handler.turns = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _tool_results(backend: str) -> list[str]:
    from mantis_agent.agent import Agent
    from mantis_agent.builtin_tools import CODING_TOOLS

    seen: list[str] = []

    async def go():
        agent = Agent(model="m", backend=backend, tools=list(CODING_TOOLS),
                      max_tokens=64, max_steps=4)
        async for msg in agent.run_iter([UserMessage(content="write a long doc")]):
            if isinstance(msg, AssistantMessage) or not isinstance(msg.content, list):
                continue
            seen.extend(str(b.content) for b in msg.content
                        if isinstance(b, ToolResultBlock))

    anyio.run(go)
    return seen


def test_a_cut_off_call_is_reported_as_a_size_problem(stub_backend) -> None:
    """The model's next move should be 'write less', not 'fix my JSON'."""
    first = _tool_results(stub_backend)[0]
    assert "CUT OFF" in first
    assert "max_tokens=64" in first                  # the actual limit it hit
    assert "SMALLER" in first
    assert "syntax was fine" in first
    # and it is warned off the retry that would loop
    assert "same place" in first
    assert "not valid JSON" not in first


def test_a_genuinely_malformed_call_still_says_so(stub_backend) -> None:
    """The original message is right for a balanced-but-wrong payload — the fix
    there really is 'send well-formed JSON'."""
    second = _tool_results(stub_backend)[1]
    assert "not valid JSON" in second
    assert "CUT OFF" not in second
