"""A finding's identity — the thing that decides "have we already said this?"

Why this is the load-bearing file
---------------------------------
A review bot runs again on every push. If run #2 cannot recognize run #1's
findings it posts them all a second time, and the fastest way to get a review
bot switched off permanently is to make it spam the same comment five times on
one pull request. So identity has to be *content* identity, and it has to be
computed from the parts of a finding that survive the ordinary churn of a
branch.

    fingerprint = H(file, normalized_code_context, category, normalized_title)

Note what is missing: **the line number**. Line numbers are the most volatile
thing about a finding and the least meaningful — an import added at the top of
the file shifts every one of them without changing a single thing about the
code being reported. A fingerprint keyed on the line would re-key the entire
review on a one-line rebase, which is precisely the failure this module exists
to prevent. The surrounding code goes in instead: it moves *with* the finding.

The four inputs, and what each one buys
--------------------------------------
* ``file`` — normalized to a POSIX-ish relative path so ``./a/b.py``,
  ``a//b.py`` and a Windows-spelled ``a\\b.py`` are one file, not three.
* ``code_context`` — a short window of the reported code, whitespace-folded
  and blank-stripped, capped at :data:`CONTEXT_LINES` lines. The cap is doing
  real work: it bounds how far away an edit has to be before it stops mattering.
  Without it, a rebase that touches the bottom of the hunk re-keys a finding at
  the top of it.
* ``category`` — the same sentence about the same line is a different finding
  when one reviewer calls it a correctness bug and another calls it a security
  bug. Both should be reportable; neither should silence the other.
* ``title`` — casefolded and punctuation-trimmed, because a model rewording
  its own summary between runs ("…has no timeout" / "…has no timeout.") is not
  a new problem, but reporting a *different* problem about the same code is.

What is deliberately NOT normalized away
----------------------------------------
Comments and identifier case are left in the context. Stripping comments needs
a per-language lexer we do not have here, and folding case would merge a
finding about ``userId`` with one about ``userid``. When in doubt this module
prefers a *changed* fingerprint (an extra comment, once) over a *stuck* one (a
stale comment that never updates when the code underneath it moves on).

Stability contract
------------------
The digest must be identical across machines, processes and releases, so there
is no salt and no ``hash()``. :data:`FINGERPRINT_VERSION` is mixed into the
digest and stamped into the published marker, so changing the algorithm later
produces a disjoint id space that publication can recognize and migrate rather
than silently double-posting.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, Optional, Sequence, Union

__all__ = [
    "CONTEXT_LINES",
    "FINGERPRINT_ID_CHARS",
    "FINGERPRINT_VERSION",
    "MARKER_RE",
    "extract_marker",
    "finding_fingerprint",
    "marker",
    "normalize_context",
    "normalize_path",
    "normalize_title",
]

#: Bump when any normalizer or the framing below changes. Old comments keep
#: their old marker, so publication can tell "unknown finding" apart from
#: "finding from a previous algorithm" and migrate instead of duplicating.
FINGERPRINT_VERSION = 1

#: Lines of surrounding code that participate in identity. Five is enough to
#: distinguish two similar call sites in one file and short enough that an
#: edit a few lines away does not re-key the finding.
CONTEXT_LINES = 5

# Hard bound on how much text is examined to find those lines. Generous for
# real source, small enough that a generated 5 MB file costs nothing.
_MAX_CONTEXT_CHARS = 65536

#: Hex characters kept from the digest. 16 hex chars = 64 bits: collision odds
#: stay negligible at pull-request scale (thousands of findings, not billions)
#: while the marker stays short enough to read in a raw comment body.
FINGERPRINT_ID_CHARS = 16

# Domain separation. A hash of "some fields" is only meaningful relative to a
# scheme; naming the scheme in the preimage means a digest from this module can
# never be confused with one from another part of the codebase that happened to
# hash the same strings.
_DOMAIN = b"mantis.ci.finding"

# --- character scrubbing ---------------------------------------------------
# Same shapes as ``child_report.py`` (which is the canonical de-fanger for
# untrusted text). They are repeated rather than imported because the job here
# is different: that module *escapes evidence for a reader*, this one *erases
# noise before hashing*. A fingerprint has no reader, so deleting is right.

_SURROGATE_RE = re.compile("[\ud800-\udfff]")

# Anything that renders as a line break without being \n. Folded, not dropped:
# dropping would join two lines and change the context for free.
_LINE_SEP_RE = re.compile("[\u2028\u2029\x0b\x0c\u0085]")

_ANSI_RE = re.compile(
    "\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)"      # OSC … BEL | ST
    "|\x9d[^\x07\x9c]*(?:\x07|\x9c)"            # 8-bit OSC
    "|\x1b\\[[0-?]*[ -/]*[@-~]"                 # CSI
    "|\x9b[0-?]*[ -/]*[@-~]"                    # 8-bit CSI
    "|\x1b[@-Z\\\\-_]"                          # two-char escapes
    "|\x1b"                                     # stray ESC
)

_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Bidi overrides/isolates and zero-width characters. A fingerprint that changes
# because someone slipped a U+200B into a line would be trivially defeatable —
# an attacker could force a duplicate comment on every push.
_INVISIBLE_RE = re.compile(
    "[\u202a-\u202e\u2066-\u2069\u200b-\u200d\ufeff\u00ad]"
)

# Every run of blank-ish characters collapses to one space, so reindentation,
# tab/space conversion and trailing whitespace are all invisible to identity.
_BLANK_RUN_RE = re.compile(
    "[ \t\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]+"
)

# Trailing punctuation on a title: models add and remove a period between runs.
_TITLE_EDGE_RE = re.compile(r"^[\s\W_]+|[\s\W_]+$")


def _scrub(text: str) -> str:
    """NFC + strip escapes, controls and invisibles, folding odd line breaks.

    Order matters and mirrors ``child_report``: normalize first (patterns must
    see one canonical form), fold line separators before the control sweep
    would eat them, then remove escapes, controls and invisibles.
    """

    out = unicodedata.normalize("NFC", _SURROGATE_RE.sub("\ufffd", str(text)))
    out = _LINE_SEP_RE.sub("\n", out)
    out = _ANSI_RE.sub("", out)
    out = _CONTROL_RE.sub("", out)
    return _INVISIBLE_RE.sub("", out)


def normalize_path(file: str) -> str:
    """Canonical repository-relative spelling of ``file``.

    Backslashes become slashes, duplicate and leading separators go, and a
    leading ``./`` is dropped. Case is preserved: on the filesystems this runs
    against, ``README`` and ``readme`` really are two files.
    """

    raw = _scrub(file).strip().replace("\\", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    return "/".join(parts)


def normalize_context(
    code: Union[str, Sequence[str], Iterable[str]],
    *,
    max_lines: int = CONTEXT_LINES,
) -> str:
    """The identity-bearing shape of a code snippet.

    Accepts a block of text or a sequence of lines. Blank lines are dropped,
    every internal run of whitespace collapses to a single space, and at most
    ``max_lines`` lines survive — so reindentation, tabs-to-spaces, added blank
    lines and edits below the window all leave the result untouched.
    """

    if isinstance(code, str):
        raw = code
    else:
        raw = "\n".join(str(line) for line in code)

    # Only the first few non-blank lines can possibly matter, so refuse to
    # scrub a megabyte of minified vendor code to find them. Slicing characters
    # rather than lines is deliberate: an exotic line separator means "lines"
    # is not known until after ``_scrub`` runs, and this bound must hold first.
    raw = raw[:_MAX_CONTEXT_CHARS]

    lines = []
    for line in _scrub(raw).split("\n"):
        folded = _BLANK_RUN_RE.sub(" ", line).strip()
        if folded:
            lines.append(folded)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


def normalize_title(title: str) -> str:
    """Casefolded, whitespace-collapsed, edge-punctuation-trimmed title.

    Cosmetic rewording between runs must not re-key a finding; a genuinely
    different sentence must. Only the cosmetics are folded — the words are the
    signal and they stay.
    """

    folded = _BLANK_RUN_RE.sub(" ", _scrub(title).replace("\n", " "))
    return _TITLE_EDGE_RE.sub("", folded).casefold()


def _framed(*parts: str) -> bytes:
    """Length-prefix every field before hashing.

    Plain concatenation would make ``file="ab", title="c"`` and ``file="a",
    title="bc"`` the same preimage — a collision an attacker can aim by
    choosing a file name. Four-byte big-endian lengths make the framing
    unambiguous. Field values are capped at 4 GiB by construction; a longer one
    would be a bug elsewhere, so the encoder simply refuses to lie about it.
    """

    chunks = [_DOMAIN, FINGERPRINT_VERSION.to_bytes(4, "big")]
    for part in parts:
        blob = part.encode("utf-8", "replace")
        chunks.append(len(blob).to_bytes(4, "big"))
        chunks.append(blob)
    return b"".join(chunks)


def finding_fingerprint(
    *,
    file: str,
    code_context: Union[str, Sequence[str], Iterable[str]],
    category: str,
    title: str,
) -> str:
    """The stable id of a finding: ``H(file, context, category, title)``.

    Keyword-only on purpose. These four arguments are all strings and three of
    them are prose; a positional call site that swapped two of them would still
    run, still produce a plausible hex id, and quietly duplicate every comment.
    """

    payload = _framed(
        normalize_path(file),
        normalize_context(code_context),
        _scrub(category).strip().casefold(),
        normalize_title(title),
    )
    # blake2b with an explicit digest size: fast, stdlib, and unlike a
    # truncated SHA-256 the short output is the algorithm's own supported mode.
    digest = hashlib.blake2b(payload, digest_size=FINGERPRINT_ID_CHARS // 2)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# The published marker
# ---------------------------------------------------------------------------

# An HTML comment is the only place a fingerprint can live in a forge comment:
# invisible to a human reader, preserved verbatim by both GitHub and GitLab,
# and readable back out of the comment listing on the next run. This is the
# one intentional piece of HTML in the whole feature — note that it is emitted
# by us from a hex digest, never from a finding's text, so no attacker-supplied
# byte can reach it.
_MARKER_TEMPLATE = "<!-- mantis-finding:v{version}:{fingerprint} -->"

MARKER_RE = re.compile(
    r"<!--\s*mantis-finding:v(?P<version>\d+):(?P<fingerprint>[0-9a-f]{8,64})\s*-->"
)


def marker(fingerprint: str) -> str:
    """The hidden marker publication embeds so a re-run can match this finding."""

    return _MARKER_TEMPLATE.format(
        version=FINGERPRINT_VERSION, fingerprint=fingerprint
    )


def extract_marker(body: str) -> Optional[str]:
    """The fingerprint carried by a comment body, or ``None``.

    Returns the *last* marker in the body: a comment quoting an older comment
    (a user replying with the original text inline) still identifies itself by
    its own trailing marker.
    """

    found = MARKER_RE.findall(str(body or ""))
    return found[-1][1] if found else None
