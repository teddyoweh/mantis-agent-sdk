"""Browser control — the policy core.

This package is the home of the Playwright-backed browser subsystem (§7 of the
browser plan). What exists so far is the part the plan's implementation order
puts first and everything else depends on: **URL and domain policy**, in
:mod:`.policy` and :mod:`.domains`.

Importing this package is free. There is no Playwright import here, no browser
launch, no socket and no DNS lookup — the whole layer is pure functions over
strings, which is what makes the security tests exhaustive rather than
illustrative. The transports, manager, session, refs and snapshot modules land
alongside it; when they do, they call into this policy rather than reimplement
any part of it.

    from mantis_agent.browser import BrowserPolicy

    policy = BrowserPolicy.from_mapping(settings.get("browser"))
    decision = policy.check_transition(target, from_url=page.url, kind="redirect")

:mod:`.domains` is deliberately independent of anything browser-shaped: the
host normalizer and label-boundary matcher there are the same ones sandbox
egress rules and channel destination allowlists need, and one correct matcher
shared three ways beats three that are each nearly right.
"""

from __future__ import annotations

from .domains import (
    MATCH_ALL,
    DomainPattern,
    DomainPatternError,
    host_in_patterns,
    host_matches,
    is_ip_literal_host,
    normalize_host,
    parse_domain_pattern,
    parse_domain_patterns,
    parse_ip_literal,
)
from .policy import (
    ADDRESS_FILE,
    ADDRESS_LINK_LOCAL,
    ADDRESS_LOOPBACK,
    ADDRESS_METADATA,
    ADDRESS_MULTICAST,
    ADDRESS_NAME,
    ADDRESS_PRIVATE,
    ADDRESS_PUBLIC,
    ADDRESS_RESERVED,
    ADDRESS_UNSPECIFIED,
    DECISION_ALLOW,
    DECISION_ASK,
    DECISION_BLOCK,
    METADATA_ADDRESSES,
    METADATA_HOSTNAMES,
    NAVIGABLE_SCHEMES,
    REASON_ALLOWLISTED,
    REASON_BLOCKED_SCHEME,
    REASON_CREDENTIALS,
    REASON_DOMAIN_BLOCKED,
    REASON_DOMAIN_NOT_ALLOWED,
    REASON_INITIAL,
    REASON_INVALID_HOST,
    REASON_INVALID_PORT,
    REASON_LINK_LOCAL,
    REASON_LOOPBACK,
    REASON_MALFORMED,
    REASON_METADATA,
    REASON_MULTICAST,
    REASON_NEW_ORIGIN,
    REASON_NO_ADDRESSES,
    REASON_NO_HOST,
    REASON_PRIVATE,
    REASON_RESERVED,
    REASON_SAME_ORIGIN,
    REASON_SESSION_APPROVED,
    REASON_UNSPECIFIED,
    AddressVerdict,
    BrowserPolicy,
    TransitionDecision,
    UrlVerdict,
    classify_address,
    origin_of,
    redact_url,
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
    "MATCH_ALL",
    "METADATA_ADDRESSES",
    "METADATA_HOSTNAMES",
    "NAVIGABLE_SCHEMES",
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
    "DomainPattern",
    "DomainPatternError",
    "TransitionDecision",
    "UrlVerdict",
    "classify_address",
    "host_in_patterns",
    "host_matches",
    "is_ip_literal_host",
    "normalize_host",
    "origin_of",
    "parse_domain_pattern",
    "parse_domain_patterns",
    "parse_ip_literal",
    "redact_url",
]
