"""``mantis serve`` — a local, read-only web dashboard over your mantis state.

One command spins up a tiny stdlib HTTP server (no extra deps) that serves a
single self-contained page showing:

* **Sessions** — every conversation across every project on this machine,
  grouped by project, drilling into the full transcript.
* **Models & hosting** — which providers are enabled, the current model /
  backend, recent models, and each provider's model list.
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
        out.append({
            "digest": d.name,
            "cwd": cwd,
            "name": Path(cwd).name if cwd else d.name,
            "path": cwd or str(d),
            "session_count": len(sessions),
            "last_activity": last,
        })
    out.sort(key=lambda p: p["last_activity"], reverse=True)
    return out


def sessions_for(cwd: str) -> list[dict[str, Any]]:
    from . import session_tree  # noqa: PLC0415

    infos = session_tree.list_sessions(cwd=cwd)
    return [{
        "session_id": s.session_id,
        "title": s.title,
        "first_prompt": s.first_prompt,
        "last_prompt": s.last_prompt,
        "modified_at": s.modified_at,
        "message_count": s.message_count,
    } for s in infos]


def session_detail(cwd: str, session_id: str) -> dict[str, Any]:
    """The full reconstructed transcript as plain JSON-able dicts.

    ``role`` is read off each typed ``Message`` object explicitly — the structs
    use ``omit_defaults=True`` so msgspec drops ``role`` (it equals its only
    value), which would leave the UI unable to tell user from assistant. The
    content blocks are msgspec-encoded so each keeps its ``type`` discriminator.
    """
    import msgspec  # noqa: PLC0415

    from . import session_tree  # noqa: PLC0415

    messages = session_tree.load_for_resume(session_id, cwd=cwd)
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.content
        enc = content if isinstance(content, str) else \
            msgspec.json.decode(msgspec.json.encode(content))
        item: dict[str, Any] = {"role": getattr(m, "role", "assistant"), "content": enc}
        if getattr(m, "isMeta", False):
            item["isMeta"] = True
        out.append(item)
    return {"session_id": session_id, "cwd": cwd, "messages": out}


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
    try:
        recent = catalog.get_recent_models()
    except Exception:  # noqa: BLE001
        recent = []
    backend_now = (last.get("backend") or "").rstrip("/")

    provs: list[dict[str, Any]] = []
    for g in groups:
        pid = g.get("provider_id")
        prov = catalog.BY_ID.get(pid)
        try:
            live = catalog.cached_live_models(pid) if pid else None
        except Exception:  # noqa: BLE001
            live = None
        host = _provider_hosting(prov) if prov else {}
        provs.append({
            "id": pid,
            "label": g.get("label"),
            "enabled": bool(g.get("enabled")),
            "note": g.get("note") or "",
            "models": list(g.get("models") or ()),
            "model_count": len(g.get("models") or ()),
            "live_count": len(live) if live else 0,
            "base_url": host.get("base_url"),
            "api_key_env": host.get("api_key_env"),
            "key_masked": host.get("key_masked"),
            "key_source": host.get("key_source"),
            "is_current": bool(prov and prov.base_url.rstrip("/") == backend_now),
            "guide": provider_guides.GUIDES.get(pid),
            "docs_url": f"{_DOCS_BASE}/providers/{pid}" if pid else None,
        })
    # What each model can actually do, straight from the SDK's own capability
    # table — a model list is just strings until you can compare context
    # windows and tool support side by side.
    info: dict[str, Any] = {}
    seen: set[str] = set()
    for p in provs:
        for mid in p["models"]:
            if mid in seen:
                continue
            seen.add(mid)
            info[mid] = _model_info(mid)
    cur_model = last.get("model")
    if cur_model and cur_model not in info:
        info[cur_model] = _model_info(cur_model)

    return {
        "current": last,
        "recent": recent,
        "providers": provs,
        "model_info": info,
        "enabled_count": sum(1 for p in provs if p["enabled"]),
        "hosting": _hosting_summary(last, backend_now),
        "selfhost_guide": provider_guides.SELFHOST,
        "selfhost_docs_url": f"{_DOCS_BASE}/guides/self-hosting",
    }


def _model_info(model_id: str) -> dict[str, Any]:
    """Context window + tool/reasoning support for one model id."""
    try:
        from .capabilities import lookup_model  # noqa: PLC0415

        cap = lookup_model(model_id)
        return {
            "ctx": cap.context_window,
            "tools": bool(cap.supports_native_tools),
            "effort": bool(cap.supports_reasoning_effort),
            "thinking": bool(cap.emits_thinking_blocks or cap.emits_inline_thinking),
            "family": cap.family,
        }
    except Exception:  # noqa: BLE001 — an unknown model just shows no badges
        return {}


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
    return {
        "name": (meta.get("name") or md.parent.name).strip(),
        "slug": md.parent.name,
        "description": meta.get("description", ""),
        "category": meta.get("category"),
        "always_load": str(meta.get("always_load", "")).lower() in ("1", "true", "yes"),
        "body": body,
        "path": short_path(md),
    }


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

    return {"global": scan(g), "project": scan(p),
            "global_dir": short_path(g), "project_dir": short_path(p),
            "cwd": os.getcwd()}


def _slugify(name: str) -> str:
    import re  # noqa: PLC0415

    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-") or "skill"


def _skill_md(name: str, description: str, body: str,
              category: str = "", always_load: bool = False) -> str:
    """Render a SKILL.md. Front-matter keys are only written when set, so a
    round-trip through the editor doesn't sprout empty fields."""
    lines = [f"name: {name}", f"description: {(description or '').strip()}"]
    if (category or "").strip():
        lines.append(f"category: {category.strip()}")
    if always_load:
        lines.append("always_load: true")
    return "---\n" + "\n".join(lines) + "\n---\n\n" + (body or "").strip() + "\n"


def add_skill(scope: str, name: str, description: str, body: str,
              category: str = "", always_load: bool = False,
              slug: str | None = None) -> dict[str, Any]:
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
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        _skill_md(name, description, body, category, always_load), encoding="utf-8")
    return {"ok": True, "scope": scope, "slug": target_slug}


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
            result.update(ok=True, tools=[{"name": t.name, "description": (t.description or "")[:220]}
                                          for t in tools])
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
    total = user_m = asst_m = tool_calls = 0
    sessions = 0
    first_ts: float | None = None
    last_ts: float | None = None

    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            cwd = _project_cwd(d)
            pname = Path(cwd).name if cwd else d.name
            proj = projects.setdefault(d.name, {"name": pname, "cwd": cwd,
                                                "sessions": 0, "msgs": 0, "tools": 0})
            for f in d.glob("*.jsonl"):
                had_msg = False
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
                            if typ not in ("user", "assistant"):
                                continue
                            had_msg = True
                            total += 1
                            proj["msgs"] += 1
                            if typ == "user":
                                user_m += 1
                            else:
                                asst_m += 1
                            content = (obj.get("message") or {}).get("content")
                            n_tools = 0
                            if isinstance(content, list):
                                for b in content:
                                    if isinstance(b, dict) and b.get("type") == "tool_use":
                                        nm = b.get("name") or "?"
                                        tools[nm] = tools.get(nm, 0) + 1
                                        n_tools += 1
                            tool_calls += n_tools
                            proj["tools"] += n_tools
                            ts = obj.get("timestamp")
                            if ts:
                                try:
                                    dt = datetime.fromisoformat(
                                        ts.replace("Z", "+00:00")).astimezone()
                                except ValueError:
                                    continue
                                key = dt.strftime("%Y-%m-%d")
                                slot = daily.setdefault(key, {"msgs": 0, "tools": 0})
                                slot["msgs"] += 1
                                slot["tools"] += n_tools
                                by_hour[dt.hour] += 1
                                by_weekday[dt.weekday()] += 1
                                punchcard[dt.weekday()][dt.hour] += 1
                                ep = dt.timestamp()
                                first_ts = ep if first_ts is None else min(first_ts, ep)
                                last_ts = ep if last_ts is None else max(last_ts, ep)
                except OSError:
                    continue
                if had_msg:
                    sessions += 1
                    proj["sessions"] += 1

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
        },
        "daily": daily,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "punchcard": punchcard,
        "top_tools": top_tools,
        "top_projects": top_projects,
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
    }


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
            if path == "/api/key":
                self._json(set_provider_key(body.get("provider"), body.get("key")))
                return
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
                                     body.get("slug")))
                return
            if path == "/api/skill/delete":
                self._json(delete_skill(body.get("scope"), body.get("slug")))
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
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/overview":
            self._json(overview())
            return
        if path == "/api/analytics":
            self._json(analytics())
            return
        if path == "/api/skills":
            self._json(skills_state())
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
