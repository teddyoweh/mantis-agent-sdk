"""Settings sources — load and persist agent config per source.

Mirrors the Claude Agent SDK ``setting_sources=["user", "project", "local"]``
field, but actually does the work: each named source resolves to a
``settings.json`` on disk, all enabled sources merge in declared order
(later overrides earlier), and the result feeds the agent's defaults so
explicit ``MantisAgentOptions`` kwargs still win.

Source map
----------

``"user"``
    Global, per-user defaults at ``$MANTIS_AGENT_HOME/settings.json`` (the
    same directory used for memory, transcripts, agents). Shared across
    every project the user touches from this machine.

``"project"``
    Project-scoped, intended to be committed: ``<cwd>/.mantis-agent/settings.json``.
    Treat this as the team's shared agent config.

``"local"``
    Project-scoped, intended to be gitignored:
    ``<cwd>/.mantis-agent/settings.local.json``. Personal overrides on top of
    the project file.

The names match Claude Code 1:1 so a user moving between the two tools
doesn't have to relearn the directory layout.

Schema (informal)
-----------------

Any of the following keys can appear in a ``settings.json`` and will be
applied to the agent if the user didn't explicitly pass them through
``MantisAgentOptions``::

    {
      "model": "qwen2.5-7b-instruct",
      "backend": "http://localhost:11434",
      "system_prompt": "Reply tersely.",
      "max_turns": 10,
      "max_tokens": 2048,
      "temperature": 0.2,
      "permission_mode": "default",
      "permissions": {
        "allow": ["Bash(npm install)", "Read"],
        "deny": ["Bash(rm -rf*)"]
      },
      "allowed_tools": ["Bash", "Read"],
      "disallowed_tools": ["WebFetch"],
      "env": {"FOO_API_KEY": "..."},
      "mcp_servers": {"calc": {"command": "python", "args": ["..."]}}
    }

``permissions.allow`` / ``permissions.deny`` are syntactic sugar for the
flat ``allowed_tools`` / ``disallowed_tools`` lists — both are accepted
and merged.

Public API
----------

* :func:`load_setting_source` — load one named source as a plain dict
  (empty when the file is missing). Raises ``ValueError`` on malformed
  JSON; the agent never silently swallows broken settings.
* :func:`save_setting_source` — replace one source's file entirely.
* :func:`update_setting_source` — deep-merge a patch into a source.
* :func:`merge_settings` — deep-merge multiple layers in order.
* :func:`load_settings` — load and merge a list of source names.
* :func:`apply_settings_to_options` — overlay loaded settings *under*
  a query-options dict so user-supplied values keep precedence.
* :func:`resolve_setting_path` — where each source's file lives.

This module never reads ``os.getcwd()`` at import time. Path resolution
is deferred to each call site, so a test that points ``$MANTIS_AGENT_HOME``
at a tmpdir or passes ``cwd=`` gets clean isolation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .paths import get_mantis_agent_dir

__all__ = [
    "KNOWN_SETTING_KEYS",
    "SETTING_SOURCES",
    "apply_settings_to_options",
    "load_setting_source",
    "load_settings",
    "merge_settings",
    "resolve_setting_path",
    "save_setting_source",
    "update_setting_source",
]


# Canonical, ordered list of source names. The order matches the
# Claude Code convention — user is the lowest-priority layer, then
# project, then local. ``load_settings`` honors the order the caller
# passes (so you can pick a subset like ``["user", "local"]``); this
# tuple is just the full canonical sequence + the source of truth for
# "is X a real source name?" validation.
SETTING_SOURCES: tuple[str, ...] = ("user", "project", "local")


# Keys recognized at the top level of settings.json. Anything outside
# this set is preserved on the wire (round-tripped through load/save)
# but ignored by :func:`apply_settings_to_options`. Listing the set
# explicitly here means a typo like ``"max_turn"`` won't silently
# pretend to set ``max_turns`` — the agent simply doesn't see it.
KNOWN_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "backend",
        "system_prompt",
        "max_turns",
        "max_tokens",
        "temperature",
        "permission_mode",
        "permissions",
        "allowed_tools",
        "disallowed_tools",
        "env",
        "mcp_servers",
        "include_memory",
        "max_budget_usd",
    }
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_setting_path(source: str, cwd: str | Path | None = None) -> Path:
    """Return the on-disk path for ``source``.

    ``"user"`` lives under ``$MANTIS_AGENT_HOME`` (resolved via
    :func:`mantis_agent.paths.get_mantis_agent_dir`). The other two live
    under ``<cwd>/.mantis-agent/``; ``cwd`` defaults to ``os.getcwd()``.

    Raises :class:`ValueError` for any name outside :data:`SETTING_SOURCES`
    so a typo bubbles up immediately instead of silently no-op'ing.
    """

    if source == "user":
        return get_mantis_agent_dir() / "settings.json"
    if source not in ("project", "local"):
        raise ValueError(
            f"unknown setting_source: {source!r}. expected one of {SETTING_SOURCES}"
        )
    root = Path(cwd).expanduser().resolve() if cwd else Path(os.getcwd()).resolve()
    filename = "settings.json" if source == "project" else "settings.local.json"
    return root / ".mantis-agent" / filename


# ---------------------------------------------------------------------------
# Load / save / update — one source at a time
# ---------------------------------------------------------------------------


def load_setting_source(source: str, cwd: str | Path | None = None) -> dict[str, Any]:
    """Read one source. Returns ``{}`` if the file does not exist.

    Raises ``ValueError`` (chained from ``JSONDecodeError``) if the file
    exists but is unparseable. A broken settings file is never silently
    swallowed — the user wants to know.
    """

    path = resolve_setting_path(source, cwd)
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"invalid JSON in setting source {source!r} at {path}: {e}"
        ) from e
    if not isinstance(loaded, dict):
        raise ValueError(
            f"setting source {source!r} at {path} must be a JSON object, "
            f"got {type(loaded).__name__}"
        )
    return loaded


def save_setting_source(
    source: str,
    data: Mapping[str, Any],
    cwd: str | Path | None = None,
) -> Path:
    """Overwrite ``source`` with ``data``. Creates the parent directory
    if missing. Returns the path that was written.

    Pretty-prints with ``indent=2`` + trailing newline so the file is
    diff-friendly when committed to a repo.
    """

    path = resolve_setting_path(source, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(data), indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")
    return path


def update_setting_source(
    source: str,
    patch: Mapping[str, Any],
    cwd: str | Path | None = None,
) -> Path:
    """Deep-merge ``patch`` into the existing source and persist.

    The merge follows :func:`merge_settings` semantics: dicts merge
    recursively, lists union (preserving order, deduplicating where
    possible), and scalars replace. Use this when the agent needs to
    record a *delta* without clobbering keys the user wrote by hand.
    """

    current = load_setting_source(source, cwd)
    merged = merge_settings(current, dict(patch))
    return save_setting_source(source, merged, cwd)


# ---------------------------------------------------------------------------
# Multi-source load + merge
# ---------------------------------------------------------------------------


def load_settings(
    sources: Iterable[str],
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Load every source in order and merge them. Later sources win.

    Returns an empty dict if ``sources`` is empty or every source is
    missing. Does *not* validate keys against :data:`KNOWN_SETTING_KEYS`
    here — that's :func:`apply_settings_to_options`'s job.
    """

    layers: list[dict[str, Any]] = []
    for s in sources:
        layers.append(load_setting_source(s, cwd))
    if not layers:
        return {}
    return merge_settings(*layers)


def merge_settings(*layers: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge dict layers in order. Later layers override earlier.

    Rules:
      * Two dicts merge recursively.
      * Two lists union: items from the earlier layer come first, then
        any new items from the later layer (so the user can extend a
        team list without dropping the originals). Dedup is best-effort
        and only triggered for hashable items.
      * Anything else: later wins.

    Pure function — none of the inputs are mutated.
    """

    out: dict[str, Any] = {}
    for layer in layers:
        out = _deep_merge(out, dict(layer))
    return out


def _deep_merge(a: dict[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(a)
    for key, b_val in b.items():
        if key in out:
            a_val = out[key]
            if isinstance(a_val, dict) and isinstance(b_val, Mapping):
                out[key] = _deep_merge(a_val, b_val)
                continue
            if isinstance(a_val, list) and isinstance(b_val, list):
                out[key] = _union_list(a_val, b_val)
                continue
        out[key] = b_val
    return out


def _union_list(a: list[Any], b: list[Any]) -> list[Any]:
    """Best-effort union preserving order: a then any new b items.

    A higher-priority layer can *revoke* an entry an earlier layer added
    by listing it with a leading ``!`` (e.g. project allows ``Bash(*)``,
    a stricter local layer lists ``!Bash(*)`` to drop it). The ``!`` token
    itself never survives into the merged list.

    Falls back to plain concat if items aren't hashable (lists of dicts).
    """

    try:
        # Removal directives ("!X") drop a matching plain "X" from either
        # layer, so a stricter local layer can revoke a broad inherited allow.
        removals = {
            item[1:]
            for item in (*a, *b)
            if isinstance(item, str) and item.startswith("!")
        }
        seen: set[Any] = set()
        result: list[Any] = []
        for item in (*a, *b):
            if isinstance(item, str) and item.startswith("!"):
                continue
            if item in removals or item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result
    except TypeError:
        # Unhashable items (dicts) — concat instead, no dedup.
        return [*a, *b]


# ---------------------------------------------------------------------------
# Overlay onto a query-options dict
# ---------------------------------------------------------------------------


def apply_settings_to_options(
    options: Mapping[str, Any],
    loaded: Mapping[str, Any],
) -> dict[str, Any]:
    """Layer ``loaded`` *under* ``options`` so user-supplied values win.

    For every recognized key in ``KNOWN_SETTING_KEYS``:
      * If the option is missing or "empty" (None, ``[]``, ``{}``), the
        loaded value populates it.
      * If the option is set explicitly, the loaded value is ignored —
        even if the loaded layer would have merged richer content. This
        matches the Claude SDK rule: explicit constructor args always
        beat on-disk settings.

    Two keys get special-cased because their shape doesn't match
    ``options`` 1:1:

    * ``permissions.allow`` and ``permissions.deny`` are flattened into
      ``allowed_tools`` / ``disallowed_tools`` (concatenated with any
      already there).
    * ``env`` is merged into a single env dict on the options (so a
      project-level setting can add API keys without overwriting the
      user-level ones).
    """

    out = dict(options)
    extra: dict[str, Any] = dict(out.get("extra") or {})

    perms = loaded.get("permissions") or {}
    if not isinstance(perms, Mapping):
        raise ValueError(
            "settings 'permissions' must be an object with allow/deny keys, "
            f"got {type(perms).__name__}"
        )
    perm_allow = list(perms.get("allow") or [])
    perm_deny = list(perms.get("deny") or [])

    # Flat keys layered underneath only when the option is genuinely
    # absent. "Absent" means None / missing — NOT merely falsy: an
    # explicit falsy value (e.g. system_prompt="" to clear an inherited
    # prompt) is a deliberate choice and must win over persisted settings.
    for key in ("model", "backend", "system_prompt", "permission_mode"):
        if not loaded.get(key):
            continue
        # The internal options dict uses "system" for system_prompt.
        target = "system" if key == "system_prompt" else key
        if out.get(key) is None and out.get(target) is None:
            out[target] = loaded[key]

    for key in ("max_turns", "max_tokens", "temperature", "max_budget_usd"):
        if out.get(key) in (None,) and loaded.get(key) is not None:
            target = "max_usd" if key == "max_budget_usd" else key
            if out.get(target) in (None,):
                out[target] = loaded[key]

    if "include_memory" in loaded and "include_memory" not in out:
        out["include_memory"] = bool(loaded["include_memory"])

    # MCP servers — merge underneath if the user gave nothing.
    if loaded.get("mcp_servers") and not out.get("mcp_servers"):
        out["mcp_servers"] = loaded["mcp_servers"]

    # allowed_tools / disallowed_tools live on extra. Patterns from
    # permissions.allow / permissions.deny are folded in so the user can
    # use either spelling in settings.json.
    settings_allowed = list(loaded.get("allowed_tools") or []) + perm_allow
    settings_disallowed = list(loaded.get("disallowed_tools") or []) + perm_deny
    if settings_allowed:
        cur = list(extra.get("allowed_tools") or [])
        # setting_sources is an opt-in, trusted on-disk config the caller
        # explicitly enabled — it is meant to CONTRIBUTE allowed_tools, not
        # be capped by whatever the caller also passed. Union (settings
        # first, existing extra appended without dup), mirroring how env and
        # permissions layers add to — rather than intersect with — caller
        # values elsewhere in this function.
        extra["allowed_tools"] = _union_list(settings_allowed, cur)
    if settings_disallowed:
        cur = list(extra.get("disallowed_tools") or [])
        # Denials only ever restrict, so unioning persisted + explicit
        # denials is safe — it can only narrow, never broaden, access.
        extra["disallowed_tools"] = _union_list(settings_disallowed, cur)

    # env — merge under so the user can override individual keys.
    if loaded.get("env"):
        env_val = loaded["env"]
        if not isinstance(env_val, Mapping):
            raise ValueError(
                "settings 'env' must be an object of NAME: value pairs, "
                f"got {type(env_val).__name__}"
            )
        merged_env = dict(env_val)
        merged_env.update(extra.get("env") or {})
        extra["env"] = merged_env

    if extra:
        out["extra"] = extra
    return out
