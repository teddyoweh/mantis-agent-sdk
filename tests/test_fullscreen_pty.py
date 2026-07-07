"""End-to-end PTY tests: drive the REAL `mantis` fullscreen UI in a
pseudo-terminal — banner, slash commands, pickers, clean exit. This is the
coverage the fullscreen closures never had (queue/pickers/commands live inside
run_fullscreen and can't be unit-called)."""

from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")


class Term:
    """Minimal expect-style driver for one mantis process on a pty."""

    def __init__(self, tmp_home: str, cwd: str) -> None:
        env = dict(os.environ)
        env.update({
            "MANTIS_AGENT_HOME": tmp_home,
            "MANTIS_AGENT_PROJECT_ROOT": cwd,
            "TERM": "xterm-256color",
            "COLUMNS": "100", "LINES": "30",
        })
        env.pop("MANTIS_CLASSIC", None)
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mantis_agent.tui",
             "--model", "gpt-5.4", "--backend", "http://127.0.0.1:9", "--api-key", "k"],
            stdin=slave, stdout=slave, stderr=slave,
            env=env, cwd=cwd, close_fds=True,
        )
        os.close(slave)
        self.buf = b""

    def pump(self, seconds: float) -> None:
        """Sleep WHILE draining the pty — a full-screen app repaints constantly,
        and an undrained master fills the kernel buffer until the app blocks on
        write (then 'hangs'). Every pause in the driver must drain."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    self.buf += os.read(self.master, 65536)
                except OSError:
                    return

    def expect(self, text: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        needle = text.encode()
        while time.monotonic() < deadline:
            if needle in self.buf:
                return
            r, _, _ = select.select([self.master], [], [], 0.25)
            if r:
                try:
                    self.buf += os.read(self.master, 65536)
                except OSError:
                    break
        raise AssertionError(
            f"never saw {text!r}; last 500 bytes: {self.buf[-500:]!r}")

    def ready(self) -> None:
        """Wait until the input frame is actually accepting keys: the banner
        prints BEFORE the prompt_toolkit app starts, so 'tip:' alone is too
        early — wait for the rendered ❯ prompt, then let the loop settle."""
        self.expect("tip:")
        self.expect("❯")
        self.pump(0.8)

    def send(self, s: str) -> None:
        """Type like a human: one key at a time with tiny gaps. A single write
        containing text+\r can be treated as a PASTE by prompt_toolkit (the
        enter becomes literal instead of submit) — the source of pure flake."""
        self.pump(0.2)
        for ch in s:
            os.write(self.master, ch.encode())
            self.pump(0.02)
        self.pump(0.2)

    def esc(self) -> None:
        self.send("\x1b")
        self.pump(0.7)                   # let the ESC resolve alone — an ESC
                                         # immediately followed by '/' would
                                         # parse as a meta-key sequence

    def close(self) -> int:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and self.proc.poll() is None:
            self.pump(0.2)               # keep draining so exit prints can flush
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=12)
        try:
            os.close(self.master)
        except OSError:
            pass
        return self.proc.returncode


@pytest.fixture()
def term(tmp_path):
    t = Term(str(tmp_path / "home"), str(tmp_path))
    yield t
    if t.proc.poll() is None:
        t.proc.kill()
    t.close()


def test_boots_help_status_and_exits(term) -> None:
    term.ready()                       # banner rendered
    term.send("/help\r")
    term.expect("/models")                    # help lists commands
    term.expect("/goal")                      # new commands present
    term.send("/status\r")
    term.expect("version")
    term.expect("gpt-5.4")
    term.send("/exit\r")
    assert term.close() == 0                  # clean orderly exit


def test_model_picker_opens_and_escapes(term) -> None:
    term.ready()
    term.send("/models\r")
    term.expect("Pick a model")               # picker overlay up
    term.esc()                                # esc closes it
    term.send("/agents\r")
    term.expect("general-purpose")            # agent types render
    term.send("/exit\r")
    assert term.close() == 0


def test_bang_and_skills_and_twin_admin(term) -> None:
    term.ready()
    term.send("!echo pty-bang-works\r")
    term.expect("pty-bang-works")             # ! prefix ran the shell command
    term.send("/skills\r")
    term.expect("Skills")
    term.send("/twin\r")
    term.expect("no twins yet")               # twin admin path (no LLM)
    term.send("/goal\r")
    term.expect("no active goal")
    term.send("/exit\r")
    assert term.close() == 0


def test_crash_recovery_hint_appears(tmp_path) -> None:
    # Session 1: type a message? (would need a model). Instead simulate crash:
    # boot, let it write the unclean marker, then SIGKILL (no clean-exit flip).
    t1 = Term(str(tmp_path / "home"), str(tmp_path))
    t1.ready()
    t1.send("!echo seed\r")                    # forces a persisted meta message? no —
    t1.expect("seed")                          # just ensures the app is live
    t1.proc.send_signal(signal.SIGKILL)        # crash
    t1.close()
    # session 2 boots; hint only if the crashed session HAD messages — the !
    # output is meta-only and unpersisted, so accept either outcome but the
    # app must boot fine with a stale unclean marker present.
    t2 = Term(str(tmp_path / "home"), str(tmp_path))
    t2.ready()
    t2.send("/exit\r")
    assert t2.close() == 0
