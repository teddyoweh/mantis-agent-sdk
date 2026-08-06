"""One credential in, one authenticated Anthropic request out.

Anthropic can be reached four ways and they authenticate differently enough
that "paste your key" is otherwise a lie:

===========  ==========================  ======================================
Mode         Credential                  How the request is authenticated
===========  ==========================  ======================================
``api_key``  ``sk-ant-api03-…``          ``x-api-key`` header
``oauth``    ``sk-ant-oat01-…``          ``Authorization: Bearer`` + the
                                         ``anthropic-beta: oauth-2025-04-20``
                                         header, refreshable
``bedrock``  AWS access key / profile    SigV4 request signing, regional host,
                                         ``anthropic.claude-*`` model ids
``vertex``   GCP access token / SA       ``Authorization: Bearer``, regional
                                         host, ``claude-*@…`` publisher ids
===========  ==========================  ======================================

The design goal is that a user pastes *whatever they have* and the right thing
happens. :func:`detect_credential` is therefore the entry point: it classifies a
pasted string by shape, so the TUI never has to ask "is this an API key or an
OAuth token?" — a question the user often cannot answer and never should have to.

Secret hygiene
--------------
A credential is a value that must never be rendered. :class:`Credential` has no
``__str__`` that leaks, its ``repr`` is redacted, and :attr:`Credential.hint`
returns only enough to recognise *which* key it is (``sk-ant-…AzuQ``). Nothing
here writes to disk; persistence is the caller's decision and belongs with the
credential store, not with detection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple

__all__ = [
    "AUTH_MODES",
    "AnthropicAuth",
    "Credential",
    "CredentialError",
    "bedrock_headers",
    "detect_credential",
    "resolve_auth",
    "sigv4_headers",
]

AUTH_MODES: Tuple[str, ...] = ("api_key", "oauth", "bedrock", "vertex")


class CredentialError(ValueError):
    """A pasted or configured credential that cannot be used as claimed."""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
#
# Prefixes are the only reliable discriminator: an Anthropic API key and an
# OAuth access token are both opaque ``sk-ant-`` strings and differ solely in
# the segment after it. Getting this wrong is not cosmetic — an OAuth token sent
# as ``x-api-key`` fails with an authentication error that reads like a bad key,
# which is exactly the confusion this module exists to remove.

_API_KEY_RE = re.compile(r"^sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}$")
_OAUTH_RE = re.compile(r"^sk-ant-oat\d{2}-[A-Za-z0-9_\-]{20,}$")
#: An AWS access key id. ``AKIA`` is a long-lived user key, ``ASIA`` a session
#: key from STS — both mean "this person wants Bedrock".
_AWS_KEY_RE = re.compile(r"^(?:AKIA|ASIA)[A-Z0-9]{12,}$")
#: A Google OAuth2 access token, as printed by ``gcloud auth print-access-token``.
_GCP_TOKEN_RE = re.compile(r"^ya29\.[A-Za-z0-9_\-\.]{20,}$")


@dataclass(frozen=True)
class Credential:
    """A classified secret. Never render the ``secret`` field."""

    mode: str
    secret: str = ""
    #: Parsed structure for credentials that carry more than one value
    #: (a service-account JSON blob, an AWS key pair).
    extra: dict = field(default_factory=dict)
    source: str = "pasted"

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"Credential(mode={self.mode!r}, secret={self.hint!r}, source={self.source!r})"

    __str__ = __repr__

    @property
    def hint(self) -> str:
        """Enough to recognise WHICH credential this is, and nothing more.

        Shown in the TUI after a paste so a user can confirm they pasted the key
        they meant to — the one legitimate reason to display any part of it.
        """

        s = self.secret or ""
        if not s:
            return "(none)"
        if len(s) <= 12:
            return "…" + s[-4:]
        return f"{s[:11]}…{s[-4:]}"


def detect_credential(text: object) -> Credential:
    """Classify a pasted credential by shape.

    Accepts the raw thing a user pastes — including a service-account JSON blob,
    an ``export FOO=bar`` line copied out of a shell, or a value wrapped in
    quotes — because that is what actually lands on the clipboard.
    """

    if text is None:
        raise CredentialError("nothing to authenticate with")
    raw = text if isinstance(text, str) else str(text)
    value = _unwrap(raw)
    if not value:
        raise CredentialError("nothing to authenticate with")

    if _API_KEY_RE.match(value):
        return Credential("api_key", value)
    if _OAUTH_RE.match(value):
        return Credential("oauth", value)
    if _AWS_KEY_RE.match(value):
        # The id alone cannot sign; the secret half arrives separately, so this
        # records the mode and lets the caller collect the rest.
        return Credential("bedrock", "", {"access_key_id": value, "needs": "secret_access_key"})
    if _GCP_TOKEN_RE.match(value):
        return Credential("vertex", value, {"token_type": "access_token"})

    # A GCP service-account key is JSON, not a token.
    if value.lstrip().startswith("{"):
        try:
            blob = json.loads(value)
        except ValueError:
            blob = None
        if isinstance(blob, dict) and blob.get("type") == "service_account":
            return Credential(
                "vertex", "",
                {"token_type": "service_account",
                 "client_email": str(blob.get("client_email") or ""),
                 "project_id": str(blob.get("project_id") or "")},
            )

    # A bare `sk-ant-` with an unknown middle segment is still Anthropic, and
    # guessing between key and token would produce the misleading auth error
    # this module exists to prevent. Say so instead.
    if value.startswith("sk-ant-"):
        raise CredentialError(
            "unrecognized Anthropic credential: expected sk-ant-api… (API key) "
            "or sk-ant-oat… (OAuth token)"
        )
    raise CredentialError(
        "unrecognized credential — expected an Anthropic API key (sk-ant-api…), "
        "an OAuth token (sk-ant-oat…), an AWS access key id (AKIA…/ASIA…) for "
        "Bedrock, or a Google access token / service-account JSON for Vertex"
    )


def _unwrap(raw: str) -> str:
    """Strip the packaging a credential arrives wrapped in.

    People paste ``export ANTHROPIC_API_KEY="sk-ant-…"``, or a value with a
    trailing newline from a terminal, or one in quotes from a config file.
    Refusing those is a papercut with no security benefit — the secret is the
    same either way.
    """

    text = raw.strip()
    if not text:
        return ""
    if "\n" not in text:
        # `export FOO=bar` / `FOO=bar` / `FOO: bar`
        m = re.match(r"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*[:=]\s*(.+)$", text)
        if m:
            text = m.group(1).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnthropicAuth:
    """Everything a request needs, once the mode is known."""

    mode: str
    base_url: str
    headers: dict
    #: True when the credential expires and the caller should refresh it.
    refreshable: bool = False
    region: str = ""
    project: str = ""

    def redacted(self) -> dict:
        """Header map safe to log — every credential-bearing value masked."""

        from .redaction import is_secret_name  # noqa: PLC0415

        out = {}
        for k, v in self.headers.items():
            lowered = k.lower()
            secretish = (
                is_secret_name(k)
                or lowered in ("authorization", "x-api-key")
                or lowered.startswith("x-amz-")
            )
            out[k] = "[redacted]" if secretish else v
        return out


ANTHROPIC_DEFAULT = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"


def resolve_auth(
    cred: Credential,
    *,
    region: str = "",
    project: str = "",
    base_url: str = "",
    anthropic_version: str = ANTHROPIC_VERSION,
) -> AnthropicAuth:
    """Turn a classified credential into a concrete request configuration.

    The header choice per mode is the whole point: an API key goes in
    ``x-api-key`` while an OAuth token goes in ``Authorization: Bearer`` *and*
    needs the beta header — send either one the other way and the API returns an
    authentication failure that reads like a bad credential.
    """

    mode = cred.mode
    if mode not in AUTH_MODES:
        raise CredentialError(f"unknown auth mode {mode!r}")

    if mode == "api_key":
        return AnthropicAuth(
            mode, base_url or ANTHROPIC_DEFAULT,
            {"x-api-key": cred.secret, "anthropic-version": anthropic_version},
        )

    if mode == "oauth":
        return AnthropicAuth(
            mode, base_url or ANTHROPIC_DEFAULT,
            {
                "authorization": f"Bearer {cred.secret}",
                "anthropic-version": anthropic_version,
                # Without this the token is rejected — the one non-obvious part
                # of OAuth auth, and the reason a pasted OAuth token that "looks
                # fine" otherwise fails.
                "anthropic-beta": OAUTH_BETA,
            },
            refreshable=True,
        )

    if mode == "vertex":
        reg = region or os.environ.get("CLOUD_ML_REGION") or "us-east5"
        proj = project or cred.extra.get("project_id") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") or ""
        if not proj:
            raise CredentialError(
                "Vertex needs a project id — set ANTHROPIC_VERTEX_PROJECT_ID or "
                "pass project="
            )
        token = cred.secret or _gcloud_access_token()
        if not token:
            raise CredentialError(
                "Vertex needs an access token — paste one, or install gcloud and "
                "run `gcloud auth application-default login`"
            )
        url = base_url or f"https://{reg}-aiplatform.googleapis.com/v1"
        return AnthropicAuth(
            mode, url,
            {"authorization": f"Bearer {token}", "anthropic-version": anthropic_version},
            refreshable=True, region=reg, project=proj,
        )

    # bedrock
    reg = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    key_id = cred.extra.get("access_key_id") or os.environ.get("AWS_ACCESS_KEY_ID") or ""
    secret_key = cred.secret or os.environ.get("AWS_SECRET_ACCESS_KEY") or ""
    if not key_id or not secret_key:
        raise CredentialError(
            "Bedrock needs both an access key id and a secret access key "
            "(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)"
        )
    url = base_url or f"https://bedrock-runtime.{reg}.amazonaws.com"
    # SigV4 signs the *whole request*, so the headers cannot be computed here —
    # they depend on method, path and body. The caller signs per request via
    # `bedrock_headers`; what belongs in the config is the material.
    return AnthropicAuth(
        mode, url, {"anthropic-version": anthropic_version}, region=reg,
    )


def _gcloud_access_token(timeout: float = 10.0) -> str:
    """Best-effort ADC token. Absent gcloud is a normal outcome, not an error."""

    try:
        proc = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


# ---------------------------------------------------------------------------
# AWS SigV4
# ---------------------------------------------------------------------------
#
# Implemented here rather than pulled from boto3: signing is ~60 lines of hmac
# and this package takes no third-party dependency for a provider most users do
# not use. The algorithm is fixed and specified, so the risk is transcription,
# which is what the test vectors are for.


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(
    *,
    method: str,
    url: str,
    body: bytes,
    region: str,
    service: str,
    access_key_id: str,
    secret_access_key: str,
    session_token: str = "",
    now: Optional[datetime] = None,
    extra_headers: Optional[dict] = None,
) -> dict:
    """AWS Signature Version 4 headers for one request.

    ``now`` is injectable so the signature is deterministic under test — a
    signer whose output changes every second cannot be checked against a vector.
    """

    from urllib.parse import quote, urlsplit  # noqa: PLC0415

    parts = urlsplit(url)
    host = parts.netloc
    path = quote(parts.path or "/", safe="/-_.~")
    query = parts.query or ""

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    date = stamp[:8]

    payload_hash = hashlib.sha256(body or b"").hexdigest()
    headers = {"host": host, "x-amz-date": stamp, "x-amz-content-sha256": payload_hash}
    if session_token:
        headers["x-amz-security-token"] = session_token
    for k, v in (extra_headers or {}).items():
        headers[k.lower()] = v

    signed_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
    canonical = "\n".join(
        [method.upper(), path, query, canonical_headers, signed_names, payload_hash]
    )

    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope,
         hashlib.sha256(canonical.encode("utf-8")).hexdigest()]
    )

    k_date = _sign(("AWS4" + secret_access_key).encode("utf-8"), date)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return headers


def bedrock_headers(
    auth: AnthropicAuth,
    *,
    method: str,
    url: str,
    body: bytes,
    access_key_id: str = "",
    secret_access_key: str = "",
    session_token: str = "",
    now: Optional[datetime] = None,
) -> dict:
    """Signed headers for one Bedrock request, merged with the base set."""

    if auth.mode != "bedrock":
        raise CredentialError(f"bedrock_headers called for mode {auth.mode!r}")
    signed = sigv4_headers(
        method=method, url=url, body=body, region=auth.region,
        service="bedrock",
        access_key_id=access_key_id or os.environ.get("AWS_ACCESS_KEY_ID", ""),
        secret_access_key=secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        session_token=session_token or os.environ.get("AWS_SESSION_TOKEN", ""),
        now=now,
        extra_headers={"content-type": "application/json"},
    )
    merged = dict(auth.headers)
    merged.update(signed)
    return merged


def clear_credentials() -> str:
    """Forget every Anthropic credential — the "reset" half of changing one.

    Rotating a key is a paste; *revoking* one locally is not, and without this
    the only way to undo a bad or leaked credential was hand-editing the key
    store and the settings env block. Clears both, plus the process env, so the
    running session stops using it immediately rather than at the next launch.
    """

    cleared = []
    try:
        from .catalog import BY_ID, api_key_for, clear_key  # noqa: PLC0415

        prov = BY_ID.get("anthropic")
        if prov is not None:
            # Check BEFORE clearing: `clear_key` succeeds whether or not
            # anything was there, so reporting off its return would claim to
            # have removed a key the user never had — and "cleared API key" when
            # nothing was set reads like the reset did the wrong thing.
            had_key = bool(api_key_for(prov))
            clear_key("anthropic")
            if had_key:
                cleared.append("API key")
    except Exception:  # noqa: BLE001 — a broken store must not block a reset
        pass

    env_keys = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_REFRESH_TOKEN",
                "ANTHROPIC_AUTH_EXPIRES_AT", "ANTHROPIC_VERTEX_TOKEN")
    hit = False
    for k in env_keys:
        if os.environ.pop(k, None):
            hit = True
    if hit:
        cleared.append("OAuth token")
    try:
        from .settings import update_setting_source  # noqa: PLC0415

        # Empty strings rather than deletion: the settings writer merges, so a
        # removed key would simply be inherited again from the previous file.
        update_setting_source("user", {"env": {k: "" for k in env_keys}})
    except Exception:  # noqa: BLE001
        pass

    return ("cleared " + " and ".join(cleared)) if cleared else "nothing to clear"


def configured_modes() -> dict:
    """Which auth methods are already usable, for the picker to show.

    A picker that lists four options identically makes the user re-derive what
    they already set up. Showing state turns "which of these do I have?" into a
    glance — and makes an accidental second paste obviously unnecessary.
    """

    from .catalog import BY_ID, api_key_for  # noqa: PLC0415

    prov = BY_ID.get("anthropic")
    out = {m: False for m in AUTH_MODES}
    try:
        out["api_key"] = bool(prov is not None and api_key_for(prov))
    except Exception:  # noqa: BLE001 — a broken key store must not break a menu
        out["api_key"] = False
    out["oauth"] = bool((os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip())
    out["bedrock"] = not setup_instructions("bedrock")
    out["vertex"] = not setup_instructions("vertex")
    return out


def setup_instructions(mode: str) -> str:
    """What still has to happen before ``mode`` can work, or ``""`` if nothing.

    ``api_key`` and ``oauth`` are one pasted value, so the UI can go straight to
    an input. Bedrock and Vertex are not — they need values that do not fit on a
    paste line, and opening an input that cannot possibly succeed is worse than
    saying so. Reports only what is actually still missing, so a user who
    already exported half of it is not told to redo that half.
    """

    if mode in ("api_key", "oauth"):
        return ""

    if mode == "bedrock":
        missing = [v for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
                   if not os.environ.get(v)]
        if not missing:
            reg = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            return "" if reg else "Bedrock: set AWS_REGION, then re-run /models"
        return ("Bedrock needs " + " and ".join(missing)
                + " in your environment, then re-run /models")

    if mode == "vertex":
        missing = []
        if not (os.environ.get("ANTHROPIC_VERTEX_TOKEN")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                or _gcloud_access_token()):
            missing.append("a Google access token "
                           "(`gcloud auth application-default login`) "
                           "or GOOGLE_APPLICATION_CREDENTIALS")
        if not os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
            missing.append("ANTHROPIC_VERTEX_PROJECT_ID")
        return ("Vertex needs " + " and ".join(missing) + ", then re-run /models"
                if missing else "")

    return f"unknown auth method {mode!r}"


def persist_credential(cred: Credential) -> str:
    """Make a credential live for this process and remember it for the next one.

    Routing by mode is the entire point. ``/models`` used to store whatever was
    pasted through the API-key path, so an OAuth token was written to the
    ``x-api-key`` store and then failed validation with an error that read like
    a bad key — the exact confusion :func:`detect_credential` exists to end.

    Returns a human-readable line for the UI. Raises :class:`CredentialError`
    for a mode that genuinely cannot be completed from one pasted value.
    """

    if cred.mode == "api_key":
        from .catalog import set_key  # noqa: PLC0415

        set_key("anthropic", cred.secret)
        return f"API key saved · {cred.hint}"

    if cred.mode == "oauth":
        # The passthrough already reads this as `Authorization: Bearer` plus the
        # oauth beta header; `anthropic_oauth.ensure_fresh_anthropic_token`
        # refreshes it in place. Persisting through the USER settings tier is
        # the same path the refresh flow uses — and the tier matters: a project
        # settings file may not set credential-shaped env vars.
        env = {"ANTHROPIC_AUTH_TOKEN": cred.secret}
        os.environ["ANTHROPIC_AUTH_TOKEN"] = cred.secret
        refresh = cred.extra.get("refresh_token")
        if refresh:
            env["ANTHROPIC_REFRESH_TOKEN"] = str(refresh)
            os.environ["ANTHROPIC_REFRESH_TOKEN"] = str(refresh)
        try:
            from .settings import update_setting_source  # noqa: PLC0415

            update_setting_source("user", {"env": env})
        except Exception:  # noqa: BLE001 — persistence is best-effort, as elsewhere
            pass
        return f"OAuth token saved · {cred.hint}"

    if cred.mode == "bedrock":
        key_id = cred.extra.get("access_key_id", "")
        if key_id:
            os.environ["AWS_ACCESS_KEY_ID"] = key_id
        raise CredentialError(
            "Bedrock needs more than one value: set AWS_SECRET_ACCESS_KEY (and "
            "AWS_REGION) in your environment, then re-run /models"
            + (f" — access key id {key_id} noted" if key_id else "")
        )

    if cred.mode == "vertex":
        if cred.extra.get("token_type") == "service_account":
            raise CredentialError(
                "Vertex service-account JSON must be a file: set "
                "GOOGLE_APPLICATION_CREDENTIALS to its path and "
                "ANTHROPIC_VERTEX_PROJECT_ID, then re-run /models"
            )
        os.environ["ANTHROPIC_VERTEX_TOKEN"] = cred.secret
        return f"Vertex access token set for this session · {cred.hint}"

    raise CredentialError(f"unknown auth mode {cred.mode!r}")


def describe(auth: AnthropicAuth, cred: Optional[Credential] = None) -> str:
    """One line for the TUI: what is authenticating, and how. Never the secret."""

    bits = [f"mode {auth.mode}"]
    if cred is not None:
        bits.append(cred.hint)
    if auth.region:
        bits.append(f"region {auth.region}")
    if auth.project:
        bits.append(f"project {auth.project}")
    bits.append(auth.base_url)
    return " · ".join(bits)
