"""Full reports survive workflow ingestion, coordination, and replay."""

import anyio
import pytest

from mantis_agent.types import AssistantMessage, TextBlock, ToolUseBlock
from mantis_agent.workflow import Workflow, WorkflowRun
from mantis_agent.workflow_defs import cache_key
from mantis_agent.workflow_store import replay_cache


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))


def answer(text):
    return AssistantMessage(content=[TextBlock(text=text)])


REPORT = "Detailed evidence with unicode ✓.\n" * 40 + "VERDICT: PASS"


def test_full_last_answer_replaces_commentary_and_is_visible_during_ingestion():
    snapshots = []
    final = "\n  " + REPORT + "\n"

    async def runner(prompt, **kwargs):
        yield answer("Intermediate commentary, not the answer.")
        yield AssistantMessage(content=[
            TextBlock(text=final[:300]), TextBlock(text=final[300:]),
        ])
        yield AssistantMessage(content=[
            ToolUseBlock(id="t1", name="read_file", input={}),
        ])
        yield answer(" \n")

    async def exercise():
        wf = Workflow("lossless", agent_runner=runner,
                      on_event=lambda run: snapshots.append(run.to_dict()))
        assert await wf.agent("inspect") == final
        return wf.run.all_agents()[0]

    ar = anyio.run(exercise)
    assert ar.result == final
    assert ar.summary == final[:197] + "…"
    assert ar.status == "done"
    assert any(a["status"] == "running" and a["result"] == final
               for snapshot in snapshots for phase in snapshot["phases"]
               for a in phase["agents"])


def test_coordinate_synthesis_and_verifier_receive_full_reports():
    reports = {"first": "First worker\n" + REPORT,
               "second": "Second worker\n" + REPORT}
    verifier_prompts = []

    async def runner(prompt, *, agent_type, **kwargs):
        yield answer("Commentary only.")
        if agent_type == "verify":
            verifier_prompts.append(prompt)
            yield answer(REPORT)
        else:
            yield answer(reports[prompt])

    async def exercise():
        wf = Workflow("coordinate", agent_runner=runner)
        return await wf.coordinate("audit", [
            {"label": key, "prompt": key} for key in reports
        ])

    synthesis = anyio.run(exercise)
    assert [f["report"] for f in synthesis["findings"]] == list(reports.values())
    assert all(report in verifier_prompts[0] for report in reports.values())
    assert "Commentary only." not in verifier_prompts[0]
    assert synthesis["verification"] == REPORT
    assert synthesis["verdict"] == "PASS"


def test_full_result_serialization_disk_and_cache_replay(tmp_path):
    async def runner(prompt, **kwargs):
        yield answer(REPORT)

    async def exercise():
        wf = Workflow("original", agent_runner=runner)
        await wf.agent("inspect", label="worker", phase="Research")
        wf.finish()
        restored = WorkflowRun.from_dict(wf.run.to_dict())
        assert restored.all_agents()[0].result == REPORT
        loaded = Workflow.load(wf.save(tmp_path / "run.json"))
        assert loaded.all_agents()[0].result == REPORT
        cache = replay_cache({"run": loaded.to_dict()})
        replay = Workflow("replayed")  # No runner: must use cache.
        result = await replay.agent(
            "inspect", label="worker", phase="Research",
            cached=cache[cache_key("Research", "worker", "inspect")],
        )
        assert result == REPORT
        ar = replay.run.all_agents()[0]
        assert ar.result == REPORT
        assert ar.summary == REPORT[:197] + "…"
        assert ar.replayed and ar.status == "done"

    anyio.run(exercise)


def test_error_keeps_partial_output_but_raises_and_records_failure():
    async def runner(prompt, **kwargs):
        yield answer(REPORT)
        raise RuntimeError("worker failed")

    async def exercise():
        wf = Workflow("error", agent_runner=runner)
        with pytest.raises(RuntimeError, match="worker failed"):
            await wf.agent("inspect")
        ar = wf.run.all_agents()[0]
        assert ar.status == "error"
        assert ar.error == "RuntimeError: worker failed"
        assert ar.result == REPORT
        assert replay_cache({"run": wf.run.to_dict()}) == {}

    anyio.run(exercise)


def test_cancellation_does_not_return_partial_report_as_success():
    async def exercise():
        started = anyio.Event()
        returned = []

        async def runner(prompt, **kwargs):
            yield answer(REPORT)
            started.set()
            await anyio.sleep_forever()

        wf = Workflow("cancel", agent_runner=runner)

        async def run_agent():
            returned.append(await wf.agent("inspect"))

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(run_agent)
                await started.wait()
                wf.cancel(wf.run.all_agents()[0].id)
        ar = wf.run.all_agents()[0]
        assert returned == [""]
        assert ar.status == "cancelled"
        assert ar.error is None
        assert ar.result == REPORT  # Inspectable, not a successful cached result.
        assert replay_cache({"run": wf.run.to_dict()}) == {}

    anyio.run(exercise)
