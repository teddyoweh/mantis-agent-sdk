"""``/workflows`` in the terminal: registration, subcommands, job/run identity.

Uses the real :class:`MantisTUI` (cheap to construct — no provider is built
until a turn runs) with an injected fake agent runner, so nothing reaches a
model. MANTIS_AGENT_HOME points at tmp_path.
"""

from __future__ import annotations

import asyncio

import pytest

from mantis_agent.tui import MantisTUI
from mantis_agent.types import AssistantMessage, TextBlock, Usage
from mantis_agent.workflow import Workflow


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MANTIS_AGENT_DISABLE_WORKFLOWS", raising=False)
    yield


def _tui() -> MantisTUI:
    return MantisTUI(model="mock", backend="mock", api_key=None, system=None,
                     max_tokens=100, temperature=None, max_turns=10)


async def _runner(prompt, *, model, agent_type, schema=None):
    yield AssistantMessage(content=[TextBlock(text="ok")],
                           usage=Usage(input_tokens=5, output_tokens=2))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_register_workflow_exposes_run_and_handle():
    tui = _tui()
    wf = Workflow("demo", agent_runner=_runner)
    tui._register_workflow(wf)

    assert tui._workflows == [wf.run]
    assert tui._workflow_handle(wf.run) is wf
    # idempotent — a re-registration must not duplicate the row
    tui._register_workflow(wf)
    assert len(tui._workflows) == 1


def test_registration_ages_out_the_oldest_runs():
    tui = _tui()
    made = [Workflow(f"w{i}", agent_runner=_runner)
            for i in range(tui._MAX_SESSION_WORKFLOWS + 3)]
    for wf in made:
        tui._register_workflow(wf)
    assert len(tui._workflows) == tui._MAX_SESSION_WORKFLOWS
    # the oldest handles are released with their rows
    assert tui._workflow_handle(made[0].run) is None
    assert tui._workflow_handle(made[-1].run) is made[-1]


def test_a_history_run_has_no_handle():
    tui = _tui()
    from mantis_agent.workflow import WorkflowRun

    assert tui._workflow_handle(WorkflowRun(id="wold", name="x")) is None


# ---------------------------------------------------------------------------
# subcommand dispatch
# ---------------------------------------------------------------------------


def test_bare_workflows_opens_the_viewer():
    assert _tui()._cmd_workflows_sub("") is False


def test_list_shows_builtins_with_their_source(capsys):
    assert _tui()._cmd_workflows_sub("list") is True
    out = capsys.readouterr().out
    assert "review" in out and "builtin" in out
    assert "needs target" in out
    assert "/workflows run <name>" in out


def test_unknown_subcommand_explains_the_options(capsys):
    assert _tui()._cmd_workflows_sub("frobnicate") is True
    out = capsys.readouterr().out
    assert "unknown /workflows" in out and "resume <run-id>" in out


def test_export_writes_a_json_file_next_to_you(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tui = _tui()
    wf = Workflow("review", agent_runner=_runner)
    tui._register_workflow(wf)

    tui._cmd_workflows_sub("export")            # defaults to the latest run
    assert "exported →" in capsys.readouterr().out
    written = list(tmp_path.glob("workflow-review-*.json"))
    assert len(written) == 1

    import json
    assert json.loads(written[0].read_text())["id"] == wf.run.id


def test_export_falls_back_to_the_durable_store(capsys, tmp_path, monkeypatch):
    from mantis_agent.workflow import WorkflowRun
    from mantis_agent.workflow_store import save_run

    save_run(WorkflowRun(id="wpast", name="research", status="done"),
             definition="research")
    monkeypatch.chdir(tmp_path)
    _tui()._cmd_workflows_sub("export wpast")
    assert "exported →" in capsys.readouterr().out
    assert list(tmp_path.glob("workflow-research-*.json"))


def test_export_of_an_unknown_run_explains_itself(capsys):
    _tui()._cmd_workflows_sub("export wnope")
    assert "no run wnope" in capsys.readouterr().out


def test_export_with_nothing_to_export_teaches_history(capsys):
    _tui()._cmd_workflows_sub("export")
    assert "nothing to export" in capsys.readouterr().out


def test_history_is_empty_but_says_where_runs_are_kept(capsys):
    _tui()._cmd_workflows_sub("history")
    assert "no saved workflow runs yet" in capsys.readouterr().out


def test_history_lists_persisted_runs(capsys):
    from mantis_agent.workflow import Phase, WorkflowRun
    from mantis_agent.workflow_store import save_run

    save_run(WorkflowRun(id="wabc", name="review", status="done",
                         phases=[Phase(title="P")]), definition="review")
    _tui()._cmd_workflows_sub("history")
    out = capsys.readouterr().out
    assert "wabc" in out and "review" in out
    assert "/workflows resume" in out


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def test_parse_key_value_args_and_loose_objective():
    tui = _tui()
    assert tui._parse_workflow_args("review target=agent.py") == (
        "review", {"target": "agent.py"})
    name, inputs = tui._parse_workflow_args('design objective="two words" x=1')
    assert name == "design" and inputs == {"objective": "two words", "x": "1"}
    # loose words become the objective
    assert tui._parse_workflow_args("understand the auth flow") == (
        "understand", {"objective": "the auth flow"})
    assert tui._parse_workflow_args("") == ("", {})


# ---------------------------------------------------------------------------
# running from the terminal
# ---------------------------------------------------------------------------


def test_run_reports_missing_arguments(capsys):
    _tui()._cmd_workflows_sub("run")
    assert "usage: /workflows run" in capsys.readouterr().out


def test_run_of_an_unknown_workflow_lists_the_real_ones(capsys):
    _tui()._cmd_workflows_sub("run ghost")
    out = capsys.readouterr().out
    assert "no workflow 'ghost'" in out and "review" in out


def test_run_requires_declared_inputs(capsys):
    _tui()._cmd_workflows_sub("run review")
    assert "needs input(s): target" in capsys.readouterr().out


def test_disabled_env_blocks_a_terminal_run(capsys, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_DISABLE_WORKFLOWS", "1")
    _tui()._cmd_workflows_sub("run review target=x")
    assert "workflows are disabled" in capsys.readouterr().out


def test_run_backgrounds_the_workflow_and_links_job_to_run(capsys, monkeypatch):
    tui = _tui()
    # Swap the child-agent factory for a stub: the command is what's under test.
    monkeypatch.setattr("mantis_agent.workflow.make_agent_runner",
                        lambda **kw: _runner)

    async def go():
        tui._cmd_workflows_sub("run review target=agent.py")
        job = tui._jobs.all()[0]
        await tui._jobs.wait(job.id, timeout_s=10)
        return job

    job = asyncio.run(go())
    out = capsys.readouterr().out

    assert "▶ workflow review" in out and f"job #{job.id}" in out
    assert "/workflows to watch" in out
    # one run, registered, with both halves of its identity wired up
    assert len(tui._workflows) == 1
    run = tui._workflows[0]
    assert run.id == job.workflow_id
    assert run.job_id == job.id
    assert job.kind == "workflow"
    assert job.status == "done"
    assert "# Workflow report — review" in job.result
    assert run.status == "done"

    # and it is inspectable afterwards, from history
    from mantis_agent.workflow_store import list_runs
    assert [r["run_id"] for r in list_runs()] == [run.id]


def test_resume_from_an_unknown_run_is_explained(capsys):
    _tui()._cmd_workflows_sub("resume wnope")
    assert "no saved run wnope" in capsys.readouterr().out


def test_resume_replays_a_persisted_run(capsys, monkeypatch):
    tui = _tui()
    monkeypatch.setattr("mantis_agent.workflow.make_agent_runner",
                        lambda **kw: _runner)

    async def first():
        tui._cmd_workflows_sub("run review target=agent.py")
        job = tui._jobs.all()[0]
        await tui._jobs.wait(job.id, timeout_s=10)

    asyncio.run(first())
    capsys.readouterr()

    async def second():
        tui._cmd_workflows_sub("resume " + tui._workflows[0].id)
        job = tui._jobs.all()[-1]
        await tui._jobs.wait(job.id, timeout_s=10)
        return job

    job = asyncio.run(second())
    out = capsys.readouterr().out
    assert "replayed free" in out
    assert "replayed from a previous run" in job.result
    assert len(tui._workflows) == 2


# ---------------------------------------------------------------------------
# /jobs ↔ /workflows identity
# ---------------------------------------------------------------------------


def test_jobs_list_names_the_workflow_run(capsys):
    tui = _tui()

    async def go():
        async def never():
            await asyncio.sleep(5)
            return "x"

        job = tui._jobs.spawn(never(), desc="workflow review", kind="workflow",
                              workflow_id="wxyz")
        tui._cmd_jobs("")
        tui._cmd_job(str(job.id))
        tui._jobs.cancel(job.id)

    asyncio.run(go())
    out = capsys.readouterr().out
    assert "run wxyz" in out
    assert "/workflows to see its phases" in out
