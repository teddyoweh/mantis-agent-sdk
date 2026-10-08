"""Steering in the real ``mantis`` fullscreen terminal, over a PTY.

A stub OpenAI-compatible server streams its answers slowly so the turn is
still running when the test types. Enter on a plain message STEERS the live
run (it lands in the next request; no new turn is queued), and Tab keeps the
old behaviour — queue the line to run after the turn.
"""

from __future__ import annotations

import json
import re
import select
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_fullscreen_pty import Term

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")

STEER = "No new dependencies, please."


class _SlowModel(BaseHTTPRequestHandler):
    """The conversation's answers 1 and 2 stream their text, then hold the
    stream open until the test releases them (a turn the user can type into);
    answer 3 is instant. Side calls (session title, next-prompt ghost — no
    tools attached) get a quick unrelated reply and aren't recorded."""

    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    release: list[threading.Event] = []
    answers = [
        "Next, I'll install styled-components for dark mode.",
        "Got it. Using the existing CSS variables instead.",
        "Done with the follow-up.",
    ]

    def log_message(self, *a):  # noqa: D102
        return

    def do_GET(self):  # noqa: N802
        body = json.dumps({"data": [{"id": "gpt-5.4"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not body.get("tools"):       # a side call (title, suggestion)
            self._quick("ok")
            return
        cls = type(self)
        cls.requests.append(body)
        idx = min(len(cls.requests) - 1, len(self.answers) - 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {"id": "1", "object": "chat.completion.chunk", "model": "gpt-5.4"}

        def chunk(delta: dict, finish: str | None = None) -> None:
            self.wfile.write(("data: " + json.dumps(
                {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            ) + "\n\n").encode())
            self.wfile.flush()

        chunk({"role": "assistant", "content": self.answers[idx]})
        if idx < len(cls.release):
            cls.release[idx].wait(30)   # the turn stays live until released
        chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def _quick(self, text: str) -> None:
        base = {"id": "1", "object": "chat.completion.chunk", "model": "gpt-5.4"}
        out = "data: " + json.dumps({**base, "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}) \
            + "\n\ndata: [DONE]\n\n"
        raw = out.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True


@pytest.fixture()
def slow_backend():
    _SlowModel.requests = []
    _SlowModel.release = [threading.Event(), threading.Event()]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _SlowModel)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        for ev in _SlowModel.release:
            ev.set()
        httpd.shutdown()
        httpd.server_close()


_CSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _flat(raw: bytes) -> str:
    """What the screen SAYS, give or take layout: prompt_toolkit repaints by
    diff, so a row arrives cut up by cursor moves and its spaces are often a
    cursor jump rather than a character. Compare without escapes or blanks."""
    return re.sub(r"\s+", "", _CSI.sub(b"", raw).decode("utf-8", "replace"))


def _expect(t: Term, text: str, timeout: float = 20.0, *, since: int = 0) -> int:
    """``Term.expect`` that survives diff-painting; returns the buffer length
    at the match so a later check can look only at what came after."""
    needle = re.sub(r"\s+", "", text)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _flat(t.buf[since:]):
            return len(t.buf)
        r, _, _ = select.select([t.master], [], [], 0.25)
        if r:
            t._drain()
    raise AssertionError(f"never saw {text!r}; screen tail: {_flat(t.buf[since:])[-400:]!r}")


def _user_texts(body: dict) -> list[str]:
    """What the user said in a request, oldest first — without the engine's
    per-request tail (task evidence) and reminders."""
    out: list[str] = []
    for m in body["messages"]:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        parts = ([p.get("text", "") for p in c if isinstance(p, dict)]
                 if isinstance(c, list) else [c] if isinstance(c, str) else [])
        out += [p for p in parts
                if not p.startswith(("[Current task evidence]", "<system-reminder>"))]
    return out


def test_enter_steers_the_running_turn_and_tab_queues(tmp_path, slow_backend) -> None:
    first, second = _SlowModel.release
    t = Term(str(tmp_path / "home"), str(tmp_path), backend=slow_backend,
             env_extra={"MANTIS_AGENT_NO_CONTEXT": "1", "MANTIS_NO_CLIPBOARD_HINT": "1"})
    try:
        t.ready()
        t.send("Add dark mode\r")
        _expect(t, "styled-components")                    # streaming: the turn is live
        t.send(STEER)
        _expect(t, "↵ steer the running turn · tab queue")  # the hint says what Enter does
        mark = len(t.buf)
        t.send("\r")
        _expect(t, "⇢ steering", since=mark)                # pinned until the run takes it in
        assert "queued" not in _flat(t.buf[mark:])          # steered, NOT queued
        first.set()                                         # the model finishes its answer…
        _expect(t, "existing CSS variables", timeout=30, since=mark)  # …and the SAME run answers

        # The second answer is still streaming: Tab queues a line for AFTER it.
        t.send("and update the docs")
        t.send("\t")
        _expect(t, "⧉ queued (1)", since=mark)
        second.set()
        _expect(t, "Done with the follow-up", timeout=30, since=mark)

        reqs = _SlowModel.requests
        assert len(reqs) == 3, [_user_texts(r) for r in reqs]
        # Request 2 is the steered continuation of request 1: same run, the
        # steer is the newest user message and nothing else was submitted.
        assert _user_texts(reqs[1])[-1] == STEER
        convo = [m for m in reqs[1]["messages"] if m.get("role") != "system"]
        assert [m["role"] for m in convo[:3]] == ["user", "assistant", "user"]
        assert sum(1 for x in _user_texts(reqs[1]) if "Add dark mode" in x) == 1
        # Request 3 is the queued line, run as its own turn after the steer's.
        assert "and update the docs" in _user_texts(reqs[2])
        assert STEER in _user_texts(reqs[2])
    finally:
        for ev in _SlowModel.release:
            ev.set()
        if t.proc.poll() is None:
            t.send("/exit\r")
        t.close()


def test_slash_steer_steers_the_running_turn(tmp_path, slow_backend) -> None:
    """``/steer <message>`` does what a plain Enter does mid-run — the message
    joins the live run's next request — and is never queued or sent as a
    literal command."""
    first, second = _SlowModel.release
    t = Term(str(tmp_path / "home"), str(tmp_path), backend=slow_backend,
             env_extra={"MANTIS_AGENT_NO_CONTEXT": "1", "MANTIS_NO_CLIPBOARD_HINT": "1"})
    try:
        t.ready()
        t.send("Add dark mode\r")
        _expect(t, "styled-components")
        t.send("/steer " + STEER)
        _expect(t, "↵ steer the running turn · tab queue")
        mark = len(t.buf)
        t.send("\r")
        _expect(t, "⇢ steering", since=mark)
        assert "queued" not in _flat(t.buf[mark:])
        first.set()
        _expect(t, "existing CSS variables", timeout=30, since=mark)
        second.set()
        reqs = _SlowModel.requests
        assert _user_texts(reqs[1])[-1] == STEER          # the message, not "/steer …"
        assert not any(x.startswith("/steer") for r in reqs for x in _user_texts(r))
    finally:
        for ev in _SlowModel.release:
            ev.set()
        if t.proc.poll() is None:
            t.send("/exit\r")
        t.close()
