"""Namespaced activity ids.

Mantis runs concurrent work through several engines and each numbers its own
work: ``Job.id`` is an ``itertools.count(1)`` int, a workflow run is a base36
string, ``subagent._RUN_COUNTER`` restarts at 1 per process. Those numbers are
user-visible (``/job 3``, ``run 4f2a``) so they must not be renumbered. Instead
every id entering the activity graph is *namespaced* with a three-letter kind
prefix::

    job:3                JobManager Job.id
    wfr:4f2a             WorkflowRun base36 id
    wfp:4f2a/Review      phase, scoped by run
    wfa:4f2a/a7          AgentRun id, scoped by run
    sub:12               subagent _RUN_COUNTER

The whole point of the prefix is that the rest of the codebase never has to
care: **ids are opaque strings outside this module**. Only :func:`make_id`,
:func:`parse_id` and :func:`resolve_ref` know the grammar.

The local part is normalized (see :data:`_UNSAFE_LOCAL`) because it frequently
comes from model-authored text — a phase is identified by its title. A phase
title is *arbitrary* text: any script, any punctuation, any length. So
normalization is built to three rules, in this order of importance:

1. **It never raises.** An id is a display/lookup handle; a title that cannot be
   spelled in the id alphabet must not throw an exception into the engine that
   is merely reporting progress (plan §10, "fail open for display"). The single
   exception is a caller passing *nothing at all* — ``""``, ``"   "``, ``"///"``
   — which is a programming error at the call site, not model text, and which
   :func:`make_id` still rejects. Any input holding one non-separator character
   produces an id.
2. **Distinct titles keep distinct ids** whenever the readable projection cannot
   carry the difference: a title with non-ASCII characters, an empty projection
   (``"***"``), or one longer than the cap gets a short content digest appended,
   so ``レビュー``, ``审查代码`` and ``Проверка`` are three nodes rather than one.
   ASCII titles keep their readable form — ``Review`` is ``Review``, not a hash.
   Titles differing only in ASCII punctuation (``Review!`` / ``Review?``) do
   still share an id; they are scoped by run and read as the same phase.
3. **No id can escape a directory.** Every ``/``-separated segment is drawn from
   ``[A-Za-z0-9._-]``, no segment is empty, and no segment is made only of dots,
   so ``..`` cannot survive. A local part carries at most one ``/`` — the scope
   separator between a run id and a title — so a title can never introduce a
   second path level.

An id is *not itself* a filename: it contains ``:`` and possibly ``/``. A
consumer that needs a filename (the journal) must call :func:`safe_path_segment`,
which maps any id to a single collision-free path component.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, Optional, Tuple

from ..errors import AgentError

__all__ = [
    "ActivityError",
    "InvalidIdError",
    "KINDS",
    "KIND_PREFIXES",
    "PREFIX_KINDS",
    "is_id",
    "kind_of",
    "local_of",
    "make_id",
    "parse_id",
    "resolve_ref",
    "resolve_refs",
    "safe_path_segment",
    "try_parse_id",
]


class ActivityError(AgentError):
    """Base for every error the activity subsystem raises.

    It lives in ``ids`` because ``ids`` is the bottom of this package's import
    order — every other activity module may import from here without creating a
    cycle, and the plan does not budget a separate ``errors`` module.
    """


class InvalidIdError(ActivityError, ValueError):
    """A malformed activity id, or an unknown kind/prefix.

    Also a ``ValueError`` so callers that already guard id parsing with
    ``except ValueError`` keep working.
    """


# ---------------------------------------------------------------------------
# The namespace table
# ---------------------------------------------------------------------------
# One prefix per node kind in §5's vocabulary. Prefixes are all three characters
# so a rail line's ids column stays aligned without padding logic. The mapping
# is bijective and is the single source of truth for both directions.

KIND_PREFIXES = {
    "session": "ses",
    "turn": "trn",
    "tool": "tol",
    "subagent": "sub",
    "workflow": "wfr",
    "phase": "wfp",
    "agent": "wfa",
    "job": "job",
    "watch": "wch",
    "swarm": "swm",
    "candidate": "cnd",
    "team": "tem",
    "peer": "per",
    "schedule": "cro",
    "run": "run",
}

PREFIX_KINDS = {prefix: kind for kind, prefix in KIND_PREFIXES.items()}

#: Node kinds in declaration order — useful for stable rendering and tests.
KINDS: Tuple[str, ...] = tuple(KIND_PREFIXES)

# ``/`` is legal in a whole local part because scoped ids (``wfp:4f2a/Review``)
# are the whole reason it is not a bare integer. Within one *segment* nothing
# outside ``_UNSAFE_SEGMENT``'s alphabet survives — that alphabet is exactly
# ``workflow_store._SAFE_ID`` / ``paths._SAFE_ID``, which is what makes
# :func:`safe_path_segment` a total function into legal filenames.
_UNSAFE_LOCAL = re.compile(r"[^A-Za-z0-9._/-]+")
_UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
# ASCII blanks only: a caller passing whitespace/separators passed nothing, but
# NBSP and friends are model text and must still produce an id.
_BLANK_LOCAL = re.compile(r"^[ \t\r\n\f\v/]*$")
#: Characters that carry no information at a segment boundary. ``.`` is in the
#: set on purpose: stripping it is what makes ``..`` impossible to express.
_EDGE = "-._"
_LOCAL_MAX = 64
#: Cap for the scope half of a scoped id, so the title half keeps useful room.
_SCOPE_MAX = 24
#: Cap for :func:`safe_path_segment`, which flattens a whole id into one name.
_SEGMENT_MAX = 80
_DIGEST_LEN = 10


def _digest(text: str) -> str:
    """A short, stable, filename-safe fingerprint of ``text``.

    ``surrogatepass`` because model text can carry lone surrogates from a broken
    decode, and a hash of a title must not be the thing that raises.
    """

    raw = text.encode("utf-8", "surrogatepass")
    return hashlib.blake2b(raw, digest_size=_DIGEST_LEN // 2).hexdigest()[:_DIGEST_LEN]


def _collapse(text: str) -> str:
    """Squeeze substitution artifacts: runs of ``-`` and the boundary chars."""

    return re.sub(r"-{2,}", "-", text).strip(_EDGE)


def _segment_token(raw: str, limit: int) -> str:
    """One path-safe, never-empty, never-``..`` token for ``raw``.

    The readable projection is kept alone only when it can stand in for the
    original: the original is ASCII (so nothing script-shaped was dropped), the
    projection is non-empty, and it fits ``limit``. Otherwise a digest of the
    original is appended — after a readable prefix when there is one — which is
    what keeps ``レビュー`` and ``审查代码`` apart while ``Review`` stays ``Review``.
    """

    text = unicodedata.normalize("NFC", raw)
    core = text.strip()
    proj = _collapse(_UNSAFE_SEGMENT.sub("-", core))
    if proj and len(proj) <= limit and not _NON_ASCII.search(core):
        return proj
    # ``core`` when the input has content, ``text`` when it is only whitespace —
    # so two different blank-ish titles still land on two different tokens.
    digest = _digest(core or text)
    room = limit - _DIGEST_LEN - 1
    head = proj[:room].strip(_EDGE) if room > 0 else ""
    return head + "-" + digest if head else digest


def _scope_token(scope: str) -> str:
    """The scope half of a scoped local id.

    A scope is not model text — it is a run id this module already produced, so
    it is safe by construction. Digesting it for length therefore breaks the one
    property the scoping exists for: that ``wfp:<run>/<phase>`` *names its run*.
    A 36-character run id used to come back as ``b7d1f0a2-1c4e-af8b946f32`` in
    the phase while the run's own node stayed ``b7d1f0a2-1c4e-4d3a-…``, so the
    two no longer agreed and nothing could get from a phase back to its run.

    Real generators are short (``workflow.py`` mints base36 counters), which is
    why this was latent — but "the id is short in practice" is not the property
    the scheme claims. An already-safe scope passes through whatever its length;
    only an unsafe one is normalized, and that one was never a run id anyway.
    """

    if scope and not _UNSAFE_SEGMENT.search(scope) and scope.strip(_EDGE):
        return scope
    return _segment_token(scope, _SCOPE_MAX)


def _normalize_local(local: object) -> str:
    """Coerce ``local`` to the restricted id alphabet.

    Returns ``""`` only for input that was blank to begin with (``""``,
    ``"   "``, ``"///"``); every other input yields a non-empty local part. At
    most one ``/`` survives, splitting an optional scope from the name, and each
    half is an independently normalized segment — so a title cannot add a path
    level, and ``..`` cannot appear as one.
    """

    raw = str(local)
    if _BLANK_LOCAL.match(raw):
        return ""
    scope, sep, name = raw.partition("/")
    if not sep or not scope.strip():
        # Unscoped: the whole string is one segment, so any ``/`` inside it
        # folds to ``-`` rather than becoming a directory boundary.
        return _segment_token(raw.lstrip("/"), _LOCAL_MAX)
    # A blank name keeps its own (derived) segment rather than collapsing onto
    # the scope: a phase whose title was whitespace is still not the run.
    scope_token = _scope_token(scope)
    # Never let a long scope starve the name: a phase whose title digested down
    # to nothing would collide with every other phase in the same run.
    room = max(_DIGEST_LEN, _LOCAL_MAX - 1 - len(scope_token))
    return scope_token + "/" + _segment_token(name, room)


def make_id(kind: str, local: object) -> str:
    """Build ``"<prefix>:<local>"`` for ``kind``.

    ``local`` may be an ``int`` (``Job.id``), a ``str`` (a base36 run id), or a
    scoped ``"<run>/<name>"`` pair. It is normalized, so
    ``make_id("phase", "4f2a/Review the code") == "wfp:4f2a/Review-the-code"``.

    Arbitrary text is safe here: a title in any script, of any length, made of
    punctuation or emoji alone, still yields a distinct, path-safe id rather
    than an exception. Raises :class:`InvalidIdError` only for an unknown kind
    or for a local part that was blank before normalization ever ran.
    """

    prefix = KIND_PREFIXES.get(kind)
    if prefix is None:
        raise InvalidIdError(f"unknown activity kind {kind!r}")
    norm = _normalize_local(local)
    if not norm:
        raise InvalidIdError(f"blank local id for kind {kind!r} (from {local!r})")
    return prefix + ":" + norm


def safe_path_segment(value: object) -> str:
    """Flatten ``value`` (typically an activity id) into one path component.

    This is the function the journal must use before an id reaches the
    filesystem: an id is not a filename — it carries ``:`` and may carry the
    scope ``/``. The result is non-empty, is never ``.`` or ``..``, contains no
    separator of any kind, matches ``[A-Za-z0-9._-]+``, and is bounded. Any
    rewriting appends a digest of the input, so two ids that differ at all get
    two different filenames.
    """

    text = unicodedata.normalize("NFC", str(value)).strip()
    proj = _collapse(_UNSAFE_SEGMENT.sub("-", text))
    if proj == text and proj and len(proj) <= _SEGMENT_MAX:
        return proj
    digest = _digest(text or str(value))
    room = _SEGMENT_MAX - _DIGEST_LEN - 1
    head = proj[:room].strip(_EDGE) if room > 0 else ""
    return head + "-" + digest if head else digest


def parse_id(s: str) -> Tuple[str, str]:
    """Split an activity id into ``(kind, local)``.

    Raises :class:`InvalidIdError` when ``s`` has no ``:``, carries an unknown
    prefix, has an empty local part, contains characters outside the id
    alphabet, or has a segment that is empty or made only of ``.-_`` (``..``
    above all). Any of those would mean ``s`` never came from :func:`make_id`,
    and accepting one is how a hand-built string becomes a path traversal.
    """

    if not isinstance(s, str) or ":" not in s:
        raise InvalidIdError(f"not an activity id: {s!r}")
    prefix, _, local = s.partition(":")
    kind = PREFIX_KINDS.get(prefix)
    if kind is None:
        raise InvalidIdError(f"unknown activity id prefix {prefix!r} in {s!r}")
    if not local:
        raise InvalidIdError(f"activity id {s!r} has an empty local part")
    if _UNSAFE_LOCAL.search(local):
        raise InvalidIdError(f"activity id {s!r} has an unsafe local part")
    for segment in local.split("/"):
        if not segment.strip(_EDGE):
            raise InvalidIdError(f"activity id {s!r} has an unsafe path segment")
    return kind, local


def try_parse_id(s: str) -> Optional[Tuple[str, str]]:
    """:func:`parse_id` that returns ``None`` instead of raising."""

    try:
        return parse_id(s)
    except InvalidIdError:
        return None


def is_id(s: str) -> bool:
    """True when ``s`` is a well-formed activity id."""

    return try_parse_id(s) is not None


def kind_of(s: str) -> str:
    """The kind of ``s`` (raises for a malformed id)."""

    return parse_id(s)[0]


def local_of(s: str) -> str:
    """The engine-local part of ``s`` (raises for a malformed id)."""

    return parse_id(s)[1]


# ---------------------------------------------------------------------------
# User shorthand resolution
# ---------------------------------------------------------------------------
# ``/job 3``, ``run 4f2a`` and rail navigation must all land on the same lookup,
# so shorthand resolution lives here next to the grammar it depends on rather
# than being re-implemented per command.


def _candidate_tiers(ref: str, ids: Iterable[str], kind: Optional[str]) -> list:
    """Return match tiers, strongest first, for ``ref`` against ``ids``."""

    pool = []
    for nid in ids:
        parsed = try_parse_id(nid)
        if parsed is None:
            continue
        if kind is not None and parsed[0] != kind:
            continue
        pool.append((nid, parsed[1]))

    low = ref.casefold()
    exact_id = [nid for nid, _ in pool if nid == ref or nid.casefold() == low]
    exact_local = [nid for nid, loc in pool if loc.casefold() == low]
    # ``Review`` should find ``wfp:4f2a/Review`` — the last path segment is the
    # human-meaningful half of a scoped id.
    last_seg = [nid for nid, loc in pool if loc.rsplit("/", 1)[-1].casefold() == low]
    prefixed = [nid for nid, loc in pool if loc.casefold().startswith(low)]
    return [exact_id, exact_local, last_seg, prefixed]


def resolve_refs(text: str, nodes: Iterable[str], *, kind: Optional[str] = None) -> list:
    """All node ids matching the user shorthand ``text``, best tier only.

    ``nodes`` is any iterable of activity ids — passing ``registry.nodes``
    (a dict keyed by id) works because iterating a dict yields its keys.

    Matching walks four tiers: full id, exact local part, last scoped segment,
    then local-part prefix. The first tier with any hit wins outright; an
    ambiguity at a strong tier is *not* broken by a weaker one, because the
    weaker tier's answer would be a guess.
    """

    ref = (text or "").strip()
    while ref[:1] == "#":
        ref = ref[1:].strip()
    if not ref:
        return []
    for tier in _candidate_tiers(ref, nodes, kind):
        if tier:
            # Deduplicate while preserving the caller's node order.
            seen = set()
            out = []
            for nid in tier:
                if nid not in seen:
                    seen.add(nid)
                    out.append(nid)
            return out
    return []


def resolve_ref(
    text: str, nodes: Iterable[str], *, kind: Optional[str] = None
) -> Optional[str]:
    """The single node id ``text`` refers to, or ``None``.

    ``None`` means either "no match" or "ambiguous" — both are cases where a
    caller must ask rather than act. Use :func:`resolve_refs` to show the user
    what the alternatives were.
    """

    hits = resolve_refs(text, nodes, kind=kind)
    return hits[0] if len(hits) == 1 else None
