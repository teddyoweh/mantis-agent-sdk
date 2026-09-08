"""Tests for the ``mantis serve`` overview / sessions / models upgrades.

Covers the new read-only endpoints (providers grid, spend, activity, workflow
detail, ollama), the aggregation math behind spend and the per-turn session
ledger, transcript redaction, and that the served page carries the new
sections. Everything runs against a temp ``$MANTIS_AGENT_HOME`` — no network
(Ollama is pointed at a closed port).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A temp home with one project, one session with two assistant turns,
    a background job record and a persisted workflow run."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(h))
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9")          # nothing listening
    monkeypatch.delenv("MANTIS_JOBS_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    # The developer's own shell may carry real provider keys; the grid must
    # start from "none" for every family so auth-state assertions are ours.
    from mantis_agent import catalog as _cat

    for p in _cat.CATALOG:
        for var in (p.api_key_env, *p.key_env_aliases):
            monkeypatch.delenv(var, raising=False)
    from mantis_agent import serve, session_tree

    serve._ollama_cache.clear()
    serve._spend_cache.clear()
    serve._analytics_cache.clear()
    serve._projects_cache.clear()

    proj = tmp_path / "proj"
    proj.mkdir()
    sid = session_tree.new_session_id()
    tx = session_tree.SessionTranscript(sid, cwd=str(proj))
    tx.append_message("user", "please read the config, my key is sk-live-abcdefghijklmnopqrstuv1234")
    tx.record_last_prompt("please read the config")
    tx.append_message("assistant", [
        {"type": "text", "text": "Reading it now."},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": ".env"}},
    ])
    tx.append_message("user", [
        {"type": "tool_result", "tool_use_id": "tu_1",
         "content": "OPENAI_API_KEY=sk-proj-ZZZZZZZZZZZZZZZZZZZZZZZZZZZZ\nDEBUG=1\n" + ("x" * 400),
         "is_error": False},
    ])
    tx.append_message("assistant", [{"type": "text", "text": "Done. " + ("word " * 100)}])
    tx.set_title("Config read")

    # a finished background job + a running one
    from mantis_agent import job_records

    done = job_records.JobRecord(session_id=sid, seq=1, kind="task", status="done", desc="lint the repo",
                                 started_at=time.time() - 30, ended_at=time.time() - 5,
                                 tool_count=4, turn_count=2, cwd=str(proj))
    done.job_id = job_records.make_job_id(sid, 1)
    job_records.save_job_record(done)
    running = job_records.JobRecord(session_id=sid, seq=2, kind="agent", status="running",
                                    desc="token=sk-live-0000000000000000000000 in desc",
                                    started_at=time.time() - 3, workflow_id="run-1", cwd=str(proj))
    running.job_id = job_records.make_job_id(sid, 2)
    job_records.save_job_record(running)

    # a workflow run with recorded usage on two providers
    from mantis_agent import workflow_store

    now = time.time()
    run = {
        "id": "run-1", "name": "review pipeline", "status": "done", "started": now - 120, "ended": now - 10,
        "definition": "review", "log_lines": ["started", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"],
        "phases": [{"title": "scan", "status": "done", "agents": [
            {"id": "a1", "label": "scanner", "model": "claude-sonnet-5", "status": "done", "started": now - 120,
             "usage": {"inputTokens": 12000, "outputTokens": 800, "costUSD": 0.05}, "cost_usd": 0.05,
             "summary": "found 3 issues", "turns": 3, "tool_count": 5},
            {"id": "a2", "label": "fixer", "model": "gpt-5.4", "status": "error", "started": now - 100,
             "usage": {"inputTokens": 5000, "outputTokens": 200, "costUSD": 0.02},
             "error": "rate limited"},
        ]}],
    }
    workflow_store.save_run(run, definition="review", inputs={"repo": "x", "api_token": "hush"})
    return {"home": h, "cwd": str(proj), "sid": sid}


# ---------------------------------------------------------------------------
# families / providers grid
# ---------------------------------------------------------------------------


def test_provider_grid_has_five_families_in_order(home):
    from mantis_agent import serve

    g = serve.provider_grid()
    ids = [f["id"] for f in g["families"]][:5]
    assert ids == ["openai", "anthropic", "google", "xai", "oss"]
    for f in g["families"]:
        assert {"label", "logo", "providers", "ready", "last_model", "is_current"} <= set(f)
        for p in f["providers"]:
            assert p["auth"] in ("saved", "env", "oauth", "none")
    oss = next(f for f in g["families"] if f["id"] == "oss")
    assert oss["local"]["reachable"] is False       # port 9 — degrades, doesn't raise
    assert len(oss["providers"]) >= 3               # groq / together / fireworks / …
    assert 0 <= g["ready_count"] <= len(g["families"])


def test_family_of_classifies_every_catalog_provider(home):
    from mantis_agent import catalog, serve

    seen = {serve.family_of(p.id) for p in catalog.CATALOG}
    assert {"openai", "anthropic", "google", "oss"} <= seen
    # xAI lands in its own family once (and only once) the catalog carries it
    assert serve.family_of("xai") == "xai"
    if "xai" in catalog.BY_ID:
        assert serve._provider_id_for_model("grok-4") == "xai"
    else:
        assert serve._provider_id_for_model("grok-4") is None
    assert serve._provider_id_for_model("claude-sonnet-5") == "anthropic"
    assert serve._provider_id_for_model("gpt-5.4") == "openai"
    assert serve._provider_id_for_model("gemini-2.5-pro") == "gemini"


def test_auth_state_distinguishes_env_saved_oauth_none(home, monkeypatch):
    from mantis_agent import catalog, serve

    anth = catalog.BY_ID["anthropic"]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert serve._auth_state(anth)["auth"] == "none"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-" + "a" * 40)
    st = serve._auth_state(anth)
    assert st["auth"] == "oauth" and st["key_masked"] and "a" * 40 not in st["key_masked"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "b" * 40)
    assert serve._auth_state(anth)["auth"] == "env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    catalog.set_key("anthropic", "sk-ant-saved-" + "c" * 30)
    # set_key also exports the key to this process's env (so the TUI sees it);
    # drop that to observe the on-disk store on its own.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert serve._auth_state(anth)["auth"] == "saved"
    # and the models page carries the same word, with the OAuth path enabled
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok" + "d" * 40)
    catalog.clear_key("anthropic")
    m = serve.models_state()
    a = next(p for p in m["providers"] if p["id"] == "anthropic")
    assert a["auth"] == "oauth" and a["enabled"] is True and a["family"] == "anthropic"


def test_xai_logo_ships_and_every_family_logo_resolves(home):
    from mantis_agent import serve
    from mantis_agent.serve_logos import PROVIDER_LOGOS

    assert PROVIDER_LOGOS["xai"]["svg"].startswith("<svg")
    for _fid, _label, logo in serve.FAMILIES:
        assert logo in PROVIDER_LOGOS, logo


# ---------------------------------------------------------------------------
# spend & usage
# ---------------------------------------------------------------------------


def test_est_tokens_and_content_chars():
    from mantis_agent import serve

    assert serve._est_tokens(0) == 0 and serve._est_tokens(400) == 100
    assert serve._content_chars("abcd") == 4
    assert serve._content_chars([{"type": "text", "text": "abcdefgh"}]) == 8
    assert serve._content_chars([{"type": "tool_result", "tool_use_id": "t", "content": "12345678"}]) == 8
    n = serve._content_chars([{"type": "tool_use", "id": "t", "name": "grep", "input": {"q": "x"}}])
    assert n > 4


def test_analytics_estimates_tokens_per_day_and_project(home):
    from mantis_agent import serve

    a = serve.analytics()
    t = a["totals"]
    assert t["in_est"] > 0 and t["out_est"] > 0
    # the second assistant turn re-reads everything before it, so input > output
    assert t["in_est"] > t["out_est"]
    day = next(iter(a["daily"].values()))
    assert day["in_est"] + day["out_est"] == t["in_est"] + t["out_est"]
    proj = a["top_projects"][0]
    assert proj["in_est"] == t["in_est"] and proj["out_est"] == t["out_est"]


def test_spend_windows_and_provider_breakdown(home):
    from mantis_agent import catalog, serve

    catalog.set_last_model("gpt-5.4", "https://api.openai.com/v1")
    serve._spend_cache.clear()
    sp = serve.spend()
    assert len(sp["days"]) == 30 and sp["days"][-1]["date"] >= sp["days"][0]["date"]
    w7, w30 = sp["totals"]["7"], sp["totals"]["30"]
    assert w7["est_tokens"] > 0 and w30["est_tokens"] >= w7["est_tokens"]
    # recorded workflow usage lands on today's row and in the totals
    assert w7["rec_in"] == 17000 and w7["rec_out"] == 1000
    assert abs(w7["rec_usd"] - 0.07) < 1e-9
    # per-provider: the two recorded models + the estimated session bucket
    # (the current provider can appear twice — once recorded, once estimated —
    # and the two are never merged)
    by = {(p["id"], p["source"]): p for p in sp["by_provider"]}
    rec_a, rec_o = by[("anthropic", "recorded")], by[("openai", "recorded")]
    assert rec_a["in"] == 12000 and rec_a["family"] == "anthropic" and rec_a["runs"] == 1
    assert rec_o["usd"] == pytest.approx(0.02) and rec_o["in"] == 5000
    est = [p for p in sp["by_provider"] if p["source"] == "estimated"]
    assert len(est) == 1 and est[0]["in"] == w30["est_in"] and est[0]["id"] == "openai"
    # gpt-5.4 has a price-table row → the session estimate is priced at it
    pr = sp["pricing"]
    assert pr["known"] and pr["provider"] == "openai"
    assert w7["est_usd"] == pytest.approx(w7["est_in"] / 1e6 * pr["prompt_per_million"] +
                                          w7["est_out"] / 1e6 * pr["completion_per_million"])
    fam_ids = [f["id"] for f in sp["by_family"]]
    assert fam_ids == sorted(fam_ids, key=lambda f: [x[0] for x in serve.FAMILIES].index(f))
    assert {"openai", "anthropic"} <= set(fam_ids)


def test_spend_leaves_dollars_blank_for_an_unpriced_model(home):
    """No row in the price table → tokens are shown, USD is None. Never a guess."""
    from mantis_agent import catalog, serve

    catalog.set_last_model("totally-unknown-model-xyz", "https://api.openai.com/v1")
    serve._spend_cache.clear()
    sp = serve.spend()
    assert sp["pricing"]["known"] is False and sp["pricing"]["provider"] == "openai"
    assert sp["totals"]["7"]["est_usd"] is None and sp["totals"]["7"]["est_tokens"] > 0
    assert sp["days"][-1]["est_usd"] is None
    est = next(p for p in sp["by_provider"] if p["source"] == "estimated")
    assert est["usd"] is None
    fam = next(f for f in sp["by_family"] if f["id"] == "openai")
    assert fam["priced"] is False


def test_spend_prices_sessions_when_the_model_has_a_row(home):
    from mantis_agent import catalog, serve

    # not in groq's flagship list — attributed to groq via the backend URL
    catalog.set_last_model("llama-3.3-70b-instruct", "https://api.groq.com/openai/v1")
    serve._spend_cache.clear()
    sp = serve.spend()
    pr = sp["pricing"]
    assert pr["known"] and pr["provider"] == "groq"
    assert pr["prompt_per_million"] == 0.59 and pr["completion_per_million"] == 0.79
    w = sp["totals"]["30"]
    expect = w["est_in"] / 1e6 * pr["prompt_per_million"] + w["est_out"] / 1e6 * pr["completion_per_million"]
    assert w["est_usd"] == pytest.approx(expect)
    assert sp["days"][-1]["est_usd"] == pytest.approx(expect)


def test_spend_is_free_for_local_backends(home):
    from mantis_agent import catalog, serve

    catalog.set_last_model("qwen3:8b", "http://localhost:11434")
    serve._spend_cache.clear()
    pr = serve.spend()["pricing"]
    assert pr["known"] and pr["prompt_per_million"] == 0 and pr["provider"] == "ollama"


# ---------------------------------------------------------------------------
# session timeline
# ---------------------------------------------------------------------------


def test_session_detail_turn_ledger_and_timestamps(home):
    from mantis_agent import catalog, serve

    catalog.set_last_model("llama-3.3-70b-instruct", "https://api.groq.com/openai/v1")
    d = serve.session_detail(home["cwd"], home["sid"])
    msgs, turns, st = d["messages"], d["turns"], d["stats"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert all(isinstance(m.get("ts"), float) for m in msgs)
    assert len(turns) == 2 and st["turns"] == 2
    # each assistant turn's context is everything before it; it only grows
    assert turns[0]["in_est"] < turns[1]["in_est"]
    assert turns[1]["ctx_est"] == st["peak_ctx_est"]
    assert st["in_est"] == turns[0]["in_est"] + turns[1]["in_est"]
    assert st["tokens_est"] == st["in_est"] + st["out_est"]
    assert st["usd_est"] is not None and turns[1]["cum_usd_est"] == pytest.approx(st["usd_est"])
    assert turns[0]["tools"] == ["read_file"]
    assert msgs[1]["turn"] == 0 and msgs[3]["turn"] == 1 and turns[1]["i"] == 3
    assert st["pricing"]["known"] and "estimated" in st["note"]


def test_session_detail_redacts_secrets_in_text_and_tool_results(home):
    from mantis_agent import serve

    d = serve.session_detail(home["cwd"], home["sid"])
    blob = json.dumps(d)
    assert "sk-live-abcdefghijklmnopqrstuv1234" not in blob
    assert "sk-proj-ZZZZZZZZZZZZZZZZZZZZZZZZZZZZ" not in blob
    assert "OPENAI_API_KEY=" in blob and "DEBUG=1" in blob       # names and harmless values survive
    assert d["messages"][2]["content"][0]["tool_use_id"] == "tu_1"   # ids are not secrets


def test_redact_text_shapes():
    from mantis_agent import serve

    r = serve._redact_text
    assert "ghp_" + "a" * 36 not in r("token ghp_" + "a" * 36 + " here")
    assert r("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345").endswith("2345")
    assert "hunter2hunter2" not in r("password=hunter2hunter2")
    assert "supersecretvalue" not in r('"api_key": "supersecretvalue"')
    assert r("https://m.co/mcp?apiKey=tok-secret-value") != "https://m.co/mcp?apiKey=tok-secret-value"
    assert r("plain prose with no secrets in it") == "plain prose with no secrets in it"
    assert r("short") == "short"
    assert serve._redact_content({"api_key": "abcdefghij", "path": "/x"})["path"] == "/x"
    assert serve._redact_content({"api_key": "abcdefghij"})["api_key"] != "abcdefghij"


# ---------------------------------------------------------------------------
# live activity
# ---------------------------------------------------------------------------


def test_activity_lists_jobs_and_runs_with_usage(home):
    from mantis_agent import serve

    act = serve.activity()
    kinds = {j["kind"]: j for j in act["jobs"]}
    assert set(kinds) == {"task", "agent"}
    assert kinds["task"]["terminal"] is True and kinds["task"]["elapsed_s"] == pytest.approx(25, abs=2)
    assert kinds["agent"]["terminal"] is False and kinds["agent"]["workflow_id"] == "run-1"
    assert "sk-live-0000000000000000000000" not in json.dumps(act)   # desc redacted
    assert "spawn_spec" not in kinds["task"]
    assert act["active_jobs"] == 1 and act["active_runs"] == 0
    run = act["runs"][0]
    assert run["run_id"] == "run-1" and run["usage"]["tokens"] == 18000
    assert run["usage"]["usd"] == pytest.approx(0.07)
    assert run["usage"]["agents"] == 2 and run["usage"]["agents_done"] == 1
    assert run["usage"]["elapsed_s"] == pytest.approx(110, abs=1)
    assert run["usage"]["models"][0] == "claude-sonnet-5"
    assert act["jobs_dir"].endswith("jobs") and "path" not in run


def test_workflow_detail_is_redacted_and_bounded(home):
    from mantis_agent import serve

    r = serve.workflow_detail("run-1")
    assert r["ok"] and r["status"] == "done" and r["usage"]["tokens"] == 18000
    assert r["inputs"]["repo"] == "x" and r["inputs"]["api_token"] == "[redacted]"
    blob = json.dumps(r)
    assert "abcdefghijklmnopqrstuvwxyz0123" not in blob          # bearer in log line masked
    agents = r["run"]["phases"][0]["agents"]
    assert agents[0]["summary"] == "found 3 issues" and agents[1]["error"] == "rate limited"
    assert serve.workflow_detail("nope")["ok"] is False
    assert serve.workflow_detail(None)["ok"] is False


def test_activity_survives_an_empty_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "empty"))
    from mantis_agent import serve

    act = serve.activity()
    assert act["jobs"] == [] and act["runs"] == [] and act["active_jobs"] == 0


# ---------------------------------------------------------------------------
# models page data
# ---------------------------------------------------------------------------


def test_ollama_state_degrades_when_daemon_is_down(home):
    from mantis_agent import serve

    serve._ollama_cache.clear()
    o = serve.ollama_state()
    assert o["reachable"] is False and o["models"] == [] and o["loaded_count"] == 0
    assert o["base_url"].endswith(":9") and o["error"]
    assert serve.ollama_state() is o          # cached for the TTL


def test_ollama_probe_parses_tags_and_ps(home, monkeypatch):
    """A fake daemon on an ephemeral port: /api/tags + /api/ps → size, params,
    quantisation and the loaded flag, loaded models first."""
    from http.server import BaseHTTPRequestHandler

    from mantis_agent import serve

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/tags":
                body = {"models": [
                    {"name": "qwen3:8b", "size": 5_000_000_000, "modified_at": "2026-08-01T00:00:00Z",
                     "details": {"parameter_size": "8.2B", "quantization_level": "Q4_K_M", "family": "qwen3"}},
                    {"name": "gpt-oss:20b", "size": 13_000_000_000, "details": {"parameter_size": "20B"}},
                ]}
            elif self.path == "/api/ps":
                body = {"models": [{"name": "gpt-oss:20b", "size_vram": 12_000_000_000, "expires_at": "x"}]}
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{httpd.server_address[1]}")
        serve._ollama_cache.clear()
        o = serve.ollama_state()
        assert o["reachable"] and o["loaded_count"] == 1
        assert [m["name"] for m in o["models"]] == ["gpt-oss:20b", "qwen3:8b"]   # loaded first
        q = o["models"][1]
        assert q["param"] == "8.2B" and q["quant"] == "Q4_K_M" and q["loaded"] is False
        assert o["models"][0]["vram"] == 12_000_000_000
        m = serve.models_state()
        assert m["ollama"]["reachable"] and "qwen3:8b" in m["model_info"]
        assert m["model_info"]["qwen3:8b"]["price"]["free"] is True
        g = serve.provider_grid()
        oss = next(f for f in g["families"] if f["id"] == "oss")
        assert oss["ready"] and oss["local"]["model_count"] == 2
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_models_state_carries_families_prices_and_learned_ctx(home, monkeypatch):
    from mantis_agent import context_limits, serve

    m = serve.models_state()
    assert [f["id"] for f in m["families"]] == ["openai", "anthropic", "google", "xai", "oss"]
    assert all(p["family"] in {"openai", "anthropic", "google", "xai", "oss"} for p in m["providers"])
    info = m["model_info"]
    from mantis_agent.budget import lookup_pricing

    # every price on the page is exactly budget.py's answer for (provider, model)
    # — present when the table has a row, absent (a dash) when it doesn't. The
    # same open-weight id lives on several hosts, so the map is per provider.
    for p in m["providers"]:
        for x in p["models"]:
            row = lookup_pricing(x, p["id"])
            if row is None:
                assert x not in p["prices"], (p["id"], x)
            else:
                assert p["prices"][x]["in"] == row.prompt_per_million, (p["id"], x)
                assert p["prices"][x]["out"] == row.completion_per_million, (p["id"], x)
            assert "ctx" in info[x], (p["id"], x)
    priced = [x for p in m["providers"] for x in p["prices"]]
    assert priced, "expected at least one priced hosted model"
    if "xai" in {p["id"] for p in m["providers"]}:
        xai = next(p for p in m["providers"] if p["id"] == "xai")
        assert xai["family"] == "xai" and xai["prices"]
    openai = next(p for p in m["providers"] if p["id"] == "openai")
    # a learned ceiling overrides the declared window and is flagged
    context_limits._reset_cache_for_tests()
    model = openai["models"][0]
    context_limits.record_limit(model, 8192, "https://api.openai.com/v1")
    serve._projects_cache.clear()
    m2 = serve.models_state()
    assert m2["model_info"][model]["ctx"] == 8192 and m2["model_info"][model]["ctx_learned"] == 8192
    context_limits.forget(model, "https://api.openai.com/v1")


def test_model_price_helper():
    from mantis_agent import serve

    from mantis_agent.budget import lookup_pricing

    assert serve._model_price("totally-unknown-model-xyz", "openai") is None
    p = serve._model_price("llama-3.3-70b-instruct", "groq")
    assert p and p["in"] == 0.59 and p["out"] == 0.79 and p["free"] is False
    assert serve._model_price("anything", "ollama")["free"] is True
    row = lookup_pricing("grok-4", "xai")
    if row is not None:
        g = serve._model_price("grok-4", "xai")
        assert g["in"] == row.prompt_per_million and g["cache_read"] == row.cache_read_per_million


# ---------------------------------------------------------------------------
# overview + HTTP
# ---------------------------------------------------------------------------


def test_overview_carries_family_readiness_activity_and_spend(home):
    from mantis_agent import serve

    o = serve.overview()
    assert set(o["families_ready"]) >= {"openai", "anthropic", "google", "oss"}
    assert o["active_jobs"] == 1 and o["active_runs"] == 0
    assert o["spend_7d"]["rec_in"] == 17000 and o["spend_7d"]["est_tokens"] > 0


def _boot(token=None, enforce_get=False):
    from mantis_agent import serve

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    httpd.token = token
    httpd.enforce_get = enforce_get
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_new_endpoints_over_http_and_page_sections(home):
    httpd, base = _boot()
    try:
        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.read()

        code, body = get("/api/providers")
        assert code == 200 and len(json.loads(body)["families"]) >= 5
        code, body = get("/api/spend")
        assert json.loads(body)["totals"]["7"]["rec_in"] == 17000
        code, body = get("/api/activity?limit=5")
        assert len(json.loads(body)["jobs"]) == 2
        code, body = get("/api/activity?limit=notanumber")
        assert code == 200
        code, body = get("/api/workflow?id=run-1")
        assert json.loads(body)["ok"] is True
        code, body = get("/api/workflow?id=missing")
        assert json.loads(body)["ok"] is False
        code, body = get("/api/ollama")
        assert json.loads(body)["reachable"] is False
        code, body = get("/api/session?cwd=" + urllib.request.quote(home["cwd"]) + "&id=" + home["sid"])
        d = json.loads(body)
        assert d["turns"] and d["stats"]["turns"] == 2

        code, html = get("/")
        page = html.decode()
        assert code == 200
        for marker in ("renderFamilies", "renderSpend", "renderActivityPage", "ctxChart", "openRun",
                       "fam-grid", "spend-card", "Local models", "sessfind",
                       "CHORDS", "visibilitychange", "prefers-color-scheme: dark", "max-width: 900px",
                       'data-v="home" class="on">Overview', "mm-grid", "myModelCard"):
            assert marker in page, marker
        assert '"xai"' in page                        # the Grok mark is inlined with the rest
        assert "http://" not in page.split("<script>")[0].replace("http://www.w3.org", "")  # no external assets in the shell
        assert "cdn." not in page and "googleapis" not in page
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_new_endpoints_respect_the_lan_token(home):
    httpd, base = _boot(token="tok123", enforce_get=True)
    try:
        for path in ("/api/providers", "/api/spend", "/api/activity", "/api/workflow?id=run-1", "/api/ollama"):
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(base + path, timeout=5)
            assert ei.value.code == 401, path
            sep = "&" if "?" in path else "?"
            with urllib.request.urlopen(base + path + sep + "k=tok123", timeout=5) as r:
                assert r.status == 200, path
            req = urllib.request.Request(base + path, headers={"X-Mantis-Token": "tok123"})
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 200, path
    finally:
        httpd.shutdown()
        httpd.server_close()
