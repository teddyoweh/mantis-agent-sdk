"""The Deploy page's usage panel: parsed from a container's own log, never
from its endpoint (a request would wake a scaled-to-zero GPU, or keep an idle
one billing)."""

from __future__ import annotations

from datetime import datetime, timezone

from mantis_agent.deploy import usage

NOW = datetime(2026, 9, 23, 20, 0, 0, tzinfo=timezone.utc).timestamp()

LOG = """\
[modal-client] 2026-09-23T17:02:00+0000 [Modal Flash] Server tunnel opened at https://a
WARNING 09-23 17:02:12 [config.py:70] Support for Transformers v4 is deprecated.
(APIServer pid=4) INFO 09-23 17:13:00 [api_server.py:617] Starting vLLM server on http://0.0.0.0:8000
(APIServer pid=4) INFO:     Application startup complete.
(APIServer pid=4) INFO 09-23 17:20:00 [loggers.py:271] Engine 000: Avg prompt throughput: 16.9 tokens/s, Avg generation throughput: 7.6 tokens/s, Running: 1 reqs, Waiting: 0 reqs, GPU KV cache usage: 0.4%, Prefix cache hit rate: 0.0%
(APIServer pid=4) INFO:     172.20.0.1:1 - "POST /v1/chat/completions HTTP/1.1" 200 OK
(APIServer pid=4) INFO:     172.20.0.1:2 - "POST /v1/chat/completions HTTP/1.1" 500 Internal Server Error
(APIServer pid=4) INFO:     172.20.0.1:3 - "GET /metrics HTTP/1.1" 200 OK
(APIServer pid=4) INFO 09-23 17:20:10 [loggers.py:271] Engine 000: Avg prompt throughput: 0.0 tokens/s, Avg generation throughput: 70.0 tokens/s, Running: 0 reqs, Waiting: 0 reqs, GPU KV cache usage: 0.0%, Prefix cache hit rate: 0.0%
(APIServer pid=4) INFO 09-23 18:06:57 [launcher.py:137] Shutting down FastAPI HTTP server.
[modal-client] 2026-09-23T19:28:10+0000 [Modal Flash] Server tunnel opened at https://b
(APIServer pid=4) INFO 09-23 19:41:00 [api_server.py:617] Starting vLLM server on http://0.0.0.0:8000
(APIServer pid=4) INFO:     Application startup complete.
""".splitlines()


def test_parse_reads_containers_stats_and_requests():
    p = usage.parse(LOG, NOW)
    assert len(p.sessions) == 2
    a, b = p.sessions
    assert a.ready - a.start == 11 * 60 and a.end is not None and a.requests == 2
    assert b.ready is not None and b.end is None
    assert [s[2] for s in p.stats] == [7.6, 70.0]
    # only inference routes count as requests; /metrics and /health don't
    assert [c for _, c in p.requests] == [200, 500]


def test_summarize_prices_gpu_time_and_keeps_a_live_container_open():
    p = usage.parse(LOG, NOW)
    out = usage.summarize(p, since=NOW - 6 * 3600, until=NOW, live=True, rate_per_hour=18.0)
    S = out["summary"]
    # 17:02→18:06:57 plus 19:28:10→now (still up)
    assert abs(S["gpu_s"] - ((64 * 60 + 57) + (31 * 60 + 50))) < 1
    assert S["requests"] == 2 and S["errors"] == 1 and S["wakes"] == 2
    assert S["tokens_out"] == int((7.6 + 70.0) * usage.STATS_INTERVAL_S)
    assert S["cost_usd"] == round(S["gpu_s"] / 3600 * 18.0, 2)
    assert out["sessions"][-1]["open"] is True and out["sessions"][-1]["end"] == NOW
    # not live: the unclosed session ends at its last log line, not at "now"
    cold = usage.summarize(p, since=NOW - 6 * 3600, until=NOW, live=False, rate_per_hour=18.0)
    assert cold["sessions"][-1]["open"] is False and cold["sessions"][-1]["end"] < NOW


def test_a_date_that_would_be_in_the_future_is_last_year():
    jan = datetime(2027, 1, 1, 0, 5, tzinfo=timezone.utc).timestamp()
    p = usage.parse(["(APIServer pid=4) INFO 12-31 23:59:00 [x.py:1] Application startup complete."], jan)
    assert datetime.fromtimestamp(p.last_t, timezone.utc).year == 2026


def test_the_usage_endpoint_never_touches_the_deployment(monkeypatch, tmp_path):
    """It reads the provider's log (control plane) and nothing else."""
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path))
    from mantis_agent import serve
    from mantis_agent.deploy import manager, store
    from mantis_agent.deploy.base import Deployment, GpuSpec

    dep = Deployment(id="mantis-x", provider="modal", model="org/x", engine="vllm", status="running",
                     gpu=GpuSpec("H200:4", "H200", 141, count=4, price_per_hour=18.0),
                     served_model_name="org/x", endpoint_url="https://x.modal.direct/v1")
    store.upsert(dep)

    async def logs(dep_id, tail=200):
        for line in LOG:
            yield line
    monkeypatch.setattr(manager, "logs", logs)
    serve._USAGE_CACHE.clear()
    import httpx

    def boom(*a, **k):
        raise AssertionError("the usage panel must never call the endpoint")
    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    monkeypatch.setattr(httpx.Client, "get", boom)
    r = serve.deploy_usage("mantis-x", 24)
    assert r["ok"] and r["summary"]["requests"] == 2 and r["status"] == "running"


def test_the_deploy_page_draws_usage_from_the_log_endpoint():
    from mantis_agent.serve_ui import INDEX_HTML

    js = INDEX_HTML.split("<script>")[1]
    css = INDEX_HTML.split("<style>")[1].split("</style>")[0]
    assert 'const uSec = section(pad, "Usage"); uSec.id = "dp-usage";' in js
    assert 'api("/api/deploy/usage?" + q({ id: USAGE.id, hours: USAGE.hours }))' in js
    for fn in ("function usageTimeline(", "function usageLines(", "function usageBars(", "function usageAxis("):
        assert fn in js, fn
    # two series, validated (validate_palette.js) in both themes, never the accent reused
    assert "--s1: #2f855a; --s2: #3b6fd4;" in css and css.count("--s1: #48a870; --s2: #648ce6;") == 2
    # every chart has a hover layer, and the two-series chart a legend
    assert js.count("= usageTip(box)") == 3
    assert "Generation</span><span><i class=\"ln p\"></i>Prompt" in js
