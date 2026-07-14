"""Filesystem layout for mantis-agent-sdk persistent state.

Mirrors Claude Code's ``~/.claude/`` directory 1:1 so a user's mental
model — and any tooling that introspects the on-disk state — carries over
unchanged. Anything Claude stores under ``.claude/X`` lives under
``.mantis-agent/X`` here.

Layout
------

    ~/.mantis-agent/
      settings.json              global settings (permission rules, hooks…)
      MEMORY.md                  top-level memory index (loaded every session)
      memory/                    individual memory entries
        {slug}.md                  one entry, frontmatter + markdown body
        {topic}/                   branched topic (promoted once 3+ entries)
          INDEX.md                   sub-index, same format as MEMORY.md
        sessions/                  per-session digests (auto-written; read-only)
          {date}-{id}.md
      sessions/                  full session transcripts
        {session_id}.jsonl         one JSON-encoded SDKMessage per line
      projects/                  per-cwd state
        {path_hash}/
          session-state.json       last-resumable session id, mode, etc.
      agents/                    user-defined agents
        {name}.md                  agent system prompt + tool list

Overrides
---------

* ``$MANTIS_AGENT_HOME`` — base dir (default ``~/.mantis-agent``).
* ``$MANTIS_AGENT_PROJECT_ROOT`` — explicit project root to derive ``projects/{hash}``
  from. Defaults to the current working directory.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Final

__all__ = [
    "ensure_dir",
    "get_agents_dir",
    "get_mantis_agent_dir",
    "get_memory_dir",
    "get_memory_index",
    "get_project_dir",
    "get_session_path",
    "get_sessions_dir",
    "get_settings_path",
    "iter_sessions",
    "normalize_base_url",
    "ollama_base_url",
    "sanitize_session_id",
]


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


_DEFAULT_DIRNAME: Final = ".mantis-agent"


def get_mantis_agent_dir() -> Path:
    """Return the resolved ``~/.mantis-agent`` directory.

    Honors ``$MANTIS_AGENT_HOME`` for portable installs / containerized runs.
    The directory is *not* created here — call :func:`ensure_dir` on the
    specific subdir you need (lazy creation keeps test isolation clean).
    """

    override = os.environ.get("MANTIS_AGENT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / _DEFAULT_DIRNAME


# ---------------------------------------------------------------------------
# Subdirectories
# ---------------------------------------------------------------------------


def get_settings_path() -> Path:
    return get_mantis_agent_dir() / "settings.json"


def get_memory_index() -> Path:
    """The single ``MEMORY.md`` file at the root. Always loaded by the agent
    at session start (cheap; capped at ~150 lines by convention)."""
    return get_mantis_agent_dir() / "MEMORY.md"


def get_memory_dir() -> Path:
    return get_mantis_agent_dir() / "memory"


def get_sessions_dir() -> Path:
    return get_mantis_agent_dir() / "sessions"


def get_agents_dir() -> Path:
    return get_mantis_agent_dir() / "agents"


def get_project_dir(cwd: str | Path | None = None) -> Path:
    """Per-project state dir, keyed by a stable hash of the project root.

    Honors ``$MANTIS_AGENT_PROJECT_ROOT`` then falls back to ``cwd`` then
    ``os.getcwd()``. We hash the absolute path so two projects with the
    same basename don't collide.
    """

    explicit = os.environ.get("MANTIS_AGENT_PROJECT_ROOT")
    root = Path(explicit or cwd or os.getcwd()).expanduser().resolve()
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    return get_mantis_agent_dir() / "projects" / digest


# ---------------------------------------------------------------------------
# Session files
# ---------------------------------------------------------------------------


def get_session_path(session_id: str) -> Path:
    """Path of one session's JSONL transcript."""
    return get_sessions_dir() / f"{sanitize_session_id(session_id)}.jsonl"


_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_session_id(session_id: str) -> str:
    """Strip filesystem-hostile characters from a user-supplied session id.

    Real session ids are UUIDs so this is a defense in depth: callers can
    pass `"my-session-2026-05-16"` and it will land at a predictable path.
    """

    return _SAFE_ID.sub("_", session_id) or "_"


def iter_sessions() -> Iterable[Path]:
    """Yield every persisted session JSONL file in chronological order
    (by mtime). Skips anything that doesn't end in ``.jsonl``."""

    d = get_sessions_dir()
    if not d.exists():
        return
    paths = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    yield from paths


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def ensure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) if missing. Returns ``path`` for chaining."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def ollama_base_url() -> str:
    """The local Ollama base URL, honoring ``$OLLAMA_HOST`` (users who run Ollama
    on a custom host/port — remote GPU box, non-default port, Docker) — else the
    default ``http://localhost:11434``. OLLAMA_HOST may be ``host``, ``host:port``,
    or a full URL; normalize all three."""
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return "http://localhost:11434"
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415
    parsed = urlparse(host)
    if parsed.port is None:
        # Inject the default port into the authority only — never after a path,
        # which would malform the URL (e.g. a reverse-proxied host/ollama subpath
        # would otherwise become host/ollama:11434).
        parsed = parsed._replace(netloc=f"{parsed.netloc}:11434")
        host = urlunparse(parsed)
    host = host.rstrip("/")
    # 0.0.0.0 is a *bind* address (OLLAMA_HOST=0.0.0.0 exposes the server on all
    # interfaces); as a client connect target it's unroutable, so rewrite it to
    # loopback — exactly what Ollama's own client does.
    return host.replace("://0.0.0.0", "://127.0.0.1")


# Endpoint paths a user might paste onto the *base* URL by mistake (copying from
# a curl example). We build these ourselves, so strip a trailing one to recover
# the real base — e.g. '.../v1/chat/completions' → '.../v1'.
_ENDPOINT_SUFFIXES = (
    "/chat/completions", "/completions", "/messages", "/responses", "/embeddings",
)


def normalize_base_url(url: str) -> str:
    """Clean a user-entered base URL: strip whitespace and a trailing OpenAI/
    Anthropic endpoint path pasted by mistake, so ``{base}/chat/completions``
    resolves correctly instead of doubling up. Leaves a proper base untouched."""
    if not url:
        return url
    u = url.strip().rstrip("/")
    low = u.lower()
    for suffix in _ENDPOINT_SUFFIXES:
        if low.endswith(suffix):
            return u[: -len(suffix)]
    return u
