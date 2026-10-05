"""Model discovery: the Hugging Face Hub API and the Ollama library.

Everything here is read-only metadata. The Hub's ``/api/models`` answers
without a token for public *and* gated repos (architectures, parameter
counts per dtype, license, ``gated``), so the dashboard can size a gated
model before the user pastes ``HF_TOKEN``; only ``resolve/main/config.json``
— which we fetch for the KV-cache maths — needs the token on gated repos,
and we degrade to a headroom rule when it is refused.

Verified shapes (September 2026):

* ``GET /api/models?search=&pipeline_tag=text-generation|image-text-to-text&sort=trendingScore
  &direction=-1&limit=N&expand[]=...`` — ``expand[]`` selects fields:
  ``safetensors.parameters`` (dtype → count), ``gated`` (``false | "auto" |
  "manual"``), ``config`` (``architectures``, ``model_type``), ``cardData``
  (``license``), ``downloads``, ``likes``, ``tags``.
* ``GET /api/models/{org}/{name}`` — the same fields, plus ``siblings``.
* ``GET https://ollama.com/library/{model}/tags`` with
  ``Accept: application/json`` → ``{"tags": [...]}``; the registry manifest
  ``/v2/library/{model}/manifests/{tag}`` gives layer sizes.
"""

from __future__ import annotations

import os
from typing import Any

from ._http import DeployHttp, raise_for
from .base import DeployError

__all__ = [
    "HF_API",
    "OLLAMA_SITE",
    "SORT_KEYS",
    "fetch_config",
    "hf_headers",
    "model_info",
    "ollama_manifest",
    "ollama_tags",
    "search",
]

HF_API = "https://huggingface.co/api"
HF_SITE = "https://huggingface.co"
OLLAMA_SITE = "https://ollama.com"

#: What ``search_models(sort=...)`` accepts → the Hub's parameter value.
#: the Hub pipelines a chat model is filed under — multimodal chat models
#: (most of the 2026 frontier) are ``image-text-to-text``, not ``text-generation``
PIPELINES: tuple[str, ...] = ("text-generation", "image-text-to-text")

SORT_KEYS: dict[str, str] = {
    "trending": "trendingScore",
    "downloads": "downloads",
    "likes": "likes",
    "updated": "lastModified",
    "created": "createdAt",
}

_EXPAND = (
    "safetensors", "gated", "config", "cardData", "downloads", "likes",
    "tags", "library_name", "pipeline_tag", "trendingScore", "lastModified",
)


def hf_headers(token: str | None = None) -> dict[str, str]:
    tok = (token or os.environ.get("HF_TOKEN") or "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _client(token: str | None = None) -> DeployHttp:
    return DeployHttp(
        provider="hf-hub", base_url=HF_API, headers=hf_headers(token),
        auth_env="HF_TOKEN", console_url="https://huggingface.co/settings/tokens",
    )


async def search(
    query: str = "",
    *,
    limit: int = 25,
    sort: str = "trending",
    token: str | None = None,
    gated: bool | None = None,
) -> list[dict[str, Any]]:
    """Chat models matching ``query`` (safetensors only — that is what
    vLLM/SGLang load), richest fields expanded.

    Two pipelines, merged: ``text-generation`` AND ``image-text-to-text``.
    The 2026 frontier is multimodal — GLM-5.3-Flash, Kimi K3, Qwen3.8,
    MiniMax M3, DeepSeek V4.1-Flash, Gemma 4 all carry the vision tag — and
    a text-generation-only search simply never returned them."""

    import anyio  # noqa: PLC0415

    key = SORT_KEYS.get(sort, SORT_KEYS["trending"])
    n = max(1, min(int(limit), 200))

    def params(pipeline: str) -> list[tuple[str, str]]:
        p: list[tuple[str, str]] = [
            ("pipeline_tag", pipeline), ("filter", "safetensors"),
            ("sort", key), ("direction", "-1"), ("limit", str(n)),
        ]
        if query.strip():
            p.append(("search", query.strip()))
        if gated is not None:
            p.append(("gated", "true" if gated else "false"))
        p.extend(("expand[]", f) for f in _EXPAND)
        return p

    results: dict[str, list[dict[str, Any]]] = {}
    client = _client(token)

    async def one(pipeline: str) -> None:
        data = await client.json("GET", "/models", params=params(pipeline), what="search")
        results[pipeline] = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    async with anyio.create_task_group() as tg:
        for pipeline in PIPELINES:
            tg.start_soon(one, pipeline)
    seen: dict[str, dict[str, Any]] = {}
    for pipeline in PIPELINES:
        for d in results.get(pipeline, []):
            seen.setdefault(str(d.get("id") or d.get("modelId") or ""), d)
    merged = [d for k, d in seen.items() if k]
    # one ordering across both lists, the one the caller asked for
    merged.sort(key=lambda d: d.get(key) or (0 if key not in ("lastModified", "createdAt") else ""), reverse=True)
    return merged[:n]


async def model_info(model_id: str, *, token: str | None = None) -> dict[str, Any]:
    """``GET /api/models/{id}`` — 404 becomes a :class:`DeployError` with a hint."""

    mid = model_id.strip().strip("/")
    if "/" not in mid:
        raise DeployError(
            f"{mid!r} is not a Hugging Face model id",
            hint="use the full `org/name` form, e.g. Qwen/Qwen3-8B, or `ollama:<tag>` for an Ollama library model",
        )
    client = _client(token)
    resp = await client.request(
        "GET", f"/models/{mid}", params=[("expand[]", f) for f in _EXPAND],
        raise_on_error=False, what="model info",
    )
    if resp.status_code == 404:
        raise DeployError(
            f"model {mid!r} not found on the Hugging Face Hub",
            hint="check the spelling at https://huggingface.co/models — ids are case-sensitive",
        )
    if resp.status_code == 401 and not hf_headers(token):
        raise DeployError(
            f"model {mid!r} needs a Hugging Face token even to read its metadata",
            hint="set HF_TOKEN (https://huggingface.co/settings/tokens) and retry",
        )
    raise_for(resp, provider="hf-hub", auth_env="HF_TOKEN",
              console_url="https://huggingface.co/settings/tokens", what="model info")
    body = resp.json()
    if not isinstance(body, dict):
        raise DeployError(f"unexpected reply for {mid} from the Hub")
    return body


async def fetch_config(model_id: str, *, token: str | None = None, revision: str = "main") -> dict[str, Any] | None:
    """The full ``config.json`` (layers, KV heads, head_dim, context).
    ``None`` when the repo is gated and we hold no accepted token, or the
    file simply isn't there."""

    mid = model_id.strip().strip("/")
    client = _client(token)
    resp = await client.request(
        "GET", f"{HF_SITE}/{mid}/resolve/{revision}/config.json",
        raise_on_error=False, retry_statuses=(429, 502, 504), what="config.json",
    )
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Ollama library
# ---------------------------------------------------------------------------


def _ollama() -> DeployHttp:
    return DeployHttp(provider="ollama-library", base_url=OLLAMA_SITE)


async def ollama_tags(model: str) -> list[str]:
    """Tags of one library model, e.g. ``["latest", "8b", "8b-q4_K_M"]``."""

    name = model.split(":", 1)[0].strip()
    resp = await _ollama().request(
        "GET", f"/library/{name}/tags", headers={"Accept": "application/json"},
        raise_on_error=False,
    )
    if resp.status_code == 404:
        raise DeployError(f"ollama library has no model {name!r}",
                          hint="browse https://ollama.com/library")
    raise_for(resp, provider="ollama-library")
    try:
        body = resp.json()
    except ValueError:
        return []
    tags = body.get("tags") if isinstance(body, dict) else None
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


async def ollama_manifest(model: str, tag: str = "latest") -> dict[str, Any]:
    """The OCI manifest (``layers[].size`` sum = download size) plus the
    parsed config blob when reachable (``model_type`` like ``"8.2B"``,
    ``file_type`` like ``"Q4_K_M"``)."""

    name, _, t = model.partition(":")
    tag = t or tag or "latest"
    client = _ollama()
    manifest = await client.json(
        "GET", f"/v2/library/{name}/manifests/{tag}",
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
        what="ollama manifest",
    )
    if not isinstance(manifest, dict):
        return {}
    layers = manifest.get("layers") or []
    size = sum(int(layer.get("size") or 0) for layer in layers if isinstance(layer, dict))
    out: dict[str, Any] = {"name": name, "tag": tag, "size_bytes": size, "layers": layers}
    cfg = manifest.get("config") or {}
    digest = cfg.get("digest") if isinstance(cfg, dict) else None
    if digest:
        resp = await client.request("GET", f"/v2/library/{name}/blobs/{digest}", raise_on_error=False)
        if resp.status_code == 200:
            try:
                blob = resp.json()
                if isinstance(blob, dict):
                    out["config"] = blob
            except ValueError:
                pass
    return out
