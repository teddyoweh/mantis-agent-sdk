"""Terminal opt-in and transcript-bound evidence lifecycle (no provider calls)."""
from types import SimpleNamespace

import anyio
import pytest

from mantis_agent.tui import MODES, MantisTUI


@pytest.fixture
def tui(tmp_path, monkeypatch):
    import mantis_agent.session_tree  # ensure its path import precedes patching
    monkeypatch.setattr("mantis_agent.paths.get_project_dir", lambda cwd=None: tmp_path)
    monkeypatch.setattr(mantis_agent.session_tree, "get_project_dir", lambda cwd=None: tmp_path)
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    app = MantisTUI(model="qwen3:32b", backend="http://localhost:11434",
                    api_key=None, system=None, max_tokens=1,
                    temperature=None, max_turns=1)
    app.transcript = SimpleNamespace(session_id="first")
    return app


def test_full_build_and_rebuild(tui, tmp_path):
    agent = tui._build_agent()
    assert agent.artifact_store.root == tmp_path / "artifacts"
    assert agent.task_state.path == tmp_path / "task-state/first.json"
    assert {"read_artifact", "task_state"} <= {t.name for t in agent.tools}
    assert "actual tool call IDs" in agent.system
    agent.task_state.set_objective("keep this")
    assert tui._build_agent().task_state is agent.task_state


def test_slim_has_no_harness(tui, tmp_path):
    tui.model = "qwen2.5-coder:7b"
    agent = tui._build_agent()
    assert agent.task_state is None and agent.artifact_store is None
    assert "task_state" not in {t.name for t in agent.tools}
    assert not (tmp_path / "artifacts").exists()


def test_build_before_transcript(tui, tmp_path):
    tui.transcript = None
    agent = tui._build_agent()
    assert tui.transcript.session_id
    assert agent.task_state.path.name == tui.transcript.session_id + ".json"


def test_session_switch_and_resume(tui):
    tui.agent = tui._build_agent()
    first = tui.agent.task_state
    first.set_objective("first task")
    old_tool = tui.agent.tools.get("task_state")
    tui.transcript = SimpleNamespace(session_id="branch")
    tui._bind_harness_state()
    assert tui.agent.task_state.inspect()["objective"] == ""
    assert tui.agent.tools.get("task_state") is not old_tool
    tui.agent.task_state.set_objective("branch task")
    tui.transcript = SimpleNamespace(session_id="first")
    tui._bind_harness_state()
    assert tui.agent.task_state.inspect()["objective"] == "first task"


def test_clear_starts_new_state(tui, monkeypatch):
    monkeypatch.setattr("mantis_agent.tui.print_banner", lambda *args: None)
    tui.agent = tui._build_agent()
    tui.agent.task_state.set_objective("old task")
    assert anyio.run(tui._handle_slash, "/clear")
    assert tui.transcript.session_id != "first"
    tui._bind_harness_state()
    assert tui.agent.task_state.inspect()["objective"] == ""


def test_corrupt_resume_fails_without_overwrite(tui, tmp_path):
    tui.agent = tui._build_agent()
    path = tmp_path / "task-state/broken.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text('{"version": 999}')
    tui.transcript = SimpleNamespace(session_id="broken")
    with pytest.raises(RuntimeError, match="has not been reset"):
        tui._bind_harness_state()
    assert path.read_text() == '{"version": 999}'


def test_sanitized_path(tui, tmp_path):
    tui.transcript = SimpleNamespace(session_id="../../other/session")
    _, state = tui._harness_state()
    assert state.path.parent == tmp_path / "task-state"
    assert state.path.name == ".._.._other_session.json"


def test_harness_tools_obey_permissions(tui):
    from mantis_agent.permissions import Allow, Deny
    agent = tui._build_agent()
    tui.mode_idx = [m[0] for m in MODES].index("plan mode on")
    assert isinstance(anyio.run(tui._permit, agent.tools.get("task_state"), {}, None), Deny)
    assert isinstance(anyio.run(tui._permit, agent.tools.get("read_artifact"), {}, None), Allow)


def test_delegation_kits_do_not_inherit_bound_state(tui, monkeypatch):
    from mantis_agent import subagent
    original = subagent.make_task_tool
    captured = []

    def capture(**kwargs):
        captured.extend(kwargs["tools"])
        return original(**kwargs)

    monkeypatch.setattr(subagent, "make_task_tool", capture)
    tui.agent = tui._build_agent()
    tui.transcript = SimpleNamespace(session_id="second")
    tui._bind_harness_state()
    assert captured
    assert not {"task_state", "read_artifact"} & {t.name for t in captured}
