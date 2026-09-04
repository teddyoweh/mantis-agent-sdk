"""Google and AWS credentials, resolved without a cloud SDK.

The Claude-on-Vertex, Claude-on-Bedrock and Gemini-on-Vertex routes need real
cloud credentials, and the whole point of those routes is that they work for
people who already have `gcloud` / `aws` configured — so "install boto3" or
"install google-auth" is the wrong answer. Both resolutions are small and
specified:

* **Google** — an OAuth2 access token, from (in order) an explicitly exported
  token, a service-account JSON key exchanged via the JWT-bearer grant, or
  `gcloud auth print-access-token`. Tokens are cached until shortly before
  they expire, because a subprocess per request would dominate latency.
* **AWS** — an access key pair, from the environment, then the shared config
  files (`~/.aws/credentials` + `~/.aws/config`, honouring `AWS_PROFILE`),
  then boto3's own chain when boto3 happens to be installed (which also picks
  up SSO caches, IMDS and assumed roles we deliberately don't reimplement).

Nothing here raises for "not configured" — that is a normal state the auth-
method surface reports. It raises only when a credential is present but
unusable (a malformed key file), because silently falling through to "not
configured" would send someone hunting for a credential they already set.
"""

from __future__ import annotations

import base64
import configparser
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AwsCredentials",
    "CloudCredentialError",
    "aws_credentials",
    "aws_region",
    "google_access_token",
    "google_project",
    "google_token_source",
]

#: Refresh a Google token this many seconds before it actually expires so an
#: in-flight request never races the boundary.
_SKEW_S = 120

#: Scope every Vertex call needs. Narrower scopes exist but are not accepted
#: for aiplatform.
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

_lock = threading.Lock()
#: ``{cache_key: (token, expires_at_epoch)}``
_token_cache: dict[str, tuple[str, float]] = {}


class CloudCredentialError(RuntimeError):
    """A credential is present but cannot be used as configured."""


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


def google_token_source() -> str | None:
    """Which Google credential we would use, without fetching a token.

    ``"env"`` (a pasted access token), ``"service_account"``,
    ``"gcloud"``, or ``None`` when nothing is configured. Used by the
    auth-method status surface, which must stay fast and offline.
    """

    if (os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
            or os.environ.get("ANTHROPIC_VERTEX_TOKEN")):
        return "env"
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path and Path(path).expanduser().is_file():
        return "service_account"
    if _gcloud_available():
        return "gcloud"
    return None


def google_project() -> str:
    """The GCP project for Vertex calls, from the usual variables."""

    for var in ("GOOGLE_CLOUD_PROJECT", "ANTHROPIC_VERTEX_PROJECT_ID",
                "GCLOUD_PROJECT", "GOOGLE_PROJECT_ID"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    # A service-account key names its own project — a user who exported the
    # key file has already said which project they mean.
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        try:
            blob = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
            return str(blob.get("project_id") or "")
        except (OSError, ValueError):
            return ""
    return ""


def google_access_token(*, timeout: float = 20.0) -> str:
    """A bearer token for Vertex, or ``""`` when nothing is configured.

    Resolution order: an exported token (``GOOGLE_OAUTH_ACCESS_TOKEN`` /
    ``ANTHROPIC_VERTEX_TOKEN``) → a service-account key at
    ``GOOGLE_APPLICATION_CREDENTIALS`` → ``gcloud auth print-access-token``.
    Everything but the exported token is cached until just before expiry.
    """

    explicit = (
        os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
        or os.environ.get("ANTHROPIC_VERTEX_TOKEN")
        or ""
    ).strip()
    if explicit:
        return explicit

    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if key_path:
        return _cached(f"sa:{key_path}", lambda: _service_account_token(key_path, timeout))
    # ``_gcloud_token`` already returns ``(token, expires_at)`` — wrapping it in
    # another tuple here made ``google_access_token`` return the tuple itself,
    # which is truthy, so an unauthenticated machine sent ``Bearer ('', 0.0)``
    # instead of raising "no credentials".
    return _cached("gcloud", lambda: _gcloud_token(timeout))


def _cached(key: str, produce: Any) -> str:
    with _lock:
        hit = _token_cache.get(key)
        if hit and time.time() < hit[1] - _SKEW_S:
            return hit[0]
    token, expires_at = produce()
    if not isinstance(token, str):  # pragma: no cover — guarded by the caller
        raise CloudCredentialError(
            f"credential producer returned {type(token).__name__}, not a token"
        )
    if token:
        with _lock:
            _token_cache[key] = (token, expires_at)
    return token


def _reset_token_cache_for_tests() -> None:
    with _lock:
        _token_cache.clear()


def _gcloud_available() -> bool:
    import shutil  # noqa: PLC0415

    return shutil.which("gcloud") is not None


def _gcloud_token(timeout: float) -> tuple[str, float]:
    """``gcloud auth print-access-token``. Absent gcloud is a normal outcome.

    gcloud does not report the expiry, and its tokens are one-hour; we cache
    for 30 minutes, which is always inside the real lifetime.
    """

    try:
        proc = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "", 0.0
    if proc.returncode != 0:
        return "", 0.0
    return proc.stdout.strip(), time.time() + 1800.0


def _service_account_token(path: str, timeout: float) -> tuple[str, float]:
    """Exchange a service-account key for an access token (JWT-bearer grant).

    RFC 7523: sign a short-lived assertion with the key's private half and
    POST it to Google's token endpoint. Implemented here rather than via
    google-auth for the same reason SigV4 is: it is a specified handful of
    lines, and the alternative is a hard dependency for a route most users
    never take.
    """

    try:
        blob = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise CloudCredentialError(
            f"GOOGLE_APPLICATION_CREDENTIALS points at {path!r}, which cannot be "
            f"read ({exc})"
        ) from exc
    except ValueError as exc:
        raise CloudCredentialError(
            f"GOOGLE_APPLICATION_CREDENTIALS file {path!r} is not valid JSON"
        ) from exc
    if not isinstance(blob, dict) or blob.get("type") != "service_account":
        raise CloudCredentialError(
            f"GOOGLE_APPLICATION_CREDENTIALS file {path!r} is not a service-account "
            f"key (type={blob.get('type') if isinstance(blob, dict) else '?'!r}); "
            "for a user credential run `gcloud auth application-default login` "
            "and unset the variable"
        )
    client_email = str(blob.get("client_email") or "")
    private_key = str(blob.get("private_key") or "")
    token_uri = str(blob.get("token_uri") or _GOOGLE_TOKEN_URI)
    if not client_email or not private_key:
        raise CloudCredentialError(
            f"service-account key {path!r} is missing client_email/private_key"
        )

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": client_email,
        "scope": _GOOGLE_SCOPE,
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = b".".join((_b64url_json(header), _b64url_json(claims)))
    assertion = signing_input + b"." + _b64url(_rs256_sign(private_key, signing_input))

    import httpx  # noqa: PLC0415

    try:
        r = httpx.post(
            token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion.decode("ascii"),
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001 — surfaced as a credential error
        raise CloudCredentialError(
            f"service-account token exchange failed for {client_email}: {exc}"
        ) from exc
    token = str(data.get("access_token") or "")
    expires_in = float(data.get("expires_in") or 3600)
    return token, time.time() + expires_in


def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _b64url_json(obj: dict[str, Any]) -> bytes:
    return _b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _rs256_sign(private_key_pem: str, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 over SHA-256.

    Uses ``cryptography`` when it is importable (it usually is, as a
    transitive dependency) and otherwise falls back to a pure-Python signer:
    a minimal DER walk to recover ``(n, d)`` and one modular exponentiation.
    Both paths produce the identical signature — the algorithm has no free
    parameters — which is what the round-trip test asserts.
    """

    import hashlib  # noqa: PLC0415

    try:  # pragma: no cover — depends on the environment, both paths tested
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import padding  # noqa: PLC0415

        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    except ImportError:
        pass

    n, d = _rsa_private_numbers(private_key_pem)
    digest = hashlib.sha256(message).digest()
    # DigestInfo prefix for SHA-256 (RFC 8017 §9.2, notes 1).
    prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    t = prefix + digest
    k = (n.bit_length() + 7) // 8
    if k < len(t) + 11:
        raise CloudCredentialError("service-account private key is too small to sign")
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    sig = pow(int.from_bytes(em, "big"), d, n)
    return sig.to_bytes(k, "big")


def _rsa_private_numbers(pem: str) -> tuple[int, int]:
    """``(modulus, private_exponent)`` from a PEM RSA key.

    Handles both ``BEGIN RSA PRIVATE KEY`` (PKCS#1) and ``BEGIN PRIVATE KEY``
    (PKCS#8 wrapping a PKCS#1 key) — service-account keys ship the latter.
    """

    body = "".join(
        line.strip() for line in pem.splitlines() if line and "-----" not in line
    )
    try:
        der = base64.b64decode(body)
    except ValueError as exc:
        raise CloudCredentialError("service-account private key is not valid PEM") from exc

    seq = _der_sequence(der)
    if len(seq) >= 3 and seq[0][0] == 0x02 and seq[1][0] == 0x30:
        # PKCS#8: version, AlgorithmIdentifier, OCTET STRING(privateKey)
        inner = seq[2][1]
        seq = _der_sequence(inner)
    # PKCS#1 RSAPrivateKey: version, n, e, d, p, q, …
    if len(seq) < 4:
        raise CloudCredentialError("service-account private key has an unexpected shape")
    n = int.from_bytes(seq[1][1], "big")
    d = int.from_bytes(seq[3][1], "big")
    return n, d


def _der_sequence(der: bytes) -> list[tuple[int, bytes]]:
    """The ``(tag, content)`` pairs inside one DER SEQUENCE."""

    tag, content, _ = _der_read(der, 0)
    if tag != 0x30:
        raise CloudCredentialError("service-account private key is not a DER SEQUENCE")
    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(content):
        item_tag, item_content, pos = _der_read(content, pos)
        out.append((item_tag, item_content))
    return out


def _der_read(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    tag = buf[pos]
    pos += 1
    length = buf[pos]
    pos += 1
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    return tag, buf[pos:pos + length], pos + length


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str = ""
    region: str = ""
    source: str = ""

    def __repr__(self) -> str:  # pragma: no cover — display only, but load-bearing
        tail = self.access_key_id[-4:] if self.access_key_id else ""
        return f"AwsCredentials(id=…{tail}, region={self.region!r}, source={self.source!r})"


def aws_region(explicit: str = "") -> str:
    """Region for Bedrock: explicit → ``AWS_REGION`` → ``AWS_DEFAULT_REGION``
    → the profile's ``region`` → ``us-east-1`` (where Bedrock's Anthropic
    models have always been available)."""

    for candidate in (explicit, os.environ.get("AWS_REGION"),
                      os.environ.get("AWS_DEFAULT_REGION")):
        value = (candidate or "").strip()
        if value:
            return value
    profile_region = _profile_value("region")
    return profile_region or "us-east-1"


def aws_credentials(*, profile: str | None = None) -> AwsCredentials | None:
    """Resolve an AWS key pair, or ``None`` when nothing is configured.

    Order: the environment → the shared credentials/config files for
    ``AWS_PROFILE`` (or ``default``) → boto3's own chain if boto3 is
    installed. The boto3 step is last because it is the slowest and the one
    that may hit the network (SSO / IMDS), and it is attempted at all so
    role-based and SSO setups work without us reimplementing them.
    """

    key_id = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip()
    secret = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip()
    if key_id and secret:
        return AwsCredentials(
            key_id, secret,
            (os.environ.get("AWS_SESSION_TOKEN") or "").strip(),
            aws_region(), "env",
        )

    name = profile or os.environ.get("AWS_PROFILE") or "default"
    section = _profile_section(name)
    if section:
        key_id = str(section.get("aws_access_key_id") or "").strip()
        secret = str(section.get("aws_secret_access_key") or "").strip()
        if key_id and secret:
            return AwsCredentials(
                key_id, secret,
                str(section.get("aws_session_token") or "").strip(),
                aws_region(str(section.get("region") or "")),
                f"profile:{name}",
            )

    try:  # pragma: no cover — only when boto3 is installed
        import boto3  # noqa: PLC0415

        frozen = boto3.Session(
            profile_name=profile or os.environ.get("AWS_PROFILE") or None
        ).get_credentials()
        if frozen is not None:
            frozen = frozen.get_frozen_credentials()
            if frozen.access_key and frozen.secret_key:
                return AwsCredentials(
                    frozen.access_key, frozen.secret_key, frozen.token or "",
                    aws_region(), "boto3",
                )
    except Exception:  # noqa: BLE001 — boto3 absent or unconfigured is normal
        pass
    return None


def _aws_config_paths() -> tuple[Path, Path]:
    creds = Path(
        os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or Path.home() / ".aws" / "credentials"
    ).expanduser()
    config = Path(
        os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config"
    ).expanduser()
    return creds, config


def _profile_section(name: str) -> dict[str, str]:
    """The merged ``[name]`` section across both shared files.

    ``~/.aws/config`` names non-default profiles ``[profile foo]`` while
    ``~/.aws/credentials`` names them ``[foo]`` — reading only one of those
    conventions is why "but I have a profile" reports as unconfigured.
    """

    out: dict[str, str] = {}
    creds_path, config_path = _aws_config_paths()
    for path, prefixed in ((config_path, True), (creds_path, False)):
        if not path.is_file():
            continue
        parser = configparser.RawConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        for section in (f"profile {name}", name) if prefixed else (name,):
            if parser.has_section(section):
                out.update({k: v for k, v in parser.items(section)})
    return out


def _profile_value(key: str) -> str:
    name = os.environ.get("AWS_PROFILE") or "default"
    return str(_profile_section(name).get(key) or "").strip()
