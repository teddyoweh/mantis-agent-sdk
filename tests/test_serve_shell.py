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
            raised.append(line.strip())
    # exactly one raised surface in the whole stylesheet, and it is the panel
    assert len(raised) == 1, raised
    assert "var(--dim)" in raised[0], "the raise takes the theme's own dim"
    panel = css.split("  .ac-panel {")[1].split("}")[0]
    assert "box-shadow" in panel, "the one raise belongs to the floating panel"
    assert "position: absolute" in panel, "and only a layered surface earns it"


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
    for gone in ("Test this route", "routeBtn", "routeBar", "hero-lbl", "m.recent"):
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
    # one grid definition, shared: the two pickers cannot drift apart
    assert ".dp-mgrid, .mm-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));" in css
    # the card is .mcard plus a page modifier, never a second card component
    assert 'el("div","mcard mmcard"' in js
    for shared in (".mcard {", ".mcard .mh {", ".omark {", ".mcard .mt {", ".mcard .mo {", ".mcard .mp2 {"):
        assert shared in css, shared
    # the table, its header, its rows and its sticky family stripe are gone
    for dead in (".mtable", ".mrow", ".mhead", ".mfam"):
        assert dead not in css, dead
    for dead in ('el("div","mtable")', 'el("div","mhead")', 'el("div","mrow"', 'el("div","mfam")',
                 '"mprice"', '"mctx"', '"mcaps"', "priceCell"):
        assert dead not in js, dead
    # grouped by source, as a labelled grid per group with its count
    assert 'el("div","mm-glabel")' in js and 'el("div","mm-grid")' in js
    assert 'el("span","mm-gn", rows.length + " model"' in js


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
    # the facts, as quiet pills
    assert 'pill(fmtCtx(a.info.ctx), " context")' in card
    assert "pills.append(pricePill(pr));" in card
    for cap in ('"cap","tools"', '"cap","effort"', '"cap","thinks"', '"cap ok","loaded"'):
        assert cap in card, cap
    # readiness is stated once, and never on the card that is already in use
    assert card.count('pill("needs a key"') == 1
    assert 'if (!a.enabled && !on) {' in card
    # ONE action, and the current card has none: its tag already says so
    assert card.count('"mm-act"') == 1
    assert 'if (!on) foot.append(el("span","mm-act", a.enabled ? "use \u2192" : "unlock \u2192"));' in card
    # the same slanted Current tag the provider cards wear, and the same motif
    assert 'el("span","ac-st cur")' in card and 'el("span","ac-stl", "Current")' in card
    assert "foot.append(curMotif());" in card
    # unlock still deep-links into that family's setup
    assert "unlockFamily(fid)" in card and "useModel(a.model, a.backend)" in card
    # the local size is said in the caption, "loaded" only as a tag
    assert '"local \u00b7 " + fmtBytes(om.size),' in js and '" \u00b7 loaded"' not in js


def test_the_motif_and_the_action_cannot_collide_on_a_model_card():
    """They are laid out side by side in the card's foot, not stacked on top
    of each other — so no width can bring them together."""
    css = _css()
    foot = css.split("  .mm-foot {")[1].split("}")[0]
    assert "display: flex" in foot and "align-items: center" in foot
    assert "margin-top: auto" in foot                      # the foot is always last
    act = css.split("  .mm-act {")[1].split("}")[0]
    assert "margin-left: auto" in act                      # ...and the action is always right
    # inside the foot the motif is a laid-out sibling, not an absolute overlay
    assert "  .mm-foot .ac-mot { position: static; flex: none; }" in css
    # the head gives its 70px reservation back unless something sits top-right
    assert "  .mmcard .mh { padding-right: 0; }" in css
    assert "  .mmcard.on .mh { padding-right: 84px; }" in css
    assert "  .mmcard > .ac-st.cur { position: absolute; top: 12px; right: 13px; }" in css
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


def test_models_state_no_longer_ships_the_recent_list(home):
    from mantis_agent import serve

    m = serve.models_state()
    assert "recent" not in m and {"current", "providers", "families", "model_info", "ollama"} <= set(m)


def test_model_picker_filters_by_company_and_shows_recency():
    """The company pills come from the results, the New pill and the Recent
    sort exist, dates read relative inside a year and absolute beyond it, and
    the VRAM bar is scaled against real GPUs rather than a fixed 80 GB."""
    from mantis_agent.serve_ui import INDEX_HTML

    js, css = INDEX_HTML.split("<script>")[1], _css()
    for marker in ("renderOrgPills", "modelShown", "paintModels", "DEPLOY.org", "dp-orgs", "whenText",
                   "isFresh", "gpuCeiling", "DEPLOY.gpuMax", "writeDeployHash", '"Recent"', '"New"'):
        assert marker in js, marker
    # the pills are derived from the current results, with counts and marks
    pills = js[js.index("function renderOrgPills("):js.index("function modelShown(")]
    assert "orgMark(o)" in pills and "counts[o]" in pills and 'add("all", "All"' in pills
    # org + New combine with the search box and the sort control
    shown = js[js.index("function modelShown("):js.index("function paintModels(")]
    assert "DEPLOY.org" in shown and "DEPLOY.fresh" in shown
    # the date rule: months inside a year, month+year beyond it
    when = js[js.index("function whenText("):js.index("const isFresh")]
    assert "365 * DAY" in when and "month: \"short\", year: \"numeric\"" in when and "updated today" in when
    # the bar is measured against the provider's largest card and coloured by fit
    assert "DEPLOY.gpuMax[DEPLOY.provider]" in js and "frac > 1 ?" in js
    for cls in (".mcard .vr .vbar.fits i", ".mcard .vr .vbar.tight i", ".mcard .vr .vbar.no i"):
        assert cls in css, cls
    assert "size unknown" in js and "not in the vLLM support list" in js
    # the GPU-provider toggle left the model picker for Fit & deploy
    assert 'fSec.querySelector(".sec-t").append(providerToggle())' in js


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
    # ...and the token line ends that row instead of sitting in its middle
    assert 'hfState.id = "hf-state"; renderHfState(hfState); sub.append(hfState)' in pick
    assert ".dp-sub .dp-status { margin-left: auto; }" in css
    assert ".dp-sub .hf-state { margin-left: 0; }" in css

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
    assert "orgName(o)" in pills
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
    assert '" curated · search above to reach all of Hugging Face"' in pick
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
    assert 'section(pad, "Providers · " + readyN' in js and 'section(pad, "GPU providers · "' in js
    # section labels are normal-weight title case now — the all-caps mono style is gone
    sec = css.split(".sec-t {")[1].split("}")[0]
    assert "uppercase" not in sec and "var(--mono)" not in sec
    for lab in ('"Pick a model"', '"Fit & deploy"', '"Deployments"', '"Choose a model"', '"Local models · Ollama"',
                '"Providers"', '"Spend & usage"', "Curated · good first deploys", '"MCP servers"'):
        assert lab in js, lab


def test_nav_order_rename_and_hash_alias():
    """My models sits right after Overview; the number keys follow the nav
    order; the `g` chords and the #models hash keep working."""
    import re as _re

    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    tabs = _re.findall(r'data-v="(\w+)"[^>]*>([^<]+)</button>', INDEX_HTML)
    assert tabs == [("home", "Overview"), ("models", "My models"), ("sessions", "Sessions"),
                    ("activity", "Activity"), ("deploy", "Deploy"), ("mcp", "MCP"),
                    ("skills", "Skills"), ("config", "Config")]
    assert 'const VIEWS = ["home","models","sessions","activity","deploy","mcp","skills","config"];' in js
    assert '"12345678".indexOf(e.key)' in js and "showTab(VIEWS[i])" in js
    # the chords still address pages by name, and #models still resolves
    assert 'm: "models"' in js and 'o: "home"' in js and 's: "sessions"' in js
    assert 'models: "My models"' in js
    assert 'tabFromHash' in js and 't === "models"' in js


def test_nav_tabs_and_segmented_controls_are_pills():
    from mantis_agent.serve_ui import INDEX_HTML

    css = _css()
    nav_on = css.split("#nav button.on {")[1].split("}")[0]
    assert "background: var(--accent-soft)" in nav_on and "color: var(--accent)" in nav_on
    assert "::after" not in css.split("#nav button.on")[1].split("\n")[0]
    assert "#nav button.on::after" not in css
    chip_on = css.split(".fchip.on {")[1].split("}")[0]
    assert "background: var(--accent-soft)" in chip_on
    base = css.split("  #nav button {")[1].split("}")[0]
    assert "padding: 6px 10px" in base and "border-radius: 6px" in base and "transition: background var(--t)" in base
    assert "font-weight" not in nav_on and 'class="k"' not in INDEX_HTML and "#nav button .k" not in css
    assert "gap: 3px" in css.split("  #nav {")[1].split("}")[0]
    assert ".sec-t::after" not in css                      # no rule after section labels
    zero = css.split(".zero {")[1].split("}")[0]
    assert "dashed" not in zero and "background: var(--panel-2)" in zero


def test_theme_tokens_are_neutral_and_defined_for_both_schemes():
    css = _css()
    assert "prefers-color-scheme: dark" in css and ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css
    dark = css.split(':root[data-theme="dark"]')[1].split("}")[0]
    for tok in ("--bg: #0a0b0d", "--panel: #111316", "--line: rgba(255,255,255,.08)", "--ink: #ededed", "--ink-2: #9a9ea6"):
        assert tok in dark, tok
    light = css.split(":root {")[1].split("}")[0]
    for tok in ("--bg: #eff1f4", "--panel: #ffffff", "--panel-2: #f4f5f7", "--ink: #111111", "--ok:", "--warn:", "--bad:", "--info:"):
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
                   'data-v="home" class="on">Overview', 'data-v="config">Config</button>',
                   'data-v="activity">Activity</button>', 'data-v="mcp">MCP</button>', 'id="activitypad"', "loadActivity", "renderActivityPage",
                   "counts_7d", "ACT_FILTERS", 'a: "activity"', '"12345678"', "xrow", "Load more",
                   "function patchList", "function skeleton", "function md(", "function splitMeta", "function ctxToggle",
                   "function toolCall", "META_RE", "system-reminder", "show context (",
                   'id="projcards"', 'id="sesscards"', "pcard", "selectProject", "selectSession", "UUIDISH",
                   "renderTopStatus", "watchEvents", "/api/events?", "EVENTS_OK", "openPalette", "paletteItems",
                   'e.key.toLowerCase() === "k"', "cycleTheme", "applyTheme", 'get("theme")', "rollback",
                   "grid-template-columns: 280px 320px", "repeat(auto-fill, minmax(260px, 1fr))", "max-width: 900px",
                   "CHORDS", "visibilitychange", "sessfind", "fam-grid", "spend-card", "mm-grid", "mmcard"):
        assert marker in page, marker
    for gone in ("live-act", "renderActivitySummary", "act-sum", "see all"):
        assert gone not in page, gone
    assert "cdn." not in page and "googleapis" not in page and "<aside" not in page
    for marker in ("dp-mgrid", "mcard", "bigMark", "providerDescriptor", "gcard", "color-mix(in srgb", "VRAM_CAP_GB", "dp-glabel"):
        assert marker in page, marker
