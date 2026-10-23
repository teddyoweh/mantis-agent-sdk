"""Ctrl+O is the way to read earlier thoughts (the inline line is a one-line
summary). It refused mid-turn ("opens once this turn finishes"), so during a
long run there was no way to read a previous thought at all. Now the pager runs
in a worker thread while the turn keeps going, opens at the end, and q returns."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")


def _model() -> ThreadingHTTPServer:
    calls = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = json.dumps({"data": [{"id": "fake-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls["n"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(delta, finish=None):
                c = {"id": "x", "object": "chat.completion.chunk", "model": "fake-model",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                self.wfile.flush()
            send({"role": "assistant"})
            if calls["n"] == 1:
                send({"reasoning_content": "FIRST-THOUGHT: list the files before anything else. "})
                send({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                      "function": {"name": "bash", "arguments": json.dumps({"command": "echo listed"})}}]})
                send({}, "tool_calls")
            else:
                for i in range(12):
                    send({"reasoning_content": f"second thought {i}. "})
                    time.sleep(0.5)
                send({"content": "ALL DONE."})
                send({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_ctrl_o_reads_earlier_thoughts_while_the_turn_runs(tmp_path) -> None:
    sys.path.insert(0, os.path.dirname(__file__))
    from test_fullscreen_pty import Term  # noqa: PLC0415  (the shared pty driver)

    srv = _model()
    home = tmp_path / "home"
    home.mkdir()
    t = Term(str(home), str(tmp_path), env_extra={"MANTIS_AGENT_NO_CONTEXT": "1"},
             model="fake-model", backend=f"http://127.0.0.1:{srv.server_address[1]}/v1")
    try:
        t.ready()
        t.send("go\r")
        t.expect("FIRST-THOUGHT", timeout=30)
        t.pump(2.0)                                   # mid second call, still thinking
        mark = len(t.buf)
        t.send("\x0f")                                # ctrl+o
        t.pump(2.5)
        opened = t.buf[mark:]
        assert b"opens once this turn finishes" not in opened
        assert b"FIRST-THOUGHT: list the files before anything else." in opened
        t.send("q")
        t.expect("ALL DONE", timeout=30)              # the turn carried on underneath
    finally:
        t.proc.kill()
        srv.shutdown()
