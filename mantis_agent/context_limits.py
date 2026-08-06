"""Learn a model's real context ceiling from the provider that enforces it.

Every context window in :mod:`.capabilities` is our own guess, and a guess that
is too *large* does not fail politely — it disables the safety net. The
compactor only fires at a fraction of the window it is told about, so if we
believe a model holds 128k while the endpoint enforces 8k, compaction never
triggers, the prompt is rejected, and the emergency retry re-compacts against
the same wrong budget and re-sends something still too big. The session wedges:
every following message overflows too, including a manual ``/compact``.

That is not hypothetical. Cerebras serves ``zai-glm-4.7`` with an 8192-token
limit on the free tier while advertising ``max_context_length: 131072`` in its
own public catalog — so provider *metadata* cannot be trusted either. The one
authority is the error the endpoint returns when it refuses:

    Please reduce the length of the messages or completion.
    Current length is 14789 while limit is 8192

That number is ground truth. Parse it, remember it, and every later turn plans
against the real ceiling. Being error-driven, this works for any provider and
any model without a table to maintain — including ones that do not exist yet,
and tiers we have no way to detect up front.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from .paths import get_mantis_agent_dir

# Ordered most-specific first. Each pattern must capture the LIMIT, never the
# "current length" — several providers put both numbers in one sentence, and
# picking the wrong one would raise the ceiling instead of lowering it.
_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI-compatible (Cerebras, Groq, Fireworks, …):
    #   "Current length is 14789 while limit is 8192"
    re.compile(r"while\s+limit\s+is\s+(\d+)", re.I),
    # OpenAI: "This model's maximum context length is 8192 tokens. However, …"
    re.compile(r"maximum\s+context\s+length\s+is\s+(\d+)", re.I),
    # Anthropic: "prompt is too long: 210000 tokens > 200000 maximum"
    re.compile(r">\s*(\d+)\s*maximum", re.I),
    # TGI / Together: "`inputs` tokens + `max_new_tokens` must be <= 8192"
    re.compile(r"must\s+be\s+<=\s*(\d+)", re.I),
    # Vertex / misc: "input tokens exceed the configured limit of 32768"
    re.compile(r"configured\s+limit\s+of\s+(\d+)", re.I),
    # Generic trailing forms: "context limit: 8192", "limit of 8192 tokens"
    re.compile(r"context\s+(?:window|length|limit)\s*(?:of|is|:)\s*(\d+)", re.I),
)

# Below this a "limit" is certainly not a context window — it is a max_tokens, a
# rate-limit count, or a stray number. Clamping a window to something tiny would
# make every request fail, which is worse than the bug being fixed.
MIN_CREDIBLE_LIMIT = 1024

_lock = threading.Lock()
_cache: dict[str, int] | None = None


def parse_limit(text: object) -> int | None:
    """The enforced token ceiling stated in a provider error, or None.

    Returns None rather than guessing: a wrong number here silently caps every
    future request on that model.
    """

    s = str(text)
    if not s:
        return None
    for pat in _LIMIT_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        try:
            value = int(m.group(1))
        except (TypeError, ValueError):  # pragma: no cover — \d+ is always int-able
            continue
        if value >= MIN_CREDIBLE_LIMIT:
            return value
    return None


def _path() -> Any:
    return get_mantis_agent_dir() / "context_limits.json"


def _load() -> dict[str, int]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        data = {str(k): int(v) for k, v in (raw.get("limits") or {}).items()
                if isinstance(v, (int, float)) and int(v) >= MIN_CREDIBLE_LIMIT}
    except Exception:  # noqa: BLE001 — absent or corrupt: start empty
        data = {}
    _cache = data
    return data


def _key(model: str, backend: str | None = None) -> str:
    """Identity for a learned limit.

    Keyed by backend host as well as model: the same model id is served by many
    providers at different tiers, and ``zai-glm-4.7`` capped at 8k on one
    endpoint says nothing about the same model elsewhere.
    """

    host = ""
    if backend:
        m = re.search(r"//([^/]+)", str(backend))
        host = (m.group(1) if m else str(backend)).lower()
    return f"{host}::{model}" if host else str(model)


def learned_limit(model: str, backend: str | None = None) -> int | None:
    """The ceiling we have actually seen enforced for this model, if any.

    Falls back to an endpoint-less entry when the host-scoped one is missing.
    The endpoint is not always known at the moment of failure — the TUI hands
    the agent a constructed provider and leaves ``backend`` unset — and a limit
    recorded bare must still be honoured once the same model is reached through
    a named endpoint. Erring toward the smaller window costs a little headroom;
    erring the other way is the failure this module exists to prevent.
    """

    data = _load()
    if backend:
        hit = data.get(_key(model, backend))
        if hit:
            return hit
    return data.get(_key(model))


def record_limit(model: str, limit: int, backend: str | None = None) -> bool:
    """Remember an enforced ceiling. Returns True if this changed anything.

    Keeps the SMALLEST value ever observed. A limit is a hard ceiling, so a
    later, larger number is either a different tier or a differently-worded
    error — and optimism here costs a failed turn, while pessimism costs a
    little headroom.
    """

    if limit < MIN_CREDIBLE_LIMIT:
        return False
    key = _key(model, backend)
    with _lock:
        data = _load()
        if key in data and data[key] <= limit:
            return False
        data[key] = int(limit)
        try:
            path = _path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"limits": data}, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:  # noqa: BLE001 — in-memory value still helps this session
            pass
    return True


def effective_window(model: str, declared: int, backend: str | None = None) -> int:
    """The window to plan against: our table, lowered by anything we have seen
    the endpoint actually enforce."""

    seen = learned_limit(model, backend)
    if not seen:
        return declared
    return seen if declared <= 0 else min(declared, seen)


def forget(model: str, backend: str | None = None) -> None:
    """Drop a learned limit (used by tests and after a tier change)."""

    with _lock:
        _load().pop(_key(model, backend), None)


def _reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


__all__ = [
    "MIN_CREDIBLE_LIMIT",
    "parse_limit",
    "learned_limit",
    "record_limit",
    "effective_window",
    "forget",
]
