"""Wave 3: file-state checkpoints + rewind restore, and auto session titles.
(The fullscreen queue/esc-esc glue lives in closures; their engine pieces —
checkpoint/restore and title generation — are tested here.)"""

from __future__ import annotations

import anyio
import pytest

from mantis_agent.tui import MantisTUI


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


def _tui() -> MantisTUI:
    return MantisTUI(model="gpt-5.4", backend="https://api.openai.com/v1", api_key="k",
                     system=None, max_tokens=1, temperature=None, max_turns=1)


# -- checkpoints + restore -----------------------------------------------------


def test_write_tools_are_wrapped_and_checkpoint(tmp_path) -> None:
    t = _tui()
    t.agent = t._build_agent()
    target = tmp_path / "app.py"
    target.write_text("v1")

    async def go():
        wf = t.agent.tools.get("write_file")
        # simulate mid-turn: message index marks the turn
        t.messages = [1, 2]                                  # index 2
        # write_file guard requires a read first
        await t.agent.tools.get("read_file").fn(path=str(target))
        await wf.fn(path=str(target), content="v2")
    anyio.run(go)
    assert target.read_text() == "v2"
    assert len(t._file_checkpoints) == 1
    ck = t._file_checkpoints[0]
    assert ck["msg_index"] == 2 and ck["backup"] is not None
    # restore: rewind to index 2 → file back to v1
    restored = t._restore_checkpoints(2)
    assert restored == 1 and target.read_text() == "v1"
    assert t._file_checkpoints == []                          # consumed


def test_restore_deletes_files_created_after_the_point(tmp_path) -> None:
    t = _tui()
    t.agent = t._build_agent()
    newfile = tmp_path / "brand_new.py"

    async def go():
        t.messages = [1, 2, 3]                                # index 3
        await t.agent.tools.get("write_file").fn(path=str(newfile), content="hello")
    anyio.run(go)
    assert newfile.exists()
    assert t._file_checkpoints[0]["backup"] is None           # didn't exist before
    t._restore_checkpoints(2)                                 # rewind before it
    assert not newfile.exists()                               # created-after → deleted


def test_restore_only_undoes_at_or_after_index(tmp_path) -> None:
    t = _tui()
    t.agent = t._build_agent()
    f = tmp_path / "f.txt"
    f.write_text("v1")

    async def go():
        rf = t.agent.tools.get("read_file")
        wf = t.agent.tools.get("write_file")
        await rf.fn(path=str(f))
        t.messages = [1]                                      # turn at index 1
        await wf.fn(path=str(f), content="v2")
        t.messages = [1, 2, 3]                                # later turn at index 3
        await rf.fn(path=str(f))
        await wf.fn(path=str(f), content="v3")
    anyio.run(go)
    assert f.read_text() == "v3"
    restored = t._restore_checkpoints(3)                      # undo only the last turn
    assert restored == 1 and f.read_text() == "v2"
    assert len(t._file_checkpoints) == 1                      # first checkpoint remains


def test_edit_file_checkpoints_too(tmp_path) -> None:
    t = _tui()
    t.agent = t._build_agent()
    f = tmp_path / "g.txt"
    f.write_text("alpha beta")

    async def go():
        await t.agent.tools.get("read_file").fn(path=str(f))
        t.messages = [1]
        await t.agent.tools.get("edit_file").fn(
            path=str(f), old_string="beta", new_string="gamma")
    anyio.run(go)
    assert f.read_text() == "alpha gamma"
    t._restore_checkpoints(0)
    assert f.read_text() == "alpha beta"


def test_module_level_tools_not_poisoned() -> None:
    # The wrapped copies must live in the registry only — the shared
    # CODING_TOOLS singletons keep their original fns (other builds/subagents
    # would otherwise double-checkpoint into the wrong TUI).
    from mantis_agent.builtin_tools.fs import write_file
    orig_fn = write_file.fn
    t = _tui()
    t.agent = t._build_agent()
    assert write_file.fn is orig_fn
    assert t.agent.tools.get("write_file").fn is not orig_fn


# -- auto session title ----------------------------------------------------------


def test_autotitle_sets_title_once(monkeypatch, tmp_path) -> None:
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.session_tree import SessionTranscript, new_session_id
    from mantis_agent.types import AssistantMessage, TextBlock, UserMessage

    t = MantisTUI(model="mock", backend="http://localhost:11434", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    prov = MockProvider(default_text="Fix login bug")
    t.agent = type("A", (), {"provider": prov})()
    t.transcript = SessionTranscript(new_session_id())
    titles: list[str] = []
    monkeypatch.setattr(t.transcript, "set_title", titles.append)
    t.messages = [
        UserMessage(content="please fix the login bug in auth.py"),
        AssistantMessage(content=[TextBlock(text="done")], stop_reason="end_turn"),
    ]
    anyio.run(t._maybe_autotitle)
    assert titles == ["Fix login bug"]
    anyio.run(t._maybe_autotitle)          # second call is a no-op
    assert titles == ["Fix login bug"]


def test_autotitle_skips_before_first_turn() -> None:
    from mantis_agent.session_tree import SessionTranscript, new_session_id
    t = _tui()
    t.agent = t._build_agent()
    t.transcript = SessionTranscript(new_session_id())
    t.messages = []
    anyio.run(t._maybe_autotitle)
    assert t._title_done is False          # still eligible later


def test_autotitle_rejects_garbage(monkeypatch) -> None:
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.session_tree import SessionTranscript, new_session_id
    from mantis_agent.types import AssistantMessage, TextBlock, UserMessage
    t = MantisTUI(model="mock", backend="http://localhost:11434", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    prov = MockProvider(default_text="<sub-agent finished with stop_reason=None>")
    t.agent = type("A", (), {"provider": prov})()
    t.transcript = SessionTranscript(new_session_id())
    titles: list[str] = []
    monkeypatch.setattr(t.transcript, "set_title", titles.append)
    t.messages = [UserMessage(content="x"),
                  AssistantMessage(content=[TextBlock(text="y")], stop_reason="end_turn")]
    anyio.run(t._maybe_autotitle)
    assert titles == []                    # marker text never becomes a title


# -- terminal tab title -----------------------------------------------------------


def test_set_terminal_title_emits_osc(monkeypatch, capsys) -> None:
    import sys as _sys
    from mantis_agent.tui import set_terminal_title
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: True)
    set_terminal_title("Create landing page")
    out = capsys.readouterr().out
    assert out == "\x1b]0;Create landing page\x07"


def test_set_terminal_title_skips_non_tty(capsys) -> None:
    from mantis_agent.tui import set_terminal_title
    set_terminal_title("nope")            # pytest's capture isn't a tty
    assert capsys.readouterr().out == ""


def test_set_terminal_title_truncates(monkeypatch, capsys) -> None:
    import sys as _sys
    from mantis_agent.tui import set_terminal_title
    monkeypatch.setattr(_sys.stdout, "isatty", lambda: True)
    set_terminal_title("x" * 500)
    assert len(capsys.readouterr().out) <= 120 + len("\x1b]0;\x07")


def test_autotitle_sets_tab_title(monkeypatch) -> None:
    from mantis_agent.providers.mock import MockProvider
    from mantis_agent.session_tree import SessionTranscript, new_session_id
    from mantis_agent.types import AssistantMessage, TextBlock, UserMessage
    import mantis_agent.tui as tui_mod

    t = MantisTUI(model="mock", backend="http://localhost:11434", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t.agent = type("A", (), {"provider": MockProvider(default_text="Login Fix")})()
    t.transcript = SessionTranscript(new_session_id())
    tabs: list[str] = []
    monkeypatch.setattr(tui_mod, "set_terminal_title", tabs.append)
    t.messages = [UserMessage(content="fix login"),
                  AssistantMessage(content=[TextBlock(text="ok")], stop_reason="end_turn")]
    anyio.run(t._maybe_autotitle)
    assert tabs == ["✳ Login Fix"]


# -- next-prompt suggestion ---------------------------------------------------------


def _twin_msgs():
    from mantis_agent.types import AssistantMessage, TextBlock, UserMessage
    return [UserMessage(content="add retry logic to the fetcher"),
            AssistantMessage(content=[TextBlock(text="Done — retries with backoff added.")],
                             stop_reason="end_turn")]


def test_suggest_next_prompt_returns_line() -> None:
    from mantis_agent.providers.mock import MockProvider
    t = MantisTUI(model="mock", backend="http://localhost:11434", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t.agent = type("A", (), {"provider": MockProvider(default_text="run the tests to verify")})()
    t.messages = _twin_msgs()
    out = anyio.run(t._suggest_next_prompt)
    assert out == "run the tests to verify"


def test_suggest_next_prompt_respects_settings_off(tmp_path) -> None:
    import json
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"suggestNext": False}))
    from mantis_agent.providers.mock import MockProvider
    t = MantisTUI(model="mock", backend="http://localhost:11434", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t.agent = type("A", (), {"provider": MockProvider(default_text="anything")})()
    t.messages = _twin_msgs()
    assert anyio.run(t._suggest_next_prompt) is None


def test_suggest_next_prompt_rejects_garbage() -> None:
    from mantis_agent.providers.mock import MockProvider
    t = MantisTUI(model="mock", backend="http://localhost:11434", api_key=None,
                  system=None, max_tokens=1, temperature=None, max_turns=1)
    t.agent = type("A", (), {"provider": MockProvider(
        default_text="<sub-agent finished with stop_reason=None>")})()
    t.messages = _twin_msgs()
    assert anyio.run(t._suggest_next_prompt) is None


def test_suggest_next_prompt_needs_a_turn() -> None:
    t = _tui()
    t.agent = t._build_agent()
    t.messages = []
    assert anyio.run(t._suggest_next_prompt) is None
