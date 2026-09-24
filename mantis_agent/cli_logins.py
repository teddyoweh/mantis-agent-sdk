"""Logins other coding CLIs already hold on this machine — Claude Code, Codex.

A user who has run ``codex login`` or signed in to Claude Code should not have
to set anything up again for mantis to notice. This module answers two
questions, offline and without ever printing a secret:

* **Is the CLI here, and is it signed in?** :func:`detect_claude_code` and
  :func:`detect_codex` report that for the auth status surfaces.
* **Can mantis use that login?** Codex keeps its credential in
  ``~/.codex/auth.json``, either an ``OPENAI_API_KEY`` (a plain platform key,
  used as one — :func:`codex_api_key`) or a ChatGPT sign-in whose tokens reach
  the ChatGPT Codex backend (:data:`CHATGPT_CODEX_URL`) through the Responses
  API. :func:`chatgpt_access_token` refreshes that token when it is about to
  expire and writes the rotated pair back in Codex's own format, so the Codex
  CLI keeps working.

Claude Code's login is detected but never read: mantis signs in to a Claude
subscription itself (``mantis-agent auth login claude``), so the status only
points the user there instead of lifting another app's token.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "CHATGPT_CODEX_URL",
    "chatgpt_access_token",
    "chatgpt_account_id",
    "chatgpt_headers",
    "codex_api_key",
    "codex_auth_path",
    "codex_client_version",
    "codex_models",
    "detect_claude_code",
    "detect_codex",
    "has_chatgpt_login",
    "is_chatgpt_codex_url",
]

#: Where a ChatGPT sign-in is served — the Codex CLI's own backend. Speaks the
#: OpenAI Responses API (streaming only, ``store: false``).
CHATGPT_CODEX_URL = "https://chatgpt.com/backend-api/codex"

_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Codex's public OAuth client id. The access token carries it as its
# ``client_id`` claim, which is preferred; this is the fallback.
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# Refresh this long before expiry so a token never lapses mid-turn.
_REFRESH_SKEW_S = 300

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def codex_auth_path() -> Path:
    """``$CODEX_HOME/auth.json`` (``~/.codex`` by default) — Codex's own rule."""

    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home).expanduser() / "auth.json"


def _read_codex_auth() -> dict[str, Any] | None:
    try:
        data = json.loads(codex_auth_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def codex_api_key() -> str | None:
    """The platform API key ``codex login --with-api-key`` stored, or None."""

    data = _read_codex_auth() or {}
    key = data.get("OPENAI_API_KEY")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _tokens(data: dict[str, Any] | None) -> dict[str, Any] | None:
    tokens = (data or {}).get("tokens")
    if isinstance(tokens, dict) and tokens.get("access_token"):
        return tokens
    return None


def has_chatgpt_login() -> bool:
    """True when Codex holds a ChatGPT sign-in mantis can route through.

    ``MANTIS_DISABLE_CODEX_LOGIN=1`` turns the whole route off, for a user who
    wants the Codex CLI's subscription left alone.
    """

    if os.environ.get("MANTIS_DISABLE_CODEX_LOGIN", "").strip() not in ("", "0"):
        return False
    return _tokens(_read_codex_auth()) is not None


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def chatgpt_account_id() -> str | None:
    """The ChatGPT workspace the token belongs to — sent as a header."""

    tokens = _tokens(_read_codex_auth())
    if tokens is None:
        return None
    account = tokens.get("account_id")
    if isinstance(account, str) and account:
        return account
    auth = _jwt_claims(str(tokens.get("access_token"))).get("https://api.openai.com/auth") or {}
    account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return account if isinstance(account, str) and account else None


def _plan_type(tokens: dict[str, Any]) -> str | None:
    auth = _jwt_claims(str(tokens.get("access_token"))).get("https://api.openai.com/auth") or {}
    plan = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
    return plan if isinstance(plan, str) and plan else None


def _expires_at(token: str) -> float | None:
    exp = _jwt_claims(token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def _write_codex_auth(data: dict[str, Any]) -> None:
    """Atomic, 0600 — the file holds a refresh token."""

    path = codex_auth_path()
    fd, tmp = tempfile.mkstemp(prefix=".auth.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _refresh(data: dict[str, Any], tokens: dict[str, Any]) -> dict[str, Any]:
    """Trade the refresh token for a new pair and persist it the way Codex does.

    Refresh tokens rotate: once this call succeeds the old one is dead, so the
    new pair MUST be written back to ``auth.json`` or the Codex CLI itself is
    logged out. Other keys in the file are preserved untouched.
    """

    import httpx  # noqa: PLC0415

    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise RuntimeError("the Codex login has no refresh token — run `codex login`")
    client_id = _jwt_claims(str(tokens.get("access_token"))).get("client_id") or _CODEX_CLIENT_ID
    r = httpx.post(
        _TOKEN_URL,
        json={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "scope": "openid profile email",
        },
        timeout=20.0,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"refreshing the ChatGPT login failed ({r.status_code}) — run `codex login`"
        )
    body = r.json()
    new_tokens = dict(tokens)
    for key in ("access_token", "refresh_token", "id_token"):
        if body.get(key):
            new_tokens[key] = body[key]
    out = dict(data)
    out["tokens"] = new_tokens
    out["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_codex_auth(out)
    return new_tokens


def chatgpt_access_token() -> str:
    """A live ChatGPT access token, refreshed when within 5 minutes of expiry.

    Re-read from disk on every call, so a refresh the Codex CLI did in the
    meantime is picked up instead of racing it. Raises ``RuntimeError`` with
    the fix when there is no usable login.
    """

    with _lock:
        data = _read_codex_auth()
        tokens = _tokens(data)
        if data is None or tokens is None:
            raise RuntimeError("no ChatGPT login found — run `codex login`")
        token = str(tokens["access_token"])
        exp = _expires_at(token)
        if exp is not None and exp - time.time() < _REFRESH_SKEW_S:
            token = str(_refresh(data, tokens)["access_token"])
        return token


def chatgpt_headers() -> dict[str, str]:
    """The fixed headers the Codex backend expects besides the bearer token."""

    headers = {"OpenAI-Beta": "responses=experimental", "originator": "codex_cli_rs"}
    account = chatgpt_account_id()
    if account:
        headers["chatgpt-account-id"] = account
    return headers


def is_chatgpt_codex_url(url: str | None) -> bool:
    return "chatgpt.com/backend-api/codex" in (url or "").lower()


def codex_models() -> list[str]:
    """Models the ChatGPT plan serves, from Codex's own ``models_cache.json``.

    Hidden entries (internal review models, reserve tiers) are skipped. Empty
    when Codex has not cached a list yet.
    """

    path = codex_auth_path().parent / "models_cache.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    items = data.get("models") if isinstance(data, dict) else data
    out: list[str] = []
    for m in items or []:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug") or m.get("id")
        if not isinstance(slug, str) or not slug:
            continue
        if m.get("visibility") == "hide" or m.get("supported_in_api") is False:
            continue
        out.append(slug)
    return out


def codex_client_version() -> str:
    """The client version to send to ``/models`` — the backend filters its
    list by it, so the one Codex last fetched with is the one that matches."""

    path = codex_auth_path().parent / "models_cache.json"
    try:
        version = json.loads(path.read_text()).get("client_version")
    except (OSError, ValueError, AttributeError):
        version = None
    return version if isinstance(version, str) and version else "0.151.0"


def detect_codex() -> dict[str, Any]:
    """``{"installed", "path", "signed_in", "mode", "plan"}`` — no secrets.

    ``mode`` is ``"chatgpt"`` (a subscription sign-in), ``"api_key"`` (a
    platform key stored by Codex) or None.
    """

    path = shutil.which("codex")
    data = _read_codex_auth()
    tokens = _tokens(data)
    mode: str | None = None
    if tokens is not None:
        mode = "chatgpt"
    elif codex_api_key():
        mode = "api_key"
    return {
        "installed": path is not None,
        "path": path,
        "signed_in": mode is not None,
        "mode": mode,
        "plan": _plan_type(tokens) if tokens else None,
        "auth_file": str(codex_auth_path()),
    }


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


def _claude_keychain_entry() -> bool:
    """Whether macOS holds Claude Code's credential item.

    ``find-generic-password`` without ``-w`` reads only the item's attributes,
    so this neither reveals the secret nor raises a Keychain access prompt.
    """

    if sys.platform != "darwin" or not shutil.which("security"):
        return False
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def detect_claude_code() -> dict[str, Any]:
    """``{"installed", "path", "signed_in", "source"}`` — detection only.

    ``source`` is ``"keychain"`` (macOS) or ``"file"``
    (``~/.claude/.credentials.json``, Linux/Windows). An ``ANTHROPIC_API_KEY``
    Claude Code may also use is already covered by the API-key method.
    """

    path = shutil.which("claude")
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    source: str | None = None
    if (config / ".credentials.json").is_file():
        source = "file"
    elif _claude_keychain_entry():
        source = "keychain"
    return {
        "installed": path is not None,
        "path": path,
        "signed_in": source is not None,
        "source": source,
    }
