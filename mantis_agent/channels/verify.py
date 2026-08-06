"""The gate: authenticate raw bytes before anything interprets them.

§7 of ``new-features/s_channels_and_reactive_operation.md``, and the first
thing that has to exist, because everything downstream is only as trustworthy
as this module. An inbound webhook endpoint on a developer machine is an
unauthenticated stream of attacker-chosen bytes one careless step away from
becoming model context that runs with the user's credentials.

Two properties carry the whole design
-------------------------------------
**Verification happens over the RAW BYTES, before JSON parsing.** Not "before
the payload is used" — before it is *parsed*. A JSON parser fed hostile input
is itself an attack surface, and the subtler half is that parsing and
re-serializing produces different bytes than the ones the signature covered:
``json.dumps(json.loads(body))`` is semantically identical and byte-wise
different, so a pipeline that verified the round-tripped form would be
verifying something the sender never signed. :func:`verify_then_parse` is the
one function that owns this ordering, and it hands the parser the *same*
``bytes`` object the HMAC ran over — the identity is asserted by test.

**Nothing is compared with ``==``.** Every secret-dependent comparison goes
through :func:`hmac.compare_digest`. A short-circuiting comparison on a MAC
leaks the length of the matching prefix through timing, which is enough to
forge a signature one byte at a time.

What each provider actually proves
----------------------------------
========  ==============================================  ============
Provider  Method                                          Covers body?
========  ==============================================  ============
github    ``X-Hub-Signature-256`` HMAC-SHA256 over body    yes
gitlab    ``X-Gitlab-Token`` constant-time compare         **no**
slack     ``v0`` HMAC over ``v0:<ts>:<body>``, 5-min window  yes
generic   HMAC over body, configured header + algorithm    yes
========  ==============================================  ============

GitLab is the odd one and the difference matters: a shared token proves *who
sent the request*, not *what the request says*. Nothing in a GitLab body is
signature-covered, so :attr:`VerifiedPayload.body_signed` is ``False`` there
and the normalizer refuses to treat that payload's ``actor`` as a routing
input (§6: "``actor`` is a routing input only when the provider's signature
covers it, and that is recorded per adapter").

Replay defense
--------------
:class:`ReplayGuard` records ``(channel, provider_event_id)`` and *refuses* a
repeat. It is deliberately separate from the queue's dedupe-for-idempotency,
which will silently acknowledge a duplicate delivery: dedupe is a politeness to
a provider retrying in good faith, replay defense is a refusal aimed at
somebody re-sending a captured request. Same key, opposite intent, so they get
different code and different errors.

The guard is in-memory and bounded, which is the right shape for a foundation
and *not* the whole answer: eviction means a very old id can be admitted again,
and a restart forgets everything. The durable unique index in
``channels/queue.py`` is the real defense; this is the cheap in-process one
that runs before a parse.

Scope: this module is pure. No I/O, no settings import, no HTTP, no keychain.
Secrets arrive as ``bytes`` from the caller — :meth:`VerifyConfig.from_config`
refuses to read one out of a configuration mapping at all, which is a
structural version of "a repository must not be able to configure a channel
secret".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Union

from ..errors import AgentError

__all__ = [
    "ALLOWED_ALGORITHMS",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_REPLAY_ENTRIES",
    "DEFAULT_REPLAY_TTL_SECONDS",
    "GITHUB_DELIVERY_HEADER",
    "GITHUB_SIGNATURE_HEADER",
    "GITLAB_EVENT_ID_HEADER",
    "GITLAB_TOKEN_HEADER",
    "METHODS",
    "PROVIDER_METHODS",
    "SLACK_FRESHNESS_SECONDS",
    "SLACK_SIGNATURE_HEADER",
    "SLACK_TIMESTAMP_HEADER",
    "VERIFY_TIERS",
    "ChannelConfigError",
    "ChannelError",
    "ChannelNoVerificationError",
    "ChannelRateLimitedError",
    "PayloadMalformedError",
    "PayloadTooLargeError",
    "ReplayDetectedError",
    "ReplayGuard",
    "SignatureInvalidError",
    "SignatureStaleError",
    "VerifiedPayload",
    "VerifyConfig",
    "body_hash_id",
    "verify",
    "verify_generic",
    "verify_github",
    "verify_gitlab",
    "verify_slack",
    "verify_then_parse",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
#
# §14's tree, flat under ``ChannelError`` exactly as the plan draws it. It lives
# in this module rather than a ``channels/errors.py`` because verify is the
# pipeline's entry point and the only module that exists so far; splitting it
# out later is a pure move with no behavioural content.
#
# The distinctions here are for *metrics and logs*, not for callers on the
# wire: §14 requires a generic ``401`` with no detail for every verification
# failure, because a verbose error is an oracle for forging signatures. The
# listener collapses all of these to one response.


class ChannelError(AgentError):
    """Base for everything the channels pipeline raises."""


class ChannelConfigError(ChannelError):
    """A channel is configured in a way that cannot be honoured safely."""


class ChannelNoVerificationError(ChannelError):
    """No verification method configured — the channel refuses to start.

    There is no unauthenticated mode, not even for loopback: a local
    unauthenticated port is reachable by every process on the machine, which
    includes anything the user ``npm install``-ed this morning.
    """


class SignatureInvalidError(ChannelError):
    """The payload did not authenticate. Never carries payload content."""


class SignatureStaleError(ChannelError):
    """Authentic, but its timestamp is outside the freshness window.

    Raised only *after* the signature verified, so the distinction is never
    visible to a caller that has not already proved it holds the secret.
    """


class ReplayDetectedError(ChannelError):
    """This ``(channel, provider_event_id)`` was already accepted."""


class PayloadTooLargeError(ChannelError):
    """Body exceeds the configured cap. Raised before any hashing."""


class PayloadMalformedError(ChannelError):
    """Authenticated bytes that are not the shape they claim to be."""


class ChannelRateLimitedError(ChannelError):
    """Too many requests — or too many verification failures — from a source."""


# ---------------------------------------------------------------------------
# Bounds and constants
# ---------------------------------------------------------------------------

#: Matches ``listener.maxBodyBytes`` in §13. Enforced here too, not only in the
#: listener, so a poller or a test harness cannot route around it.
DEFAULT_MAX_BODY_BYTES = 1_048_576

#: Slack's documented window, and the value §7 names.
SLACK_FRESHNESS_SECONDS = 300

#: Header values are attacker-controlled and unbounded. Cap them before they
#: reach ``bytes.fromhex`` or an ``int()``.
MAX_SIGNATURE_CHARS = 512
MAX_TIMESTAMP_CHARS = 32

#: SHA-1 and MD5 are absent on purpose. GitHub still ships a legacy
#: ``X-Hub-Signature`` (sha1); accepting it would silently downgrade a channel
#: that looks correctly configured.
ALLOWED_ALGORITHMS: frozenset = frozenset({"sha256", "sha384", "sha512"})

GITHUB_SIGNATURE_HEADER = "X-Hub-Signature-256"
GITHUB_DELIVERY_HEADER = "X-GitHub-Delivery"
GITLAB_TOKEN_HEADER = "X-Gitlab-Token"
GITLAB_EVENT_ID_HEADER = "X-Gitlab-Event-UUID"
SLACK_SIGNATURE_HEADER = "X-Slack-Signature"
SLACK_TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"

#: The verification methods a channel may name.
METHODS: tuple = ("hmac-sha256", "gitlab-token", "slack-v0", "hmac")

#: A provider's default method. Used by :meth:`VerifyConfig.for_provider` so a
#: channel definition can say ``"provider": "github"`` and get the right one.
PROVIDER_METHODS: dict = {
    "github": "hmac-sha256",
    "gitlab": "gitlab-token",
    "slack": "slack-v0",
    "generic": "hmac",
}

#: Settings tiers that may configure verification at all — the same owner/admin
#: split ``memory_trust.POLICY_TIERS`` draws. §13: "Channel definitions are
#: user- or managed-tier only". A cloned repository must not be able to point a
#: channel at a secret it chose.
VERIFY_TIERS: frozenset = frozenset({"user", "managed"})

#: Keys that would mean a secret was written into a settings blob. Refused
#: outright rather than ignored, so a user who tries it gets told.
_INLINE_SECRET_KEYS: frozenset = frozenset({
    "secret", "secretvalue", "secret_value", "token", "password", "key",
    "sharedsecret", "shared_secret", "signingsecret", "signing_secret",
})

DEFAULT_REPLAY_ENTRIES = 4096
DEFAULT_REPLAY_TTL_SECONDS = 86_400.0

#: What a header source may be: a mapping, or an iterable of pairs (which is
#: what a raw HTTP layer hands you, and the only form that preserves duplicates).
HeaderSource = Union[Mapping[str, str], Iterable[tuple]]


# ---------------------------------------------------------------------------
# Header access
# ---------------------------------------------------------------------------


def _headers(source: HeaderSource | None) -> dict:
    """Lower-cased name → list of values, preserving duplicates.

    Duplicates are kept rather than collapsed because *which* duplicate wins is
    exactly the ambiguity a header-smuggling attack exploits; see :func:`_one`.
    """
    out: dict = {}
    if source is None:
        return out
    items = source.items() if isinstance(source, Mapping) else source
    for pair in items:
        try:
            name, value = pair
        except (TypeError, ValueError):
            continue
        out.setdefault(str(name).strip().lower(), []).append(str(value))
    return out


def _one(headers: dict, name: str) -> str:
    """The single value of ``name``, or ``""`` when absent.

    Two *differing* values for the same header is a hard failure. A proxy chain
    or a crafted request can present both a real signature and a forged one;
    picking either (first, last, longest) is a decision the sender controls, so
    the only safe answer is to refuse. Identical repeats are harmless and pass.
    """
    values = headers.get(name.lower())
    if not values:
        return ""
    if len(set(values)) > 1:
        raise SignatureInvalidError(f"conflicting values for header {name!r}")
    return values[0]


# ---------------------------------------------------------------------------
# The verified payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedPayload:
    """Raw bytes that have passed the gate, plus what the gate learned.

    Existence of one of these *is* the proof that verification ran: only the
    ``verify_*`` functions in this module construct one from a signature, and
    ``channels.normalize.normalize`` accepts nothing else. That is why the
    poller door (:meth:`from_trusted_source`) is a named classmethod — pollers
    genuinely have no signature to check, and making them say so in one
    greppable place is better than letting every adapter improvise.

    ``body`` is the *same object* the HMAC covered, never a copy or a re-encode.
    """

    body: bytes
    provider: str
    signature_method: str
    provider_event_id: str
    body_signed: bool
    timestamp: float | None = None

    @classmethod
    def from_trusted_source(
        cls,
        body: bytes,
        *,
        provider: str,
        signature_method: str,
        event_id: str = "",
    ) -> VerifiedPayload:
        """For ingestion paths whose authenticity comes from the connection.

        IMAP (the account is the authentication) and MCP (the server's existing
        trust decision governs) per §7's table. ``body_signed`` is ``False``:
        nothing cryptographically binds the content, so a routing rule must not
        trust the sender it names.
        """
        raw = _as_bytes(body)
        return cls(
            body=raw,
            provider=str(provider or "unknown"),
            signature_method=str(signature_method or "none"),
            provider_event_id=str(event_id or body_hash_id(raw)),
            body_signed=False,
        )


def body_hash_id(body: bytes) -> str:
    """A dedupe id derived from the payload itself.

    Used when the provider ships no delivery-id header — Slack, notably, puts
    its ``event_id`` *inside* the body, and reading it would mean parsing before
    the id exists to guard the parse. Hashing the raw bytes needs no parse and
    is stable across a provider's retries, which is exactly what dedupe wants.
    """
    return "sha256:" + hashlib.sha256(_as_bytes(body)).hexdigest()


def _as_bytes(body: Any) -> bytes:
    """Raw bytes, or a hard refusal.

    A ``str`` body means somebody already decoded — the first step toward
    verifying something other than what was signed. It is refused rather than
    encoded, because guessing the sender's encoding is precisely the mistake.
    """
    if isinstance(body, bytes):
        return body
    if isinstance(body, (bytearray, memoryview)):
        return bytes(body)
    raise ChannelError(
        "channel verification requires the raw request body as bytes; "
        f"got {type(body).__name__}"
    )


def _check_size(body: bytes, max_body_bytes: int) -> None:
    """Cap first. Hashing an unbounded body is a free CPU-exhaustion primitive
    for an unauthenticated caller."""
    if max_body_bytes > 0 and len(body) > max_body_bytes:
        raise PayloadTooLargeError(
            f"payload is {len(body)} bytes, over the {max_body_bytes}-byte cap"
        )


def _check_secret(secret: bytes) -> bytes:
    """An empty secret makes every signature verify against a known key."""
    if isinstance(secret, str):
        raise ChannelConfigError("channel secret must be bytes, not str")
    if not secret:
        raise ChannelConfigError("channel secret is empty; refusing to verify")
    return bytes(secret)


def _digest(secret: bytes, message: bytes, algorithm: str) -> bytes:
    if algorithm not in ALLOWED_ALGORITHMS:
        raise ChannelConfigError(
            f"algorithm {algorithm!r} is not permitted; "
            f"allowed: {', '.join(sorted(ALLOWED_ALGORITHMS))}"
        )
    return hmac.new(secret, message, getattr(hashlib, algorithm)).digest()


def _hex_matches(expected: bytes, provided: str) -> bool:
    """Constant-time compare of a hex-encoded MAC against a computed digest.

    The hex is decoded rather than string-compared so that case (``AB`` vs
    ``ab``) is handled by the encoding rather than by a ``.lower()`` nobody
    would remember to add. Malformed hex — odd length, non-hex characters, the
    truncated tail of a real signature — fails at :meth:`bytes.fromhex` and is
    reported as a mismatch, never as a distinguishable parse error.
    """
    text = provided.strip()
    if not text or len(text) > MAX_SIGNATURE_CHARS:
        return False
    try:
        got = bytes.fromhex(text)
    except ValueError:
        return False
    return hmac.compare_digest(expected, got)


# ---------------------------------------------------------------------------
# Per-provider verification
# ---------------------------------------------------------------------------


def verify_github(
    body: bytes,
    headers: HeaderSource | None = None,
    *,
    secret: bytes,
    header: str = GITHUB_SIGNATURE_HEADER,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> VerifiedPayload:
    """``X-Hub-Signature-256``: HMAC-SHA256 over the raw body.

    Only the ``sha256=`` header is read. GitHub also sends the legacy
    ``X-Hub-Signature`` (sha1); it is not consulted, and a ``sha1=`` prefix on
    the -256 header is rejected rather than honoured — a valid weak signature
    must never satisfy a strong verifier.

    The dedupe id is ``X-GitHub-Delivery``, which GitHub keeps stable across its
    own retries. Absent, we fall back to the body hash.
    """
    raw = _as_bytes(body)
    _check_size(raw, max_body_bytes)
    key = _check_secret(secret)
    hdrs = _headers(headers)

    presented = _one(hdrs, header)
    if not presented:
        raise SignatureInvalidError(f"missing {header} header")
    prefix, _, mac = presented.partition("=")
    if not mac or prefix.strip().lower() != "sha256":
        raise SignatureInvalidError(f"{header} is not a sha256 signature")
    if not _hex_matches(_digest(key, raw, "sha256"), mac):
        raise SignatureInvalidError(f"{header} does not match")

    return VerifiedPayload(
        body=raw,
        provider="github",
        signature_method="hmac-sha256",
        provider_event_id=_one(hdrs, GITHUB_DELIVERY_HEADER) or body_hash_id(raw),
        body_signed=True,
    )


def verify_gitlab(
    body: bytes,
    headers: HeaderSource | None = None,
    *,
    secret: bytes,
    header: str = GITLAB_TOKEN_HEADER,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> VerifiedPayload:
    """``X-Gitlab-Token``: a shared secret, compared in constant time.

    Weaker than the others by construction and worth being explicit about: the
    token authenticates the *sender*, not the *content*. Nothing in the body is
    covered, so ``body_signed`` is ``False`` and the normalizer will not let a
    routing rule trust this payload's ``actor``. The body still cannot be
    forged by a third party without the token — but a party that has the token
    can say anything, and a replayed request is byte-identical to a real one,
    which is why :class:`ReplayGuard` matters most here.
    """
    raw = _as_bytes(body)
    _check_size(raw, max_body_bytes)
    key = _check_secret(secret)
    hdrs = _headers(headers)

    presented = _one(hdrs, header)
    if not presented or len(presented) > MAX_SIGNATURE_CHARS:
        raise SignatureInvalidError(f"missing or oversized {header} header")
    if not hmac.compare_digest(key, presented.encode("utf-8", "surrogatepass")):
        raise SignatureInvalidError(f"{header} does not match")

    return VerifiedPayload(
        body=raw,
        provider="gitlab",
        signature_method="token",
        provider_event_id=_one(hdrs, GITLAB_EVENT_ID_HEADER) or body_hash_id(raw),
        body_signed=False,
    )


def verify_slack(
    body: bytes,
    headers: HeaderSource | None = None,
    *,
    secret: bytes,
    now: float | None = None,
    freshness_seconds: float = SLACK_FRESHNESS_SECONDS,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> VerifiedPayload:
    """Slack ``v0``: HMAC-SHA256 over ``v0:<timestamp>:<body>``, then freshness.

    The base string is assembled from **bytes**, never from a decoded body, so
    the digest covers what arrived.

    Order is deliberate and is the opposite of Slack's own suggestion: the HMAC
    runs *first*, freshness second. Checking the clock first would let an
    unauthenticated prober distinguish "stale" from "invalid" and learn what the
    server thinks the time is; doing it after means a stale answer is only ever
    given to someone who has already proved they hold the secret. The security
    property either order must have — never accept a stale but validly-signed
    request — holds in both, and this order leaks less.

    The window is two-sided. A far-future timestamp would otherwise keep a
    captured request replayable for as long as the attacker chose.
    """
    raw = _as_bytes(body)
    _check_size(raw, max_body_bytes)
    key = _check_secret(secret)
    hdrs = _headers(headers)

    presented = _one(hdrs, SLACK_SIGNATURE_HEADER)
    if not presented:
        raise SignatureInvalidError(f"missing {SLACK_SIGNATURE_HEADER} header")
    version, _, mac = presented.partition("=")
    if not mac or version.strip().lower() != "v0":
        raise SignatureInvalidError(f"{SLACK_SIGNATURE_HEADER} is not a v0 signature")

    stamp = _one(hdrs, SLACK_TIMESTAMP_HEADER).strip()
    if not stamp or len(stamp) > MAX_TIMESTAMP_CHARS:
        raise SignatureInvalidError(
            f"missing or oversized {SLACK_TIMESTAMP_HEADER} header"
        )

    base = b"v0:" + stamp.encode("utf-8", "surrogatepass") + b":" + raw
    if not _hex_matches(_digest(key, base, "sha256"), mac):
        raise SignatureInvalidError(f"{SLACK_SIGNATURE_HEADER} does not match")

    # Past the gate: the sender holds the secret, so a precise answer costs
    # nothing. A signed-but-unparseable timestamp means freshness cannot be
    # established, which is the same refusal as an expired one.
    try:
        when = float(stamp)
    except ValueError:
        raise SignatureStaleError("timestamp is not a number") from None
    moment = time.time() if now is None else float(now)
    if freshness_seconds > 0 and abs(moment - when) > freshness_seconds:
        raise SignatureStaleError(
            f"timestamp is outside the {freshness_seconds:g}s freshness window"
        )

    return VerifiedPayload(
        body=raw,
        provider="slack",
        signature_method="slack-v0",
        # Slack's own ``event_id`` lives inside the body, and reading it would
        # mean parsing before the id that guards the parse exists.
        provider_event_id=body_hash_id(raw),
        body_signed=True,
        timestamp=when,
    )


def verify_generic(
    body: bytes,
    headers: HeaderSource | None = None,
    *,
    secret: bytes,
    header: str,
    prefix: str = "",
    algorithm: str = "sha256",
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> VerifiedPayload:
    """HMAC over the raw body with a configured header, prefix and algorithm.

    The escape hatch for providers with no adapter of their own. ``prefix`` is
    the literal string in front of the hex (``"sha256="``, ``"t="``, ``""``);
    it is matched exactly, so a payload cannot choose a different algorithm by
    relabelling itself.
    """
    if not header:
        raise ChannelConfigError("generic HMAC verification requires a header name")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise ChannelConfigError(
            f"algorithm {algorithm!r} is not permitted; "
            f"allowed: {', '.join(sorted(ALLOWED_ALGORITHMS))}"
        )
    raw = _as_bytes(body)
    _check_size(raw, max_body_bytes)
    key = _check_secret(secret)
    hdrs = _headers(headers)

    presented = _one(hdrs, header)
    if not presented:
        raise SignatureInvalidError(f"missing {header} header")
    if prefix:
        if not presented.startswith(prefix):
            raise SignatureInvalidError(f"{header} is missing the {prefix!r} prefix")
        presented = presented[len(prefix):]
    if not _hex_matches(_digest(key, raw, algorithm), presented):
        raise SignatureInvalidError(f"{header} does not match")

    return VerifiedPayload(
        body=raw,
        provider="generic",
        signature_method="hmac-" + algorithm,
        provider_event_id=body_hash_id(raw),
        body_signed=True,
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyConfig:
    """How one channel authenticates, resolved and validated.

    The secret is a constructor argument, never a key in the mapping
    :meth:`from_config` reads. That is the structural form of §7's "secrets from
    the OS keychain or a ``0o600`` file, never from project settings": there is
    no code path where a settings blob's contents become the key, so no future
    caller can accidentally open one.
    """

    method: str
    secret: bytes = field(repr=False)
    header: str = ""
    prefix: str = ""
    algorithm: str = "sha256"
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    freshness_seconds: float = SLACK_FRESHNESS_SECONDS

    @classmethod
    def from_config(
        cls,
        data: Mapping | None = None,
        *,
        secret: bytes,
        tier: str = "user",
    ) -> VerifyConfig:
        """Build from a channel's ``verify`` block.

        Every failure here is a refusal to start, never a downgrade:

        * a tier a repository can ship → :class:`ChannelConfigError`;
        * no block, no ``method``, or ``method: "none"`` →
          :class:`ChannelNoVerificationError` (§7: "a channel with no
          verification method configured refuses to start");
        * an inline secret-shaped key → :class:`ChannelConfigError`;
        * an unknown method or a weak algorithm → :class:`ChannelConfigError`.
        """
        if tier not in VERIFY_TIERS:
            raise ChannelConfigError(
                f"channel verification may only be configured at the "
                f"{'/'.join(sorted(VERIFY_TIERS))} tier, not {tier!r}"
            )
        if not isinstance(data, Mapping) or not data:
            raise ChannelNoVerificationError(
                "channel has no verify block; there is no unauthenticated mode"
            )
        for key in data:
            if str(key).strip().lower().replace("-", "_") in _INLINE_SECRET_KEYS:
                raise ChannelConfigError(
                    f"verify block may not carry a secret inline ({key!r}); "
                    f"use secretRef to name a keychain entry or a 0o600 file"
                )

        method = str(data.get("method") or "").strip().lower()
        if not method or method == "none":
            raise ChannelNoVerificationError(
                "channel declares no verification method; refusing to start"
            )
        if method not in METHODS:
            raise ChannelConfigError(
                f"unknown verification method {method!r}; known: {', '.join(METHODS)}"
            )

        algorithm = str(data.get("algorithm") or "sha256").strip().lower()
        if algorithm not in ALLOWED_ALGORITHMS:
            raise ChannelConfigError(
                f"algorithm {algorithm!r} is not permitted; "
                f"allowed: {', '.join(sorted(ALLOWED_ALGORITHMS))}"
            )
        if method == "hmac" and not str(data.get("header") or "").strip():
            raise ChannelConfigError(
                "generic hmac verification requires a 'header' naming the "
                "signature header"
            )

        return cls(
            method=method,
            secret=_check_secret(secret),
            header=str(data.get("header") or "").strip(),
            prefix=str(data.get("prefix") or ""),
            algorithm=algorithm,
            max_body_bytes=_positive_int(
                data.get("maxBodyBytes", data.get("max_body_bytes")),
                DEFAULT_MAX_BODY_BYTES,
            ),
            freshness_seconds=_positive_float(
                data.get("freshnessSeconds", data.get("freshness_seconds")),
                SLACK_FRESHNESS_SECONDS,
            ),
        )

    @classmethod
    def for_provider(cls, provider: str, *, secret: bytes, **extra) -> VerifyConfig:
        """The conventional config for a named provider.

        ``VerifyConfig.for_provider("github", secret=…)`` is what a channel
        definition that names only ``"provider": "github"`` resolves to.
        """
        key = str(provider or "").strip().lower()
        method = PROVIDER_METHODS.get(key)
        if method is None:
            raise ChannelConfigError(
                f"no verification method known for provider {provider!r}; "
                f"known: {', '.join(sorted(PROVIDER_METHODS))}"
            )
        data = dict(extra)
        data["method"] = method
        return cls.from_config(data, secret=secret)


def _positive_int(value: Any, fallback: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    return out if out > 0 else fallback


def _positive_float(value: Any, fallback: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return fallback
    return out if out > 0 else fallback


def verify(
    config: VerifyConfig,
    body: bytes,
    headers: HeaderSource | None = None,
    *,
    now: float | None = None,
) -> VerifiedPayload:
    """Dispatch to the method ``config`` names.

    A config object is the only way to reach this function, and a config object
    cannot exist without a method — so there is no "verification disabled"
    branch to fall through.
    """
    if not isinstance(config, VerifyConfig):
        raise ChannelConfigError("verify() requires a VerifyConfig")
    if config.method == "hmac-sha256":
        return verify_github(
            body, headers,
            secret=config.secret,
            header=config.header or GITHUB_SIGNATURE_HEADER,
            max_body_bytes=config.max_body_bytes,
        )
    if config.method == "gitlab-token":
        return verify_gitlab(
            body, headers,
            secret=config.secret,
            header=config.header or GITLAB_TOKEN_HEADER,
            max_body_bytes=config.max_body_bytes,
        )
    if config.method == "slack-v0":
        return verify_slack(
            body, headers,
            secret=config.secret,
            now=now,
            freshness_seconds=config.freshness_seconds,
            max_body_bytes=config.max_body_bytes,
        )
    if config.method == "hmac":
        return verify_generic(
            body, headers,
            secret=config.secret,
            header=config.header,
            prefix=config.prefix,
            algorithm=config.algorithm,
            max_body_bytes=config.max_body_bytes,
        )
    raise ChannelConfigError(f"unknown verification method {config.method!r}")


# ---------------------------------------------------------------------------
# Replay defense
# ---------------------------------------------------------------------------


class ReplayGuard:
    """Refuses a ``(channel, provider_event_id)`` that has already been accepted.

    Bounded on both axes — ``max_entries`` and ``ttl_seconds`` — because an
    unauthenticated caller chooses how many distinct ids it sends, and an
    unbounded set of them is a memory-exhaustion primitive. Eviction is
    oldest-first, which means the guard's honest guarantee is *"no replay within
    the last ``max_entries`` events or ``ttl_seconds``, whichever binds first"*.
    That is deliberately weaker than the durable unique index the queue will
    carry; this one exists to refuse a replay **before a parse happens**, which
    a database round-trip is too late to do.

    :meth:`check` is check-and-record under one lock. It has to be: a provider
    retrying in parallel, or an attacker firing the same captured request from
    eight sockets, would otherwise slip several past a test-then-set.
    """

    __slots__ = ("_seen", "_lock", "max_entries", "ttl_seconds")

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_REPLAY_ENTRIES,
        ttl_seconds: float = DEFAULT_REPLAY_TTL_SECONDS,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._seen: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def check(self, channel: str, provider_event_id: str, *, now: float | None = None) -> None:
        """Record this delivery, or raise :class:`ReplayDetectedError`.

        An empty ``provider_event_id`` is refused rather than admitted: an event
        with no id cannot be guarded, and silently letting it through would make
        "send no delivery header" the bypass.
        """
        if not provider_event_id:
            raise ReplayDetectedError(
                "event has no provider_event_id and cannot be replay-checked"
            )
        moment = time.time() if now is None else float(now)
        key = (str(channel), str(provider_event_id))
        with self._lock:
            self._prune(moment)
            if key in self._seen:
                raise ReplayDetectedError(
                    f"event {provider_event_id!r} was already accepted on "
                    f"channel {channel!r}"
                )
            self._seen[key] = moment
            while len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)

    def seen(self, channel: str, provider_event_id: str, *, now: float | None = None) -> bool:
        """Has this delivery been recorded? Read-only — records nothing.

        For a ``channel test`` dry run, which must not consume the id it is
        rehearsing.
        """
        moment = time.time() if now is None else float(now)
        with self._lock:
            self._prune(moment)
            return (str(channel), str(provider_event_id)) in self._seen

    def _prune(self, now: float) -> None:
        """Drop expired entries. Insertion-ordered, so the walk stops at the
        first live one — except when a caller passes a ``now`` that moves
        backwards, which only costs a full scan, never correctness."""
        if not self.ttl_seconds:
            return
        cutoff = now - self.ttl_seconds
        for key in list(self._seen):
            if self._seen[key] > cutoff:
                break
            del self._seen[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


# ---------------------------------------------------------------------------
# The ordering guarantee
# ---------------------------------------------------------------------------


def verify_then_parse(
    config: VerifyConfig,
    body: bytes,
    headers: HeaderSource | None = None,
    *,
    now: float | None = None,
    parse: Callable = json.loads,
    guard: ReplayGuard | None = None,
    channel: str = "",
) -> tuple:
    """Size → signature → replay → **then** parse. Returns ``(payload, parsed)``.

    This is the only function that should ever hand a channel payload to a
    parser, and the sequence above is its entire reason to exist. Three things
    are load-bearing:

    * ``parse`` is called with ``payload.body`` — the *same object* the HMAC ran
      over. Not a copy, not a re-encode, not a decoded ``str``. A test asserts
      the identity, because an accidental round-trip would void the signature's
      meaning while still passing every "does it verify?" test.
    * ``parse`` is injectable so tests can instrument it and prove it was never
      reached on a bad signature (§16: "asserted by instrumenting the parser").
      In production it is :func:`json.loads`, which accepts ``bytes`` directly.
    * Replay is checked *before* the parse, not after. The point of the guard is
      to avoid doing work on a re-sent request, and parsing is the work.

    A parse failure past the gate is :class:`PayloadMalformedError`: the bytes
    are authentic, so this is a real integration problem worth reporting
    precisely — unlike a verification failure, which the listener must collapse
    into an opaque ``401``.
    """
    payload = verify(config, body, headers, now=now)
    if guard is not None:
        guard.check(channel, payload.provider_event_id, now=now)
    try:
        parsed = parse(payload.body)
    except ChannelError:
        raise
    except Exception as exc:
        raise PayloadMalformedError(
            f"authenticated payload did not parse: {type(exc).__name__}"
        ) from exc
    return payload, parsed
