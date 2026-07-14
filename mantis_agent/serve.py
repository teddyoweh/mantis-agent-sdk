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
    return {
        "current": last,
        "recent": recent,
        "providers": provs,
        "enabled_count": sum(1 for p in provs if p["enabled"]),
        "hosting": _hosting_summary(last, backend_now),
        "selfhost_guide": provider_guides.SELFHOST,
        "selfhost_docs_url": f"{_DOCS_BASE}/guides/self-hosting",
    }


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
        "path": str(md),
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
            "global_dir": str(g), "project_dir": str(p), "cwd": os.getcwd()}


def _slugify(name: str) -> str:
    import re  # noqa: PLC0415

    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-") or "skill"


def add_skill(scope: str, name: str, description: str, body: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    g, p = _skills_dirs()
    base = p if scope == "project" else g
    slug = _slugify(name)
    d = base / slug
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {(description or '').strip()}\n---\n\n{(body or '').strip()}\n"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")
    return {"ok": True, "scope": scope, "slug": slug}


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


def mcp_state() -> dict[str, Any]:
    import os  # noqa: PLC0415

    from . import paths  # noqa: PLC0415

    settings_servers: dict[str, Any] = {}
    try:
        from .settings import SETTING_SOURCES, load_settings  # noqa: PLC0415
        settings_servers = (load_settings(SETTING_SOURCES) or {}).get("mcpServers") or {}
    except Exception:  # noqa: BLE001
        pass
    gfile = paths.get_mantis_agent_dir() / "mcp.json"
    pfile = Path(os.getcwd()) / ".mcp.json"
    merged: dict[str, dict[str, Any]] = {}
    for scope, servers in (("settings", settings_servers), ("global", _read_mcp(gfile)),
                           ("project", _read_mcp(pfile))):
        for name, entry in (servers or {}).items():
            merged[name] = {"name": name, "scope": scope, **_entry_summary(entry)}
    return {"servers": list(merged.values()), "global_file": str(gfile),
            "project_file": str(pfile), "cwd": os.getcwd()}


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
        "top_tools": top_tools,
        "top_projects": top_projects,
    }


def overview() -> dict[str, Any]:
    projects = list_projects()
    m = models_state()
    return {
        "version": _version(),
        "home": str(_base_dir()),
        "current": m["current"],
        "project_count": len(projects),
        "session_count": sum(p["session_count"] for p in projects),
        "enabled_providers": m["enabled_count"],
        "provider_count": len(m["providers"]),
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
                                     body.get("description"), body.get("body")))
                return
            if path == "/api/skill/delete":
                self._json(delete_skill(body.get("scope"), body.get("slug")))
                return
            if path == "/api/mcp":
                self._json(add_mcp(body.get("scope"), body.get("name"), body.get("entry")))
                return
            if path == "/api/mcp/delete":
                self._json(delete_mcp(body.get("scope"), body.get("name")))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _route(self, path: str, q: dict[str, list[str]]) -> None:
        if path in ("/", "/index.html"):
            from .serve_ui import INDEX_HTML  # noqa: PLC0415

            html = INDEX_HTML.replace("__TOKEN__", getattr(self.server, "token", "") or "")
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
