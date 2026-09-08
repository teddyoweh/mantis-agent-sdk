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
    card = js[js.index("function authCard(e) {"):js.index("function metaBit(")]
    head, body = card.split("if (!open) return card;")
    # the collapsed half builds exactly these four things
    assert "bigMark(" in head and 'el("div","fn"' in head and 'el("span","ac-st " + st8.cls)' in head
    assert 'btn(open ? "Close" : st8.cls === "off" ? "Connect" : "Manage", "gho"' in head
    for later in ("ac-types", "authMethodForm", "ac-models", "ac-meta", "ac-d", "metaBit("):
        assert later not in head, "collapsed card renders " + later
        assert later in body, "opened card is missing " + later
    # one card open at a time, remembered on AUTH.open
    assert "AUTH.open = open ? null : e.key" in js and 'const open = AUTH.open === e.key' in js
    # the state is one badge with its own surface, in the header, always present
    assert 'el("span","ac-st " + st8.cls)' in js
    assert "height: 20px" in css.split(".ac-st {")[1].split("}")[0]


def test_provider_card_surface_is_neutral_and_the_active_one_is_dashed(fake):
    """No colour wash in either theme: the surface is the same panel in every
    state, the current provider is marked by a dashed accent outline, and the
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


def test_the_current_card_is_marked_by_a_dashed_outline_not_a_border(fake):
    """The active marker is a dashed accent outline around the WHOLE card,
    drawn as an SVG stroke so the dash array is ours to specify, inset from
    the edge so it reads as a marker rather than a border, and following the
    card's corner radius exactly."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]

    # an SVG stroke, not `outline: dashed` (no control over the array) and not
    # a border (it would change layout and break the equal-height matrix)
    assert "outline: 1px dashed" not in css and "outline: 1.5px dashed" not in css
    rect = css.split("  .ac-dash rect {")[1].split("}")[0]
    assert "stroke-width: 1.5" in rect
    assert "stroke-dasharray: 2.5 3.5" in rect
    assert "opacity: .75" in rect
    assert "stroke: var(--accent)" in rect
    assert "border" not in rect

    # the marker costs no layout, so collapsed cards stay one height
    box = css.split("  .ac-dash {")[1].split("}")[0]
    assert "position: absolute" in box and "pointer-events: none" in box
    # inset 4px + half the 1.5px stroke, so the stroke's OUTER edge lands 4px in
    assert "top: 4.75px" in box and "left: 4.75px" in box
    assert "calc(100% - 9.5px)" in box

    # the corner radius is the card's 12px less the 4.75px the path is inset by
    mk = js[js.index("function dashMarker()"):js.index("function authCard(")]
    assert '"rx", "7.25"' in mk and '"ry", "7.25"' in mk
    assert '"width", "100%"' in mk and '"height", "100%"' in mk
    # SVG will not parse calc() in a geometry attribute — the box is sized in CSS
    assert "calc(" not in "".join(ln for ln in mk.split("\n") if not ln.strip().startswith("//"))
    assert 'setAttribute("aria-hidden", "true")' in mk

    # Current wears the accent marker; Ready wears the same shape held far
    # back, in neutral ink so it can never be mistaken for Current; the two
    # quiet states wear none
    assert 'if (st8.cls === "cur" || st8.cls === "rdy") card.append(dashMarker());' in js
    rdy = css.split("  .acard.rdy .ac-dash rect {")[1].split("}")[0]
    assert "stroke: var(--ink-3)" in rdy and "opacity: .3" in rdy
    for quiet in (".acard.idle .ac-dash", ".acard.off .ac-dash"):
        assert quiet not in css, quiet

    # the slow creep is opt-out-able and loops without a seam: one dash cycle
    # is 2.5 + 3.5 = 6px, and the offset travels a whole number of them
    assert "@media (prefers-reduced-motion: no-preference) {" in css
    anim = css.split("@media (prefers-reduced-motion: no-preference) {")[1][:200]
    assert ".acard.cur .ac-dash rect { animation: dashmove 18s linear infinite; }" in anim
    off = css.split("@keyframes dashmove { to { stroke-dashoffset: -")[1].split(";")[0]
    assert int(off) % 6 == 0, "a partial dash cycle would jump at the loop point"


def test_a_providers_name_survives_a_narrow_card(fake):
    """"Qwen (DashScope)" printed as one string truncates from the right and
    takes the NAME with it — "Qwen (DashSco…". The stem and the vendor are
    separate spans so only the vendor degrades."""
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]
    card = js[js.index("function authCard(e) {"):js.index("function authMethodForm(")]
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
