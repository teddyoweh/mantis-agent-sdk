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


def test_activity_counts_last_seven_days(home, monkeypatch):
    from mantis_agent import job_records, serve, workflow_store

    now = time.time()
    a = serve.activity()
    assert a["counts_7d"] == {"running": 0, "done": 0, "error": 0}
    sid = home["sid"]
    for seq, (status, when) in enumerate([("running", now - 5), ("done", now - 60), ("error", now - 120), ("done", now - 9 * 86400)], 1):
        rec = job_records.JobRecord(session_id=sid, seq=seq, kind="task", status=status, desc="j%d" % seq,
                                    started_at=when - 10, ended_at=None if status == "running" else when, cwd=home["named"])
        rec.job_id = job_records.make_job_id(sid, seq)
        job_records.save_job_record(rec)
    workflow_store.save_run({"id": "r-ok", "name": "ok", "status": "done", "started": now - 50, "ended": now - 1, "phases": []}, definition="d")
    workflow_store.save_run({"id": "r-bad", "name": "bad", "status": "failed", "started": now - 50, "ended": now - 1, "phases": []}, definition="d")
    a = serve.activity(limit=10)
    assert a["counts_7d"] == {"running": 1, "done": 2, "error": 2}       # the 9-day-old job is outside the window
    assert {"jobs", "runs", "active_jobs", "active_runs", "jobs_dir", "runs_dir"} <= set(a)   # shape unchanged


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
    """Flat surfaces get their depth from a background step, never a shadow.
    The two exceptions are the focus ring and the ONE genuinely layered
    surface — the provider panel, which floats above the grid and has to read
    as being off the page."""
    css = _css()
    raised = []
    for line in css.split("\n"):
        if "box-shadow" not in line:
            continue
        for m in re.finditer(r"box-shadow:\s*([^;]+);", line):
            v = m.group(1).strip()
            if v == "none" or re.match(r"^0 0 0 \dpx var\(--[a-z0-9-]+\)$", v):
                continue
            # an INSET ring is a drawn shape, not elevation: it is how a live
            # step reads as a hollow circle against a filled done one, so the
            # two differ without relying on colour
            if re.match(r"^inset 0 0 0 \dpx var\(--[a-z0-9-]+\)$", v):
                continue
            # the focus ring: a thin accent ring inside a soft halo — still a
            # drawn ring on the control, not elevation
            if re.match(r"^0 0 0 [\d.]+px var\(--accent\), 0 0 0 \dpx var\(--accent-soft\)$", v):
                continue
            raised.append(line.strip())
    # A raise is earned only by a surface that genuinely floats over the page.
    # There are exactly three: the provider panel, the menu a control opens
    # over the content, and the chart's hover card, which floats over the plot.
    assert len(raised) == 3, raised
    # one hover card, shared by every chart (Overview trace, spend bars, Deploy usage)
    tip = css.split("  .trace .tip, .bars-wrap .tip, .us-chart .tip {")[1].split("}")[0]
    assert "box-shadow" in tip and "position: absolute" in tip
    assert all("var(--dim)" in r for r in raised), "the raise takes the theme's own dim"
    panel = css.split("  .ac-panel {")[1].split("}")[0]
    assert "box-shadow" in panel and "position: absolute" in panel
    assert ".cmp-bar" not in css
    menu = css.split("  .pmenu {")[1].split("}")[0]
    assert "box-shadow" in menu and "position: absolute" in menu
    # ...and nothing flat has one
    for flat in ("  .acard {", "  .mcard {", "  .tile {", "  .nowcard {"):
        assert "box-shadow" not in css.split(flat)[1].split("}")[0], flat


def test_stylesheet_has_no_lines_at_all():
    """Elevation is a background step, never a border. The only ``border``
    declarations allowed are resets (``border: 0`` / ``none``) and radii; the
    only outline is the 2px focus ring (a box-shadow)."""
    css = _css()
    offenders = []
    for line in css.split("\n"):
        # the state badge's glyph is drawn WITH a stroke on purpose: a hollow
        # ring and an outlined diamond are how the four states stay tellable
        # apart without colour. That's a shape, not elevation.
        if ".ac-stg" in line:
            continue
        for m in re.finditer(r"(?<![a-z-])(border(?:-top|-bottom|-left|-right|-color|-width|-style)?|outline)\s*:\s*([^;]+);", line):
            prop, val = m.group(1), m.group(2).strip()
            if prop == "outline" and val == "none":
                continue
            if prop == "border" and val in ("0", "none"):
                continue
            offenders.append(line.strip())
    assert not offenders, offenders
    body = "\n".join(x for x in css.split("\n") if ".ac-stg" not in x)
    assert "1px solid" not in body and "1px dashed" not in body and "border-color" not in body
    # the focus ring survives
    assert ":focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent)" in css


def test_models_page_has_family_tabs_and_no_route_strip():
    """The Models list is tabbed by family (with counts, remembered in the
    hash) and the page-level "Test this route · recent" strip is gone."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    for marker in ("MODEL_TAB", "tabFromHash", "TAB_SLUG", "mtabs", 'MODEL_TAB === "local"',
                   '"Open models"', '"Local"', 'el("span","tn2"', "applyModelFilter"):
        assert marker in js, marker
    assert ".mtabs" in css and ".mtabs .tn2" in css
    # the tab narrows the cards and hides the group labels; "all" keeps them
    assert 'r.dataset.fam === MODEL_TAB' in js and 'MODEL_TAB === "all" && n) ? "" : "none"' in js
    # the URL says the name people use, the code keeps the family id
    assert 'anthropic: "claude"' in js and 'xai: "grok"' in js
    # gone: the route strip, its chips, its CSS, and the data that fed it
    for gone in ("Test this route", "routeBtn", "routeBar", "hero-lbl"):
        assert gone not in js, gone
    for gone in (".recent {", ".hero-lbl"):
        assert gone not in css, gone
    assert "Enable a provider, or point mantis at your own server." not in js


def test_choose_a_model_is_a_card_grid_not_a_table():
    """"Choose a model" draws the SAME card the Deploy picker draws — one
    component, two pages — in the same responsive grid. The table it replaced
    is gone from the stylesheet as well as the script."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    # one grid definition, shared: the two pickers cannot drift apart, and it
    # is a fixed four across so neighbours' readings line up down the column
    assert ".dp-mgrid, .mm-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));" in css
    for w, n in ((1360, 3), (860, 2)):
        assert "@media (max-width: %dpx) { .dp-mgrid, .mm-grid { grid-template-columns: repeat(%d, minmax(0, 1fr)); } }" % (w, n) in css
    # the card is .mcard plus a page modifier, never a second card component
    assert 'el("div","mcard mmcard"' in js
    for shared in (".mcard {", ".mcard .mh {", ".omark {", ".mcard .mt {", ".mcard .mo {",
                   ".mstats {", ".mstat {", ".mmeta {", ".mfoot {"):
        assert shared in css, shared
    # the table, its header, its rows and its sticky family stripe are gone
    for dead in (".mtable", ".mrow", ".mhead", ".mfam"):
        assert dead not in css, dead
    for dead in ('el("div","mtable")', 'el("div","mhead")', 'el("div","mrow"', 'el("div","mfam")',
                 '"mprice"', '"mctx"', '"mcaps"', "priceCell"):
        assert dead not in js, dead
    # grouped by source, as a labelled grid per group with its count
    assert 'el("div","mm-glabel")' in js and 'el("div","mm-grid")' in js
    # the group's count is the cards it shows, so it counts aliases only
    assert 'el("span","mm-gn", nr + " model"' in js


def test_family_tabs_carry_their_vendor_marks():
    """The tab row is the same pill treatment as the Deploy page's org filter,
    marks included: 20px box, 17px of ink, no square behind it — one size for
    all three pill rows, so bumping one can never leave the others behind. All
    is every family at once and wears none; Open models is not a vendor and
    wears a neutral glyph rather than borrowing Ollama's llama, which is what
    the catalogue lists as its logo."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]

    # exactly the size the Deploy page's two pill rows already use
    assert "  .mtabs .mark2 { width: 20px; height: 20px; border-radius: 6px; background: none;" in css
    assert "  .mtabs .mark2 svg { width: 17px; height: 17px; }" in css
    for row in (".dp-orgs .omark",):
        box = css.split("  %s {" % row)[1].split("}")[0]
        assert "width: 20px" in box and "background: none" in box, row
        assert "width: 17px" in css.split("  %s svg {" % row)[1].split("}")[0], row
    # a mark is sized to its label, and all three rows label at the same size
    for row in (".mtabs .fchip", ".dp-orgs .fchip"):
        assert "font-size: 14px" in css.split("  %s {" % row)[1].split("}")[0], row
    assert "  .mtabs .fchip { display: inline-flex; align-items: center; gap: 7px;" in css

    # All alone gets nothing, the way the Deploy page's All pill does
    assert 'if (t.id !== "all") c.append(famMark(t.id, t.logo, t.label));' in js
    assert 'add("all", "All", null, models.length);' in js
    # one rule for what a family looks like, used by the tab AND by the group
    # label it scrolls to, so the two can never disagree
    # the definition, the tab, the group label it scrolls to, the rail's row
    # under My models, and the Overview's provider row
    assert js.count("famMark(") == 5
    assert 'fh.append(famMark(fid, famLogo[fid], famLabel[fid] || fid));' in js
    fm = js[js.index("function famMark("):js.index("function anyMark()")]
    assert 'fid === "oss" ? anyMark() : fid === "selfhost" ? hostMark() : providerMark(logo || fid, label)' in fm
    # Local IS Ollama, so there the llama is the honest mark
    assert 'tabs.push({ id: "local", label: "Local", n: nLocal, logo: "ollama" });' in js

    # the stand-in is drawn, not borrowed: four blocks on the card motif's own
    # pixel grid, normalised to the same 13px of ink as every real mark
    i = js.index("function anyMark()")
    any_ = js[i:js.index("\n}", i)]
    assert 'viewBox="0 0 13 13"' in any_ and 'shape-rendering="crispEdges"' in any_
    assert any_.count('<rect x=') == 4
    assert "mm-anymark" in any_
    # neutral ink, never a vendor's colour
    assert "  .mm-anymark { color: var(--ink-3); }" in css
    assert "  .mm-anymark svg { fill: currentColor; }" in css


def test_the_current_tag_is_a_corner_tag_by_one_rule():
    """It sits in the card's top-right corner, keeps its slant, rounds its
    top-right to the card's own radius so it follows the curve, and bleeds a
    little past the edge where the card clips it flat. One rule covers the
    hero and the model cards.

    It is NOT applied to the provider card: that card's height is fixed at
    63px and its action starts at y17, so a 22px corner tag would sit 5px on
    top of it. Measured, not guessed — see measure_tag.py. Its four state
    badges therefore all stay inline together, so the indicator never moves
    depending on which state it is in.
    """
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    tag = css.split("  .cornertag {")[1].split("}")[0]
    assert "position: absolute" in tag and "top: 0" in tag
    assert "right: -4px" in tag, "the bleed is what makes the slant read as deliberate"
    assert "border-radius: 0 var(--radius) 0 9px" in tag, "it follows the card's own corner"
    assert "margin-right: 0" in tag, "the inline pill's margin would push it off the corner"
    # the slant and its counter-skewed label survive
    assert "transform: skewX(-8deg)" in css.split("  .ac-st.cur {")[1].split("}")[0]
    assert ".ac-st.cur > * { transform: skewX(8deg); }" in css
    # the cards that carry it clip the bleed
    assert "  .mmcard, .nowcard { overflow: hidden; }" in css
    # ...and keep their content out from under it
    assert "  .mmcard.on .mh { padding-right: 84px; }" in css
    assert "  .nowcard.hastag .now-top { padding-right: 104px; }" in css

    # exactly the two card kinds that have room for it
    assert js.count('"ac-st cur cornertag"') == 2
    hero = js[js.index("function nowRunning(pad, m) {"):js.index("function reachRow(")]
    assert 'card.append(st);' in hero and 'card.classList.add("hastag");' in hero
    # Switch stays in the action lane and is the only thing in it
    assert 'const acts = el("div","now-a");' in hero
    assert hero.index("card.append(st);") < hero.index('const acts = el("div","now-a");')
    # the provider card's badge is untouched: still inline, still all four
    auth = js[js.index("function authCard(e, panel) {"):js.index("function metaBit(")]
    assert 'el("span","ac-st " + st8.cls)' in auth and "cornertag" not in auth
    assert "head.append(stw);" in auth


def test_a_model_card_says_each_fact_once():
    """Title, caption, the pills, one action — and readiness only where it is
    not ready, because a grid of "ready" pills would drown the one card that
    needs a key."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    card = js[js.index("function myModelCard("):js.index("async function loadModels()")]
    # the model id titles the card; the serving provider is its caption
    assert 'el("div","mt", a.model)' in card and 'el("div","mo", a.label)' in card
    # the vendor's mark, in the same optically normalised square the Deploy
    # card uses — not a second mark treatment
    assert 'fillMark(el("span","omark"), a.pid, a.label)' in card
    # The numbers are READINGS, not pills: context, then what it charges going
    # in and coming out, each in the same one of three columns on every card.
    assert 'stat(a.info.ctx ? fmtCtx(a.info.ctx) : "\u2014", "context"' in card
    assert 'stat("$" + price2(pr.in), "in / 1M")' in card
    assert 'stat("$" + price2(pr.out), "out / 1M")' in card
    # a single price tile spans the in/out pair's space, so no card ends in a hole
    assert 'stat("Free", a.local ? "runs on your machine" : "no API charge", "acc span2")' in card
    assert 'stat("—", "no price listed", "mute span2")' in card
    assert "pricePill" not in card, "the price is a reading now, not a pill"
    # The quiet line is STATE, never a capability list: "tools · effort" was
    # on nearly every card, and a fact true of everything says nothing about
    # any one of them. Only what differs card to card and changes what you do.
    for gone in ('"tools"', '"effort"', '"thinks"'):
        assert gone not in card, gone
    assert '"ok", "loaded"' in card
    assert 'el("div","mmeta")' in card and '"mp2"' not in card
    # readiness is stated once, and never on the card that is already in use
    assert card.count('"not connected"') == 1
    assert 'if (!a.enabled && !on) {' in card
    # ONE action, as a real button, and the current card has none: its tag
    # already says so
    assert card.count('el("button","mbtn"') == 1
    assert 'a.enabled ? "Use model" : "Connect"' in card
    assert "if (!on) {" in card
    assert "use \u2192" not in card and "mm-act" not in card
    # the same slanted Current tag the provider cards wear — and that alone
    assert 'el("span","ac-st cur cornertag")' in card and 'el("span","ac-stl", "Current")' in card
    assert "Motif" not in card and "ac-mot" not in card
    # unlock still deep-links into that family's setup
    assert "unlockFamily(fid, a.pid)" in card and "useModel(a.model, a.backend)" in card
    # the local size is said in the caption, "loaded" only as a tag
    assert '"local \u00b7 " + fmtBytes(om.size),' in js and '" \u00b7 loaded"' not in js


def test_dated_snapshots_fold_under_the_alias_they_pin():
    """OpenAI serves gpt-4o AND three dated gpt-4o's. Listed as peers they are
    most of the grid; folded under the alias they are one word on its card."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    fold = js[js.index("function foldSnapshots("):js.index("const stat = (v, label, cls)")]

    # A trailing ISO or compact date, and ONLY when the same provider also
    # serves the undated id. That second half is the whole safety of it:
    # claude-opus-4-5-20251101 has no undated twin, so it stays a model.
    assert r"const SNAP_RE = /^(.+?)-(\d{4}-\d{2}-\d{2}|\d{8})$/;" in js
    assert r'const base = byKey.get(mt[1] + "\u0000" + r.pid);' in fold
    assert "if (!base || base === r) return;" in fold
    # a snapshot is emitted straight after the alias it pins, never elsewhere
    assert "if (r.snapOf) return;" in fold and "(r.snaps || []).forEach(sn => out.push(sn));" in fold

    # No label on the card says any of this: no "1 dated", no "dated" tag, no
    # toggle. The alias simply stands for its pins. Only a search reaches a
    # snapshot, because typing a date still has to be able to find one.
    for gone in ("snapb", "snaptag", "SNAPS_OPEN", '" dated"'):
        assert gone not in INDEX_HTML, gone
    assert 'const okS = !r.dataset.snap || !!q;' in js
    assert "const on = okQ && okF && okT && okS;" in js
    # the counts describe the cards on screen, not the folded total
    assert "const nPrimary = allModels.filter(a => !a.snapOf).length;" in js
    assert 'const tabs = [{ id: "all", label: "All", n: nPrimary }];' in js


def test_the_model_cards_foot_holds_the_pin_and_the_one_action():
    """The foot is laid out, not stacked: the compare pin and the action are
    flex siblings, so no width can bring them together."""
    from mantis_agent.serve_ui import INDEX_HTML

    css = _css()
    # one foot for both cards now: the Deploy card and My models share it, so
    # the pin and the verb cannot be laid out two different ways
    foot = css.split("  .mfoot {")[1].split("}")[0]
    assert "display: flex" in foot and "align-items: center" in foot
    assert "margin-top: auto" in foot                       # the foot is always last
    assert ".mm-foot" not in css, "the my-models-only foot is gone"
    assert 'el("div","mfoot")' in INDEX_HTML
    # no pin: Compare is gone, so the foot holds exactly one thing, a button
    assert ".mm-pin" not in css and "togglePin" not in INDEX_HTML
    btn = css.split("  .mbtn {")[1].split("}")[0]
    assert "margin-left: auto" in btn and "border: 0" in btn
    # hovering a card is not choosing it: green only under the pointer itself
    assert ".mcard:hover .mbtn { background: var(--fill-2); color: var(--ink); }" in css
    assert ".mbtn:hover, .mbtn:focus-visible { background: var(--accent);" in css
    # The facts and the action are divided the way this sheet divides
    # everything: not by a line but by a run of single pixels — the rail's
    # branch motif at a sparser 4px pitch — which takes the accent on the
    # card under the cursor, as the rail's stub does under the row you are on.
    assert "repeating-linear-gradient(to right, var(--fill-2) 0 1px, transparent 1px 4px)" in foot
    assert "background-size: 100% 1px" in foot
    assert ".mcard.on .mfoot {" in css and ".mcard:hover .mfoot" not in css
    assert "repeating-linear-gradient(to right, var(--accent) 0 1px, transparent 1px 4px)" in css
    # nothing is painted into the foot any more
    assert "ac-mot" not in css
    # the head gives its 70px reservation back unless something sits top-right
    # The head reserves a corner ONLY for the Current tag. The verb moved into
    # the foot, so there is no longer a 70px hole punched in every title for
    # it — and therefore nothing for the my-models card to reset.
    assert "padding-right" not in css.split("  .mcard .mh {")[1].split("}")[0]
    assert "  .mmcard.on .mh { padding-right: 84px; }" in css
    # the tag is a corner tag now, by one rule shared with the hero
    assert "  .mmcard, .nowcard { overflow: hidden; }" in css
    # keyboard focus is a background step, like every other state on the page
    assert "  .mmcard.kb { background: var(--fill); }" in css
    for banned in ("border", "outline", "box-shadow"):
        assert banned not in css.split("  .mmcard.kb {")[1].split("}")[0], banned

def test_a_free_model_is_not_a_price_of_zero():
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    fn = js[js.index("function pricePill("):js.index("function myModelCard(")]
    assert 'pill("free", "", "acc")' in fn
    assert "your hardware, no API charge" in fn
    assert 'pill("\u2014", " / 1M")' in fn and "no row in the price table" in fn


def test_my_models_leads_with_what_you_are_running():
    """The question this page exists to answer is "what am I running". That was
    a word in the third of three stat tiles; it is now the first thing on the
    page, with the route, the window, the price and the capabilities beside it."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    hero = js[js.index("function nowRunning(pad, m) {"):js.index("function reachRow(")]
    # the id at a size you read without meaning to
    assert 'el("div","now-id", cur)' in hero
    assert "  .now-id { font-family: var(--mono); font-size: 24px;" in css
    # the route in, said once
    assert r'via.push("local \u00b7 Ollama")' in hero
    assert 'via.push("via " + (prov.label || prov.id))' in hero
    assert 'prov.auth === "oauth" ? "subscription"' in hero
    # the same pills the cards use, so a fact looks the same everywhere
    assert 'pill(fmtCtx(info.ctx), " context")' in hero and "facts.append(pricePill(price));" in hero
    for cap in ('"cap","tools"', '"cap","effort"', '"cap","thinks"'):
        assert cap in hero, cap
    # a model the capability table has never heard of says so
    assert "no capability row" in hero
    # nothing is set yet is its own state, not a blank hero
    assert "No model is set on this machine yet." in hero
    # Switch is where "go back to what I was running" belongs, so it is a menu
    # of recents with a way out to the grid — not a permanent row over it
    assert 'popMenu("Switch"' in hero and 'document.getElementById("mm-grid-top")' in hero
    assert '{ head: "Recently used" }' in hero and '"Browse all models"' in hero


def test_the_hero_never_claims_a_check_it_did_not_run():
    """A saved key is not an answering endpoint. Until something checks, the
    page says it has not been checked and offers the check."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    row = js[js.index("function reachRow(cur, prov, local) {"):js.index("async function checkReach(")]
    assert "Reachability not checked this session." in row
    assert 'btn("Check now", "gho"' in row
    # only after a check does it make a claim, and it says when and how fast
    assert 'r.ok ? "Answering" : "Did not answer"' in row
    assert 'bits.push("checked " + ago(r.at));' in row
    assert 'if (r.ok && r.ms != null) bits.push(r.ms + "ms");' in row

    chk = js[js.index("async function checkReach(cur, prov, local) {"):js.index("// ---- compare ---")]
    # a local runtime has no credential to validate; what it has is a daemon
    assert "if (local) {" in chk and "Ollama is not answering" in chk
    # ...and with nothing connected there is nothing to check, which it says
    assert '"no connected method to check"' in chk
    assert 'post("/api/auth/validate", { family: fam.id, method: meth.id, model: cur })' in chk


def test_setup_folds_away_but_opens_itself_when_it_is_the_job():
    """Thirteen rows of "Not connected" above the models made the page lead
    with what you had not done. It folds — and opens when nothing is connected,
    when a deep link named a provider, or when a link asked for it."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    assert "  .provbox { display: none; margin-top: 10px; }" in css
    assert "  .provbox.on { display: block; }" in css
    assert 'const authBox = el("div","provbox"); authBox.id = "auth-cards";' in js
    assert 'sum.setAttribute("aria-controls", "auth-cards");' in js
    assert 'sum.setAttribute("aria-expanded", on ? "true" : "false");' in js
    # the strip is written by the grid, so summary and grid cannot disagree
    grid = js[js.index('ph.append(el("span","setup-n"'):]
    assert 'sum.innerHTML = "";' in grid[:900]
    assert 'connected ? String(connected) + " connected" : "Nothing connected yet"' in js
    # the three things that open it
    assert "const forced = !connected || !!AUTH.open || qp.get(\"prov\") === \"open\";" in js
    # everything the grid had is still inside it
    for kept in ('el("div","auth-glabel", label)', 'el("div","auth-grid")', "ac-panel"):
        assert kept in js, kept


def test_the_grid_can_be_asked_a_question_it_can_answer():
    """Family, cheapest, biggest context — three orderings computable from
    tables this machine has. Nothing offers "fastest", because nothing here
    measures speed."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    assert '[["family", "By family"], ["cheap", "Cheapest"], ["ctx", "Biggest context"]]' in js
    sorts = js[js.index("const SORTS = ["):js.index("sec.append(bar);")]
    assert "fastest" not in sorts.lower(), "nothing here measures speed"
    # it is a labelled menu, and the label states the current ordering so the
    # answer is readable without opening it
    assert "const sortLabel = () => (SORTS.find(([k]) => k === MODEL_SORT) || SORTS[0])[1];" in js
    assert 'popMenu(sortLabel,' in js and 'prefix: "Sort:"' in js
    # ...and it lives in the search row, not in a row of its own
    assert "bar.append(sortBtn);" in js
    for dead in (".mm-sortbar", ".mm-recent", ".mm-rec ", "el(\"div\",\"mm-sortbar\")"):
        assert dead not in css and dead not in js, dead
    # unknowns sort last rather than pretending to be zero
    keys = js[js.index("const keyOf = a =>"):js.index("const byId = {};")]
    assert ": Infinity;" in keys and "unpriced sorts last" in keys
    assert "-(a.info.ctx || 0)" in keys and "unknown sorts last" in keys
    # the ordering names itself, and says what it ordered by
    assert 'MODEL_SORT === "cheap" ? "Cheapest first" : "Biggest context first"' in js
    assert "by $ in + $ out per 1M" in js
    # cards are MOVED between grids, never rebuilt, so a card keeps its state
    assert "rows.forEach(c => flat.append(c));" in js
    assert "  .mm-off { display: none !important; }" in css
    # the family tabs, filter pills and search all still drive the same apply()
    assert "applyModelFilter = apply;" in js and "find.input.oninput = apply;" in js


def test_a_page_states_its_situation_as_readings_not_a_paragraph():
    """A wrapped sentence carrying four facts reads as filler. The facts become
    discrete value-and-label readings on one line that never wraps, at most one
    short clause of prose survives, and the honesty note rides as a footnote
    rather than as half the paragraph."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    assert "function pageReads(pad, reads, clause, note)" in js
    row = css.split("  .page-r {")[1].split("}")[0]
    assert "flex-wrap: nowrap" in row and "overflow: hidden" in row
    rd = css.split("  .page-rd {")[1].split("}")[0]
    assert "white-space: nowrap" in rd
    # it degrades by dropping the least important reading, never by breaking
    # a sentence in half
    assert "@media (max-width: 1200px) { .page-rd.opt2 { display: none; } }" in css
    assert "@media (max-width: 1000px) { .page-rd.opt1, .page-rc { display: none; } }" in css
    # the note is a tooltip on a mark, not prose
    # no sentence and no ⓘ under a title: the note is the row's own tooltip
    assert "page-rn" not in js and 'r.title = [clause, note].filter(Boolean).join(" — ");' in js

    # every page states itself this way
    assert js.count("pageReads(pad, [") >= 5
    # Deploy repaints its readings as deploys start and finish, so it states
    # them into its own holder rather than straight onto the page
    assert "pageReads(w, reads," in js
    for probe in ('k: "sessions"', 'k: "projects"', 'k: "first run"',
                  'k: "running"', 'k: "done"', 'k: "failed"',
                  'k: "burn · unpriced"', 'k: "providers ready"', 'k: "deploying"',
                  'k: "models"', 'k: "families"',
                  'k: "always loaded"', 'k: "on demand"',
                  'k: "connected"', 'k: "tools for the agent"', 'k: "settings"'):
        assert probe in js, probe
    # the sentence that used to wrap is gone
    assert "every number here is read off this machine's own transcripts." not in js
    assert "Every number on this page is read off this machine's own transcripts." in js


def test_the_control_stack_is_two_rows_and_search_comes_first():
    """Four stacked rows before the first model — families, search, recents,
    sort — is a lot to read past. Search is the primary act and the family
    filter narrows what it returns, so the search row is first and the families
    sit under it; sort folds into the search row; recents move to the hero,
    where "go back to what I was running" belongs."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    body = js[js.index('const tabRow = el("div","mtabs");'):js.index("// One labelled card grid per source")]
    # exactly two control rows reach the section, in this order
    assert body.count("sec.append(") == 2, body.count("sec.append(")
    assert body.index("sec.append(bar);") < body.index("sec.append(tabRow);")
    # the rows that used to be there are gone entirely
    for dead in ("mm-recent", "mm-sortbar", "Recently used\")"):
        assert dead not in body, dead
    # ...and the search row carries the readiness chips and the sort menu
    assert "bar.append(chips);" in body and "bar.append(sortBtn);" in body
    for c in (".mm-recent", ".mm-sortbar"):
        assert c not in css, c


def test_a_menu_hands_focus_back_and_says_what_is_chosen():
    """A control whose options would otherwise be a permanent row of pills. It
    has to be operable without a mouse, and it must not swallow focus."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    fn = js[js.index("function popMenu(label, items, opts) {"):js.index("// ---- compare ---")]
    # the trigger states the choice without being opened
    assert "lab.append(el(\"b\", null, typeof label === \"function\" ? label() : label));" in fn
    assert 'b.setAttribute("aria-haspopup", "menu");' in fn
    assert 'b.setAttribute("aria-expanded", "false");' in fn
    assert 'b.setAttribute("aria-expanded", "true");' in fn
    assert 'menu.setAttribute("role", "menu");' in fn
    assert 'mi.setAttribute("role", "menuitem");' in fn
    # open with the keyboard, walk it, close it, and get focus back
    assert 'if (ev.key === "ArrowDown") { ev.preventDefault(); open(); }' in fn
    assert 'if (ev.key === "Escape") { ev.stopPropagation(); close(true); return; }' in fn
    assert 'ev.key !== "ArrowDown" && ev.key !== "ArrowUp"' in fn
    assert "if (refocus) b.focus();" in fn
    assert "if (rows[0]) rows[0].focus();" in fn
    # a click outside closes it, and the listener does not outlive the menu
    assert 'document.addEventListener("mousedown", away, true);' in fn
    assert 'document.removeEventListener("mousedown", away, true);' in fn
    # the surface is a menu, not a dialog: no focus trap, no scrim
    assert "scrim" not in fn and "trap" not in fn


def test_recently_used_lives_on_the_hero_and_never_lies_about_reach():
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    hero = js[js.index("function nowRunning(pad, m) {"):js.index("function reachRow(")]
    # only models this page actually knows about
    assert "(MM_ROWS || []).find(a => a.model === id)).filter(Boolean).slice(0, 5)" in hero
    # one that needs a key says so and goes to setup instead of pretending
    assert "run: () => a.enabled ? useModel(a.model, a.backend) : unlockFamily(a.fam)," in hero
    assert 'side: a.enabled ? null : "not connected"' in hero


def test_models_state_ships_a_bounded_recent_list(home):
    """Switching back should be one click, so the page gets the catalog's own
    switching history — bounded, and never including the model you are already
    on, which would be a shortcut to where you already are."""
    from mantis_agent import catalog, serve

    for mid in ("claude-sonnet-5", "gpt-5.4-mini", "gpt-5.4"):
        catalog.push_recent_model(mid)
    catalog.set_last_model("gpt-5.4", "https://api.openai.com/v1")
    m = serve.models_state()
    assert {"current", "recent", "providers", "families", "model_info", "ollama"} <= set(m)
    assert isinstance(m["recent"], list) and len(m["recent"]) <= 6
    assert "gpt-5.4" not in m["recent"], "the current model is not a shortcut"
    assert "gpt-5.4-mini" in m["recent"]


def test_model_picker_filters_by_company_and_shows_recency():
    """The company pills come from the results, the New pill and the Recent
    sort exist, dates read relative inside a year and absolute beyond it, and
    the VRAM bar is scaled against real GPUs rather than a fixed 80 GB."""
    from mantis_agent.serve_ui import INDEX_HTML

    js, css = INDEX_HTML.split("<script>")[1], _css()
    for marker in ("renderOrgPills", "modelShown", "paintModels", "DEPLOY.org", "dp-orgs", "whenText",
                   "isFresh", "gpuCeiling", "DEPLOY.gpuMax", "writeDeployHash", '"Recent"', '"Last 30 days"'):
        assert marker in js, marker
    # the pills are derived from the current results, with counts and marks
    pills = js[js.index("function renderOrgPills("):js.index("function modelShown(")]
    assert "orgMark(slugOf[o])" in pills and "counts[o]" in pills and 'add("all", "All"' in pills
    # a company is its NAME, not its Hub namespace: openai and openai-community
    # are one OpenAI pill, and the filter keys on the same name
    assert "const orgKey = slug => orgName(String(slug || \"\").toLowerCase()).toLowerCase();" in js
    assert "const o = orgKey(slug);" in pills
    assert 'orgKey(m.org || _orgOf(m.id)) !== DEPLOY.org' in js
    # and a name with spaces survives the round trip through the URL
    assert 'encodeURIComponent(DEPLOY.org)' in js and "decodeURIComponent(raw)" in js
    # org + New combine with the search box and the sort control
    shown = js[js.index("function modelShown("):js.index("function paintModels(")]
    assert "DEPLOY.org" in shown and "DEPLOY.fresh" in shown
    # the date rule: months inside a year, month+year beyond it
    when = js[js.index("function whenText("):js.index("const isFresh")]
    assert "365 * DAY" in when and "month: \"short\", year: \"numeric\"" in when and "updated today" in when
    # VRAM is measured against the provider's largest card, and the verdict is
    # carried by the reading's own colour plus a word — not by a 5px bar whose
    # scale nothing on the card explained
    assert "DEPLOY.gpuMax[DEPLOY.provider]" in js and "frac > 1 ?" in js
    assert 'vcls = frac > 1 ? "red" : frac > 0.85 ? "amb" : "";' in js
    # the small-print line under the numbers is gone: a line appears only when
    # something is wrong, and the date rides beside the name
    assert '"Won\'t fit any GPU on offer" : "Tight fit"' in js and '"vLLM can\'t serve it"' in js
    assert 'el("span","mwhen" + (isFresh(m) ? " fresh" : ""), short)' in js
    assert "  .mstat { min-width: 0; display: flex; flex-direction: column; gap: 2px; padding: 9px 10px 8px;" in css
    for cls in (".mstat.amb b", ".mstat.red b", ".mstat.mute b"):
        assert cls in css, cls
    assert ".vbar" not in css, "the unexplained bar is gone"
    # "vLLM unverified" was small print on every card; the warning now only
    # appears when vLLM definitely can't serve it
    assert "not in the vLLM support list" not in js
    # the GPU-provider toggle is gone: the deploy sheet looks across every
    # provider that can deploy, so there is nothing left to scope
    assert "providerToggle" not in js


def test_model_picker_reads_search_then_narrow():
    """You search, THEN you narrow. The search row comes first and carries the
    sort control with it; the org pills and the token line sit underneath, and
    the token line ends the row rather than floating in its middle."""
    from mantis_agent.serve_ui import INDEX_HTML

    js, css = INDEX_HTML.split("<script>")[1], _css()
    pick = js[js.index("function renderDpPicker(sec) {"):js.index("function parseParams(")]

    # source selector, then search, then filters, then results — in that order
    for i, marker in enumerate(['el("div","dp-src")', 'el("div","dp-find")',
                                'el("div","dp-sub")', 'grid.id = "dp-models"']):
        assert marker in pick, marker
    order = [pick.index(x) for x in ('el("div","dp-src")', 'el("div","dp-find")',
                                     'el("div","dp-sub")', 'grid.id = "dp-models"')]
    assert order == sorted(order), "the picker rows are out of order"

    # the sort control rides in the SEARCH row, not with the org pills
    assert pick.index("chips.append(fresh);") < pick.index('bar.append(chips); sec.append(bar);')
    assert 'bar.append(find.wrap);' in pick
    # the org pills are in the row BELOW the search box
    assert 'orgRow.id = "dp-orgs"; sub.append(orgRow)' in pick
    # ...and nothing else rides in that row: the standing token status is gone,
    # because it only matters on a gated model, where the deploy sheet asks in context
    assert "hf-state" not in js and "renderHfState" not in js
    assert ".dp-sub .dp-status { margin-left: auto; }" in css

    # the heading is gone: the search box is the instruction
    assert 'section(pad, "Pick a model"' not in js
    # ...as is the implementation jargon that captioned the provider strip
    code = "\n".join(x for x in js.split("\n") if not x.strip().startswith("//"))
    assert "keys → user settings env" not in code


def test_org_pills_use_real_names_and_do_not_mangle_unknown_slugs():
    """A Hub org id is a slug. Known orgs get their real name; an unknown one
    keeps its slug EXACTLY, because title-casing turns "zai-org" into
    "Zai-Org" and "ifm" into a company that does not exist."""
    from mantis_agent.serve_logos import ORG_NAMES
    from mantis_agent.serve_ui import INDEX_HTML

    js, css = INDEX_HTML.split("<script>")[1], _css()
    # the names ride the mark file's own alias table
    for slug, name in (("zai-org", "Z.ai"), ("moonshotai", "Moonshot"), ("meta-llama", "Meta"),
                       ("minimaxai", "MiniMax"), ("mistralai", "Mistral"), ("ibm-granite", "IBM"),
                       ("deepseek-ai", "DeepSeek"), ("nousresearch", "Nous"), ("qwen", "Qwen")):
        assert ORG_NAMES[slug] == name, slug
    assert "orcarouter" not in ORG_NAMES, "an unknown org must fall through to its slug"

    assert "const ORG_NAMES = __ORGNAMES__;" in js
    assert 'const orgName = o => ORG_NAMES[String(o || "").toLowerCase()] || String(o || "");' in js
    # the capitalize that mangled the slugs is gone
    orgs = css.split("  .dp-orgs .fchip {")[1].split("}")[0]
    assert "text-transform" not in orgs
    # pills and the model card's second line both take the mapped name
    pills = js[js.index("function renderOrgPills("):js.index("function modelShown(")]
    assert "orgName(slugOf[o])" in pills
    assert 'el("div","mo", orgName(og))' in js

    # the row shows the busiest few plus an overflow, and never hides the
    # filter that is currently active behind it
    assert "const TOP = 6;" in pills
    assert "DEPLOY.org !== \"all\" && orgs.includes(DEPLOY.org) && !head.includes(DEPLOY.org)" in pills
    assert 'el("button","fchip dp-more"' in pills and "DEPLOY.orgsOpen" in pills
    assert 'more.setAttribute("aria-expanded"' in pills


def test_the_curated_list_reads_as_a_starting_point_not_the_whole_world():
    """"30 curated" with nothing else on screen reads as a hard limit. The
    sources are a visible control, the counts say what they are counting, and
    a query states that it reaches all of Hugging Face."""
    from mantis_agent.serve_ui import INDEX_HTML

    js, css = INDEX_HTML.split("<script>")[1], _css()
    pick = js[js.index("function renderDpPicker(sec) {"):js.index("function parseParams(")]

    # three named sources, as a real tablist
    for k, lab in (("curated", "Curated"), ("hub", "Hugging Face"), ("ollama", "Ollama")):
        assert '["%s", "%s"' % (k, lab) in pick, k
    assert 'srcRow.setAttribute("role", "tablist")' in pick
    assert 'c.setAttribute("role", "tab")' in pick and 'aria-selected' in pick
    assert ".dp-srcb.on" in css and ".dp-srcb:focus-visible" in css

    # typing reaches the Hub even from the curated list — the starting point
    # is never a filter you have to escape
    assert 'if (DEPLOY.q && DEPLOY.source === "curated") setSource("hub");' in pick

    # the status line says how many, out of what
    assert 'searching all of Hugging Face…' in pick
    assert '" of all Hugging Face, for “" + DEPLOY.q + "”"' in pick
    # the curated list says nothing under the grid — its tab already names it
    assert "search above to reach all of Hugging Face" not in pick
    # Ollama is a real source, and says so when the daemon is not running
    assert '"reading local models…"' in pick
    assert '"Ollama is not running on this machine"' in pick
    assert '" pulled locally"' in pick
    assert 'await api("/api/ollama")' in pick


def test_no_signal_path_and_short_captions():
    """The pill-chain strips are gone from every page, and no page caption
    runs longer than one short line."""
    from mantis_agent.serve_ui import INDEX_HTML

    css = _css()
    assert ".path {" not in css and ".path .n" not in css and "arw" not in css
    js = INDEX_HTML.split("<script>")[1]
    assert "signalPath" not in js and "──▶" not in js
    for m in re.finditer(r'pageHead\(pad, "([^"]+)", [^,]+,\s*"([^"]*)"', js):
        assert len(m.group(2)) <= 70, (m.group(1), m.group(2))
    assert 'cardHead(pcard, "key", "Providers", readyN + "/" + fams.length + " ready"' in js
    assert "renderProvSection(pSec, !ready.length);" in js and "GPU providers ready" in js
    # section labels are normal-weight title case now — the all-caps mono style is gone
    sec = css.split(".sec-t {")[1].split("}")[0]
    assert "uppercase" not in sec and "var(--mono)" not in sec
    for lab in ('"Active"', '"Deployments"', '"Choose a model"', '"Local models · Ollama"',
                '"Providers"', '"Spend & usage"', '"MCP servers"'):
        assert lab in js, lab
    # The model grid holds cards and nothing else. A "Curated · good first
    # deploys" stripe across it only repeated the Curated tab that is already
    # selected above it, and a row of cards needs no caption to be read.
    assert "good first deploys" not in js.split("function paintModels(")[1]
    assert "dp-glabel" not in INDEX_HTML and "m._label" not in js


def test_the_rail_groups_fold_and_remember():
    """Each section heading is a control: it folds its own items away and the
    choice survives a reload, because which sections you care about is a
    property of how you work, not of this visit. A page you navigate to is
    never left hidden inside a folded group."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]

    assert INDEX_HTML.count('<div class="ngrp" data-g="') == 5
    for g in ("workspace", "models", "work", "extend", "system"):
        assert 'data-g="%s"' % g in INDEX_HTML, g
    assert INDEX_HTML.count('<button class="ng" aria-expanded="true">') == 5
    assert INDEX_HTML.count('<i class="ngc">') == 5
    assert "  .ngrp.shut .ngi { display: none; }" in css
    assert "  .ngrp.shut .ngc { transform: rotate(0deg); opacity: 1; }" in css
    # quiet until you reach for it, or five headings read as five buttons
    assert "  .ngc { flex: none; font-size: 8px; font-style: normal; color: var(--ink-3); opacity: 0;" in css
    assert "button.ng:hover .ngc, button.ng:focus-visible .ngc { opacity: 1; }" in css

    assert 'const GROUP_KEY = "mantis-nav-shut";' in js
    assert "localStorage.setItem(GROUP_KEY" in js and "localStorage.getItem(GROUP_KEY)" in js
    assert "function revealGroup(v)" in js and "revealGroup(name);" in js
    # the items are their own scope now, so a group heading is not a page row
    assert "#nav .ngi > button {" in css and "#nav .ngi > button.on {" in css


def test_the_rail_becomes_a_drawer_before_it_becomes_a_strip():
    """Below 1000px the rail leaves the flow and slides over the page behind a
    scrim, opened by the one control the bar grows for it. It is never a 60px
    strip of wordless marks there, so the folded preference does not apply
    below the breakpoint."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]

    d = css.split("  @media (max-width: 1000px) {")[1].split("\n  }")[0]
    assert "position: fixed" in d and "transform: translateX(-100%)" in d
    assert 'body[data-drawer="on"] #rail { transform: none; }' in d
    assert 'body[data-drawer="on"] #scrim { display: block; }' in d
    assert ".ham { display: inline-flex; }" in d
    assert 'body[data-rail="min"] { --rail-w: 236px; }' in d, "the drawer is always the full rail"
    assert "grid-template-columns: minmax(0, 1fr)" in d, "the rail leaves the flow"
    assert "  #scrim { display: none; position: fixed; inset: 0; z-index: 55; background: var(--dim); }" in css

    assert '<button class="ham" id="ham" aria-label="open navigation" aria-expanded="false">' in INDEX_HTML
    assert "menu: IC0 +" in js, "the hamburger uses a drawn mark like every other"
    assert "function openDrawer()" in js and "function closeDrawer()" in js
    assert 'h.setAttribute("aria-expanded", "true")' in js
    assert 'document.getElementById("scrim").onclick = closeDrawer;' in js
    assert "closeDrawer();                     // on a phone the rail is over the page" in js
    assert 'const NARROW = window.matchMedia("(max-width: 1000px)");' in js
    assert "function railFit() { applyRail(!NARROW.matches && railPref(), false); }" in js


def test_the_rail_walks_with_the_arrow_keys():
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    assert ":focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent)" in css
    walk = js[js.index('document.getElementById("nav").addEventListener("keydown"'):]
    walk = walk[:walk.index("});")]
    assert 'e.key !== "ArrowDown" && e.key !== "ArrowUp"' in walk
    assert '"#nav .ngi > button, #nav .nsub.on .nsr"' in walk
    assert "b.offsetParent !== null" in walk
    assert "e.preventDefault();" in walk


def test_a_count_that_needs_acting_on_is_a_filled_badge():
    """Every other reading in the rail is quiet mono text. A failed run is the
    one you are meant to go and look at, so it is the one that gets a surface —
    and it is a real count from the activity ledger or it is nothing."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    due = css.split("  .nc.due {")[1].split("}")[0]
    assert "background: var(--bad)" in due and "border-radius: 8px" in due
    # three weights across the whole sheet (400/500/600); 700 is gone
    assert "font-weight: 600" in due and "font-weight: 700" not in css
    for banned in ("border:", "outline", "box-shadow"):
        assert banned not in due, banned
    # a count is a number, not a literal: sans, tabular, the smallest step
    assert "  .nc { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3);" in css

    counts = js[js.index("function railCounts(o) {"):js.index("async function loadOverview()")]
    assert "if (act && !live && (o.failed_7d || 0) > 0) {" in counts, "never two numbers on one row"
    assert 'act.className = "nc due";' in counts
    assert "failed in the last 7 days" in counts
    assert 'set("mcp", o.mcp_count ? String(o.mcp_count) : "");' in counts
    assert 'set("skills", o.skill_count ? String(o.skill_count) : "");' in counts


def test_activity_children_are_states_that_actually_have_something_in_them():
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    i = js.index('if (v === "activity") {')
    sub = js[i:js.index("return [];", i)]
    assert '["running", "Running", "run"], ["done", "Done", "ok"], ["error", "Failed", "bad"]' in sub
    assert ".filter(([k]) => (c[k] || 0) > 0)" in sub, "an empty state must not be listed"
    assert "ACT.filter = k;" in sub and 'showTab("activity")' in sub
    assert '<div class="nsub" id="sub-activity"></div>' in INDEX_HTML


def test_the_branch_under_a_child_is_pixels_not_a_hairline():
    """The reference draws a hairline down its children. This sheet draws no
    lines at all, so the branch is a run of 1px blocks on a 3px pitch — the
    card motif's material at its finest grain — with a stub across to each row
    and the accent on the row you are on."""
    css = _css()
    sub = css.split("  .nsub { display: none;")[1].split("}")[0]
    assert "repeating-linear-gradient(to bottom, var(--fill-2) 0 1px, transparent 1px 3px)" in sub
    assert "background-position: 16px 0" in sub and "background-repeat: no-repeat" in sub
    stub = css.split("  .nsr::before {")[1].split("}")[0]
    assert "repeating-linear-gradient(to right, var(--fill-2) 0 1px, transparent 1px 3px)" in stub
    assert "position: absolute" in stub and "left: 17px" in stub
    on = css.split("  .nsr.on::before {")[1].split("}")[0]
    assert "var(--accent)" in on and "width: 7px" in on


def test_every_page_shaped_view_leads_with_a_stat_row():
    """The Overview's reading tile is the page-header pattern everywhere: a
    label, a figure, and a line of context. It is the SAME component, so a
    figure means the same thing wherever you meet it."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    assert "function statRow(pad, stats)" in js
    row = js[js.index("function statRow(pad, stats)"):js.index("function cardHead(")]
    assert "el(\"div\",\"tiles\")" in row, "the Overview's own grid, not a second one"
    assert "tile(box, x.icon, x.label, x.value, x.small, x.sub, x.chart" in row
    # three across reads as three
    assert "  .tiles.tiles-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }" in css
    # ...and it keeps three until there is only room for one: 2 + 1 leaves a
    # hole where the fourth tile isn't
    assert "@media (max-width: 1240px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }" in css
    assert "@media (max-width: 760px) { .tiles, .tiles.tiles-3 { grid-template-columns: minmax(0, 1fr); } }" in css
    # a tile with no series does not reserve a chart's height
    assert "  .tile.flat { min-height: 0; padding-bottom: 13px; }" in css
    assert "if (chart) { const c = el(\"div\",\"tile-c\"); c.innerHTML = chart; t.append(c); }" in js
    assert 't.classList.add("flat");' in js

    # Activity and Deploy state their situation with it. My models does not:
    # it leads with the one model you are actually running, at hero size.
    # Deploy used to, and with nothing running its three tiles read 0, — and
    # 0/6. It leads with its Active cards instead; the burn is a reading.
    # Activity's three tiles repeated its own readings line — they are gone too
    assert js.count("statRow(pad, [") == 0
    for gone in ('label: "Running"', 'label: "Done"', 'label: "Failed"'):
        assert gone not in js, gone
    for gone in ('label: "Live deployments"', 'label: "Hourly burn"', "dp-tiles"):
        assert gone not in js, gone
    assert "nowRunning(pad, m);" in js


def test_a_stat_never_invents_a_trend_or_a_second_opinion():
    """None of the page stat rows has a comparable previous period on its
    endpoint, so none of them carries a delta or a chart. And the one figure
    that IS also printed further down the page is filled from that section's
    own predicate, so the two can never disagree."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    # no page leads with a stat row any more, so there is nothing to check here
    assert "statRow(pad, [" not in js

    # the folded provider strip is written by the grid it summarises
    grid = js[js.index('ph.append(el("span","setup-n"'):]
    assert 'document.getElementById("provsum")' in grid[:400]
    assert "AUTH.setProv" in grid[:1400]
    # an unpriced endpoint is never counted as free
    assert "const priced = billing.filter(d => depRate(d) != null);" in js
    # the burn is what bills NOW: serving or booting, never an app asleep at zero
    assert "const billing = up.concat(booting);" in js
    assert 'd.status === "scaled_to_zero"' in js and '{ v: asleep.length, k: "asleep" }' in js
    assert '"burn · " + unpriced + " unpriced"' in js


def test_the_breadcrumb_is_a_trail_you_can_walk_back_up():
    """A page you have narrowed says so, and the chevron drops the step you
    took to get there. A page that is only itself gets no back affordance —
    a button that does nothing is furniture."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    fn = js[js.index("function setCrumb(extra) {"):js.index("function openFamily(")]
    assert "const levels = Array.isArray(extra) ? extra.filter(Boolean) : [];" in fn
    assert "if (levels.length) {" in fn, "no back affordance without a level to go back to"
    assert 'el("button","crumb-b"' in fn and 'back.setAttribute("aria-label"' in fn
    assert r'c.append(el("span","sep", "\u203a"));' in fn, "a trail separator, not a slash"
    # an intermediate level is clickable; the one you are on is text
    assert 'if (i === levels.length - 1) { c.append(el("span","cs", lv.label)); return; }' in fn
    assert 'el("button","crumb-l", lv.label)' in fn
    # a plain summary string is still a summary, not a level
    assert 'if (!levels.length && extra) { c.append(el("span","sep","/"));' in fn

    # the three narrowings that can actually be undone
    ref = js[js.index("function refreshCrumb() {"):js.index("function openFamily(")]
    assert 'curView === "models" && MODEL_TAB && MODEL_TAB !== "all"' in ref
    assert 'curView === "activity" && ACT.filter !== "all"' in ref
    assert 'curView === "sessions" && curProject' in ref
    assert "openFamily(\"all\")" in ref and 'ACT.filter = "all"' in ref
    # ...and it follows every one of them
    for hook in ("apply(); refreshCrumb();", "renderActivityPage(); refreshCrumb();",
                 "paintSubs(); refreshCrumb();", "  refreshCrumb();\n  document.querySelectorAll(\"#sesscards"):
        assert hook in js, hook

    for c in (".crumb-b {", ".crumb-l {"):
        assert c in css, c
    # a reset is allowed; a drawn edge is not
    back = css.split("  .crumb-b {")[1].split("}")[0]
    assert "border: 0" in back and "border-radius" in back
    assert "outline:" not in back and "1px solid" not in back


def test_nav_order_rename_and_hash_alias():
    """The rail reads Overview, then the two model pages, then the two work
    pages; the number keys follow that order; the `g` chords and the #models
    hash keep working."""
    import re as _re

    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    tabs = _re.findall(r'data-v="(\w+)"[^>]*>.*?<span class="nl">([^<]+)</span>', INDEX_HTML)
    assert tabs == [("home", "Overview"), ("models", "My models"), ("deploy", "Deploy"),
                    ("sessions", "Sessions"), ("activity", "Activity"), ("mcp", "MCP"),
                    ("skills", "Skills"), ("memory", "Memory"), ("config", "Config")]
    assert 'const VIEWS = ["home","models","deploy","sessions","activity","mcp","skills","memory","config"];' in js
    assert '"123456789".indexOf(e.key)' in js and "showTab(VIEWS[i])" in js
    # the chords still address pages by name, and #models still resolves
    assert 'm: "models"' in js and 'o: "home"' in js and 's: "sessions"' in js
    assert 'models: "My models"' in js
    assert 'tabFromHash' in js and 't === "models"' in js


def test_nav_rows_and_segmented_controls_are_pills():
    from mantis_agent.serve_ui import INDEX_HTML

    css = _css()
    nav_on = css.split("#nav .ngi > button.on {")[1].split("}")[0]
    # selection is neutral; green means primary/live/healthy, never "selected"
    assert "background: var(--fill)" in nav_on and "color: var(--ink)" in nav_on
    assert "#nav .ngi > button.on .ic { color: var(--accent); }" in css
    assert "::after" not in css.split("#nav .ngi > button.on")[1].split("\n")[0]
    assert "#nav button.on::after" not in css
    chip_on = css.split(".fchip.on {")[1].split("}")[0]
    assert "background: var(--panel)" in chip_on and "color: var(--ink)" in chip_on
    base = css.split("  #nav .ngi > button {")[1].split("}")[0]
    # radii come from one small set now (6 / 8 / 12)
    # Notion-like rows: 30px tall, 6px corners
    assert "padding: 6px 10px" in base and "border-radius: 6px" in base and "height: 30px" in base and "transition: background var(--t)" in base
    # the active row changes colour and fill only — never weight, so the rail
    # never reflows under the cursor
    assert "font-weight" not in nav_on and 'class="k"' not in INDEX_HTML and "#nav button .k" not in css
    # the icon is coloured by the row it sits in, not by a rule of its own
    assert "#nav .ngi > button.on .ic { color: var(--accent); }" in css
    assert ".sec-t::after" not in css                      # no rule after section labels
    zero = css.split(".zero {")[1].split("}")[0]
    assert "dashed" not in zero and "background: var(--panel-2)" in zero


def test_theme_tokens_are_neutral_and_defined_for_both_schemes():
    css = _css()
    assert "prefers-color-scheme: dark" in css and ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css
    dark = css.split(':root[data-theme="dark"]')[1].split("}")[0]
    for tok in ("--bg: #191919", "--panel: #202020", "--line: rgba(255,255,255,.08)", "--ink: #e6e6e4", "--ink-2: #a5a5a2"):
        assert tok in dark, tok
    light = css.split(":root {")[1].split("}")[0]
    for tok in ("--bg: #f3f4f6", "--panel: #ffffff", "--panel-2: #f8f9fa", "--ink: #0e0f11", "--ok:", "--warn:", "--bad:", "--info:"):
        assert tok in light, tok
    # olive is gone from the surfaces
    for old in ("#efece5", "#0d0f0a", "#1a1d15", "#14160f"):
        assert old not in css, old
    # body text is 14px — Notion's reading size, not a dense 13
    assert "font-size: 14px" in css.split("\n  body {")[1].split("}")[0]


def test_page_carries_the_shell_cards_transcript_and_palette(home):
    httpd, base = _boot()
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            page = r.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
    for marker in ('<header id="top">', 'id="cmdk"', 'id="themebtn"', 'id="lanind"', 'id="palette"', 'id="palin"',
                   'data-v="home" class="on"><i class="ic" data-i="home"></i><span class="nl">Overview</span>',
                   'data-v="config"><i class="ic" data-i="config"></i><span class="nl">Config</span>',
                   'data-v="activity"><i class="ic" data-i="activity"></i>', 'data-v="mcp"><i class="ic" data-i="mcp"></i>',
                   'id="rail"', 'id="railtog"', 'id="crumb"', 'id="sub-models"', 'id="n-sessions"',
                   'id="activitypad"', "loadActivity", "renderActivityPage",
                   "counts_7d", "ACT_FILTERS", 'a: "activity"', '"123456789"', "xrow", "Load more",
                   "function patchList", "function skeleton", "function md(", "function splitMeta", "function ctxToggle",
                   "function toolCall", "META_RE", "system-reminder", "show context (",
                   'id="projcards"', 'id="sesscards"', "pcard", "selectProject", "selectSession", "UUIDISH",
                   "renderTopStatus", "watchEvents", "/api/events?", "EVENTS_OK", "openPalette", "paletteItems",
                   'e.key.toLowerCase() === "k"', "cycleTheme", "applyTheme", 'get("theme")', "trackJob({ id: r.job, kind: \"connect\"",
                   "grid-template-columns: 280px 320px", "repeat(auto-fill, minmax(260px, 1fr))", "max-width: 900px",
                   "CHORDS", "visibilitychange", "sessfind", "fam-grid", "spend-card", "mm-grid", "mmcard"):
        assert marker in page, marker
    for gone in ("live-act", "renderActivitySummary", "act-sum", "see all"):
        assert gone not in page, gone
    assert "cdn." not in page and "googleapis" not in page
    for marker in ("dp-mgrid", "mcard", "bigMark", "providerDescriptor", "dcard", "color-mix(in srgb", "VRAM_CAP_GB"):
        assert marker in page, marker


# ==========================================================================
# The rail redesign: eight pages, each with its own mark, counts that say
# whether a page has anything in it, and sub-rows under the page you are on.
# ==========================================================================
def test_every_page_in_the_rail_carries_a_mark():
    """A collapsed rail is marks only, so every page needs one — drawn on the
    same 24-unit grid at the same stroke, in currentColor so the row colours
    it."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    views = re.search(r"const VIEWS = \[([^\]]+)\]", js).group(1).replace('"', "").split(",")
    icons = js[js.index("const ICONS = {"):js.index("// One helper, three sizes")]
    for v in views:
        assert ('data-i="%s"' % v) in INDEX_HTML, v          # the row asks for it
        assert ("\n  %s:" % v) in icons, v                   # the set defines it
    # one drawing style, stated once and inherited by every glyph
    assert 'stroke="currentColor" stroke-width="1.7"' in js and 'viewBox="0 0 24 24"' in js
    assert js.count("const IC0 = ") == 1
    # the readings and card heads draw from the same set
    for name in ("msg", "tool", "spend", "streak", "key", "clock", "folder", "trace"):
        assert ("\n  %s:" % name) in icons, name
    # the marks are painted into the served markup, never hand-inlined per row
    assert 'i.ic[data-i]' in js and "function paintIcons(root)" in js


def test_sub_rows_open_only_under_the_page_you_are_on():
    """The rail expands one section — the page you are on — and only when it
    has rows. Everything else stays a list of pages."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    sub = js[js.index("function subRows(v)"):js.index("// The bar says where you are")]
    assert "const rows = v === curView ? subRows(v) : [];" in sub
    assert "const open = rows.length > 0 && SUB_OPEN;" in sub
    # the three pages that have something worth listing
    for v in ('v === "models"', 'v === "sessions"', 'v === "deploy"'):
        assert v in sub, v
    for v in ("models", "deploy", "sessions"):
        assert ('id="sub-%s"' % v) in INDEX_HTML, v
    # a caret exists only on an open page that has rows, and it folds them
    # the 8px caret rendered as a stray dot after the active row — it is gone
    assert "#nav .ngi > button.has-sub .ncar { display: none; }" in css
    assert 'if (b.dataset.v === curView && e.target.closest(".ncar")) { SUB_OPEN = !SUB_OPEN; paintSubs(); return; }' in js
    # rows repaint from data the pages already fetched — no request to open one
    assert js.count("paintSubs()") >= 5


def test_the_rail_folds_and_remembers():
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    assert 'const RAIL_KEY = "mantis-rail";' in js and 'localStorage.setItem(RAIL_KEY' in js
    assert '(e.metaKey || e.ctrlKey) && e.key === "\\\\"' in js and "toggleRail()" in js
    # a narrow window folds it without spending the saved preference
    assert 'NARROW = window.matchMedia("(max-width: 1000px)")' in js
    assert "applyRail(!NARROW.matches && railPref(), false)" in js
    # folded, the words go and the marks stay
    folded = css.split('body[data-rail="min"] .nl,')[1].split("}")[0]
    assert "display: none" in folded
    assert 'body[data-rail="min"] { --rail-w: 60px; }' in css


def test_overview_is_four_readings_then_two_columns():
    """Four tiles across the top, then a column of instruments and a column of
    state. The tile window is named by the data, so an empty week never reads
    as an empty machine."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    home = js[js.index("async function loadHome()"):js.index("// ====", js.index("async function loadHome()"))]
    assert 'const WIN = sumWin(7, 0, "msgs") ? 7 : sumWin(30, 0, "msgs") ? 30 : 0;' in home
    assert 'const wlab = WIN ? " · " + WIN + "d" : " · all time";' in home
    for label in ('"Messages" + wlab', '"Tool calls" + wlab', '"Spend"', '"Trace"', '"Projects"', '"Providers"',
                  '"When you work"', '"What it reaches for"', '"What ran"'):
        assert label in home, label
    # instruments left, state right — never mixed
    assert 'const L = el("div","ov-c"), R = el("div","ov-c");' in home
    assert "grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr)" in css
    assert ".tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr))" in css
    # one grammar for the three micro-charts, all drawn edge to edge
    for fn in ("function sparkLine(", "function sparkBars(", "function dayBlocks("):
        assert fn in js, fn
    assert js.count('preserveAspectRatio="none"') >= 3
    # a move under a point is noise, and noise is not tinted
    dlt = js[js.index("function deltaEl("):js.index("function tile(")]
    assert 'const cls = pc >= 1 ? "up" : pc <= -1 ? "dn" : "";' in dlt and "if (!prev) return null;" in dlt


def test_the_five_families_are_rows_not_cards():
    """On the Overview the families read as standings — one row each, state on
    the right until you hover, when it becomes the probe that changes it."""
    from mantis_agent.serve_ui import INDEX_HTML

    css, js = _css(), INDEX_HTML.split("<script>")[1]
    fam = css.split("  .fam {")[1].split("}")[0]
    assert "grid-template-columns: 26px minmax(0, 1fr) auto" in fam
    # every cell is placed: an item with a definite row is laid out before the
    # auto ones, which is what put the name last when they were not
    for cell in (".fam .mark2 { grid-column: 1;", "  .fam .fn { grid-column: 2; grid-row: 1;",
                 "  .fam .fm { grid-column: 2; grid-row: 2;", "  .fam .fr { grid-column: 3;"):
        assert cell in css, cell
    assert ".fam:hover .ff, .fam:focus-within .ff { display: flex; }" in css
    assert ".fam:hover .fst, .fam:focus-within .fst { display: none; }" in css
    # the row is no longer its own filled surface — the card it sits in is
    assert ".card, .card2, .dpc, .setup," in css and ".fam, .dpc" not in css
    assert 'const grid = el("div","provs"); grid.id = "fam-grid";' in js


def test_the_bar_says_where_you_are():
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    assert 'function setCrumb(extra)' in js and "c.append(icon(curView));" in js
    # the page is the head of the trail, and it is always printed
    assert 'const home = el("b", null, PAGE_NAMES[curView] || curView);' in js and "c.append(home);" in js
    assert js.count("const PAGE_NAMES = ") == 1        # the rail and the palette share one list
    # the crumb names the page; the page's readings carry its numbers
    assert 'if (curView === "home") setCrumb();' in js
