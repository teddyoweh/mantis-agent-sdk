"""Per-token offers — run a model on a provider's own serving instead of renting GPUs.

The Deploy sheet answers "where can this model run?" with GPUs by the hour.
For the popular open models that is the expensive answer: Fireworks serves a
set of them on its own fleet (serverless) and Modal on Shared Endpoints, both
billed per token, with nothing to boot, scale or stop. :func:`offers` lists
those options for one Hugging Face id so the sheet can put them first, and
:func:`use_offer` switches mantis to one.

Fireworks: which models are serverless comes live from its control plane
(``supports_serverless=true`` on the public library, cached an hour). No API
returns prices, so they are ``budget.PRICING_TABLE`` rows, dated — the same
rows the terminal's cost readout bills at. Using one is switching mantis to
``accounts/fireworks/models/<slug>`` on the inference API with the key the
Fireworks deploys already use.

Modal: Shared Endpoints (Team and Enterprise plans) are created per workspace
from the Modal dashboard and answer at one gateway,
``https://inference.us-west.modal.direct/v1``, with the endpoint's hostname as
``model`` and the workspace proxy token as the bearer (``wk-….ws-…``). Neither
the SDK nor the CLI creates a shared one (``modal endpoint create`` makes a
dedicated endpoint), so the offer links to Modal's create page and connects
to the hostname pasted back. Which library models have a shared endpoint, and
their prices, are :data:`MODAL_SHARED`, dated.

Cerebras and OpenRouter publish their model lists with live prices and the
Hugging Face repo each model is. So one model is a row with every provider
you are connected to that serves it (a :class:`Route` each). A provider you
have no key for is not shown and its list is not fetched.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..paths import get_mantis_agent_dir
from .base import DeployError

__all__ = [
    "FIREWORKS_INFERENCE_BASE",
    "MODAL_SHARED",
    "MODAL_SHARED_BASE",
    "Offer",
    "Route",
    "Served",
    "catalog",
    "fireworks_offer",
    "modal_offer",
    "offers",
    "use_offer",
]

FIREWORKS_INFERENCE_BASE = "https://api.fireworks.ai/inference/v1"
FIREWORKS_KEYS_URL = "https://app.fireworks.ai/settings/users/api-keys"
MODAL_SHARED_BASE = "https://inference.us-west.modal.direct/v1"
MODAL_PROXY_TOKENS_URL = "https://modal.com/settings/proxy-auth-tokens"
#: keyless model lists with live prices and the Hugging Face repo of each model
CEREBRAS_PUBLIC_MODELS = "https://api.cerebras.ai/public/v1/models"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
_PUBLIC_TTL_S = 3600.0
_public_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

#: Modal library models with a Shared Endpoint: HF id → (prompt, cached prompt,
#: completion) USD per 1M tokens — modal.com/library/<org>/<model>, October 2026.
MODAL_SHARED: dict[str, tuple[float, float | None, float]] = {
    "zai-org/GLM-5.3-Flash": (0.45, 0.09, 1.50),
    "nvidia/GLM-5.3-Flash-NVFP4": (0.15, 0.03, 0.50),
    "zai-org/GLM-5.3": (1.40, 0.26, 4.40),
    "moonshotai/Kimi-K3": (3.00, 0.30, 15.00),
    "deepseek-ai/DeepSeek-V4.1-Flash": (0.30, 0.03, 1.20),
    "meta-models/Muse-Spark-Open-816B-A42B": (1.25, 0.15, 4.25),
    "Qwen/Qwen3.8-2.4T-A95B": (2.00, 0.25, 6.00),
    "Qwen/Qwen3.8-Max-VL-Thinking": (2.00, 0.25, 6.00),
    "thinkingmachines/Inkling-NVFP4": (1.20, 0.27, 5.00),
}

_MODAL_HOST = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.modal\.direct$")


@dataclass(frozen=True)
class Offer:
    """One way to use a model per token. ``ready`` means one click switches
    mantis to it; otherwise ``needs`` says what is missing, in words, and
    ``setup_url`` is where to fix it."""

    provider: str                 # "fireworks" | "modal"
    display_name: str
    kind: str                     # "serverless" (Fireworks) | "shared" (Modal)
    model: str                    # what goes in ``model=``; "" until a Modal endpoint is known
    backend: str                  # OpenAI base URL including /v1
    price_in: float | None        # USD per 1M input tokens; None = no row in the table
    price_out: float | None
    price_cached: float | None = None
    context: int | None = None
    ready: bool = False
    auth_ready: bool = False      # the credential is saved (Modal: the proxy token)
    needs: str = ""
    setup_url: str = ""
    logo: str = ""                # the deploy logo id the sheet's marks use


@dataclass(frozen=True)
class Route:
    """One provider serving a model per token."""

    provider: str                 # "fireworks", "cerebras", "openrouter", "modal"
    provider_name: str
    model: str                    # what goes in ``model=`` there; "" for a Modal endpoint not made yet
    backend: str                  # OpenAI base URL including /v1
    price_in: float | None        # USD per 1M input tokens; None = not published
    price_out: float | None
    price_cached: float | None = None
    context: int | None = None
    tools: bool = False
    connected: bool = False       # one click uses it
    needs: str = ""               # what connecting takes, in words, when not
    logo: str = ""

    @property
    def blended(self) -> float | None:
        """3:1 input to output, the usual single number for comparing prices."""
        if self.price_in is None or self.price_out is None:
            return None
        return (3 * self.price_in + self.price_out) / 4


@dataclass(frozen=True)
class Served:
    """One model you can use per token right now, with every provider that
    serves it — connected ones first, cheapest first. A row of the Deploy
    page's "Use now" list."""

    name: str
    hf_id: str                    # "" for a provider's own model (Fireworks' Ember-1)
    routes: tuple[Route, ...]
    context: int | None = None
    tools: bool = False


def _route_rank(r: Route) -> tuple[bool, bool, float]:
    return (not r.connected, r.blended is None, r.blended or 0.0)


# ---------------------------------------------------------------------------
# Fireworks serverless
# ---------------------------------------------------------------------------


def _fireworks_key() -> str:
    """The Fireworks key from wherever it was saved — the environment, the
    saved deploy credentials (mantis settings, read without exporting them),
    or the model catalogue."""
    key = (os.environ.get("FIREWORKS_API_KEY") or "").strip()
    if key:
        return key
    try:
        from ..settings import SETTING_SOURCES, load_settings_env_safe  # noqa: PLC0415

        key = str((load_settings_env_safe(SETTING_SOURCES) or {}).get("FIREWORKS_API_KEY") or "").strip()
        if key:
            return key
    except Exception:  # noqa: BLE001 — unreadable settings: try the catalogue
        pass
    try:
        from .. import catalog  # noqa: PLC0415

        return (catalog.api_key_for(catalog.BY_ID["fireworks"]) or "").strip()
    except Exception:  # noqa: BLE001 — no catalogue, no key
        return ""


def _fireworks_price(slug: str) -> tuple[float | None, float | None, float | None]:
    """Exact table row only — a prefix match would bill ``glm-5p3-flash`` at
    ``glm-5p3``'s rate, and a wrong price is worse than a dash."""
    from ..budget import PRICING_TABLE  # noqa: PLC0415

    pr = PRICING_TABLE.get(("fireworks", slug.lower()))
    if pr is None:
        return None, None, None
    return pr.prompt_per_million, pr.completion_per_million, pr.cache_read_per_million


async def fireworks_offer(hf_model: str) -> Offer | None:
    """Fireworks serves ``hf_model`` per token, or None. Needs the key: the
    serverless list is an authenticated call, and without a key there is
    nothing to use it with anyway."""
    from .providers.fireworks_dedicated import FireworksDedicatedProvider, library_match  # noqa: PLC0415

    key = _fireworks_key()
    if not key:
        return None
    # Two slugs can share a repo (qwen3p8-max and qwen3p8-2p4t-a95b are both
    # Qwen/Qwen3.8-2.4T-A95B, and only the first has a serverless price), so
    # the priced entry is matched first.
    rows = sorted(await FireworksDedicatedProvider().serverless_models(key=key),
                  key=lambda m: _fireworks_price(str(m.get("name", "")).rsplit("/", 1)[-1])[0] is None)
    hit = library_match(hf_model, rows)
    if hit is None:
        return None
    name = str(hit["name"])
    p_in, p_out, p_cached = _fireworks_price(name.rsplit("/", 1)[-1])
    ctx = hit.get("contextLength")
    return Offer(
        provider="fireworks", display_name="Fireworks", kind="serverless", model=name,
        backend=FIREWORKS_INFERENCE_BASE, price_in=p_in, price_out=p_out, price_cached=p_cached,
        context=int(ctx) if isinstance(ctx, int) and ctx > 0 else None,
        ready=True, auth_ready=True, setup_url=FIREWORKS_KEYS_URL, logo="fireworks-dedicated",
    )


_HF_URL = re.compile(r"huggingface\.co/([^/?#]+/[^/?#]+)")
_NOT_CHAT = re.compile(r"embed|rerank", re.I)


def _per_m(v: Any) -> float | None:
    """A per-token price string ("0.00000035") as USD per 1M tokens."""
    try:
        return round(float(v) * 1e6, 4)
    except (TypeError, ValueError):
        return None


async def _public_list(url: str) -> list[dict[str, Any]]:
    """A keyless ``/models`` list, cached an hour; an outage is an empty list."""
    import time  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    now = time.monotonic()
    hit = _public_cache.get(url)
    if hit and now - hit[0] < _PUBLIC_TTL_S:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
            r = await client.get(url)
        r.raise_for_status()
        rows = [m for m in (r.json().get("data") or []) if isinstance(m, dict)]
    except Exception:  # noqa: BLE001 — a missing source drops its routes, nothing else
        return hit[1] if hit else []
    _public_cache[url] = (now, rows)
    return rows


def _catalog_provider(pid: str) -> tuple[str, str, bool]:
    """(label, base URL, connected) for a model-catalogue provider."""
    from .. import catalog  # noqa: PLC0415

    prov = catalog.BY_ID[pid]
    try:
        on = bool(catalog.is_enabled(prov))
    except Exception:  # noqa: BLE001
        on = False
    return prov.label, prov.base_url, on


async def _fireworks_routes() -> list[tuple[str, str, Route]]:
    """(repo, display name, route) for every Fireworks serverless chat model."""
    from .providers.fireworks_dedicated import FireworksDedicatedProvider  # noqa: PLC0415

    key = _fireworks_key()
    if not key:
        return []
    out: list[tuple[str, str, Route]] = []
    for m in await FireworksDedicatedProvider().serverless_models(key=key):
        name = str(m.get("name") or "")
        slug = name.rsplit("/", 1)[-1]
        label = str(m.get("displayName") or slug)
        if not slug or _NOT_CHAT.search(slug + " " + label):
            continue
        hit = _HF_URL.search(str(m.get("huggingFaceUrl") or ""))
        p_in, p_out, p_cached = _fireworks_price(slug)
        ctx = m.get("contextLength")
        out.append((hit.group(1) if hit else "", label, Route(
            provider="fireworks", provider_name="Fireworks", model=name, backend=FIREWORKS_INFERENCE_BASE,
            price_in=p_in, price_out=p_out, price_cached=p_cached,
            context=int(ctx) if isinstance(ctx, int) and ctx > 0 else None,
            tools=bool(m.get("supportsTools")), connected=True, logo="fireworks")))
    return out


async def _cerebras_routes() -> list[tuple[str, str, Route]]:
    label, base, on = _catalog_provider("cerebras")
    if not on:
        return []           # no key: not yours, not shown, not fetched
    out: list[tuple[str, str, Route]] = []
    for m in await _public_list(CEREBRAS_PUBLIC_MODELS):
        hf, mid = str(m.get("hugging_face_id") or ""), str(m.get("id") or "")
        if not hf or not mid:
            continue
        pr = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
        lim = m.get("limits") if isinstance(m.get("limits"), dict) else {}
        cap = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
        ctx = lim.get("max_context_length")
        out.append((hf, str(m.get("name") or mid), Route(
            provider="cerebras", provider_name="Cerebras", model=mid, backend=base,
            price_in=_per_m(pr.get("prompt")), price_out=_per_m(pr.get("completion")),
            context=int(ctx) if isinstance(ctx, int) and ctx > 0 else None, tools=bool(cap.get("tools")),
            connected=on, needs="" if on else "a Cerebras key", logo="cerebras")))
    return out


async def _openrouter_routes() -> list[tuple[str, str, Route]]:
    label, base, on = _catalog_provider("openrouter")
    if not on:
        return []           # no key: not yours, not shown, not fetched
    out: list[tuple[str, str, Route]] = []
    for m in await _public_list(OPENROUTER_MODELS):
        hf, mid = str(m.get("hugging_face_id") or ""), str(m.get("id") or "")
        # ":free" and ":batch" are the same weights on other terms (rate-limited,
        # or a batch API an agent loop can't use); the plain id is the offer
        if not hf or not mid or ":" in mid:
            continue
        pr = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
        ctx = m.get("context_length")
        name = str(m.get("name") or mid).split(": ", 1)[-1]
        out.append((hf, name, Route(
            provider="openrouter", provider_name="OpenRouter", model=mid, backend=base,
            price_in=_per_m(pr.get("prompt")), price_out=_per_m(pr.get("completion")),
            price_cached=_per_m(pr.get("input_cache_read")),
            context=int(ctx) if isinstance(ctx, int) and ctx > 0 else None,
            tools="tools" in (m.get("supported_parameters") or []),
            connected=on, needs="" if on else "an OpenRouter key", logo="openrouter")))
    return out


def _modal_routes() -> list[tuple[str, str, Route]]:
    out: list[tuple[str, str, Route]] = []
    for hf in MODAL_SHARED:
        o = modal_offer(hf)
        if o is None:
            continue
        out.append((hf, "", Route(
            provider="modal", provider_name="Modal", model=o.model, backend=o.backend,
            price_in=o.price_in, price_out=o.price_out, price_cached=o.price_cached,
            connected=o.ready, needs=o.needs, logo="modal")))
    return out


async def _routes_by_repo() -> tuple[dict[str, list[Route]], dict[str, str], dict[str, str]]:
    """Every per-token route, grouped by the model it serves: (routes, name,
    repo) keyed by the lowercased repo (a provider's own model keys on its
    id). One per provider per model — the priced, cheaper one wins."""
    routes: dict[str, list[Route]] = {}
    named: dict[str, dict[str, str]] = {}     # key → provider → that provider's name for it
    repos: dict[str, str] = {}
    sources = []
    for fn in (_fireworks_routes, _cerebras_routes, _openrouter_routes):
        try:
            sources.append(await fn())
        except Exception:  # noqa: BLE001 — one source down hides only its routes
            sources.append([])
    sources.append(_modal_routes())
    for rows in sources:          # name priority follows this order
        for hf, name, r in rows:
            key = hf.lower() if hf else f"{r.provider}:{r.model}"
            have = routes.setdefault(key, [])
            same = next((x for x in have if x.provider == r.provider), None)
            if same is not None:
                if _route_rank(r) < _route_rank(same):
                    have[have.index(same)] = r
                    if name:
                        named[key][r.provider] = name    # the name follows the route that won
                continue
            have.append(r)
            if name:
                named.setdefault(key, {})[r.provider] = name
            repos.setdefault(key, hf)
    names = {k: next(iter(v.values())) for k, v in named.items() if v}
    return routes, names, repos


async def catalog() -> list[Served]:
    """Every model you can use per token right now, with each connected
    provider that serves it, cheapest first. Only routes you can use: a
    provider without a key, or a Modal shared endpoint not made yet, is left
    off the row. Rows sort by their best price."""
    routes, names, repos = await _routes_by_repo()
    out: list[Served] = []
    for key, rs in routes.items():
        rs = sorted((r for r in rs if r.connected), key=_route_rank)
        if not rs:
            continue
        hf = repos.get(key, "")
        out.append(Served(
            name=names.get(key) or hf.rsplit("/", 1)[-1] or rs[0].model, hf_id=hf, routes=tuple(rs),
            context=rs[0].context, tools=any(r.tools for r in rs)))
    out.sort(key=lambda m: _route_rank(m.routes[0]) + (m.name.lower(),))
    return out


# ---------------------------------------------------------------------------
# Modal Shared Endpoints
# ---------------------------------------------------------------------------


def _modal_entry(hf_model: str) -> tuple[str, tuple[float, float | None, float]] | None:
    want = hf_model.strip().lower()
    for hf, price in MODAL_SHARED.items():
        if hf.lower() == want:
            return hf, price
    return None


def _shared_path() -> Any:
    return get_mantis_agent_dir() / "shared_endpoints.json"


def _load_shared() -> dict[str, str]:
    try:
        data = json.loads(_shared_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_shared(hf_model: str, host: str) -> None:
    data = _load_shared()
    data[hf_model.strip().lower()] = host
    p = _shared_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _modal_proxy_token() -> bool:
    return bool((os.environ.get("MODAL_PROXY_TOKEN_ID") or "").strip()
                and (os.environ.get("MODAL_PROXY_TOKEN_SECRET") or "").strip())


def _check_modal_shared(host: str) -> str:
    """Ask Modal's gateway which shared endpoints the proxy token reaches —
    a free ``GET /v1/models``, no inference. Raises when Modal says no; returns
    a warning when the check itself couldn't run (some networks reset every
    ``*.modal.direct`` handshake), so the switch still happens."""
    import httpx  # noqa: PLC0415

    bearer = (os.environ.get("MODAL_PROXY_TOKEN_ID", "").strip() + "."
              + os.environ.get("MODAL_PROXY_TOKEN_SECRET", "").strip())
    try:
        r = httpx.get(MODAL_SHARED_BASE + "/models", headers={"Authorization": "Bearer " + bearer}, timeout=10.0)
    except httpx.HTTPError:
        return ("couldn't reach Modal's gateway from this network to check the endpoint "
                "(some networks reset *.modal.direct) — switched anyway")
    if r.status_code in (401, 403):
        raise DeployError("Modal turned the proxy token away",
                          hint="check it at " + MODAL_PROXY_TOKENS_URL
                               + " — Shared Endpoints need a Team or Enterprise plan")
    if r.status_code != 200:
        return f"Modal's gateway answered {r.status_code} to the check — switched anyway"
    try:
        rows = r.json().get("data") or []
    except ValueError:
        rows = []
    ids = [str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("id")]
    if host not in ids:
        raise DeployError(f"{host} isn't an endpoint this proxy token reaches",
                          hint=("it reaches: " + ", ".join(ids[:6])) if ids
                               else "create the Shared Endpoint on Modal first")
    return ""


def modal_create_url(hf_model: str) -> str:
    return "https://modal.com/endpoints?model=" + quote(hf_model, safe="") + "&type=shared"


def modal_offer(hf_model: str) -> Offer | None:
    """A Modal Shared Endpoint for ``hf_model``, or None when Modal has none.
    No network: the table says whether one exists, and the endpoint you made
    is remembered from the last time it was connected."""
    entry = _modal_entry(hf_model)
    if entry is None:
        return None
    hf, (p_in, p_cached, p_out) = entry
    host = _load_shared().get(hf.lower(), "")
    token = _modal_proxy_token()
    needs = ""
    if not host:
        needs = "a Shared Endpoint on Modal (Team plan)"
    elif not token:
        needs = "your Modal proxy token"
    return Offer(
        provider="modal", display_name="Modal", kind="shared", model=host,
        backend=MODAL_SHARED_BASE, price_in=p_in, price_out=p_out, price_cached=p_cached,
        ready=bool(host and token), auth_ready=token, needs=needs, setup_url=modal_create_url(hf), logo="modal",
    )


# ---------------------------------------------------------------------------
# Both
# ---------------------------------------------------------------------------


async def offers(hf_model: str) -> list[Offer]:
    """Every per-token way to use ``hf_model``, ready ones first, then
    cheapest. One provider failing never hides the others."""
    out: list[Offer] = []
    try:
        fw = await fireworks_offer(hf_model)
        if fw is not None:
            out.append(fw)
    except Exception:  # noqa: BLE001 — a Fireworks outage only drops its row
        pass
    md = modal_offer(hf_model)
    if md is not None:
        out.append(md)
    want = hf_model.strip().lower()
    for fn in (_cerebras_routes, _openrouter_routes):
        try:
            rows = await fn()
        except Exception:  # noqa: BLE001
            continue
        for hf, _name, r in rows:
            if hf.lower() == want:
                out.append(Offer(
                    provider=r.provider, display_name=r.provider_name, kind="api", model=r.model,
                    backend=r.backend, price_in=r.price_in, price_out=r.price_out, price_cached=r.price_cached,
                    context=r.context, ready=r.connected, auth_ready=r.connected, needs=r.needs, logo=r.logo))
                break

    def rank(o: Offer) -> tuple[bool, bool, float]:
        b = None if o.price_in is None or o.price_out is None else (3 * o.price_in + o.price_out) / 4
        return (not o.ready, b is None, b or 0.0)

    out.sort(key=rank)
    return out


def use_offer(provider: str, *, hf_model: str, model: str = "",
              token_id: str = "", token_secret: str = "") -> dict[str, Any]:
    """Switch mantis to a per-token offer. Fireworks takes the serverless
    model name; Modal takes the shared endpoint's hostname (and the proxy
    token, when it isn't saved yet). Nothing is created anywhere."""
    from .. import catalog  # noqa: PLC0415

    provider = (provider or "").strip()
    model = (model or "").strip()
    if provider == "fireworks":
        if not model.startswith("accounts/fireworks/models/"):
            raise DeployError("not a Fireworks serverless model: " + (model or "(empty)"))
        if not _fireworks_key():
            raise DeployError("no Fireworks key saved", hint="add one under Deploy → Fireworks")
        backend = FIREWORKS_INFERENCE_BASE
    elif provider == "modal":
        host = re.sub(r"^https?://", "", model).strip("/").lower()
        if not _MODAL_HOST.match(host):
            raise DeployError("that isn't a Modal endpoint hostname",
                              hint="it looks like my-endpoint.us-west.modal.direct — copy it from "
                                   "the endpoint's request example on Modal")
        if host.startswith("inference."):
            raise DeployError("that is Modal's gateway, not your endpoint",
                              hint="paste the endpoint's own hostname, the one its request example sends as model")
        if (token_id or "").strip() or (token_secret or "").strip():
            if not ((token_id or "").strip() and (token_secret or "").strip()):
                raise DeployError("the proxy token needs both halves, wk-… and ws-…")
            from .store import save_credentials  # noqa: PLC0415

            save_credentials("modal", {"MODAL_PROXY_TOKEN_ID": token_id.strip(),
                                       "MODAL_PROXY_TOKEN_SECRET": token_secret.strip()})
        if not _modal_proxy_token():
            raise DeployError("Modal shared endpoints need a proxy token",
                              hint="create one at " + MODAL_PROXY_TOKENS_URL)
        warning = _check_modal_shared(host)
        if (hf_model or "").strip():
            _save_shared(hf_model, host)
        model, backend = host, MODAL_SHARED_BASE
    elif provider in catalog.BY_ID:
        prov = catalog.BY_ID[provider]
        if not catalog.is_enabled(prov):
            raise DeployError(f"{prov.label} isn't connected", hint="add its key under My models → Connect")
        if not model:
            raise DeployError(f"which {prov.label} model? none was given")
        backend = prov.base_url
    else:
        raise DeployError(f"no per-token offer from {provider!r}")
    catalog.set_last_model(model, backend)
    catalog.push_recent_model(model)
    out: dict[str, Any] = {"model": model, "backend": backend, "provider": provider}
    if provider == "modal" and warning:
        out["warning"] = warning
    return out
