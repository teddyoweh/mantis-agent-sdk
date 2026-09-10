import asyncio
import hashlib
import json

import pytest

from mantis_agent.task_state import TaskState, make_task_state_tool
from mantis_agent.types import ToolResultBlock, ToolUseBlock


def observe(state, ident="run", *, error=False, output="1 test passed"):
    state.observe(
        [ToolUseBlock(id=ident, name="bash", input={"command": "pytest"})],
        [ToolResultBlock(tool_use_id=ident, content=output, is_error=error)],
    )


def test_durable_reload(tmp_path):
    path = tmp_path / "nested" / "state.json"
    state = TaskState(path)
    state.set_objective("Ship a tested fix")
    state.add_check("tests", "Tests pass")
    observe(state)
    state.record_check("tests", "passed", ["run"])
    state.record_hypothesis("cause", "Missing validation", "rejected", ["run"])
    loaded = TaskState(path)
    assert loaded.inspect() == state.inspect()
    assert loaded.progress_count == 2
    assert not loaded.has_unfinished_work
    assert (
        loaded.inspect()["evidence"]["run"]["output_sha256"]
        == hashlib.sha256(b"1 test passed").hexdigest()
    )
    assert "not automatically proven" in loaded.render()


@pytest.mark.parametrize(
    "error,output", [(True, "failed"), (False, '{"exit_code": 1}'), (False, '{"returncode": 2}')]
)
def test_failed_evidence_denied(error, output):
    state = TaskState()
    state.add_check("a", "Criterion")
    observe(state, error=error, output=output)
    with pytest.raises(ValueError, match="Failed execution"):
        state.record_check("a", "passed", ["run"])
    assert state.progress_count == 0
    state.record_check("a", "failed", ["run"])
    assert state.has_unfinished_work


def test_reused_call_id_reopens_certified_check():
    state = TaskState()
    state.add_check("tests", "Tests pass")
    observe(state)
    state.record_check("tests", "passed", ["run"])
    observe(state, error=True, output="regression")
    assert state.inspect()["checks"]["tests"]["status"] == "open"
    assert state.has_unfinished_work
    assert state.progress_count == 1
    with pytest.raises(ValueError, match="Failed execution"):
        state.record_check("tests", "passed", ["run"])


def test_nonexistent_and_self_evidence_denied():
    state = TaskState()
    state.add_check("a", "Criterion")
    state.observe(
        [ToolUseBlock(id="self", name="task_state", input={})],
        [
            ToolResultBlock(tool_use_id="self", content="ok"),
            ToolResultBlock(tool_use_id="orphan", content="ok"),
        ],
    )
    for ids in (["invented"], ["self"], ["orphan"], []):
        with pytest.raises(ValueError):
            state.record_check("a", "passed", ids)
    with pytest.raises(ValueError):
        state.record_hypothesis("h", "Claim", "supported", ["invented"])
    assert state.inspect()["evidence"] == {}


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        "{}",
        '{"version":1,"version":1}',
        '{"version":true,"objective":"","checks":{},"hypotheses":{},"evidence":{}}',
    ],
)
def test_malformed_files(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        TaskState(path)
    assert path.read_text() == content


def test_loaded_reference_validation(tmp_path):
    state = TaskState()
    state.add_check("a", "Criterion")
    data = state.inspect()
    data["checks"]["a"].update(status="passed", credited=True, evidence_call_ids=["fake"])
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        TaskState(path)


def test_progress_is_unique_and_descriptions_immutable():
    state = TaskState()
    state.add_check("a", "Criterion")
    observe(state)
    for _ in range(4):
        state.record_check("a", "failed", ["run"])
        state.record_check("a", "passed", ["run"])
        state.record_hypothesis("h", "Claim", "supported", ["run"])
        state.record_hypothesis("h", "Claim", "open")
    assert state.progress_count == 2
    assert state.has_unfinished_work
    with pytest.raises(ValueError):
        state.add_check("a", "Different criterion")
    with pytest.raises(ValueError):
        state.record_hypothesis("h", "Different claim")
    observe(state, error=True)
    assert state.inspect()["evidence"]["run"]["outcome"] == "error"
    assert state.progress_count == 2


def test_bounded_records_and_rendering(tmp_path):
    state = TaskState(tmp_path / "state.json")
    state.set_objective("x" * 1000)
    for i in range(64):
        state.add_check(str(i), "c" * 500)
        state.record_hypothesis(str(i), "h" * 500)
    with pytest.raises(ValueError):
        state.add_check("overflow", "No room")
    for i in range(140):
        observe(state, str(i), output="x" * 3000)
    assert len(state.inspect()["evidence"]) == 128
    assert len(state.render()) <= 6000
    assert "Recent execution evidence" in state.render()
    assert "Uncertainties" in state.render()
    assert TaskState(state.path).inspect() == state.inspect()


def test_atomic_failure_keeps_memory_and_file(tmp_path, monkeypatch):
    state = TaskState(tmp_path / "state.json")
    state.set_objective("Original")
    before = state.path.read_bytes()

    def fail(*args):
        raise OSError("disk failed")

    monkeypatch.setattr("mantis_agent.task_state.os.replace", fail)
    with pytest.raises(OSError):
        state.set_objective("New")
    assert state.inspect()["objective"] == "Original"
    assert state.path.read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 1


def test_tool_api_and_detached_snapshot():
    state = TaskState()
    tool = make_task_state_tool(state)
    assert tool.name == "task_state"
    assert tool.is_concurrency_safe is False
    asyncio.run(tool.fn(operation="add_check", id="a", description="Criterion"))
    snapshot = json.loads(asyncio.run(tool.fn(operation="inspect")))
    snapshot["checks"].clear()
    assert state.has_unfinished_work
    with pytest.raises(ValueError):
        asyncio.run(tool.fn(operation="observe"))
