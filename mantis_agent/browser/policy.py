"""`BrowserPolicy` and the URL gate every navigation passes through — §9.

The browser subsystem's threat model is not "a user typed a bad URL". It is a
model reading an attacker-controlled page and following what it says, in a
process that sits inside the user's network. That makes this module the
security boundary: by the time Playwright is holding the URL, the decision has
already been made.

Four rules shape the code:

**Validate what the browser will dial, not what the string looks like.**
Hosts go through :mod:`.domains`, which IDNA-encodes unicode, folds the
alternate label separators, and decodes the ``inet_aton`` spellings of an
address. ``http://2852039166/latest/meta-data/`` is a request to the AWS
metadata service and a naive check reads it as an odd hostname.

**Every hop is its own decision.** :meth:`BrowserPolicy.check_transition` is
called for the initial navigation *and* for each redirect, popup and
script-driven origin change. :meth:`BrowserPolicy.check_chain` exists so the
"approval for one redirect authorized an unrelated final target" bug is
structurally impossible: approving hop 2 has no effect on hop 3.

**Metadata is unconditional.** ``allowPrivateNetwork`` defaults to ``True``
because §9 wants localhost development to work out of the box. The cost of
that default is that 169.254.0.0/16 lives inside "private", so the metadata
services are denied by their own rule, ahead of every setting — as is
link-local generally, which no local development target ever needs and which
is where IMDS, and the 6to4/IPv4-mapped spellings of it, live.

**Failure is closed and named.** Every refusal carries a stable ``reason``
slug (``REASON_*``) plus a sentence for a human, so the TUI can explain the
block and the model can tell "policy said no" apart from "the page 404'd".

The javascript scheme is blocked *always*. ``allowJavascript`` in §9 governs
the ``browser_evaluate`` tool, which is a different thing wearing a similar
name; conflating them would turn a debugging affordance into a navigation
primitive.

This module is pure: no Playwright, no sockets, no DNS. The one place that
needs a resolver is :meth:`BrowserPolicy.check_resolved_host`, which takes the
addresses the *caller* resolved — keeping the policy testable and letting the
transport pin the address it validated (the DNS-rebinding seam).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..redaction import is_secret_name
from .domains import (
    DomainPattern,
    host_in_patterns,
    normalize_host,
    parse_domain_patterns,
    parse_ip_literal,
)

__all__ = [
    "ADDRESS_FILE",
    "ADDRESS_LINK_LOCAL",
    "ADDRESS_LOOPBACK",
    "ADDRESS_METADATA",
    "ADDRESS_MULTICAST",
    "ADDRESS_NAME",
    "ADDRESS_PRIVATE",
    "ADDRESS_PUBLIC",
    "ADDRESS_RESERVED",
    "ADDRESS_UNSPECIFIED",
    "DECISION_ALLOW",
    "DECISION_ASK",
    "DECISION_BLOCK",
    "METADATA_ADDRESSES",
    "METADATA_HOSTNAMES",
    "REASON_ALLOWLISTED",
    "REASON_BLOCKED_SCHEME",
    "REASON_CREDENTIALS",
    "REASON_DOMAIN_BLOCKED",
    "REASON_DOMAIN_NOT_ALLOWED",
    "REASON_INITIAL",
    "REASON_INVALID_HOST",
    "REASON_INVALID_PORT",
    "REASON_LINK_LOCAL",
    "REASON_LOOPBACK",
    "REASON_MALFORMED",
    "REASON_METADATA",
    "REASON_MULTICAST",
    "REASON_NEW_ORIGIN",
    "REASON_NO_ADDRESSES",
    "REASON_NO_HOST",
    "REASON_PRIVATE",
    "REASON_RESERVED",
    "REASON_SAME_ORIGIN",
    "REASON_SESSION_APPROVED",
    "REASON_UNSPECIFIED",
    "AddressVerdict",
    "BrowserPolicy",
    "TransitionDecision",
    "UrlVerdict",
    "classify_address",
    "origin_of",
    "redact_url",
]

# --- address classes -------------------------------------------------------
ADDRESS_PUBLIC = "public"
ADDRESS_LOOPBACK = "loopback"
ADDRESS_PRIVATE = "private"
ADDRESS_LINK_LOCAL = "link-local"
ADDRESS_MULTICAST = "multicast"
ADDRESS_RESERVED = "reserved"
ADDRESS_UNSPECIFIED = "unspecified"
ADDRESS_METADATA = "metadata"
#: A name we have not resolved. Its real class is only known after DNS, which
#: is why :meth:`BrowserPolicy.check_resolved_host` exists.
ADDRESS_NAME = "name"
#: A ``file:`` URL, which has no address at all.
ADDRESS_FILE = "file"

# --- refusal / approval reasons -------------------------------------------
REASON_MALFORMED = "malformed-url"
REASON_BLOCKED_SCHEME = "blocked-scheme"
REASON_NO_HOST = "no-host"
REASON_INVALID_HOST = "invalid-host"
REASON_INVALID_PORT = "invalid-port"
REASON_CREDENTIALS = "embedded-credentials"
REASON_METADATA = "metadata-endpoint"
REASON_LOOPBACK = "loopback-blocked"
REASON_PRIVATE = "private-blocked"
REASON_LINK_LOCAL = "link-local-blocked"
REASON_MULTICAST = "multicast-blocked"
REASON_RESERVED = "reserved-blocked"
REASON_UNSPECIFIED = "unspecified-blocked"
REASON_DOMAIN_BLOCKED = "domain-blocked"
REASON_DOMAIN_NOT_ALLOWED = "domain-not-allowed"
REASON_NO_ADDRESSES = "unresolvable-host"
REASON_SAME_ORIGIN = "same-origin"
REASON_INITIAL = "initial-navigation"
REASON_ALLOWLISTED = "allowlisted"
REASON_SESSION_APPROVED = "session-approved"
REASON_NEW_ORIGIN = "new-origin"

# --- transition decisions --------------------------------------------------
DECISION_ALLOW = "allow"
DECISION_ASK = "ask"
DECISION_BLOCK = "block"

#: Schemes a navigation may use. Everything else — ``file``, ``data``,
#: ``javascript``, ``chrome``, ``chrome-extension``, ``blob``, ``about``,
#: ``view-source``, ``ws`` — is refused. An allowlist, because the interesting
#: schemes are the ones nobody thought to deny.
NAVIGABLE_SCHEMES = frozenset({"http", "https"})

_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Cloud instance-metadata endpoints, blocked regardless of every other
#: setting. The link-local pair is the well-known one (AWS/GCP/Azure/
#: OpenStack/DigitalOcean all answer on 169.254.169.254); the rest are the
#: vendors that chose their own address, plus AWS's ECS task endpoint and IMDS
#: over IPv6.
METADATA_ADDRESSES = frozenset({
    "169.254.169.254",   # AWS / GCP / Azure / OpenStack / DO / Hetzner
    "169.254.169.253",   # AWS VPC DNS
    "169.254.169.123",   # AWS VPC NTP
    "169.254.170.2",     # AWS ECS task metadata / credentials
    "100.100.100.200",   # Alibaba Cloud
    "192.0.0.192",       # Oracle Cloud
    "fd00:ec2::254",     # AWS IMDS over IPv6
})

#: Metadata endpoints that answer to a *name*. Blocked before resolution so a
#: DNS answer never gets the chance to look public.
METADATA_HOSTNAMES = (
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
    "instance-data",
    "instance-data.ec2.internal",
    "metadata.azure.com",
)

# Names that mean "this machine" without being an address (RFC 6761 reserves
# ``localhost`` and everything under it for exactly this).
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain")

_MASK = "•••"

# C0 controls + space: what a browser trims from both ends of a URL before it
# parses anything.
_URL_TRIM = "".join(chr(c) for c in range(0x21))


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------


def _unwrap(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> Any:
    """The IPv4 address embedded in an IPv6 one, if there is one.

    ``::ffff:169.254.169.254`` (IPv4-mapped), ``2002:a9fe:a9fe::`` (6to4) and
    Teredo all carry an IPv4 destination inside an address that looks, to
    ``is_private`` and friends, entirely unremarkable. Each is a working way to
    spell the metadata service.
    """

    for attr in ("ipv4_mapped", "sixtofour", "teredo"):
        value = getattr(ip, attr, None)
        if value is None:
            continue
        if attr == "teredo":  # (server, client) — the client is the target
            value = value[1]
        return value
    return None


def classify_address(value: object) -> str:
    """The class of an address or host name: one of the ``ADDRESS_*`` slugs.

    Accepts an :mod:`ipaddress` object, an IP literal in any spelling
    :func:`~mantis_agent.browser.domains.parse_ip_literal` understands, or a
    host name. Names resolve to :data:`ADDRESS_NAME` unless they are
    self-evidently local (``localhost`` and anything under it) or a known
    metadata endpoint.

    Order matters: :data:`ADDRESS_METADATA` is checked first because
    169.254.169.254 is *also* link-local, and reporting it as link-local would
    hide the one fact worth telling the user.
    """

    ip: Any = None
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        ip = value
    else:
        host = normalize_host(value)
        if host is None:
            return ADDRESS_NAME
        ip = parse_ip_literal(host)
        if ip is None:
            if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
                return ADDRESS_LOOPBACK
            if host_in_patterns(host, METADATA_HOSTNAMES) is not None:
                return ADDRESS_METADATA
            return ADDRESS_NAME

    embedded = _unwrap(ip)
    if embedded is not None:
        inner = classify_address(embedded)
        # A wrapper never launders its payload: if the embedded address is
        # blocked, so is the wrapper.
        if inner != ADDRESS_PUBLIC:
            return inner

    if str(ip) in METADATA_ADDRESSES:
        return ADDRESS_METADATA
    if ip.is_unspecified:
        return ADDRESS_UNSPECIFIED
    if ip.is_loopback:
        return ADDRESS_LOOPBACK
    if ip.is_link_local:
        return ADDRESS_LINK_LOCAL
    if ip.is_multicast:
        return ADDRESS_MULTICAST
    # ``is_reserved`` is checked *before* ``is_private`` because
    # :mod:`ipaddress` reports 240.0.0.0/4 as both, and "reserved" is the
    # accurate half — it also keeps 240/4 out of the range that
    # ``allowPrivateNetwork`` can switch back on. 0.0.0.0/8 ("this network")
    # gets the same treatment for the same reason.
    if ip.is_reserved or (ip.version == 4 and int(ip) < (1 << 24)):
        return ADDRESS_RESERVED
    if ip.is_private:
        return ADDRESS_PRIVATE
    return ADDRESS_PUBLIC


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _clean(url: object) -> str:
    """A URL string with the characters a browser drops before parsing removed.

    Chromium and Firefox strip TAB/CR/LF from a URL *anywhere* in it and trim
    leading/trailing C0 controls and spaces. So ``java\\nscript:alert(1)`` is a
    javascript URL to the browser and a harmless-looking unknown scheme to a
    validator that skips this step — validate the string the browser will
    actually use.
    """

    text = str(url or "")
    text = text.replace("\t", "").replace("\r", "").replace("\n", "")
    return text.strip(_URL_TRIM)


def _netloc(host: str, port: Optional[int], scheme: str) -> str:
    """``host[:port]``, with the scheme's default port elided and IPv6 bracketed."""

    shown = "[" + host + "]" if ":" in host else host
    if port is None or _DEFAULT_PORTS.get(scheme) == port:
        return shown
    return "{}:{}".format(shown, port)


def origin_of(url: object) -> str:
    """``scheme://host[:port]`` for ``url``, or ``""`` when it has no origin.

    The unit an allowlist entry and a "allow for this session" approval are
    scoped to.
    """

    try:
        parts = urlsplit(_clean(url))
        host = normalize_host(parts.hostname)
        port = parts.port
    except ValueError:
        return ""
    if not parts.scheme or not host:
        return ""
    return "{}://{}".format(parts.scheme.lower(), _netloc(host, port, parts.scheme.lower()))


def redact_url(url: object) -> str:
    """``url`` rendered safe to log, trace, show in a hook, or print.

    Two leaks live in a URL: userinfo (``https://alice:hunter2@…``) and query
    parameters whose *name* reads like a credential. The name test is
    :func:`mantis_agent.redaction.is_secret_name`, deliberately — one answer to
    "is this a secret?" for the whole codebase, so a new word gets added in one
    place.
    """

    text = _clean(url)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return _MASK
    if not parts.netloc:
        return text  # a relative reference or plain text: nothing to strip

    scheme = parts.scheme.lower()
    netloc = _netloc(host or "", port, scheme)
    if parts.username or parts.password:
        netloc = "{}@{}".format(_MASK, netloc)

    query = parts.query
    if query:
        out: List[str] = []
        for pair in query.split("&"):
            name, sep, value = pair.partition("=")
            out.append(name + sep + (_MASK if sep and is_secret_name(name) else value))
        query = "&".join(out)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AddressVerdict:
    """The answer for one address (or one resolved host)."""

    allowed: bool
    address_class: str
    address: str = ""
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class UrlVerdict:
    """The answer for one URL.

    ``url`` is the **navigable** form: normalized host, credentials stripped,
    default port elided. Hand this to the transport rather than the caller's
    string, so what was validated is what gets fetched.

    ``display_url`` is the **loggable** form, additionally masking secret-ish
    query parameters. Never put ``url`` in a transcript and never navigate to
    ``display_url``.
    """

    allowed: bool
    url: str
    display_url: str
    scheme: str = ""
    host: str = ""
    port: Optional[int] = None
    address_class: str = ADDRESS_NAME
    is_ip_literal: bool = False
    credentials_redacted: bool = False
    reason: str = ""
    detail: str = ""
    matched_pattern: Optional[DomainPattern] = None

    @property
    def origin(self) -> str:
        if not self.scheme or not self.host:
            return ""
        return "{}://{}".format(self.scheme, _netloc(self.host, self.port, self.scheme))


@dataclass(frozen=True)
class TransitionDecision:
    """The answer for one navigation *hop*.

    ``decision`` is :data:`DECISION_ALLOW`, :data:`DECISION_ASK` (a new origin
    with no allowlist configured — the human prompt path) or
    :data:`DECISION_BLOCK` (policy says no and no prompt can change that).
    """

    decision: str
    verdict: UrlVerdict
    kind: str = "navigate"
    from_origin: str = ""
    to_origin: str = ""
    same_origin: bool = False
    reason: str = ""
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == DECISION_ALLOW


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _compiled(patterns: Tuple[str, ...]) -> Tuple[Tuple[DomainPattern, ...], Tuple[str, ...]]:
    """Parsed patterns + parse errors, memoized on the raw tuple.

    :class:`BrowserPolicy` is frozen and hashable precisely so this cache can
    exist: a policy is consulted on every redirect of every navigation.
    """

    return parse_domain_patterns(patterns)


def _as_tuple(value: object) -> Tuple[str, ...]:
    """A settings value that should be a list of strings, as a tuple.

    A bare string is one entry, not a sequence of characters — ``"corp.com"``
    in a config file means one domain, and splitting it into ``('c','o',…)``
    would produce an allowlist that matches nothing with no error at all.
    """

    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_int(value: object, default: int, *, low: int, high: int) -> int:
    """An integer setting, clamped. Junk falls back to the default rather than
    raising: a typo in ``settings.json`` must not take the browser down."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        number = int(str(value).strip(), 10)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_choice(value: object, default: str, choices: Sequence[str]) -> str:
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    return default


@dataclass(frozen=True)
class BrowserPolicy:
    """The §9 settings block, as an immutable value.

    Defaults are the plan's: disabled, chromium, headless, ephemeral profile,
    no domain lists, private network reachable (so ``http://localhost:3000``
    works on day one), and file URLs / JavaScript evaluation / uploads all off.

    Frozen and hashable so it can be a cache key and so a hook cannot mutate a
    policy out from under a check that already ran — §12 requires a *recheck*
    after hook mutation, which means constructing a new policy, not editing
    this one. Use :func:`dataclasses.replace`.
    """

    enabled: bool = False
    engine: str = "chromium"
    headless: bool = True
    mode: str = "ephemeral"
    allowed_domains: Tuple[str, ...] = ()
    blocked_domains: Tuple[str, ...] = ()
    allow_private_network: bool = True
    allow_file_urls: bool = False
    allow_javascript: bool = False
    allow_uploads: bool = False
    upload_roots: Tuple[str, ...] = ()
    download_root: str = ".mantis/artifacts/browser-downloads"
    persistent_profile: Optional[str] = None
    record_trace: str = "on-failure"
    max_pages: int = 8
    navigation_timeout_ms: int = 30000
    action_timeout_ms: int = 10000
    #: Beyond §9's list, which says "reject **or** redact". Rejecting is the
    #: default because a URL carrying credentials is nearly always an attempt to
    #: smuggle a host past a reader; setting this False keeps the navigation and
    #: strips the userinfo instead.
    reject_embedded_credentials: bool = True

    def __post_init__(self) -> None:
        # Callers construct these with lists. Coerce to tuples so the instance
        # stays hashable (and so nobody mutates a policy through its list).
        for name in ("allowed_domains", "blocked_domains", "upload_roots"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, _as_tuple(value))

    # -- construction -------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "BrowserPolicy":
        """Build from the §9 JSON block (camelCase, snake_case also accepted).

        Every field is junk-tolerant and clamped. Unknown keys are ignored:
        settings files outlive the code that reads them.
        """

        d: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
        # Some callers pass the whole settings document.
        if "browser" in d and isinstance(d.get("browser"), Mapping):
            d = d["browser"]
        base = cls()

        def get(*names: str) -> Any:
            for name in names:
                if name in d:
                    return d[name]
            return None

        return cls(
            enabled=_as_bool(get("enabled"), base.enabled),
            engine=_as_choice(get("engine"), base.engine, ("chromium", "firefox", "webkit")),
            headless=_as_bool(get("headless"), base.headless),
            mode=_as_choice(
                get("mode"), base.mode, ("ephemeral", "persistent", "attached", "remote")
            ),
            allowed_domains=_as_tuple(get("allowedDomains", "allowed_domains")),
            blocked_domains=_as_tuple(get("blockedDomains", "blocked_domains")),
            allow_private_network=_as_bool(
                get("allowPrivateNetwork", "allow_private_network"), base.allow_private_network
            ),
            allow_file_urls=_as_bool(get("allowFileUrls", "allow_file_urls"), base.allow_file_urls),
            allow_javascript=_as_bool(
                get("allowJavascript", "allow_javascript"), base.allow_javascript
            ),
            allow_uploads=_as_bool(get("allowUploads", "allow_uploads"), base.allow_uploads),
            upload_roots=_as_tuple(get("uploadRoots", "upload_roots")),
            download_root=str(get("downloadRoot", "download_root") or base.download_root),
            persistent_profile=(
                str(get("persistentProfile", "persistent_profile"))
                if get("persistentProfile", "persistent_profile")
                else None
            ),
            record_trace=_as_choice(
                get("recordTrace", "record_trace"),
                base.record_trace,
                ("on", "off", "on-failure", "retain-on-failure"),
            ),
            max_pages=_as_int(get("maxPages", "max_pages"), base.max_pages, low=1, high=64),
            navigation_timeout_ms=_as_int(
                get("navigationTimeoutMs", "navigation_timeout_ms"),
                base.navigation_timeout_ms,
                low=1000,
                high=600000,
            ),
            action_timeout_ms=_as_int(
                get("actionTimeoutMs", "action_timeout_ms"),
                base.action_timeout_ms,
                low=500,
                high=600000,
            ),
            reject_embedded_credentials=_as_bool(
                get("rejectEmbeddedCredentials", "reject_embedded_credentials"),
                base.reject_embedded_credentials,
            ),
        )

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        base: Optional["BrowserPolicy"] = None,
    ) -> "BrowserPolicy":
        """``base`` with the §13 ``MANTIS_BROWSER*`` overrides applied.

        Environment sits at the top of the precedence chain (defaults <
        settings < env), so this takes an already-built policy and returns a
        new one. Unset variables change nothing.
        """

        src = os.environ if env is None else env
        policy = base or cls()
        changes: dict = {}
        if src.get("MANTIS_BROWSER") is not None:
            changes["enabled"] = _as_bool(src.get("MANTIS_BROWSER"), policy.enabled)
        if src.get("MANTIS_BROWSER_HEADLESS") is not None:
            changes["headless"] = _as_bool(src.get("MANTIS_BROWSER_HEADLESS"), policy.headless)
        if src.get("MANTIS_BROWSER_ENGINE"):
            changes["engine"] = _as_choice(
                src.get("MANTIS_BROWSER_ENGINE"), policy.engine, ("chromium", "firefox", "webkit")
            )
        for var, attr in (
            ("MANTIS_BROWSER_ALLOWED_DOMAINS", "allowed_domains"),
            ("MANTIS_BROWSER_BLOCKED_DOMAINS", "blocked_domains"),
        ):
            value = src.get(var)
            if value is not None:
                changes[attr] = tuple(part.strip() for part in value.split(",") if part.strip())
        return replace(policy, **changes) if changes else policy

    # -- introspection ------------------------------------------------------

    def domain_problems(self) -> List[str]:
        """Unusable entries in either domain list, as sentences for a UI.

        A bad entry is dropped, not approximated — so a broken allowlist gets
        *narrower*, never wider — but it has to be reportable or the user will
        never understand why their domain is blocked.
        """

        problems = list(_compiled(self.allowed_domains)[1])
        problems.extend(_compiled(self.blocked_domains)[1])
        return problems

    # -- the gate -----------------------------------------------------------

    def check_address(self, value: object) -> AddressVerdict:
        """Whether one address (or classified host) may be reached."""

        cls_ = classify_address(value)
        text = str(value)
        if cls_ == ADDRESS_METADATA:
            return AddressVerdict(
                False, cls_, text, REASON_METADATA,
                "cloud instance-metadata endpoints are never reachable from the browser",
            )
        if cls_ == ADDRESS_LINK_LOCAL:
            return AddressVerdict(
                False, cls_, text, REASON_LINK_LOCAL,
                "link-local addresses are never reachable (this is where metadata services live)",
            )
        if cls_ == ADDRESS_MULTICAST:
            return AddressVerdict(False, cls_, text, REASON_MULTICAST, "multicast address")
        if cls_ == ADDRESS_RESERVED:
            return AddressVerdict(False, cls_, text, REASON_RESERVED, "reserved address")
        if cls_ == ADDRESS_UNSPECIFIED:
            return AddressVerdict(
                False, cls_, text, REASON_UNSPECIFIED,
                "the unspecified address routes to this host",
            )
        if cls_ in (ADDRESS_LOOPBACK, ADDRESS_PRIVATE) and not self.allow_private_network:
            reason = REASON_LOOPBACK if cls_ == ADDRESS_LOOPBACK else REASON_PRIVATE
            return AddressVerdict(
                False, cls_, text, reason,
                "browser.allowPrivateNetwork is off, so {} targets are blocked".format(cls_),
            )
        return AddressVerdict(True, cls_, text)

    def check_resolved_host(self, host: object, addresses: Iterable[object]) -> AddressVerdict:
        """Whether a host may be reached, given the addresses it resolved to.

        **All** answers must pass. A DNS-rebinding attacker returns a public
        address and a loopback address in the same reply and lets the browser
        pick, so "one of them was fine" is not a property worth having. An
        empty answer set is a refusal, not a pass.

        The transport should then pin the address it validated instead of
        letting the browser re-resolve — that is the other half of the defence,
        and it lives there because this module does no I/O.
        """

        listed = [a for a in (addresses or ())]
        if not listed:
            return AddressVerdict(
                False, ADDRESS_NAME, str(host), REASON_NO_ADDRESSES,
                "host {!r} resolved to no usable address".format(str(host)),
            )
        first: Optional[AddressVerdict] = None
        for address in listed:
            verdict = self.check_address(address)
            if not verdict.allowed:
                return verdict
            first = first or verdict
        return first  # type: ignore[return-value]

    def check_url(self, url: object, *, base_url: object = None) -> UrlVerdict:
        """Validate one URL. Run before the initial navigation **and** on every
        redirect and origin transition.

        ``base_url`` resolves relative references against the current page, so
        a client-side ``location = "/admin"`` is checked as the absolute URL it
        becomes.
        """

        text = _clean(url)
        if base_url:
            base = _clean(base_url)
            if base:
                try:
                    text = urljoin(base, text)
                except ValueError:
                    return _refuse(text, REASON_MALFORMED, "URL could not be resolved")
        if not text:
            return _refuse(text, REASON_MALFORMED, "empty URL")

        try:
            parts = urlsplit(text)
        except ValueError:
            return _refuse(text, REASON_MALFORMED, "URL could not be parsed")

        scheme = (parts.scheme or "").lower()
        if not scheme:
            return _refuse(text, REASON_MALFORMED, "URL has no scheme")
        if scheme not in NAVIGABLE_SCHEMES:
            if scheme == "file" and self.allow_file_urls:
                # A ``file:`` URL with a host is not a local file: on Windows
                # ``file://server/share`` is an SMB fetch, which would turn the
                # narrowest escape hatch in the policy into an outbound network
                # request that skipped every address check below.
                if parts.netloc and normalize_host(parts.hostname) not in (None, "localhost"):
                    return _refuse(
                        text, REASON_NO_HOST,
                        "file: URLs may only address the local machine",
                        scheme=scheme,
                    )
                return UrlVerdict(
                    True, text, redact_url(text), scheme=scheme, address_class=ADDRESS_FILE
                )
            return _refuse(
                text, REASON_BLOCKED_SCHEME,
                "the {!r} scheme is not navigable (http/https only)".format(scheme),
                scheme=scheme,
            )

        try:
            port = parts.port
        except ValueError:
            return _refuse(text, REASON_INVALID_PORT, "URL has an invalid port", scheme=scheme)

        raw_host = parts.hostname
        if not raw_host:
            return _refuse(text, REASON_NO_HOST, "URL has no host", scheme=scheme)
        host = normalize_host(raw_host)
        if host is None:
            return _refuse(
                text, REASON_INVALID_HOST,
                "{!r} is not a usable host name".format(raw_host),
                scheme=scheme,
            )

        # From here on we describe the URL by its *normalized* form, with any
        # userinfo already gone — nothing downstream should ever see it, not
        # even in a refusal message.
        clean_url = urlunsplit(
            (scheme, _netloc(host, port, scheme), parts.path, parts.query, parts.fragment)
        )
        had_credentials = bool(parts.username or parts.password)
        address_class = classify_address(host)
        common = dict(
            url=clean_url,
            display_url=redact_url(clean_url),
            scheme=scheme,
            host=host,
            port=port,
            address_class=address_class,
            is_ip_literal=parse_ip_literal(host) is not None,
            credentials_redacted=had_credentials,
        )

        if had_credentials and self.reject_embedded_credentials:
            return UrlVerdict(
                allowed=False,
                reason=REASON_CREDENTIALS,
                detail=(
                    "the URL embeds credentials; everything before '@' is userinfo, so the real "
                    "host is {}".format(host)
                ),
                **common,
            )

        address = self.check_address(host)
        if not address.allowed:
            return UrlVerdict(
                allowed=False, reason=address.reason, detail=address.detail, **common
            )

        blocked = host_in_patterns(host, _compiled(self.blocked_domains)[0])
        if blocked is not None:
            return UrlVerdict(
                allowed=False,
                reason=REASON_DOMAIN_BLOCKED,
                detail="{} is blocked by browser.blockedDomains entry {!r}".format(
                    host, blocked.raw
                ),
                matched_pattern=blocked,
                **common,
            )

        if self.allowed_domains:
            allowed = host_in_patterns(host, _compiled(self.allowed_domains)[0])
            if allowed is None:
                return UrlVerdict(
                    allowed=False,
                    reason=REASON_DOMAIN_NOT_ALLOWED,
                    detail="{} is not in browser.allowedDomains".format(host),
                    **common,
                )
            return UrlVerdict(allowed=True, matched_pattern=allowed, **common)

        return UrlVerdict(allowed=True, **common)

    def check_transition(
        self,
        to_url: object,
        *,
        from_url: object = None,
        kind: str = "navigate",
        session_allowed: Sequence[Union[str, DomainPattern]] = (),
    ) -> TransitionDecision:
        """Decide one navigation hop: allow, ask a human, or block.

        ``kind`` is how the hop happened (``initial``, ``redirect``, ``click``,
        ``form``, ``popup``, ``script``) and is carried into the decision so a
        prompt can say *what* is navigating. ``session_allowed`` holds the
        "allow this domain for the session" approvals already granted.

        The rules, in order:

        1. A URL the address/scheme/domain gate refuses is **blocked** — no
           prompt, because no answer to a prompt makes 169.254.169.254 safe.
        2. The initial navigation is allowed (the tool's own permission layer
           is what gates *starting* to browse).
        3. Same host, and not an https→http downgrade: **allowed**. This is
           §9's "same-origin actions may inherit authorization", widened to the
           host because the allowlists are host-scoped; a downgrade is treated
           as a new origin because it is a change in who can read the traffic.
        4. On the allowlist, or already approved for this session: **allowed**.
        5. Otherwise a new origin with no allowlist configured: **ask**.

        Note what cannot happen: approving hop *n* grants nothing to hop *n+1*,
        because every hop re-enters here with its own ``from``/``to`` pair.
        """

        verdict = self.check_url(to_url, base_url=from_url)
        from_origin = origin_of(from_url) if from_url else ""
        to_origin = verdict.origin
        same_origin = bool(from_origin) and from_origin == to_origin

        def decide(decision: str, reason: str, detail: str) -> TransitionDecision:
            return TransitionDecision(
                decision=decision,
                verdict=verdict,
                kind=kind,
                from_origin=from_origin,
                to_origin=to_origin,
                same_origin=same_origin,
                reason=reason,
                detail=detail,
            )

        if not verdict.allowed:
            return decide(DECISION_BLOCK, verdict.reason, verdict.detail)
        if not from_url or kind == "initial":
            return decide(DECISION_ALLOW, REASON_INITIAL, "initial navigation")

        from_parts = urlsplit(_clean(from_url))
        from_host = normalize_host(from_parts.hostname)
        from_scheme = (from_parts.scheme or "").lower()
        if from_host and from_host == verdict.host:
            downgrade = from_scheme == "https" and verdict.scheme == "http"
            if not downgrade:
                return decide(
                    DECISION_ALLOW,
                    REASON_SAME_ORIGIN,
                    "same host as the current page",
                )

        allowed = host_in_patterns(verdict.host, _compiled(self.allowed_domains)[0])
        if allowed is not None:
            return decide(
                DECISION_ALLOW,
                REASON_ALLOWLISTED,
                "{} matches allowedDomains entry {!r}".format(verdict.host, allowed.raw),
            )
        approved = host_in_patterns(verdict.host, list(session_allowed or ()))
        if approved is not None:
            return decide(
                DECISION_ALLOW,
                REASON_SESSION_APPROVED,
                "{} was approved for this session".format(verdict.host),
            )
        return decide(
            DECISION_ASK,
            REASON_NEW_ORIGIN,
            "{} → {} ({})".format(from_origin or "(none)", to_origin, kind),
        )

    def check_chain(
        self,
        urls: Sequence[object],
        *,
        session_allowed: Sequence[Union[str, DomainPattern]] = (),
        kind: str = "redirect",
    ) -> List[TransitionDecision]:
        """Validate a redirect chain hop by hop, stopping at the first refusal.

        The returned list is as long as the number of hops actually decided —
        so a chain that ends in a block never reports on the URLs the browser
        would not have reached. This is the shape §9's "never let approval for
        one redirect authorize an unrelated final target" takes in code.
        """

        decisions: List[TransitionDecision] = []
        previous: object = None
        for index, url in enumerate(urls or ()):
            decision = self.check_transition(
                url,
                from_url=previous,
                kind="initial" if index == 0 else kind,
                session_allowed=session_allowed,
            )
            decisions.append(decision)
            if decision.decision != DECISION_ALLOW:
                break
            previous = decision.verdict.url or url
        return decisions


def _refuse(text: str, reason: str, detail: str, *, scheme: str = "") -> UrlVerdict:
    """A refusal for a URL we could not even fully parse.

    ``url``/``display_url`` are redacted rather than echoed: a malformed URL is
    exactly the kind that carries a credential in a place the parser lost track
    of, and a refusal message is still a message that gets logged.
    """

    safe = redact_url(text)
    return UrlVerdict(
        allowed=False, url="", display_url=safe, scheme=scheme, reason=reason, detail=detail
    )
