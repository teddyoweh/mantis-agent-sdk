"""One host normalizer and one domain matcher, for everything that has a
domain allowlist.

Three features in this codebase want to answer "is this host on the list?":
browser navigation policy (:mod:`mantis_agent.browser.policy`), sandbox network
egress, and channel/webhook destinations. Written three times it will be wrong
in at least one of them, and the wrong answer is always the same wrong answer —
a substring or suffix test that lets ``corp.com.evil.net`` pass for
``corp.com``. So the matcher lives here, once, and takes the two things that
make it correct seriously:

**Normalization is not lowercasing.** A host is what a *browser* would dial,
which means IDNA-encoding unicode labels (so the Cyrillic-а homograph in
``аpple.com`` becomes ``xn--pple-43d.com`` and stops being Apple), treating
U+3002/U+FF0E/U+FF61 as the label separators IDNA says they are, dropping the
root dot in ``github.com.``, and canonicalizing the inet_aton spellings of an
IPv4 address — ``http://2852039166/`` is 169.254.169.254 and a validator that
only understands dotted quads hands out cloud credentials.

**Matching is on DNS label boundaries.** ``endswith("." + apex)`` with an
explicit length check, never ``endswith(apex)`` and never ``in``. IP literals
match by equality only: ``"1.2.3.4".endswith(".2.3.4")`` is true and means
nothing.

Pattern syntax, deliberately tiny:

======================  ====================================================
``example.com``         that host, exactly
``*.example.com``       that host **and** anything under it
``.example.com``        cookie-style synonym for ``*.example.com``
``*``                   everything
======================  ====================================================

``*.x`` covering ``x`` itself is the one judgement call. The alternative makes
``blockedDomains: ["*.evil.net"]`` silently miss ``evil.net``, and a hole in a
blocklist is worse than a mild widening of an allowlist. Any other use of ``*``
(``*evil.com``, ``a.*.com``) raises :class:`DomainPatternError` rather than
guessing — a pattern nobody can parse must not become a pattern that matches
everything.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "MATCH_ALL",
    "DomainPattern",
    "DomainPatternError",
    "host_in_patterns",
    "host_matches",
    "is_ip_literal_host",
    "normalize_host",
    "parse_domain_pattern",
    "parse_domain_patterns",
    "parse_ip_literal",
]

#: The pattern that matches every host.
MATCH_ALL = "*"

# IDNA's label separators. ASCII "." is the one everybody knows; the other
# three are why ``host.split(".")`` is not a label splitter — a browser reads
# ``github。com`` as two labels, so anything comparing it as one is comparing
# the wrong string.
_DOT_RE = re.compile("[.。．｡]")

# Characters an ASCII label may contain. Underscore is in (``_dmarc.…`` is a
# real name); everything else — ``*``, ``%``, ``/``, whitespace — means the
# caller handed us something that is not a host.
_ASCII_LABEL_RE = re.compile(r"^[a-z0-9_-]+$")

# Rejected outright anywhere in a host string: these are the delimiters that
# separate a host from its neighbours in a URL, so their presence means the
# caller sliced the URL wrong and we are about to validate the wrong thing.
_HOST_DELIMS = set(" \t\r\n\v\f/\\?#@")

_MAX_NAME_LEN = 253
_MAX_LABEL_LEN = 63


class DomainPatternError(ValueError):
    """A domain pattern that cannot be parsed.

    Raised rather than returning a permissive default: ``allowedDomains:
    ["*.corp .com"]`` should be a loud configuration error, never an accidental
    ``match everything``.
    """


# ---------------------------------------------------------------------------
# IP literals
# ---------------------------------------------------------------------------


def parse_ip_literal(host: object) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """The address ``host`` denotes, or ``None`` if it isn't one.

    Strict dotted-quad/IPv6 first, then the ``inet_aton`` spellings every
    browser and every ``connect()`` still accept and :mod:`ipaddress` rejects:
    a bare integer (``2852039166``), octal (``0251.0376.0251.0376``), hex
    (``0xA9FEA9FE``) and the short forms (``127.1``). Those four spellings are
    the standard way an SSRF payload writes ``169.254.169.254``, so a validator
    that doesn't decode them is not a validator.

    IPv6 may arrive bracketed (``[::1]``) and with a zone id (``fe80::1%en0``);
    both are accepted and stripped.
    """

    s = str(host or "").strip().lower()
    if not s:
        return None
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if "%" in s:  # scope/zone id — irrelevant to classification
        s = s.split("%", 1)[0]
    try:
        return ipaddress.ip_address(s)
    except ValueError:
        pass
    if ":" in s:  # a failed IPv6 stays failed; never fall through to IPv4 parsing
        return None

    parts = s.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values: List[int] = []
    for part in parts:
        try:
            values.append(_parse_ipv4_part(part))
        except ValueError:
            return None
    # inet_aton: the last part absorbs whatever octets the earlier ones didn't.
    *leading, last = values
    if any(v > 0xFF for v in leading):
        return None
    span = 4 - len(leading)
    if last >= 1 << (8 * span):
        return None
    total = last
    for i, v in enumerate(leading):
        total += v << (8 * (3 - i))
    try:
        return ipaddress.IPv4Address(total)
    except ValueError:  # pragma: no cover — total is bounded above
        return None


def _parse_ipv4_part(part: str) -> int:
    """One inet_aton component: ``0x`` hex, leading-zero octal, else decimal."""

    if not part:
        raise ValueError("empty component")
    if part.startswith(("0x", "0X")):
        body = part[2:]
        if not body or not all(c in "0123456789abcdef" for c in body):
            raise ValueError("bad hex component")
        return int(body, 16)
    if part.startswith("0") and len(part) > 1:
        body = part[1:]
        if not all(c in "01234567" for c in body):
            raise ValueError("bad octal component")
        return int(body, 8)
    if not part.isdigit() or not part.isascii():
        raise ValueError("bad decimal component")
    return int(part, 10)


def is_ip_literal_host(host: object) -> bool:
    """Whether ``host`` is an address rather than a name."""

    return parse_ip_literal(host) is not None


# ---------------------------------------------------------------------------
# Host normalization
# ---------------------------------------------------------------------------


def normalize_host(host: object) -> Optional[str]:
    """``host`` as the canonical lowercase form to compare and store, or
    ``None`` when it is not a host at all.

    Fails closed on everything ambiguous — empty labels (``a..b``), a label
    over 63 octets, a name over 253, characters that only appear when a URL was
    sliced wrong (``/``, ``@``, whitespace, ``*``), and any unicode label IDNA
    refuses to encode. ``None`` means *do not navigate here*; it never means
    "use the input as-is".
    """

    raw = str(host or "").strip()
    if not raw:
        return None

    # Bracketed IPv6 is the one form that legitimately contains ":" and "[]".
    if raw.startswith("[") and raw.endswith("]"):
        ip = parse_ip_literal(raw)
        return str(ip) if ip is not None else None

    if any(c in _HOST_DELIMS for c in raw) or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        return None
    if ":" in raw:
        # Either an unbracketed IPv6 literal — which is what ``urlsplit`` hands
        # back for ``http://[::1]/`` — or a host that still has its port
        # attached, which means the caller sliced the URL by hand and is about
        # to validate a string DNS never sees. Only the first is a host.
        ip = parse_ip_literal(raw)
        return str(ip) if ip is not None else None

    labels = _DOT_RE.split(raw)
    if len(labels) > 1 and labels[-1] == "":
        labels = labels[:-1]  # the root dot in "github.com."
    if not labels or any(not label for label in labels):
        return None

    out: List[str] = []
    for label in labels:
        encoded = _encode_label(label)
        if encoded is None:
            return None
        out.append(encoded)

    name = ".".join(out)
    if len(name) > _MAX_NAME_LEN:
        return None

    # Only now can "0251.0376.0251.0376" be recognized for what it is.
    ip = parse_ip_literal(name)
    return str(ip) if ip is not None else name


def _encode_label(label: str) -> Optional[str]:
    """One DNS label, lowercased and IDNA-encoded, or ``None`` if invalid."""

    if label.isascii():
        low = label.lower()
        if len(low) > _MAX_LABEL_LEN or not _ASCII_LABEL_RE.match(low):
            return None
        return low
    try:
        # ``str.encode("idna")`` runs nameprep (which case-folds and normalizes)
        # and then ToASCII. It raises for anything IDNA won't represent, which
        # is exactly the set we want to refuse rather than pass through raw.
        encoded = label.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if not encoded or len(encoded) > _MAX_LABEL_LEN or not _ASCII_LABEL_RE.match(encoded):
        return None
    return encoded


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainPattern:
    """One parsed entry of an allow/block list.

    ``host`` is the normalized apex (empty only for :data:`MATCH_ALL`),
    ``include_subdomains`` is the ``*.`` opt-in, and ``raw`` is kept verbatim so
    a UI can report *which line the user wrote* rather than what we made of it.
    """

    host: str
    include_subdomains: bool = False
    match_all: bool = False
    raw: str = ""

    def matches(self, host: object) -> bool:
        """Whether ``host`` (raw or normalized) falls under this pattern."""

        if self.match_all:
            return normalize_host(host) is not None
        candidate = normalize_host(host)
        if candidate is None or not self.host:
            return False
        if candidate == self.host:
            return True
        if not self.include_subdomains:
            return False
        # An address has no subdomains. Without this, ".2.3.4" would match
        # "1.2.3.4" — a suffix test that reads as a label test and isn't.
        if is_ip_literal_host(candidate) or is_ip_literal_host(self.host):
            return False
        # The label boundary, stated once: strictly longer than ".apex", and
        # ending on it. "corp.com.evil.net" fails both halves.
        return candidate.endswith("." + self.host)


def parse_domain_pattern(value: object) -> DomainPattern:
    """Parse one allow/block entry, raising :class:`DomainPatternError`.

    Tolerant about the shapes people actually type — ``https://corp.com/path``,
    a trailing slash, a ``:port`` — because rejecting those produces a
    configuration error the user cannot act on. Intolerant about ``*``
    anywhere except a leading ``*.``, because that is the one mistake whose
    lenient reading is "match everything".
    """

    raw = str(value or "").strip()
    if not raw:
        raise DomainPatternError("empty domain pattern")
    text = raw

    if "://" in text:  # a whole URL was pasted in
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if text == MATCH_ALL:
        return DomainPattern(host="", include_subdomains=True, match_all=True, raw=raw)

    include_subdomains = False
    if text.startswith("*."):
        include_subdomains, text = True, text[2:]
    elif text.startswith("."):
        include_subdomains, text = True, text[1:]
    if "*" in text:
        raise DomainPatternError(
            f"invalid domain pattern {raw!r}: '*' is only allowed as a leading '*.'"
        )

    # Strip a port, but only when it cannot be part of an IPv6 literal.
    if not text.startswith("[") and text.count(":") == 1:
        text = text.split(":", 1)[0]
    if text.startswith("[") and "]" in text:
        text = text[: text.index("]") + 1]

    # Patterns use STRICT address parsing: "2.3.4" in a config file is far more
    # likely to be a (bad) name than someone's shorthand for 2.3.0.4.
    literal = _strict_ip(text)
    if literal is not None:
        if include_subdomains:
            raise DomainPatternError(
                f"invalid domain pattern {raw!r}: an IP address has no subdomains"
            )
        return DomainPattern(host=str(literal), include_subdomains=False, raw=raw)

    host = normalize_host(text)
    if host is None:
        raise DomainPatternError(f"invalid domain pattern {raw!r}: not a host")
    return DomainPattern(host=host, include_subdomains=include_subdomains, raw=raw)


def _strict_ip(text: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    s = text.strip().lower()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    try:
        return ipaddress.ip_address(s)
    except ValueError:
        return None


def parse_domain_patterns(
    values: Iterable[object],
) -> Tuple[Tuple[DomainPattern, ...], Tuple[str, ...]]:
    """Parse a whole list into ``(patterns, error_messages)``.

    Split rather than raising so one typo cannot take a session down — but the
    unusable entry is *dropped*, never approximated, and the message names it
    so a ``/browser status`` can say which line to fix.
    """

    patterns: List[DomainPattern] = []
    errors: List[str] = []
    for value in values or ():
        try:
            patterns.append(parse_domain_pattern(value))
        except DomainPatternError as exc:
            errors.append(str(exc))
    return tuple(patterns), tuple(errors)


def host_matches(host: object, pattern: Union[str, DomainPattern]) -> bool:
    """Whether ``host`` matches one pattern. An unparseable pattern matches
    nothing (the fail-closed direction for an allowlist)."""

    if isinstance(pattern, DomainPattern):
        return pattern.matches(host)
    try:
        return parse_domain_pattern(pattern).matches(host)
    except DomainPatternError:
        return False


def host_in_patterns(
    host: object, patterns: Sequence[Union[str, DomainPattern]]
) -> Optional[DomainPattern]:
    """The first pattern ``host`` matches, or ``None``.

    Returns the *rule* rather than a bool so callers can report "blocked by
    ``*.evil.net``" — an explanation the user can edit.
    """

    for pattern in patterns or ():
        if isinstance(pattern, DomainPattern):
            compiled: Optional[DomainPattern] = pattern
        else:
            try:
                compiled = parse_domain_pattern(pattern)
            except DomainPatternError:
                compiled = None
        if compiled is not None and compiled.matches(host):
            return compiled
    return None
