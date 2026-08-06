"""Capability tiers — proving the advisor is an *escalation*.

``advisor.py`` will resolve whatever model string it is handed. That is the
right behaviour for reachability and the wrong one for the premise: point
``/advisor`` at a 7B local model from an Opus session and the agent dutifully
escalates its hardest decisions to something weaker, silently, forever. The
whole feature is the direction of the arrow, and until this module existed
nothing checked which way it pointed.

What makes the check awkward is exactly what makes the advisor worth having:
**it does not share a provider with the main model.** A ranking that only
understood one vendor's model ids would refuse to compare the two sides of the
pairing the module was built for — a local Qwen escalating to a hosted Opus. So
the ordering here is deliberately provider-neutral and deliberately coarse:

* Five tiers (``frontier`` 5 … ``small`` 1), not a leaderboard. Coarse survives
  a release cycle; a benchmark number does not.
* Resolution is ``override > explicit > pattern > default``. Explicit entries
  are exact model ids. Patterns are how local models get ranked at all —
  ``qwen3:32b`` carries its own parameter count and no registry will ever be
  complete — and the *most specific* pattern wins, so ``gpt-oss-120b`` reads as
  120B rather than matching the ``*20b*`` rule.
* An unmatched model resolves to ``standard`` and is **flagged unknown**, which
  is the difference between reporting a guess and asserting a fact.
* The user can pin a tier per model. Someone running strong weights the table
  has never heard of has to be able to say so.

:func:`compare` reports the relation and never blocks. A downgrade is a warning
you cannot miss, not a refusal — the table is a coarse heuristic and the person
configuring it may know something it does not. Refusing on that basis would be
the same overreach as escalating downwards, in the other direction.

The table itself is data (``mantis_agent/data/capability_tiers.json``),
re-readable from ``$MANTIS_CAPABILITY_TIERS``, and reloaded when the file
changes on disk — model landscapes move faster than releases do.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DOWNGRADE",
    "ESCALATION",
    "SAME_TIER",
    "SOURCE_DEFAULT",
    "SOURCE_EXPLICIT",
    "SOURCE_OVERRIDE",
    "SOURCE_PATTERN",
    "UNKNOWN",
    "CapabilityTable",
    "Tier",
    "TierComparison",
    "TierPattern",
    "TierResolution",
    "compare",
    "load_tier_table",
    "normalize_overrides",
    "override_problems",
    "parse_tier_table",
    "resolve_tier",
    "tier_table_path",
    "user_capability_overrides",
]

#: Point this at another JSON file to replace the shipped table wholesale.
TABLE_ENV = "MANTIS_CAPABILITY_TIERS"

#: Where a per-model tier pin lives: ``{"advisor": {"capabilityOverrides": …}}``.
#: Read from the ``user`` settings tier ONLY — see :func:`user_capability_overrides`.
OVERRIDES_SETTING = "capabilityOverrides"

# How a tier was arrived at, most authoritative first. Reported on every
# resolution because "why is this model small?" is otherwise a source-reading
# exercise, and tier disputes are the predictable support burden here.
SOURCE_OVERRIDE = "override"
SOURCE_EXPLICIT = "explicit"
SOURCE_PATTERN = "pattern"
SOURCE_DEFAULT = "default"

# The relations :func:`compare` reports.
ESCALATION = "escalation"
SAME_TIER = "same-tier"
DOWNGRADE = "downgrade"
UNKNOWN = "unknown"

_CONFIDENCE = {
    SOURCE_OVERRIDE: "high",     # the user said so
    SOURCE_EXPLICIT: "high",     # a curated entry for this exact id
    SOURCE_PATTERN: "medium",    # inferred from the name's shape
    SOURCE_DEFAULT: "low",       # a guess, and reported as one
}


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tier:
    """One rung. ``rank`` is the only thing compared; the name is for humans."""

    name: str
    rank: int
    description: str = ""


@dataclass(frozen=True)
class TierPattern:
    """A glob over model names — ``*:70b*``, ``*-opus-*``, ``gpt-5*-mini*``.

    Patterns exist because model names are self-describing in the places a
    registry cannot reach: local tags carry their parameter count, and routers
    re-spell the same weights four different ways.
    """

    match: str
    tier: str
    note: str = ""

    @property
    def specificity(self) -> int:
        """Literal (non-wildcard) characters — how much the pattern claims.

        ``*120b*`` (4) beats ``*20b*`` (3) on ``gpt-oss-120b``, which is the
        whole reason the size rules can be written as plain substrings instead
        of a hand-ordered list.
        """
        return sum(1 for ch in self.match if ch not in "*?")


@dataclass(frozen=True)
class CapabilityTable:
    """The parsed ordering: tiers, exact ids, patterns, and a fallback."""

    tiers: dict[str, Tier] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)   # lowercased id → tier
    patterns: tuple[TierPattern, ...] = ()
    default: str = "standard"
    version: int = 0
    updated: str = ""
    source_path: str = ""

    def rank(self, tier_name: str) -> int:
        t = self.tiers.get(tier_name)
        return t.rank if t is not None else 0

    def describe(self, tier_name: str) -> str:
        t = self.tiers.get(tier_name)
        return t.description if t is not None else ""

    def has_tier(self, tier_name: Any) -> bool:
        return isinstance(tier_name, str) and tier_name in self.tiers

    def tier_names_by_rank(self) -> list[str]:
        """Strongest first — for rendering the table in ``/advisor tiers``."""
        return [t.name for t in sorted(self.tiers.values(),
                                       key=lambda t: (-t.rank, t.name))]


def parse_tier_table(data: Mapping[str, Any], *, source_path: str = "") -> CapabilityTable:
    """Build a :class:`CapabilityTable` from the JSON shape, or raise.

    Accepts the file's ``{"capabilityTiers": {…}}`` wrapper or the inner object
    on its own, so a hand-written override file can be either.

    Structural problems raise :class:`ValueError` rather than degrading: an
    entry naming a tier that does not exist would silently vanish the model
    into the default, which is precisely the invisible failure this module
    exists to end.
    """
    raw = data.get("capabilityTiers") if isinstance(data.get("capabilityTiers"), Mapping) else data
    if not isinstance(raw, Mapping):
        raise ValueError("capability tiers: expected a JSON object")

    tiers: dict[str, Tier] = {}
    for name, spec in (raw.get("tiers") or {}).items():
        if not isinstance(spec, Mapping) or not isinstance(spec.get("rank"), int):
            raise ValueError(f"capability tiers: tier {name!r} needs an integer rank")
        tiers[str(name)] = Tier(name=str(name), rank=int(spec["rank"]),
                                description=str(spec.get("description") or ""))
    if not tiers:
        raise ValueError("capability tiers: no tiers defined")

    default = str(raw.get("default") or "")
    if default not in tiers:
        raise ValueError(f"capability tiers: default {default!r} is not a defined tier")

    models: dict[str, str] = {}
    for model, tier in (raw.get("models") or {}).items():
        if tier not in tiers:
            raise ValueError(f"capability tiers: model {model!r} names unknown tier {tier!r}")
        models[str(model).strip().lower()] = str(tier)

    patterns: list[TierPattern] = []
    for entry in raw.get("patterns") or ():
        if not isinstance(entry, Mapping):
            raise ValueError("capability tiers: each pattern must be an object")
        match = str(entry.get("match") or "").strip().lower()
        tier = entry.get("tier")
        if not match:
            raise ValueError("capability tiers: a pattern is missing 'match'")
        if tier not in tiers:
            raise ValueError(f"capability tiers: pattern {match!r} names unknown tier {tier!r}")
        patterns.append(TierPattern(match=match, tier=str(tier),
                                    note=str(entry.get("note") or "")))

    return CapabilityTable(
        tiers=tiers,
        models=models,
        patterns=tuple(patterns),
        default=default,
        version=int(raw.get("version") or 0),
        updated=str(raw.get("updated") or ""),
        source_path=source_path,
    )


def tier_table_path() -> Path:
    """The table in force: ``$MANTIS_CAPABILITY_TIERS`` or the shipped copy."""
    override = (os.environ.get(TABLE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "data" / "capability_tiers.json"


# Parsed tables, keyed by (path, mtime_ns, size). Keying on the stat rather
# than the path alone is what makes the table refreshable: edit the JSON and
# the next resolution sees it, without a restart and without re-parsing on
# every call in the common case.
_CACHE: dict[tuple, CapabilityTable] = {}

# Last-resort table, so a resolution never raises just because the packaged
# file is missing from an odd install layout.
_FALLBACK = CapabilityTable(
    tiers={
        "frontier": Tier("frontier", 5), "advanced": Tier("advanced", 4),
        "standard": Tier("standard", 3), "fast": Tier("fast", 2),
        "small": Tier("small", 1),
    },
    default="standard",
)


def load_tier_table_file(path: str | Path) -> CapabilityTable:
    """Read and parse one table file. Raises on missing or malformed data."""
    p = Path(path).expanduser()
    return parse_tier_table(json.loads(p.read_text(encoding="utf-8")), source_path=str(p))


def load_tier_table(path: str | Path | None = None) -> CapabilityTable:
    """The table in force, parsed and cached until the file changes.

    A broken *user-supplied* table falls back to the shipped one: tiers are
    advice, and advice must not take a session down. A broken *shipped* table
    is a packaging bug and there is a test asserting it parses.
    """
    target = Path(path).expanduser() if path is not None else tier_table_path()
    packaged = Path(__file__).resolve().parent / "data" / "capability_tiers.json"
    try:
        st = target.stat()
        key = (str(target), st.st_mtime_ns, st.st_size)
        cached = _CACHE.get(key)
        if cached is None:
            cached = load_tier_table_file(target)
            if len(_CACHE) > 8:       # every edit mints a key; don't hoard them
                _CACHE.clear()
            _CACHE[key] = cached
        return cached
    except Exception:  # noqa: BLE001 — unreadable/malformed override, see below
        if target == packaged:
            return _FALLBACK
    try:
        return load_tier_table(packaged)
    except Exception:  # noqa: BLE001
        return _FALLBACK


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def normalize_overrides(
    raw: Mapping[str, Any] | None,
    *,
    table: CapabilityTable | None = None,
) -> dict[str, str]:
    """The usable pins from a raw ``{model: tier}`` mapping, lowercased."""
    tbl = table if table is not None else load_tier_table()
    out: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return out
    for model, tier in raw.items():
        if not isinstance(model, str) or not model.strip():
            continue
        if tbl.has_tier(tier):
            out[model.strip().lower()] = str(tier)
    return out


def override_problems(
    raw: Mapping[str, Any] | None,
    *,
    table: CapabilityTable | None = None,
) -> list[str]:
    """Human-readable reasons each rejected pin was dropped.

    Dropping a pin silently would leave the user believing it took effect —
    the same class of quiet failure as a silently weaker advisor.
    """
    tbl = table if table is not None else load_tier_table()
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        return problems
    known = ", ".join(tbl.tier_names_by_rank())
    for model, tier in raw.items():
        if not isinstance(model, str) or not model.strip():
            problems.append(f"capability override with an empty model name (tier {tier!r})")
        elif not tbl.has_tier(tier):
            problems.append(
                f"capability override {model!r}: {tier!r} is not a tier (expected one of {known})"
            )
    return problems


def user_capability_overrides(cwd: str | Path | None = None) -> dict[str, str]:
    """Per-model tier pins from the **user** settings tier, never the repo's.

    ``project`` and ``local`` settings resolve under ``<cwd>/.mantis-agent/``,
    i.e. files a cloned repository can ship. A repo that could declare its
    preferred model ``frontier`` could turn the escalation check off from
    inside the working tree, so this reads the machine owner's own file only.
    """
    try:
        from .settings import load_setting_source  # noqa: PLC0415

        data = load_setting_source("user", cwd) or {}
        block = data.get("advisor")
        raw = block.get(OVERRIDES_SETTING) if isinstance(block, Mapping) else None
        return normalize_overrides(raw)
    except Exception:  # noqa: BLE001 — a bad settings file must not block launch
        return {}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierResolution:
    """Where a model landed, and — just as importantly — how it got there."""

    model: str
    tier: str
    rank: int
    source: str                  # SOURCE_OVERRIDE | _EXPLICIT | _PATTERN | _DEFAULT
    matched: str = ""            # the exact id or glob that decided it
    known: bool = True           # False ⇒ the tier is the default, i.e. a guess
    confidence: str = "high"     # high | medium | low

    def explain(self) -> str:
        """One line answering "why is this model that tier?".

        This is what ``/advisor tiers <model>`` prints, and it is the only way
        a tier dispute gets settled without reading source.
        """
        head = f"{self.model or '(unset)'} → {self.tier} (rank {self.rank})"
        if self.source == SOURCE_OVERRIDE:
            return f"{head} via user override"
        if self.source == SOURCE_EXPLICIT:
            return f"{head} via explicit table entry {self.matched!r}"
        if self.source == SOURCE_PATTERN:
            return f"{head} via pattern {self.matched!r}"
        return f"{head} via default — unknown model, so this tier is a guess"


def _candidates(model: str) -> list[str]:
    """The spellings to try: the full id, then its last path segment.

    ``openai/gpt-oss-120b``, ``accounts/fireworks/models/deepseek-v3`` and
    ``z-ai/glm-4.7`` are the same weights as the bare ids under a router's
    naming. Stripping the prefix is what keeps the table provider-neutral
    without a per-vendor branch.
    """
    name = (model or "").strip().lower()
    if not name:
        return []
    out = [name]
    tail = name.rsplit("/", 1)[-1]
    if tail and tail != name:
        out.append(tail)
    return out


def _best_pattern(table: CapabilityTable, name: str) -> TierPattern | None:
    """The most specific pattern matching ``name``; ties go to declaration order."""
    best: TierPattern | None = None
    best_key = (-1, 0)
    for index, pat in enumerate(table.patterns):
        if fnmatch.fnmatchcase(name, pat.match):
            key = (pat.specificity, -index)
            if key > best_key:
                best, best_key = pat, key
    return best


def resolve_tier(
    model: str | None,
    *,
    overrides: Mapping[str, Any] | None = None,
    table: CapabilityTable | None = None,
) -> TierResolution:
    """Resolve ``model`` to a tier: override > explicit > pattern > default.

    Never raises. An unrecognized model gets the default tier with
    ``known=False`` so the caller reports low confidence rather than fact.
    """
    tbl = table if table is not None else load_tier_table()
    pins = normalize_overrides(overrides, table=tbl) if overrides else {}
    name = (model or "").strip()

    for candidate in _candidates(name):
        pinned = pins.get(candidate)
        if pinned:
            return TierResolution(name, pinned, tbl.rank(pinned), SOURCE_OVERRIDE,
                                  matched=candidate, confidence=_CONFIDENCE[SOURCE_OVERRIDE])

    for candidate in _candidates(name):
        explicit = tbl.models.get(candidate)
        if explicit:
            return TierResolution(name, explicit, tbl.rank(explicit), SOURCE_EXPLICIT,
                                  matched=candidate, confidence=_CONFIDENCE[SOURCE_EXPLICIT])

    for candidate in _candidates(name):
        pat = _best_pattern(tbl, candidate)
        if pat is not None:
            return TierResolution(name, pat.tier, tbl.rank(pat.tier), SOURCE_PATTERN,
                                  matched=pat.match, confidence=_CONFIDENCE[SOURCE_PATTERN])

    return TierResolution(name, tbl.default, tbl.rank(tbl.default), SOURCE_DEFAULT,
                          known=False, confidence=_CONFIDENCE[SOURCE_DEFAULT])


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierComparison:
    """Advisor versus main model. Advice, never a veto.

    ``relation`` is the reported verdict and degrades to ``UNKNOWN`` the moment
    either side is a guess. ``ordering`` is the raw rank comparison taken at
    face value, kept separate so a low-confidence result can still say *what it
    looks like* — "probably a downgrade" is far more useful than "unknown".
    """

    advisor: TierResolution
    main: TierResolution
    relation: str                # ESCALATION | SAME_TIER | DOWNGRADE | UNKNOWN
    ordering: str                # ESCALATION | SAME_TIER | DOWNGRADE
    confident: bool
    severity: str                # ok | note | warning
    summary: str

    @property
    def is_downgrade(self) -> bool:
        """True when the ranks say the advisor is weaker — confidently or not.

        Callers gate on this rather than on ``relation``, because an unknown
        model that *looks* weaker is exactly the configuration accident this
        module was written to catch.
        """
        return self.ordering == DOWNGRADE

    def needs_confirmation(self, allow_downgrade: bool = False) -> bool:
        """Whether the user should be asked before this pairing is accepted.

        Nothing here blocks: the answer feeds a prompt (or the
        ``allowDowngrade`` policy), not a refusal.
        """
        return self.is_downgrade and not allow_downgrade

    def explain(self) -> str:
        """The full picture for ``/advisor``: verdict plus both routes taken."""
        return "\n".join((
            self.summary,
            f"  advisor  {self.advisor.explain()}",
            f"  main     {self.main.explain()}",
        ))


def _ordering(advisor: TierResolution, main: TierResolution) -> str:
    if advisor.rank > main.rank:
        return ESCALATION
    if advisor.rank < main.rank:
        return DOWNGRADE
    return SAME_TIER


def _summary(advisor: TierResolution, main: TierResolution,
             relation: str, ordering: str) -> str:
    adv = f"advisor {advisor.model or '(unset)'} is {advisor.tier} (rank {advisor.rank})"
    mai = f"main {main.model or '(unset)'} is {main.tier} (rank {main.rank})"
    if relation == ESCALATION:
        return f"escalation: {adv}; {mai}"
    if relation == SAME_TIER:
        return (f"same tier ({advisor.tier}): {adv}; {mai} — a second opinion, "
                "not more capability")
    if relation == DOWNGRADE:
        return (f"DOWNGRADE: {adv}; {mai} — consulting it escalates to something "
                "weaker than the model you are already running")
    guessed = " and ".join(
        side for side, res in (("advisor", advisor), ("main", main)) if not res.known
    )
    tail = {
        DOWNGRADE: " — it looks like a DOWNGRADE",
        ESCALATION: " — it looks like an escalation",
        SAME_TIER: " — the two look comparable",
    }[ordering]
    return (f"low confidence: the {guessed} tier is a guess ({adv}; {mai}){tail}")


def compare(
    advisor_model: str | None,
    main_model: str | None,
    *,
    overrides: Mapping[str, Any] | None = None,
    table: CapabilityTable | None = None,
) -> TierComparison:
    """Compare the advisor against the model it is meant to be stronger than.

    Returns the relation; it never raises and never blocks. A downgrade comes
    back as ``severity="warning"`` with ``DOWNGRADE`` shouted in the summary,
    because silently escalating to a weaker model defeats the entire feature —
    but the user may know something the table does not, so the decision stays
    theirs.
    """
    tbl = table if table is not None else load_tier_table()
    pins = normalize_overrides(overrides, table=tbl) if overrides else {}
    advisor = resolve_tier(advisor_model, overrides=pins, table=tbl)
    main = resolve_tier(main_model, overrides=pins, table=tbl)

    ordering = _ordering(advisor, main)
    confident = advisor.known and main.known
    relation = ordering if confident else UNKNOWN
    # A suspected downgrade still warns. Softening it because one side was a
    # guess would reintroduce the silence this module exists to remove.
    if ordering == DOWNGRADE:
        severity = "warning"
    elif not confident or ordering == SAME_TIER:
        severity = "note"
    else:
        severity = "ok"
    return TierComparison(
        advisor=advisor,
        main=main,
        relation=relation,
        ordering=ordering,
        confident=confident,
        severity=severity,
        summary=_summary(advisor, main, relation, ordering),
    )
