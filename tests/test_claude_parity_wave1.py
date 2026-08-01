"""Claude Code parity wave 1: ! bash prefix, # memory notes, custom slash
commands (.mantis/commands/*.md), persistent history, /status /cost /doctor
/permissions, and the long-turn bell."""

from __future__ import annotations

import pytest

from mantis_agent.tui import (
    SLASH_COMMANDS,
    MantisTUI,
    all_slash_commands,
    bang_context_block,
    build_help_lines,
    discover_custom_commands,
    expand_custom_command,
    expand_slash_prompt,
    notify_turn_done,
    quick_memory_note,
    run_bang_command,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


def _tui() -> MantisTUI:
    return MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="sk-x",
                     system=None, max_tokens=1, temperature=None, max_turns=1)


# -- ! bash prefix -------------------------------------------------------------


def test_run_bang_command_captures_output() -> None:
    assert run_bang_command("echo hello-mantis").strip() == "hello-mantis"


def test_run_bang_command_reports_exit_code() -> None:
    out = run_bang_command("echo boom >&2; exit 3")
    assert "boom" in out and "(exit 3)" in out


def test_run_bang_command_no_output_marker() -> None:
    assert "exit 0" in run_bang_command("true")


def test_run_bang_command_never_raises() -> None:
    out = run_bang_command("definitely-not-a-real-cmd-xyz 2>/dev/null")
    assert isinstance(out, str) and out  # captured failure text, no exception


def test_run_bang_command_truncates() -> None:
    out = run_bang_command("python3 -c 'print(\"x\"*50000)'", max_chars=1000)
    assert len(out) < 1200 and "truncated" in out


def test_bang_context_block_shape() -> None:
    b = bang_context_block("git status", "clean")
    assert "<bash-input>git status</bash-input>" in b
    assert "<bash-output>" in b and "clean" in b


# -- # memory quick-note --------------------------------------------------------


def test_quick_memory_note_creates_entry_and_appends_index(tmp_path, monkeypatch) -> None:
    from mantis_agent.paths import get_memory_index
    # Pre-existing hand-curated index must be APPENDED to, never regenerated.
    idx = get_memory_index()
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text("# Memory index\n\n- [hand-written](hand.md) — keep me\n")
    p = quick_memory_note("always use uv for python installs")
    assert p.exists()
    text = p.read_text()
    assert "always use uv" in text and "type: user" in text
    idx_text = idx.read_text()
    assert "keep me" in idx_text            # curated line survived
    assert p.stem in idx_text                # new line appended


def test_quick_memory_note_slug_collision(tmp_path) -> None:
    p1 = quick_memory_note("same words here")
    p2 = quick_memory_note("same words here")
    assert p1 != p2 and p2.stem.endswith("-2")


# -- custom slash commands -------------------------------------------------------


def _write_cmd(root, name, body, desc="Does things.") -> None:
    d = root / "commands"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\ndescription: {desc}\n---\n{body}")


def test_discover_custom_commands_user_and_project(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    _write_cmd(home, "deploy", "Deploy the app with $ARGUMENTS")
    (proj / ".mantis" / "commands").mkdir(parents=True)
    (proj / ".mantis" / "commands" / "review-pr.md").write_text("Review PR $ARGUMENTS deeply.")
    cmds = discover_custom_commands(proj)
    assert "deploy" in cmds and "review-pr" in cmds
    assert cmds["deploy"][0] == "Does things."
    assert cmds["review-pr"][0] == "custom command"  # no frontmatter → default desc


def test_custom_command_cannot_shadow_builtin(tmp_path) -> None:
    home = tmp_path / "home"
    _write_cmd(home, "model", "Should never load.")
    assert "model" not in discover_custom_commands(tmp_path / "nowhere")


def test_expand_custom_command_arguments(tmp_path) -> None:
    home = tmp_path / "home"
    _write_cmd(home, "deploy", "Deploy the app to $ARGUMENTS now.")
    assert expand_custom_command("/deploy staging") == "Deploy the app to staging now."
    assert expand_custom_command("/deploy") == "Deploy the app to  now."


def test_expand_custom_command_appends_args_without_placeholder(tmp_path) -> None:
    home = tmp_path / "home"
    _write_cmd(home, "lint", "Run the full lint suite.")
    assert expand_custom_command("/lint src/ only") == "Run the full lint suite.\n\nsrc/ only"
    assert expand_custom_command("/lint") == "Run the full lint suite."


def test_expand_custom_command_unknown_is_none(tmp_path) -> None:
    assert expand_custom_command("/nope") is None
    assert expand_custom_command("plain text") is None


def test_expand_slash_prompt_routes_custom(tmp_path) -> None:
    home = tmp_path / "home"
    _write_cmd(home, "ship", "Ship it: $ARGUMENTS")
    assert expand_slash_prompt("/ship v2") == "Ship it: v2"
    assert expand_slash_prompt("/init") is not None  # built-ins still first


def test_all_slash_commands_merges_and_tags(tmp_path) -> None:
    home = tmp_path / "home"
    _write_cmd(home, "deploy", "Deploy.", desc="Ship to prod")
    merged = all_slash_commands()
    assert merged["/deploy"] == "Ship to prod (custom)"
    assert merged["/model"] == SLASH_COMMANDS["/model"]  # built-ins intact
    # /help renders them (uncategorized commands land in the trailing bucket)
    cmds = [c for _, c, _ in build_help_lines(merged)]
    assert "/deploy" in cmds


# -- /status /cost /doctor /permissions -------------------------------------------


class _Rec:
    """Console stub recording plain text."""
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.width = 80
    def print(self, *a, **k) -> None:
        self.lines.append(" ".join(str(x) for x in a))
    def text(self) -> str:
        return "\n".join(self.lines)


def test_show_status_renders_key_facts() -> None:
    t = _tui()
    t.console = _Rec()
    t._show_status(1234, 0.5)
    out = t.console.text()
    assert "gpt-5.4" in out and "api.openai.com" in out
    assert "1,234" in out and "$0.5" in out
    assert "read-only tools auto-run" in out


def test_show_cost_no_pricing_is_graceful() -> None:
    t = MantisTUI(model="totally-unknown-model", backend="http://localhost:11434",
                  api_key=None, system=None, max_tokens=1, temperature=None, max_turns=1)
    t.console = _Rec()
    t._show_cost(0, 0.0)
    assert "no pricing entry" in t.console.text()


def test_show_permissions_lists_rules(monkeypatch, tmp_path) -> None:
    import json
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": ["Bash(git status*)"], "deny": ["Read(.env)"]}}))
    t = _tui()
    t.console = _Rec()
    t._show_permissions()
    out = t.console.text()
    assert "allow" in out and "git status" in out
    assert "deny" in out and ".env" in out
    assert "effect" in out and "mutations ask first" in out


def test_show_permissions_no_rules(monkeypatch) -> None:
    t = _tui()
    t.console = _Rec()
    t._show_permissions()
    assert "no rules configured" in t.console.text()


def test_show_doctor_offline_never_raises(monkeypatch) -> None:
    # Point at an unreachable backend — doctor must degrade to ✗ rows, not raise.
    t = MantisTUI(model="m", backend="http://127.0.0.1:1", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t.console = _Rec()
    t._show_doctor()
    out = t.console.text()
    assert "install" in out and "backend" in out and "unreachable" in out


def test_status_auth_source_env_over_store(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    t = _tui()
    assert "OPENAI_API_KEY" in t._auth_source()
    monkeypatch.delenv("OPENAI_API_KEY")
    # falls back to the generic CLI key
    assert "api-key" in t._auth_source() or "none" in t._auth_source()


# -- bell -------------------------------------------------------------------------


def test_notify_turn_done_thresholds(capsys, monkeypatch) -> None:
    notify_turn_done(2.0)               # short turn → silent
    assert "\a" not in capsys.readouterr().out
    notify_turn_done(30.0)              # long turn → bell
    assert "\a" in capsys.readouterr().out


def test_notify_respects_none_channel(capsys, tmp_path) -> None:
    import json
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"notifChannel": "none"}))
    notify_turn_done(30.0)
    assert "\a" not in capsys.readouterr().out


# -- /update /release-notes + --resume ------------------------------------------


def test_release_notes_reads_changelog() -> None:
    t = _tui()
    t.console = _Rec()
    t._show_release_notes(count=2)
    out = t.console.text()
    # repo checkout has a CHANGELOG.md — the top section must render
    assert "Release notes" in out and "##" in out


def test_run_update_editable_checkout_says_git_pull() -> None:
    t = _tui()
    msg = t._run_update()
    # tests run inside the source checkout → editable guidance, not pip
    assert "editable install" in msg and "git" in msg


def test_resume_flag_lists_sessions(monkeypatch, capsys, tmp_path) -> None:
    from mantis_agent.tui import main
    # empty home → bare --resume exits 1 with a friendly message
    rc = main(["--resume"])
    assert rc == 1
    assert "no past conversations" in capsys.readouterr().err


def test_resume_flag_unknown_id_errors(monkeypatch, capsys) -> None:
    from mantis_agent.tui import main
    rc = main(["--resume", "zzzzzzz"])
    assert rc == 1
    assert "no session matching" in capsys.readouterr().err


# -- live spinner todo block (fullscreen) -----------------------------------------


def test_format_live_todo_rows_shape() -> None:
    from mantis_agent.tui import format_live_todo_rows
    todos = [
        {"content": "Map the codebase", "status": "completed"},
        {"content": "Implement", "activeForm": "Implementing the fix", "status": "in_progress"},
        {"content": "Verify everything works end to end with a really long label", "status": "pending"},
    ]
    rows = format_live_todo_rows(todos, width=50)
    assert len(rows) == 3
    assert "⎿" in rows[0] and "⎿" not in rows[1]      # branch on first row only
    assert "\033[9;90m" in rows[0]                     # done → strikethrough+dim
    assert "Implementing the fix" in rows[1] and "\033[1;32m" in rows[1]  # active bold green
    assert rows[2].endswith("…" + "")                  # long pending label truncated
    assert "…" in rows[2]


def test_format_live_todo_rows_caps_and_counts_overflow() -> None:
    from mantis_agent.tui import format_live_todo_rows
    todos = [{"content": f"t{i}", "status": "pending"} for i in range(12)]
    rows = format_live_todo_rows(todos, width=80, max_rows=8)
    assert len(rows) == 9 and "+4 more" in rows[-1]


# -- time_ago / ellipsize (session picker rows) ------------------------------------


def test_time_ago_buckets() -> None:
    from mantis_agent.tui import time_ago
    now = 1_000_000_000.0
    assert time_ago(now - 5, now=now) == "just now"
    assert time_ago(now - 300, now=now) == "5m ago"
    assert time_ago(now - 3 * 3600, now=now) == "3h ago"
    assert time_ago(now - 2 * 86400, now=now) == "2d ago"
    assert time_ago(now - 3 * 7 * 86400, now=now) == "3w ago"
    assert time_ago(now - 200 * 86400, now=now) == "6mo ago"
    assert time_ago(now - 2 * 365 * 86400, now=now) == "2y ago"
    assert time_ago(now + 999, now=now) == "just now"   # clock skew → never negative


def test_ellipsize() -> None:
    from mantis_agent.tui import ellipsize
    assert ellipsize("short", 10) == "short"
    assert ellipsize("exactly-10", 10) == "exactly-10"
    out = ellipsize("a very long title that should be cut", 12)
    assert out.endswith("…") and len(out) <= 12
    assert ellipsize("multi\n  line   text", 50) == "multi line text"  # collapses ws


# -- resume replays the transcript --------------------------------------------------


def test_replay_transcript_renders_history() -> None:
    from mantis_agent.types import AssistantMessage, TextBlock, ToolResultBlock, UserMessage
    t = _tui()
    from rich.console import Console
    import io
    buf = io.StringIO()
    t.console = Console(file=buf, force_terminal=True, width=80)
    t.messages = [
        UserMessage(content="first question"),
        AssistantMessage(content=[TextBlock(text="the answer")], stop_reason="end_turn"),
        UserMessage(content="<system>meta</system>", isMeta=True),          # skipped
        UserMessage(content=[ToolResultBlock(tool_use_id="x", content="raw")]),  # skipped
        UserMessage(content="second question"),
        AssistantMessage(content=[TextBlock(text="second answer")], stop_reason="end_turn"),
    ]
    t._replay_transcript()
    out = buf.getvalue()
    assert "first question" in out and "the answer" in out
    assert "second question" in out and "second answer" in out
    assert "meta" not in out and "raw" not in out


def test_replay_transcript_caps_and_notes_earlier() -> None:
    from mantis_agent.types import UserMessage
    t = _tui()
    from rich.console import Console
    import io
    buf = io.StringIO()
    t.console = Console(file=buf, force_terminal=True, width=80)
    t.messages = [UserMessage(content=f"msg number {i}") for i in range(40)]
    t._replay_transcript(limit=10)
    out = buf.getvalue()
    assert "msg number 39" in out          # newest shown
    assert "msg number 5" not in out       # oldest hidden
    assert "earlier messages" in out and "30" in out    # and it says so


def test_replay_transcript_empty_is_noop() -> None:
    t = _tui()
    t.console = _Rec()
    t._replay_transcript()
    assert t.console.text() == ""


# -- ctrl+v image paste (fullscreen submit path) -----------------------------------


def test_build_user_content_flushes_attachments(monkeypatch) -> None:
    # the fullscreen submit path must flush pending Ctrl+V attachments into
    # content blocks (it used to send the raw string, dropping the image)
    from mantis_agent.types import ImageBlock, TextBlock
    t = _tui()
    blk = ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "aGk="})
    t.pending_attachments = [("[Image #1]", blk)]
    out = t._build_user_content("describe [Image #1] please")
    assert any(isinstance(b, ImageBlock) for b in out)
    txt = next(b for b in out if isinstance(b, TextBlock))
    assert "[Image #1]" not in txt.text and "describe" in txt.text
    assert t.pending_attachments == []          # consumed


def test_build_user_content_plain_stays_string() -> None:
    t = _tui()
    assert t._build_user_content("just text") == "just text"


def test_build_user_content_attaches_dragged_image_path(tmp_path) -> None:
    """A path dragged into the MIDDLE of a sentence still attaches the image —
    the whole-line check missed it, so the model answered blind."""
    import base64

    from mantis_agent.types import ImageBlock, TextBlock
    png = tmp_path / "shot.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
        "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
    t = _tui()
    out = t._build_user_content(f"what's wrong with {png} here?")
    assert any(isinstance(b, ImageBlock) for b in out)
    # The path stays in the prose — it's how the user refers to the file.
    assert str(png) in next(b for b in out if isinstance(b, TextBlock)).text


def test_build_user_content_leaves_non_image_paths_alone(tmp_path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    t = _tui()
    assert t._build_user_content(f"read {f} for me") == f"read {f} for me"
