"""``mantis serve`` — a local, read-only web dashboard over your mantis state.

One command spins up a tiny stdlib HTTP server (no extra deps) that serves a
single self-contained page showing:

* **Overview** — the five provider families (OpenAI, Claude, Gemini, Grok,
  open source) with auth state and a one-click reachability test; spend and
  usage per day and per provider (session tokens *estimated* from transcript
  size, workflow runs *recorded*); live background jobs and workflow runs.
* **Sessions** — every conversation across every project on this machine,
  as project and session cards (friendly titles, cost pills), drilling into a
  timeline with context fill and cost per turn. Secrets are masked before
  anything leaves the process.
* **Models & hosting** — the model list grouped by family with context
  window and price per 1M tokens, local Ollama models with size and loaded
  state, and each provider's setup.
* **Config** — the merged effective settings plus the user/project/local layers.

Everything is read straight from ``~/.mantis-agent`` — nothing is mutated. By
default it binds to loopback only. ``--lan`` exposes it to your local network
(so another device can open it) behind a URL token.

The UI markup lives in :mod:`mantis_agent.serve_ui`; this module is the server
and the read-only data layer over the on-disk stores.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Read-only data layer — everything comes from ~/.mantis-agent, no mutation.
# mantis internals are imported lazily inside helpers so ``mantis serve --help``
# and the module import stay cheap (matches the rest of the package's style).
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    from . import paths  # noqa: PLC0415

    return paths.get_mantis_agent_dir()


def _projects_root() -> Path:
    return _base_dir() / "projects"


def _version() -> str:
    try:
        from . import __version__  # noqa: PLC0415

        return __version__
    except Exception:  # noqa: BLE001
        return "?"


def _project_cwd(project_dir: Path) -> str | None:
    """Recover the real project path from inside a session file. The dir name is
    a one-way ``sha1(cwd)[:12]`` digest, but every message entry stores its
    ``cwd`` verbatim — so read the first entry that has one."""
    for f in sorted(project_dir.glob("*.jsonl")):
        try:
            with f.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if cwd:
                        return str(cwd)
        except OSError:
            continue
    return None


_projects_cache: dict[str, Any] = {}
_projects_lock = threading.Lock()


def list_projects() -> list[dict[str, Any]]:
    """Cached wrapper over :func:`_list_projects_compute`, keyed on the
    projects-tree signature so repeated /api/projects and /api/overview calls
    don't re-glob and re-read every transcript when nothing changed."""
    sig = _projects_signature()
    with _projects_lock:
        if _projects_cache.get("sig") == sig and "data" in _projects_cache:
            return _projects_cache["data"]
    data = _list_projects_compute()
    with _projects_lock:
        _projects_cache["sig"] = sig
        _projects_cache["data"] = data
    return data


def _list_projects_compute() -> list[dict[str, Any]]:
    """Every project dir under ``projects/`` with its recovered cwd, visible
    session count, and last-activity time. Newest activity first."""
    from . import session_tree  # noqa: PLC0415

    root = _projects_root()
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        files = list(d.glob("*.jsonl"))
        if not files:
            continue
        cwd = _project_cwd(d)
        try:
            sessions = session_tree.list_sessions(cwd=cwd) if cwd else []
        except Exception:  # noqa: BLE001
            sessions = []
        last = max((s.modified_at for s in sessions),
                   default=d.stat().st_mtime)
        first_prompt = next((x for x in (_clean_prompt(s.first_prompt or s.title) or _first_user_prompt(s.path)
                                         for s in sessions) if x), None)
        out.append({
            "digest": d.name,
            "cwd": cwd,
            "name": Path(cwd).name if cwd else d.name,
            "title": _project_title(cwd, d.name, first_prompt),
            "first_prompt": first_prompt,
            "path": cwd or str(d),
            "session_count": len(sessions),
            "last_activity": last,
        })
    # cost pills from the analytics pass (same signature → same cache)
    try:
        a = analytics()
        pricing = _session_pricing(*_current_model_backend())
        for p in out:
            led = (a.get("projects") or {}).get(p["digest"]) or {}
            p["msgs"] = int(led.get("msgs") or 0)
            p["tokens_est"] = int(led.get("in_est") or 0) + int(led.get("out_est") or 0)
            p["usd_est"] = _usd(pricing, int(led.get("in_est") or 0), int(led.get("out_est") or 0))
    except Exception:  # noqa: BLE001 — pills are optional
        pass
    out.sort(key=lambda p: p["last_activity"], reverse=True)
    return out


_UUIDISH_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$|^[0-9a-f]{12,}$", re.I)
_META_BLOCK_RE = re.compile(r"<(system-reminder|env)>.*?</\1>|\[context\]", re.S | re.I)


def _clean_prompt(text: Any, limit: int = 90) -> str | None:
    """A prompt fit for a card title: meta blocks stripped, whitespace folded,
    truncated. ``None`` when nothing human remains."""
    if not text:
        return None
    t = _META_BLOCK_RE.sub(" ", str(text))
    t = " ".join(t.split()).strip()
    if not t:
        return None
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _first_user_prompt(path: Any, max_lines: int = 60) -> str | None:
    """The first human prompt in a transcript, read from the file head. The
    session lister's own ``first_prompt`` skips prompts that open with an
    injected block, which is exactly the case where a card needs a title."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "user" or obj.get("isMeta"):
                    continue
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(str(b.get("text") or "") for b in content
                                       if isinstance(b, dict) and b.get("type") == "text")
                got = _clean_prompt(content)
                if got:
                    return got
    except OSError:
        return None
    return None


def _project_title(cwd: str | None, digest: str, first_prompt: str | None) -> str:
    """Basename of the cwd, unless that is itself an id-shaped string (a temp
    dir, a checkout named by hash) — then the first real prompt, then a short
    label. Never a bare UUID: the id is a caption, not a name."""
    base = Path(cwd).name if cwd else ""
    if base and not _UUIDISH_RE.match(base):
        return base
    if first_prompt:
        return first_prompt
    return "project · " + digest[:8]


def _current_model_backend() -> tuple[str | None, str | None]:
    from . import catalog  # noqa: PLC0415

    try:
        last = catalog.get_last_model() or {}
    except Exception:  # noqa: BLE001
        last = {}
    return last.get("model"), last.get("backend")


def sessions_for(cwd: str) -> list[dict[str, Any]]:
    from . import session_tree  # noqa: PLC0415

    infos = session_tree.list_sessions(cwd=cwd)
    try:
        ledger = analytics().get("sessions") or {}
        model, backend = _current_model_backend()
        pricing = _session_pricing(model, backend)
    except Exception:  # noqa: BLE001
        ledger, pricing, model = {}, {"known": False}, None
    out = []
    for s in infos:
        led = ledger.get(s.session_id) or {}
        t_in, t_out = int(led.get("in_est") or 0), int(led.get("out_est") or 0)
        prompt = _clean_prompt(s.first_prompt) or _first_user_prompt(s.path)
        out.append({
            "session_id": s.session_id,
            "title": _clean_prompt(s.title) if s.title and not _UUIDISH_RE.match(str(s.title)) else None,
            "display_title": (_clean_prompt(s.title) if s.title and not _UUIDISH_RE.match(str(s.title)) else None)
                             or prompt or ("session · " + s.session_id[:8]),
            "first_prompt": prompt,
            "last_prompt": _clean_prompt(s.last_prompt),
            "modified_at": s.modified_at,
            "message_count": s.message_count,
            "tokens_est": t_in + t_out,
            "usd_est": _usd(pricing, t_in, t_out),
            "tools": int(led.get("tools") or 0),
            "model": model,   # what the estimate is priced at — transcripts record no model
        })
    return out


def _content_chars(content: Any) -> int:
    """How many characters a message's content occupies — the basis of the
    token *estimate* (≈ 4 chars/token) the dashboard uses, because transcripts
    persist ``{role, content}`` only and never the provider's usage record."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, dict):
        return len(json.dumps(content, default=str))
    n = 0
    for b in content:
        if not isinstance(b, dict):
            n += len(str(b))
            continue
        t = b.get("type")
        if t == "text":
            n += len(b.get("text") or "")
        elif t == "thinking":
            n += len(b.get("thinking") or "")
        elif t == "tool_use":
            n += len(b.get("name") or "") + len(json.dumps(b.get("input") or {}, default=str))
        elif t == "tool_result":
            n += _content_chars(b.get("content"))
        elif t == "image":
            n += 1600  # a rough image-token charge; the base64 itself isn't billed as text
        else:
            n += len(json.dumps(b, default=str))
    return n


def _est_tokens(chars: int) -> int:
    return int(round(chars / 4.0))


def _ts_epoch(ts: Any) -> float | None:
    """ISO-8601 (as the transcript writes it) → epoch seconds, or None."""
    from datetime import datetime  # noqa: PLC0415

    if not ts:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Secret-shaped substrings inside free text: vendor key prefixes, bearer
# tokens, and `NAME=value` / `"name": "value"` pairs whose NAME looks like a
# credential. Transcripts routinely contain `cat .env` output and curl
# commands, so the page masks these before they leave the process.
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk|xai|gsk|rk|pk|ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abpors]|AIza|AKIA|ASIA|hf|r8|pplx|csk|fw|tgp|ya29)"
    r"[-_]?[A-Za-z0-9_\-]{16,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer|x-api-key:?|api[-_]?key:?)\s+([A-Za-z0-9._\-]{16,})")
_KV_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_\-]*(?:key|token|secret|password|passwd|credential|cookie)[A-Za-z0-9_\-]*)"
    r"([\"']?\s*[=:]\s*[\"']?)([^\s\"'&,;]{8,})")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _redact_text(s: str) -> str:
    """Mask credential-shaped values in one string; see :func:`_redact_content`."""
    if not s or len(s) < 12:
        return s
    s = _KV_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + (_mask_key(m.group(3)) or ""), s)
    s = _BEARER_RE.sub(lambda m: m.group(1) + " " + (_mask_key(m.group(2)) or ""), s)
    s = _SECRET_TOKEN_RE.sub(lambda m: _mask_key(m.group(0)) or "", s)

    def _url(m: re.Match[str]) -> str:
        try:
            return redact_url_value(m.group(0))
        except Exception:  # noqa: BLE001
            return m.group(0)
    return _URL_RE.sub(_url, s)


def _redact_content(obj: Any) -> Any:
    """Recursively redact every string inside message content. Dict values
    under a credential-looking key are masked whole (``_redact_settings``
    semantics); every other string is scanned for key-shaped substrings."""
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, list):
        return [_redact_content(x) for x in obj]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and _SECRET_KEY_RE.search(str(k)) and str(k) not in ("tool_use_id", "id"):
                out[k] = _mask_key(v)
            else:
                out[k] = _redact_content(v)
        return out
    return obj


def _session_pricing(model: str | None, backend: str | None) -> dict[str, Any]:
    """Pricing the dashboard bills *estimated* session tokens at: the current
    model's rate from ``budget.lookup_pricing`` (provider-hinted), free for
    local/self-hosted, unknown otherwise."""
    from .budget import lookup_pricing  # noqa: PLC0415

    model = model or ""
    hint = _provider_id_for_model(model) or _provider_id_for_backend(backend)
    b = (backend or "").lower()
    if not hint and ("localhost" in b or "127.0.0.1" in b or ":11434" in b):
        hint = "ollama"
    pr = lookup_pricing(model, hint) if model else None
    return {
        "model": model or None,
        "provider": hint,
        "known": pr is not None,
        "prompt_per_million": pr.prompt_per_million if pr else None,
        "completion_per_million": pr.completion_per_million if pr else None,
    }


def _usd(pricing: dict[str, Any], tokens_in: int, tokens_out: int) -> float | None:
    if not pricing.get("known"):
        return None
    return (tokens_in / 1e6) * float(pricing["prompt_per_million"] or 0) + \
        (tokens_out / 1e6) * float(pricing["completion_per_million"] or 0)


def session_detail(cwd: str, session_id: str) -> dict[str, Any]:
    """The full reconstructed transcript as plain JSON-able dicts, plus the
    per-turn ledger the timeline draws: estimated context fill, output tokens
    and cost for every assistant turn.

    ``role`` is read off each typed ``Message`` object explicitly — the structs
    use ``omit_defaults=True`` so msgspec drops ``role`` (it equals its only
    value), which would leave the UI unable to tell user from assistant. The
    content blocks are msgspec-encoded so each keeps its ``type`` discriminator.
    Timestamps come from the underlying transcript chain; secrets are masked.
    """
    import msgspec  # noqa: PLC0415

    from . import catalog, session_tree  # noqa: PLC0415

    entries = session_tree.load_entries(session_tree._session_path(session_id, cwd))
    leaf = session_tree.latest_leaf([e for e in entries if not e.is_sidechain])
    chain = session_tree.build_chain(entries, leaf.uuid) if leaf else []
    messages = session_tree.entries_to_messages(chain) if chain else []
    # Timestamps ride on the chain, not on the Message objects. Align them by
    # walking the same slice entries_to_messages replays (after the last
    # compaction boundary); if the counts disagree (a dropped dangling
    # tool_use) timestamps are omitted rather than misattributed.
    start = 0
    for i, e in enumerate(chain):
        c = e.message.get("content")
        if e.message.get("role") == "system" and isinstance(c, dict) and c.get("__compact_boundary__"):
            start = i
    stamps = [e.timestamp for e in chain[start:] if e.message.get("role") in ("user", "assistant")]
    n_msgs = sum(1 for m in messages if getattr(m, "role", None) in ("user", "assistant"))
    stamps_ok = len(stamps) == n_msgs

    try:
        last = catalog.get_last_model() or {}
    except Exception:  # noqa: BLE001
        last = {}
    pricing = _session_pricing(last.get("model"), last.get("backend"))
    info = _model_info(last.get("model") or "") if last.get("model") else {}
    ctx_window = info.get("ctx")

    out: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    ctx_chars = 0
    tot_in = tot_out = 0
    cum_usd = 0.0
    si = 0
    for m in messages:
        role = getattr(m, "role", None)
        content = m.content
        enc = content if isinstance(content, str) else \
            msgspec.json.decode(msgspec.json.encode(content))
        if role not in ("user", "assistant"):
            # compaction boundary: context restarts from the summary
            summary = getattr(m, "summary", "") or ""
            ctx_chars = len(summary)
            out.append({"role": "system", "content": _redact_text(summary), "compact": True,
                        "compacted_count": getattr(m, "compacted_count", 0)})
            continue
        item: dict[str, Any] = {"role": role, "content": _redact_content(enc)}
        if getattr(m, "isMeta", False):
            item["isMeta"] = True
        if stamps_ok:
            item["ts"] = _ts_epoch(stamps[si])
        si += 1
        chars = _content_chars(enc)
        if role == "assistant":
            t_in, t_out = _est_tokens(ctx_chars), _est_tokens(chars)
            tot_in += t_in
            tot_out += t_out
            usd = _usd(pricing, t_in, t_out)
            if usd is not None:
                cum_usd += usd
            tools = [b.get("name") for b in enc if isinstance(b, dict) and b.get("type") == "tool_use"] \
                if isinstance(enc, list) else []
            turns.append({"i": len(out), "ts": item.get("ts"), "ctx_est": t_in + t_out,
                          "in_est": t_in, "out_est": t_out, "usd_est": usd,
                          "cum_usd_est": cum_usd if pricing.get("known") else None,
                          "tools": tools})
            item["turn"] = len(turns) - 1
        ctx_chars += chars
        out.append(item)
    return {
        "session_id": session_id, "cwd": cwd, "messages": out, "turns": turns,
        "stats": {"in_est": tot_in, "out_est": tot_out, "tokens_est": tot_in + tot_out,
                  "usd_est": cum_usd if pricing.get("known") else None,
                  "turns": len(turns), "ctx_window": ctx_window,
                  "peak_ctx_est": max((t["ctx_est"] for t in turns), default=0),
                  "pricing": pricing,
                  "note": "tokens are estimated from transcript size (≈4 chars/token); "
                          "transcripts do not record provider usage"},
    }


def _mask_key(v: str | None) -> str | None:
    """Mask a secret to just a hint — first 3 + last 4 chars. Never expose the
    full key over the wire, even on loopback."""
    if not v:
        return None
    v = str(v)
    if len(v) <= 8:
        return "•" * max(4, len(v))
    return f"{v[:3]}…{v[-4:]}"


def _provider_hosting(prov: Any) -> dict[str, Any]:
    """Base URL + where this provider's API key resolves from (env / saved
    store), masked. Mirrors ``catalog.api_key_for`` resolution order."""
    import os  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    key: str | None = None
    source: str | None = None
    for name in (prov.api_key_env, *getattr(prov, "key_env_aliases", ())):
        if name and os.environ.get(name):
            key, source = os.environ[name], "env"
            break
    if key is None:
        try:
            saved = catalog.saved_key(prov.id)
        except Exception:  # noqa: BLE001
            saved = None
        if saved:
            key, source = saved, "saved"
    return {
        "base_url": prov.base_url,
        "api_key_env": prov.api_key_env,
        "key_masked": _mask_key(key),
        "key_source": source,
    }


def _hosting_summary(last: dict[str, Any], backend_now: str) -> dict[str, Any]:
    """Classify the CURRENT backend: a known provider, a self-host URL, local
    Ollama, or the built-in default."""
    from . import catalog  # noqa: PLC0415

    model = last.get("model")
    if not backend_now:
        return {"kind": "default", "label": "default backend", "model": model, "backend": ""}
    if "localhost" in backend_now or "127.0.0.1" in backend_now:
        return {"kind": "local", "label": "Local (Ollama)", "model": model, "backend": backend_now}
    prov = next((p for p in catalog.CATALOG
                 if p.base_url.rstrip("/") == backend_now), None)
    if prov:
        return {"kind": "provider", "label": prov.label, "model": model, "backend": backend_now}
    return {"kind": "selfhost", "label": "Self-hosted", "model": model, "backend": backend_now}


_DOCS_BASE = "https://mantisagent.cc/docs"


def models_state() -> dict[str, Any]:
    from . import catalog  # noqa: PLC0415
    from . import provider_guides  # noqa: PLC0415

    try:
        groups = catalog.grouped_provider_models()
    except Exception:  # noqa: BLE001
        groups = []
    try:
        last = catalog.get_last_model() or {}
    except Exception:  # noqa: BLE001
        last = {}
    backend_now = (last.get("backend") or "").rstrip("/")

    provs: list[dict[str, Any]] = []
    for g in groups:
        pid = g.get("provider_id")
        prov = catalog.BY_ID.get(pid)
        try:
            live = catalog.cached_live_models(pid) if pid else None
        except Exception:  # noqa: BLE001
            live = None
        host = _auth_state(prov) if prov else {}
        provs.append({
            "id": pid,
            "label": g.get("label"),
            "family": family_of(pid),
            "enabled": bool(g.get("enabled")) or host.get("auth") == "oauth",
            "note": g.get("note") or "",
            "models": list(g.get("models") or ()),
            "model_count": len(g.get("models") or ()),
            "live_count": len(live) if live else 0,
            "base_url": host.get("base_url"),
            "api_key_env": host.get("api_key_env"),
            "key_masked": host.get("key_masked"),
            "key_source": host.get("key_source"),
            "auth": host.get("auth") or "none",
            "is_current": bool(prov and prov.base_url.rstrip("/") == backend_now),
            "guide": provider_guides.GUIDES.get(pid),
            "docs_url": f"{_DOCS_BASE}/providers/{pid}" if pid else None,
        })
    # What each model can actually do, straight from the SDK's own capability
    # table — a model list is just strings until you can compare context
    # windows and tool support side by side. Price per 1M tokens comes from
    # budget.py's table (provider-hinted); a learned, endpoint-enforced context
    # ceiling from context_limits.py overrides the declared window.
    info: dict[str, Any] = {}
    seen: set[str] = set()
    for p in provs:
        # The same open-weight id is served by several hosts at different
        # prices, so price is per (provider, model) — never just per model.
        p["prices"] = {}
        for mid in p["models"]:
            price = _model_price(mid, p["id"])
            if price is not None:
                p["prices"][mid] = price
            if mid in seen:
                continue
            seen.add(mid)
            info[mid] = _model_info(mid, p["id"], p.get("base_url"))
    cur_model = last.get("model")
    if cur_model and cur_model not in info:
        info[cur_model] = _model_info(cur_model, _provider_id_for_model(cur_model), backend_now)

    oll = ollama_state()
    for m in oll.get("models") or []:
        if m["name"] not in info:
            info[m["name"]] = _model_info(m["name"], "ollama", oll.get("base_url"))

    # What you have DEPLOYED is a model family of its own on this page — the
    # GLM you are running on your own GPUs belongs next to the ones you rent by
    # the token. From the local store only (no provider calls), live ones only.
    deployments: list[dict[str, Any]] = []
    try:
        from .deploy import store as _dstore  # noqa: PLC0415

        for d in _dstore.load_all():
            # booting is still yours and still billing — it belongs here too
            if not ((d.is_live or d.status == "starting") and d.endpoint_url):
                continue
            dd = _dep_dict(d)
            dd["in_use"] = bool(backend_now and d.endpoint_url.rstrip("/") == backend_now)
            deployments.append(dd)
            name = d.served_model_name or d.model
            if name not in info:
                info[name] = _model_info(name, None, d.endpoint_url)
    except Exception:  # noqa: BLE001 — no deploy core, no store: no family
        deployments = []

    # What you switched to before this one. The catalog already keeps it; the
    # page had no way to get at it, so switching back meant finding the card
    # again. Only ids — whether each one is still reachable is decided by the
    # same provider data as everything else on the page.
    try:
        recent = [m for m in catalog.get_recent_models() if m != last.get("model")]
    except Exception:  # noqa: BLE001 — a shortcut is never worth a 500
        recent = []
    return {
        "current": last,
        "recent": recent[:6],
        "providers": provs,
        "families": [{"id": f[0], "label": f[1], "logo": f[2]} for f in FAMILIES],
        "model_info": info,
        "ollama": oll,
        "deployments": _deploy_redact(deployments),
        "enabled_count": sum(1 for p in provs if p["enabled"]),
        "hosting": _hosting_summary(last, backend_now),
        "selfhost_guide": provider_guides.SELFHOST,
        "selfhost_docs_url": f"{_DOCS_BASE}/guides/self-hosting",
    }


def _model_price(model_id: str, provider_id: str | None) -> dict[str, Any] | None:
    """USD per 1M tokens for a model on a provider, from ``budget.lookup_pricing``.
    ``None`` when the table has no row (the page shows a dash, never a guess)."""
    try:
        from .budget import lookup_pricing  # noqa: PLC0415

        pr = lookup_pricing(model_id, provider_id)
    except Exception:  # noqa: BLE001
        pr = None
    if pr is None:
        return None
    return {"in": pr.prompt_per_million, "out": pr.completion_per_million,
            "cache_read": pr.cache_read_per_million, "free": pr.prompt_per_million == 0 and pr.completion_per_million == 0}


def _model_info(model_id: str, provider_id: str | None = None,
                backend: str | None = None) -> dict[str, Any]:
    """Context window + tool/reasoning support for one model id, plus price per
    1M tokens and any endpoint-enforced context ceiling mantis has learned."""
    out: dict[str, Any] = {}
    try:
        from .capabilities import lookup_model  # noqa: PLC0415

        cap = lookup_model(model_id)
        out = {
            "ctx": cap.context_window,
            "tools": bool(cap.supports_native_tools),
            "effort": bool(cap.supports_reasoning_effort),
            "thinking": bool(cap.emits_thinking_blocks or cap.emits_inline_thinking),
            "family": cap.family,
        }
    except Exception:  # noqa: BLE001 — an unknown model just shows no badges
        out = {}
    try:
        from . import context_limits  # noqa: PLC0415

        learned = context_limits.learned_limit(model_id, backend)
        if learned:
            out["ctx_learned"] = learned
            if out.get("ctx"):
                out["ctx"] = context_limits.effective_window(model_id, out["ctx"], backend)
    except Exception:  # noqa: BLE001
        pass
    price = _model_price(model_id, provider_id)
    if price is not None:
        out["price"] = price
    return out


def test_provider(provider_id: str | None, backend: str | None = None,
                  key: str | None = None) -> dict[str, Any]:
    """Can we actually reach this provider with the key we have?

    Same promise the MCP page makes: prove the wiring works before you depend
    on it. One ``GET {base}/models`` with a short timeout — cheap, read-only,
    and supported by every OpenAI-compatible endpoint we list."""
    import time  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    base = (backend or "").rstrip("/")
    label = backend or provider_id or "endpoint"
    if provider_id:
        prov = catalog.BY_ID.get(provider_id)
        if prov is None:
            return {"ok": False, "error": f"unknown provider '{provider_id}'"}
        base = prov.base_url.rstrip("/")
        label = prov.label
        if not key:
            key = (_provider_hosting(prov) or {}).get("key_masked") and None
            try:
                key = catalog.api_key_for(prov)
            except Exception:  # noqa: BLE001
                key = None
    if not base:
        return {"ok": False, "error": "no endpoint to test"}

    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key            # Anthropic-style auth
        headers["anthropic-version"] = "2023-06-01"
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0)) as c:
            r = c.get(f"{base}/models", headers=headers)
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 401 or r.status_code == 403:
            return {"ok": False, "ms": ms, "label": label, "status": r.status_code,
                    "error": "the endpoint rejected this key"}
        if r.status_code >= 400:
            return {"ok": False, "ms": ms, "label": label, "status": r.status_code,
                    "error": f"HTTP {r.status_code} from {base}/models"}
        try:
            data = r.json()
            listed = data.get("data") if isinstance(data, dict) else None
            count = len(listed) if isinstance(listed, list) else None
        except ValueError:
            count = None
        return {"ok": True, "ms": ms, "label": label, "count": count, "base_url": base}
    except Exception as e:  # noqa: BLE001 — an unreachable host is an answer
        return {"ok": False, "ms": int((time.monotonic() - t0) * 1000), "label": label,
                "error": f"{type(e).__name__}: {e}".strip()}


_SECRET_KEY_RE = re.compile(r"key|token|secret|password|apikey", re.I)


def _redact_settings(obj: Any, in_env: bool = False) -> Any:
    """Recursively mask secret-looking values so raw API keys/tokens never leave
    the process. All ``env`` values are masked (they routinely hold provider
    keys), plus any value whose key name looks like a credential. Mirrors
    :func:`_mask_key` so the config path matches the masking done elsewhere."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            is_env = str(k) == "env"
            if in_env or (isinstance(v, str) and _SECRET_KEY_RE.search(str(k))):
                out[k] = _mask_key(v) if isinstance(v, str) else _redact_settings(v)
            else:
                out[k] = _redact_settings(v, in_env=is_env)
        return out
    if isinstance(obj, list):
        return [_redact_settings(x, in_env=in_env) for x in obj]
    return obj


def config_state() -> dict[str, Any]:
    from . import settings as S  # noqa: PLC0415

    layers: dict[str, Any] = {}
    paths: dict[str, str] = {}
    for src in S.SETTING_SOURCES:
        try:
            layers[src] = _redact_settings(S.load_setting_source(src))
        except Exception:  # noqa: BLE001
            layers[src] = {}
        try:
            p = S.resolve_setting_path(src)
            paths[src] = str(p) + ("" if p.exists() else "  (not present)")
        except Exception:  # noqa: BLE001
            paths[src] = ""
    try:
        merged = _redact_settings(S.load_settings(S.SETTING_SOURCES))
    except Exception:  # noqa: BLE001
        merged = {}
    return {"merged": merged, "layers": layers, "paths": paths}


# ---------------------------------------------------------------------------
# Write actions — enable a provider (save/clear its key), connect a self-host
# endpoint, or set the current model. These mutate ~/.mantis-agent just like the
# TUI's /enable and /connect commands. They take effect on the NEXT mantis
# launch (an already-running TUI keeps its own live model).
# ---------------------------------------------------------------------------


def set_provider_key(provider_id: str | None, key: str | None) -> dict[str, Any]:
    """Save or clear one catalogue provider's key. No longer routed: the
    dashboard connects providers through ``/api/auth/set`` only, so there is
    exactly one way in. Kept because the setup flows still call it directly."""
    from . import catalog  # noqa: PLC0415

    prov = catalog.BY_ID.get((provider_id or "").strip())
    if not prov:
        return {"ok": False, "error": f"unknown provider {provider_id!r}"}
    key = (key or "").strip()
    if not key:
        catalog.clear_key(prov.id)
        return {"ok": True, "provider": prov.id, "cleared": True}
    catalog.set_key(prov.id, key)
    valid, detail = True, "saved"
    try:
        valid, detail = catalog.validate_provider(prov, timeout=5.0)
    except Exception as e:  # noqa: BLE001
        valid, detail = False, str(e)
    return {"ok": True, "provider": prov.id, "valid": valid, "detail": detail,
            "key_masked": _mask_key(key)}


def connect_selfhost(backend: str | None, model: str | None,
                     key: str | None = None) -> dict[str, Any]:
    from . import catalog, paths  # noqa: PLC0415

    backend = (backend or "").strip()
    model = (model or "").strip()
    if not backend.startswith(("http://", "https://")):
        return {"ok": False, "error": "backend must be an http(s) URL"}
    if not model:
        return {"ok": False, "error": "model id required"}
    backend = paths.normalize_base_url(backend)
    catalog.set_last_model(model, backend)
    catalog.push_recent_model(model)
    if (key or "").strip():
        try:
            from .settings import update_setting_source  # noqa: PLC0415

            update_setting_source("user", {"env": {"MANTIS_AGENT_API_KEY": key.strip()}})
        except Exception as e:  # noqa: BLE001
            return {"ok": True, "model": model, "backend": backend,
                    "warning": f"model set but key not saved: {e}"}
    return {"ok": True, "model": model, "backend": backend}


def set_current(model: str | None, backend: str | None = None) -> dict[str, Any]:
    from . import catalog  # noqa: PLC0415

    model = (model or "").strip()
    if not model:
        return {"ok": False, "error": "model required"}
    catalog.set_last_model(model, (backend or "").strip() or None)
    catalog.push_recent_model(model)
    return {"ok": True, "model": model, "backend": backend or ""}


# ---------------------------------------------------------------------------
# Skills (global + project) and MCP servers — view / add / delete.
# ---------------------------------------------------------------------------


def _skills_dirs() -> tuple[Path, Path]:
    import os  # noqa: PLC0415

    from . import paths  # noqa: PLC0415

    return (paths.get_mantis_agent_dir() / "skills",
            Path(os.getcwd()) / ".mantis" / "skills")


def _read_skill(md: Path) -> dict[str, Any] | None:
    from .skills import _parse_skill_md  # noqa: PLC0415

    try:
        meta, body = _parse_skill_md(md.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    # ``allowed-tools`` is the Claude-Code spelling; accept the underscore too
    raw_tools = meta.get("allowed-tools") or meta.get("allowed_tools") or ""
    tools = [t.strip() for t in re.split(r"[,\s]+", str(raw_tools)) if t.strip()]
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    return {
        "name": (meta.get("name") or md.parent.name).strip(),
        "slug": md.parent.name,
        "description": meta.get("description", ""),
        "category": meta.get("category"),
        "always_load": str(meta.get("always_load", "")).lower() in ("1", "true", "yes"),
        "tools": tools,
        "body": body,
        "raw": raw,
        "meta": {str(k): str(v) for k, v in meta.items()},
        "path": short_path(md),
    }


# ---------------------------------------------------------------------------
# Memory page — what the agent is told every session, and what it remembers.
#
# Two different things, the same split Claude Code makes:
#   * INSTRUCTIONS (project_memory.py) — human-written files loaded into
#     every session: ~/.mantis-agent/MANTIS.md (you, every project), the
#     repo's AGENTS.md / MANTIS.md / .mantis/rules/*.md (the team), and
#     MANTIS.local.md (you, this project only). The CLAUDE.md hierarchy.
#   * MEMORY (memory.py) — what the agent writes down itself:
#     ~/.mantis-agent/MEMORY.md (the index, loaded every session) and one
#     file per memory under ~/.mantis-agent/memory/.
# Writes are allowed ONLY to the slots listed here — never to a path the
# page sends — so a request can't be turned into "write any file".
# ---------------------------------------------------------------------------

_INSTR_ABOUT = {
    "user": "Your instructions for every project on this machine.",
    "agents": "Project instructions, checked into the repo and shared with your team. The cross-tool name — Claude Code and other agents read it too.",
    "mantis": "Project instructions for mantis specifically, checked into the repo.",
    "local": "Your private notes for this project only. Keep it out of git.",
    "rule": "An always-on rule for this project (a file in .mantis/rules/).",
    "managed": "Organization policy, set by an administrator. Read-only.",
    "other": "Picked up from a parent folder or pulled in by an @import.",
}


def _instr_slots(cwd: Path | None = None) -> list[dict[str, Any]]:
    import os  # noqa: PLC0415

    from . import paths  # noqa: PLC0415
    from .project_memory import (  # noqa: PLC0415
        AGENTS_FILE, LOCAL_FILE, MANAGED_PATH, PROJECT_FILE, RULES_SUBDIR, WORKSPACE_DIR, load_memory_files,
    )

    base = Path(cwd or os.getcwd()).resolve()
    try:
        loaded = {f.path.resolve(): f for f in load_memory_files(base)}
    except Exception:  # noqa: BLE001 — an unreadable file must not blank the page
        loaded = {}
    slots: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def add(sid: str, kind: str, tier: str, label: str, p: Path, writable: bool, always: bool) -> None:
        p = p.resolve() if p.exists() else p
        if p in seen:
            return
        seen.add(p)
        exists = p.is_file()
        if not exists and not always:
            return
        try:
            content = p.read_text(encoding="utf-8") if exists else ""
        except OSError:
            content = ""
        lf = loaded.get(p)
        slots.append({"id": sid, "kind": kind, "tier": tier, "label": label, "path": short_path(p), "_abs": str(p),
                      "exists": exists, "loaded": lf is not None, "writable": writable,
                      "imported_by": short_path(lf.parent) if (lf and lf.parent) else None,
                      "about": _INSTR_ABOUT.get(kind, ""), "content": content, "bytes": len(content.encode())})

    add("user", "user", "user", "Personal · MANTIS.md", paths.get_mantis_agent_dir() / PROJECT_FILE, True, True)
    add("agents", "agents", "project", AGENTS_FILE, base / AGENTS_FILE, True, True)
    add("mantis", "mantis", "project", PROJECT_FILE, base / PROJECT_FILE, True, True)
    add("local", "local", "local", LOCAL_FILE, base / LOCAL_FILE, True, True)
    rules = base / WORKSPACE_DIR / RULES_SUBDIR
    if rules.is_dir():
        for f in sorted(rules.glob("*.md")):
            add("rule:" + f.name, "rule", "project", "rules/" + f.name, f, True, False)
    add("managed", "managed", "managed", "Managed policy", MANAGED_PATH, False, False)
    # anything else the loader actually uses (a parent folder's AGENTS.md, an
    # @import) is shown read-only, so the page never hides what is in context
    for p, f in loaded.items():
        add("loaded:" + str(len(slots)), "other", f.tier, p.name, p, False, False)
    return slots


def memory_state() -> dict[str, Any]:
    import os  # noqa: PLC0415

    from . import memory as _mem  # noqa: PLC0415
    from . import paths  # noqa: PLC0415

    idx = paths.get_memory_index()
    entries = []
    for e in _mem.list_memory_entries():
        entries.append({"slug": e.slug, "name": e.name, "description": e.description, "type": e.type,
                        "body": e.body, "path": short_path(e.path) if e.path else None})
    content = _mem.load_memory_index()
    return {"cwd": short_path(Path(os.getcwd())),
            # the absolute path stays server-side: writes resolve it from the slot id
            "instructions": [{k: v for k, v in x.items() if k != "_abs"} for x in _instr_slots()],
            "index": {"path": short_path(idx), "exists": idx.is_file(), "content": content,
                      "lines": len([x for x in content.splitlines() if x.strip()])},
            "memory_dir": short_path(paths.get_memory_dir()),
            "entries": entries}


def save_instruction(sid: str | None, content: Any) -> dict[str, Any]:
    slot = next((s for s in _instr_slots() if s["id"] == sid), None)
    if slot is None:
        return {"ok": False, "error": "unknown file"}
    if not slot["writable"]:
        return {"ok": False, "error": f"{slot['label']} is read-only here"}
    target = Path(slot["_abs"])
    text = str(content or "")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n" if text.strip() else "", encoding="utf-8")
    return {"ok": True, "id": sid, "path": slot["path"], "bytes": len(text.encode())}


def save_memory_index(content: Any) -> dict[str, Any]:
    from . import memory as _mem  # noqa: PLC0415
    from . import paths  # noqa: PLC0415

    _mem.ensure_memory_dir()
    idx = paths.get_memory_index()
    text = str(content or "")
    idx.write_text(text.rstrip() + "\n" if text.strip() else "", encoding="utf-8")
    return {"ok": True, "path": short_path(idx)}


def _memory_path(slug: str) -> Path | None:
    from . import paths  # noqa: PLC0415

    base = paths.get_memory_dir().resolve()
    p = (base / f"{slug}.md").resolve()
    return p if base in p.parents else None


def save_memory(slug: str | None, name: Any, description: Any, mtype: Any, body: Any) -> dict[str, Any]:
    """Create or update one memory. A CREATE never lands on an existing file
    (the page autosaves), and gets its one-line pointer in MEMORY.md — the
    index is what the agent reads, so a memory missing from it is invisible."""
    from . import memory as _mem  # noqa: PLC0415
    from . import paths  # noqa: PLC0415

    name = str(name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    t = str(mtype or "project")
    t = t if t in ("user", "feedback", "project", "reference") else "project"
    create = not slug
    sl = str(slug) if slug else _slugify(name)
    p = _memory_path(sl)
    if p is None:
        return {"ok": False, "error": "bad memory name"}
    if create and p.exists():
        return {"ok": False, "exists": True, "slug": sl, "error": f"a memory called '{sl}' already exists"}
    if not create and not p.exists():
        return {"ok": False, "error": f"'{sl}' not found"}
    desc = " ".join(str(description or "").split())
    _mem.save_memory_entry(_mem.MemoryEntry(slug=sl, name=" ".join(name.split()), description=desc,
                                            type=t, body=str(body or "")))  # type: ignore[arg-type]
    if create:
        idx = paths.get_memory_index()
        cur = idx.read_text(encoding="utf-8") if idx.is_file() else ""
        line = f"- [{' '.join(name.split())}](memory/{sl}.md)" + (f" — {desc}" if desc else "")
        idx.write_text((cur.rstrip() + "\n" if cur.strip() else "") + line + "\n", encoding="utf-8")
    return {"ok": True, "slug": sl, "path": short_path(p)}


def delete_memory(slug: str | None) -> dict[str, Any]:
    from . import paths  # noqa: PLC0415

    p = _memory_path(str(slug or ""))
    if not slug or p is None or not p.is_file():
        return {"ok": False, "error": "memory not found"}
    p.unlink()
    idx = paths.get_memory_index()
    if idx.is_file():
        keep = [x for x in idx.read_text(encoding="utf-8").splitlines()
                if f"({slug}.md)" not in x and f"(memory/{slug}.md)" not in x]
        idx.write_text("\n".join(keep).rstrip() + "\n" if keep else "", encoding="utf-8")
    return {"ok": True}


def skills_state() -> dict[str, Any]:
    import os  # noqa: PLC0415

    g, p = _skills_dirs()

    def scan(d: Path) -> list[dict[str, Any]]:
        out = []
        if d.is_dir():
            for md in sorted(d.glob("*/SKILL.md")):
                s = _read_skill(md)
                if s:
                    out.append(s)
        return out

    gs, ps = scan(g), scan(p)
    for x in gs:
        x["scope"] = "global"
    for x in ps:
        x["scope"] = "project"
    everything = gs + ps
    return {"global": gs, "project": ps,
            "global_dir": short_path(g), "project_dir": short_path(p),
            "counts": {"total": len(everything),
                       "always": sum(1 for x in everything if x["always_load"]),
                       "on_demand": sum(1 for x in everything if not x["always_load"]),
                       "global": len(gs), "project": len(ps)},
            # every tool any skill declares — the editor offers these plus the built-ins
            "tools_seen": sorted({t for x in everything for t in x["tools"]}),
            "cwd": os.getcwd()}


def _slugify(name: str) -> str:
    import re  # noqa: PLC0415

    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-") or "skill"


def _skill_md(name: str, description: str, body: str,
              category: str = "", always_load: bool = False,
              tools: Any = None) -> str:
    """Render a SKILL.md. Front-matter keys are only written when set, so a
    round-trip through the editor doesn't sprout empty fields."""
    lines = [f"name: {name}", f"description: {(description or '').strip()}"]
    if (category or "").strip():
        lines.append(f"category: {category.strip()}")
    tool_list = [str(t).strip() for t in (tools or []) if str(t).strip()]
    if tool_list:
        lines.append("allowed-tools: " + ", ".join(tool_list))
    if always_load:
        lines.append("always_load: true")
    return "---\n" + "\n".join(lines) + "\n---\n\n" + (body or "").strip() + "\n"


def add_skill(scope: str, name: str, description: str, body: str,
              category: str = "", always_load: bool = False,
              slug: str | None = None, tools: Any = None) -> dict[str, Any]:
    """Create a skill — or overwrite one when ``slug`` names an existing skill
    (the editor's save path). Renaming keeps the original directory so links
    and the agent's own references stay valid."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    g, p = _skills_dirs()
    base = (p if scope == "project" else g).resolve()
    target_slug = _slugify(slug) if slug else _slugify(name)
    d = (base / target_slug).resolve()
    if d.parent != base:
        return {"ok": False, "error": "bad skill name"}
    if slug and not (d / "SKILL.md").exists():
        return {"ok": False, "error": f"'{slug}' not found in {scope}"}
    # A CREATE never lands on an existing skill: the page autosaves, and a
    # new draft titled like an old skill would silently replace its file.
    if not slug and (d / "SKILL.md").exists():
        return {"ok": False, "exists": True, "slug": target_slug,
                "error": f"a skill called '{target_slug}' already exists in {scope}"}
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        _skill_md(name, description, body, category, always_load, tools), encoding="utf-8")
    return {"ok": True, "scope": scope, "slug": target_slug, "path": short_path(d / "SKILL.md")}


def delete_skill(scope: str, slug: str) -> dict[str, Any]:
    import shutil  # noqa: PLC0415

    g, p = _skills_dirs()
    base = (p if scope == "project" else g).resolve()
    target = (base / (slug or "")).resolve()
    # Guard: only a direct child of the skills dir that actually holds a SKILL.md.
    if target.parent != base or not (target / "SKILL.md").exists():
        return {"ok": False, "error": "skill not found"}
    shutil.rmtree(target)
    return {"ok": True}


def _mcp_file(scope: str) -> Path:
    import os  # noqa: PLC0415

    from . import paths  # noqa: PLC0415

    if scope == "project":
        return Path(os.getcwd()) / ".mcp.json"
    return paths.get_mantis_agent_dir() / "mcp.json"


def _read_mcp(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    s = data.get("mcpServers") if isinstance(data, dict) else None
    return s if isinstance(s, dict) else {}


def _entry_summary(entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict):
        return {"transport": "?", "detail": ""}
    if entry.get("command"):
        args = " ".join(str(a) for a in entry.get("args") or [])
        return {"transport": "stdio", "detail": (str(entry["command"]) + (" " + args if args else "")).strip()}
    if entry.get("url"):
        t = str(entry.get("type", "")).lower()
        return {"transport": "sse" if t == "sse" else "http", "detail": str(entry["url"])}
    return {"transport": "?", "detail": ""}


def short_path(p: Any) -> str:
    """``/Users/me/.mantis-agent/mcp.json`` → ``~/.mantis-agent/mcp.json``.

    Absolute home paths are noise in a UI — and a screenshot of the dashboard
    shouldn't broadcast the account name either."""
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if home and s.startswith(home) else s


def _secret_fields(entry: Any) -> list[str]:
    """Which parts of an entry were masked, so the UI can label the reveal."""
    if not isinstance(entry, dict):
        return []
    out = []
    for key in ("env", "headers"):
        vals = entry.get(key)
        if isinstance(vals, dict) and any(str(v) for v in vals.values()):
            out.append(key)
    url = str(entry.get("url") or "")
    if url and redact_url_value(url) != url:
        out.append("url")
    return out


def redact_url_value(url: str) -> str:
    from .mcp.manager import redact_mcp_entry  # noqa: PLC0415

    return str(redact_mcp_entry({"url": url}).get("url") or url)


def mcp_state() -> dict[str, Any]:
    """Every configured MCP server with its FULL entry, credentials masked.

    The dashboard is an inspector, not just a list: it shows what each server
    actually runs (command/args/env keys, url/headers) and where that entry
    lives. Values that look like credentials never leave the process in the
    clear here — ``/api/mcp/entry`` serves the raw entry only when the editor
    explicitly asks for it."""
    import os  # noqa: PLC0415

    from . import paths  # noqa: PLC0415
    from .mcp.manager import (  # noqa: PLC0415
        project_mcp_is_trusted,
        redact_mcp_entry,
    )

    settings_servers: dict[str, Any] = {}
    try:
        from .settings import SETTING_SOURCES, load_settings  # noqa: PLC0415
        settings_servers = (load_settings(SETTING_SOURCES) or {}).get("mcpServers") or {}
    except Exception:  # noqa: BLE001
        pass
    gfile = paths.get_mantis_agent_dir() / "mcp.json"
    pfile = Path(os.getcwd()) / ".mcp.json"
    merged: dict[str, dict[str, Any]] = {}
    for scope, servers, path in (("settings", settings_servers, "settings.json"),
                                 ("global", _read_mcp(gfile), str(gfile)),
                                 ("project", _read_mcp(pfile), str(pfile))):
        for name, entry in (servers or {}).items():
            safe = redact_mcp_entry(entry) if isinstance(entry, dict) else {}
            merged[name] = {"name": name, "scope": scope, "path": path,
                            "display_path": short_path(path),
                            "entry": safe, "secrets": _secret_fields(entry),
                            "editable": scope in ("global", "project"),
                            **_entry_summary(safe)}
    servers_out = sorted(merged.values(), key=lambda s: str(s["name"]).lower())
    trusted = project_mcp_is_trusted()
    return {"servers": servers_out,
            "global_file": short_path(gfile), "project_file": short_path(pfile),
            "cwd": os.getcwd(),
            "project_exists": pfile.exists(), "project_trusted": trusted,
            # Project stdio servers stay withheld until the file is trusted —
            # surface that here so the page can offer the one-click fix.
            "withheld": [s["name"] for s in servers_out
                         if s["scope"] == "project" and s["transport"] == "stdio"
                         and not trusted]}


def mcp_entry_raw(name: str, scope: str) -> dict[str, Any]:
    """The unredacted entry for one editable server, for the JSON editor."""
    if scope not in ("global", "project"):
        return {"ok": False, "error": "only global/project entries are editable here"}
    servers = _read_mcp(_mcp_file(scope))
    entry = servers.get(name or "")
    if not isinstance(entry, dict):
        return {"ok": False, "error": f"'{name}' not found in {scope}"}
    return {"ok": True, "name": name, "scope": scope, "entry": entry}


# The server is threaded (a thread per request), so the read-modify-write of the
# shared MCP JSON file must be serialized or concurrent add/delete calls lose
# updates (the last writer clobbers the other's change).
_mcp_write_lock = threading.Lock()


def add_mcp(scope: str, name: str, entry: Any) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    if not (isinstance(entry, dict) and (entry.get("command") or entry.get("url"))):
        return {"ok": False, "error": "need a command (stdio) or url (http/sse)"}
    f = _mcp_file(scope)
    with _mcp_write_lock:
        data: dict[str, Any] = {}
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        servers[name] = entry
        data["mcpServers"] = servers
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "scope": scope, "name": name}


def add_mcp_paste(scope: str, text: str) -> dict[str, Any]:
    """Add server(s) from one pasted blob — the dashboard's version of the
    terminal's ``/mcp`` add field. Accepts a whole ``{"mcpServers": {...}}``
    document, a bare ``{name: entry}`` map, a single entry object, a shell
    command, or a URL. Unnamed input needs a name, which the UI supplies."""
    from .mcp.manager import parse_mcp_paste  # noqa: PLC0415

    servers, err = parse_mcp_paste(text or "")
    if err is not None:
        return {"ok": False, "error": err}
    if "" in servers:
        return {"ok": False, "error": "that config has no server name — add one",
                "needs_name": True}
    added = []
    for name, entry in servers.items():
        r = add_mcp(scope, name, entry)
        if not r.get("ok"):
            return r
        added.append(name)
    return {"ok": True, "added": added, "scope": scope}


def test_mcp(name: str) -> dict[str, Any]:
    """Actually connect to a configured server and report what it exposes.

    This is the question a config page can't answer by reading JSON — does this
    thing work? Runs one real handshake + ``tools/list`` with a short timeout in
    the request thread and tears the connection straight back down."""
    import time  # noqa: PLC0415

    import anyio  # noqa: PLC0415

    from .mcp.client import MCPClient  # noqa: PLC0415
    from .mcp.manager import load_mcp_server_configs  # noqa: PLC0415

    cfg = load_mcp_server_configs().get(name or "")
    if cfg is None:
        return {"ok": False, "error": f"'{name}' is not configured"}

    result: dict[str, Any] = {}

    async def go() -> None:
        client = MCPClient(cfg, server_id=name, request_timeout_s=12.0)
        t0 = time.monotonic()
        try:
            await client.__aenter__()
            tools = await client.list_tools()
            def params(t: Any) -> list[dict[str, Any]]:
                sch = getattr(t, "input_schema", None) or {}
                props = sch.get("properties") if isinstance(sch, dict) else None
                req = set(sch.get("required") or []) if isinstance(sch, dict) else set()
                if not isinstance(props, dict):
                    return []
                return [{"name": str(k), "type": str(v.get("type") or "") if isinstance(v, dict) else "",
                         "required": k in req} for k, v in list(props.items())[:12]]

            result.update(ok=True, tools=[{"name": t.name, "description": (t.description or "")[:400],
                                           "params": params(t)} for t in tools])
        except BaseException as e:  # noqa: BLE001 — a dead server must not 500
            msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            result.update(ok=False, error=msg)
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
            result["ms"] = int((time.monotonic() - t0) * 1000)

    try:
        anyio.run(go)
    except BaseException as e:  # noqa: BLE001
        result.setdefault("ok", False)
        result.setdefault("error", f"{type(e).__name__}: {e}")
    return {"name": name, **result}


def trust_project_mcp_file() -> dict[str, Any]:
    """Approve this project's .mcp.json so its stdio servers may spawn."""
    from .mcp.manager import trust_project_mcp  # noqa: PLC0415

    if not trust_project_mcp():
        return {"ok": False, "error": "no .mcp.json in this directory to trust"}
    return {"ok": True}


def delete_mcp(scope: str, name: str) -> dict[str, Any]:
    if scope not in ("global", "project"):
        return {"ok": False, "error": "only global/project entries are editable here"}
    f = _mcp_file(scope)
    with _mcp_write_lock:
        if not f.exists():
            return {"ok": False, "error": "no config file"}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            return {"ok": False, "error": "config file is not valid JSON"}
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict) or name not in servers:
            return {"ok": False, "error": f"'{name}' not found in {scope}"}
        del servers[name]
        data["mcpServers"] = servers
        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"ok": True}


def _projects_signature() -> tuple[int, float]:
    """(transcript count, newest mtime) across the whole projects tree — cheap
    stat-only fingerprint used to invalidate the analytics/projects caches when
    any transcript changes without re-reading their contents."""
    root = _projects_root()
    count = 0
    latest = 0.0
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.jsonl"):
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                count += 1
                if mtime > latest:
                    latest = mtime
    return (count, latest)


_analytics_cache: dict[str, Any] = {}
_analytics_lock = threading.Lock()


def analytics() -> dict[str, Any]:
    """Cached wrapper over :func:`_analytics_compute`, keyed on the projects-tree
    signature so repeated Home loads don't re-parse every transcript when nothing
    changed."""
    sig = _projects_signature()
    with _analytics_lock:
        if _analytics_cache.get("sig") == sig and "data" in _analytics_cache:
            return _analytics_cache["data"]
    data = _analytics_compute()
    with _analytics_lock:
        _analytics_cache["sig"] = sig
        _analytics_cache["data"] = data
    return data


def _analytics_compute() -> dict[str, Any]:
    """Scan every transcript across every project and roll up usage stats:
    message/tool volume over time, tool leaderboard, per-project breakdown, and
    when-you-work distributions. All local, all read-only."""
    from datetime import datetime  # noqa: PLC0415

    root = _projects_root()
    daily: dict[str, dict[str, int]] = {}        # 'YYYY-MM-DD' -> {msgs, tools}
    by_hour = [0] * 24
    by_weekday = [0] * 7
    punchcard = [[0] * 24 for _ in range(7)]     # weekday × hour
    tools: dict[str, int] = {}
    projects: dict[str, dict[str, Any]] = {}
    per_session: dict[str, dict[str, Any]] = {}   # session id (file stem) -> its own ledger
    total = user_m = asst_m = tool_calls = 0
    sessions = 0
    in_est_total = out_est_total = 0
    first_ts: float | None = None
    last_ts: float | None = None

    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            cwd = _project_cwd(d)
            pname = Path(cwd).name if cwd else d.name
            proj = projects.setdefault(d.name, {"name": pname, "cwd": cwd,
                                                "sessions": 0, "msgs": 0, "tools": 0,
                                                "in_est": 0, "out_est": 0})
            for f in d.glob("*.jsonl"):
                had_msg = False
                ctx_chars = 0   # what the model re-reads on every assistant turn
                sess = {"msgs": 0, "tools": 0, "in_est": 0, "out_est": 0, "first_ts": None, "last_ts": None}
                try:
                    with f.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            typ = obj.get("type")
                            content = (obj.get("message") or {}).get("content")
                            if typ == "system" and isinstance(content, dict) \
                                    and content.get("__compact_boundary__"):
                                ctx_chars = len(str(content.get("summary") or ""))
                                continue
                            if typ not in ("user", "assistant"):
                                continue
                            had_msg = True
                            total += 1
                            proj["msgs"] += 1
                            sess["msgs"] += 1
                            if typ == "user":
                                user_m += 1
                            else:
                                asst_m += 1
                            n_tools = 0
                            if isinstance(content, list):
                                for b in content:
                                    if isinstance(b, dict) and b.get("type") == "tool_use":
                                        nm = b.get("name") or "?"
                                        tools[nm] = tools.get(nm, 0) + 1
                                        n_tools += 1
                            tool_calls += n_tools
                            proj["tools"] += n_tools
                            sess["tools"] += n_tools
                            chars = _content_chars(content)
                            t_in = t_out = 0
                            if typ == "assistant":
                                t_in, t_out = _est_tokens(ctx_chars), _est_tokens(chars)
                                in_est_total += t_in
                                out_est_total += t_out
                                proj["in_est"] += t_in
                                proj["out_est"] += t_out
                                sess["in_est"] += t_in
                                sess["out_est"] += t_out
                            ctx_chars += chars
                            ts = obj.get("timestamp")
                            if ts:
                                try:
                                    dt = datetime.fromisoformat(
                                        ts.replace("Z", "+00:00")).astimezone()
                                except ValueError:
                                    continue
                                key = dt.strftime("%Y-%m-%d")
                                slot = daily.setdefault(key, {"msgs": 0, "tools": 0,
                                                              "in_est": 0, "out_est": 0})
                                slot["msgs"] += 1
                                slot["tools"] += n_tools
                                slot["in_est"] += t_in
                                slot["out_est"] += t_out
                                by_hour[dt.hour] += 1
                                by_weekday[dt.weekday()] += 1
                                punchcard[dt.weekday()][dt.hour] += 1
                                ep = dt.timestamp()
                                first_ts = ep if first_ts is None else min(first_ts, ep)
                                last_ts = ep if last_ts is None else max(last_ts, ep)
                                sess["first_ts"] = ep if sess["first_ts"] is None else min(sess["first_ts"], ep)
                                sess["last_ts"] = ep if sess["last_ts"] is None else max(sess["last_ts"], ep)
                except OSError:
                    continue
                if had_msg:
                    sessions += 1
                    proj["sessions"] += 1
                    per_session[f.stem] = sess

    top_tools = sorted(({"name": k, "count": v} for k, v in tools.items()),
                       key=lambda x: x["count"], reverse=True)[:10]
    tool_total = sum(tools.values())
    top_projects = sorted(
        (p for p in projects.values() if p["sessions"]),
        key=lambda p: p["msgs"], reverse=True)[:8]
    busiest = max(daily.items(), key=lambda kv: kv[1]["msgs"], default=(None, {"msgs": 0}))

    return {
        "totals": {
            "sessions": sessions,
            "projects": sum(1 for p in projects.values() if p["sessions"]),
            "messages": total,
            "user_messages": user_m,
            "assistant_messages": asst_m,
            "tool_calls": tool_calls,
            "unique_tools": len(tools),
            "tool_total": tool_total,
            "active_days": len(daily),
            "first_seen": first_ts,
            "last_seen": last_ts,
            "avg_msgs_per_session": round(total / sessions, 1) if sessions else 0,
            "busiest_day": busiest[0],
            "busiest_day_msgs": busiest[1]["msgs"],
            "in_est": in_est_total,
            "out_est": out_est_total,
        },
        "daily": daily,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "punchcard": punchcard,
        "top_tools": top_tools,
        "top_projects": top_projects,
        # every project's ledger by digest and every session's by id — what the
        # project and session CARDS read their pills from (one cached pass)
        "projects": {k: v for k, v in projects.items() if v["sessions"]},
        "sessions": per_session,
    }


def overview() -> dict[str, Any]:
    import os  # noqa: PLC0415

    projects = list_projects()
    m = models_state()
    try:
        sk = skills_state()
        skill_count = len(sk["global"]) + len(sk["project"])
    except Exception:  # noqa: BLE001 — the header must render regardless
        skill_count = 0
    try:
        mcp_count = len(mcp_state()["servers"])
    except Exception:  # noqa: BLE001
        mcp_count = 0
    try:
        grid = provider_grid()
        fam_ready = {f["id"]: bool(f["ready"]) for f in grid["families"]}
    except Exception:  # noqa: BLE001
        fam_ready = {}
    try:
        act = activity(limit=20)
        active_jobs, active_runs = act["active_jobs"], act["active_runs"]
        act_counts = act.get("counts_7d") or {}
    except Exception:  # noqa: BLE001
        active_jobs = active_runs = 0
        act_counts = {}
    try:
        sp = spend()
        spend_7 = sp["totals"]["7"]
    except Exception:  # noqa: BLE001
        spend_7 = {}
    return {
        "version": _version(),
        "home": str(_base_dir()),
        "cwd": os.getcwd(),
        "current": m["current"],
        "hosting": m.get("hosting") or {},
        "project_count": len(projects),
        "session_count": sum(p["session_count"] for p in projects),
        "enabled_providers": m["enabled_count"],
        "provider_count": len(m["providers"]),
        "skill_count": skill_count,
        "mcp_count": mcp_count,
        "families_ready": fam_ready,
        "family_ready_count": sum(1 for v in fam_ready.values() if v),
        "active_jobs": active_jobs,
        "active_runs": active_runs,
        # the rail badges what you are meant to act on; it is a real count or
        # it is nothing, never a placeholder
        "counts_7d": act_counts,
        "failed_7d": int(act_counts.get("error") or 0),
        "spend_7d": spend_7,
        "deployments_live": deployments_live_count(),
    }


# ---------------------------------------------------------------------------
# Provider families — the five kinds of thing mantis can talk to. Everything
# below iterates over whatever the catalog actually contains, so a family with
# no catalogued provider yet (or a provider added later) degrades to an empty
# or extra card rather than a crash.
# ---------------------------------------------------------------------------

FAMILIES: tuple[tuple[str, str, str], ...] = (
    # id, label, logo id (falls back to a letter tile when the mark is missing)
    ("openai", "OpenAI", "openai"),
    ("anthropic", "Claude", "anthropic"),
    ("google", "Gemini", "gemini"),
    ("xai", "Grok", "xai"),
    ("oss", "Open source", "ollama"),
)
_FAMILY_BY_PROVIDER = {"openai": "openai", "anthropic": "anthropic",
                       "gemini": "google", "google": "google", "xai": "xai", "grok": "xai"}


def family_of(provider_id: str | None) -> str:
    return _FAMILY_BY_PROVIDER.get((provider_id or "").lower(), "oss")


def _provider_id_for_model(model: str | None) -> str | None:
    """Which catalogued provider a model id belongs to. Uses the catalog's own
    heuristic first; ``grok-*`` maps to xAI once (and only once) the catalog
    knows that provider."""
    from . import catalog  # noqa: PLC0415

    if not model:
        return None
    try:
        p = catalog.provider_for_model(model)
    except Exception:  # noqa: BLE001
        p = None
    if p is not None:
        return p.id
    low = model.lower()
    if low.startswith(("grok-", "grok/", "x-ai/")) and "xai" in catalog.BY_ID:
        return "xai"
    return None


def _provider_id_for_backend(backend: str | None) -> str | None:
    """The catalogued provider whose base URL is ``backend`` — how a model the
    flagship lists don't mention (a live-listed id, a fine-tune) still gets
    attributed to the provider that served it."""
    from . import catalog  # noqa: PLC0415

    b = (backend or "").rstrip("/").lower()
    if not b:
        return None
    for p in catalog.CATALOG:
        if p.base_url.rstrip("/").lower() == b:
            return p.id
    return None


def _auth_state(prov: Any) -> dict[str, Any]:
    """saved / env / oauth / none — plus the masked hint. OAuth is the Anthropic
    ``ANTHROPIC_AUTH_TOKEN`` path (a Claude subscription or gateway bearer),
    which ``catalog.is_enabled`` honours but ``api_key_for`` never returns."""
    import os  # noqa: PLC0415

    host = _provider_hosting(prov)
    state = host.get("key_source") or "none"
    if state == "none" and prov.id == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        state = "oauth"
        host["key_masked"] = _mask_key(os.environ["ANTHROPIC_AUTH_TOKEN"])
    host["auth"] = state
    return host


_ollama_cache: dict[str, Any] = {}
_ollama_lock = threading.Lock()
OLLAMA_TTL_S = 10.0


def ollama_state(*, ttl_s: float = OLLAMA_TTL_S) -> dict[str, Any]:
    """Local Ollama: every pulled model with its size and whether it's loaded
    in memory right now (``/api/tags`` + ``/api/ps``). Short timeout, cached
    for a few seconds so an auto-refreshing page doesn't hammer the daemon."""
    import time  # noqa: PLC0415

    now = time.monotonic()
    with _ollama_lock:
        if _ollama_cache.get("at", -1e9) + ttl_s > now and "data" in _ollama_cache:
            return _ollama_cache["data"]
    data = _ollama_probe()
    with _ollama_lock:
        _ollama_cache["at"] = now
        _ollama_cache["data"] = data
    return data


def _ollama_probe() -> dict[str, Any]:
    from . import paths  # noqa: PLC0415

    base = paths.ollama_base_url()
    out: dict[str, Any] = {"base_url": base, "reachable": False, "models": [], "loaded_count": 0}
    try:
        import httpx  # noqa: PLC0415

        with httpx.Client(timeout=httpx.Timeout(1.5)) as c:
            tags = c.get(f"{base}/api/tags")
            if tags.status_code >= 400:
                out["error"] = f"HTTP {tags.status_code}"
                return out
            listed = (tags.json() or {}).get("models") or []
            loaded: dict[str, dict[str, Any]] = {}
            try:
                ps = c.get(f"{base}/api/ps")
                if ps.status_code < 400:
                    for m in (ps.json() or {}).get("models") or []:
                        loaded[str(m.get("name") or m.get("model"))] = m
            except Exception:  # noqa: BLE001 — /api/ps is newer than /api/tags
                pass
    except Exception as e:  # noqa: BLE001 — not running is the common case
        out["error"] = f"{type(e).__name__}"
        return out
    out["reachable"] = True
    models = []
    for m in listed:
        name = str(m.get("name") or m.get("model") or "")
        det = m.get("details") or {}
        lm = loaded.get(name)
        models.append({
            "name": name,
            "size": int(m.get("size") or 0),
            "param": det.get("parameter_size"),
            "quant": det.get("quantization_level"),
            "family": det.get("family"),
            "modified_at": m.get("modified_at"),
            "loaded": lm is not None,
            "vram": int(lm.get("size_vram") or 0) if lm else 0,
            "expires_at": lm.get("expires_at") if lm else None,
        })
    models.sort(key=lambda x: (not x["loaded"], x["name"]))
    out["models"] = models
    out["loaded_count"] = sum(1 for x in models if x["loaded"])
    return out


def provider_grid() -> dict[str, Any]:
    """The five families with their providers' auth state, the last model used
    per family, and whether the family is ready to run right now."""
    from . import catalog  # noqa: PLC0415

    try:
        recent = catalog.get_recent_models()
    except Exception:  # noqa: BLE001
        recent = []
    try:
        last = catalog.get_last_model() or {}
    except Exception:  # noqa: BLE001
        last = {}
    cur_model = last.get("model")
    backend_now = (last.get("backend") or "").rstrip("/")
    cur_pid = _provider_id_for_backend(backend_now) or _provider_id_for_model(cur_model)
    local_now = bool(backend_now) and ("localhost" in backend_now or "127.0.0.1" in backend_now)

    fams: dict[str, dict[str, Any]] = {}
    for fid, label, logo in FAMILIES:
        fams[fid] = {"id": fid, "label": label, "logo": logo, "providers": [],
                     "ready": False, "last_model": None, "is_current": False}
    for prov in catalog.CATALOG:
        fid = family_of(prov.id)
        fam = fams.setdefault(fid, {"id": fid, "label": fid, "logo": prov.id, "providers": [],
                                    "ready": False, "last_model": None, "is_current": False})
        host = _auth_state(prov)
        enabled = host["auth"] != "none"
        fam["providers"].append({
            "id": prov.id, "label": prov.label, "base_url": prov.base_url,
            "api_key_env": prov.api_key_env, "auth": host["auth"],
            "key_masked": host.get("key_masked"), "enabled": enabled,
            "is_current": prov.id == cur_pid and not local_now,
            "models": list(prov.models)[:4],
        })
        fam["ready"] = fam["ready"] or enabled
        fam["is_current"] = fam["is_current"] or (prov.id == cur_pid and not local_now)
    # Last-used model per family, from the recents list (newest first).
    for m in [cur_model, *recent]:
        if not m:
            continue
        fid = family_of(_provider_id_for_model(m))
        if fid in fams and fams[fid]["last_model"] is None:
            fams[fid]["last_model"] = m
    # The open-source family is also "ready" when a local runtime answers.
    oll = ollama_state()
    oss = fams.get("oss")
    if oss is not None:
        oss["local"] = {"reachable": oll.get("reachable"), "base_url": oll.get("base_url"),
                        "model_count": len(oll.get("models") or []),
                        "loaded_count": oll.get("loaded_count", 0)}
        oss["ready"] = oss["ready"] or bool(oll.get("reachable"))
        if local_now:
            oss["is_current"] = True
            oss["last_model"] = cur_model or oss["last_model"]
    ordered = [fams[f[0]] for f in FAMILIES if f[0] in fams] + \
              [v for k, v in fams.items() if k not in {f[0] for f in FAMILIES}]
    return {"families": ordered, "current": last,
            "ready_count": sum(1 for f in ordered if f["ready"])}


# ---------------------------------------------------------------------------
# Spend & usage — sessions (estimated from transcript size) plus workflow runs
# (recorded per agent), per day and per provider.
# ---------------------------------------------------------------------------


def _runs_signature() -> tuple[int, float]:
    from . import workflow_store  # noqa: PLC0415

    d = workflow_store.runs_dir()
    count, latest = 0, 0.0
    if d.is_dir():
        for f in d.glob("*.json"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            count += 1
            latest = max(latest, mt)
    return (count, latest)


_spend_cache: dict[str, Any] = {}
_spend_lock = threading.Lock()


def _workflow_usage_rows() -> list[dict[str, Any]]:
    """One row per agent run across every persisted workflow record:
    ``{ts, model, provider, in, out, usd, run_id}``. Runs record real usage, so
    these are the only *measured* numbers on the spend panel."""
    from . import workflow_store  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    try:
        runs = workflow_store.list_runs(limit=400)
    except Exception:  # noqa: BLE001
        return rows
    for r in runs:
        rec = workflow_store.load_record(r["run_id"]) if r.get("run_id") else None
        if not rec:
            continue
        run = rec.get("run") or {}
        saved = float(rec.get("saved_at") or 0.0)
        for ph in run.get("phases") or []:
            for a in ph.get("agents") or []:
                u = a.get("usage") or {}
                usd = a.get("cost_usd")
                if usd is None:
                    usd = u.get("costUSD")
                started = a.get("started") or run.get("started") or saved
                ts = float(started) if isinstance(started, (int, float)) and started > 1e9 else saved
                model = a.get("model") or ""
                rows.append({"ts": ts, "model": model, "provider": _provider_id_for_model(model),
                             "in": int(u.get("inputTokens") or 0), "out": int(u.get("outputTokens") or 0),
                             "usd": float(usd or 0.0), "run_id": rec.get("run_id"),
                             "status": a.get("status")})
    return rows


def spend() -> dict[str, Any]:
    sig = (_projects_signature(), _runs_signature())
    with _spend_lock:
        if _spend_cache.get("sig") == sig and "data" in _spend_cache:
            return _spend_cache["data"]
    data = _spend_compute()
    with _spend_lock:
        _spend_cache["sig"] = sig
        _spend_cache["data"] = data
    return data


def _spend_compute() -> dict[str, Any]:
    from datetime import datetime, timedelta  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    a = analytics()
    try:
        last = catalog.get_last_model() or {}
    except Exception:  # noqa: BLE001
        last = {}
    pricing = _session_pricing(last.get("model"), last.get("backend"))
    est_pid = pricing.get("provider")
    est_fid = family_of(est_pid) if est_pid else "oss"

    today = datetime.now().date()
    days: list[dict[str, Any]] = []
    by_day: dict[str, dict[str, Any]] = {}
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        slot = a["daily"].get(key) or {}
        row = {"date": key, "est_in": int(slot.get("in_est") or 0), "est_out": int(slot.get("out_est") or 0),
               "est_usd": None, "rec_in": 0, "rec_out": 0, "rec_usd": 0.0, "msgs": int(slot.get("msgs") or 0)}
        row["est_usd"] = _usd(pricing, row["est_in"], row["est_out"])
        days.append(row)
        by_day[key] = row
    wf_rows = _workflow_usage_rows()
    by_prov: dict[str, dict[str, Any]] = {}
    cutoff30 = (today - timedelta(days=29))
    for r in wf_rows:
        d = datetime.fromtimestamp(r["ts"]).date()
        key = d.strftime("%Y-%m-%d")
        if key in by_day:
            by_day[key]["rec_in"] += r["in"]
            by_day[key]["rec_out"] += r["out"]
            by_day[key]["rec_usd"] += r["usd"]
        if d >= cutoff30:
            pid = r["provider"] or "other"
            p = by_prov.setdefault(pid, {"id": pid, "family": family_of(r["provider"]),
                                         "label": (catalog.BY_ID[pid].label if pid in catalog.BY_ID
                                                   else ("self-hosted / other" if pid == "other" else pid)),
                                         "in": 0, "out": 0, "usd": 0.0, "runs": set(), "source": "recorded"})
            p["in"] += r["in"]
            p["out"] += r["out"]
            p["usd"] += r["usd"]
            p["runs"].add(r["run_id"])

    def window(n: int) -> dict[str, Any]:
        rows = days[-n:]
        est_in = sum(x["est_in"] for x in rows)
        est_out = sum(x["est_out"] for x in rows)
        return {"days": n, "est_in": est_in, "est_out": est_out, "est_tokens": est_in + est_out,
                "est_usd": _usd(pricing, est_in, est_out),
                "rec_in": sum(x["rec_in"] for x in rows), "rec_out": sum(x["rec_out"] for x in rows),
                "rec_usd": sum(x["rec_usd"] for x in rows),
                "msgs": sum(x["msgs"] for x in rows)}

    w30 = window(30)
    providers = []
    if w30["est_tokens"]:
        pid = est_pid or "local"
        providers.append({"id": pid, "family": est_fid,
                          "label": (catalog.BY_ID[pid].label if pid in catalog.BY_ID else
                                    ("Local / self-hosted" if pid in ("local", "ollama") else pid)),
                          "in": w30["est_in"], "out": w30["est_out"], "usd": w30["est_usd"],
                          "sessions": True, "source": "estimated"})
    for p in by_prov.values():
        p["runs"] = len(p["runs"])
        providers.append(p)
    providers.sort(key=lambda p: ((p["usd"] or 0), p["in"] + p["out"]), reverse=True)
    fam_tot: dict[str, dict[str, Any]] = {}
    for p in providers:
        f = fam_tot.setdefault(p["family"], {"id": p["family"], "in": 0, "out": 0, "usd": 0.0, "priced": True})
        f["in"] += p["in"]
        f["out"] += p["out"]
        if p["usd"] is None:
            f["priced"] = False
        else:
            f["usd"] += p["usd"]
    fam_order = [f[0] for f in FAMILIES]
    return {
        "pricing": pricing,
        "days": days,
        "totals": {"7": window(7), "30": window(30)},
        "by_provider": providers,
        "by_family": sorted(fam_tot.values(), key=lambda f: fam_order.index(f["id"]) if f["id"] in fam_order else 99),
        "all_time": {"est_in": a["totals"].get("in_est", 0), "est_out": a["totals"].get("out_est", 0)},
        "note": "session tokens are estimated from transcript size (≈4 chars/token) and priced at "
                "the current model's rate; workflow runs are recorded by the provider.",
    }


# ---------------------------------------------------------------------------
# Live activity — background jobs and workflow runs.
# ---------------------------------------------------------------------------


def _job_dict(rec: Any) -> dict[str, Any]:
    import msgspec  # noqa: PLC0415

    d = msgspec.to_builtins(rec)
    d.pop("spawn_spec", None)              # redacted on disk already; not needed by the page
    d["elapsed_s"] = round(float(rec.elapsed_s), 1)
    d["terminal"] = bool(rec.is_terminal)
    d["desc"] = _redact_text(str(d.get("desc") or ""))[:240]
    d["error"] = _redact_text(str(d.get("error") or ""))[:600]
    d["cwd"] = short_path(d.get("cwd") or "") if d.get("cwd") else ""
    return d


def _run_usage(rec: dict[str, Any]) -> dict[str, Any]:
    run = rec.get("run") or {}
    t_in = t_out = 0
    usd = 0.0
    agents = done = 0
    models: dict[str, int] = {}
    for ph in run.get("phases") or []:
        for a in ph.get("agents") or []:
            agents += 1
            if a.get("status") in ("done", "completed", "ok"):
                done += 1
            u = a.get("usage") or {}
            t_in += int(u.get("inputTokens") or 0)
            t_out += int(u.get("outputTokens") or 0)
            c = a.get("cost_usd")
            usd += float(c if c is not None else (u.get("costUSD") or 0.0))
            m = a.get("model") or ""
            if m:
                models[m] = models.get(m, 0) + 1
    started, ended = run.get("started"), run.get("ended")
    elapsed = None
    if isinstance(started, (int, float)):
        import time  # noqa: PLC0415

        end = ended if isinstance(ended, (int, float)) else time.time()
        elapsed = max(0.0, float(end) - float(started))
    return {"in": t_in, "out": t_out, "tokens": t_in + t_out, "usd": usd, "agents": agents,
            "agents_done": done, "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "models": sorted(models, key=models.get, reverse=True)[:3], "started": started, "ended": ended}


def activity(limit: int = 40) -> dict[str, Any]:
    from . import job_records, workflow_store  # noqa: PLC0415

    jobs: list[dict[str, Any]] = []
    try:
        for rec in job_records.list_job_records(limit=limit):
            try:
                jobs.append(_job_dict(rec))
            except Exception:  # noqa: BLE001 — one odd record must not blank the panel
                continue
    except Exception:  # noqa: BLE001
        pass
    runs: list[dict[str, Any]] = []
    try:
        for r in workflow_store.list_runs(limit=limit):
            rec = workflow_store.load_record(r["run_id"]) if r.get("run_id") else None
            usage = _run_usage(rec) if rec else {}
            runs.append({**{k: v for k, v in r.items() if k != "path"},
                         "name": _redact_text(str(r.get("name") or "")),
                         "usage": usage,
                         "active": str(r.get("status") or "") in ("running", "queued", "paused")})
    except Exception:  # noqa: BLE001
        pass
    active_jobs = sum(1 for j in jobs if not j["terminal"])
    # a 7-day roll-up for the overview's summary card: running · done · error
    import time  # noqa: PLC0415

    cutoff = time.time() - 7 * 86400
    counts = {"running": 0, "done": 0, "error": 0}
    for j in jobs:
        ts = j.get("ended_at") or j.get("started_at") or j.get("created_at") or 0
        if not j["terminal"]:
            counts["running"] += 1
        elif ts and ts >= cutoff:
            counts["error" if str(j.get("status")) in ("error", "failed", "timeout", "cancelled", "canceled") else "done"] += 1
    for r in runs:
        if r["active"]:
            counts["running"] += 1
        elif (r.get("saved_at") or 0) >= cutoff:
            counts["error" if str(r.get("status")) in ("error", "failed", "timeout", "cancelled", "canceled") else "done"] += 1
    return {"jobs": jobs, "runs": runs, "active_jobs": active_jobs,
            "active_runs": sum(1 for r in runs if r["active"]),
            "counts_7d": counts,
            "jobs_dir": short_path(job_records.jobs_dir()),
            "runs_dir": short_path(workflow_store.runs_dir())}


def workflow_detail(run_id: str | None) -> dict[str, Any]:
    """One persisted run, phases and agents included. Inputs were redacted on
    write; free text (results, errors, log lines) is masked here and bounded so
    a chatty run can't ship megabytes to the page."""
    from . import workflow_store  # noqa: PLC0415

    rec = workflow_store.load_record(run_id or "") if run_id else None
    if not rec:
        return {"ok": False, "error": f"run {run_id!r} not found"}
    run = dict(rec.get("run") or {})
    phases = []
    for ph in run.get("phases") or []:
        agents = []
        for a in ph.get("agents") or []:
            a = dict(a)
            for k in ("summary", "result", "error", "label"):
                if a.get(k):
                    a[k] = _redact_text(str(a[k]))[:4000]
            a["recent_activities"] = [_redact_text(str(x))[:200] for x in (a.get("recent_activities") or [])[-8:]]
            agents.append(a)
        phases.append({**ph, "agents": agents})
    run["phases"] = phases
    run["log_lines"] = [_redact_text(str(x))[:400] for x in (run.get("log_lines") or [])[-300:]]
    return {"ok": True, "run_id": rec.get("run_id"), "definition": rec.get("definition"),
            "status": rec.get("status") or run.get("status"), "saved_at": rec.get("saved_at"),
            "job_id": rec.get("job_id"), "inputs": rec.get("inputs") or {},
            "summary": rec.get("summary") or {}, "version": rec.get("version"),
            "usage": _run_usage(rec), "run": run,
            "path": short_path(workflow_store.run_path(str(rec.get("run_id") or run_id)))}


# ---------------------------------------------------------------------------
# Deploy — bring-your-own GPU provider. The dashboard is the hub: add a
# provider credential once, search any open model, see which GPUs fit and what
# they cost, deploy, watch it come up, then "Use this model" so the SDK and the
# terminal point at it. Everything below is a thin, sync, JSON-shaped skin over
# ``mantis_agent.deploy.manager`` (the async contract). Long operations —
# deploy, teardown — run in a background thread as a *job* the page polls.
# Nothing here ever echoes a credential value.
# ---------------------------------------------------------------------------

_DEPLOY_SECRET_KEYS = frozenset({
    "hf_token", "token", "secret", "password", "api_key", "apikey", "key", "value", "values",
})
_ENV_REF_RE = re.compile(r"^\$\{?[A-Z0-9_]+\}?$")


def _deploy_redact(obj: Any, key: str | None = None) -> Any:
    """Deploy-shaped redaction. Unlike :func:`_redact_content` it leaves env
    var *names* (``auth_env``, ``api_key_env``, ``credential_fields[].env``)
    readable — those are the labels the page needs — while masking every
    value under a secret-shaped key, every header value that isn't an env
    reference, and every key-shaped substring in free text."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks in ("auth_headers", "headers") and isinstance(v, dict):
                out[ks] = {hk: (hv if isinstance(hv, str) and _ENV_REF_RE.match(hv)
                                else _mask_key(str(hv))) for hk, hv in v.items()}
            elif ks.lower() in _DEPLOY_SECRET_KEYS and isinstance(v, str):
                out[ks] = _mask_key(v)
            elif ks.lower() in _DEPLOY_SECRET_KEYS and isinstance(v, dict):
                out[ks] = {vk: _mask_key(str(vv)) if vv else None for vk, vv in v.items()}
            else:
                out[ks] = _deploy_redact(v, ks)
        return out
    if isinstance(obj, list):
        return [_deploy_redact(x, key) for x in obj]
    if isinstance(obj, str) and key in ("message", "error", "hint", "lines", "reason", "logs", "detail"):
        return _redact_text(obj)
    return obj


def _dc(obj: Any) -> Any:
    """Dataclass / datetime / tuple → JSON-able, recursively. ``Deployment.raw``
    is dropped: it is the provider's own object and may carry anything."""
    import dataclasses  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {}
        for f in dataclasses.fields(obj):
            if f.name == "raw":
                continue
            out[f.name] = _dc(getattr(obj, f.name))
        return out
    if isinstance(obj, datetime):
        return obj.timestamp()
    if isinstance(obj, dict):
        return {str(k): _dc(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_dc(x) for x in obj]
    return obj


def _gpu_dict(g: Any) -> dict[str, Any]:
    d = _dc(g)
    d["display"] = getattr(g, "display", None) or d.get("label") or d.get("provider_id")
    d["total_vram_gb"] = getattr(g, "total_vram_gb", None) or (
        int(d.get("vram_gb") or 0) * int(d.get("count") or 1))
    return d


def _dep_dict(dep: Any) -> dict[str, Any]:
    d = _dc(dep)
    d["gpu"] = _gpu_dict(dep.gpu) if getattr(dep, "gpu", None) is not None else None
    d["is_live"] = bool(getattr(dep, "is_live", False))
    return d


def _run_async(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one coroutine function to completion on this (request) thread.
    ``anyio.run`` is what the CLI uses for the same calls."""
    import anyio  # noqa: PLC0415

    return anyio.run(lambda: fn(*args, **kwargs))


def _deploy_err(e: BaseException) -> dict[str, Any]:
    """A provider failure is an answer, not a 500. ``hint`` is user-facing."""
    if isinstance(e, NotImplementedError):
        return {"ok": False, "error": "deploy core isn't available in this build yet",
                "hint": "update mantis-agent-sdk"}
    d: dict[str, Any] = {"ok": False, "error": _redact_text(str(e) or type(e).__name__),
                         "kind": type(e).__name__}
    hint = getattr(e, "hint", None)
    if hint:
        d["hint"] = _redact_text(str(hint))
    prov = getattr(e, "provider", None)
    if prov:
        d["provider"] = prov
    return d


# What the last validate/save learned per provider — so the strip can show
# "validated · $12.40 balance" without a network call on every refresh.
_deploy_accounts: dict[str, dict[str, Any]] = {}
_deploy_lock = threading.Lock()


_GUIDE_KEYS = ("name", "intro", "steps", "keys_url", "key_hint", "free_note", "cost_note", "pricing_url", "docs_url", "env_var")


def _deploy_guide(pid: str) -> dict[str, Any] | None:
    """The how-to-get-a-key guide for a deploy provider, from
    ``provider_guides`` — normalised to the keys the form renders, ``None``
    when the module has no entry yet."""
    try:
        from . import provider_guides  # noqa: PLC0415

        # Deploy providers live in their own table (``deploy_guide``); the
        # general ``guide_for`` table is the fallback for ids shared with the
        # inference catalog (e.g. ``hf``).
        g = provider_guides.deploy_guide(pid) or provider_guides.guide_for(pid)
    except Exception:  # noqa: BLE001
        g = None
    if not isinstance(g, dict):
        return None
    out = {k: g.get(k) for k in _GUIDE_KEYS if g.get(k) is not None}
    if "key_hint" not in out and g.get("key_shape"):
        out["key_hint"] = g["key_shape"]
    if "steps" in out and not isinstance(out["steps"], list):
        out["steps"] = [str(out["steps"])]
    return out or None


def deploy_providers() -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415
    from .serve_logos import PROVIDER_LOGOS  # noqa: PLC0415

    try:
        provs = _run_async(_dm.providers)
    except Exception as e:  # noqa: BLE001
        return {**_deploy_err(e), "providers": []}
    out = []
    for p in provs:
        d = _dc(dict(p))
        pid = str(d.get("id") or "")
        d["logo"] = pid if pid in PROVIDER_LOGOS else None
        d["guide"] = _deploy_guide(pid)
        with _deploy_lock:
            d["account"] = _deploy_accounts.get(pid)
        out.append(d)
    return {"ok": True, "providers": _deploy_redact(out),
            "configured_count": sum(1 for p in out if p.get("configured"))}


def deploy_save_creds(provider: str | None, values: Any) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    provider = (provider or "").strip()
    if not provider:
        return {"ok": False, "error": "provider required"}
    if not isinstance(values, dict) or not values:
        return {"ok": False, "error": "values required — {ENV_VAR: value}"}
    clean = {str(k).strip(): str(v).strip() for k, v in values.items() if str(v).strip()}
    if not clean:
        return {"ok": False, "error": "no credential values given"}
    try:
        acct = _run_async(_dm.save_credentials, provider, clean)
    except Exception as e:  # noqa: BLE001
        return _deploy_err(e)
    ad = _dc(acct)
    with _deploy_lock:
        _deploy_accounts[provider] = ad
        if "HF_TOKEN" in clean:
            _inspect_cache.clear()      # gated verdicts were computed without it
    # Only NAMES go back — never the value, not even masked (it was just typed).
    return _deploy_redact({"ok": True, "provider": provider, "saved": sorted(clean), "account": ad})


def deploy_validate(provider: str | None) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    provider = (provider or "").strip()
    if not provider:
        return {"ok": False, "error": "provider required"}
    try:
        acct = _run_async(_dm.validate, provider)
    except Exception as e:  # noqa: BLE001
        d = _deploy_err(e)
        with _deploy_lock:
            _deploy_accounts[provider] = {"ok": False, "provider": provider, "message": d["error"]}
        return d
    ad = _dc(acct)
    with _deploy_lock:
        _deploy_accounts[provider] = ad
    return _deploy_redact({"ok": bool(getattr(acct, "ok", False)), "provider": provider, "account": ad})


def deploy_gpus(provider: str | None, min_vram: Any = None) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    provider = (provider or "").strip()
    if not provider:
        return {"ok": False, "error": "provider required", "gpus": []}
    try:
        mv = int(min_vram) if min_vram not in (None, "") else None
    except ValueError:
        mv = None
    try:
        gpus = _run_async(_dm.gpus, provider, min_vram_gb=mv)
    except Exception as e:  # noqa: BLE001
        return {**_deploy_err(e), "gpus": []}
    rows = [_gpu_dict(g) for g in gpus]
    # cheapest first; unpriced rows sink to the bottom rather than sorting as $0
    rows.sort(key=lambda r: (r.get("price_per_hour") is None, r.get("price_per_hour") or 0.0,
                             r.get("total_vram_gb") or 0))
    return {"ok": True, "provider": provider, "gpus": rows}


def deploy_models(query: str = "", sort: str = "trending", limit: Any = 25,
                  source: str | None = None) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    try:
        lim = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        lim = 25
    sort = (sort or "trending").strip().lower()
    # "recent" is what the page calls it; the Hub layer's key is "updated"
    sort = {"recent": "updated", "lastmodified": "updated", "new": "updated"}.get(sort, sort)
    if sort not in ("trending", "downloads", "likes", "updated", "created"):
        sort = "trending"
    src = (source or "auto").strip().lower()
    if src not in ("curated", "hub", "auto"):
        src = "auto"
    kw = {"source": src} if src != "auto" and _accepts_kw(_dm.search_models, "source") else {}
    try:
        models = _run_async(_dm.search_models, (query or "").strip(), limit=lim, sort=sort, **kw)
    except Exception as e:  # noqa: BLE001
        return {**_deploy_err(e), "models": [], "query": query or "", "sort": sort}
    rows, missing = [], []
    for m in models:
        d = _deploy_redact(_dc(m))
        d["org"] = _org_of(d.get("id"))
        cached = _model_info_get(d.get("id") or "")
        if cached and not cached.get("error"):
            d = _merge_info(d, cached)
        elif _needs_info(d) and not (cached and cached.get("error")):
            missing.append(d["id"])
        rows.append(d)
    if missing:
        enrich_models(missing)
    return {"ok": True, "query": query or "", "sort": sort,
            "curated": not (query or "").strip() and src != "hub",
            "models": rows, "partial": bool(missing), "pending": missing,
            "hf_token_set": hf_token_set()}


# ---- progressive enrichment: search results are bare ids until inspect_model
# has looked each one up. Results are cached on disk for a day so the second
# paint is complete; a bounded pool of worker threads fills the gaps.
_INFO_FIELDS = ("architectures", "params_b", "dtype", "gated", "gated_kind", "license", "downloads", "likes",
                "context_len", "vllm_ok", "est_vram_gb", "tags", "reason")
# how current a model is — the Hub knows, but ModelInfo has no field for it,
# so the enrichment pass reads it from the Hub's own model record
_DATE_FIELDS = ("last_modified", "created_at")
MODEL_INFO_TTL_S = 24 * 3600
MODEL_INFO_ERR_TTL_S = 3600
ENRICH_WORKERS = 4
_model_info_cache: dict[str, dict[str, Any]] = {}
_model_info_loaded = False
_enrich_pending: set[str] = set()
_enrich_sem = threading.Semaphore(ENRICH_WORKERS)


def _cache_dir() -> Path:
    return _base_dir() / "cache"


def _model_info_path() -> Path:
    return _cache_dir() / "model-info.json"


def hf_token_set() -> bool:
    """Is a Hugging Face token configured? Gating is decided against this: a
    gated repo is deployable the moment one is present. Same env the
    preflight check and every adapter read (``HF_TOKEN``)."""
    import os  # noqa: PLC0415

    if (os.environ.get("HF_TOKEN") or "").strip():
        return True
    try:
        from .settings import SETTING_SOURCES, load_settings  # noqa: PLC0415

        env = (load_settings(SETTING_SOURCES) or {}).get("env") or {}
        return bool(str(env.get("HF_TOKEN") or "").strip())
    except Exception:  # noqa: BLE001
        return False


def _org_of(model_id: Any) -> str | None:
    mid = str(model_id or "")
    return mid.split("/", 1)[0].lower() if "/" in mid else None


def _needs_info(d: dict[str, Any]) -> bool:
    return (d.get("params_b") is None or d.get("vllm_ok") is None
            or d.get("est_vram_gb") is None or not d.get("last_modified"))


def _merge_info(d: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    for k in _DATE_FIELDS:
        if info.get(k):
            out[k] = info[k]
    for k in _INFO_FIELDS:
        v = info.get(k)
        if v is not None and (out.get(k) is None or k in ("vllm_ok", "est_vram_gb", "reason")):
            out[k] = v
    return out


def _model_info_load() -> None:
    global _model_info_loaded  # noqa: PLW0603
    if _model_info_loaded:
        return
    _model_info_loaded = True
    try:
        data = json.loads(_model_info_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _model_info_cache.update({str(k): v for k, v in data.items() if isinstance(v, dict)})
    except (OSError, ValueError):
        pass


def _model_info_save() -> None:
    try:
        p = _model_info_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(_model_info_cache), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _model_info_get(model_id: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Cached facts for one id, or ``None`` when absent or past its TTL."""
    import time  # noqa: PLC0415

    with _deploy_lock:
        _model_info_load()
        entry = _model_info_cache.get(model_id)
    if not entry:
        return None
    ttl = MODEL_INFO_ERR_TTL_S if entry.get("error") else MODEL_INFO_TTL_S
    if (now or time.time()) - float(entry.get("at") or 0) > ttl:
        return None
    return entry


def _hub_dates(model_id: str) -> dict[str, Any]:
    """``lastModified`` / ``createdAt`` from the Hub's model record. Best
    effort: a card without a date just shows no date."""
    from .deploy import hf_hub  # noqa: PLC0415

    try:
        raw = _run_async(hf_hub.model_info, model_id)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Any] = {}
    for src, dst in (("lastModified", "last_modified"), ("createdAt", "created_at")):
        v = (raw or {}).get(src)
        if v:
            out[dst] = str(v)
    return out


def _thin(entry: dict[str, Any]) -> bool:
    """Did the lookup come back without the facts the card needs?"""
    return entry.get("vllm_ok") is None or entry.get("params_b") is None or entry.get("est_vram_gb") is None


def _enrich_one(model_id: str) -> None:
    import time  # noqa: PLC0415

    from .deploy import manager as _dm  # noqa: PLC0415

    with _enrich_sem:
        entry: dict[str, Any] = {}
        # A thin first answer is usually a slow config fetch, not a missing
        # one — so try exactly once more before leaving the card with gaps.
        for attempt in (1, 2):
            try:
                info = _run_async(_dm.inspect_model, model_id)
                entry = {k: v for k, v in _dc(info).items() if k in _INFO_FIELDS}
                if not _thin(entry):
                    break
                if attempt == 1:
                    entry["retried"] = True
                    time.sleep(0.8)
            except Exception as e:  # noqa: BLE001 — remembered briefly so a bad id isn't re-asked every paint
                entry = {"error": _redact_text(str(e) or type(e).__name__)[:200]}
                break
        if not entry.get("error"):
            entry.update(_hub_dates(model_id))
        entry["at"] = time.time()
        with _deploy_lock:
            _model_info_cache[model_id] = entry
            _enrich_pending.discard(model_id)
            _model_info_save()


def enrich_models(ids: list[str]) -> list[str]:
    """Queue background lookups for ids without fresh cached info. Returns
    the ids now pending (already-queued ones included)."""
    started = []
    with _deploy_lock:
        for mid in ids:
            mid = str(mid or "").strip()
            if not mid or mid in _enrich_pending:
                continue
            _enrich_pending.add(mid)
            started.append(mid)
    for mid in started:
        threading.Thread(target=_enrich_one, args=(mid,), name="mantis-enrich", daemon=True).start()
    with _deploy_lock:
        return sorted(_enrich_pending)


def deploy_models_enrich(ids: Any) -> dict[str, Any]:
    """What the background lookups have found so far for these ids."""
    wanted = [x.strip() for x in str(ids or "").split(",") if x.strip()][:100]
    found: dict[str, Any] = {}
    pending: list[str] = []
    for mid in wanted:
        entry = _model_info_get(mid)
        if entry and not entry.get("error"):
            found[mid] = _deploy_redact({k: v for k, v in entry.items() if k not in ("at", "retried")})
        elif entry and entry.get("error"):
            found[mid] = {"error": entry["error"]}
        else:
            pending.append(mid)
    if pending:
        pending = [m for m in enrich_models(pending) if m in pending]
    return {"ok": True, "models": found, "pending": pending}


def warm_curated_models() -> None:
    """At server start: enqueue lookups for the curated list so the first
    paint of the Deploy page is usually complete. Best-effort, in a thread."""
    def go() -> None:
        try:
            from .deploy import manager as _dm  # noqa: PLC0415

            models = _run_async(_dm.search_models, "", limit=40, sort="trending")
            enrich_models([m.id for m in models if _needs_info(_dc(m)) and not _model_info_get(m.id)])
        except Exception:  # noqa: BLE001 — no deploy core, no network: nothing to warm
            pass
    threading.Thread(target=go, name="mantis-warm-curated", daemon=True).start()


# ---- org avatars: a same-origin, disk-cached proxy for the Hub's org/user
# avatar so model cards can show a mark without the page ever leaving the
# machine. 30-day TTL; a miss is remembered for an hour; the org id is
# validated so the cache path can't escape its directory.
ORG_AVATAR_TTL_S = 30 * 86400
ORG_AVATAR_MISS_TTL_S = 3600
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def _avatar_dir() -> Path:
    return _cache_dir() / "org-avatars"


def _fetch_org_avatar(org: str) -> tuple[bytes, str] | None:
    """(bytes, content-type) from the Hub, or ``None``. Overridable in tests."""
    import httpx  # noqa: PLC0415

    with httpx.Client(timeout=httpx.Timeout(connect=4.0, read=8.0, write=4.0, pool=4.0), follow_redirects=True) as c:
        url = None
        for kind in ("organizations", "users"):
            try:
                r = c.get(f"https://huggingface.co/api/{kind}/{org}/overview")
            except Exception:  # noqa: BLE001
                continue
            if r.status_code == 200:
                try:
                    url = (r.json() or {}).get("avatarUrl")
                except ValueError:
                    url = None
                if url:
                    break
        if not url:
            return None
        if url.startswith("/"):
            url = "https://huggingface.co" + url
        img = c.get(url)
        if img.status_code != 200 or not img.content:
            return None
        ctype = (img.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            return None
        return img.content[:2_000_000], ctype


def org_avatar(org: str | None) -> tuple[int, bytes, str]:
    """(status, body, content-type): 200 with the image, 204 when the Hub
    has none (or is unreachable), 400 for an id that isn't an org id."""
    import time  # noqa: PLC0415

    org = (org or "").strip()
    if not _ORG_ID_RE.match(org) or ".." in org:
        return 400, b"bad org id", "text/plain; charset=utf-8"
    key = org.lower()
    d = _avatar_dir()
    meta_p = d / f"{key}.json"
    now = time.time()
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        age = now - float(meta.get("at") or 0)
        if meta.get("miss") and age < ORG_AVATAR_MISS_TTL_S:
            return 204, b"", "application/octet-stream"
        if not meta.get("miss") and age < ORG_AVATAR_TTL_S:
            body = (d / meta["file"]).read_bytes()
            return 200, body, meta.get("ctype") or "image/png"
    except (OSError, ValueError, KeyError):
        pass
    try:
        got = _fetch_org_avatar(org)
    except Exception:  # noqa: BLE001
        got = None
    try:
        d.mkdir(parents=True, exist_ok=True)
        if got is None:
            meta_p.write_text(json.dumps({"miss": True, "at": now}), encoding="utf-8")
            return 204, b"", "application/octet-stream"
        body, ctype = got
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/svg+xml": "svg", "image/gif": "gif"}.get(ctype, "img")
        fname = f"{key}.{ext}"
        (d / fname).write_bytes(body)
        meta_p.write_text(json.dumps({"file": fname, "ctype": ctype, "at": now}), encoding="utf-8")
    except OSError:
        if got is None:
            return 204, b"", "application/octet-stream"
        body, ctype = got
    return 200, body, ctype


_inspect_cache: dict[str, tuple[float, dict[str, Any]]] = {}
INSPECT_TTL_S = 60.0


def _verdict_parts(v: Any) -> tuple[str, str]:
    """``"fits"`` / ``"tight"`` / ``"no: needs 48 GB"`` → (kind, reason)."""
    s = str(v or "").strip()
    head = re.split(r"[\s:—–-]+", s, maxsplit=1)
    kind = (head[0] or "").lower()
    reason = head[1].strip() if len(head) > 1 else ""
    if kind not in ("fits", "tight", "no"):
        kind, reason = "no", s
    return kind, reason


def _recommend_gpu(fits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one card a one-click deploy should use: across every configured
    provider, the cheapest GPU that fits outright — skipping any the provider
    says are out of stock — falling back to the cheapest ``tight`` fit. Ties
    go to more VRAM. ``None`` when nothing on offer can hold the model."""
    def pick(kind: str) -> dict[str, Any] | None:
        best: tuple[float, float] | None = None
        out: dict[str, Any] | None = None
        for entry in fits:
            for g in entry.get("gpus") or []:
                if g.get("verdict") != kind or g.get("available") is False:
                    continue
                # vLLM shards a model across 1, 2, 4 or 8 cards — a T4 ×3 is a
                # price in a catalogue, not something it can actually serve on
                if int(g.get("count") or 1) not in (1, 2, 4, 8):
                    continue
                price = g.get("price_per_hour")
                key = (float(price) if price is not None else float("inf"),
                       -float(g.get("total_vram_gb") or g.get("vram_gb") or 0))
                if best is None or key < best:
                    best = key
                    out = {"provider": entry.get("provider"), "gpu": g.get("provider_id"),
                           "verdict": kind, "price_per_hour": price}
        return out
    return pick("fits") or pick("tight")


def deploy_inspect(model: str | None, *, ttl_s: float = INSPECT_TTL_S) -> dict[str, Any]:
    """Pre-flight facts for one model plus, per configured provider, which
    of its GPUs fit. Cached for a minute: the page re-asks on every click."""
    import time  # noqa: PLC0415

    from .deploy import manager as _dm  # noqa: PLC0415

    model = (model or "").strip()
    if not model:
        return {"ok": False, "error": "model required"}
    now = time.monotonic()
    with _deploy_lock:
        hit = _inspect_cache.get(model)
        if hit and hit[0] + ttl_s > now:
            return hit[1]
    try:
        info = _run_async(_dm.inspect_model, model)
    except Exception as e:  # noqa: BLE001
        return {**_deploy_err(e), "model": model}
    try:
        provs = [dict(p) for p in _run_async(_dm.providers) if p.get("configured")]
    except Exception:  # noqa: BLE001
        provs = []
    fits: list[dict[str, Any]] = []
    for p in provs:
        pid = str(p.get("id") or "")
        entry: dict[str, Any] = {"provider": pid, "display_name": p.get("display_name") or pid,
                                 "engines": list(p.get("engines") or ()),
                                 "scale_to_zero": bool(p.get("scale_to_zero")),
                                 "public_by_default": bool(p.get("public_by_default")),
                                 "gpus": []}
        try:
            cands = _run_async(_dm.gpus, pid)
            pairs = _run_async(_dm.fit, info, list(cands))
            for pair in pairs:
                g, v = pair[0], pair[1]
                kind, reason = _verdict_parts(v)
                row = _gpu_dict(g)
                row["verdict"] = kind
                row["reason"] = reason
                entry["gpus"].append(row)
        except Exception as e:  # noqa: BLE001 — one provider's outage must not blank the panel
            entry["error"] = _deploy_err(e)["error"]
        fits.append(entry)
    out = _deploy_redact({"ok": True, "model": _dc(info), "fits": fits,
                          "recommended": _recommend_gpu(fits),
                          "checked_at": time.time(), "cache_ttl_s": ttl_s,
                          "hf_token_set": hf_token_set()})
    with _deploy_lock:
        _inspect_cache[model] = (now, out)
    return out


# -- jobs: deploy / teardown run in the background; the page polls -------------

_deploy_jobs: dict[str, dict[str, Any]] = {}
JOB_MAX_LINES = 400
JOB_KEEP = 40


# ---------------------------------------------------------------------------
# Agent-powered model search
#
# `manager.find_models` interprets a plain question and answers with GROUPS of
# models plus the columns that matter for that question. It can be slow (it may
# call a model), so it runs as a background job with the same progress channel
# the deploy jobs use, and the page polls /api/deploy/job for the steps.
#
# The contract is not assumed to exist: an older build has no find_models at
# all, so the absence is reported as a normal degraded answer rather than a
# 500, exactly like the rest of the Deploy page.

# The column vocabulary is fixed; anything outside it is dropped rather than
# passed through to the page, so a bad answer can't inject arbitrary keys.
_FIND_COLUMNS = ("params", "dtype", "vram", "license", "downloads", "updated",
                 "context", "fit", "price")


def _find_model_row(m: Any) -> dict[str, Any]:
    d = _deploy_redact(_dc(m))
    d["org"] = _org_of(d.get("id"))
    cached = _model_info_get(d.get("id") or "")
    if cached and not cached.get("error"):
        d = _merge_info(d, cached)
    return d


def _find_group(g: Any) -> dict[str, Any]:
    d = g if isinstance(g, dict) else _dc(g)
    models = d.get("models") or []
    return {
        "title": _redact_text(str(d.get("title") or "")),
        "reason": _redact_text(str(d.get("reason") or "")),
        "best": (str(d["best"]) if d.get("best") else None),
        "models": [_find_model_row(m) for m in models],
    }


def _find_dict(r: Any) -> dict[str, Any]:
    """SmartSearchResult -> the page's shape. Every string is redacted on the
    way out: an interpretation line is model-written text and could otherwise
    echo back something that was in the environment."""
    d = r if isinstance(r, dict) else _dc(r)
    cols = [c for c in (d.get("columns") or []) if c in _FIND_COLUMNS]
    filters = d.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    src = str(d.get("source") or "rules")
    return {
        "query": _redact_text(str(d.get("query") or "")),
        "interpretation": _redact_text(str(d.get("interpretation") or "")),
        "groups": [_find_group(g) for g in (d.get("groups") or [])],
        "columns": cols or ["params", "vram", "license"],
        "filters": {str(k): _deploy_redact(v) if isinstance(v, dict) else v for k, v in filters.items()},
        "source": src if src in ("agent", "rules") else "rules",
        "notes": [_redact_text(str(n)) for n in (d.get("notes") or [])],
    }


def deploy_find(query: str = "", provider: str | None = None, limit: Any = 24,
                agent: Any = "1") -> dict[str, Any]:
    """Start an agent-powered search. Returns a job id; the page polls
    /api/deploy/job for progress lines and the finished result."""
    from .deploy import manager as _dm  # noqa: PLC0415

    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "ask a question first"}
    try:
        lim = max(1, min(int(limit), 60))
    except (TypeError, ValueError):
        lim = 24
    use_agent = str(agent if agent is not None else "1").lower() not in ("0", "false", "no")
    finder = getattr(_dm, "find_models", None)
    if finder is None:
        return {"ok": False, "error": "agent search isn't available in this build yet",
                "hint": "update mantis-agent-sdk"}
    job = _new_job("find", q)

    def go(**kw: Any) -> Any:
        import os  # noqa: PLC0415

        return finder(q, provider_id=(provider or None), limit=lim,
                      hf_token=(os.environ.get("HF_TOKEN") or None),
                      use_agent=use_agent, **kw)

    _run_job(job, go, progress=_job_progress(job), _shape=_find_dict)
    return {"ok": True, "job": job["id"], "query": q, "agent": use_agent}


def _new_job(kind: str, target: str) -> dict[str, Any]:
    import time  # noqa: PLC0415

    job = {"id": secrets.token_urlsafe(9), "kind": kind, "target": target, "status": "running",
           "lines": [], "started_at": time.time(), "ended_at": None, "result": None,
           "error": None, "hint": None,
           # structured progress for the page: which stage, which deployment
           # (known the moment it is created, long before it is ready), how
           # long it has been booting, and what was asked for
           "stage": None, "deployment_id": None, "boot_s": None, "meta": {},
           "connected": None, "connect_error": None, "cancel": False,
           # what the container itself is saying while it boots, and the
           # fatal line if it is dying — read from the provider's logs
           "boot_line": None, "fatal": None}
    with _deploy_lock:
        _deploy_jobs[job["id"]] = job
        # bounded: forget the oldest finished jobs
        done = [j for j in _deploy_jobs.values() if j["status"] != "running"]
        for old in sorted(done, key=lambda j: j["started_at"])[:-JOB_KEEP] if len(done) > JOB_KEEP else []:
            _deploy_jobs.pop(old["id"], None)
    return job


def _job_progress(job: dict[str, Any]) -> Any:
    def progress(line: Any) -> None:
        s = _redact_text(str(line))[:400]
        with _deploy_lock:
            if len(job["lines"]) < JOB_MAX_LINES:
                job["lines"].append(s)
            else:
                job["lines"][-1] = s
    return progress


def _accepts_kw(fn: Any, name: str) -> bool:
    import inspect as _inspect  # noqa: PLC0415

    try:
        ps = _inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in ps or any(p.kind is _inspect.Parameter.VAR_KEYWORD for p in ps.values())


# A container that dies on import, or vLLM that can't allocate the model,
# looks exactly like a slow boot from the outside: the endpoint is simply not
# up yet. The difference is in the container's own log, so while a deploy is
# booting the server reads that log and (a) puts the last real line on the
# card, (b) stops the deploy at the first fatal line instead of letting the
# wait run to its 20-minute timeout at the GPU's hourly rate.
BOOT_TAIL_S = 25.0
_FATAL_RE = re.compile(
    r"Runner failed with exception|OutOfMemoryError|CUDA out of memory|Engine core initialization failed"
    r"|EngineCore failed|EngineDeadError|^(?:\w+\.)*\w+Error: |^Error: ", re.M)
_NOISE_RE = re.compile(r"^\s+|^Traceback|^\s*\^+$|^\[modal-client\]|^\s*$")


def _boot_tail(job: dict[str, Any], dep_id: str) -> None:
    from .deploy import manager as _dm  # noqa: PLC0415

    seen_tb = False
    while True:
        with _deploy_lock:
            if job["status"] != "running" or job.get("stage") != "boot" or job.get("cancel"):
                return
        try:
            async def collect() -> list[str]:
                out: list[str] = []
                async for line in _dm.logs(dep_id, tail=60):
                    out.append(_redact_text(str(line))[:400])
                    if len(out) >= 60:
                        break
                return out
            lines = _run_async(collect)
        except Exception:  # noqa: BLE001 — logs are best-effort; the wait continues
            lines = []
        fatal, last = None, None
        for ln in lines:
            if "Traceback (most recent call last)" in ln:
                seen_tb = True
            m = _FATAL_RE.search(ln)
            if m and (seen_tb or not ln.startswith(" ")):
                fatal = ln.strip()
            if not _NOISE_RE.match(ln):
                last = ln.strip()
        with _deploy_lock:
            if job["status"] != "running":
                return
            if last:
                job["boot_line"] = last[:200]
            if fatal:
                job["fatal"] = fatal[:300]
                job["cancel"] = True          # the deploy stops waiting; the leftover is torn down
                return
        time.sleep(BOOT_TAIL_S)


def _job_events(job: dict[str, Any]) -> Any:
    """``manager.deploy``'s structured events, folded onto the job record."""
    def on_event(kind: str, data: dict[str, Any]) -> None:
        with _deploy_lock:
            if kind == "stage":
                job["stage"] = str(data.get("stage") or "") or None
                start_tail = job["stage"] == "boot" and bool(job.get("deployment_id"))
            else:
                start_tail = False
            if kind == "created":
                job["deployment_id"] = str(data.get("id") or "") or None
            elif kind == "heartbeat":
                try:
                    job["boot_s"] = int(data.get("elapsed_s") or 0)
                except (TypeError, ValueError):
                    pass
        if start_tail:
            threading.Thread(target=_boot_tail, args=(job, job["deployment_id"]),
                             name="mantis-boot-tail", daemon=True).start()
    return on_event


def _run_job(job: dict[str, Any], coro_fn: Any, *args: Any, **kwargs: Any) -> None:
    """Run one async manager call on a background thread, recording progress
    lines and the result on the job. `_shape` lets a caller convert a result
    that isn't a Deployment into the page's shape (agent search does)."""
    import time  # noqa: PLC0415

    shape = kwargs.pop("_shape", None)
    after = kwargs.pop("_after", None)

    def body() -> None:
        try:
            res = _run_async(coro_fn, *args, **kwargs)
            with _deploy_lock:
                if shape is not None:
                    job["result"] = shape(res) if res is not None else None
                else:
                    job["result"] = _deploy_redact(_dep_dict(res)) if res is not None and hasattr(res, "gpu") \
                        else (_deploy_redact(_dc(res)) if res is not None else None)
            if after is not None and res is not None:
                # a follow-up that belongs to the same job (switch mantis to
                # the new endpoint): its failure is recorded, never fatal —
                # the deployment itself succeeded
                try:
                    after(res)
                except Exception as e:  # noqa: BLE001
                    with _deploy_lock:
                        job["connect_error"] = _deploy_err(e).get("error")
            with _deploy_lock:
                job["status"] = "done"
        except BaseException as e:  # noqa: BLE001 — the job record is the error channel
            d = _deploy_err(e)
            with _deploy_lock:
                if job.get("fatal"):
                    job["status"] = "error"
                    job["error"] = "the container died while booting: " + job["fatal"]
                    job["hint"] = "its logs have the full traceback; it was torn down so it isn't billing"
                else:
                    # asked to stop, and stopped: that is not a failure
                    job["status"] = "cancelled" if job.get("cancel") else "error"
                    job["error"] = d.get("error")
                    job["hint"] = d.get("hint")
        finally:
            with _deploy_lock:
                job["ended_at"] = time.time()
        # A cancel that landed after the endpoint was created but before the
        # page learned its id leaves one thing still billing. If nobody has
        # started deleting it, this is the last place that can.
        if job["kind"] == "deploy" and job.get("cancel") and job.get("deployment_id"):
            dep_id = job["deployment_id"]
            with _deploy_lock:
                handled = any(j["kind"] == "teardown" and j.get("deployment_id") == dep_id
                              for j in _deploy_jobs.values())
            if not handled:
                deploy_down(dep_id)
        # a finished deploy/teardown changes the list — drop the fit cache too,
        # the provider's availability may have moved. A search changes nothing.
        if job["kind"] != "find":
            with _deploy_lock:
                _inspect_cache.clear()

    threading.Thread(target=body, name=f"mantis-deploy-{job['kind']}", daemon=True).start()


def deploy_job(job_id: str | None) -> dict[str, Any]:
    import time  # noqa: PLC0415

    with _deploy_lock:
        job = _deploy_jobs.get(job_id or "")
        if job is None:
            return {"ok": False, "error": f"job {job_id!r} not found"}
        d = dict(job)
        d["lines"] = list(job["lines"])
        d["meta"] = dict(job.get("meta") or {})
    d["ok"] = True
    d["elapsed_s"] = round((d["ended_at"] or time.time()) - d["started_at"], 1)
    return d


#: How long a finished deploy/teardown stays on the page after it ends, so a
#: reload — or coming back from another tab — still shows how it went.
JOB_RECENT_S = 15 * 60


def deploy_jobs() -> dict[str, Any]:
    """Every deploy/teardown still running, plus those that ended in the last
    fifteen minutes — what the Deploy page re-attaches to after a reload, so
    closing the tab never loses a deploy in flight. Lines are left out; the
    page asks /api/deploy/job for one it is showing."""
    import time  # noqa: PLC0415

    now = time.time()
    out = []
    with _deploy_lock:
        for j in _deploy_jobs.values():
            if j["kind"] not in ("deploy", "teardown", "connect"):
                continue
            if j["status"] != "running" and (now - (j["ended_at"] or now)) > JOB_RECENT_S:
                continue
            d = {k: v for k, v in j.items() if k not in ("lines", "result")}
            d["meta"] = dict(j.get("meta") or {})
            d["last_line"] = j["lines"][-1] if j["lines"] else None
            d["elapsed_s"] = round((j["ended_at"] or now) - j["started_at"], 1)
            d["endpoint_url"] = ((j.get("result") or {}).get("endpoint_url")
                                 if isinstance(j.get("result"), dict) else None)
            out.append(d)
    out.sort(key=lambda d: -d["started_at"])
    return {"ok": True, "jobs": out}


_OPT_INT = ("max_model_len", "tensor_parallel", "min_replicas", "max_replicas", "idle_timeout_s",
            "request_timeout_s")
_OPT_STR = ("name", "hf_token", "served_model_name", "quantization", "region", "engine_version")


def _build_opts(raw: Any) -> Any:
    from .deploy.base import DeployOpts  # noqa: PLC0415

    opts = DeployOpts()
    if not isinstance(raw, dict):
        return opts
    for k in _OPT_STR:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            setattr(opts, k, v.strip())
    for k in _OPT_INT:
        v = raw.get(k)
        if v in (None, ""):
            continue
        try:
            setattr(opts, k, int(v))
        except (TypeError, ValueError):
            continue
    if "trust_remote_code" in raw:
        opts.trust_remote_code = bool(raw.get("trust_remote_code"))
    ea = raw.get("extra_engine_args")
    if isinstance(ea, str):
        opts.extra_engine_args = ea.split()
    elif isinstance(ea, list):
        opts.extra_engine_args = [str(x) for x in ea]
    if isinstance(raw.get("extra"), dict):
        opts.extra = dict(raw["extra"])
    return opts


def _display_meta(raw: Any) -> dict[str, Any]:
    """What the page showed when it asked — the GPU's name, its rate, the
    provider's name — kept on the job so a card can say "L40S · $1.10/h" while
    it boots and after a reload. Only these three keys, only plain values."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for k in ("gpu_label", "provider_name"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = _redact_text(v.strip())[:60]
    try:
        pr = raw.get("price_per_hour")
        if pr is not None:
            out["price_per_hour"] = round(float(pr), 4)
    except (TypeError, ValueError):
        pass
    return out


def deploy_up(provider: str | None, model: str | None, gpu: Any, engine: str | None,
              opts: Any = None, use_when_ready: Any = False, display: Any = None) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider or not model:
        return {"ok": False, "error": "provider and model required"}
    gpu_id = gpu.get("provider_id") if isinstance(gpu, dict) else gpu
    gpu_id = str(gpu_id or "").strip()
    if not gpu_id:
        return {"ok": False, "error": "gpu required"}
    engine = (engine or "vllm").strip().lower()
    if engine not in ("vllm", "sglang", "tgi", "llamacpp"):
        return {"ok": False, "error": f"unknown engine {engine!r}"}
    # A second click while the first is still starting must not rent a second
    # GPU: the same model on the same card at the same provider is one deploy.
    with _deploy_lock:
        for j in _deploy_jobs.values():
            m = j.get("meta") or {}
            if (j["kind"] == "deploy" and j["status"] == "running" and m.get("provider") == provider
                    and m.get("model") == model and m.get("gpu") == gpu_id):
                return {"ok": True, "job": j["id"], "provider": provider, "model": model,
                        "gpu": gpu_id, "engine": m.get("engine"), "existing": True}
    use = bool(use_when_ready)
    job = _new_job("deploy", model)
    job["meta"] = {"provider": provider, "model": model, "gpu": gpu_id, "engine": engine,
                   "use_when_ready": use, **_display_meta(display)}

    def connect_after(dep: Any) -> None:
        from .deploy import manager as _dm2  # noqa: PLC0415

        # "Ready" only means the endpoint lists its models. A model can load
        # and still be unable to answer (wrong chat template, bad weights), so
        # mantis is only pointed at it after it answers a real prompt — never
        # on the strength of /models alone.
        probe = getattr(_dm2, "try_endpoint", None)
        if probe is not None:
            try:
                r = _run_async(probe, dep.id, "Reply with the single word: ok")
            except Exception as e:  # noqa: BLE001
                raise RuntimeError("didn't switch mantis to it — it didn't answer a test prompt: "
                                   + str(_deploy_err(e).get("error") or e)) from e
            if not str((r or {}).get("reply") or "").strip():
                raise RuntimeError("didn't switch mantis to it — it answered a test prompt with nothing")
        try:
            _run_async(_dm2.connect, dep.id, set_current=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("didn't switch mantis to it — " + str(_deploy_err(e).get("error") or e)) from e
        with _deploy_lock:
            job["connected"] = True

    kw: dict[str, Any] = {}
    # structured stages are a newer part of the contract; a deploy core (or a
    # test double) that predates them still runs, with text progress only
    if _accepts_kw(_dm.deploy, "on_event"):
        kw["on_event"] = _job_events(job)
    if _accepts_kw(_dm.deploy, "cancelled"):
        kw["cancelled"] = lambda: bool(job.get("cancel"))
    _run_job(job, _dm.deploy, provider, model, gpu=gpu_id, engine=engine,
             opts=_build_opts(opts), wait=True, progress=_job_progress(job),
             _after=(connect_after if use else None), **kw)
    return {"ok": True, "job": job["id"], "provider": provider, "model": model, "gpu": gpu_id,
            "engine": engine}


def _deploy_cost(dep: Any) -> dict[str, Any] | None:
    """Best-effort cost for one deployment via its adapter. ``None`` where the
    provider can't say — the page shows a dash, never a guess."""
    from .deploy import base as _db  # noqa: PLC0415

    try:
        prov = _db.get_provider(dep.provider)
        return _dc(_run_async(prov.cost, dep))
    except Exception:  # noqa: BLE001 — NotSupported, network, unconfigured
        return None


def deploy_list(refresh: Any = False, with_cost: bool = True) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    ref = str(refresh).lower() in ("1", "true", "yes")
    try:
        deps = _run_async(_dm.list_deployments, refresh=ref)
    except Exception as e:  # noqa: BLE001
        return {**_deploy_err(e), "deployments": []}
    try:
        from . import catalog  # noqa: PLC0415

        cur = (catalog.get_last_model() or {}).get("backend") or ""
    except Exception:  # noqa: BLE001
        cur = ""
    cur = cur.rstrip("/")
    rows = []
    for dep in deps:
        d = _dep_dict(dep)
        d["cost"] = _deploy_cost(dep) if with_cost and dep.status not in ("deleted", "deleting") else None
        # the one deployment mantis is pointed at right now, if any
        d["in_use"] = bool(cur and (d.get("endpoint_url") or "").rstrip("/") == cur)
        rows.append(d)
    rows.sort(key=lambda r: (not r.get("is_live"), -(r.get("created_at") or 0)))
    return _deploy_redact({"ok": True, "refreshed": ref, "deployments": rows,
                           "live_count": sum(1 for r in rows if r.get("is_live"))})


def deploy_status(dep_id: str | None) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    if not dep_id:
        return {"ok": False, "error": "id required"}
    try:
        dep = _run_async(_dm.status, dep_id, refresh=True)
    except Exception as e:  # noqa: BLE001
        return _deploy_err(e)
    d = _dep_dict(dep)
    d["cost"] = _deploy_cost(dep)
    return _deploy_redact({"ok": True, "deployment": d})


def deploy_logs(dep_id: str | None, tail: Any = 200) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415
    from .deploy.base import NotSupported  # noqa: PLC0415

    if not dep_id:
        return {"ok": False, "error": "id required"}
    try:
        n = max(10, min(int(tail), 2000))
    except (TypeError, ValueError):
        n = 200

    async def collect() -> list[str]:
        out: list[str] = []
        async for line in _dm.logs(dep_id, tail=n):
            out.append(_redact_text(str(line))[:1000])
            if len(out) >= n:
                break
        return out

    try:
        lines = _run_async(collect)
    except NotSupported as e:
        return {"ok": False, "supported": False, "error": _redact_text(str(e)),
                "hint": getattr(e, "hint", None) or "this provider has no logs API — use its console"}
    except Exception as e:  # noqa: BLE001
        return _deploy_err(e)
    return {"ok": True, "supported": True, "id": dep_id, "tail": n, "lines": lines}


_USAGE_CACHE: dict[str, tuple[float, list[str]]] = {}
_USAGE_TTL_S = 45.0


def deploy_usage(dep_id: str | None, hours: Any = 24) -> dict[str, Any]:
    """Throughput, load, boot vs serving time and what it cost, for one
    deployment — parsed from its container log (``deploy/usage.py``).

    The log comes from the provider's control plane, never from the endpoint:
    a request to a scaled-to-zero GPU app boots it, and even a metrics scrape
    resets its idle timer, so a dashboard left open would keep it billing.
    Whether a container is up right now comes from the stored status, which
    is itself read without touching the endpoint."""
    import time  # noqa: PLC0415

    from .deploy import manager as _dm  # noqa: PLC0415
    from .deploy import store as _ds  # noqa: PLC0415
    from .deploy import usage as _du  # noqa: PLC0415
    from .deploy.base import NotSupported  # noqa: PLC0415

    if not dep_id:
        return {"ok": False, "error": "id required"}
    dep = _ds.get(dep_id)
    if dep is None:
        return {"ok": False, "error": f"no deployment {dep_id!r}"}
    try:
        h = max(1.0, min(float(hours), 24.0 * 14))
    except (TypeError, ValueError):
        h = 24.0
    now = time.time()
    hit = _USAGE_CACHE.get(dep_id)
    if hit and now - hit[0] < _USAGE_TTL_S:
        lines = hit[1]
    else:
        async def collect() -> list[str]:
            return [str(x) async for x in _dm.logs(dep_id, tail=5000)]

        try:
            lines = _run_async(collect)
        except NotSupported as e:
            return {"ok": False, "supported": False, "error": _redact_text(str(e))}
        except Exception as e:  # noqa: BLE001
            # the CLI hiccups now and then; a minute-old log beats an error
            if not hit:
                return _deploy_err(e)
            lines = hit[1]
        else:
            _USAGE_CACHE[dep_id] = (now, lines)
    rate = dep.gpu.price_per_hour if dep.gpu else None
    live = dep.status in ("running", "starting")
    out = _du.summarize(_du.parse(lines, now), since=now - h * 3600, until=now, live=live, rate_per_hour=rate)
    return {"ok": True, "id": dep_id, "hours": h, "status": dep.status, **out}


def deploy_connect_job(dep_id: str | None) -> dict[str, Any]:
    """Point mantis at a deployment as a JOB: a scaled-to-zero replica can
    take a cold start (minutes) to wake, and a POST that blocks that long is a
    button that says "Connecting…" forever. The card follows the job."""
    from .deploy import manager as _dm  # noqa: PLC0415

    if not dep_id:
        return {"ok": False, "error": "id required"}
    with _deploy_lock:
        for j in _deploy_jobs.values():
            if j["kind"] == "connect" and j["status"] == "running" and j.get("deployment_id") == dep_id:
                return {"ok": True, "job": j["id"], "existing": True}
    job = _new_job("connect", dep_id)
    job["deployment_id"] = dep_id
    kw: dict[str, Any] = {}
    if _accepts_kw(_dm.connect, "progress"):
        kw["progress"] = _job_progress(job)

    def shape(info: Any) -> dict[str, Any]:
        d = dict(info or {})
        model, backend = d.get("model"), d.get("backend")
        d["shell"] = (f"MANTIS_AGENT_MODEL={model} MANTIS_AGENT_BASE_URL={backend} mantis" if model and backend else None)
        return d

    _run_job(job, _dm.connect, dep_id, set_current=True, _shape=shape, **kw)
    return {"ok": True, "job": job["id"]}


def deploy_forget(dep_id: str | None) -> dict[str, Any]:
    """Drop a record mantis adopted from the provider but cannot use — no
    endpoint, unknown state. It only forgets the record; nothing is deleted
    on the provider, which is why it is offered only for such records."""
    from .deploy import store  # noqa: PLC0415

    if not dep_id:
        return {"ok": False, "error": "id required"}
    try:
        dep = store.find(dep_id)
    except Exception as e:  # noqa: BLE001
        return _deploy_err(e)
    if dep.endpoint_url and dep.status in ("running", "scaled_to_zero", "starting", "pending", "building"):
        return {"ok": False, "error": "that one is live — stop it instead",
                "hint": "Forget is only for records with no endpoint"}
    store.remove(dep_id)
    return {"ok": True, "id": dep_id}


def deploy_connect(dep_id: str | None) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    if not dep_id:
        return {"ok": False, "error": "id required"}
    try:
        info = _run_async(_dm.connect, dep_id, set_current=True)
    except Exception as e:  # noqa: BLE001
        return _deploy_err(e)
    d = dict(info or {})
    model, backend = d.get("model"), d.get("backend")
    d["shell"] = (f"MANTIS_AGENT_MODEL={model} MANTIS_AGENT_BASE_URL={backend} mantis"
                  if model and backend else None)
    d["python"] = (f'MantisAgentOptions(model="{model}", backend="{backend}")'
                   if model and backend else None)
    return _deploy_redact({"ok": True, "id": dep_id, **d})


def deploy_down(dep_id: str | None) -> dict[str, Any]:
    from .deploy import manager as _dm  # noqa: PLC0415

    if not dep_id:
        return {"ok": False, "error": "id required"}
    with _deploy_lock:
        for j in _deploy_jobs.values():
            if j["kind"] == "deploy" and j["status"] == "running" and j.get("deployment_id") == dep_id:
                j["cancel"] = True
    job = _new_job("teardown", dep_id)
    job["deployment_id"] = dep_id
    _run_job(job, _dm.teardown, dep_id, progress=_job_progress(job))
    return {"ok": True, "job": job["id"], "id": dep_id}


_PKG_RE = re.compile(r"`([A-Za-z0-9_.\-\[\]]+)`")


def _install_cmd(pkg: str) -> list[str]:
    """Install into THIS interpreter's environment. A uv-managed venv has no
    pip, so prefer uv when it is on PATH; argv only, never a shell."""
    import shutil  # noqa: PLC0415

    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, pkg]
    return [sys.executable, "-m", "pip", "install", pkg]


def deploy_install(provider: str | None) -> dict[str, Any]:
    """One click for "needs the X package": install it into the dashboard's
    own environment as a job the page can watch, then re-check the provider.
    Refused unless the provider itself says it is missing that package."""
    import subprocess  # noqa: PLC0415

    from .deploy import manager as _dm  # noqa: PLC0415

    pid = (provider or "").strip()
    try:
        prov = next((p for p in _run_async(_dm.providers) if p.get("id") == pid), None)
    except Exception as e:  # noqa: BLE001
        return _deploy_err(e)
    if prov is None:
        return {"ok": False, "error": f"unknown provider {pid!r}"}
    if prov.get("requirements_ok", True):
        return {"ok": True, "job": None, "installed": True, "already": True}
    m = _PKG_RE.search(str(prov.get("requirements_hint") or ""))
    if not m:
        return {"ok": False, "error": "this provider doesn't say which package it needs",
                "hint": str(prov.get("requirements_hint") or "")}
    pkg = m.group(1)
    with _deploy_lock:
        for j in _deploy_jobs.values():
            if j["kind"] == "install" and j["status"] == "running" and j.get("target") == pkg:
                return {"ok": True, "job": j["id"], "package": pkg, "existing": True}
    job = _new_job("install", pkg)
    job["meta"] = {"provider": pid, "package": pkg}
    say = _job_progress(job)

    async def run() -> dict[str, Any]:
        import importlib  # noqa: PLC0415

        cmd = _install_cmd(pkg)
        say("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # noqa: S603
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.strip():
                say(line.rstrip())
        rc = proc.wait(timeout=900)
        if rc != 0:
            raise RuntimeError(f"install exited {rc} — see the lines above")
        importlib.invalidate_caches()
        fresh = next((p for p in await _dm.providers() if p.get("id") == pid), None) or {}
        ok = bool(fresh.get("requirements_ok", False))
        say(("✓ " + pkg + " installed — " + str(fresh.get("display_name") or pid) + " can deploy")
            if ok else ("installed, but " + pid + " still reports: " + str(fresh.get("requirements_hint") or "not ready")))
        if not ok:
            raise RuntimeError(str(fresh.get("requirements_hint") or pid + " is still not ready"))
        return {"installed": True, "package": pkg, "provider": pid}

    _run_job(job, run)
    return {"ok": True, "job": job["id"], "package": pkg}


def deploy_try(dep_id: str | None, prompt: Any = None) -> dict[str, Any]:
    """One chat completion against a deployment, timed — "does it actually
    answer". Blocking: it runs on this request's own thread, and a cold
    replica can take minutes to wake."""
    from .deploy import manager as _dm  # noqa: PLC0415

    if not dep_id:
        return {"ok": False, "error": "id required"}
    fn = getattr(_dm, "try_endpoint", None)
    if fn is None:
        return {"ok": False, "error": "this build can't test an endpoint yet"}
    try:
        r = _run_async(fn, dep_id, str(prompt or "") or "Say hello in one short sentence.")
    except Exception as e:  # noqa: BLE001
        return _deploy_err(e)
    return _deploy_redact({"ok": True, "id": dep_id, **r})


def deploy_cancel(job_id: str | None) -> dict[str, Any]:
    """Stop a deploy that is still starting. Before anything is rented it just
    stops; once the provider has created the endpoint, stopping means deleting
    it, so a teardown job is started too and returned for the page to follow."""
    with _deploy_lock:
        job = _deploy_jobs.get(job_id or "")
        if job is None or job["kind"] != "deploy":
            return {"ok": False, "error": f"no deploy job {job_id!r}"}
        if job["status"] != "running":
            return {"ok": True, "job": job["id"], "status": job["status"], "teardown": None}
        job["cancel"] = True
        dep_id = job.get("deployment_id")
    down = deploy_down(dep_id) if dep_id else None
    return {"ok": True, "job": job["id"], "status": "cancelling",
            "teardown": (down or {}).get("job") if down and down.get("ok") else None}


def deployments_live_count() -> int:
    """Live deployments from the store only (no network) — for the rail and
    the overview's signal path. Zero when the deploy core isn't present."""
    from .deploy import manager as _dm  # noqa: PLC0415

    try:
        return sum(1 for d in _run_async(_dm.list_deployments, refresh=False) if d.is_live)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Auth methods — every way to authenticate each provider family. The contract
# lives in ``mantis_agent.auth_methods``; this is the JSON skin over it.
# Values only ever travel INWARD: what comes back is env var NAMES, the
# contract's own masked hints, and whether each method is configured/active.
# ---------------------------------------------------------------------------

# auth family id -> (display label, logo id, the id serve.FAMILIES uses)
_AUTH_FAMILY_UI: dict[str, tuple[str, str, str]] = {
    "anthropic": ("Claude", "anthropic", "anthropic"),
    "openai": ("OpenAI", "openai", "openai"),
    "gemini": ("Gemini", "gemini", "google"),
    "xai": ("Grok", "xai", "xai"),
    "oss": ("Open models", "ollama", "oss"),
}


def _auth_err(e: BaseException) -> dict[str, Any]:
    if isinstance(e, NotImplementedError):
        return {"ok": False, "error": "provider setup isn't available in this build yet",
                "hint": "update mantis-agent-sdk"}
    return {"ok": False, "error": _redact_text(str(e) or type(e).__name__), "kind": type(e).__name__}


def _mask_hint(v: Any) -> str | None:
    """The contract already hands back masked hints; anything that still looks
    whole is masked again here so a bug upstream can't leak a key."""
    s = str(v or "")
    if not s:
        return None
    return s if ("…" in s or "•" in s or len(s) <= 8) else (_mask_key(s) or None)


def _field_dict(f: Any) -> dict[str, Any]:
    return {"env": f.env, "label": f.label, "secret": bool(f.secret),
            "required": bool(f.required), "help": f.help, "placeholder": f.placeholder}


def _status_dict(st: Any) -> dict[str, Any]:
    st = dict(st or {})
    return {"configured": bool(st.get("configured")), "source": st.get("source"),
            "active": bool(st.get("active")), "hint": _redact_text(str(st.get("hint") or "")),
            "masked": {k: _mask_hint(v) for k, v in (st.get("masked") or {}).items()}}


def _family_models(fam_ui_id: str) -> int:
    try:
        m = models_state()
    except Exception:  # noqa: BLE001
        return 0
    n = sum(len(p.get("models") or ()) for p in m.get("providers") or [] if p.get("family") == fam_ui_id)
    if fam_ui_id == "oss":
        n += len((m.get("ollama") or {}).get("models") or [])
    return n


def auth_family_methods(family: str | None) -> dict[str, Any]:
    """One family's methods with their fields and per-method status."""
    from . import auth_methods as A  # noqa: PLC0415

    family = (family or "").strip()
    if family not in A.FAMILIES:
        return {"ok": False, "error": f"unknown family {family!r}",
                "hint": "expected one of " + ", ".join(A.FAMILIES)}
    try:
        methods = A.auth_methods(family)
    except Exception as e:  # noqa: BLE001
        return {**_auth_err(e), "methods": []}
    try:
        status = A.method_status(family)
    except Exception:  # noqa: BLE001 — a broken probe must not hide the methods
        status = {}
    label, logo, ui_id = _AUTH_FAMILY_UI.get(family, (family, family, family))
    out = []
    for m in methods:
        st = _status_dict(status.get(m.id) or {})
        out.append({
            "id": m.id, "family": m.family, "label": m.label, "kind": m.kind,
            "description": m.description, "backend": m.backend, "docs_url": m.docs_url,
            "recommended": bool(m.recommended),
            "fields": [_field_dict(f) for f in m.fields],
            "token_env": (m.extra or {}).get("token_env"),
            # A login another CLI holds (Codex): the dashboard shows the command
            # to run instead of a form or a browser sign-in.
            "cli_login": ((m.extra or {}).get("login")
                          if (m.extra or {}).get("detected_from") else None),
            "status": st,
        })
    active = next((m["id"] for m in out if m["status"]["active"]), None)
    return {"ok": True, "family": family, "label": label, "logo": logo, "ui_family": ui_id,
            "methods": out, "active": active,
            "configured": [m["id"] for m in out if m["status"]["configured"]]}


def auth_families() -> dict[str, Any]:
    """Every family with its active method, a one-line status and model count."""
    from . import auth_methods as A  # noqa: PLC0415

    fams = []
    for fam in A.FAMILIES:
        d = auth_family_methods(fam)
        label, logo, ui_id = _AUTH_FAMILY_UI.get(fam, (fam, fam, fam))
        if not d.get("ok"):
            fams.append({"family": fam, "label": label, "logo": logo, "ui_family": ui_id,
                         "ok": False, "error": d.get("error"), "methods": [], "active": None,
                         "configured": [], "model_count": _family_models(ui_id), "status_line": "Not connected"})
            continue
        active = d["active"]
        am = next((m for m in d["methods"] if m["id"] == active), None)
        if am:
            masked = next((v for v in (am["status"]["masked"] or {}).values() if v), None)
            line = "Connected via " + am["label"] + (" · " + masked if masked else "")
        elif d["configured"]:
            line = "Configured, not active"
        else:
            line = "Not connected"
        fams.append({"family": fam, "label": label, "logo": logo, "ui_family": ui_id, "ok": True,
                     "active": active, "active_label": am["label"] if am else None,
                     "active_kind": am["kind"] if am else None,
                     "configured": d["configured"], "methods": d["methods"],
                     "method_count": len(d["methods"]), "model_count": _family_models(ui_id),
                     "connected": bool(active), "status_line": line,
                     "recommended": next((m["id"] for m in d["methods"] if m["recommended"]), None)})
    return {"ok": True, "families": fams,
            "connected_count": sum(1 for f in fams if f.get("connected"))}


def auth_set(family: str | None, method: str | None, values: Any) -> dict[str, Any]:
    from . import auth_methods as A  # noqa: PLC0415

    family, method = (family or "").strip(), (method or "").strip()
    if not family or not method:
        return {"ok": False, "error": "family and method required"}
    clean = {str(k).strip(): str(v) for k, v in (values or {}).items() if str(v).strip()}         if isinstance(values, dict) else {}
    try:
        r = dict(A.set_method(family, method, clean))
    except Exception as e:  # noqa: BLE001
        return _auth_err(e)
    r["message"] = _redact_text(str(r.get("message") or ""))
    # only the NAMES of what was saved travel back
    return _deploy_redact({**r, "family": family, "method": method, "saved": sorted(clean),
                           "status": auth_family_methods(family)})


def auth_clear(family: str | None, method: str | None) -> dict[str, Any]:
    from . import auth_methods as A  # noqa: PLC0415

    family, method = (family or "").strip(), (method or "").strip()
    if not family or not method:
        return {"ok": False, "error": "family and method required"}
    try:
        r = dict(A.clear_method(family, method))
    except Exception as e:  # noqa: BLE001
        return _auth_err(e)
    r["message"] = _redact_text(str(r.get("message") or ""))
    return _deploy_redact({**r, "family": family, "method": method,
                           "status": auth_family_methods(family)})


def auth_validate(family: str | None, method: str | None, model: str | None = None) -> dict[str, Any]:
    from . import auth_methods as A  # noqa: PLC0415

    family, method = (family or "").strip(), (method or "").strip()
    if not family or not method:
        return {"ok": False, "error": "family and method required"}
    try:
        r = dict(_run_async(A.validate_method, family, method, model=(model or None)))
    except Exception as e:  # noqa: BLE001
        return _auth_err(e)
    r["message"] = _redact_text(str(r.get("message") or ""))
    r["models"] = [str(x) for x in (r.get("models") or [])][:8]
    return _deploy_redact({**r, "family": family, "method": method})


def auth_oauth_start(family: str | None) -> dict[str, Any]:
    from . import auth_methods as A  # noqa: PLC0415

    try:
        r = dict(A.oauth_start((family or "").strip()))
    except Exception as e:  # noqa: BLE001
        return _auth_err(e)
    return {"ok": True, "url": r.get("url"), "handle": r.get("handle"),
            "instructions": _redact_text(str(r.get("instructions") or ""))}


def auth_oauth_finish(handle: str | None, code: str | None) -> dict[str, Any]:
    from . import auth_methods as A  # noqa: PLC0415

    if not (handle or "").strip() or not (code or "").strip():
        return {"ok": False, "error": "handle and code required"}
    try:
        r = dict(A.oauth_finish(handle.strip(), code.strip()))
    except Exception as e:  # noqa: BLE001
        return _auth_err(e)
    r["message"] = _redact_text(str(r.get("message") or ""))
    fam = r.get("family") or "anthropic"
    return _deploy_redact({**r, "status": auth_family_methods(fam)})


# ---------------------------------------------------------------------------
# Events — a version counter over everything the pages render. The page
# long-polls ``/api/events?since=<version>`` and refreshes only when it moves,
# so nothing flickers on a timer while the world is still. Cheap: every input
# is a stat() or an in-memory dict; no transcript is read.
# ---------------------------------------------------------------------------


def _state_version() -> str:
    import hashlib  # noqa: PLC0415

    parts: list[Any] = []
    try:
        parts.append(_projects_signature())
    except Exception:  # noqa: BLE001
        parts.append(None)
    try:
        parts.append(_runs_signature())
    except Exception:  # noqa: BLE001
        parts.append(None)
    try:
        from . import job_records  # noqa: PLC0415

        jd = job_records.jobs_dir()
        parts.append(tuple(sorted((f.name, f.stat().st_mtime) for f in jd.glob("*.json"))) if jd.is_dir() else ())
    except Exception:  # noqa: BLE001
        parts.append(None)
    base = _base_dir()
    for name in ("models.json", "settings.json", "mcp.json"):
        try:
            parts.append((name, (base / name).stat().st_mtime))
        except OSError:
            parts.append((name, None))
    with _deploy_lock:
        parts.append(tuple(sorted((j["id"], j["status"], j.get("stage"), len(j["lines"]),
                                   j.get("deployment_id"), j.get("connected"))
                                  for j in _deploy_jobs.values())))
        parts.append(tuple(sorted(_deploy_accounts)))
    return hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()[:12]


def events(since: str | None, timeout_s: float = 25.0) -> dict[str, Any]:
    """Block (up to ``timeout_s``) until the version differs from ``since``."""
    import time  # noqa: PLC0415

    timeout_s = max(0.0, min(float(timeout_s), 30.0))
    t0 = time.monotonic()
    v = _state_version()
    while since and v == since and time.monotonic() - t0 < timeout_s:
        time.sleep(0.5)
        v = _state_version()
    return {"version": v, "changed": bool(since) and v != since, "ts": time.time()}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "mantis-serve"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _auth_ok(self, method: str, q: dict[str, list[str]]) -> bool:
        token = getattr(self.server, "token", None)
        given = (q.get("k") or [None])[0] or self.headers.get("X-Mantis-Token")
        # Writes ALWAYS require the token (inlined into the served page, so a
        # random cross-origin web page can't POST to your localhost — same-origin
        # policy stops it reading the token). Reads are open on a loopback bind.
        if method == "POST":
            return bool(token) and secrets.compare_digest(given or "", token)
        if getattr(self.server, "enforce_get", False):
            return secrets.compare_digest(given or "", token or "")
        return True

    def _host_ok(self) -> bool:
        """DNS-rebinding defense: only serve requests whose Host header names a
        bind address we actually own. A malicious page that rebinds its own
        hostname to 127.0.0.1 still sends ``Host: attacker.com``, which is not in
        the allowlist, so it can neither read data nor scrape the write token."""
        allowed = getattr(self.server, "allowed_hosts", None)
        if not allowed:  # no allowlist configured — fail open only if unset
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(host) and host in allowed

    def log_message(self, *args: Any) -> None:  # noqa: D401 — silence stderr spam
        return

    # -- routing ----------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send(421, b"misdirected request - bad Host header",
                       "text/plain; charset=utf-8")
            return
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        # Public logo asset (header + favicon) — no token, no data, so serve it
        # before the auth gate so it loads in LAN mode too.
        if path == "/mantis.svg":
            from .serve_ui import MANTIS_SVG  # noqa: PLC0415

            self._send(200, MANTIS_SVG.encode("utf-8"), "image/svg+xml; charset=utf-8")
            return
        if not self._auth_ok(self.command, q):
            self._send(401, b"unauthorized - open the URL printed by "
                       b"`mantis serve` (it carries the access token)",
                       "text/plain; charset=utf-8")
            return
        try:
            self._route(path, q)
        except Exception as e:  # noqa: BLE001 — never crash the server on one request
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send(421, b"misdirected request - bad Host header",
                       "text/plain; charset=utf-8")
            return
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if not self._auth_ok("POST", q):
            self._send(401, b"unauthorized", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "invalid JSON body"}, 400)
            return
        try:
            if path == "/api/connect":
                self._json(connect_selfhost(body.get("backend"), body.get("model"),
                                            body.get("key")))
                return
            if path == "/api/use":
                self._json(set_current(body.get("model"), body.get("backend")))
                return
            if path == "/api/skill":
                self._json(add_skill(body.get("scope"), body.get("name"),
                                     body.get("description"), body.get("body"),
                                     body.get("category") or "",
                                     bool(body.get("always_load")),
                                     body.get("slug"), body.get("tools")))
                return
            if path == "/api/skill/delete":
                self._json(delete_skill(body.get("scope"), body.get("slug")))
                return
            if path == "/api/memory/file":
                self._json(save_instruction(body.get("id"), body.get("content")))
                return
            if path == "/api/memory/index":
                self._json(save_memory_index(body.get("content")))
                return
            if path == "/api/memory/entry":
                self._json(save_memory(body.get("slug"), body.get("name"), body.get("description"),
                                       body.get("type"), body.get("body")))
                return
            if path == "/api/memory/entry/delete":
                self._json(delete_memory(body.get("slug")))
                return
            if path == "/api/mcp":
                self._json(add_mcp(body.get("scope"), body.get("name"), body.get("entry")))
                return
            if path == "/api/mcp/paste":
                self._json(add_mcp_paste(body.get("scope"), body.get("text")))
                return
            if path == "/api/mcp/test":
                self._json(test_mcp(body.get("name")))
                return
            if path == "/api/mcp/trust":
                self._json(trust_project_mcp_file())
                return
            if path == "/api/model/test":
                self._json(test_provider(body.get("provider"), body.get("backend"),
                                         body.get("key")))
                return
            if path == "/api/mcp/delete":
                self._json(delete_mcp(body.get("scope"), body.get("name")))
                return
            # -- auth: how each provider family is authenticated --
            if path == "/api/auth/set":
                self._json(auth_set(body.get("family"), body.get("method"), body.get("values")))
                return
            if path == "/api/auth/clear":
                self._json(auth_clear(body.get("family"), body.get("method")))
                return
            if path == "/api/auth/validate":
                self._json(auth_validate(body.get("family"), body.get("method"), body.get("model")))
                return
            if path == "/api/auth/oauth/start":
                self._json(auth_oauth_start(body.get("family")))
                return
            if path == "/api/auth/oauth/finish":
                self._json(auth_oauth_finish(body.get("handle"), body.get("code")))
                return
            # -- deploy: bring-your-own GPU provider (mutating) --
            if path == "/api/deploy/creds":
                self._json(deploy_save_creds(body.get("provider"), body.get("values")))
                return
            if path == "/api/deploy/validate":
                self._json(deploy_validate(body.get("provider")))
                return
            if path == "/api/deploy/up":
                self._json(deploy_up(body.get("provider"), body.get("model"), body.get("gpu"),
                                     body.get("engine"), body.get("opts"),
                                     body.get("use_when_ready"), body.get("display")))
                return
            if path == "/api/deploy/connect":
                self._json(deploy_connect_job(body.get("id")) if body.get("job") else deploy_connect(body.get("id")))
                return
            if path == "/api/deploy/forget":
                self._json(deploy_forget(body.get("id")))
                return
            if path == "/api/deploy/install":
                self._json(deploy_install(body.get("provider")))
                return
            if path == "/api/deploy/try":
                self._json(deploy_try(body.get("id"), body.get("prompt")))
                return
            if path == "/api/deploy/cancel":
                self._json(deploy_cancel(body.get("job")))
                return
            if path == "/api/deploy/down":
                self._json(deploy_down(body.get("id")))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _route(self, path: str, q: dict[str, list[str]]) -> None:
        if path in ("/", "/index.html"):
            from .serve_logos import PROVIDER_LOGOS  # noqa: PLC0415
            from .serve_ui import INDEX_HTML  # noqa: PLC0415

            html = INDEX_HTML.replace("__TOKEN__", getattr(self.server, "token", "") or "")
            # Provider logos are inlined so the page stays offline and doesn't
            # phone twelve CDNs. json.dumps also escapes </script> safely.
            html = html.replace("__LOGOS__", json.dumps(PROVIDER_LOGOS).replace("</", "<\\/"))
            from .serve_logos import ORG_LOGOS, ORG_NAMES  # noqa: PLC0415

            html = html.replace("__ORGLOGOS__", json.dumps(ORG_LOGOS).replace("</", "<\\/"))
            html = html.replace("__ORGNAMES__", json.dumps(ORG_NAMES).replace("</", "<\\/"))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/overview":
            self._json(overview())
            return
        if path == "/api/events":
            try:
                to = float((q.get("timeout") or ["25"])[0])
            except ValueError:
                to = 25.0
            self._json(events((q.get("since") or [None])[0], to))
            return
        if path == "/api/analytics":
            self._json(analytics())
            return
        if path == "/api/providers":
            self._json(provider_grid())
            return
        if path == "/api/spend":
            self._json(spend())
            return
        if path == "/api/activity":
            try:
                limit = int((q.get("limit") or ["40"])[0])
            except ValueError:
                limit = 40
            self._json(activity(limit=max(1, min(limit, 500))))
            return
        if path == "/api/workflow":
            self._json(workflow_detail((q.get("id") or [None])[0]))
            return
        if path == "/api/ollama":
            self._json(ollama_state())
            return
        if path == "/api/skills":
            self._json(skills_state())
            return
        if path == "/api/memory":
            self._json(memory_state())
            return
        if path == "/api/mcp":
            self._json(mcp_state())
            return
        if path == "/api/mcp/entry":
            self._json(mcp_entry_raw((q.get("name") or [""])[0],
                                     (q.get("scope") or [""])[0]))
            return
        if path == "/api/projects":
            self._json({"projects": list_projects()})
            return
        if path == "/api/models":
            self._json(models_state())
            return
        if path == "/api/config":
            self._json(config_state())
            return
        if path == "/api/sessions":
            cwd = (q.get("cwd") or [None])[0]
            if not cwd:
                self._json({"error": "cwd query param required"}, 400)
                return
            self._json({"sessions": sessions_for(cwd)})
            return
        if path == "/api/session":
            cwd = (q.get("cwd") or [None])[0]
            sid = (q.get("id") or [None])[0]
            if not cwd or not sid:
                self._json({"error": "cwd and id query params required"}, 400)
                return
            self._json(session_detail(cwd, sid))
            return
        # -- deploy: bring-your-own GPU provider (read) --
        if path == "/api/auth/families":
            self._json(auth_families())
            return
        if path == "/api/auth/methods":
            self._json(auth_family_methods((q.get("family") or [""])[0]))
            return
        if path == "/api/deploy/providers":
            self._json(deploy_providers())
            return
        if path == "/api/deploy/gpus":
            self._json(deploy_gpus((q.get("provider") or [""])[0], (q.get("min_vram") or [None])[0]))
            return
        if path == "/api/deploy/find":
            self._json(deploy_find((q.get("q") or [""])[0], (q.get("provider") or [None])[0],
                                   (q.get("limit") or ["24"])[0], (q.get("agent") or ["1"])[0]))
            return
        if path == "/api/deploy/models":
            self._json(deploy_models((q.get("q") or [""])[0], (q.get("sort") or ["trending"])[0],
                                     (q.get("limit") or ["25"])[0], (q.get("source") or [None])[0]))
            return
        if path == "/api/deploy/models/enrich":
            self._json(deploy_models_enrich((q.get("ids") or [""])[0]))
            return
        if path == "/api/deploy/org-avatar":
            code, body, ctype = org_avatar((q.get("org") or [""])[0])
            self._send(code, body, ctype)
            return
        if path == "/api/deploy/inspect":
            self._json(deploy_inspect((q.get("model") or [""])[0]))
            return
        if path == "/api/deploy/list":
            self._json(deploy_list((q.get("refresh") or ["0"])[0]))
            return
        if path == "/api/deploy/status":
            self._json(deploy_status((q.get("id") or [""])[0]))
            return
        if path == "/api/deploy/logs":
            self._json(deploy_logs((q.get("id") or [""])[0], (q.get("tail") or ["200"])[0]))
            return
        if path == "/api/deploy/usage":
            self._json(deploy_usage((q.get("id") or [""])[0], (q.get("hours") or ["24"])[0]))
            return
        if path == "/api/deploy/job":
            self._json(deploy_job((q.get("id") or [""])[0]))
            return
        if path == "/api/deploy/jobs":
            self._json(deploy_jobs())
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def _lan_ip() -> str:
    """Best-effort primary LAN IP (no traffic actually sent — just picks the
    interface the OS would route out of)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _print_banner(loopback: bool, port: int, suffix: str) -> None:
    """Print the mantis mascot beside the serve info — matches the terminal's
    startup banner. Falls back to plain lines if rich/mascot are unavailable."""
    try:
        from rich.console import Console  # noqa: PLC0415
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        from .tui import BODY, _mascot_lines  # noqa: PLC0415

        console = Console()
        mascot = _mascot_lines(Text)

        title = Text()
        title.append("mantis serve", style=f"bold {BODY}")
        title.append(f"   dashboard · v{_version()}", style="bright_black")
        lines = [title]
        loc = Text()
        loc.append("local    ", style="bright_black")
        loc.append(f"http://127.0.0.1:{port}{suffix}", style="white")
        lines.append(loc)
        if not loopback:
            net = Text()
            net.append("network  ", style="bright_black")
            net.append(f"http://{_lan_ip()}:{port}{suffix}", style="white")
            lines.append(net)
            lines.append(Text("! exposed to your local network — anyone with this "
                              "URL can read your sessions", style="#d8a542"))
        lines.append(Text("ctrl-c to stop", style="bright_black"))

        top = max(0, (len(mascot) - len(lines)) // 2)
        info = [Text("")] * top + lines
        info += [Text("")] * (len(mascot) - len(info))
        grid = Table.grid(padding=(0, 2))
        grid.add_column()
        grid.add_column()
        for i in range(len(mascot)):
            grid.add_row(mascot[i], info[i])
        console.print()
        console.print(grid)
        console.print()
    except Exception:  # noqa: BLE001 — banner is cosmetic, never block startup
        print(f"\n  mantis serve · dashboard · v{_version()}")
        print(f"    local   http://127.0.0.1:{port}{suffix}")
        if not loopback:
            print(f"    network http://{_lan_ip()}:{port}{suffix}")
        print("    ctrl-c to stop\n")


def _apply_settings_env() -> dict[str, str]:
    """settings.json ``env`` into this process, exactly as the terminal does at
    launch: a real shell export wins, empty values (a cleared credential) are
    skipped, and the project/local tiers can't set the guarded variables.

    Without it the dashboard and the terminal disagreed about the same
    machine — a Claude subscription saved by ``mantis setup`` signed the
    terminal in and left every Claude card here saying "not connected"."""
    import os  # noqa: PLC0415

    out: dict[str, str] = {}
    try:
        from .settings import SETTING_SOURCES, load_settings_env_safe  # noqa: PLC0415

        for k, v in (load_settings_env_safe(SETTING_SOURCES) or {}).items():
            if isinstance(k, str) and isinstance(v, str) and v.strip() and not (os.environ.get(k) or "").strip():
                os.environ[k] = v
                out[k] = v
    except Exception:  # noqa: BLE001 — broken settings must not block the dashboard
        pass
    return out


def run_serve(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="mantis serve",
        description="Local web dashboard for your sessions, models, and config.")
    ap.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1 — loopback only)")
    ap.add_argument("--lan", action="store_true",
                    help="bind all interfaces (0.0.0.0) so other devices on your "
                         "network can open it, behind a URL access token")
    ap.add_argument("--no-open", action="store_true",
                    help="don't auto-open a browser")
    args = ap.parse_args(argv)
    _apply_settings_env()

    host = "0.0.0.0" if args.lan else args.host  # noqa: S104 — opt-in LAN bind
    loopback = host in ("127.0.0.1", "localhost", "::1")
    # Always mint a token: writes (save key / connect) require it, and it's
    # inlined into the page so a random web tab can't CSRF your localhost. On a
    # loopback bind, READS stay open (so `curl` and a plain URL still work).
    token = secrets.token_urlsafe(12)

    try:
        httpd = ThreadingHTTPServer((host, args.port), _Handler)
    except OSError as e:
        print(f"mantis serve: can't bind {host}:{args.port} — {e}", file=sys.stderr)
        print("  try a different --port", file=sys.stderr)
        return 1
    httpd.token = token  # type: ignore[attr-defined]
    httpd.enforce_get = not loopback  # type: ignore[attr-defined]
    # DNS-rebinding allowlist: only accept Host headers that name an address we
    # actually bound. Loopback names always allowed; the LAN IP too under --lan.
    host_names = ["127.0.0.1", "localhost", "[::1]", "::1"]
    if not loopback:
        host_names.append(_lan_ip())
        if host not in ("0.0.0.0", "::"):  # noqa: S104 — explicit non-wildcard host
            host_names.append(host)
    allowed_hosts: set[str] = set()
    for name in host_names:
        allowed_hosts.add(name.lower())
        allowed_hosts.add(f"{name}:{args.port}".lower())
    httpd.allowed_hosts = allowed_hosts  # type: ignore[attr-defined]
    httpd.daemon_threads = True

    suffix = "/" if loopback else f"/?k={token}"
    local_url = f"http://127.0.0.1:{args.port}{suffix}"

    _print_banner(loopback, args.port, suffix)
    warm_curated_models()

    if not args.no_open:
        try:
            import webbrowser  # noqa: PLC0415

            webbrowser.open(local_url)
        except Exception:  # noqa: BLE001
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        httpd.server_close()
    return 0
