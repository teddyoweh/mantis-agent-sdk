"""``/dash`` — the in-terminal mini dashboard, and the one-line form ``/status``
opens with.

Rendered to a recording console at a fixed width and asserted on as text, so
the panel is checked the way a user sees it: does it fit 80 columns, does it
say the model, the family, the auth source, the context fill, the cost, what is
running, and what was edited.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from mantis_agent import catalog
from mantis_agent.tui import (
    MantisTUI,
    ctx_bar,
    ctx_fill_color,
    dashboard_line,
    render_dashboard,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _console(width: int = 80) -> Console:
    return Console(width=width, record=True, force_terminal=False, color_system=None)


def _tui(model: str = "claude-sonnet-5", backend: str = "https://api.anthropic.com/v1",
         api_key: str | None = "sk-ant-x") -> MantisTUI:
    t = MantisTUI(model=model, backend=backend, api_key=api_key, system=None,
                  max_tokens=1, temperature=None, max_turns=1)
    t.console = _console()
    return t


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """No saved keys and no provider env so auth states are deterministic."""
    monkeypatch.setattr(catalog, "saved_key", lambda pid: None)
    for p in catalog.CATALOG:
        for var in (p.api_key_env, *p.key_env_aliases):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _facts(**over: Any) -> dict:
    base = {
        "version": "2.62.0",
        "model": "claude-sonnet-5",
        "backend": "https://api.anthropic.com/v1",
        "family": {"id": "anthropic", "glyph": "✦", "name": "Claude",
                   "provider_id": "anthropic", "provider_label": "Claude (Anthropic)"},
        "auth": "OAuth token",
        "ctx_used": 76000, "ctx_window": 200000,
        "breakdown": {"system": 2100, "context": 400, "conversation": 73500, "total": 76000},
        "cost": 0.034, "tokens_in": 401000, "tokens_out": 31000,
        "messages": 12, "elapsed_s": 245,
        "mode": "accept edits on", "effort": "high", "sandbox": "on",
        "jobs": {"total": 3, "running": 1, "failed": 1},
        "workflows": {"total": 1, "running": 1, "failed": 0},
        "mcp": {"servers": 2, "connected": 2, "tools": 14},
        "skills": 3, "tools": 31,
        "edits": ["mantis_agent/tui.py", "tests/test_dash.py"],
    }
    base.update(over)
    return base


def _lines(width: int = 80, **over: Any) -> list[str]:
    c = _console(width)
    c.print(render_dashboard(_facts(**over), width))
    return c.export_text().splitlines()


# -- the panel ----------------------------------------------------------------


def test_panel_fits_80_columns_and_24_rows() -> None:
    lines = _lines(80)
    assert lines, "nothing rendered"
    assert max(len(ln) for ln in lines) <= 80
    assert len(lines) <= 12  # leaves the transcript and the prompt room on 80x24


def test_panel_says_model_family_and_auth() -> None:
    text = "\n".join(_lines(80))
    assert "✦ claude-sonnet-5" in text
    assert "Claude (Anthropic)" in text
    assert "OAuth token" in text


def test_panel_context_bar_is_proportional_with_tokens_and_percent() -> None:
    text = "\n".join(_lines(80))
    assert "38%" in text                      # 76k / 200k
    assert "76k / 200k" in text
    assert "124k free" in text
    row = next(ln for ln in text.splitlines() if "context" in ln)
    bar = re.search(r"[█░]+", row).group(0)
    assert bar.count("█") == round(0.38 * len(bar))
    assert "system 2.1k" in text and "conversation 74k" in text


def test_panel_session_cost_tokens_and_mode() -> None:
    text = "\n".join(_lines(80))
    assert "$0.03" in text
    assert "401k in" in text and "31k out" in text
    assert "12 messages" in text and "4m" in text
    assert "accept edits on" in text and "effort=high" in text and "sandbox on" in text


def test_panel_jobs_workflows_mcp_skills_and_edits() -> None:
    text = "\n".join(_lines(80))
    assert "jobs 3 (1 running · 1 failed)" in text
    assert "workflows 1 (1 running)" in text
    assert "mcp 2/2 servers · 14 tools" in text
    assert "skills 3" in text
    assert "mantis_agent/tui.py · tests/test_dash.py" in text


def test_panel_scales_up_with_a_wider_terminal() -> None:
    narrow = next(ln for ln in _lines(80) if "context" in ln)
    wide = next(ln for ln in _lines(120) if "context" in ln)
    assert len(re.search(r"[█░]+", wide).group(0)) > len(re.search(r"[█░]+", narrow).group(0))
    assert max(len(ln) for ln in _lines(120)) <= 120


def test_panel_before_any_turn_and_with_nothing_running() -> None:
    text = "\n".join(_lines(80, ctx_used=0, breakdown={}, cost=0.0, tokens_in=0,
                            tokens_out=0, messages=0, edits=[],
                            jobs={"total": 0, "running": 0, "failed": 0},
                            workflows={"total": 0, "running": 0, "failed": 0},
                            mcp={"servers": 0, "connected": 0, "tools": 0}, skills=0))
    assert "0%" in text
    assert "local / no API cost" in text
    assert "jobs 0" in text and "workflows 0" in text
    assert "mcp none" in text
    assert "none yet" in text


def test_panel_edits_show_five_and_count_the_rest() -> None:
    text = "\n".join(_lines(100, edits=[f"f{i}.py" for i in range(8)]))
    assert "f4.py" in text and "f5.py" not in text
    assert "(+3)" in text


def test_long_model_id_never_breaks_the_frame() -> None:
    lines = _lines(80, model="accounts/fireworks/models/" + "x" * 60)
    assert all(len(ln) <= 80 for ln in lines)
    assert all(ln.startswith("│") or ln.startswith("╭") or ln.startswith("╰") for ln in lines)


# -- the one-line form ----------------------------------------------------------


def test_dashboard_line_is_compact_and_complete() -> None:
    line = dashboard_line(_facts())
    assert line.startswith("✦ claude-sonnet-5 · Claude (Anthropic) · OAuth token")
    assert "ctx 38%" in line and "$0.03" in line and "accept edits on" in line
    assert "3 jobs (1 running)" in line and "1 workflow" in line
    assert "mcp 2/2 · 14 tools" in line
    assert "\n" not in line


def test_dashboard_line_drops_what_is_zero() -> None:
    line = dashboard_line(_facts(cost=0.0, jobs={"total": 0}, workflows={"total": 0},
                                 mcp={"servers": 0}, ctx_used=0))
    assert "$" not in line and "job" not in line and "mcp" not in line and "ctx" not in line


# -- the colour ramp ------------------------------------------------------------


def test_ctx_fill_colour_ramp() -> None:
    assert ctx_fill_color(0) == "green"
    assert ctx_fill_color(59) == "green"
    assert ctx_fill_color(60) == "yellow"
    assert ctx_fill_color(84) == "yellow"
    assert ctx_fill_color(85) == "red"
    assert ctx_fill_color(100) == "red"


def test_ctx_bar_is_proportional() -> None:
    assert ctx_bar(0, 20) == "░" * 20
    assert ctx_bar(50, 20) == "█" * 10 + "░" * 10
    assert ctx_bar(100, 20) == "█" * 20
    assert len(ctx_bar(33, 7)) == 7


# -- facts gathered from a live session ------------------------------------------


class _Job(SimpleNamespace):
    pass


def test_facts_read_the_same_sources_as_the_dedicated_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat-xyz")
    t = _tui()
    t.messages = ["m1", "m2"]
    t._jobs = SimpleNamespace(all=lambda: [
        _Job(status="running"), _Job(status="done"), _Job(status="error")])
    t._workflows = [SimpleNamespace(status="running"), SimpleNamespace(status="done")]
    t._mcp_manager = SimpleNamespace(
        status_rows=lambda: [{"state": "connected"}, {"state": "failed"}],
        tools={"gh": [1, 2, 3], "db": []})
    t._file_checkpoints = [{"path": "/repo/a.py"}, {"path": "/repo/b.py"}, {"path": "/repo/a.py"}]
    t._tokens_in, t._tokens_out = 1000, 200
    t.effort = "max"
    facts = t._dash_facts(5000, 0.5, 32000, light=True)
    assert facts["family"]["name"] == "Claude" and facts["family"]["glyph"] == "✦"
    assert facts["auth"] == "OAuth token"
    assert facts["ctx_used"] == 5000 and facts["ctx_window"] == 32000
    assert facts["jobs"] == {"total": 3, "running": 1, "failed": 1}
    assert facts["workflows"] == {"total": 2, "running": 1, "failed": 0}
    assert facts["mcp"] == {"servers": 2, "connected": 1, "tools": 3}
    assert facts["edits"] == ["/repo/a.py", "/repo/b.py"]   # newest first, deduped
    assert facts["tokens_in"] == 1000 and facts["tokens_out"] == 200
    assert facts["messages"] == 2 and facts["effort"] == "max"
    assert facts["mode"] == "default"


def test_facts_for_a_local_model(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _tui(model="qwen3:8b", backend="http://localhost:11434/v1", api_key=None)
    facts = t._dash_facts(light=True)
    assert facts["family"]["id"] == "local" and facts["family"]["glyph"] == "⌂"
    assert facts["auth"] == "local"


def test_show_dash_prints_the_panel() -> None:
    t = _tui()
    t._show_dash(1000, 0.0, 8000)
    out = t.console.export_text()
    assert "mantis · dashboard" in out
    assert "claude-sonnet-5" in out
    assert max(len(ln) for ln in out.splitlines()) <= 80


def test_status_opens_with_the_one_line_dashboard() -> None:
    t = _tui()
    t._show_status(3000, 0.02, 32000)
    out = t.console.export_text().splitlines()
    first = next(ln for ln in out if ln.strip())
    assert first.strip().startswith("▎✦ claude-sonnet-5")
    assert "ctx 9%" in first and "$0.02" in first
    assert any("Status" in ln for ln in out)
