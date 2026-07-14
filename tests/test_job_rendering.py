"""Background-job completion rendering is compact and Claude-Code-like."""

from __future__ import annotations

from dataclasses import dataclass

from mantis_agent.tui import format_job_completion_line


@dataclass
class FakeJob:
    id: int = 3
    status: str = "done"
    desc: str = "research the parser failure and summarize root cause"
    result: str = "all good"
    elapsed_s: float = 12.8
    turn_count: int = 2
    tool_count: int = 5


def test_job_completion_line_includes_live_metrics_and_context_note() -> None:
    line = format_job_completion_line(FakeJob(), width=90)

    assert "⦿ job" in line
    assert "✓ done" in line
    assert "#3 · 12s · 2 turns · 5 tools" in line
    assert "research the parser failure" in line
    assert "result added to context" in line


def test_job_completion_line_surfaces_failure_preview_and_escapes_markup() -> None:
    line = format_job_completion_line(
        FakeJob(status="error", desc="run [red]danger[/]", result="RuntimeError: bad [x]"),
        width=70,
    )

    assert "✗ error" in line
    assert "run \\[red]danger\\[/]" in line
    assert "RuntimeError: bad \\[x]" in line
    assert "result added to context" not in line
