"""Hub / Ollama discovery client against a mocked wire (respx)."""

from __future__ import annotations

import anyio
import httpx
import pytest
import respx

from mantis_agent.deploy import hf_hub
from mantis_agent.deploy.base import DeployError


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(_s: float) -> None:
        return None

    monkeypatch.setattr("mantis_agent.deploy._http.sleep", _fast)
    monkeypatch.delenv("HF_TOKEN", raising=False)


def test_search_sends_the_verified_query_shape():
    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://huggingface.co/api/models").mock(
            return_value=httpx.Response(200, json=[{"id": "Qwen/Qwen3-8B", "downloads": 5}, "junk"]))
        rows = anyio.run(lambda: hf_hub.search("qwen", limit=5, sort="downloads"))
    assert rows == [{"id": "Qwen/Qwen3-8B", "downloads": 5}]
    q = route.calls.last.request.url.params
    assert q["pipeline_tag"] == "text-generation" and q["search"] == "qwen"
    assert q["sort"] == "downloads" and q["direction"] == "-1" and q["limit"] == "5"
    assert "safetensors" in q.get_list("expand[]") and q["filter"] == "safetensors"
    assert "authorization" not in route.calls.last.request.headers


def test_token_is_sent_when_present(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_abc")
    with respx.mock() as router:
        route = router.get("https://huggingface.co/api/models/Qwen/Qwen3-8B").mock(
            return_value=httpx.Response(200, json={"id": "Qwen/Qwen3-8B"}))
        meta = anyio.run(lambda: hf_hub.model_info("Qwen/Qwen3-8B"))
    assert meta["id"] == "Qwen/Qwen3-8B"
    assert route.calls.last.request.headers["authorization"] == "Bearer hf_abc"


def test_model_info_404_and_bad_id():
    with respx.mock() as router:
        router.get("https://huggingface.co/api/models/nope/missing").mock(return_value=httpx.Response(404, json={"error": "x"}))
        with pytest.raises(DeployError, match="not found"):
            anyio.run(lambda: hf_hub.model_info("nope/missing"))
    with pytest.raises(DeployError, match="not a Hugging Face model id"):
        anyio.run(lambda: hf_hub.model_info("qwen3"))


def test_fetch_config_degrades_on_401():
    with respx.mock() as router:
        router.get("https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct/resolve/main/config.json").mock(
            return_value=httpx.Response(401, json={"error": "gated"}))
        assert anyio.run(lambda: hf_hub.fetch_config("meta-llama/Llama-3.1-8B-Instruct")) is None
        router.get("https://huggingface.co/Qwen/Qwen3-8B/resolve/main/config.json").mock(
            return_value=httpx.Response(200, json={"num_hidden_layers": 36}))
        assert anyio.run(lambda: hf_hub.fetch_config("Qwen/Qwen3-8B")) == {"num_hidden_layers": 36}


def test_rate_limit_is_retried_with_retry_after():
    with respx.mock() as router:
        router.get("https://huggingface.co/api/models").mock(side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}, json={"error": "slow down"}),
            httpx.Response(200, json=[{"id": "a/b"}]),
        ])
        rows = anyio.run(lambda: hf_hub.search("x"))
    assert rows == [{"id": "a/b"}]


def test_ollama_tags_and_manifest():
    with respx.mock() as router:
        tags = router.get("https://ollama.com/library/qwen3/tags").mock(
            return_value=httpx.Response(200, json={"tags": ["latest", "8b", "8b-q4_K_M"]}))
        router.get("https://ollama.com/v2/library/qwen3/manifests/8b").mock(return_value=httpx.Response(200, json={
            "config": {"digest": "sha256:cfg"},
            "layers": [{"size": 5_000_000_000}, {"size": 200_000_000}],
        }))
        router.get("https://ollama.com/v2/library/qwen3/blobs/sha256:cfg").mock(
            return_value=httpx.Response(200, json={"model_family": "qwen3", "model_type": "8.2B", "file_type": "Q4_K_M"}))
        router.get("https://ollama.com/library/nothere/tags").mock(return_value=httpx.Response(404))
        assert anyio.run(lambda: hf_hub.ollama_tags("qwen3:8b")) == ["latest", "8b", "8b-q4_K_M"]
        assert tags.calls.last.request.headers["accept"] == "application/json"
        m = anyio.run(lambda: hf_hub.ollama_manifest("qwen3:8b"))
        assert m["size_bytes"] == 5_200_000_000 and m["config"]["file_type"] == "Q4_K_M"
        with pytest.raises(DeployError, match="no model"):
            anyio.run(lambda: hf_hub.ollama_tags("nothere"))
