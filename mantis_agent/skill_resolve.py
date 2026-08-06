"""Where a skill came from, which copy won, and what it stacks on top of.

:func:`mantis_agent.skills.discover_skills` currently merges skill files into a
``dict`` keyed by name, so when the user's ``~/.mantis-agent/skills/review`` and
the repo's ``./.mantis/skills/review`` both exist the winner is *whatever
iteration order produced*. Nothing records that a file was overridden, nothing
can explain the choice, and the only way to add a project-specific paragraph to
a user skill is to copy the whole thing and replace it. This module is the
missing policy layer for all three:

**Tiers and precedence.** Every candidate carries a ``tier``. The order is
fixed and total — ``managed`` > ``runtime`` > ``user`` > ``local`` > nearest
``project`` > farther ``project`` > ``plugin`` > ``builtin`` — so resolution is
a pure function of the inputs, never of directory-walk order.

**Nearest-project-wins.** Repo-tier definitions are ordered by how far *up*
from the cwd their project root sits, reusing ``project_memory._walk_up`` (the
same ancestor walk ``MANTIS.md`` discovery already uses). In a monorepo,
``packages/api/.mantis/skills/deploy`` beats the repository root's.

**Shadowing is recorded, never silent.** :func:`resolve` returns the winner
*and* every loser with its full path, and :func:`explain` prints the chain for
``/skills why <name>``. A definition that lost is a fact the user is entitled
to; discarding it is the bug this module exists to fix.

**Same-tier ties are an error.** Two plugins that both define ``review`` at
equal precedence produce :class:`SkillCollisionError` naming both paths, not an
arbitrary pick. There is no correct answer, so we refuse to invent one.

**Stacking.** ``extends: user:review`` composes instead of replacing: parent
body, separator, child body; metadata merges shallowly with the child winning
per key. ``allowed_tools`` is the exception and the whole point — it
**intersects, never unions**. A child may narrow its parent's grant and may
never widen it, so a project skill cannot use ``extends`` to hand itself tools
the user's skill never had. Cycles are reported with the chain; depth is capped.

This module is pure: no I/O beyond ``is_dir`` path probing in
:func:`project_skill_dirs`, no parsing, no prompt assembly, and no knowledge of
the trust gate (that shipped in :mod:`mantis_agent.skill_trust`, which decides
*whether* a project candidate exists at all — this module only orders the ones
that survive). Stdlib + the package's own error base.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .errors import AgentError

__all__ = [
    "BUILTIN_TIER",
    "FAR_DEPTH",
    "LOCAL_TIER",
    "MANAGED_TIER",
    "MAX_STACK_DEPTH",
    "NESTED_TIERS",
    "PLUGIN_TIER",
    "PROJECT_TIER",
    "RUNTIME_TIER",
    "Resolution",
    "ResolutionSet",
    "SkillCandidate",
    "SkillCollisionError",
    "SkillCycleError",
    "SkillDepthExceededError",
    "SkillError",
    "SkillReferenceError",
    "SkillToolWideningError",
    "StackedSkill",
    "TIER_ORDER",
    "USER_TIER",
    "candidate_from_skill",
    "explain",
    "intersect_allowed_tools",
    "normalize_allowed_tools",
    "project_depth",
    "project_skill_dirs",
    "resolve",
    "stack",
    "stack_separator",
    "tier_rank",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
#
# These live here rather than in errors.py because they are meaningful only to
# the resolver; when the wider skills package lands (plan §18) it can re-export
# them. Every one carries the *data* the message was built from, so a UI can
# re-render the failure without scraping the string.


class SkillError(AgentError):
    """Base for every skill resolution / composition failure."""


class SkillCollisionError(SkillError):
    """Two definitions of one name sit at exactly the same precedence.

    Deliberately fatal for that name rather than "pick one": at equal
    precedence there is no rule that would make the choice reproducible, and a
    coin flip that lands differently on another machine is worse than an error
    that names both files.
    """

    __slots__ = ("name", "candidates")

    def __init__(self, name: str, candidates: Sequence[SkillCandidate]):
        self.name = name
        self.candidates = tuple(candidates)
        where = "\n".join(f"  {c.tier}  {c.location}" for c in self.candidates)
        tier = self.candidates[0].tier if self.candidates else "?"
        super().__init__(
            f"skill {name!r} is defined more than once at the same precedence "
            f"(tier {tier!r}):\n{where}\n"
            "Rename one, or move it to a different tier."
        )

    @property
    def paths(self) -> tuple[Path | None, ...]:
        return tuple(c.path for c in self.candidates)


class SkillCycleError(SkillError):
    """``extends`` forms a loop. The full chain is reported, not just the tip."""

    __slots__ = ("chain",)

    def __init__(self, chain: Sequence[SkillCandidate]):
        self.chain = tuple(chain)
        rendered = " -> ".join(c.ref for c in self.chain)
        super().__init__(f"extends cycle: {rendered}")


class SkillDepthExceededError(SkillError):
    """The ``extends`` chain is longer than the configured cap."""

    __slots__ = ("chain", "max_depth")

    def __init__(self, chain: Sequence[SkillCandidate], max_depth: int):
        self.chain = tuple(chain)
        self.max_depth = max_depth
        rendered = " -> ".join(c.ref for c in self.chain)
        super().__init__(
            f"extends chain exceeds max depth {max_depth}: {rendered}"
        )


class SkillToolWideningError(SkillError):
    """A child's ``allowed_tools`` asked for something its parent never granted.

    The narrowing rule is the reason stacking is safe to offer to project-tier
    files at all, so a widening attempt is refused loudly instead of being
    silently intersected away — a skill whose declared grant does not match its
    effective grant is a lie the user would have to discover the hard way.
    """

    __slots__ = ("parent_ref", "child_ref", "widened", "parent_tools")

    def __init__(
        self,
        parent_ref: str,
        child_ref: str,
        widened: Sequence[str],
        parent_tools: Sequence[str],
    ):
        self.parent_ref = parent_ref
        self.child_ref = child_ref
        self.widened = tuple(widened)
        self.parent_tools = tuple(parent_tools)
        super().__init__(
            f"{child_ref} cannot widen {parent_ref}'s allowed_tools: "
            f"{', '.join(self.widened)} not granted by parent "
            f"({', '.join(self.parent_tools) or 'none'})"
        )


class SkillReferenceError(SkillError):
    """An ``extends`` reference is malformed, traverses paths, or resolves to
    nothing."""

    __slots__ = ("ref", "source_ref")

    def __init__(self, ref: str, source_ref: str, reason: str):
        self.ref = ref
        self.source_ref = source_ref
        super().__init__(f"{source_ref}: extends {ref!r} — {reason}")


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

#: Shipped with mantis itself. Lowest precedence: anything the user or the repo
#: writes is a deliberate override of a default.
BUILTIN_TIER = "builtin"
#: Installed packages (plan ``j_plugin_packages_and_marketplaces.md``).
PLUGIN_TIER = "plugin"
#: ``<repo>/.mantis/skills`` — arrives with a clone, gated by skill_trust.
PROJECT_TIER = "project"
#: ``<repo>/.mantis/skills.local`` — the user's own uncommitted repo overlay, so
#: it outranks the checked-in project tier while still being repo-scoped.
LOCAL_TIER = "local"
#: ``~/.mantis-agent/skills`` — the user's own files.
USER_TIER = "user"
#: ``SkillRegistry.add()`` — the embedding host chose it in code, at runtime.
RUNTIME_TIER = "runtime"
#: ``/etc/mantis-agent/skills`` — machine administrator policy. Wins outright.
MANAGED_TIER = "managed"

#: Highest precedence first. This ordering is the contract; everything else in
#: the module is bookkeeping around it.
TIER_ORDER: tuple[str, ...] = (
    MANAGED_TIER,
    RUNTIME_TIER,
    USER_TIER,
    LOCAL_TIER,
    PROJECT_TIER,
    PLUGIN_TIER,
    BUILTIN_TIER,
)

_TIER_RANK: dict[str, int] = {t: i for i, t in enumerate(TIER_ORDER)}

#: Tiers whose members are additionally ordered by distance from the cwd.
NESTED_TIERS = frozenset({LOCAL_TIER, PROJECT_TIER})

#: Depth assigned to a repo-tier candidate whose path is not an ancestor of the
#: cwd at all. Sorts after every real depth — an unrelated tree should never
#: beat the tree you are standing in.
FAR_DEPTH = 1_000_000


def tier_rank(tier: str) -> int:
    """Precedence index of ``tier``; lower wins.

    An unrecognised tier sorts *after* every known one. That is the fail-closed
    direction: a definition claiming ``tier: superuser`` gets less authority
    than the tiers we actually reason about, not more.
    """

    return _TIER_RANK.get(tier, len(TIER_ORDER))


# ---------------------------------------------------------------------------
# Locating repo-tier directories (nearest-project-wins)
# ---------------------------------------------------------------------------


def _walk_up(start: Path) -> list[Path]:
    """``start`` and every ancestor, nearest first.

    Reuses ``project_memory._walk_up`` rather than repeating it: "which
    directories count as *this* project" must give the same answer for skills
    as it does for ``MANTIS.md``, and two copies of that walk would eventually
    disagree. Imported lazily to keep this module's import graph a leaf.
    """

    from .project_memory import _walk_up as _pm_walk_up  # noqa: PLC0415

    return _pm_walk_up(start)


def _resolved(path: str | Path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:  # pragma: no cover — unresolvable path
        return Path(path).absolute()


def project_depth(path: str | Path, cwd: str | Path | None = None) -> int:
    """How far up from ``cwd`` the project root containing ``path`` sits.

    ``0`` means the cwd itself, ``1`` its parent, and so on. Smaller is nearer,
    and nearer wins — a monorepo package's skill beats the repository root's
    copy of the same name. A path off the cwd's branch resolves at whatever
    ancestor the two do share, which is naturally farther than anything inside
    the branch; :data:`FAR_DEPTH` is the fallback for no shared ancestor at all
    (another drive or mount root).
    """

    target = _resolved(path)
    base = _resolved(cwd) if cwd is not None else _resolved(Path.cwd())
    for depth, ancestor in enumerate(_walk_up(base)):
        try:
            target.relative_to(ancestor)
        except ValueError:
            continue
        return depth
    return FAR_DEPTH


def project_skill_dirs(
    cwd: str | Path | None = None,
    *,
    include_local: bool = True,
) -> list[tuple[Path, str, int]]:
    """Existing repo-tier skill directories as ``(dir, tier, depth)``.

    Walks up from ``cwd`` so a nested package's ``.mantis/skills`` is found
    *and* is reported nearer than the repository root's. ``skills.local`` is
    surfaced as the :data:`LOCAL_TIER` at the same depth. Returned in
    precedence order (local before project, nearer before farther) so a caller
    that simply concatenates already gets the right sequence.

    This is the only function in the module that touches the filesystem, and it
    only probes ``is_dir`` — no file is opened, so nothing here can put text in
    a prompt.
    """

    from .project_memory import WORKSPACE_DIR  # noqa: PLC0415
    from .skills import SKILLS_SUBDIR  # noqa: PLC0415

    base = _resolved(cwd) if cwd is not None else _resolved(Path.cwd())
    out: list[tuple[Path, str, int]] = []
    for depth, ancestor in enumerate(_walk_up(base)):
        ws = ancestor / WORKSPACE_DIR
        pairs = [(ws / f"{SKILLS_SUBDIR}.local", LOCAL_TIER)] if include_local else []
        pairs.append((ws / SKILLS_SUBDIR, PROJECT_TIER))
        for d, tier in pairs:
            try:
                if d.is_dir():
                    out.append((d, tier, depth))
            except OSError:  # pragma: no cover — unreadable ancestor
                continue
    return out


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillCandidate:
    """One definition of one name, before resolution decides whether it wins.

    ``meta`` is the raw frontmatter mapping, kept whole so unknown keys survive
    a stack (plan §5: "a file written for a newer version still loads").
    ``allowed_tools`` is pulled out of it because it is the one key with
    non-shallow merge semantics, and ``extends`` because it drives composition.

    ``depth`` is only meaningful for :data:`NESTED_TIERS` and is normally left
    ``None`` for :func:`resolve` to fill in from the cwd.

    ``trusted`` is display-only provenance for :func:`explain`; the gate that
    actually withholds untrusted project files is
    :mod:`mantis_agent.skill_trust`, upstream of this module.
    """

    name: str
    tier: str = USER_TIER
    path: Path | None = None
    body: str = ""
    description: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] | None = None
    extends: str | None = None
    depth: int | None = None
    trusted: bool = True

    @property
    def ref(self) -> str:
        """``tier:name`` — the form ``extends`` uses and ``explain`` prints."""

        return f"{self.tier}:{self.name}"

    @property
    def location(self) -> str:
        """Path if it came from a file, else a ``<tier>`` placeholder — runtime
        and builtin candidates have no file and must still be nameable."""

        return str(self.path) if self.path is not None else f"<{self.tier}>"

    def effective_depth(self) -> int:
        """Sort depth: real depth for nested tiers, ``0`` everywhere else."""

        if self.tier not in NESTED_TIERS:
            return 0
        return FAR_DEPTH if self.depth is None else self.depth


def candidate_from_skill(
    skill: Any,
    *,
    tier: str | None = None,
    path: str | Path | None = None,
    depth: int | None = None,
    trusted: bool = True,
    meta: Mapping[str, Any] | None = None,
) -> SkillCandidate:
    """Adapt a :class:`mantis_agent.skills.Skill` into a candidate.

    ``Skill.source`` already carries ``"user"`` / ``"project"``, so the existing
    discovery output slots in without any change to ``skills.py``; pass ``tier``
    explicitly for the tiers ``Skill`` cannot yet express.
    """

    allowed = normalize_allowed_tools(getattr(skill, "allowed_tools", None))
    return SkillCandidate(
        name=skill.name,
        tier=tier or getattr(skill, "source", USER_TIER) or USER_TIER,
        path=Path(path) if path is not None else None,
        body=getattr(skill, "body", "") or "",
        description=getattr(skill, "description", "") or "",
        meta=dict(meta or {}),
        allowed_tools=allowed,
        extends=getattr(skill, "extends", None),
        depth=depth,
        trusted=trusted,
    )


def normalize_allowed_tools(value: Any) -> tuple[str, ...] | None:
    """Coerce a frontmatter ``allowed_tools`` value to a tuple, or ``None``.

    ``None`` means *undeclared* — inherit whatever the parent granted. An empty
    list is NOT the same thing: it declares "no tools", which is a real (and
    maximally narrow) grant, so it is preserved as an empty tuple.
    """

    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[,\n]", value)]
        return tuple(p for p in parts if p)
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """The winner for one name plus every definition it shadows, in order."""

    name: str
    winner: SkillCandidate
    shadowed: tuple[SkillCandidate, ...] = ()

    @property
    def all_candidates(self) -> tuple[SkillCandidate, ...]:
        return (self.winner, *self.shadowed)

    @property
    def is_shadowing(self) -> bool:
        return bool(self.shadowed)


@dataclass(frozen=True)
class ResolutionSet:
    """Every resolved name. Mapping-ish so callers can treat it as a dict."""

    by_name: dict[str, Resolution] = field(default_factory=dict)

    def get(self, name: str) -> Resolution | None:
        return self.by_name.get(name)

    def __getitem__(self, name: str) -> Resolution:
        return self.by_name[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.by_name

    def __iter__(self) -> Iterator[str]:
        return iter(self.by_name)

    def __len__(self) -> int:
        return len(self.by_name)

    def winners(self) -> list[SkillCandidate]:
        """The effective set — what discovery should hand to the registry."""

        return [r.winner for r in self.by_name.values()]

    def shadowed(self) -> list[SkillCandidate]:
        """Everything that lost, for a ``/skills`` listing to show as dimmed."""

        return [c for r in self.by_name.values() for c in r.shadowed]


def _sort_key(c: SkillCandidate) -> tuple[int, int]:
    return (tier_rank(c.tier), c.effective_depth())


def _dedupe(candidates: Sequence[SkillCandidate]) -> list[SkillCandidate]:
    """Drop exact re-scans of the same file at the same tier.

    Walking up from the cwd can legitimately hand the same directory back twice
    (a symlinked package, a repeated root); that is not a collision, so it must
    not be reported as one.
    """

    seen: set[tuple[str, str]] = set()
    out: list[SkillCandidate] = []
    for c in candidates:
        if c.path is None:
            out.append(c)
            continue
        key = (c.tier, str(_resolved(c.path)))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def resolve(
    candidates: Iterable[SkillCandidate],
    *,
    cwd: str | Path | None = None,
) -> ResolutionSet:
    """Group ``candidates`` by name and decide each winner deterministically.

    Depth is filled in for repo-tier candidates that did not carry one, using
    ``cwd`` (default: the process cwd), so nearest-project-wins works on the
    raw output of a directory walk.

    Raises :class:`SkillCollisionError` when the top two candidates for a name
    tie on both tier and depth. A tie strictly *below* the winner is not fatal —
    it changes nothing the model sees, and refusing to resolve a name whose
    effective definition is unambiguous would be strictness without a payoff.

    Every loser is retained on the :class:`Resolution` — nothing is dropped,
    because silent shadowing is exactly the failure this replaces.
    """

    base = _resolved(cwd) if cwd is not None else None
    grouped: dict[str, list[SkillCandidate]] = {}
    for c in candidates:
        if c.tier in NESTED_TIERS and c.depth is None and c.path is not None:
            # Depth is derived from the cwd, not authored, so filling it in here
            # keeps callers from having to know the rule.
            c = replace(c, depth=project_depth(c.path, base))  # noqa: PLW2901
        grouped.setdefault(c.name, []).append(c)

    out: dict[str, Resolution] = {}
    for name, group in grouped.items():
        # ``sorted`` is stable, so equal keys keep discovery order — which is
        # precisely the ambiguity we refuse to resolve silently below.
        ordered = sorted(_dedupe(group), key=_sort_key)
        if len(ordered) > 1 and _sort_key(ordered[0]) == _sort_key(ordered[1]):
            tied = [c for c in ordered if _sort_key(c) == _sort_key(ordered[0])]
            raise SkillCollisionError(name, tied)
        out[name] = Resolution(name=name, winner=ordered[0], shadowed=tuple(ordered[1:]))
    return ResolutionSet(by_name=out)


# ---------------------------------------------------------------------------
# ``/skills why`` — the resolution chain
# ---------------------------------------------------------------------------


def explain(name: str, resolved: ResolutionSet, *, stacked: StackedSkill | None = None) -> str:
    """Render the plan §13 resolution chain for ``/skills why <name>``.

    Prints the winner, why it won, and every definition it shadows *with its
    path*. Feed the same :class:`ResolutionSet` that produced the live registry
    and the output cannot drift from the actual decision — the point of the
    command is to be trustworthy about a choice the user could not otherwise
    see.
    """

    res = resolved.get(name)
    if res is None:
        return f"{name}\n  no definition found"

    win = res.winner
    nearest = win.tier in NESTED_TIERS and any(s.tier == win.tier for s in res.shadowed)
    head = f"{name}  → {win.tier}" + (" (nearest)" if nearest else "")
    trust = "trusted" if win.trusted else "UNTRUSTED"
    # ``✓``/``·`` columns are padded to the same width so the paths line up and
    # the eye can compare them — the whole value of the command is the diff
    # between the file that won and the files that did not.
    lines = [head, f"  ✓ {win.tier:<9} {win.location}   {trust}"]

    if res.shadowed:
        lines.append("    shadows")
        for s in res.shadowed:
            note = ""
            if s.tier == win.tier and s.tier in NESTED_TIERS:
                note = "   (farther)"
            elif not s.trusted:
                note = "   (untrusted)"
            lines.append(f"  · {s.tier:<9} {s.location}{note}")

    chain = stacked.chain if stacked is not None else ()
    if win.extends:
        lines.append(f"  {'extends':<11} {win.extends}")
    tools = stacked.allowed_tools if stacked is not None else win.allowed_tools
    if tools is not None:
        narrowed = ""
        root_tools = chain[0].allowed_tools if len(chain) > 1 else None
        if root_tools is not None and tuple(root_tools) != tuple(tools):
            narrowed = "   [narrowed from parent]"
        lines.append(f"  {'tools':<11} {', '.join(tools) or '(none)'}{narrowed}")
    blocks = win.meta.get("shell") if isinstance(win.meta, Mapping) else None
    if isinstance(blocks, (list, tuple)) and blocks:
        lines.append(f"  {'shell':<11} {len(blocks)} block(s)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stacking (``extends``)
# ---------------------------------------------------------------------------

#: Maximum number of ``extends`` hops above the child. Three is deep enough for
#: managed → user → project layering and shallow enough that a mistake surfaces
#: as an error rather than as a slow, enormous prompt.
MAX_STACK_DEPTH = 3

#: Grants that mean "everything". Recognised so that widening *to* them is
#: caught rather than passing through as an ordinary unknown tool name.
_UNCONSTRAINED = frozenset({"all", "*"})

# A reference is ``name`` or ``tier:name``. No slashes, no dots-dots, nothing
# that could be read as a path — ``extends`` names a definition, never a file.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class StackedSkill:
    """A child composed onto its ancestors.

    ``chain`` is root-ancestor-first, child last — the order the bodies were
    concatenated in, so a UI can show the layering it produced.
    """

    name: str
    body: str
    description: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] | None = None
    chain: tuple[SkillCandidate, ...] = ()

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(c.ref for c in self.chain)

    @property
    def depth(self) -> int:
        """Number of ``extends`` hops that produced this body."""

        return max(0, len(self.chain) - 1)


def stack_separator(parent_ref: str, child_ref: str) -> str:
    """The visible boundary between a parent body and the child extending it.

    Visible on purpose: the model is reading two documents written by two
    different authors at two different trust levels, and it should be able to
    tell where one stops.
    """

    return f"\n\n--- {child_ref} extends {parent_ref} ---\n\n"


def _covers(granted: str, requested: str) -> bool:
    """Does a parent's grant already include ``requested``?

    Exact match, plus the one narrowing shape that is unambiguous: a bare tool
    name covers its parameterised form, so a parent granting ``bash`` covers a
    child asking only for ``bash(git diff:*)``. Anything subtler (wildcard
    containment inside the parameter grammar) belongs to the permission engine
    in ``f_permission_policy_engine_and_auto_mode.md``; guessing at it here
    would risk calling a widening a narrowing, which is the failure that
    matters.
    """

    g = granted.strip()
    r = requested.strip()
    if g == r:
        return True
    return "(" in r and r.split("(", 1)[0].strip() == g


def intersect_allowed_tools(
    parent: Sequence[str] | None,
    child: Sequence[str] | None,
    *,
    parent_ref: str = "parent",
    child_ref: str = "child",
) -> tuple[str, ...] | None:
    """Combine two ``allowed_tools`` grants by **intersection**, never union.

    * child undeclared → inherits the parent's grant verbatim;
    * parent undeclared or unconstrained (``all`` / ``*``) → the child's grant
      stands, since narrowing an unbounded grant is always allowed;
    * otherwise every child entry must be covered by the parent — anything that
      is not raises :class:`SkillToolWideningError`.

    This is the rule that makes ``extends`` safe across a trust boundary: a
    repo-tier skill may layer instructions onto a user skill without being able
    to enlarge what that skill was permitted to drive.
    """

    if child is None:
        return tuple(parent) if parent is not None else None
    child_t = tuple(child)
    if parent is None:
        return child_t
    parent_t = tuple(parent)
    if any(p.strip().lower() in _UNCONSTRAINED for p in parent_t):
        return child_t
    if any(c.strip().lower() in _UNCONSTRAINED for c in child_t):
        raise SkillToolWideningError(
            parent_ref, child_ref,
            [c for c in child_t if c.strip().lower() in _UNCONSTRAINED],
            parent_t,
        )
    widened = [c for c in child_t if not any(_covers(p, c) for p in parent_t)]
    if widened:
        raise SkillToolWideningError(parent_ref, child_ref, widened, parent_t)
    # Every child entry is covered, so the intersection *is* the child's list —
    # keep its order and its (possibly narrower) parameterised spellings.
    return child_t


def _parse_ref(ref: str, source: SkillCandidate) -> tuple[str | None, str]:
    """``"user:review"`` → ``("user", "review")``; ``"review"`` → ``(None, "review")``."""

    raw = (ref or "").strip()
    tier: str | None = None
    name = raw
    if ":" in raw:
        head, _, tail = raw.partition(":")
        if head in _TIER_RANK:
            tier, name = head, tail.strip()
        else:
            raise SkillReferenceError(ref, source.ref, f"unknown tier {head!r}")
    if not _REF_RE.match(name):
        # Covers "", "../../etc/passwd", "a/b" — an extends target is a *name*.
        raise SkillReferenceError(ref, source.ref, "not a valid skill name")
    return tier, name


def _lookup_parent(child: SkillCandidate, resolved: ResolutionSet) -> SkillCandidate:
    tier, name = _parse_ref(child.extends or "", child)
    res = resolved.get(name)
    if res is None:
        raise SkillReferenceError(child.extends or "", child.ref, "no such skill")
    pool = res.all_candidates
    if tier is not None:
        # Tier-qualified: match only that tier, and do NOT skip the child
        # itself — ``project:x`` extending ``project:x`` is a cycle, and saying
        # so is more useful than reporting the target as missing.
        for c in pool:
            if c.tier == tier:
                return c
        raise SkillReferenceError(child.extends or "", child.ref, f"no {tier}-tier definition")
    # Bare name: the winner, unless that is the child, in which case fall
    # through to the next-ranked definition. This is what lets a project skill
    # say ``extends: review`` and mean "the copy I am shadowing".
    for c in pool:
        if _identity(c) != _identity(child):
            return c
    raise SkillReferenceError(child.extends or "", child.ref, "resolves to itself")


def _identity(c: SkillCandidate) -> tuple[str, str, str]:
    return (c.tier, c.name, str(_resolved(c.path)) if c.path is not None else "")


def stack(
    child: SkillCandidate,
    resolved: ResolutionSet,
    *,
    max_depth: int = MAX_STACK_DEPTH,
) -> StackedSkill:
    """Compose ``child`` onto everything its ``extends`` chain names.

    Bodies concatenate root-first with :func:`stack_separator` between them;
    ``meta`` merges shallowly with the child winning per key; ``allowed_tools``
    intersects downward via :func:`intersect_allowed_tools` and a widening
    attempt raises. A candidate with no ``extends`` returns a one-element stack,
    so callers can run everything through here unconditionally.
    """

    chain: list[SkillCandidate] = [child]
    seen = {_identity(child)}
    cur = child
    hops = 0
    while cur.extends:
        hops += 1
        parent = _lookup_parent(cur, resolved)
        if _identity(parent) in seen:
            raise SkillCycleError([*chain, parent])
        if hops > max_depth:
            raise SkillDepthExceededError([*chain, parent], max_depth)
        seen.add(_identity(parent))
        chain.append(parent)
        cur = parent

    chain.reverse()  # root ancestor first — the order bodies are laid down in
    meta: dict[str, Any] = {}
    tools: tuple[str, ...] | None = None
    description = ""
    # (ref, body) for the layers that actually contribute text, so an empty
    # parent body doesn't leave a separator dangling above nothing.
    layers: list[tuple[str, str]] = []
    prev: SkillCandidate | None = None
    for c in chain:
        if prev is None:
            tools = c.allowed_tools
        else:
            tools = intersect_allowed_tools(
                tools, c.allowed_tools, parent_ref=prev.ref, child_ref=c.ref
            )
        meta.update(c.meta or {})
        if c.description:
            description = c.description  # child wins, same as every other key
        if c.body.strip():
            layers.append((c.ref, c.body.strip()))
        prev = c

    parts: list[str] = []
    for i, (ref, text) in enumerate(layers):
        if i:
            parts.append(stack_separator(layers[i - 1][0], ref))
        parts.append(text)

    return StackedSkill(
        name=child.name,
        body="".join(parts),
        description=description,
        meta=meta,
        allowed_tools=tools,
        chain=tuple(chain),
    )
