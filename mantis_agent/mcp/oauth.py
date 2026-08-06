"""OAuth 2.1 + PKCE for MCP servers — the pure, network-free half.

Hosted MCP servers authenticate with OAuth 2.1 authorization-code + PKCE
(RFC 7636), discovery (RFC 8414 / RFC 9728) and resource indicators
(RFC 8707). This module owns every part of that which is a *computation*:
deriving a challenge, minting and checking ``state``, validating a discovery
document, canonicalizing a resource identifier, building a request body, and
proving a token was minted for the server we are about to send it to.

Nothing here opens a socket, spawns a browser, or reads the filesystem. The
loopback callback server, the HTTP fetches and the manager wiring live one
layer up; they are the parts that need mocking, and keeping them out means
every security-relevant decision below is testable as a plain function call.

Relationship to ``anthropic_oauth``
-----------------------------------
:mod:`mantis_agent.anthropic_oauth` is this codebase's existing OAuth flow —
a faithful port of Claude Code's manual-redirect PKCE login against a *single,
hardcoded* authorization server. The primitives here are spelled the same way
on purpose (``_b64url`` → :func:`b64url_nopad`, ``make_pkce``, ``make_state``,
``exchange_code``'s body → :func:`build_token_exchange_body`) so the two read
as one implementation. What is new is everything that follows from the server
being *unknown at build time*: discovery must be validated instead of trusted,
the redirect is a loopback listener instead of a fixed paste page, and a token
carries an audience that has to be checked. Those are exactly the places where
a second, subtly different OAuth implementation would become a liability, so
they are concentrated here rather than sprinkled through the manager.

Threat model, stated once
-------------------------
The MCP server is third-party code and its metadata document is attacker-
controlled input. Concretely:

* A resource that names an authorization server on a private or metadata
  address turns our token fetch into an SSRF probe → :func:`validate_https_url`.
* A resource that claims to *be* another resource redirects the audience of the
  token we are about to mint → ``expected_resource`` in
  :func:`parse_protected_resource_metadata`.
* An authorization server advertising only ``plain`` PKCE downgrades the
  challenge to a value the browser already saw → S256 is required.
* A token minted for server A, replayed at server B, is a confused deputy with
  the user's credentials → :func:`validate_token_audience`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import secrets
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import msgspec

from .client import MCPProtocolError

__all__ = [
    "CODE_CHALLENGE_METHOD",
    "DEFAULT_STATE_TTL_S",
    "MAX_VERIFIER_LEN",
    "MIN_VERIFIER_LEN",
    "AuthorizationServerMetadata",
    "MCPOAuthAudienceError",
    "MCPOAuthDiscoveryError",
    "MCPOAuthError",
    "MCPOAuthStateMismatchError",
    "PendingAuthorization",
    "PkcePair",
    "ProtectedResourceMetadata",
    "TokenResponse",
    "authorization_server_metadata_urls",
    "b64url_nopad",
    "build_authorization_url",
    "build_loopback_redirect_uri",
    "build_refresh_body",
    "build_token_exchange_body",
    "canonical_resource",
    "code_challenge_for",
    "make_pkce",
    "make_state",
    "parse_authorization_server_metadata",
    "parse_protected_resource_metadata",
    "parse_token_response",
    "protected_resource_metadata_url",
    "redirect_uri_matches",
    "resources_match",
    "scopes_from",
    "states_equal",
    "token_audiences",
    "unverified_jwt_claims",
    "validate_code_verifier",
    "validate_https_url",
    "validate_token_audience",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MCPOAuthError(MCPProtocolError):
    """Base for every OAuth failure. Subclasses ``MCPProtocolError`` because
    from the session's point of view a malformed discovery document and a
    malformed JSON-RPC response are the same class of problem: the server
    said something we cannot act on.

    Messages are written to name the fix — per §12 of the plan, an auth error
    the user cannot act on is a bug report waiting to happen."""


class MCPOAuthDiscoveryError(MCPOAuthError):
    """A discovery document, token response, or URL failed validation."""


class MCPOAuthStateMismatchError(MCPOAuthError):
    """The authorization callback's ``state`` did not match, was replayed, or
    arrived after the pending authorization expired. Always fatal for that
    attempt: there is no safe way to continue a flow whose CSRF token failed."""


class MCPOAuthAudienceError(MCPOAuthError):
    """A token's audience does not include the MCP server it would be sent to.
    This is the cross-server replay guard; it is never a warning."""


# ---------------------------------------------------------------------------
# PKCE (RFC 7636)
# ---------------------------------------------------------------------------

#: RFC 7636 §4.1 bounds on ``code_verifier`` length.
MIN_VERIFIER_LEN = 43
MAX_VERIFIER_LEN = 128

#: The only method we will use or accept. ``plain`` is a downgrade: the
#: challenge equals the verifier, so anyone who saw the authorization request
#: can complete the exchange.
CODE_CHALLENGE_METHOD = "S256"

# RFC 7636 §4.1 unreserved set. Anything else means the value did not come from
# a conforming generator and may not survive the round trip through the
# authorization server's query string.
_VERIFIER_CHARS = re.compile(r"^[A-Za-z0-9\-._~]+$")


def b64url_nopad(raw: bytes) -> str:
    """URL-safe base64, padding stripped — the encoding RFC 7636 mandates.

    Same helper as ``anthropic_oauth._b64url``; spelled out here so this module
    stays importable without dragging in the Anthropic-specific constants."""

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def validate_code_verifier(verifier: str) -> str:
    """Return ``verifier`` if it is a conforming ``code_verifier``, else raise.

    Called on generation *and* available to callers restoring a verifier from
    state, so a truncated or mangled value fails here rather than as an opaque
    ``invalid_grant`` from the token endpoint."""

    if not isinstance(verifier, str):
        raise ValueError("code_verifier must be a string")
    if not MIN_VERIFIER_LEN <= len(verifier) <= MAX_VERIFIER_LEN:
        raise ValueError(
            f"code_verifier must be {MIN_VERIFIER_LEN}-{MAX_VERIFIER_LEN} characters, "
            f"got {len(verifier)}")
    if not _VERIFIER_CHARS.match(verifier):
        raise ValueError("code_verifier contains characters outside the RFC 7636 unreserved set")
    return verifier


def code_challenge_for(verifier: str) -> str:
    """``base64url(SHA-256(ascii(verifier)))``, unpadded — RFC 7636 §4.2."""

    return b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())


class PkcePair(msgspec.Struct, frozen=True):
    """One PKCE proof: the secret we keep and the digest we publish.

    Frozen because the pairing is the security property — a verifier swapped
    after the authorization request was sent produces a challenge mismatch at
    the token endpoint, and the failure is unactionable at that distance."""

    verifier: str
    challenge: str
    method: str = CODE_CHALLENGE_METHOD


def make_pkce(nbytes: int = 64) -> PkcePair:
    """A fresh verifier/challenge pair.

    ``secrets.token_urlsafe`` yields ~1.33 characters per byte, all from the
    unreserved set, so 64 bytes lands at 86 characters — comfortably inside
    RFC 7636's 43–128 window with room for the generator's rounding. The bounds
    on ``nbytes`` exist so a caller cannot quietly ask for a verifier that is
    too short to be a credential (or too long for the spec)."""

    if not 32 <= nbytes <= 96:
        raise ValueError("nbytes must be between 32 and 96 to stay within RFC 7636 bounds")
    verifier = validate_code_verifier(secrets.token_urlsafe(nbytes))
    return PkcePair(verifier=verifier, challenge=code_challenge_for(verifier))


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

#: How long a pending authorization stays valid. Matches the 5-minute callback
#: timeout in the plan — a browser tab left open for an hour is not a login
#: the user is still waiting on, and a long-lived state is a longer window for
#: a guessed or leaked value to be redeemed.
DEFAULT_STATE_TTL_S = 300.0

#: Wrong-state callbacks tolerated before the pending authorization is dead.
#: The loopback listener answers anything that reaches the port, so without a
#: cap a local process could grind against ``state`` in a loop. Five is far
#: more than a legitimate flow (which sends exactly one) ever needs.
_MAX_STATE_ATTEMPTS = 5


def make_state(nbytes: int = 32) -> str:
    """A single-use, cryptographically random CSRF token for the auth request."""

    if nbytes < 16:
        raise ValueError("state needs at least 16 bytes of entropy")
    return b64url_nopad(secrets.token_bytes(nbytes))


def states_equal(expected: Any, received: Any) -> bool:
    """Constant-time ``state`` comparison.

    :func:`secrets.compare_digest` rather than ``==`` so the callback cannot be
    used as an oracle that leaks the expected value one character at a time.
    Non-strings and empties are ``False`` without comparing: an empty state is
    never valid, and comparing ``None`` would raise where a caller expects a
    boolean."""

    if not isinstance(expected, str) or not isinstance(received, str):
        return False
    if not expected or not received:
        return False
    return secrets.compare_digest(expected, received)


class PendingAuthorization(msgspec.Struct):
    """Everything a callback needs to be believed, and nothing it does not.

    Mutable by design: ``consumed`` and ``attempts`` are the single-use and
    anti-guessing state, and they must be observable by the caller after
    :meth:`consume` raises. The verifier never leaves this object except
    through a successful :meth:`consume`, which is the only path that should
    reach a token endpoint."""

    state: str
    verifier: str
    resource: str
    redirect_uri: str
    issuer: str = ""
    scope: str = ""
    client_id: str = ""
    created_at: float = msgspec.field(default_factory=time.time)
    ttl_s: float = DEFAULT_STATE_TTL_S
    consumed: bool = False
    attempts: int = 0

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.created_at + self.ttl_s

    def consume(self, received_state: Any, *, now: float | None = None) -> str:
        """Validate the callback's ``state`` and hand back the code verifier.

        Ordering matters. Expiry and replay are checked *before* the comparison
        so a dead authorization gives the same answer for every input; the
        constant-time comparison then decides the live case.

        A mismatch increments ``attempts`` but does **not** mark the
        authorization consumed — otherwise any process that can reach the
        loopback port could cancel a legitimate login by sending one wrong
        state first. Repeated mismatches do eventually kill it, which is the
        bounded version of the same concern."""

        if self.consumed:
            raise MCPOAuthStateMismatchError(
                "authorization state already used — a callback cannot be replayed; "
                "re-run `mantis mcp login` to start a new flow")
        if self.is_expired(now):
            raise MCPOAuthStateMismatchError(
                "authorization expired before the callback arrived; "
                "re-run `mantis mcp login`")
        if self.attempts >= _MAX_STATE_ATTEMPTS:
            raise MCPOAuthStateMismatchError(
                "too many invalid authorization callbacks; "
                "re-run `mantis mcp login` to start a new flow")
        if not states_equal(self.state, received_state):
            self.attempts += 1
            raise MCPOAuthStateMismatchError(
                "authorization state mismatch — the callback did not come from "
                "the login we started; no code was exchanged")
        self.consumed = True
        return self.verifier

    def __repr__(self) -> str:
        """Redacted, for the same reason :class:`OAuthCredential`'s is: the
        verifier is a one-time secret that completes the exchange, and the
        state is the CSRF token. Neither belongs in a traceback."""

        return (f"PendingAuthorization(resource={self.resource!r}, "
                f"redirect_uri={self.redirect_uri!r}, issuer={self.issuer!r}, "
                f"state=[redacted], verifier=[redacted], "
                f"consumed={self.consumed}, attempts={self.attempts})")


# ---------------------------------------------------------------------------
# URL validation and canonicalization
# ---------------------------------------------------------------------------

# Names that resolve to cloud metadata or in-cluster services. This is a
# best-effort *name* filter only: a hostname that resolves to a private address
# cannot be caught without resolving it, which is the connecting layer's job
# (the shared egress validator). Blocking the literals costs nothing and stops
# the copy-pasteable version of the attack.
_BLOCKED_HOSTNAMES = frozenset({
    "metadata", "metadata.google.internal", "instance-data",
    "metadata.goog", "169.254.169.254.nip.io",
})

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})

#: Default ports dropped during canonicalization, per RFC 3986 §6.2.3.
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _host_of(parts: Any) -> str:
    """Lowercased hostname with IPv6 brackets removed, or ``""``."""

    host = parts.hostname or ""
    return host.lower()


def _is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_blocked_host(host: str) -> bool:
    """True for addresses no discovery document has any business naming."""

    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False        # a name; resolution-time checks belong to the caller
    if ip.is_loopback:
        return False        # allowed or not is the caller's call, not "blocked"
    return bool(
        ip.is_private or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified)


def validate_https_url(url: Any, *, field: str, allow_loopback: bool = True) -> str:
    """Return ``url`` unchanged if it is safe to fetch, else raise.

    The rules, each of which exists because a discovery document is attacker
    input: HTTPS only (loopback may use plaintext so a locally-run development
    authorization server still works), no userinfo (``https://real@evil/`` is a
    classic misread), no fragment (meaningless on a fetched URL and a way to
    hide the real path from a glance), and no private, link-local, or metadata
    address — pointing discovery at ``169.254.169.254`` is how an SSRF gets a
    cloud credential."""

    if not isinstance(url, str) or not url.strip():
        raise MCPOAuthDiscoveryError(f"{field} is missing")
    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError as e:
        raise MCPOAuthDiscoveryError(f"{field} is not a valid URL: {e}") from e
    scheme = (parts.scheme or "").lower()
    host = _host_of(parts)
    if not host:
        raise MCPOAuthDiscoveryError(f"{field} has no host: {raw!r}")
    if parts.username or parts.password:
        raise MCPOAuthDiscoveryError(f"{field} must not embed credentials")
    if parts.fragment:
        raise MCPOAuthDiscoveryError(f"{field} must not carry a fragment")
    loopback = _is_loopback_host(host)
    if loopback and not allow_loopback:
        raise MCPOAuthDiscoveryError(f"{field} must not be a loopback address")
    if scheme == "https":
        pass
    elif scheme == "http" and loopback and allow_loopback:
        pass
    else:
        raise MCPOAuthDiscoveryError(
            f"{field} must use https (http is allowed only for loopback): {raw!r}")
    if _is_blocked_host(host):
        raise MCPOAuthDiscoveryError(
            f"{field} points at a private or metadata address, which is never a "
            f"legitimate authorization server: {raw!r}")
    try:
        parts.port  # noqa: B018 — property raises on an out-of-range port
    except ValueError as e:
        raise MCPOAuthDiscoveryError(f"{field} has an invalid port: {raw!r}") from e
    return raw


def canonical_resource(url: str) -> str:
    """The canonical form of a resource identifier, for *comparison only*.

    RFC 8707 resource indicators are compared as URIs, and the differences that
    do not change which server is meant — case in scheme and host, an explicit
    default port, a trailing slash, a fragment — must not produce a false
    mismatch that locks a user out of their own credential. Deliberately
    lenient: a value it cannot parse comes back stripped and lowercased so a
    comparison merely fails instead of raising. Validation is
    :func:`validate_https_url`'s job, and the split keeps a bogus ``aud`` claim
    from being reported as a discovery failure."""

    if not isinstance(url, str):
        return ""
    raw = url.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    scheme = (parts.scheme or "").lower()
    host = _host_of(parts)
    if not scheme or not host:
        return raw.lower()
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    if ":" in host:                                   # IPv6 literal
        netloc = f"[{host}]" + (f":{port}" if port is not None
                                and port != _DEFAULT_PORTS.get(scheme) else "")
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def resources_match(a: str, b: str) -> bool:
    """Whether two resource identifiers name the same MCP server."""

    left, right = canonical_resource(a), canonical_resource(b)
    return bool(left) and left == right


# ---------------------------------------------------------------------------
# Discovery URLs
# ---------------------------------------------------------------------------


def _well_known(url: str, suffix: str) -> str:
    """Insert ``/.well-known/<suffix>`` between authority and path.

    RFC 8414 §3.1 and RFC 9728 §3.1 both specify insertion, not appending: an
    issuer of ``https://host/tenant1`` yields
    ``https://host/.well-known/oauth-authorization-server/tenant1``. Getting
    this backwards is the single most common reason discovery 404s."""

    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, f"/.well-known/{suffix}{path}", "", ""))


def protected_resource_metadata_url(resource: str) -> str:
    """Where to fetch RFC 9728 metadata for an MCP server URL."""

    return _well_known(resource, "oauth-protected-resource")


def authorization_server_metadata_urls(issuer: str) -> tuple[str, ...]:
    """Every location an authorization server may publish its metadata, in the
    order to try them.

    OAuth's own well-known path first, then OpenID Connect's — the plan's
    "falling back to ``/.well-known/openid-configuration``". When the issuer
    carries a path component there is a third form, OIDC's *appended* layout
    (``<issuer>/.well-known/openid-configuration``), which predates RFC 8414's
    insertion rule and is still what many tenant-scoped providers serve. For a
    path-less issuer the appended and inserted forms are identical, so the
    tuple is two entries and never contains a duplicate."""

    parts = urlsplit(issuer.strip())
    urls = [
        _well_known(issuer, "oauth-authorization-server"),
        _well_known(issuer, "openid-configuration"),
    ]
    if parts.path.rstrip("/"):
        base = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
        urls.append(f"{base}/.well-known/openid-configuration")
    return tuple(urls)


# ---------------------------------------------------------------------------
# Discovery documents
# ---------------------------------------------------------------------------

#: A conforming document names one or two authorization servers. The cap is not
#: about correctness — it bounds how many URLs a hostile resource can make us
#: consider, and keeps a 10k-entry list from becoming a work amplifier.
_MAX_AUTHORIZATION_SERVERS = 8

#: Cap on any advertised string list (scopes, methods…). Same reasoning.
_MAX_LIST_ITEMS = 64


def _require_mapping(doc: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(doc, Mapping):
        raise MCPOAuthDiscoveryError(f"{what} must be a JSON object, got {type(doc).__name__}")
    return doc


def _string_list(doc: Mapping[str, Any], key: str, what: str) -> tuple[str, ...]:
    """A tuple of strings from an optional metadata array field.

    Absent is fine (returns ``()``); present-but-wrong-shaped is not. A server
    that sends ``"scopes_supported": "read write"`` is close enough to correct
    that silently ignoring it would hide a real misconfiguration."""

    value = doc.get(key)
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise MCPOAuthDiscoveryError(f"{what}: {key} must be an array")
    if len(value) > _MAX_LIST_ITEMS:
        raise MCPOAuthDiscoveryError(f"{what}: {key} has an implausible {len(value)} entries")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MCPOAuthDiscoveryError(f"{what}: {key} must contain non-empty strings")
        out.append(item.strip())
    return tuple(out)


class ProtectedResourceMetadata(msgspec.Struct, frozen=True):
    """RFC 9728 metadata: which authorization servers speak for this resource.

    Frozen and built only by :func:`parse_protected_resource_metadata`, so a
    value of this type is always one that passed validation — there is no
    partially-checked instance to accidentally trust."""

    resource: str
    authorization_servers: tuple[str, ...]
    scopes_supported: tuple[str, ...] = ()
    bearer_methods_supported: tuple[str, ...] = ()
    resource_documentation: str = ""

    @property
    def issuer(self) -> str:
        """The authorization server to use — the first advertised one."""

        return self.authorization_servers[0]


def parse_protected_resource_metadata(
    doc: Any,
    *,
    expected_resource: str | None = None,
) -> ProtectedResourceMetadata:
    """Validate an RFC 9728 protected-resource document.

    ``expected_resource`` is the MCP server URL we asked about. When given, the
    document's own ``resource`` must canonically equal it. That check is what
    stops a server from nominating an authorization server for a *different*
    audience and steering the token we are about to mint somewhere else — the
    plan's "the authorization server must be discovered from the resource's own
    metadata"."""

    d = _require_mapping(doc, "protected-resource metadata")
    resource = d.get("resource")
    if not isinstance(resource, str) or not resource.strip():
        raise MCPOAuthDiscoveryError("protected-resource metadata: `resource` is required")
    validate_https_url(resource, field="protected-resource metadata `resource`")
    if expected_resource is not None and not resources_match(resource, expected_resource):
        raise MCPOAuthDiscoveryError(
            f"protected-resource metadata describes {resource!r}, not the server we "
            f"asked about ({expected_resource!r}); refusing to authenticate against it")

    servers_raw = d.get("authorization_servers")
    if not isinstance(servers_raw, (list, tuple)) or not servers_raw:
        raise MCPOAuthDiscoveryError(
            "protected-resource metadata: `authorization_servers` must be a non-empty array")
    if len(servers_raw) > _MAX_AUTHORIZATION_SERVERS:
        raise MCPOAuthDiscoveryError(
            f"protected-resource metadata advertises {len(servers_raw)} authorization "
            f"servers; at most {_MAX_AUTHORIZATION_SERVERS} are considered")
    servers = []
    for entry in servers_raw:
        if not isinstance(entry, str):
            raise MCPOAuthDiscoveryError(
                "protected-resource metadata: `authorization_servers` must contain strings")
        servers.append(validate_https_url(entry, field="authorization_servers entry"))

    doc_url = d.get("resource_documentation")
    return ProtectedResourceMetadata(
        resource=resource.strip(),
        authorization_servers=tuple(servers),
        scopes_supported=_string_list(d, "scopes_supported", "protected-resource metadata"),
        bearer_methods_supported=_string_list(
            d, "bearer_methods_supported", "protected-resource metadata"),
        resource_documentation=doc_url.strip() if isinstance(doc_url, str) else "",
    )


class AuthorizationServerMetadata(msgspec.Struct, frozen=True):
    """RFC 8414 authorization-server metadata, after validation."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""
    revocation_endpoint: str = ""
    scopes_supported: tuple[str, ...] = ()
    code_challenge_methods_supported: tuple[str, ...] = ()
    grant_types_supported: tuple[str, ...] = ()
    response_types_supported: tuple[str, ...] = ()
    token_endpoint_auth_methods_supported: tuple[str, ...] = ()

    @property
    def supports_dynamic_registration(self) -> bool:
        """Whether RFC 7591 registration is available — the branch that decides
        between registering a client and requiring a configured ``client_id``."""

        return bool(self.registration_endpoint)

    @property
    def supports_revocation(self) -> bool:
        """``logout`` revokes when this is true, and deletes locally either way."""

        return bool(self.revocation_endpoint)


def parse_authorization_server_metadata(
    doc: Any,
    *,
    expected_issuer: str | None = None,
) -> AuthorizationServerMetadata:
    """Validate an RFC 8414 (or OIDC discovery) document.

    Three checks carry weight beyond shape. ``issuer`` must be *identical* to
    the issuer we derived the well-known URL from — RFC 8414 §3.3 says
    identical, and anything looser lets one tenant's document stand in for
    another's. Advertised PKCE methods, when present, must include ``S256``:
    a server offering only ``plain`` is offering a downgrade, and per the plan
    PKCE is mandatory. And every endpoint goes through
    :func:`validate_https_url`, so a document cannot aim our token POST at a
    private address."""

    d = _require_mapping(doc, "authorization-server metadata")

    issuer = d.get("issuer")
    if not isinstance(issuer, str) or not issuer.strip():
        raise MCPOAuthDiscoveryError("authorization-server metadata: `issuer` is required")
    issuer = issuer.strip()
    validate_https_url(issuer, field="issuer")
    iparts = urlsplit(issuer)
    if iparts.query or iparts.fragment:
        raise MCPOAuthDiscoveryError(
            "authorization-server metadata: `issuer` must not carry a query or fragment")
    if expected_issuer is not None and issuer != expected_issuer.strip():
        raise MCPOAuthDiscoveryError(
            f"authorization-server metadata issuer {issuer!r} does not match the issuer "
            f"it was discovered from ({expected_issuer!r})")

    authorize = validate_https_url(d.get("authorization_endpoint"),
                                   field="authorization_endpoint")
    token = validate_https_url(d.get("token_endpoint"), field="token_endpoint")

    registration = d.get("registration_endpoint")
    registration = (validate_https_url(registration, field="registration_endpoint")
                    if registration is not None else "")
    revocation = d.get("revocation_endpoint")
    revocation = (validate_https_url(revocation, field="revocation_endpoint")
                  if revocation is not None else "")

    what = "authorization-server metadata"
    challenge_methods = _string_list(d, "code_challenge_methods_supported", what)
    if "code_challenge_methods_supported" in d and CODE_CHALLENGE_METHOD not in challenge_methods:
        raise MCPOAuthDiscoveryError(
            f"authorization server does not advertise {CODE_CHALLENGE_METHOD} PKCE "
            f"(offers {list(challenge_methods) or 'nothing'}); mantis will not use `plain`")
    response_types = _string_list(d, "response_types_supported", what)
    if response_types and "code" not in response_types:
        raise MCPOAuthDiscoveryError(
            "authorization server does not support the authorization-code flow "
            f"(offers {list(response_types)})")

    return AuthorizationServerMetadata(
        issuer=issuer,
        authorization_endpoint=authorize,
        token_endpoint=token,
        registration_endpoint=registration,
        revocation_endpoint=revocation,
        scopes_supported=_string_list(d, "scopes_supported", what),
        code_challenge_methods_supported=challenge_methods,
        grant_types_supported=_string_list(d, "grant_types_supported", what),
        response_types_supported=response_types,
        token_endpoint_auth_methods_supported=_string_list(
            d, "token_endpoint_auth_methods_supported", what),
    )


# ---------------------------------------------------------------------------
# Authorization request
# ---------------------------------------------------------------------------


def build_loopback_redirect_uri(port: int, *, host: str = "127.0.0.1",
                                path: str = "/callback") -> str:
    """The redirect URI for a loopback listener on ``port``.

    ``127.0.0.1`` literal rather than ``localhost``: the name can resolve to
    ``::1`` (or, on a hostile resolver, elsewhere), and the authorization
    server compares the redirect URI as a string, so the literal is both safer
    and more predictable. Never ``0.0.0.0`` — that is a bind address that would
    expose the callback to the network."""

    if not _is_loopback_host(host.lower()):
        raise MCPOAuthDiscoveryError(f"redirect host must be loopback, got {host!r}")
    if not 1 <= int(port) <= 65535:
        raise MCPOAuthDiscoveryError(f"invalid callback port {port!r}")
    if not path.startswith("/"):
        raise MCPOAuthDiscoveryError("redirect path must be absolute")
    return f"http://{host}:{int(port)}{path}"


def redirect_uri_matches(expected: str, actual: str) -> bool:
    """Exact-match check for the callback's request target.

    Deliberately *not* canonicalizing: RFC 8252 §7.3 and OAuth 2.1 both require
    simple string comparison of the redirect URI (modulo the port, which the
    loopback exception allows the client to vary — and which we fix ourselves
    before the request is sent, so there is nothing left to vary here)."""

    return isinstance(expected, str) and isinstance(actual, str) and expected == actual


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    resource: str,
    scopes: Sequence[str] = (),
    extra: Mapping[str, str] | None = None,
) -> str:
    """The URL to open in the user's browser.

    Query parameters already present on the endpoint are preserved (tenant-
    scoped providers put routing there) and ours are appended after, so a
    malicious document cannot pre-set ``redirect_uri`` and have it win — the
    last occurrence is what conforming servers read, and ours is last."""

    endpoint = validate_https_url(authorization_endpoint, field="authorization_endpoint")
    validate_https_url(redirect_uri, field="redirect_uri")
    parts = urlsplit(endpoint)
    if not _is_loopback_host(_host_of(urlsplit(redirect_uri))):
        raise MCPOAuthDiscoveryError(
            f"redirect_uri must be a loopback address, got {redirect_uri!r}")
    if not client_id or not state or not code_challenge:
        raise MCPOAuthDiscoveryError(
            "client_id, state and code_challenge are all required to authorize")

    params: list[tuple[str, str]] = list(parse_qsl(parts.query, keep_blank_values=True))
    params.extend([
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("state", state),
        ("code_challenge", code_challenge),
        ("code_challenge_method", CODE_CHALLENGE_METHOD),
        ("resource", canonical_resource(resource)),
    ])
    scope = " ".join(s for s in scopes if s)
    if scope:
        params.append(("scope", scope))
    for key, value in (extra or {}).items():
        params.append((str(key), str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))


def build_token_exchange_body(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    resource: str,
    client_secret: str | None = None,
) -> dict[str, str]:
    """Form body for the authorization-code exchange.

    ``resource`` (RFC 8707) is not optional here even though the RFC allows it
    to be: it is what makes the issued token audience-bound, and an unbound
    token is one that can be replayed at another MCP server."""

    validate_code_verifier(code_verifier)
    if not code:
        raise MCPOAuthDiscoveryError("authorization code is empty")
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "resource": canonical_resource(resource),
    }
    if client_secret:
        body["client_secret"] = client_secret
    return body


def build_refresh_body(
    *,
    refresh_token: str,
    client_id: str,
    resource: str,
    scope: str | None = None,
    client_secret: str | None = None,
) -> dict[str, str]:
    """Form body for a refresh. Carries ``resource`` for the same reason the
    exchange does — a refreshed token must stay bound to the same audience."""

    token = (refresh_token or "").strip()
    if not token:
        raise MCPOAuthDiscoveryError("no refresh token available; run `mantis mcp login`")
    body = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": client_id,
        "resource": canonical_resource(resource),
    }
    if scope:
        body["scope"] = scope
    if client_secret:
        body["client_secret"] = client_secret
    return body


# ---------------------------------------------------------------------------
# Token response + audience binding
# ---------------------------------------------------------------------------

#: Bound on a JWT segment we will base64-decode and JSON-parse. Access tokens
#: are a few hundred bytes; anything past this is not a token we should be
#: spending CPU on.
_MAX_JWT_SEGMENT = 64 * 1024


class TokenResponse(msgspec.Struct, frozen=True):
    """A validated token endpoint response (RFC 6749 §5.1)."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int | None = None
    refresh_token: str = ""
    scope: str = ""


def unverified_jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode a JWT's payload **without verifying its signature**.

    We are the token's *bearer*, not its audience-validating resource server:
    we cannot hold the authorization server's key, so there is nothing to
    verify against. What the claims are used for is correspondingly narrow —
    deciding whether to *send* this token to a given server. Reading an
    unsigned claim can only make us more conservative (refuse to send), never
    less, so the missing verification is not a hole here. Returns ``None`` for
    anything that is not a decodable three-segment JWT, which includes every
    opaque token."""

    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    if not payload or len(payload) > _MAX_JWT_SEGMENT:
        return None
    padded = payload + "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    try:
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def token_audiences(token: str) -> tuple[str, ...]:
    """The ``aud`` claim as a tuple, empty for an opaque or aud-less token.

    ``aud`` is a string or an array of strings (RFC 7519 §4.1.3); both shapes
    are common in the wild and both are handled."""

    claims = unverified_jwt_claims(token)
    if not claims:
        return ()
    aud = claims.get("aud")
    if isinstance(aud, str):
        return (aud,) if aud else ()
    if isinstance(aud, (list, tuple)):
        return tuple(a for a in aud if isinstance(a, str) and a)
    return ()


def validate_token_audience(
    access_token: str,
    *,
    resource: str,
    require_audience: bool = False,
) -> tuple[str, ...]:
    """Refuse a token that was not minted for ``resource``.

    Returns the audiences found (possibly empty). Raises
    :class:`MCPOAuthAudienceError` when the token names an audience and
    ``resource`` is not among them — that is the cross-server replay case, and
    it is unconditional.

    An *absent* audience is a different situation: opaque tokens are legal and
    common, and there is no claim to disagree with. The default is to allow it
    rather than lock out every server that issues opaque tokens;
    ``require_audience=True`` is the strict posture for a deployment that knows
    its servers issue JWTs. Comparison is canonical, so ``:443`` or a trailing
    slash in the claim is not a spurious mismatch."""

    audiences = token_audiences(access_token)
    if not audiences:
        if require_audience:
            raise MCPOAuthAudienceError(
                f"token carries no audience claim but one is required for {resource!r}")
        return ()
    if any(resources_match(aud, resource) for aud in audiences):
        return audiences
    raise MCPOAuthAudienceError(
        f"token audience {list(audiences)} does not include {canonical_resource(resource)!r}; "
        "refusing to send a token minted for a different server")


def parse_token_response(
    doc: Any,
    *,
    resource: str,
    require_audience: bool = False,
) -> TokenResponse:
    """Validate a token endpoint response and its audience binding.

    Only bearer tokens are accepted: every other ``token_type`` (``mac``,
    ``dpop``) needs a proof-of-possession we do not implement, and treating one
    as a bearer would put a token on the wire in a way the server will reject
    at best."""

    d = _require_mapping(doc, "token response")

    access_token = d.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise MCPOAuthDiscoveryError("token response: `access_token` is missing")
    access_token = access_token.strip()

    token_type = d.get("token_type", "Bearer")
    if not isinstance(token_type, str) or token_type.strip().lower() != "bearer":
        raise MCPOAuthDiscoveryError(
            f"token response: unsupported token_type {token_type!r}; only bearer is supported")

    expires_in = d.get("expires_in")
    if expires_in is not None:
        # bool is an int subclass and would sail through int(); reject it.
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise MCPOAuthDiscoveryError("token response: `expires_in` must be a number")
        expires_in = int(expires_in)
        if expires_in <= 0:
            raise MCPOAuthDiscoveryError("token response: `expires_in` must be positive")

    refresh_token = d.get("refresh_token", "")
    if refresh_token is None:
        refresh_token = ""
    if not isinstance(refresh_token, str):
        raise MCPOAuthDiscoveryError("token response: `refresh_token` must be a string")

    scope = d.get("scope", "")
    if scope is None:
        scope = ""
    if not isinstance(scope, str):
        raise MCPOAuthDiscoveryError("token response: `scope` must be a string")

    validate_token_audience(access_token, resource=resource, require_audience=require_audience)

    return TokenResponse(
        access_token=access_token,
        token_type=token_type.strip(),
        expires_in=expires_in,
        refresh_token=refresh_token.strip(),
        scope=scope.strip(),
    )


def scopes_from(value: Iterable[str] | str | None) -> tuple[str, ...]:
    """Normalize a scope list or space-delimited scope string to a tuple.

    Config accepts ``["read", "write"]``; the wire uses ``"read write"``. One
    helper so both spellings land in the same shape."""

    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(s for s in value.split() if s)
    return tuple(str(s).strip() for s in value if str(s).strip())
