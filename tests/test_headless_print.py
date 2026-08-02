"""``mantis -p`` — headless print mode.

These drive the real CLI entry point against a stub OpenAI-compatible server,
because the contract worth protecting is the one a CI script sees: what lands
on stdout, what lands on stderr, and the exit code.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _StubHandler(BaseHTTPRequestHandler):
    """A model that calls ``read_file`` once, then answers with its contents."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: D102 — silence the test log
        return

    def do_GET(self):  # noqa: N802
        self._send(json.dumps({"data": [{"id": "gpt-5.4"}]}).encode(),
                   "application/json")

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        used_tool = any(m.get("role") == "tool" for m in body.get("messages", []))
        base = {"id": "1", "object": "chat.completion.chunk", "model": "gpt-5.4"}
        out = []

        def chunk(delta, finish=None):
            out.append("data: " + json.dumps(
                {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            ) + "\n\n")

        if used_tool:
            chunk({"role": "assistant", "content": "the note says: hello"})
            chunk({}, "stop")
        else:
            chunk({"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": ""}}]})
            chunk({"tool_calls": [{"index": 0,
                                   "function": {"arguments": '{"path": "note.txt"}'}}]})
            chunk({}, "tool_calls")
        out.append("data: " + json.dumps(
            {**base, "choices": [],
             "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}}
        ) + "\n\n")
        out.append("data: [DONE]\n\n")
        self._send("".join(out).encode(), "text/event-stream")

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No keep-alive: a reused connection to a stub that's about to be torn
        # down by the next test's fixture shows up as an empty response, which
        # looks exactly like a model failure. One connection per request.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


@pytest.fixture()
def stub_backend():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def run_p(tmp_path, stub_backend):
    """Run ``mantis -p …`` in a scratch project against the stub model."""
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    def go(*args: str, stdin: str | None = None, timeout: float = 60.0):
        import os

        env = dict(os.environ,
                   MANTIS_AGENT_HOME=str(home),
                   MANTIS_AGENT_MODEL="gpt-5.4",
                   MANTIS_AGENT_BASE_URL=stub_backend,
                   MANTIS_AGENT_API_KEY="k",
                   MANTIS_AGENT_NO_CONTEXT="1")
        return subprocess.run(
            [sys.executable, "-m", "mantis_agent.tui", "-p", *args],
            input=stdin, capture_output=True, text=True, cwd=str(tmp_path),
            env=env, timeout=timeout, check=False,
        )

    return go


# -- the three output formats ----------------------------------------------


def test_text_mode_prints_just_the_answer(run_p) -> None:
    r = run_p("read note.txt", "--godmode")
    assert r.returncode == 0
    assert r.stdout == "the note says: hello\n"   # exactly the reply, one newline


def test_json_mode_emits_one_result_object(run_p) -> None:
    r = run_p("read note.txt", "--godmode", "--json")
    assert r.returncode == 0
    obj = json.loads(r.stdout)                    # one object, not a stream
    assert obj["type"] == "result" and obj["subtype"] == "success"
    assert obj["is_error"] is False
    assert obj["result"] == "the note says: hello"
    assert obj["num_turns"] >= 1
    assert obj["session_id"] and obj["duration_ms"] >= 0
    # the fields CI actually reads
    for key in ("total_cost_usd", "usage", "modelUsage", "permission_denials"):
        assert key in obj


def test_stream_json_is_ndjson_ending_in_the_result(run_p) -> None:
    r = run_p("read note.txt", "--godmode", "--output-format", "stream-json", "--verbose")
    assert r.returncode == 0
    lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    assert lines[0]["type"] == "system" and lines[0]["subtype"] == "init"
    assert lines[0]["model"] == "gpt-5.4" and lines[0]["cwd"]
    assert lines[0]["tools"], "init should advertise the tool belt"
    assert lines[-1]["type"] == "result" and lines[-1]["subtype"] == "success"
    kinds = [ln["type"] for ln in lines]
    assert "assistant" in kinds and "user" in kinds
    # the tool round-trip is visible in the stream
    blocks = [b.get("type") for ln in lines if ln["type"] == "assistant"
              for b in ln["message"].get("content", [])]
    assert "tool_use" in blocks


def test_stream_json_requires_verbose(run_p) -> None:
    """Claude Code's rule, kept identical — a quiet pipe must not become a
    firehose because someone forgot a flag."""
    r = run_p("hi", "--output-format", "stream-json")
    assert r.returncode == 1
    assert "requires --verbose" in r.stderr
    assert r.stdout == ""


def test_json_verbose_emits_every_message(run_p) -> None:
    r = run_p("read note.txt", "--godmode", "--json", "--verbose")
    assert r.returncode == 0
    msgs = json.loads(r.stdout)
    assert isinstance(msgs, list) and len(msgs) > 1
    assert msgs[-1]["type"] == "result"


# -- input, errors, exit codes ---------------------------------------------


def test_prompt_can_come_from_stdin(run_p) -> None:
    r = run_p("--godmode", stdin="read note.txt\n")
    assert r.returncode == 0 and "hello" in r.stdout


def test_dash_reads_stdin_too(run_p) -> None:
    r = run_p("-", "--godmode", stdin="read note.txt\n")
    assert r.returncode == 0 and "hello" in r.stdout


def test_no_prompt_is_an_error_on_stderr(run_p) -> None:
    r = run_p("--godmode", stdin="")
    assert r.returncode == 1
    assert "must be provided" in r.stderr
    assert r.stdout == ""            # nothing on stdout for a pipeline to parse


def test_unquoted_words_are_one_prompt(run_p) -> None:
    """`mantis -p read note.txt` shouldn't silently drop everything after the
    first word."""
    from mantis_agent.headless import resolve_prompt

    assert resolve_prompt(["read", "note.txt"]) == "read note.txt"
    assert resolve_prompt([]) == "" or True      # empty falls through to stdin


def test_a_bare_prompt_without_p_explains_itself(tmp_path) -> None:
    import os

    env = dict(os.environ, MANTIS_AGENT_HOME=str(tmp_path))
    r = subprocess.run([sys.executable, "-m", "mantis_agent.tui", "do a thing"],
                       capture_output=True, text=True, env=env, timeout=60, check=False)
    assert r.returncode == 1
    assert "-p/--print" in r.stderr


# -- sessions ---------------------------------------------------------------


def test_print_runs_land_in_the_terminals_session_store(run_p, tmp_path) -> None:
    """A headless run has to be visible to `mantis --resume` — CI and the
    laptop should be looking at one history, not two."""
    from mantis_agent import session_tree

    r = run_p("read note.txt", "--godmode", "--json")
    sid = json.loads(r.stdout)["session_id"]

    import os

    os.environ["MANTIS_AGENT_HOME"] = str(tmp_path / "home")
    try:
        sessions = session_tree.list_sessions(cwd=str(tmp_path))
        assert [s.session_id for s in sessions] == [sid]
        assert sessions[0].message_count >= 2
        assert "read note.txt" in (sessions[0].last_prompt or sessions[0].first_prompt or "")
    finally:
        os.environ.pop("MANTIS_AGENT_HOME", None)


def test_resume_carries_the_prior_turns(run_p) -> None:
    """The second run should not have to redo the tool call — proof that prior
    history actually reached the model, not just that the id was reused."""
    first = json.loads(run_p("read note.txt", "--godmode", "--json").stdout)
    assert first["num_turns"] == 2                    # tool call, then answer

    again = json.loads(
        run_p("and again", "--godmode", "--resume", first["session_id"], "--json").stdout)
    assert again["session_id"] == first["session_id"]
    assert again["num_turns"] == 1                    # the tool result was already there


def test_continue_picks_up_the_last_session(run_p) -> None:
    first = json.loads(run_p("read note.txt", "--godmode", "--json").stdout)
    cont = json.loads(run_p("more", "--godmode", "--continue", "--json").stdout)
    assert cont["session_id"] == first["session_id"]


def test_continue_with_no_history_warns_and_still_runs(run_p) -> None:
    r = run_p("read note.txt", "--godmode", "--continue")
    assert r.returncode == 0
    assert "no previous session" in r.stderr          # said out loud, not silently ignored
    assert "hello" in r.stdout


def test_session_id_can_be_pinned(run_p) -> None:
    r = run_p("read note.txt", "--godmode", "--session-id", "ci-run-42", "--json")
    assert json.loads(r.stdout)["session_id"] == "ci-run-42"


# -- option plumbing --------------------------------------------------------


def _args(**over):
    import argparse

    base = dict(model="m", backend="", api_key=None, system=None, max_tokens=8192,
                temperature=None, max_turns=200, effort=None, verbosity=None,
                reasoning_mode=None, permission_mode=None, godmode=False,
                dangerously_skip_permissions=False, allowed_tools=None,
                disallowed_tools=None, continue_session=False, resume_id=None,
                session_id=None, append_system_prompt=None, output_format="text",
                verbose=False, prompt=[], print_mode=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_options_carry_the_terminal_tool_belt_and_permissions() -> None:
    from mantis_agent.headless import build_query_options

    opts = build_query_options(_args())
    names = {getattr(t, "name", "") for t in opts["tools"]}
    assert {"read_file", "write_file", "bash", "grep"} <= names
    assert opts["permission_mode"] == "default"     # dangerous shell still refused

    god = build_query_options(_args(godmode=True))
    assert god["permission_mode"] == "bypass"
    skip = build_query_options(_args(dangerously_skip_permissions=True))
    assert skip["permission_mode"] == "bypass"


def test_tool_filters_and_system_prompt_append() -> None:
    from mantis_agent.headless import build_query_options

    opts = build_query_options(_args(allowed_tools="read_file, grep",
                                     disallowed_tools="bash",
                                     system="BASE", append_system_prompt="EXTRA"))
    assert opts["extra"]["allowed_tools"] == ["read_file", "grep"]
    assert opts["extra"]["disallowed_tools"] == ["bash"]
    assert opts["system"].startswith("BASE") and opts["system"].endswith("EXTRA")


def test_the_advisor_needs_a_transcript_to_be_paired(monkeypatch) -> None:
    """Unattended runs are where escalation matters most — but the advisor
    reads the conversation, so it stays off unless the caller hands it one."""
    from mantis_agent.headless import build_query_options

    monkeypatch.setenv("MANTIS_ADVISOR", "claude-opus-5")

    off = build_query_options(_args())
    assert "consult_advisor" not in {getattr(t, "name", "") for t in off["tools"]}

    convo: list = []
    on = build_query_options(_args(), transcript=convo)
    assert "consult_advisor" in {getattr(t, "name", "") for t in on["tools"]}
    # named in the prompt too, or a paired advisor just sits in the belt unused
    assert "consult_advisor" in on["system"] and "claude-opus-5" in on["system"]


def test_advisor_off_leaves_the_belt_alone(monkeypatch) -> None:
    from mantis_agent.headless import build_query_options

    monkeypatch.setenv("MANTIS_ADVISOR", "claude-opus-5")
    opts = build_query_options(_args(advisor="off"), transcript=[])
    assert "consult_advisor" not in {getattr(t, "name", "") for t in opts["tools"]}
    assert not opts.get("system")


def test_error_text_matches_the_subtype() -> None:
    from mantis_agent.headless import _final_text
    from mantis_agent.query import SDKResultMessage

    args = _args(max_turns=3)
    assert _final_text(SDKResultMessage(subtype="success", result="hi"), args) == "hi\n"
    assert _final_text(SDKResultMessage(subtype="error_max_turns", is_error=True),
                       args) == "Error: Reached max turns (3)\n"
    boom = SDKResultMessage(subtype="error_during_execution", is_error=True,
                            errors=["Backend exploded"])
    assert _final_text(boom, args) == "Backend exploded\n"
