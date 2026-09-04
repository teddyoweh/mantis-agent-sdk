"""Tests for the ``mantis serve`` shell redesign: neutral tokens, no
elevation shadows, project/session cards with friendly titles, the hidden
context toggle and tool rows in the transcript, the ``/api/events`` version
long-poll, and the ⌘K palette. Everything runs against a temp home."""

from __future__ import annotations

import json
import re
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(h))
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")
    from mantis_agent import catalog, serve, session_tree

    for p in catalog.CATALOG:
        for var in (p.api_key_env, *p.key_env_aliases):
            monkeypatch.delenv(var, raising=False)
    for cache in (serve._spend_cache, serve._analytics_cache, serve._projects_cache, serve._ollama_cache):
        cache.clear()

    named = tmp_path / "my-app"
    named.mkdir()
    uuid_dir = tmp_path / "3b54e81e-e362-4c1a-9d2b-1234567890ab"
    uuid_dir.mkdir()
    sid = session_tree.new_session_id()
    tx = session_tree.SessionTranscript(sid, cwd=str(named))
    tx.append_message("user", "Fix the login bug\n\n<system-reminder>hidden reminder</system-reminder>")
    tx.record_last_prompt("Fix the login bug")
    tx.append_message("assistant", [{"type": "text", "text": "**Done.** " + "word " * 60}])
    sid2 = session_tree.new_session_id()
    tx2 = session_tree.SessionTranscript(sid2, cwd=str(uuid_dir))
    tx2.append_message("user", "<system-reminder>x</system-reminder> Refactor the parser into smaller functions please, " +
                       "and keep the public API exactly as it is today")
    tx2.record_last_prompt("Refactor the parser")
    tx2.append_message("assistant", [{"type": "text", "text": "ok"}])
    catalog.set_last_model("gpt-5.4", "https://api.openai.com/v1")
    return {"home": h, "named": str(named), "uuid_dir": str(uuid_dir), "sid": sid, "sid2": sid2}


# ---------------------------------------------------------------------------
# projects & sessions as cards
# ---------------------------------------------------------------------------


def test_project_titles_are_never_bare_ids(home):
    from mantis_agent import serve

    by = {p["path"]: p for p in serve.list_projects()}
    assert by[home["named"]]["title"] == "my-app"
    u = by[home["uuid_dir"]]
    assert u["title"].startswith("Refactor the parser") and u["title"].endswith("…") and len(u["title"]) <= 90
    assert not serve._UUIDISH_RE.match(u["title"])
    for p in by.values():
        assert {"title", "first_prompt", "session_count", "last_activity", "msgs", "tokens_est", "usd_est"} <= set(p)
        assert p["tokens_est"] > 0 and p["usd_est"] is not None       # gpt-5.4 has a price row
    assert serve._project_title(None, "abcdef1234567890", None) == "project · abcdef12"
    assert serve._project_title("/tmp/deadbeefdeadbeef", "d", "hello there") == "hello there"
    assert serve._project_title("/home/me/proj", "d", None) == "proj"


def test_clean_prompt_strips_meta_and_truncates():
    from mantis_agent import serve

    assert serve._clean_prompt("<system-reminder>a\nb</system-reminder> hello   world") == "hello world"
    assert serve._clean_prompt("[context]<env>cwd=/x</env>") is None
    assert serve._clean_prompt("") is None and serve._clean_prompt(None) is None
    long = "x" * 200
    assert serve._clean_prompt(long, limit=20) == "x" * 19 + "…"


def test_sessions_carry_display_title_and_cost_pills(home):
    from mantis_agent import serve

    rows = serve.sessions_for(home["named"])
    assert len(rows) == 1
    s = rows[0]
    assert s["display_title"] == "Fix the login bug" and "system-reminder" not in s["first_prompt"]
    assert s["tokens_est"] > 0 and s["usd_est"] is not None and s["model"] == "gpt-5.4"
    assert s["message_count"] >= 1
    u = serve.sessions_for(home["uuid_dir"])[0]
    assert u["display_title"].startswith("Refactor the parser")
    # analytics ledger keyed by session id
    a = serve.analytics()
    assert home["sid"] in a["sessions"] and a["sessions"][home["sid"]]["msgs"] == 2
    assert len(a["projects"]) == 2


# ---------------------------------------------------------------------------
# /api/events — the version counter behind the reactive refresh
# ---------------------------------------------------------------------------


def test_events_version_moves_only_when_state_changes(home):
    from mantis_agent import serve, session_tree

    r = serve.events(None, 0)
    v = r["version"]
    assert len(v) == 12 and r["changed"] is False
    again = serve.events(v, 0)
    assert again["version"] == v and again["changed"] is False
    tx = session_tree.SessionTranscript(session_tree.new_session_id(), cwd=home["named"])
    tx.append_message("user", "another turn")
    r2 = serve.events(v, 0)
    assert r2["changed"] is True and r2["version"] != v
    v2 = r2["version"]
    with serve._deploy_lock:
        serve._deploy_accounts["fake"] = {"ok": True}
    try:
        assert serve.events(v2, 0)["changed"] is True
    finally:
        with serve._deploy_lock:
            serve._deploy_accounts.pop("fake", None)


def test_events_long_poll_waits_then_returns(home):
    from mantis_agent import serve

    v = serve.events(None, 0)["version"]
    t0 = time.monotonic()
    r = serve.events(v, 0.6)
    assert r["changed"] is False and time.monotonic() - t0 >= 0.5
    assert serve.events("stale", 5)["changed"] is True        # differs immediately → no wait


def _boot(token=None, enforce_get=False):
    from mantis_agent import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    httpd.token = token
    httpd.enforce_get = enforce_get
    httpd.daemon_threads = True
    import threading
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_events_and_cards_over_http(home):
    httpd, base = _boot()
    try:
        with urllib.request.urlopen(base + "/api/events?timeout=0", timeout=5) as r:
            e = json.loads(r.read())
        assert e["version"] and e["changed"] is False
        with urllib.request.urlopen(base + "/api/events?since=" + e["version"] + "&timeout=notanumber", timeout=40) as r:
            assert json.loads(r.read())["version"] == e["version"]
        with urllib.request.urlopen(base + "/api/projects", timeout=5) as r:
            projects = json.loads(r.read())["projects"]
        assert all(not re.match(r"^[0-9a-f-]{36}$", p["title"]) for p in projects)
        with urllib.request.urlopen(base + "/api/sessions?cwd=" + urllib.request.quote(home["named"]), timeout=5) as r:
            assert json.loads(r.read())["sessions"][0]["display_title"] == "Fix the login bug"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_events_respect_the_lan_token(home):
    httpd, base = _boot(token="tok", enforce_get=True)
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/api/events?timeout=0", timeout=5)
        assert ei.value.code == 401
        req = urllib.request.Request(base + "/api/events?timeout=0", headers={"X-Mantis-Token": "tok"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# the page: tokens, no shadows, shell, cards, transcript, palette
# ---------------------------------------------------------------------------


def _css():
    from mantis_agent.serve_ui import INDEX_HTML

    return INDEX_HTML.split("<style>")[1].split("</style>")[0]


def test_stylesheet_has_no_elevation_shadows():
    css = _css()
    for line in css.split("\n"):
        if "box-shadow" not in line:
            continue
        for m in re.finditer(r"box-shadow:\s*([^;]+);", line):
            v = m.group(1).strip()
            assert v == "none" or re.match(r"^0 0 0 \dpx var\(--[a-z-]+\)$", v), line.strip()


def test_theme_tokens_are_neutral_and_defined_for_both_schemes():
    css = _css()
    assert "prefers-color-scheme: dark" in css and ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css
    dark = css.split(':root[data-theme="dark"]')[1].split("}")[0]
    for tok in ("--bg: #0a0b0d", "--panel: #111316", "--line: rgba(255,255,255,.08)", "--ink: #ededed", "--ink-2: #9a9ea6"):
        assert tok in dark, tok
    light = css.split(":root {")[1].split("}")[0]
    for tok in ("--bg: #fafafa", "--panel: #ffffff", "--line: rgba(0,0,0,.08)", "--ink: #111111", "--ok:", "--warn:", "--bad:", "--info:"):
        assert tok in light, tok
    # olive is gone from the surfaces
    for old in ("#efece5", "#0d0f0a", "#1a1d15", "#14160f"):
        assert old not in css, old
    assert "font-size: 13px" in css.split("\n  body {")[1].split("}")[0]


def test_page_carries_the_shell_cards_transcript_and_palette(home):
    httpd, base = _boot()
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            page = r.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
    for marker in ('<header id="top">', 'id="cmdk"', 'id="themebtn"', 'id="lanind"', 'id="palette"', 'id="palin"',
                   'data-v="home" class="on">overview', 'data-v="config">config<span class="k">7</span>',
                   "function patchList", "function skeleton", "function md(", "function splitMeta", "function ctxToggle",
                   "function toolCall", "META_RE", "system-reminder", "show context (",
                   'id="projcards"', 'id="sesscards"', "pcard", "selectProject", "selectSession", "UUIDISH",
                   "renderTopStatus", "watchEvents", "/api/events?", "EVENTS_OK", "openPalette", "paletteItems",
                   'e.key.toLowerCase() === "k"', "cycleTheme", "applyTheme", 'get("theme")', "rollback",
                   "grid-template-columns: 280px 320px", "repeat(auto-fill, minmax(260px, 1fr))", "max-width: 900px",
                   "CHORDS", "visibilitychange", "sessfind", "fam-grid", "live-act", "spend-card", "$ / 1M in"):
        assert marker in page, marker
    assert "cdn." not in page and "googleapis" not in page and "<aside" not in page
