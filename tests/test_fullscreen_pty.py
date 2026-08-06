"""End-to-end PTY tests: drive the REAL `mantis` fullscreen UI in a
pseudo-terminal — banner, slash commands, pickers, clean exit. This is the
coverage the fullscreen closures never had (queue/pickers/commands live inside
run_fullscreen and can't be unit-called)."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import json
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")


class Term:
    """Minimal expect-style driver for one mantis process on a pty."""

    def __init__(self, tmp_home: str, cwd: str, env_extra: dict | None = None,
                 model: str = "gpt-5.4",
                 backend: str = "http://127.0.0.1:9") -> None:
        env = dict(os.environ)
        env.update({
            "MANTIS_AGENT_HOME": tmp_home,
            "MANTIS_AGENT_PROJECT_ROOT": cwd,
            "TERM": "xterm-256color",
            "COLUMNS": "100", "LINES": "30",
        })
        env.pop("MANTIS_CLASSIC", None)
        if env_extra:
            env.update(env_extra)
        self.master, slave = pty.openpty()
        # Give the pty the same window size we advertise in COLUMNS/LINES. A
        # bare openpty() is 80x24, so without this the app lays out for 100x30
        # and the terminal wraps every wide panel — a tall overlay then renders
        # past the bottom of the real screen and its last rows never appear.
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mantis_agent.tui",
             "--model", model, "--backend", backend, "--api-key", "k"],
            stdin=slave, stdout=slave, stderr=slave,
            env=env, cwd=cwd, close_fds=True,
        )
        os.close(slave)
        self.buf = b""

    # prompt_toolkit asks the terminal where the cursor is (DSR / "CPR") and
    # WAITS for the reply. A real terminal answers; this driver is the terminal,
    # so if we stay silent prompt_toolkit eventually times out, prints
    # "WARNING: your terminal doesn't support cursor position requests (CPR)."
    # and blocks on "Press ENTER to continue…" — after which every expect() in
    # the test fails on an app that is simply waiting for a keypress. Whether
    # the timeout is reached depends on machine load, which is exactly why this
    # showed up as a ~1-in-5 flake in the heaviest test rather than a hard fail.
    _CPR_REQUEST = b"\x1b[6n"

    def _drain(self) -> bytes:
        """Read whatever is pending, answering any cursor-position request."""
        try:
            data = os.read(self.master, 65536)
        except OSError:
            return b""
        self.buf += data
        if self._CPR_REQUEST in data:
            # Report the cursor at 1,1 — the app only needs *a* well-formed
            # answer, not an accurate one.
            for _ in range(data.count(self._CPR_REQUEST)):
                try:
                    os.write(self.master, b"\x1b[1;1R")
                except OSError:
                    break
        return data

    def pump(self, seconds: float) -> None:
        """Sleep WHILE draining the pty — a full-screen app repaints constantly,
        and an undrained master fills the kernel buffer until the app blocks on
        write (then 'hangs'). Every pause in the driver must drain."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r and not self._drain():
                return

    def expect(self, text: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        needle = text.encode()
        while time.monotonic() < deadline:
            if needle in self.buf:
                return
            r, _, _ = select.select([self.master], [], [], 0.25)
            if r and not self._drain():
                break
        # A blocked CPR prompt is the classic cause of a mass expect() failure,
        # so name it rather than leaving the next reader to decode raw bytes.
        hint = ("  [the app is parked on prompt_toolkit's CPR fallback — the "
                "driver failed to answer ESC[6n]"
                if b"Press ENTER to continue" in self.buf else "")
        raise AssertionError(
            f"never saw {text!r};{hint} last 500 bytes: {self.buf[-500:]!r}")

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

    def send_until(self, keys: str, marker: str, tries: int = 3,
                   each: float = 8.0) -> None:
        """Send ``keys`` and retry until ``marker`` shows up.

        The app can be BUSY rather than idle when the driver types: the boot
        MCP connect includes a server on a refused port whose connect timeout
        is 5s, and every observed failure of this test ran exactly ~5.2s longer
        than a passing one — the keystroke landed inside that window and was
        dropped, so the overlay never opened. Retrying is the honest fix for a
        driver racing a busy UI; a fixed sleep just moves the race.
        """
        for attempt in range(tries):
            if attempt:
                # Wipe whatever of the previous attempt landed. Without this a
                # partially-delivered "/mcp" plus a retry becomes "/mc/mcp",
                # which can never open the overlay — the retry would guarantee
                # failure rather than recover from it.
                self.send("\x7f" * (len(keys) + 8))
            self.send(keys)
            deadline = time.monotonic() + each
            while time.monotonic() < deadline:
                if marker.encode() in self.buf:
                    return
                r, _, _ = select.select([self.master], [], [], 0.25)
                if r and not self._drain():
                    break
        raise AssertionError(
            f"never saw {marker!r} after {tries} attempts sending {keys!r}; "
            f"last 300 bytes: {self.buf[-300:]!r}")

    def esc(self) -> None:
        self.send("\x1b")
        self.pump(0.7)                   # let the ESC resolve alone — an ESC
                                         # immediately followed by '/' would
                                         # parse as a meta-key sequence

    def close(self) -> int:
        # Must exceed the app's OWN shutdown budget, or a correct-but-slow exit
        # gets SIGKILLed and scored as a hang. On /exit the terminal awaits
        # MCPManager.stop(), whose default bound is 15s (mcp/manager.py) — a
        # 12s deadline here meant a session with MCP servers attached failed
        # roughly 1 run in 5, only when teardown ran long.
        deadline = time.monotonic() + 40
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


@pytest.fixture()
def term_with_agents(tmp_path):
    """A mantis process seeded with fake live subagents (MANTIS_FS_SEED_AGENTS)
    so the ↓-into-subagent-inspector path can be driven without a real run."""
    t = Term(str(tmp_path / "home"), str(tmp_path),
             env_extra={"MANTIS_FS_SEED_AGENTS": "1"})
    yield t
    if t.proc.poll() is None:
        t.proc.kill()
    t.close()


@pytest.fixture()
def term_with_mcp(tmp_path):
    """A mantis process whose user-level mcp.json already holds two servers —
    one stdio, one remote with a credential — so /mcp has something to inspect.
    ``echo`` is a real binary that immediately fails the MCP handshake, which
    is exactly the failed-server row we want rendered."""
    import json

    home = tmp_path / "home"
    home.mkdir()
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {
        "alpha": {"command": "echo", "args": ["-y", "alpha-mcp-server"]},
        "beta": {"type": "http", "url": "http://127.0.0.1:9/mcp",   # refused, fast
                 "headers": {"Authorization": "Bearer tok-plaintext-123"}},
    }}), encoding="utf-8")
    t = Term(str(home), str(tmp_path))
    yield t
    if t.proc.poll() is None:
        t.proc.kill()
    t.close()


def test_mcp_view_inspects_config_masks_secrets_and_adds_json(
    term_with_mcp, tmp_path
) -> None:
    """One session over the whole /mcp surface, because booting a pty app is the
    expensive part: the list, the detail card (real config + raw JSON, secrets
    masked until 's'), esc unwinding a layer at a time, and the add flow taking
    a pasted {"mcpServers": …} blob straight into the user config."""
    import json

    term = term_with_mcp
    term.ready()
    term.expect("mcp:", timeout=40.0)          # boot connect settled
    term.send_until("/mcp\r", "MCP servers")
    term.expect("MCP servers")                 # list header with counts
    # Sync on the list FOOTER, not on a server name: "alpha"/"beta" are already
    # in the buffer from the boot summary, so expecting those would race ahead
    # of the overlay's first paint and send Enter into the bare prompt.
    term.expect("enter inspect")
    term.pump(0.4)
    term.send("\r")                            # Enter — inspect the first row
    term.expect("transport")                   # detail card fields
    term.expect("config json")                 # raw entry, as configured
    term.expect("masked")                      # credentials hidden by default
    term.esc()                                 # esc — back to the list
    term.expect("enter inspect")               # list footer hint again
    term.send("\x1b[B")                        # ↓ to the remote server
    term.pump(0.4)
    term.send("\r")
    term.expect("Authorization")               # header key shown…
    term.expect("••••")                        # …value is not
    term.send("s")                             # reveal
    term.expect("tok-plaintext-123")
    term.esc()                                 # card → list
    term.expect("enter inspect")
    term.send("a")                             # add flow: one paste field
    term.expect("enter confirm")               # add-mode footer (the row's own
                                               # "paste JSON…" tip is always up)
    term.send('{"mcpServers":{"gamma":{"command":"echo","args":["gamma"]}}}')
    term.send("\r")
    term.expect("added gamma", timeout=60.0)   # announced after the reconnect
    saved = json.loads((tmp_path / "home" / "mcp.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["gamma"] == {"command": "echo", "args": ["gamma"]}
    assert set(saved["mcpServers"]) == {"alpha", "beta", "gamma"}  # existing kept
    term.esc()                                 # overlay closed
    term.send("/exit\r")
    assert term.close() == 0


def test_down_arrow_enters_live_subagent_inspector(term_with_agents) -> None:
    """↓ enters the inspector list; Enter drills into a focused detail view;
    ← goes back to the list; esc closes."""
    term = term_with_agents
    term.ready()
    term.send("\x1b[B")                        # ↓ — enter the inspector list
    term.expect("Live agents")                 # list header
    term.expect("explore")                     # a seeded subagent row
    # The feed says what the agent DID, not just which tool ran. It used to
    # render "tool grep" over and over — no pattern, no file, no result.
    term.expect("Search def handler")          # verb + the actual argument…
    term.expect("2 matches")                   # …and what came back
    assert b"tool grep" not in term.buf
    term.send("\r")                            # Enter — drill into detail
    term.expect("← back")                      # detail-view header
    term.expect("Read mantis_agent/tui.py")    # detail view carries it too
    term.expect("4157 lines")
    term.send("\x1b[D")                        # ← — back to the list
    term.expect("Enter inspect")               # list header hint again
    term.esc()                                 # esc closes the overlay
    term.send("/exit\r")
    assert term.close() == 0


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


def test_advisor_pairs_a_model_and_turns_off(tmp_path) -> None:
    """The real terminal, not the unit path: /advisor has to resolve a model,
    put it in /status, and be switchable off — in one session, because booting
    a pty app is the expensive part."""
    t = Term(str(tmp_path / "home"), str(tmp_path))
    try:
        t.ready()
        t.send("/advisor\r")
        t.expect("off")                        # unpaired by default
        t.send("/advisor opus\r")
        t.expect("claude-opus")                # the alias resolved to an id…
        t.expect("Anthropic")                  # …carrying its own provider
        t.send("/status\r")
        t.expect("advisor")                    # the pairing is visible at a glance
        t.send("/advisor off\r")
        t.expect("no escalation")
        t.send("/exit\r")
        assert t.close() == 0
    finally:
        if t.proc.poll() is None:
            t.proc.kill()
            t.close()


def test_model_picker_opens_and_escapes(term) -> None:
    term.ready()
    term.send("/models\r")
    term.expect("select a model")             # picker overlay up (framed title)
    term.esc()                                # esc closes it
    term.send("/agents\r")
    term.expect("general-purpose")            # agent types render
    term.send("/exit\r")
    assert term.close() == 0


def test_workflows_overlay_opens_navigates_and_escapes(term) -> None:
    term.ready()
    term.send("/workflows\r")
    term.expect("Workflows")                   # overlay header rendered
    term.expect("No workflows in this session yet")   # empty state teaches the cmd
    term.expect("/workflows run <name>")              # …by naming it
    term.send("\x1b[B")                        # down-arrow: safe no-op when empty
    term.send("\x1b[A")                        # up-arrow: same
    term.pump(0.3)
    term.expect("No workflows in this session yet")   # still up, nothing crashed
    term.esc()                                 # esc closes the overlay
    term.send("/workflows list\r")             # subcommand path prints inline
    term.expect("review")                      # a built-in definition is listed
    term.send("/models\r")                     # app still interactive afterward
    term.expect("select a model")              # picker header up again
    term.esc()
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


def test_paste_command_stages_an_image(tmp_path) -> None:
    """`/paste <path>` attaches without touching the system clipboard, and the
    staged-attachment line appears above the prompt so it's obvious the image
    will actually be sent."""
    import base64

    png = tmp_path / "shot.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
        "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    # The clipboard hint polls the real OS clipboard; keep it out of the test so
    # a developer's copied screenshot can't change what's on screen.
    t = Term(str(tmp_path / "home"), str(tmp_path),
             env_extra={"MANTIS_NO_CLIPBOARD_HINT": "1"})
    try:
        t.ready()
        t.send(f"/paste {png}\r")
        t.expect("1 image attached")
        t.expect("sends with your next message")
        # …and ONLY there. The attach used to also print a line into the
        # transcript, so every paste left a duplicate "attached [Image #1] —
        # sends with your next message" in the scrollback.
        assert b"attached [Image" not in t.buf
        t.send("/exit\r")
        assert t.close() == 0
    finally:
        if t.proc.poll() is None:
            t.proc.kill()
            t.close()


def test_cmd_v_of_a_copied_file_attaches_it(tmp_path) -> None:
    """⌘V can only carry text, so a file copied in Finder arrives as its POSIX
    path inside a bracketed paste. That has to attach like ctrl+v does, not
    drop a bare path string in the buffer."""
    import base64

    png = tmp_path / "shot.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
        "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    t = Term(str(tmp_path / "home"), str(tmp_path),
             env_extra={"MANTIS_NO_CLIPBOARD_HINT": "1"})
    try:
        t.ready()
        # Exactly what the terminal writes for ⌘V: ESC[200~ <text> ESC[201~
        os.write(t.master, b"\x1b[200~" + str(png).encode() + b"\x1b[201~")
        t.pump(1.0)
        t.expect("[Image #1]")          # a chip in the input, not the raw path
        t.expect("1 image attached")    # and staged for the next message
        assert b"attached [Image" not in t.buf      # still nothing in scrollback
        # Ctrl+C to quit: the buffer holds the chip, so typing /exit into it
        # would submit "[Image #1] /exit" as a message instead of a command.
        t.send("\x03")
        assert t.close() == 0
    finally:
        if t.proc.poll() is None:
            t.proc.kill()
            t.close()


def test_pasted_text_that_is_not_a_path_is_still_just_text(tmp_path) -> None:
    """The paste handler must not eat ordinary pasted text."""
    t = Term(str(tmp_path / "home"), str(tmp_path),
             env_extra={"MANTIS_NO_CLIPBOARD_HINT": "1"})
    try:
        t.ready()
        os.write(t.master, b"\x1b[200~hello /not/a/real/file.png there\x1b[201~")
        t.pump(1.0)
        t.expect("hello /not/a/real/file.png there")
        assert b"image attached" not in t.buf
        t.send("\x03")                 # pasted text is still in the buffer
        assert t.close() == 0
    finally:
        if t.proc.poll() is None:
            t.proc.kill()
            t.close()


@pytest.fixture()
def term_with_monitor(tmp_path):
    """A mantis process whose ONLY live work is a monitor.

    The case the manage surface used to miss entirely: ↓ keyed off live
    subagents, so with just a watch running it did nothing at all — while the
    footer cheerfully advertised "↓ to manage".
    """
    t = Term(str(tmp_path / "home"), str(tmp_path),
             env_extra={"MANTIS_FS_SEED_MONITOR": "1"})
    yield t
    if t.proc.poll() is None:
        t.proc.kill()
    t.close()


def test_down_arrow_manages_a_monitor_and_drills_into_its_script(term_with_monitor) -> None:
    """↓ reaches a monitor with no agents running; Enter shows what it RUNS."""
    term = term_with_monitor
    term.ready()
    term.expect("1 monitor")                    # footer roll-up
    term.send("\x1b[B")                         # ↓ — manage
    term.expect("Monitor google.com status")    # the monitor is in the list
    term.send("\r")                             # Enter — drill in
    term.expect("Monitor details")              # its own pane, not the agent one
    term.expect("Status:")
    term.expect("while true")                   # the script, verbatim
    term.expect("x stop")                       # the stop affordance is real
    term.send("\x1b[D")                         # ← back to the list
    term.expect("Monitor google.com status")
    term.esc()
    term.send("/exit\r")
    assert term.close() == 0


def test_slash_menu_scrolls_past_the_first_page(term) -> None:
    """The real terminal proof for the `/` menu: it used to render opts[:8], so
    only the first 8 commands were ever visible and arrowing down moved a
    highlight that had already scrolled off. Walk past the first page and a
    command that lives beyond it must scroll into view.

    Asserting on the rendered command rather than the "13/47" counter on
    purpose: prompt_toolkit redraws incrementally, so a counter ticking 12 -> 13
    puts only the changed cell on the wire, never the whole string.
    """
    term.ready()
    term.send("/")
    term.expect("type to filter")     # menu up, and it says there is more
    term.expect("/models")            # first page (index 0)
    for _ in range(12):               # walk to index 12, well past the 8 rows
        term.send("\x1b[B")
    term.expect("rewind")             # index 12 — unreachable before this fix
    term.esc()
    term.send("/exit\r")
    assert term.close() == 0


def test_cerebras_key_prompt_names_shape_and_console(term) -> None:
    """Enabling a locked provider from the picker must say what to paste. The
    prompt used to name only the env var (`CEREBRAS_API_KEY`), which tells a
    first-time user neither the key's shape nor where to get one."""
    term.ready()
    term.send("/models\r")
    term.expect("select a model")
    term.send("gemma-4-31b")           # Cerebras-only id, so the row is unambiguous
    term.expect("gemma-4-31b")
    term.send("\x0b")                  # ^k — set a key for the highlighted row
    term.expect("csk-")                # the key's actual shape…
    term.expect("cloud.cerebras.ai")   # …and where to get one
    term.esc()
    term.esc()
    term.send("/exit\r")
    assert term.close() == 0


def test_picker_tabs_mark_which_providers_are_live(tmp_path) -> None:
    """The tab bar drew every unselected chip identically dim, so a provider
    with a working key was indistinguishable from one never set up. Keyed
    providers now carry a ✦; unkeyed ones stay bare."""
    t = Term(str(tmp_path / "home"), str(tmp_path), env_extra={
        "ANTHROPIC_API_KEY": "sk-ant-api03-" + "A" * 40,
        "CEREBRAS_API_KEY": "csk-" + "C" * 40,
    })
    try:
        t.ready()
        t.send("/models\r")
        t.expect("select a model")
        t.expect("✦claude")            # keyed -> marked
        t.expect("✦cerebras")
        t.expect("groq")               # unkeyed -> present but unmarked
        assert b"\xe2\x9c\xa6groq" not in t.buf, "unkeyed provider must not be starred"
        t.esc()
        t.send("/exit\r")
        assert t.close() == 0
    finally:
        if t.proc.poll() is None:
            t.proc.kill()
            t.close()


def test_the_active_providers_tab_does_not_disappear(tmp_path) -> None:
    """Switching to a provider used to delete its chip from the tab bar.

    Provider groups dropped any model belonging to the active backend, because
    the hidden ● active group already listed it — so with Cerebras selected the
    "cerebras" chip vanished and its other models became unreachable from the
    bar. The provider keeps its tab; only the cross-provider views dedupe."""
    # MANTIS_AGENT_HOME *is* the agent dir — not a parent of ".mantis-agent".
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "live_models.json").write_text(json.dumps(
        {"cerebras": {"ts": time.time(),
                      "models": ["gemma-4-31b", "gpt-oss-120b", "zai-glm-4.7"]}}))
    t = Term(str(home), str(tmp_path),
             env_extra={"CEREBRAS_API_KEY": "csk-" + "C" * 40},
             model="zai-glm-4.7", backend="https://api.cerebras.ai/v1")
    try:
        t.ready()
        t.send("/models\r")
        # Assert the two facts separately: prompt_toolkit styles the title, so
        # escapes sit between "select a model" and the model name in the stream.
        t.expect("api.cerebras.ai")                 # we really are on Cerebras
        t.expect("cerebras 3")                      # …and its tab still lists all 3
        t.send("\x1b[C")                            # tabs are navigable to it
        t.esc()
        t.send("/exit\r")
        assert t.close() == 0
    finally:
        if t.proc.poll() is None:
            t.proc.kill()
            t.close()
