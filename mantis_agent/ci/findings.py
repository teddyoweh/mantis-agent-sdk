"""What a code-review finding *is*: schema, sanitization, ranking, capping.

This is the pure core of the CI review feature — no forge, no network, no
model. Everything downstream (the review workflow, publication, the JSON
output, autofix) speaks in :class:`Finding` objects produced here, which is
why three unrelated-looking concerns live in one module: they are all
invariants of the same object.

1. Structure over prose
-----------------------
A finding is a struct, not a paragraph, and ``failure_scenario`` is
**required**. That single required field is the biggest quality lever in the
plan: a reviewer forced to state concrete inputs producing a concrete wrong
output cannot dress a style preference up as a bug, because there are no
inputs that make two-space indentation return the wrong answer. Findings that
cannot fill it in do not get built.

2. Sanitization is an invariant, not a step
-------------------------------------------
Everything in a pull request is attacker-controlled — title, description,
commit messages, and the diff the reviewer is quoting back at you. A finding
is rendered into a comment posted under the repository's own name, so a body
containing ``<img src=https://evil/x.png>`` is an IP-logging beacon with the
repo's reputation attached, and ``[click here](…)`` is a phishing link your
users have a reason to trust.

So sanitization is not a function publication remembers to call: a
:class:`Finding` **refuses to exist** in an unsanitized state.
``__post_init__`` re-derives the sanitized form of every text field and
rejects the struct if it differs. That check runs on direct construction, on
``msgspec.structs.replace``, and on JSON decode — the three ways a finding can
enter the program — so there is no path to a live ``<img>`` in a comment even
if a future caller forgets this module exists.

The sanitizers are idempotent, which is what makes that invariant expressible:
the plan sanitizes on ingest *and* again at render time, and text that
degraded on every pass would be unusable by the third one.

3. Volume, capped honestly
--------------------------
Fifteen findings ranked by severity get read; ninety get the bot muted. But a
cap that silently truncates is worse than no cap, because the reviewer now
believes they have seen everything. :func:`cap_findings` therefore returns a
:class:`CapReport` — what is shown, what was dropped, and a one-line summary
("8 lower-severity findings not shown.") that publication renders verbatim.

Identity lives next door in :mod:`.fingerprint`; see that module for why a
finding's id deliberately excludes its line number.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple, Union

import msgspec

from .fingerprint import finding_fingerprint

__all__ = [
    "KNOWN_CATEGORIES",
    "MAX_BODY",
    "MAX_FAILURE_SCENARIO",
    "MAX_FINDINGS",
    "MAX_PER_FILE",
    "MAX_SUGGESTION",
    "MAX_TITLE",
    "SEVERITIES",
    "SEVERITY_RANK",
    "TRUNCATION_MARK",
    "CIError",
    "CapReport",
    "Finding",
    "FindingsSchemaError",
    "cap_findings",
    "dedupe_findings",
    "fence_for",
    "finding_from_dict",
    "make_finding",
    "rank_findings",
    "sanitize_code",
    "sanitize_text",
]


class CIError(Exception):
    """Base of the CI error family (plan §14).

    Defined here because this is the first CI module to need one; the rest of
    the tree (`ForgeAuthError`, `DiffTooLargeError`, …) should subclass it from
    a shared ``ci/errors.py`` when the adapters land.
    """


class FindingsSchemaError(CIError, ValueError):
    """A finding is missing substance, out of range, or not sanitized.

    Subclasses ``ValueError`` deliberately: msgspec wraps a ``ValueError`` from
    ``__post_init__`` into a ``ValidationError`` during decode, so a hostile
    JSON payload fails decoding rather than producing a struct nobody checked.
    """


#: Ordered worst-first. Order *is* the ranking, so this tuple is the single
#: place severity precedence is written down.
SEVERITIES: Tuple[str, ...] = ("critical", "high", "medium", "low", "nit")

SEVERITY_RANK: Dict[str, int] = {name: i for i, name in enumerate(SEVERITIES)}

#: The review dimensions from plan §8. Closed on purpose: ``category`` is one
#: of the four fingerprint inputs, so a model free-styling "correctness-ish"
#: one run and "correctness" the next would post the same finding twice.
KNOWN_CATEGORIES: Tuple[str, ...] = (
    "correctness", "security", "perf", "tests", "api", "docs",
)

# Field caps, from plan §8 (title/body) plus two the plan leaves open. They are
# chosen for a *comment*, not for a document: a finding that does not fit in a
# glance is not going to be acted on, and length is also the cheapest denial of
# service against a reviewer's attention.
MAX_TITLE = 80
MAX_BODY = 1000
MAX_FAILURE_SCENARIO = 500
MAX_SUGGESTION = 2000

#: Appended when a field is cut. Visible on purpose — a silently truncated
#: sentence reads as a complete one and can invert its own meaning.
TRUNCATION_MARK = " […]"

#: Volume defaults, from plan §13.
MAX_FINDINGS = 15
MAX_PER_FILE = 5


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------
#
# The character-level half mirrors ``child_report.py`` — the canonical
# de-fanger for untrusted text in this codebase — and the markdown half is
# specific to the surface findings are rendered on. Every rule below is
# idempotent, because sanitization runs on ingest and again at render time.

_SURROGATE_RE = re.compile("[\ud800-\udfff]")

# Line breaks that are not \n (U+2028/U+2029, VT, FF, NEL). Folded rather than
# deleted: deleting joins two lines, which is how a hidden second sentence gets
# smuggled onto the end of the first.
_LINE_SEP_RE = re.compile("[\u2028\u2029\x0b\x0c\u0085]")

_ANSI_RE = re.compile(
    "\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)"      # OSC … BEL | ST
    "|\x9d[^\x07\x9c]*(?:\x07|\x9c)"            # 8-bit OSC
    "|\x1b\\[[0-?]*[ -/]*[@-~]"                 # CSI
    "|\x9b[0-?]*[ -/]*[@-~]"                    # 8-bit CSI
    "|\x1b[@-Z\\\\-_]"                          # two-char escapes
    "|\x1b"                                     # stray ESC
)

# C0/C1 controls, keeping \t and \n. \r is not kept: in a terminal it
# overwrites the line just printed, which is a way to make a rendered finding
# say something other than what it contains.
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Bidi overrides/isolates and zero-width characters — "the text you read is not
# the text that is there". In a *finding* this matters twice over: it is both
# an attack on the reviewer reading the comment and, when it appears in the
# diff, something the review is supposed to report.
_INVISIBLE_RE = re.compile(
    "[\u202a-\u202e\u2066-\u2069\u200b-\u200d\ufeff\u00ad]"
)

# Exotic blanks folded to a plain space so no downstream pattern has to know
# about NBSP or IDEOGRAPHIC SPACE.
_UNICODE_SPACE_RE = re.compile(
    "[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]"
)

# Markdown images: the beacon. Replaced by an explicit, legible marker that
# keeps the URL as evidence — the reviewer should be able to see what the pull
# request tried to make them load. The URL is defanged a few lines later.
#
# The replacement uses ROUND brackets on purpose. A nested construct like
# ``[![pixel](u1)](u2)`` is a real pattern, and emitting square brackets here
# would leave a well-formed ``[…](u2)`` behind that the link rule can no longer
# match (its text class excludes ``]``). Round brackets let the outer link
# unfold on the next line.
_MD_IMAGE_RE = re.compile(r"!\[([^\]\n]{0,200})\]\(([^)\n]{0,500})\)")

# Whatever image syntax survives the inline rule — reference-style ``![x][ref]``
# above all — loses its ``!`` and degrades to a link. Cheap, and it means "no
# field can render an image" needs no case analysis to believe.
_RESIDUAL_IMAGE_RE = re.compile(r"!\[")

# Markdown inline links: unfolded into "text (url)". The link text is the
# phishing surface ("click here to fix"), so the destination is always shown.
_MD_LINK_RE = re.compile(r"\[([^\]\n]{0,200})\]\(([^)\n]{0,500})\)")

# URL defanging, the convention every security tool uses: ``https[://]evil`` is
# instantly readable, cannot be autolinked by GitHub or GitLab, and cannot be
# clicked by accident. Dropping the URL entirely would hide evidence; leaving
# it live would post a clickable attacker link under the repo's name.
_SCHEME_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]{1,20})://")

# Schemes that are dangerous without a "//" authority. Required to be followed
# by non-space so ordinary prose ("data: the results…") is left alone.
_BARE_SCHEME_RE = re.compile(r"\b(javascript|data|vbscript|mailto):(?=\S)", re.I)

# Both forges autolink bare ``www.`` hosts and e-mail addresses too.
_WWW_RE = re.compile(r"\bwww\.", re.I)
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9\-]+\.[A-Za-z0-9.\-]+)")

_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.M)
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_WS_RUN_RE = re.compile(r"\s+")
_BACKTICK_RUN_RE = re.compile(r"`+")


def _scrub(text: str) -> str:
    """Character-level neutralization shared by both sanitizers."""

    out = unicodedata.normalize("NFC", _SURROGATE_RE.sub("\ufffd", str(text)))
    out = _LINE_SEP_RE.sub("\n", out)
    out = _ANSI_RE.sub("", out)
    out = _CONTROL_RE.sub("", out)
    out = _INVISIBLE_RE.sub("", out)
    return _UNICODE_SPACE_RE.sub(" ", out)


def _defang(text: str) -> str:
    """Make every URL and address in ``text`` inert but still readable."""

    out = _SCHEME_RE.sub(r"\1[://]", text)
    out = _BARE_SCHEME_RE.sub(r"\1[:]", out)
    out = _WWW_RE.sub("www[.]", out)
    return _EMAIL_RE.sub(r"\1[@]\2", out)


def _truncate(text: str, max_chars: int) -> str:
    """Cut to ``max_chars`` *including* the mark, so a second pass is a no-op."""

    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATION_MARK):
        return text[:max_chars]
    return text[: max_chars - len(TRUNCATION_MARK)].rstrip() + TRUNCATION_MARK


def sanitize_text(
    text: str, *, max_chars: int, single_line: bool = False
) -> str:
    """A prose field, safe to render into a forge comment.

    In order: scrub control characters and invisibles; unfold markdown images
    and links; defang URLs and addresses; escape ``<`` and ``>``; tidy
    whitespace; cap the length.

    The angle-bracket escaping is unconditional, which is the one deliberately
    blunt rule here. A cleverer "only escape things that look like tags" rule
    would keep ``Vec<String>`` pretty and would eventually be wrong about
    something — and both forges render raw ``<img>``, ``<details>`` and
    ``<a href>`` from comment bodies. Code in a finding belongs in
    ``suggestion``, which goes through :func:`sanitize_code` and is rendered in
    a fence where none of this applies.
    """

    out = _scrub(text)
    if single_line:
        out = out.replace("\n", " ")
    out = _MD_IMAGE_RE.sub(r"(image removed: \2)", out)
    out = _RESIDUAL_IMAGE_RE.sub("[", out)
    out = _MD_LINK_RE.sub(r"\1 (\2)", out)
    out = _defang(out)
    out = out.replace("<", "&lt;").replace(">", "&gt;")
    if single_line:
        out = _WS_RUN_RE.sub(" ", out)
    else:
        out = _BLANK_LINES_RE.sub("\n\n", _TRAILING_WS_RE.sub("", out))
    return _truncate(out.strip(), max_chars)


def sanitize_code(text: str, *, max_chars: int) -> str:
    """A suggested replacement, safe to render *inside a fence*.

    Deliberately weaker than :func:`sanitize_text`, because a suggestion is
    applied to the repository as-is. Nothing here rewrites the code: markdown
    is inert inside a fence, and defanging a URL in a string literal would
    corrupt the patch. What is removed is what must never survive into a
    committed file — control characters, exotic blanks, and above all bidi
    overrides, which are the trojan-source attack: code that renders as one
    thing and compiles as another is exactly what a suggestion must not be
    able to smuggle past a reviewer who is trusting the rendering.

    Indentation is preserved exactly; a patch that lost its tabs would not
    apply. Fencing safety is the renderer's job via :func:`fence_for`.
    """

    out = _scrub(text)
    out = _TRAILING_WS_RE.sub("", out)
    return _truncate(out.strip("\n"), max_chars)


def fence_for(text: str) -> str:
    """The shortest code fence that ``text`` cannot break out of.

    CommonMark closes a fence only on a run of backticks at least as long as
    the opener, so a suggestion containing ``` is contained by a longer fence
    rather than by mangling the code.
    """

    longest = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(text)), default=0)
    return "`" * max(3, longest + 1)


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


class Finding(msgspec.Struct, frozen=True):
    """One reviewed defect, as published and as serialized.

    Field order differs from the sketch in plan §8 only because msgspec (like
    dataclasses) requires defaulted fields last; the set is identical.
    ``failure_scenario`` stays required — that is the whole point of it.

    Build these with :func:`make_finding`, which sanitizes and fingerprints.
    Constructing one directly is allowed but every invariant is still checked,
    so hand-built findings must already be clean.
    """

    id: str                       # stable fingerprint; see .fingerprint
    file: str
    line: int
    severity: str                 # one of SEVERITIES
    category: str                 # one of KNOWN_CATEGORIES
    title: str                    # <= MAX_TITLE, sanitized, single line
    body: str                     # <= MAX_BODY, sanitized, no HTML/images/links
    failure_scenario: str         # concrete inputs -> concrete wrong output
    end_line: int = 0             # 0 = single-line finding
    suggestion: str = ""          # exact replacement text, if any
    confidence: float = 0.0
    verified_by: int = 0          # skeptics that failed to refute it

    def __post_init__(self) -> None:
        """Reject anything that is not a publishable finding.

        Runs on construction, on ``msgspec.structs.replace`` and on decode, so
        this is the choke point every finding passes through exactly once.
        """

        for name in ("id", "file", "title", "body", "failure_scenario"):
            if not str(getattr(self, name)).strip():
                raise FindingsSchemaError(
                    f"finding.{name} is required and may not be blank"
                    + (
                        " — state concrete inputs and the concrete wrong output"
                        if name == "failure_scenario"
                        else ""
                    )
                )
        if self.severity not in SEVERITY_RANK:
            raise FindingsSchemaError(
                f"unknown severity {self.severity!r}; expected one of {SEVERITIES}"
            )
        if self.category not in KNOWN_CATEGORIES:
            raise FindingsSchemaError(
                f"unknown category {self.category!r}; expected one of "
                f"{KNOWN_CATEGORIES}"
            )
        if self.line < 0 or self.end_line < 0:
            raise FindingsSchemaError("line numbers must be non-negative")
        if self.end_line and self.end_line < self.line:
            raise FindingsSchemaError(
                f"end_line {self.end_line} precedes line {self.line}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise FindingsSchemaError(
                f"confidence {self.confidence!r} is outside [0.0, 1.0]"
            )
        if self.verified_by < 0:
            raise FindingsSchemaError("verified_by must be non-negative")

        # The sanitization invariant. Cheap (the sanitizers are pure and the
        # fields are capped) and it is what makes "no finding can render an
        # image" a property of the type rather than of a code path.
        for name, cap, single in (
            ("title", MAX_TITLE, True),
            ("body", MAX_BODY, False),
            ("failure_scenario", MAX_FAILURE_SCENARIO, False),
        ):
            value = getattr(self, name)
            if sanitize_text(value, max_chars=cap, single_line=single) != value:
                raise FindingsSchemaError(
                    f"finding.{name} is not sanitized; build findings with "
                    "make_finding() rather than by hand"
                )
        if sanitize_code(self.suggestion, max_chars=MAX_SUGGESTION) != self.suggestion:
            raise FindingsSchemaError("finding.suggestion is not sanitized")


def make_finding(
    *,
    file: str,
    line: int,
    severity: str,
    category: str,
    title: str,
    body: str,
    failure_scenario: str,
    code_context: Union[str, Sequence[str]],
    end_line: int = 0,
    suggestion: str = "",
    confidence: float = 0.0,
    verified_by: int = 0,
) -> Finding:
    """Sanitize, fingerprint and validate one finding. The only constructor.

    ``code_context`` — the lines the finding is about — is required and is not
    stored: it exists to key the fingerprint, so that the identity of a finding
    travels with the code rather than with the line it happened to sit on
    today. Callers pass the hunk they were reviewing.

    Keyword-only throughout. Five of these arguments are strings a positional
    call could silently transpose, and a transposed ``category``/``title`` pair
    would still produce a valid-looking finding with the wrong identity.
    """

    clean_title = sanitize_text(title, max_chars=MAX_TITLE, single_line=True)
    clean_category = str(category or "").strip().lower()
    clean_file = str(file or "").strip()
    return Finding(
        id=finding_fingerprint(
            file=clean_file,
            code_context=code_context,
            category=clean_category,
            title=clean_title,
        ),
        file=clean_file,
        line=int(line),
        severity=str(severity or "").strip().lower(),
        category=clean_category,
        title=clean_title,
        body=sanitize_text(body, max_chars=MAX_BODY),
        failure_scenario=sanitize_text(
            failure_scenario, max_chars=MAX_FAILURE_SCENARIO
        ),
        end_line=int(end_line),
        suggestion=sanitize_code(suggestion, max_chars=MAX_SUGGESTION),
        confidence=float(confidence),
        verified_by=int(verified_by),
    )


# Everything a model is allowed to say about a finding. ``id`` is absent on
# purpose: identity is derived from content by us, never asserted by the thing
# being reviewed, or a hostile diff could aim a fingerprint at an existing
# comment and rewrite it.
_RAW_REQUIRED = ("file", "line", "severity", "category", "title", "body",
                 "failure_scenario")
_RAW_OPTIONAL = ("end_line", "suggestion", "confidence", "verified_by")


def finding_from_dict(
    raw: Mapping[str, Any], *, code_context: Union[str, Sequence[str]]
) -> Finding:
    """Build a finding from decoded model output, strictly.

    Unknown keys are an error rather than being ignored. Silently dropping
    them would hide both a schema drift in our own prompt and a hostile diff
    persuading the reviewer to emit extra fields ("comment_body", "id") in the
    hope that something downstream reads them.
    """

    if not isinstance(raw, Mapping):
        raise FindingsSchemaError(f"finding must be an object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - set(_RAW_REQUIRED) - set(_RAW_OPTIONAL))
    if unknown:
        raise FindingsSchemaError(f"unknown finding field(s): {', '.join(unknown)}")
    missing = [key for key in _RAW_REQUIRED if key not in raw]
    if missing:
        raise FindingsSchemaError(f"missing finding field(s): {', '.join(missing)}")

    def _num(key: str, default: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FindingsSchemaError(f"finding.{key} must be a number")
        return float(value)

    return make_finding(
        file=str(raw["file"]),
        line=int(_num("line", 0)),
        severity=str(raw["severity"]),
        category=str(raw["category"]),
        title=str(raw["title"]),
        body=str(raw["body"]),
        failure_scenario=str(raw["failure_scenario"]),
        code_context=code_context,
        end_line=int(_num("end_line", 0)),
        suggestion=str(raw.get("suggestion", "")),
        confidence=_num("confidence", 0.0),
        verified_by=int(_num("verified_by", 0)),
    )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _rank_key(finding: Finding) -> Tuple[Any, ...]:
    """Severity, then confidence, then how much verification survived.

    The tail (file, line, id) is not cosmetic: without it two equally severe
    findings could swap places between runs, which reorders a comment for no
    reason and makes the diff of a summary comment unreadable.
    """

    return (
        SEVERITY_RANK.get(finding.severity, len(SEVERITIES)),
        -finding.confidence,
        -finding.verified_by,
        finding.file,
        finding.line,
        finding.id,
    )


def rank_findings(findings: Iterable[Finding]) -> List[Finding]:
    """Most-important-first, by a total order (so re-runs do not reshuffle)."""

    return sorted(findings, key=_rank_key)


def dedupe_findings(findings: Iterable[Finding]) -> List[Finding]:
    """One finding per fingerprint, keeping the best-ranked copy.

    Two review dimensions noticing the same defect is a *good* signal, not a
    reason to say it twice; the surviving copy is the one with the higher
    severity, confidence and verification.
    """

    seen: Dict[str, Finding] = {}
    for finding in rank_findings(findings):
        seen.setdefault(finding.id, finding)
    return rank_findings(seen.values())


# ---------------------------------------------------------------------------
# Volume control
# ---------------------------------------------------------------------------


class CapReport(msgspec.Struct, frozen=True):
    """The result of capping: what is shown, and an honest account of the rest.

    Serialized straight into the CI JSON output, which is why the settings are
    carried alongside the counts — a run's own record has to explain itself
    without the configuration file that produced it.
    """

    shown: Tuple[Finding, ...] = ()
    dropped: Tuple[Finding, ...] = ()
    duplicates: int = 0
    suppressed_nits: int = 0
    below_min_severity: int = 0
    over_per_file: Dict[str, int] = msgspec.field(default_factory=dict)
    over_max: int = 0
    max_findings: int = MAX_FINDINGS
    max_per_file: int = MAX_PER_FILE
    min_severity: str = "nit"
    suppress_nits: bool = True

    def summary(self) -> str:
        """One line for the published comment, or ``""`` if nothing was cut.

        Publication renders this verbatim under the findings. Saying nothing
        when nothing was dropped matters as much as speaking up when something
        was: a permanent "some findings hidden" footer teaches reviewers to
        assume the bot is always holding something back.
        """

        parts: List[str] = []
        if self.over_max:
            parts.append(
                f"{self.over_max} lower-severity findings not shown "
                f"(limit {self.max_findings} per review)."
            )
        if self.over_per_file:
            files = ", ".join(sorted(self.over_per_file))
            hidden = sum(self.over_per_file.values())
            parts.append(
                f"{hidden} further findings hidden in {files} "
                f"(limit {self.max_per_file} per file)."
            )
        if self.suppressed_nits:
            parts.append(f"{self.suppressed_nits} nit-level findings suppressed.")
        if self.below_min_severity:
            parts.append(
                f"{self.below_min_severity} findings below severity "
                f"'{self.min_severity}' not shown."
            )
        if self.duplicates:
            parts.append(f"{self.duplicates} duplicate findings merged.")
        return " ".join(parts)


def cap_findings(
    findings: Iterable[Finding],
    *,
    max_findings: int = MAX_FINDINGS,
    max_per_file: int = MAX_PER_FILE,
    min_severity: str = "nit",
    suppress_nits: bool = True,
) -> CapReport:
    """Rank, cap, and account for everything that did not make the cut.

    Order of operations, each step feeding the next: dedupe by fingerprint,
    drop nits, apply the severity floor, rank, cap per file, cap overall.
    Deduping first is what makes the counts truthful — three dimensions
    reporting one defect is one finding, not three suppressed ones.

    ``min_severity`` defaults to the bottom of the scale rather than plan
    §13's ``"low"`` because ``suppress_nits`` already *is* the nit floor;
    keeping the two knobs independent means turning nits back on actually
    shows them. A caller passing the configured ``minSeverity`` verbatim gets
    exactly the documented behaviour.
    """

    # An unknown severity name opens the floor rather than closing it: a typo
    # in configuration should show a reviewer too much, never hide a critical
    # finding behind a setting nobody can see took effect.
    floor = SEVERITY_RANK.get(str(min_severity).lower(), len(SEVERITIES) - 1)
    all_findings = list(findings)

    deduped = dedupe_findings(all_findings)
    duplicates = len(all_findings) - len(deduped)

    dropped: List[Finding] = []
    survivors: List[Finding] = []
    suppressed_nits = 0
    below_min = 0
    for finding in deduped:
        if suppress_nits and finding.severity == "nit":
            suppressed_nits += 1
            dropped.append(finding)
        elif SEVERITY_RANK.get(finding.severity, len(SEVERITIES)) > floor:
            below_min += 1
            dropped.append(finding)
        else:
            survivors.append(finding)

    per_file: Dict[str, int] = {}
    over_per_file: Dict[str, int] = {}
    kept: List[Finding] = []
    for finding in rank_findings(survivors):
        count = per_file.get(finding.file, 0)
        if count >= max_per_file:
            over_per_file[finding.file] = over_per_file.get(finding.file, 0) + 1
            dropped.append(finding)
            continue
        per_file[finding.file] = count + 1
        kept.append(finding)

    over_max = max(0, len(kept) - max_findings)
    if over_max:
        dropped.extend(kept[max_findings:])
        kept = kept[:max_findings]

    return CapReport(
        shown=tuple(kept),
        dropped=tuple(rank_findings(dropped)),
        duplicates=duplicates,
        suppressed_nits=suppressed_nits,
        below_min_severity=below_min,
        over_per_file=over_per_file,
        over_max=over_max,
        max_findings=max_findings,
        max_per_file=max_per_file,
        min_severity=str(min_severity).lower(),
        suppress_nits=suppress_nits,
    )
