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
    "exchange_code",
    "make_pkce",
    "make_state",
]


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
