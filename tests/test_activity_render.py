"""The rail, the footer roll-up and the monitor drill-down.

These render pure strings so the exact bytes can be asserted without standing up
a prompt_toolkit application — the same split that lets ``_footer_line`` be
tested apart from ``footer_ft``.
"""

from __future__ import annotations

import asyncio

from mantis_agent.activity.render import (
    RailItem,
    footer_counts,
    monitor_detail,
    rail_item_from_job,
    rail_items_from_jobs,
    rail_line,
    rail_rows,
)
from mantis_agent.jobs import JobManager

SCRIPT = (
    "prev=up\n"
    "while true; do\n"
    "  code=$(curl -s -o /dev/null -w '%{http_code}' https://www.google.com || echo 000)\n"
    "  if [ \"$code\" = \"200\" ]; then cur=up; else cur=down; fi\n"
    "done"
)


def _monitor(**kw) -> RailItem:
    base = dict(
        id="job:4", kind="watch", label="Monitor google.com status",
        detail="google.com up/down status changes", elapsed_s=8.0, script=SCRIPT,
    )
    base.update(kw)
    return RailItem(**base)


# --------------------------------------------------------------------------
# footer roll-up
# --------------------------------------------------------------------------


def test_footer_counts_reads_like_the_status_line() -> None:
    items = [
        _monitor(),
        RailItem(id="wfr:1", kind="workflow", label="wf"),
        *[RailItem(id=f"sub:{i}", kind="task", label=f"a{i}") for i in range(3)],
    ]
    assert footer_counts(items) == "1 monitor · 1 workflow · 3 agents"


def test_footer_is_empty_when_nothing_runs() -> None:
    # An ordinary chat session that never spawned anything must not grow chrome.
    assert footer_counts([]) == ""
    assert footer_counts([_monitor(status="done")]) == ""


def test_footer_singular_and_plural() -> None:
    assert footer_counts([_monitor()]) == "1 monitor"
    assert footer_counts([_monitor(), _monitor(id="job:5")]) == "2 monitors"


def test_terminal_work_is_excluded_from_live_counts() -> None:
    live = [_monitor(), RailItem(id="job:9", kind="task", label="x", status="error")]
    assert footer_counts(live) == "1 monitor"
    assert "1 agent" in footer_counts(live, live_only=False)


# --------------------------------------------------------------------------
# rail rows
# --------------------------------------------------------------------------


def test_rail_line_keeps_the_facts_and_clips_the_label() -> None:
    item = RailItem(
        id="wfr:1", kind="workflow", label="mantis-foundations",
        detail="Build the foundational first slice of 10 of the 21 feature plans",
        progress="10/20 agents done", elapsed_s=2751.0, tokens=245200,
    )
    line = rail_line(item, width=92)
    # The right-hand facts answer "should I wait for this?" and are never the
    # part that gets truncated.
    assert "10/20 agents done" in line
    assert "45m 51s" in line
    assert "245.2k tokens" in line
    assert len(line) <= 92
    assert "…" in line  # the label/detail absorbed the clipping


def test_rail_line_without_facts_is_just_the_label() -> None:
    line = rail_line(RailItem(id="x", kind="task", label="explore"), width=40)
    assert line.endswith("explore")


def test_rail_rows_marks_the_selection_and_caps_the_list() -> None:
    items = [RailItem(id=f"s{i}", kind="task", label=f"agent {i}") for i in range(9)]
    rows = rail_rows(items, width=60, sel=2, limit=4)
    assert rows[2].startswith("❯ ")
    assert rows[0].startswith("  ")
    assert rows[-1] == "  +5 more"


def test_rail_rows_without_limit_shows_everything() -> None:
    items = [RailItem(id=f"s{i}", kind="task", label=f"a{i}") for i in range(3)]
    rows = rail_rows(items, width=60)
    assert len(rows) == 3
    assert not any("more" in r for r in rows)


# --------------------------------------------------------------------------
# monitor drill-down
# --------------------------------------------------------------------------


def test_monitor_detail_is_titled_by_the_user_facing_name() -> None:
    # The tool and the job kind are both `watch`, but the pane a user opens is
    # "Monitor details" — "Watch details" reads as an imperative.
    assert monitor_detail(_monitor()).startswith("Monitor details")


def test_monitor_detail_shows_the_script_verbatim() -> None:
    out = monitor_detail(_monitor(), width=100)
    assert "Status:   running" in out
    assert "Runtime:  8s" in out
    assert "prev=up" in out
    assert "while true; do" in out
    assert "No output available" in out


def test_monitor_detail_clips_long_script_lines_to_width() -> None:
    long_line = "x" * 500
    out = monitor_detail(_monitor(script=long_line), width=80)
    assert all(len(line) <= 80 for line in out.splitlines()), "a line overran the pane"


def test_monitor_detail_truncates_a_long_script_with_a_count() -> None:
    out = monitor_detail(_monitor(script="\n".join(f"line {i}" for i in range(40))),
                         max_script_lines=5)
    assert "+34 more lines" in out


def test_monitor_detail_tails_output_when_present() -> None:
    out = monitor_detail(_monitor(output="\n".join(f"evt {i}" for i in range(50))),
                         max_output_lines=3)
    assert "evt 49" in out
    assert "evt 0" not in out
    assert "No output available" not in out


# --------------------------------------------------------------------------
# projection off the real engine
# --------------------------------------------------------------------------


def test_projection_from_a_real_job_manager() -> None:
    async def go() -> list[RailItem]:
        jm = JobManager()

        async def forever() -> str:
            await asyncio.sleep(60)
            return ""

        mon = jm.spawn(forever(), desc="Monitor google.com status", kind="watch")
        mon.script = SCRIPT
        jm.spawn(forever(), desc="explore", kind="task")
        await asyncio.sleep(0.01)
        try:
            return rail_items_from_jobs(jm)
        finally:
            jm.cancel_all()

    items = asyncio.run(go())
    assert footer_counts(items) == "1 monitor · 1 agent"
    # Monitors sort first — a still-running monitor is what users forget about.
    assert items[0].kind == "watch"
    assert items[0].script == SCRIPT
    assert "stop" in items[0].actions
    assert monitor_detail(items[0]).startswith("Monitor details")


def test_projection_tolerates_a_job_missing_optional_fields() -> None:
    class Bare:
        id = 7
        desc = "bare"

    item = rail_item_from_job(Bare())
    assert item.id == "job:7"
    assert item.label == "bare"
    assert item.kind == "job"
    assert item.script == ""
