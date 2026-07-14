"""Native Claude OAuth login — a faithful port of Claude Code's
``services/oauth`` PKCE flow (``constants/oauth.ts`` + ``services/oauth/client.ts``).

It lets a user sign in with their **Claude subscription (Pro / Max)** instead of
an API key: we open the authorize page, they approve and paste back the code,
and we exchange it for an OAuth **access token**. mantis stores that token as
``ANTHROPIC_AUTH_TOKEN`` and the Anthropic passthrough sends it as
``Authorization: Bearer`` with the ``anthropic-beta: oauth-2025-04-20`` header —
exactly what Claude Code does.

Constants are the real production values from the reference source. Stdlib only
for PKCE/URL building; ``httpx`` (already a dep) for the token exchange.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse

# Production OAuth config (constants/oauth.ts → PROD_OAUTH_CONFIG).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_AI_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"  # subscription login
CONSOLE_AUTHORIZE_URL = "https://platform.claude.com/oauth/authorize"  # console (API-key issuance)
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
MANUAL_REDIRECT_URL = "https://platform.claude.com/oauth/code/callback"
OAUTH_BETA_HEADER = "oauth-2025-04-20"

# claude.ai subscription scopes (CLAUDE_AI_OAUTH_SCOPES): inference + the
# claude-code session/tooling scopes.
CLAUDE_AI_SCOPES = (
    "user:inference",
    "user:profile",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)

__all__ = [
    "CLIENT_ID",
    "CLAUDE_AI_AUTHORIZE_URL",
    "CONSOLE_AUTHORIZE_URL",
    "OAUTH_BETA_HEADER",
    "TOKEN_URL",
    "build_authorize_url",
    "ensure_fresh_anthropic_token",
    "exchange_code",
    "make_pkce",
    "make_state",
    "refresh_access_token",
]

# Refresh this many seconds BEFORE the token actually expires, so an in-flight
# request never races the expiry boundary.
_REFRESH_SKEW_S = 120


def _b64url(raw: bytes) -> str:
    """URL-safe base64 with no padding (RFC 7636 PKCE encoding)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` — S256, per RFC 7636."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def make_state() -> str:
    return _b64url(secrets.token_bytes(24))


def build_authorize_url(*, code_challenge: str, state: str, console: bool = False) -> str:
    """The URL to open in a browser. ``console=True`` uses the platform-console
    authorize page (org API-key issuance); default is the claude.ai subscription
    login. Uses the manual-redirect flow so no local server is needed — the page
    shows a code the user pastes back."""
    base = CONSOLE_AUTHORIZE_URL if console else CLAUDE_AI_AUTHORIZE_URL
    params = {
        "code": "true",  # show the code (and the Claude Max upsell), per the source
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": MANUAL_REDIRECT_URL,
        "scope": " ".join(CLAUDE_AI_SCOPES),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return base + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str, *, code_verifier: str, state: str, timeout: float = 30.0) -> dict:
    """Exchange the pasted authorization code for tokens (POST ``TOKEN_URL``).

    The manual-redirect code often arrives as ``<code>#<state>`` — we split it so
    the user can paste the whole thing. Returns the token JSON
    (``access_token``, ``refresh_token``, ``expires_in``). Raises on HTTP error.
    """
    import httpx  # noqa: PLC0415

    raw = code.strip()
    if "#" in raw:
        raw, _, embedded_state = raw.partition("#")
        state = embedded_state or state
    body = {
        "grant_type": "authorization_code",
        "code": raw.strip(),
        "redirect_uri": MANUAL_REDIRECT_URL,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
        "state": state,
    }
    r = httpx.post(TOKEN_URL, json=body,
                   headers={"content-type": "application/json"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def refresh_access_token(refresh_token: str, *, timeout: float = 30.0) -> dict:
    """Exchange a ``refresh_token`` for a fresh access token (POST ``TOKEN_URL``
    with ``grant_type=refresh_token``). Returns the token JSON (``access_token``,
    usually a rotated ``refresh_token``, and ``expires_in``). Raises on HTTP
    error so the caller can fall back to a full re-login."""
    import httpx  # noqa: PLC0415

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token.strip(),
        "client_id": CLIENT_ID,
    }
    r = httpx.post(TOKEN_URL, json=body,
                   headers={"content-type": "application/json"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def ensure_fresh_anthropic_token() -> str | None:
    """Refresh the stored OAuth access token if it's expired (or about to be).

    Reads ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_REFRESH_TOKEN`` /
    ``ANTHROPIC_AUTH_EXPIRES_AT`` (set at login by the setup wizard). When a
    refresh token exists and the access token is within ``_REFRESH_SKEW_S`` of
    expiry, it POSTs ``grant_type=refresh_token`` and rewrites all three values
    both in ``os.environ`` (so the current process picks them up immediately)
    and in user settings (so future processes do too).

    Returns the current (possibly refreshed) access token, or ``None`` when
    there's nothing to do / no refresh is possible. Never raises — on failure it
    leaves the existing credentials untouched so the request path can surface a
    normal 401 and prompt a re-login."""
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    token = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip() or None
    refresh = (os.environ.get("ANTHROPIC_REFRESH_TOKEN") or "").strip() or None
    if not refresh:
        return token  # no refresh path — nothing we can do
    expires_at_raw = (os.environ.get("ANTHROPIC_AUTH_EXPIRES_AT") or "").strip()
    try:
        expires_at = int(expires_at_raw) if expires_at_raw else 0
    except ValueError:
        expires_at = 0
    # Still comfortably valid → keep using it.
    if token and expires_at and time.time() < expires_at - _REFRESH_SKEW_S:
        return token
    try:
        tokens = refresh_access_token(refresh)
    except Exception:  # noqa: BLE001 — best-effort; fall back to existing creds
        return token
    new_token = tokens.get("access_token")
    if not new_token:
        return token
    env: dict[str, str] = {"ANTHROPIC_AUTH_TOKEN": str(new_token)}
    os.environ["ANTHROPIC_AUTH_TOKEN"] = str(new_token)
    new_refresh = tokens.get("refresh_token")
    if new_refresh:
        env["ANTHROPIC_REFRESH_TOKEN"] = str(new_refresh)
        os.environ["ANTHROPIC_REFRESH_TOKEN"] = str(new_refresh)
    expires_in = tokens.get("expires_in")
    if expires_in:
        try:
            new_exp = str(int(time.time()) + int(expires_in))
            env["ANTHROPIC_AUTH_EXPIRES_AT"] = new_exp
            os.environ["ANTHROPIC_AUTH_EXPIRES_AT"] = new_exp
        except (TypeError, ValueError):
            pass
    try:
        from .settings import update_setting_source  # noqa: PLC0415
        update_setting_source("user", {"env": env})
    except Exception:  # noqa: BLE001 — persistence is best-effort
        pass
    return str(new_token)
