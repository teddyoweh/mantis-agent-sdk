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

import json
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
    "load_mcp_server_configs",
    "parse_server_entry",
]


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


def load_mcp_server_configs(cwd: str | Path | None = None) -> dict[str, ServerConfig]:
    """Merged ``{name: ServerConfig}`` from settings.json → user mcp.json →
    project .mcp.json (later wins by name). Malformed entries are dropped."""
    from ..paths import get_mantis_agent_dir  # noqa: PLC0415

    base = Path(cwd) if cwd is not None else Path.cwd()
    raw: dict[str, Any] = {}
    # settings.json mcpServers (lowest priority)
    try:
        from ..settings import SETTING_SOURCES, load_settings  # noqa: PLC0415
        s = (load_settings(SETTING_SOURCES) or {}).get("mcpServers")
        if isinstance(s, dict):
            raw.update(s)
    except Exception:  # noqa: BLE001
        pass
    raw.update(_read_mcp_file(get_mantis_agent_dir() / "mcp.json"))
    raw.update(_read_mcp_file(base / ".mcp.json"))

    out: dict[str, ServerConfig] = {}
    for name, entry in raw.items():
        cfg = parse_server_entry(entry)
        if cfg is not None and isinstance(name, str) and name.strip():
            out[name.strip()] = cfg
    return out


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
            except Exception as e:  # noqa: BLE001 — isolate per server
                self.errors[name] = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            adapted: list[Tool] = []
            for rt in remote:
                t = rt.to_mantis_agent_tool(client)
                # Namespace like Claude Code so two servers' `search` tools
                # can't collide and the model can tell where a tool lives.
                t.name = f"mcp__{name}__{rt.name}"
                adapted.append(t)
            self.clients[name] = client
            self.tools[name] = adapted
            all_tools.extend(adapted)
        return all_tools

    def status_rows(self) -> list[dict[str, str]]:
        """One row per configured server for the ``/mcp`` renderer."""
        rows: list[dict[str, str]] = []
        for name, cfg in self.configs.items():
            if name in self.clients:
                state, detail = "connected", f"{len(self.tools.get(name) or [])} tools"
            elif name in self.errors:
                state, detail = "failed", self.errors[name]
            else:
                state, detail = "pending", ""
            rows.append({"name": name, "transport": _transport_label(cfg),
                         "state": state, "detail": detail})
        return rows

    def summary(self) -> str:
        """One-line startup summary: ``github (5 tools) · db (2 tools) · slack ✗``."""
        parts = [f"{n} ({len(ts)} tools)" for n, ts in self.tools.items()]
        parts += [f"{n} ✗" for n in self.errors]
        return " · ".join(parts)

    async def aclose(self) -> None:
        for client in self.clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        self.clients.clear()

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
        import asyncio  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        self._stop_event = asyncio.Event()

        async def _runner() -> None:
            try:
                tools = await self.connect_all()
            except Exception as e:  # noqa: BLE001
                if not ready.done():
                    ready.set_exception(e)
                return
            if not ready.done():
                ready.set_result(tools)
            await self._stop_event.wait()
            await self.aclose()

        self._runner = asyncio.ensure_future(_runner())
        return await ready

    async def stop(self) -> None:
        """Close every server (from any task). Idempotent."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._runner is not None:
            try:
                await self._runner
            except Exception:  # noqa: BLE001
                pass
            self._runner = None
