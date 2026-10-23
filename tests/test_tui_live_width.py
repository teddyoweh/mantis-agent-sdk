"""A launcher that exports COLUMNS/LINES pinned the terminal to its launch
size: rich and shutil prefer those variables to the terminal's own size, so
after the pane narrowed (a sidebar opening) every thinking line, answer and
tool output was laid out for the old, wider window and ran off the right edge.
On a real terminal the live size wins."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import signal
import struct
import sys
import termios
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mantis_agent.tui import follow_live_terminal_size

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")


class _FakeTTY:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd


def test_a_real_terminal_drops_inherited_columns(monkeypatch) -> None:
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 70, 0, 0))
        monkeypatch.setattr(sys, "stdout", _FakeTTY(slave))
        monkeypatch.setenv("COLUMNS", "200")
        monkeypatch.setenv("LINES", "60")
        assert follow_live_terminal_size() is True
        assert "COLUMNS" not in os.environ and "LINES" not in os.environ
    finally:
        os.close(master)
        os.close(slave)


def test_a_pipe_keeps_them(monkeypatch) -> None:
    import io
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setenv("COLUMNS", "200")
    assert follow_live_terminal_size() is False
    assert os.environ["COLUMNS"] == "200"


# -- the real terminal, narrowed mid-session --------------------------------

_THINK = ("I'm mid-task: find traffic camera networks and feeds near the address, then verify every "
          "camera feed URL before writing the report. ") * 3
_ANSWER = "Here is a long answer line that must wrap at the edge: " + "camera-feed " * 30


class _Model(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "fake-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(delta, finish=None):
            chunk = {"id": "x", "object": "chat.completion.chunk", "model": "fake-model",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        send({"role": "assistant"})
        send({"reasoning_content": _THINK})
        send({"content": _ANSWER})
        send({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def test_narrowing_the_window_narrows_what_is_printed(tmp_path) -> None:
    sys.path.insert(0, os.path.dirname(__file__))
    from test_fullscreen_pty import Term  # noqa: PLC0415  (the shared pty driver)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    home.mkdir()
    # Term exports COLUMNS=100 like a launcher that pins it
    t = Term(str(home), str(tmp_path), env_extra={"MANTIS_AGENT_NO_CONTEXT": "1"},
             model="fake-model", backend=f"http://127.0.0.1:{srv.server_address[1]}/v1")
    try:
        t.ready()
        fcntl.ioctl(t.master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 70, 0, 0))
        os.kill(t.proc.pid, signal.SIGWINCH)
        t.pump(1.0)
        t.send("go\r")
        t.expect("camera-feed", timeout=40)
        t.pump(2.0)
    finally:
        t.proc.kill()
        srv.shutdown()
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]|\x1b[=>]", "",
                  t.buf.decode("utf-8", "replace"))
    printed = [ln for ln in re.split(r"\r?\n|\r", text) if "thinking (" in ln or "camera-feed" in ln]
    assert printed, "nothing printed"
    assert max(len(ln.rstrip()) for ln in printed) <= 70, [len(ln) for ln in printed]
