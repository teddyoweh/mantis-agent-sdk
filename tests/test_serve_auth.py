"""Tests for the provider setup surface — every way to authenticate each
family (Claude via API key, subscription OAuth, Vertex, Bedrock, Azure; the
same shape for the rest).

The dashboard is a JSON skin over the frozen ``mantis_agent.auth_methods``
contract, so everything here runs against a *fake*: the real dataclasses with
monkeypatched module functions. That pins the dashboard's own behaviour —
input validation, redaction, method switching, the OAuth two-step, graceful
degradation — independently of the providers work.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

KEY = "sk-ant-api03-" + "Z" * 40
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    """A fake auth_methods: real AuthMethod/AuthField objects, fake state."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(h))
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")

    from mantis_agent import auth_methods as A
    from mantis_agent import serve

    for cache in (serve._ollama_cache, serve._projects_cache, serve._analytics_cache, serve._spend_cache):
        cache.clear()

    calls: dict[str, list] = {}
    state = {"saved": {}, "active": {}, "fail_set": False, "no_impl": False, "oauth": {}}

    def rec(name, *a, **kw):
        calls.setdefault(name, []).append((a, kw))

    real_methods = A.auth_methods          # bind before patching, or _methods recurses

    def _methods(family):
        if state["no_impl"]:
            raise NotImplementedError
        if family not in A.FAMILIES:
            raise ValueError(f"unknown provider family {family!r}")
        return list(real_methods(family))

    def method_status(family):
        if state["no_impl"]:
            raise NotImplementedError
        out = {}
        for m in _methods(family):
            saved = state["saved"].get((family, m.id)) or {}
            configured = bool(saved) or m.id == "bedrock"          # bedrock: ambient CLI creds
            src = "saved" if saved else ("cli" if m.id == "bedrock" else None)
            out[m.id] = {
                "configured": configured, "source": src,
                "active": state["active"].get(family) == m.id,
                "hint": "profile:default credentials" if src == "cli" else ("saved" if saved else "set " + (m.fields[0].env if m.fields else "nothing")),
                # the contract hands back masked hints; one is deliberately WHOLE
                # here so the dashboard's own re-masking is exercised
                "masked": {k: (v if k == "AWS_SECRET_ACCESS_KEY" else v[:3] + "…" + v[-4:]) for k, v in saved.items()},
                "label": m.label, "kind": m.kind, "backend": m.backend,
            }
        return out

    def set_method(family, method_id, values):
        rec("set_method", family, method_id, dict(values))
        if state["no_impl"]:
            raise NotImplementedError
        if state["fail_set"]:
            return {"ok": False, "family": family, "method": method_id, "backend": None,
                    "message": "NOPE_API_KEY is not a field of " + family}
        state["saved"][(family, method_id)] = dict(values)
        state["active"][family] = method_id
        m = next(x for x in _methods(family) if x.id == method_id)
        return {"ok": True, "family": family, "method": method_id, "backend": m.backend,
                "message": "saved and active"}

    def clear_method(family, method_id):
        rec("clear_method", family, method_id)
        state["saved"].pop((family, method_id), None)
        if state["active"].get(family) == method_id:
            state["active"].pop(family)
        return {"ok": True, "family": family, "method": method_id, "cleared": ["X"], "message": "forgotten"}

    async def validate_method(family, method_id, *, model=None):
        rec("validate_method", family, method_id, model)
        if method_id == "azure":
            return {"ok": False, "message": "404 from https://x.azure.com — check the endpoint", "models": [], "latency_ms": 40}
        return {"ok": True, "message": "reached", "models": ["claude-opus-5", "claude-sonnet-5"], "latency_ms": 123}

    def oauth_start(family):
        rec("oauth_start", family)
        if family != "anthropic":
            raise ValueError(f"{family} has no browser login; use an API key")
        state["oauth"]["h1"] = True
        return {"url": "https://claude.ai/oauth/authorize?code=1", "handle": "h1",
                "instructions": "Approve in the tab, then paste the code."}

    def oauth_finish(handle, code):
        rec("oauth_finish", handle, code)
        if handle not in state["oauth"]:
            return {"ok": False, "family": "anthropic", "method": "oauth", "message": "unknown handle"}
        state["saved"][("anthropic", "oauth")] = {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-" + "Q" * 40}
        state["active"]["anthropic"] = "oauth"
        return {"ok": True, "family": "anthropic", "method": "oauth", "backend": "anthropic",
                "message": "signed in · expires in 8h"}

    monkeypatch.setattr(A, "auth_methods", _methods)
    monkeypatch.setattr(A, "method_status", method_status)
    monkeypatch.setattr(A, "set_method", set_method)
    monkeypatch.setattr(A, "clear_method", clear_method)
    monkeypatch.setattr(A, "validate_method", validate_method)
    monkeypatch.setattr(A, "oauth_start", oauth_start)
    monkeypatch.setattr(A, "oauth_finish", oauth_finish)
    return {"serve": serve, "calls": calls, "state": state}


# ---------------------------------------------------------------------------
# families & methods
# ---------------------------------------------------------------------------


def test_families_list_every_way_to_connect(fake):
    serve = fake["serve"]
    r = serve.auth_families()
    assert r["ok"]
    fams = {f["family"]: f for f in r["families"]}
    assert list(fams) == ["anthropic", "openai", "gemini", "xai", "oss"]
    c = fams["anthropic"]
    assert c["label"] == "Claude" and c["logo"] == "anthropic" and c["ui_family"] == "anthropic"
    assert c["method_count"] == 5 and c["recommended"] == "api_key"
    assert c["status_line"] == "Configured, not active"          # bedrock CLI creds, nothing active
    assert fams["gemini"]["ui_family"] == "google"               # the models page's family id
    assert fams["oss"]["label"] == "Open models"
    assert r["connected_count"] == 0


def test_methods_carry_fields_status_and_no_values(fake):
    serve = fake["serve"]
    d = serve.auth_family_methods("anthropic")
    assert d["ok"] and [m["id"] for m in d["methods"]] == ["api_key", "oauth", "vertex", "bedrock", "azure"]
    api = d["methods"][0]
    assert api["kind"] == "api_key" and api["recommended"] is True and api["backend"] == "anthropic"
    assert api["fields"] == [{"env": "ANTHROPIC_API_KEY", "label": "API key", "secret": True,
                              "required": True,
                              "help": "sk-ant-api… from console.anthropic.com → Settings → API Keys",
                              "placeholder": "sk-ant-api03-…"}]
    oauth = d["methods"][1]
    assert oauth["kind"] == "oauth" and oauth["fields"] == [] and oauth["token_env"] == "ANTHROPIC_AUTH_TOKEN"
    bedrock = next(m for m in d["methods"] if m["id"] == "bedrock")
    assert bedrock["kind"] == "cloud" and bedrock["status"]["source"] == "cli" and bedrock["status"]["configured"] is True
    assert len(bedrock["fields"]) == 5 and any(not f["secret"] for f in bedrock["fields"])
    assert serve.auth_family_methods("nope")["ok"] is False
    assert serve.auth_family_methods("")["ok"] is False


# ---------------------------------------------------------------------------
# set / clear / validate — and redaction
# ---------------------------------------------------------------------------


def test_set_method_saves_and_never_echoes_the_value(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.auth_set("anthropic", "api_key", {"ANTHROPIC_API_KEY": KEY, "BLANK": "  "})
    assert r["ok"] and r["saved"] == ["ANTHROPIC_API_KEY"] and r["backend"] == "anthropic"
    assert KEY not in json.dumps(r) and KEY[:16] not in json.dumps(r)
    assert calls["set_method"][0][0] == ("anthropic", "api_key", {"ANTHROPIC_API_KEY": KEY})
    # the fresh status rides along so the panel repaints without a second call
    st = {m["id"]: m for m in r["status"]["methods"]}
    assert st["api_key"]["status"]["active"] is True and st["api_key"]["status"]["configured"] is True
    assert serve.auth_families()["families"][0]["status_line"].startswith("Connected via API key")
    assert serve.auth_set("", "api_key", {})["ok"] is False
    assert serve.auth_set("anthropic", "", {})["ok"] is False


def test_masked_hints_are_remasked_by_the_dashboard(fake):
    """The contract masks its hints, but a whole-looking value must never get
    through this layer either."""
    serve = fake["serve"]
    serve.auth_set("anthropic", "bedrock", {"AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
                                            "AWS_SECRET_ACCESS_KEY": AWS_SECRET})
    d = serve.auth_family_methods("anthropic")
    bed = next(m for m in d["methods"] if m["id"] == "bedrock")
    assert AWS_SECRET not in json.dumps(d)
    assert bed["status"]["masked"]["AWS_SECRET_ACCESS_KEY"] == serve._mask_key(AWS_SECRET)
    assert bed["status"]["masked"]["AWS_ACCESS_KEY_ID"] == "AKI…MPLE"      # already masked, left alone


def test_switching_the_active_method(fake):
    serve = fake["serve"]
    serve.auth_set("anthropic", "api_key", {"ANTHROPIC_API_KEY": KEY})
    assert serve.auth_families()["families"][0]["active"] == "api_key"
    serve.auth_set("anthropic", "vertex", {"GOOGLE_CLOUD_PROJECT": "my-proj"})
    f = serve.auth_families()["families"][0]
    assert f["active"] == "vertex" and set(f["configured"]) == {"api_key", "vertex", "bedrock"}
    assert f["status_line"].startswith("Connected via Google Vertex AI")
    # forgetting the active one leaves the others configured
    r = serve.auth_clear("anthropic", "vertex")
    assert r["ok"] and serve.auth_families()["families"][0]["active"] is None
    assert serve.auth_clear("anthropic", "")["ok"] is False


def test_validate_reports_latency_models_or_the_explained_error(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.auth_validate("anthropic", "api_key")
    assert r["ok"] and r["latency_ms"] == 123 and r["models"] == ["claude-opus-5", "claude-sonnet-5"]
    assert calls["validate_method"][0][0] == ("anthropic", "api_key", None)
    bad = serve.auth_validate("anthropic", "azure")
    assert bad["ok"] is False and "check the endpoint" in bad["message"]
    serve.auth_validate("anthropic", "api_key", "claude-opus-5")
    assert calls["validate_method"][-1][0][2] == "claude-opus-5"
    assert serve.auth_validate("", "api_key")["ok"] is False


# ---------------------------------------------------------------------------
# the OAuth two-step
# ---------------------------------------------------------------------------


def test_oauth_start_then_finish(fake):
    serve, calls = fake["serve"], fake["calls"]
    r = serve.auth_oauth_start("anthropic")
    assert r["ok"] and r["url"].startswith("https://claude.ai/") and r["handle"] == "h1"
    assert "paste the code" in r["instructions"]
    bad = serve.auth_oauth_start("openai")
    assert bad["ok"] is False and "no browser login" in bad["error"]
    assert serve.auth_oauth_finish("h1", "")["ok"] is False
    assert serve.auth_oauth_finish("", "code")["ok"] is False
    fin = serve.auth_oauth_finish("h1", "abc123#state")
    assert fin["ok"] and fin["method"] == "oauth" and "expires in 8h" in fin["message"]
    assert calls["oauth_finish"][-1][0] == ("h1", "abc123#state")
    f = serve.auth_families()["families"][0]
    assert f["active"] == "oauth" and f["active_kind"] == "oauth"
    assert f["status_line"].startswith("Connected via Claude subscription")
    assert "Q" * 40 not in json.dumps(f)                      # the token never leaves
    assert serve.auth_oauth_finish("nope", "x")["ok"] is False


def test_endpoints_degrade_when_the_contract_is_unimplemented(fake):
    serve, state = fake["serve"], fake["state"]
    state["no_impl"] = True
    fams = serve.auth_families()
    assert fams["ok"] and all(f["ok"] is False for f in fams["families"])
    assert "isn't available" in fams["families"][0]["error"]
    d = serve.auth_family_methods("anthropic")
    assert d["ok"] is False and "isn't available" in d["error"] and d["hint"]
    r = serve.auth_set("anthropic", "api_key", {"ANTHROPIC_API_KEY": KEY})
    assert r["ok"] is False and "isn't available" in r["error"]


def test_set_method_failure_is_reported_not_raised(fake):
    serve, state = fake["serve"], fake["state"]
    state["fail_set"] = True
    r = serve.auth_set("anthropic", "api_key", {"ANTHROPIC_API_KEY": KEY})
    assert r["ok"] is False and "is not a field of" in r["message"] and KEY not in json.dumps(r)


# ---------------------------------------------------------------------------
# over HTTP
# ---------------------------------------------------------------------------


def _boot(token=None, enforce_get=False):
    from mantis_agent import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    httpd.token = token
    httpd.enforce_get = enforce_get
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


GETS = ("/api/auth/families", "/api/auth/methods?family=anthropic")
POSTS = (("/api/auth/set", {"family": "anthropic", "method": "api_key", "values": {"ANTHROPIC_API_KEY": KEY}}),
         ("/api/auth/validate", {"family": "anthropic", "method": "api_key"}),
         ("/api/auth/oauth/start", {"family": "anthropic"}),
         ("/api/auth/oauth/finish", {"handle": "h1", "code": "abc"}),
         ("/api/auth/clear", {"family": "anthropic", "method": "api_key"}))


def test_every_auth_endpoint_routes_over_http(fake):
    httpd, base = _boot(token="tok")
    try:
        for path in GETS:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                assert r.status == 200 and "ok" in json.loads(r.read()), path
        for path, payload in POSTS:
            req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Mantis-Token": "tok"})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read())
            assert r.status == 200, path
            assert KEY not in json.dumps(body), path
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_auth_endpoints_require_the_token(fake):
    httpd, base = _boot(token="tok", enforce_get=True)
    try:
        for path in GETS:
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(base + path, timeout=5)
            assert ei.value.code == 401, path
        for path, payload in POSTS:
            req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=5)
            assert ei.value.code == 401, path
        assert not fake["calls"].get("set_method")
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def test_page_carries_the_setup_surface_and_the_unlock_deep_link(fake):
    httpd, base = _boot()
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            page = r.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()
    for marker in ("loadAuthFamilies", "renderAuthCards", "authCard", "authEntries", "authMethodForm", "oauthFlow",
                   "unlockFamily", 'section(pad, "Providers")', 'authBox.id = "auth-cards"', "acard", "ac-types",
                   "/api/auth/families", "/api/auth/set", "/api/auth/validate",
                   "/api/auth/oauth/start", "/api/auth/oauth/finish", "Sign in with", "Save & test",
                   "recommended", "detected", "Ambient credentials detected",
                   "First-party", "Open-source & self-host", "Check reachability", "connected",
                   "chmod 600", "FIRST_PARTY"):
        assert marker in page, marker
    js2 = page.split("<script>")[1]
    # ONE way to connect a provider: the legacy expanding list is gone
    for gone in ("providerDetail", '"Connect a provider"', "Enable provider", "Save new key",
                 "saveKeyFn", "removeKeyFn", "focusProvider", '"/api/key"'):
        assert gone not in js2, gone
    # several methods render as a toggle; a lone one needs no chooser at all
    assert "e.methods.length > 1" in js2 and 'el("div","ac-one"' not in js2
    # the useful parts of the old row survive inside the card
    assert "ac-models" in js2 and "listed" in js2 and "live" in js2
    # grouped, and a card that opens a form must not stretch its neighbours
    assert "auth-grid" in page and "align-items: start" in page
    # a locked model row deep-links into its family's setup, not a generic list
    assert "unlockFamily(fid)" in page and "focusProvider(a.pid)" not in page
    js = page.split("<script>")[1]
    panel = js[js.index("function authMethodForm("):js.index("function probeBox(")]
    assert "secret" in panel and "masked" in panel      # fields render masked + saved hints


def test_collapsed_provider_card_says_four_things_and_nothing_else(fake):
    """A collapsed card carries the mark, the name, a dot-and-word state and
    one ghost action. Endpoint, env var, description, method toggle, models
    and docs all wait until it is opened."""
    from mantis_agent.serve_ui import INDEX_HTML

    js, css = INDEX_HTML.split("<script>")[1], INDEX_HTML.split("<style>")[1].split("</style>")[0]
    card = js[js.index("function authCard(e, panel) {"):js.index("function metaBit(")]
    # the grid always gets the collapsed half; only the floating panel is
    # built with the body, so an open card never changes the grid's geometry
    head, body = card.split("if (!panel) return card;")
    # the collapsed half builds exactly these four things
    assert "bigMark(" in head and 'el("div","fn"' in head and 'el("span","ac-st " + st8.cls)' in head
    assert 'btn(open ? "Close" : st8.cls === "off" ? "Connect" : "Manage", "gho"' in head
    for later in ("ac-types", "authMethodForm", "ac-models", "ac-meta", "ac-d", "metaBit("):
        assert later not in head, "collapsed card renders " + later
        assert later in body, "opened card is missing " + later
    # one card open at a time, remembered on AUTH.open
    assert "if (open) closeAuthPanel(); else openAuthPanel(e.key);" in js
    assert 'const open = AUTH.open === e.key' in js
    # the state is one badge with its own surface, in the header, always present
    assert 'el("span","ac-st " + st8.cls)' in js
    assert "height: 20px" in css.split(".ac-st {")[1].split("}")[0]


def test_provider_card_surface_is_neutral_and_the_active_one_is_pixelated(fake):
    """No colour wash in either theme: the surface is the same panel in every
    state, the current provider is marked by a ring of pixel blocks, and the
    vendor's own colour appears in exactly one place — the mark's square."""
    from mantis_agent.serve_ui import INDEX_HTML

    js, css = INDEX_HTML.split("<script>")[1], INDEX_HTML.split("<style>")[1].split("</style>")[0]
    base = css.split("  .acard {")[1].split("}")[0]
    assert "background: var(--panel)" in base and "min-height" not in base
    # the wash is gone; hover and open are a background STEP, never a tint
    assert ".acard.use { background: var(--accent-soft)" not in css
    assert ".acard:hover { background: var(--panel-2); }" in css
    assert ".acard.open { background: var(--panel-2); }" in css
    for tinted in (".acard.use { background:", ".acard.cur { background:", ".acard.idle { background:",
                   ".acard.off { background:"):
        assert tinted not in css, tinted
    # no rail at all — a solid bar on one edge, present on some cards and
    # absent on others, reads as a rendering fault
    assert "::before" not in css.split("  .acard {")[1].split(".ac-h {")[0]
    # no per-vendor tint anywhere on the card's surfaces: one neutral square
    # for every mark, and the accent — never a vendor hue — on the marker
    marks = js[js.index("function markSvg(m)"):js.index("// ---- models & hosting ----")]
    assert "color-mix" not in marks and "style.background" not in marks
    assert "var(--vendor" not in css
    # two type sizes, two weights
    assert "font-size: 14.5px" in css.split(".ac-h .fn {")[1].split("}")[0]
    assert "font-weight: 400" in css.split(".ac-act {")[1].split("}")[0]
    # 140ms, on hover and expand, and nothing else
    assert "transition: background var(--t)" in base
    assert ".ac-body { transition: opacity 140ms ease; }" in css


def test_opened_card_shows_the_method_control_and_one_filled_action(fake):
    """Opened, the card gains the auth-type control (tablist semantics and
    all), the selected method's fields, what it serves, and exactly one
    filled button."""
    from mantis_agent.serve_ui import INDEX_HTML

    js, css = INDEX_HTML.split("<script>")[1], INDEX_HTML.split("<style>")[1].split("</style>")[0]
    ctl = js[js.index('const seg = el("div","ac-types")'):js.index("card.append(seg);")]
    for bit in ('setAttribute("role", "tablist")', 'setAttribute("role", "tab")', '"aria-selected"',
                "ArrowRight", "ArrowLeft", "Home", "End", "tabIndex", "drawFade"):
        assert bit in ctl, bit
    # the marker sits inside its own segment, under its own class name
    assert 'el("span","ac-mktick"' in js and 'el("span","ac-mkdot"' in js
    assert ".ac-mkdot { flex: none; width: 6px; height: 6px; padding: 0" in css
    # single-purpose names: three collisions taught us not to borrow a modifier
    for stolen in ('"ac-mk act"', '"ac-mk cfg"', '? " live" : ""', ".fchip.live"):
        assert stolen not in js and stolen not in css, stolen
    seg = css.split("  .ac-seg {")[1].split("}")[0]
    assert "height: 26px" in seg and "position: relative" in seg and "flex: none" in seg
    assert ".ac-seg.on { background: var(--accent); color: #fff" in css
    assert ".ac-seg:focus-visible" in css
    # only the opened card's primary action is filled
    form = js[js.index("function authMethodForm("):js.index("function probeBox(")]
    assert form.count('"pri"') == 1 and '"gho"' in form
    # deep link so a card can be opened directly
    assert 'new URLSearchParams(location.search).get("openprov")' in js


def test_at_most_one_provider_is_ever_marked_current(fake, monkeypatch):
    """Several providers can be connected at once, but only one backs the
    model the SDK will use — so "Current" and "Ready" are different states and
    exactly one card may claim the first."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    # Current is read off the provider that serves the current model, never
    # off "this family has an active auth method"
    cur = js[js.index("function isCurrentProvider("):js.index("function cardState(")]
    assert "p.is_current" in cur and 'host.kind === "local"' in cur and 'host.kind === "selfhost"' in cur
    assert "e.active" not in cur                     # that fact means Ready, not Current
    state = js[js.index("function cardState("):js.index("function authCard(")]
    order = [state.index("isCurrentProvider(e)"), state.index("e.active"), state.index("m.status.configured")]
    assert order == sorted(order), "Current must be decided before Ready"
    for cls, label in (("cur", "Current"), ("rdy", "Ready"), ("idle", "Not active"), ("off", "Not connected")):
        assert 'cls: "%s", badge: "%s"' % (cls, label) in state, cls
    # the state element is one badge, present on every card in the same place
    assert 'el("span","ac-st " + st8.cls)' in js and 'el("span","ac-stg")' in js
    # ...and the four states differ by SHAPE as well as hue, so the badge
    # survives a colour-blind reading
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]
    assert ".ac-st.cur .ac-stg { background: currentColor; }" in css      # filled dot
    assert ".ac-st.rdy .ac-stg { border: 1.5px solid currentColor; }" in css   # hollow ring
    assert "transform: rotate(45deg)" in css.split(".ac-st.idle .ac-stg {")[1].split("}")[0]  # diamond
    assert "opacity: .45" in css.split(".ac-st.off .ac-stg {")[1].split("}")[0]               # faint dot
    # a rail that some cards have and others don't reads as a fault: it's gone
    assert ".acard::before" not in css and ".acard.use::before" not in css


def test_no_card_state_wears_an_outline(fake):
    """An outline — however it is drawn — reads as a border at real size, and
    with every connected card wearing one the grid became a field of dotted
    rectangles. Nothing on the auth grid, in any state, may carry one."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]

    # the ring, and everything that drew it, is gone
    for dead in (".ac-pix", "pixMarker", "pixmarch", "ac-dash", "dashMarker"):
        assert dead not in css and dead not in js, dead
    # nothing on the provider grid strokes a dashed path any more (the context
    # diagram elsewhere in the sheet still may — it is drawing a chart, not a
    # card state)
    for sel, block in _all_rules(css):
        if ".ac" in sel or ".dpc" in sel:
            assert "stroke-dasharray" not in block, sel
    # no card rule paints a border, an outline or a ring on any state
    for sel in (".acard", ".acard.cur", ".acard.rdy", ".acard.idle", ".acard.off", ".acard::before"):
        for block in _rules(css, sel):
            for banned in ("border:", "outline:", "border-top", "border-left", "box-shadow"):
                assert banned not in block, (sel, banned, block)
    assert ".acard::before" not in css and ".acard.use::before" not in css


def _all_rules(css):
    """(selector list, declarations) for every block in the sheet."""
    out = []
    for chunk in css.split("}"):
        if "{" not in chunk:
            continue
        head, _, body = chunk.partition("{")
        out.append((head.strip(), body))
    return out


def _rules(css, sel):
    """Every declaration block whose selector list contains exactly `sel`."""
    return [body for head, body in _all_rules(css)
            if sel in [x.strip() for x in head.split(",")]]


def test_the_current_card_is_marked_by_a_pixel_dither_on_its_surface(fake):
    """The active marker sits ON the card and spans it: hard 3px blocks on an
    8px column pitch, spread from the left padding to the right one so the
    card is textured rather than trimmed or decorated in one corner. Current
    and Ready differ in ROW COUNT and DENSITY — they cover the same width, so
    reach cannot carry the difference — which keeps the pair readable in
    greyscale."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]

    box = css.split("  .ac-mot {")[1].split("}")[0]
    # it is painted over the card, costs no layout, and eats no clicks
    assert "position: absolute" in box and "pointer-events: none" in box
    # anchored to the ONE left edge the card already has — the mark's — on an
    # INTEGER offset, and stated wide enough to reach the opposite padding.
    # An <svg> is a replaced element, so left+right would be ignored in favour
    # of its intrinsic 300px: the width has to be spelled out.
    assert "left: 14px" in box and "bottom: 2px" in box
    assert "width: calc(100% - 28px)" in box
    assert "right:" not in box, "left+right does not size a replaced element"
    for banned in ("border", "outline", "stroke"):
        assert banned not in box, banned
    assert "  .ac-mot rect { fill: var(--accent); }" in css
    # Ready is the same field held back: neutral and half-strength
    rdy = css.split("  .acard.rdy .ac-mot rect {")[1].split("}")[0]
    assert "fill: var(--ink-3)" in rdy and "opacity: .5" in rdy
    # the two quiet states carry nothing at all
    for quiet in (".acard.idle .ac-mot", ".acard.off .ac-mot"):
        assert quiet not in css, quiet
    # a dither does not march: animating it reads as noise, not as life
    assert ".ac-mot" not in css.split("@media (prefers-reduced-motion: no-preference) {")[1][:400]

    mk = js[js.index("const MOT_BLK"):js.index("// Current is two rows")]
    # integer block on an integer pitch, and antialiasing off at every scale
    assert "const MOT_BLK = 3, MOT_PITCH = 8, MOT_ROW = 4;" in mk
    assert 'setAttribute("shape-rendering", "crispEdges")' in mk
    assert 'setAttribute("aria-hidden", "true")' in mk
    # no viewBox: one user unit is one CSS pixel at any width, so a band that
    # spans a wider card draws bigger gaps, never bigger blocks
    assert "viewBox" not in mk
    assert 'svg.setAttribute("width"' not in mk, "the width comes from the card, not the markup"

    paint = js[js.index("function paintMotif(svg) {"):js.index("// A ResizeObserver is the right")]
    # every block is placed on the pitch — never a fractional coordinate
    assert 'b.setAttribute("x", c * MOT_PITCH); b.setAttribute("y", r * MOT_ROW);' in paint
    assert 'b.setAttribute("width", MOT_BLK); b.setAttribute("height", MOT_BLK);' in paint
    # the column count comes from the measured box, so the band spans the card
    assert "const cols = Math.floor((w - MOT_BLK) / MOT_PITCH) + 1;" in paint
    # deterministic: the same card draws the same field on every repaint
    assert "Math.random" not in paint
    assert "(c * MOT_STEP + r * MOT_TURN) % MOT_MOD >= fill" in paint
    # ...and it is only redrawn when the width it was drawn at actually moved
    assert 'if (svg.dataset.w === String(w)) return;' in paint
    # a width is only known after layout, and changes when the grid reflows
    assert "new ResizeObserver(" in js and "MOT_RO.unobserve(e.target)" in js

    # Current is deeper and denser than Ready — the whole difference, since
    # both now span the same width
    cur_rows, cur_fill = _motif_args(js, "curMotif")
    rdy_rows, rdy_fill = _motif_args(js, "rdyMotif")
    assert cur_rows > rdy_rows and cur_fill > rdy_fill
    # measured on a 400px card: the numbers the design was picked at
    cur_n, rdy_n = _blocks(50, cur_rows, cur_fill), _blocks(50, rdy_rows, rdy_fill)
    assert 20 <= cur_n <= 30, cur_n            # spread thin, not a heavier band
    assert cur_n > 3 * rdy_n, (cur_n, rdy_n)   # readable without colour

    # the band lives in the 12px strip below the 39px head, so it cannot reach
    # the mark, the name, the badge or the action — and the height is untouched
    assert ".acard:not(.open) { height: 63px; }" in css
    assert ".acard .ac-h { height: 39px; }" in css
    assert 2 + cur_rows * 4 <= 12, "the band must stay inside the bottom padding"

    # Current and Ready wear it; nothing else does, and an opened card — a
    # form in a scrolling panel — wears none at all
    assert 'if (panel) { /* the panel states itself in its head */ }' in js
    assert 'else if (st8.cls === "cur") card.append(curMotif());' in js
    assert 'else if (st8.cls === "rdy") card.append(rdyMotif());' in js


def _motif_args(js, name):
    call = js.split("const %s = () => pixMotif(" % name)[1].split(")")[0]
    rows, fill = [int(x) for x in call.split(",")]
    return rows, fill


def _blocks(cols, rows, fill):
    """The field the page draws, counted here from the same integer rule."""
    return sum(1 for c in range(cols) for r in range(rows)
               if (c * 6183 + r * 5000) % 10000 < fill)


def test_one_motif_across_every_card_kind(fake):
    """The provider card, the GPU provider card, the current model's card in
    the grid and the hero that says what you are running are all marked the
    same way, by the same function — and on the cards whose height is not
    fixed the motif is LAID OUT beside the action rather than
    pinned over it, because a card that grows or wraps would otherwise close
    the gap."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]

    # one builder, four callers
    assert js.count("const curMotif = () => pixMotif(") == 1
    assert js.count("card.append(curMotif());") == 3      # provider card, GPU card, hero
    assert 'if (st8.cls === "cur") card.append(curMotif());' in js
    assert "if (p.configured && ready) card.append(curMotif());" in js
    assert "foot.append(curMotif());" in js               # the model card's foot
    # the hero is the current model, so it wears the band too
    hero = js[js.index("function nowRunning(pad, m) {"):js.index("function reachRow(")]
    assert "card.append(curMotif());" in hero
    assert "  .nowcard > .ac-mot { left: 18px; width: calc(100% - 36px); bottom: 6px; }" in css

    # the fixed-height provider card pins it inside its own bottom padding
    assert "position: absolute" in css.split("  .ac-mot {")[1].split("}")[0]
    # the two variable-height cards lay it out instead — and each states a
    # width, because a band that does not span its card is a decoration
    assert "  .dpc .ac-mot { position: static; width: 100%; }" in css
    foot = css.split("  .mm-foot .ac-mot {")[1].split("}")[0]
    assert "position: static" in foot and "flex: 1 1 0" in foot
    # ...and on the GPU card it comes after the engine chips, before the
    # bottom-pinned action row, so no card height can bring the two together
    dpc = js[js.index('const chips = el("div","chips");\n    (p.engines'):]
    dpc = dpc[:dpc.index('const ff = el("div","ff");')]
    assert "card.append(chips);" in dpc and "curMotif()" in dpc
    assert "  .dpc .ff { display: flex; align-items: center; gap: 6px; margin-top: auto;" in css


def test_a_band_is_filled_as_soon_as_its_cards_are_in_the_page(fake):
    """The bands are drawn from the card's measured width, so they cannot be
    drawn until the card has one. Leaving that entirely to the observer left
    the provider grid — which arrives from a fetch — showing empty bands on a
    slow render. Every grid therefore fills its own bands the moment it has
    inserted them, and the observer only handles what changes afterwards."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]

    # one synchronous pass, called by each of the three grids that draw bands
    assert "function paintMotifsNow() { paintMotifs(); }" in js
    assert js.count("paintMotifsNow();") == 4        # three grids, plus every re-sort
    # ...the provider grid, once its cards are in the box and measurable
    auth = js[js.index("function renderAuthCards(box, r) {"):js.index("// the panel is a sibling of the grids")]
    assert "AUTH.group = now;" in auth and "paintMotifsNow();" in auth
    assert auth.index("box.append(grid);") < auth.index("paintMotifsNow();")
    # and a re-sort moves cards between grids, so it repaints what it moved
    assert "apply();\n      paintMotifsNow();" in js
    # a view built while it was display:none has no box either
    assert "  scheduleMotifs();          // a view built while hidden had no box to measure" in js

    # The first observer delivery can arrive before a freshly built card has
    # been inserted, so a not-yet-connected motif must NOT be dropped — only
    # one that has already been painted, i.e. a card that has been replaced.
    ro = js[js.index("const MOT_RO ="):js.index("function watchMotif(svg)")]
    assert "if (e.target.isConnected) { paintMotif(e.target); return; }" in ro
    assert "if (e.target.dataset.w) MOT_RO.unobserve(e.target);" in ro
    # a resize still repaints even where the observer is doing the watching
    assert 'addEventListener("resize", scheduleMotifs);' in js


def test_current_is_the_only_slanted_badge(fake):
    """Current is a tag pinned to the card, not a word in the row — and it is
    the only slanted one, so it differs from the rest in shape as well as
    colour. Its label is counter-skewed so it reads upright."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]
    cur = css.split("  .ac-st.cur {")[1].split("}")[0]
    assert "transform: skewX(-8deg)" in cur
    # the label is counter-skewed by the SAME angle, or it reads italicised
    assert ".ac-st.cur > * { transform: skewX(8deg); }" in css
    # ...which only works on an element, never a bare text node
    assert 'el("span","ac-stl", st8.badge)' in js
    # every other state stays square
    for other in ("rdy", "idle", "off"):
        assert "skew" not in css.split("  .ac-st.%s {" % other)[1].split("}")[0], other


def test_a_providers_name_survives_a_narrow_card(fake):
    """"Qwen (DashScope)" printed as one string truncates from the right and
    takes the NAME with it — "Qwen (DashSco…". The stem and the vendor are
    separate spans so only the vendor degrades."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]
    card = js[js.index("function authCard(e, panel) {"):js.index("function authMethodForm(")]
    assert 'el("span","ac-nm", par[1])' in card and 'el("span","ac-nv", par[2])' in card
    # the stem never shrinks; the vendor is the part that gives way
    assert "flex: none" in css.split("  .ac-nm {")[1].split("}")[0]
    nv = css.split("  .ac-nv {")[1].split("}")[0]
    assert "text-overflow: ellipsis" in nv and "min-width: 0" in nv
    # only the half that can truncate earns a tooltip — a tooltip repeating
    # text that is fully visible is the same fact twice
    assert card.index("nm.title = e.label") < card.index("} else nm.textContent = e.label;")
    # "Self-hosted endpoint" says endpoint twice over: the card's body is a
    # URL field. The short name is the one that fits.
    entries = js[js.index("function authEntries()"):js.index("function provMeta(")]
    assert 'mm.label === "Self-hosted endpoint" ? "Self-hosted" : mm.label' in entries
    assert "full: mm.label" in entries, "the contract's own label still travels, for comparison"


def test_providers_are_grouped_and_the_tally_cannot_disagree_with_the_cards(fake):
    """Connected providers gather at the top regardless of family, and the
    header's number is the Connected group's card count — the same predicate
    produces both, so the two cannot drift apart."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    r = js[js.index("function renderAuthCards(box, r) {"):js.index("const VENDOR_TINT")]

    # ONE predicate. The old tally asked "has an active auth method", which
    # misses a provider that is current from the environment and carries no
    # method of its own — that is how the header could read 3 while 4 cards
    # showed a connected state.
    assert 'const isConnected = e => ["cur", "rdy"].includes(cardState(e).cls);' in r
    assert "entries.filter(e => e.active).length" not in r
    assert "const connected = conn.length;" in r
    assert "const conn = entries.filter(isConnected)" in r

    # three groups, in order, each provider in exactly one
    for i, label in enumerate(["Connected", "First-party", "Open-source & self-host"]):
        assert '"%s"' % label in r, label
    assert r.index('["Connected"') < r.index('["First-party"') < r.index('["Open-source & self-host"')
    assert 'e.kind === "family" && !isConnected(e)' in r
    assert 'e.kind === "method" && !isConnected(e)' in r

    # Current sorts to the front of the Connected group
    assert '(cardState(a).cls === "cur" ? 0 : 1) - (cardState(b).cls === "cur" ? 0 : 1)' in r

    # an empty group hides its label rather than showing a bare heading
    assert "if (!list.length) return;" in r

    # the progress bar is gone — the Connected group is that ratio at full
    # size, with names on it
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]
    assert ".setup-bar" not in css and 'el("div","setup-bar")' not in js
    # the move is a 140ms fade, and only for those who want motion
    assert '.ac-moved { animation: ac-land 140ms ease-out; }' in css
    assert "@keyframes ac-land { from { opacity: 0; } to { opacity: 1; } }" in css


def test_collapsed_cards_are_one_height_by_construction(fake):
    """The grid is a matrix: every collapsed card is the same height because
    the CSS fixes it, not because their contents happen to match. An opened
    card grows inside its own cell and never stretches a sibling.

    (The browser-side check that all 15 cards measure the same lives in the
    headless render gate; this pins the construction that guarantees it.)"""
    from mantis_agent.serve_ui import INDEX_HTML

    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]
    assert ".acard:not(.open) { height: 63px; }" in css
    assert ".acard .ac-h { height: 39px; }" in css
    assert "overflow: hidden" in css.split("  .acard {")[1].split("}")[0]
    # a sibling opening must never reflow the row
    assert "align-items: start" in css.split(".auth-grid {")[1].split("}")[0]


def test_every_mark_is_optically_normalised_to_one_square():
    """Vendors draw on their own grids — measured across this set a mark's ink
    covers 50%-100% of its declared viewBox — so each mark carries a `fit`
    viewBox centred on its measured ink that makes it fill the same share of
    the square. Every square is the same size and neutrally filled; the glyph
    carries the vendor's colour.

    (The browser-side check that all 15 rendered ink boxes measure the same
    lives in the render gate; this pins the data that guarantees it.)"""
    from mantis_agent.serve_logos import (
        INK_TARGET,
        MARK_INK,
        ORG_LOGOS,
        PROVIDER_LOGOS,
        fit_viewbox,
    )
    from mantis_agent.serve_ui import INDEX_HTML

    assert 0.5 < INK_TARGET < 0.8
    # the ink table really does describe a wide spread — that's the problem it solves
    wide, narrow = [], []
    for key, (x, y, w, h) in MARK_INK.items():
        assert w > 0 and h > 0, key
        wide.append(max(w, h) / 24.0)
        narrow.append(min(w, h) / 24.0)
    assert min(narrow) < 0.6 and max(wide) >= 1.0      # 50%-wide to edge-to-edge
    # a fit viewBox is square, centred on the ink, and sized by the target
    fit = fit_viewbox((0.0, 6.0, 12.0, 6.0), 0.5)
    fx, fy, fw, fh = (float(v) for v in fit.split())
    assert fw == fh == 24.0 and fx + fw / 2 == 6.0 and fy + fh / 2 == 9.0
    # every mark the page can draw carries one
    for name, marks in (("provider", PROVIDER_LOGOS), ("org", ORG_LOGOS)):
        for mark_id, entry in marks.items():
            if entry.get("svg"):
                assert entry.get("fit"), "%s mark %s was never measured" % (name, mark_id)
                assert len(entry["fit"].split()) == 4, mark_id
    # the page swaps the fit in and centres what's left
    js, css = INDEX_HTML.split("<script>")[1], INDEX_HTML.split("<style>")[1].split("</style>")[0]
    assert "function markSvg(m)" in js and "'viewBox=\"' + m.fit + '\"'" in js
    assert 'preserveAspectRatio="xMidYMid meet"' in js
    # one neutral square for all of them — no per-vendor tinted background
    fill = js.split("function fillMark(")[1].split("function bigMark(")[0]
    assert "color-mix" not in fill and "style.background" not in fill
    for box in ("\n  .mark2 {", "\n  .omark {", "\n  .dpc .bigmark {"):
        rule = css.split(box)[1].split("}")[0]
        assert "background: var(--fill)" in rule, box
    # the svg fills its square; the fit viewBox does the insetting, not the CSS
    assert ".ac-h .bigmark svg { width: 32px; height: 32px; display: block; }" in css
    # the container centres a letter stand-in the same way it centres a glyph:
    # an inline span would put the letter on the text baseline, high and left
    box = css.split("  .ac-h .bigmark {")[1].split("}")[0]
    assert "display: inline-flex" in box and "align-items: center" in box
    assert "justify-content: center" in box and "line-height: 1" in box
    # and the letter is sized to the same .62 ink target the fit viewBox gives
    ltr = css.split("  .ac-h .bigmark.letter {")[1].split("}")[0]
    assert "font-size: 27px" in ltr and "translateY(-0.9px)" in ltr
    assert ".mark2 svg { width: 22px; height: 22px; display: block; }" in css
    # the letter stand-in matches the UI's type scale, centred like the glyphs
    assert ".bigmark.letter { font-family: var(--sans); font-size: 15px; font-weight: 600" in css
    assert ".mark2.letter" in css and ".omark.letter" in css
    assert 'w.classList.add("letter")' in js


def test_no_fact_is_printed_twice_on_a_provider_card(fake):
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    # the description belongs to the card; the oauth flow never repeats it
    oauth = js[js.index("function oauthFlow("):js.index("function unlockFamily(")]
    assert "no API key, no per-token bill" not in oauth
    # the env var appears only in its field's help line
    assert 'el("div","ac-env"' not in js and 'class="ac-meta"' not in js.split("function authCard(")[1].split("function metaBit(")[0].split("if (!open) return card;")[0]
    assert 'help.append(el("span","envn"' in js
    # an open-source card IS its method: with no chooser to draw, its name is
    # never printed a second time — not as a label, not on the via line
    assert 'el("div","ac-one"' not in js and "e.methods.length > 1" in js
    assert 'only && only !== e.label && only !== e.full ? only : ""' in js
    # and a description that opens with the card's own name is trimmed to what
    # it adds — "Ollama (local)" is the heading, never also the first body line
    assert "never restate the card's own name in its body" in js
    assert 'const desc = (m.description || "").replace(new RegExp("^" + e.label' in js
    # a self-host backend is a template until the user fills it in
    assert "the URL you set above" in js and "/[{}]/.test(String(ep))" in js
    # marks render identically whatever grid the vendor drew on
    assert 'preserveAspectRatio="xMidYMid meet"' in js and "function markSvg(m)" in js


def test_every_empty_state_has_an_illustration(fake):
    """Each empty state is a drawing + one headline + one line, and the SVGs
    are self-contained — no external references anywhere."""
    import re

    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    art = re.search(r"const ART = \{(.*?)\n\};", js, re.S)
    assert art, "the ART set is gone"
    body = art.group(1)
    for name in ("deploy", "socket", "session", "mcp", "skill", "search", "activity"):
        assert name + ":" in body, name
    assert body.count("<svg viewBox=") == 7
    for bad in ("http://", "https://", "url(", "xlink", "<image", "<use"):
        assert bad not in body, bad
    assert "currentColor" in body and "var(--accent)" in body
    assert "function emptyState(icon, title, line, action)" in js
    for call in ('emptyState("deploy", "No deployments yet"', 'emptyState("session", "No sessions yet"',
                 'emptyState("mcp", "No MCP servers configured"', 'emptyState("skill", "No skills yet"',
                 'emptyState("search", "No model matches"', 'emptyState("activity"',
                 'emptyState("socket", "Add a GPU provider to deploy any model"'):
        assert call in js, call
    assert ".zero .zart svg" in INDEX_HTML and ".zero .zact" in INDEX_HTML
