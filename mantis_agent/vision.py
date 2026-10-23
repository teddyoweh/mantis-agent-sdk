"""Can this model see images? Answered from evidence before guesses.

1. What this machine has watched the endpoint do: a request carrying an image
   that succeeded means yes, an image refusal ("does not support image
   inputs") means no. The engine records both, and they are kept on disk.
2. What the provider publishes, where it does: Fireworks' model record carries
   ``supportsImageInput``. It's looked up once, in the background, never on
   the path of a keystroke.
3. The name markers in ``tui.model_supports_vision``, the last resort.

A name list alone said GLM 5.3 Flash couldn't see images, and it can.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

_lock = threading.Lock()
_verdicts: dict[str, bool] | None = None
_looking: set[str] = set()

_FIREWORKS_ID = re.compile(r"accounts/([a-z0-9-]+)/models/([a-z0-9._-]+)")


def _path() -> Path:
    from .paths import get_mantis_agent_dir  # noqa: PLC0415

    return get_mantis_agent_dir() / "vision.json"


def _key(model: str) -> str:
    return (model or "").strip().lower()


def _load() -> dict[str, bool]:
    global _verdicts
    if _verdicts is None:
        try:
            raw = json.loads(_path().read_text(encoding="utf-8"))
            _verdicts = {str(k): bool(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            _verdicts = {}
    return _verdicts


def known(model: str) -> bool | None:
    """The recorded verdict for ``model``, or ``None`` when nothing is known."""
    with _lock:
        return _load().get(_key(model))


def record(model: str, sees_images: bool) -> None:
    """Remember what the endpoint did. Best-effort: a read-only home just means
    it is learned again next time."""
    key = _key(model)
    if not key:
        return
    with _lock:
        verdicts = _load()
        if verdicts.get(key) is sees_images:
            return
        verdicts[key] = sees_images
        try:
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(verdicts, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            pass


def published(model: str) -> bool | None:
    """What the provider says, for the providers that say: a Fireworks model
    id (``accounts/<acct>/models/<slug>``) → its ``supportsImageInput``.
    ``None`` when the provider doesn't publish it or can't be reached."""
    m = _FIREWORKS_ID.search(_key(model))
    if m is None:
        return None
    try:
        import httpx  # noqa: PLC0415

        from .deploy.serverless import _fireworks_key  # noqa: PLC0415

        key = _fireworks_key()
        if not key:
            return None
        r = httpx.get(f"https://api.fireworks.ai/v1/accounts/{m.group(1)}/models/{m.group(2)}",
                      headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if r.status_code != 200:
            return None
        flag = r.json().get("supportsImageInput")
        return flag if isinstance(flag, bool) else None
    except Exception:  # noqa: BLE001 — an outage is "unknown", never an error
        return None


def look_up(model: str) -> bool:
    """Start the background lookup of what the provider publishes for
    ``model``, once per process. True while one is running or was started
    for it now, meaning the answer may still come; False when the provider
    publishes nothing mantis can read."""
    key = _key(model)
    if not _FIREWORKS_ID.search(key):
        return False
    with _lock:
        if key in _looking:
            return True
        _looking.add(key)

    def run() -> None:
        flag = published(model)
        if flag is not None:
            record(model, flag)

    threading.Thread(target=run, name="mantis-vision-lookup", daemon=True).start()
    return True


def _reset_for_tests() -> None:
    global _verdicts
    with _lock:
        _verdicts = None
        _looking.clear()


__all__ = ["known", "look_up", "published", "record"]
