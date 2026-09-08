"""Natural-language model search — "what should I deploy?", answered in groups.

The deploy picker used to take a substring and hand back a flat list sorted by
whatever the Hub felt trendy about. This module takes the sentence a person
actually types — *"best open coding model under 40B, 2025"*, *"cheapest model
that fits 24GB"*, *"strongest reasoning model I can run on an A100"* — and
answers with **labelled groups**, each carrying the one line of reasoning that
justifies it, plus the set of columns that matter for *that* question.

Two tiers, in this order:

1. **Agent tier** (:func:`agent_search`). mantis is an agent SDK, so the
   interpretation is done by an agent running on *the user's own configured
   model* (:func:`configured_model`), with a Hub-search tool and a
   ``response_format`` schema — structured output, never prose parsing. It is
   deliberately cheap: a pre-fetched candidate list in the prompt, ~4 turns,
   a small token cap, and a hard budget on tool calls. Its job is
   *interpretation and grouping*, not retrieval.
2. **Rule tier** (:func:`rules_search`). When no model is configured, when the
   caller passes ``use_agent=False``, or when the agent errors / answers with
   something we cannot verify, the query is parsed by :func:`parse_query` into
   a filter set and grouped by a fixed strategy. This path is good enough to
   ship on its own — it is what the tests pin.

**What this module refuses to claim.** Everything a group says has to be
defensible from Hub metadata (parameter counts per dtype, dominant dtype,
tags, licence, downloads, likes, ``lastModified``, architectures, ``gated``)
plus mantis's own VRAM estimate and the provider's GPU catalogue. There are no
benchmark numbers here, no "code score", no leaderboard rank — mantis does not
run evals, and a fabricated score is worse than no score. When a query asks for
something unverifiable ("best at code"), the grouping falls back to what *is*
checkable — the repo id / Hub tags saying so, download counts, recency — and
the group's ``reason`` says exactly that. The agent tier is held to the same
bar: it may only group ids that came back from the Hub, and its columns are
intersected with :data:`COLUMNS`.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable, Literal

from .base import GpuSpec, ModelInfo

__all__ = [
    "COLUMNS",
    "COLUMN_SOURCES",
    "GPU_VRAM",
    "QueryFilters",
    "SearchGroup",
    "SmartSearchResult",
    "TASKS",
    "agent_search",
    "column_value",
    "configured_model",
    "describe",
    "find_models",
    "choose_columns",
    "gather_candidates",
    "parse_query",
    "rules_search",
]

#: The only column names a result may ask the UI to render. A fixed vocabulary
#: means the dashboard can lay out any answer without guessing, and it is the
#: allow-list the agent tier's output is intersected with.
COLUMNS: tuple[str, ...] = (
    "params", "dtype", "vram", "context", "fit", "price", "license", "downloads", "updated",
)

#: Where each column's value comes from: ``"info.<field>"`` is a
#: :class:`~mantis_agent.deploy.base.ModelInfo` attribute, ``"extra.<key>"`` is
#: a per-model entry of :attr:`SmartSearchResult.extras` (things ModelInfo does
#: not carry: the Hub's ``lastModified``, the fit verdict, the hourly price of
#: the cheapest GPU that fits).
COLUMN_SOURCES: dict[str, str] = {
    "params": "info.params_b",
    "dtype": "info.dtype",
    "vram": "info.est_vram_gb",
    "context": "info.context_len",
    "fit": "extra.fit",
    "price": "extra.price_per_hour",
    "license": "info.license",
    "downloads": "info.downloads",
    "updated": "extra.updated",
}

#: Task words → (id/tag signals, the term we hand the Hub's own search).
#: These are *name and tag* signals. They say a repo calls itself a coding
#: model; they do not say it is good at coding, and nothing here pretends to.
TASKS: dict[str, tuple[tuple[str, ...], str]] = {
    "coding": (
        ("code", "coder", "codestral", "devstral", "starcoder", "swe", "programming", "codegen"),
        "coder",
    ),
    "reasoning": (
        ("reason", "thinking", "think", "r1", "qwq", "math", "proof", "olympiad"),
        "reasoning",
    ),
    "vision": (
        ("vision", "-vl", "vl-", "visual", "image-text", "multimodal", "ocr", "llava", "pixtral"),
        "vision language",
    ),
    "math": (("math", "proof", "olympiad", "numina", "deepseek-math"), "math"),
    "chat": (("chat", "instruct", "it", "assistant"), "instruct"),
    "multilingual": (("multilingual", "translate", "translation", "polyglot"), "multilingual"),
    "embedding": (("embed", "embedding", "retrieval", "rerank", "bge", "gte"), "embedding"),
    "agentic": (("agent", "agentic", "tool", "function-calling", "toolcall"), "agentic"),
}

_TASK_WORDS: dict[str, str] = {
    "code": "coding", "coding": "coding", "coder": "coding", "programming": "coding",
    "program": "coding", "dev": "coding", "developer": "coding", "swe": "coding",
    "software": "coding", "refactor": "coding", "autocomplete": "coding",
    "reasoning": "reasoning", "reason": "reasoning", "thinking": "reasoning",
    "logic": "reasoning", "chain-of-thought": "reasoning",
    "vision": "vision", "visual": "vision", "image": "vision", "images": "vision",
    "multimodal": "vision", "vlm": "vision", "ocr": "vision", "screenshot": "vision",
    "math": "math", "maths": "math", "mathematics": "math", "arithmetic": "math",
    "chat": "chat", "chatbot": "chat", "conversation": "chat", "assistant": "chat",
    "multilingual": "multilingual", "translation": "multilingual", "translate": "multilingual",
    "embedding": "embedding", "embeddings": "embedding", "embed": "embedding",
    "retrieval": "embedding", "rerank": "embedding", "reranker": "embedding",
    "agent": "agentic", "agentic": "agentic", "tool-use": "agentic", "tools": "agentic",
    "function-calling": "agentic",
}

#: GPU name → VRAM in GB, for "…on an A100" style constraints. The number is
#: the card's advertised memory; the fit maths still applies vLLM's 0.9
#: utilisation ceiling on top of it.
GPU_VRAM: dict[str, int] = {
    "b200": 180, "h200": 141, "h100": 80, "a100": 80, "mi300x": 192,
    "l40s": 48, "l40": 48, "a6000": 48, "a40": 48, "v100": 32, "5090": 32,
    "l4": 24, "a10g": 24, "a10": 24, "4090": 24, "3090": 24, "4080": 16,
    "t4": 16, "a4000": 16, "p100": 16,
}

#: Licences we are willing to call permissive/commercial-friendly. Anything
#: else (Llama community, Gemma terms, research-only, unknown) is reported as
#: it is rather than being graded.
PERMISSIVE_LICENSES: tuple[str, ...] = (
    "apache-2.0", "apache2.0", "apache", "mit", "bsd", "bsd-3-clause", "bsd-2-clause",
    "cc-by-4.0", "cc0-1.0", "openrail", "unlicense", "isc", "mpl-2.0",
)

_BRANDS: tuple[str, ...] = (
    "qwen", "llama", "deepseek", "mistral", "mixtral", "gemma", "phi", "glm", "kimi",
    "minimax", "yi", "falcon", "olmo", "granite", "command", "hermes", "nemotron",
    "starcoder", "codestral", "devstral", "gpt-oss", "smollm", "exaone", "internlm",
    "solar", "aya", "molmo", "pixtral", "cohere", "jamba", "dbrx", "arctic", "qwq",
)

_STOPWORDS = frozenset("""
a an and are as at be best better big biggest but by can cheap cheaper cheapest could do
does fast faster fastest find fit fits for from get give good great has have i in into is
it its latest least like list me model models most my need needs new newest of on one only
open or our out params parameters pick please quick recommend run running runs search show
small smaller smallest some strong strongest support supports that the their there these
they this to top under up use using want was we what when where which who will with would
you your
""".split())


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------


@dataclass
class QueryFilters:
    """What :func:`parse_query` understood. Serialised into
    :attr:`SmartSearchResult.filters` so a UI can show it *and* let the user
    edit it — every field here is a knob, not a hidden heuristic."""

    max_params_b: float | None = None
    min_params_b: float | None = None
    about_params_b: float | None = None   # a bare "8B" in the query
    max_vram_gb: float | None = None
    gpu: str | None = None                # the GPU the user named, verbatim
    task: str | None = None               # a key of TASKS
    license: str | None = None            # "apache-2.0", "mit", or "permissive"
    since: int | None = None              # year: only repos updated in/after it
    recency: bool = False                 # "latest" / "newest" — sort, not a cut-off
    exclude_gated: bool = False
    objective: str = "best"               # best | cheapest | smallest | largest | popular | newest
    sort: str = "trending"                # hf_hub.search sort key
    text: str = ""                        # what we hand the Hub's own search box

    def as_dict(self) -> dict[str, Any]:
        """Only the knobs that are actually set — a UI renders this verbatim."""

        out: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if v in (None, "", False):
                continue
            if k == "objective" and v == "best":
                continue
            if k == "sort" and v == "trending":
                continue
            out[k] = v
        return out


@dataclass
class SearchGroup:
    """One labelled section of an answer.

    ``reason`` is one short line and must be defensible from metadata — it is
    where "this is a name/tag signal, not a benchmark" gets said out loud.
    ``best`` is the id of the standout *within this group*, or ``None``.
    """

    title: str
    reason: str
    models: list[ModelInfo] = field(default_factory=list)
    best: str | None = None


@dataclass
class SmartSearchResult:
    """The whole answer: what was asked, what we understood, the groups, the
    columns worth rendering, the filters we derived, which tier answered, and
    every caveat the user deserves to see. JSON-serialisable via
    :meth:`to_dict`."""

    query: str
    interpretation: str
    groups: list[SearchGroup] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    source: Literal["agent", "rules"] = "rules"
    notes: list[str] = field(default_factory=list)
    #: Per-model values for the columns ModelInfo does not carry, keyed by
    #: model id: ``updated`` (the Hub's ``lastModified``, ISO), ``fit`` (the
    #: pre-flight verdict), ``price_per_hour`` and ``gpu`` (the cheapest
    #: provider GPU the model fits on).
    extras: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def models(self) -> list[ModelInfo]:
        """Every model in the answer, in group order, deduped."""

        seen: set[str] = set()
        out: list[ModelInfo] = []
        for g in self.groups:
            for m in g.models:
                if m.id not in seen:
                    seen.add(m.id)
                    out.append(m)
        return out

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON: dataclasses expanded, tuples listed."""

        return _jsonable(self)  # type: ignore[return-value]


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f: _jsonable(v) for f, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# query parsing (the rule tier's front door)
# ---------------------------------------------------------------------------

_MAX_WORDS = r"(?:under|below|less\s+than|smaller\s+than|fewer\s+than|at\s+most|up\s+to|no\s+more\s+than|max(?:imum)?|<=?|≤)"
_MIN_WORDS = r"(?:over|above|more\s+than|bigger\s+than|larger\s+than|at\s+least|no\s+less\s+than|min(?:imum)?|>=?|≥)"
_PARAM = r"(\d+(?:\.\d+)?)\s*(?:b\b|billion)"
_VRAM = r"(\d+(?:\.\d+)?)\s*(?:gb|gib|gigabytes?|gigs?\b|g\s*of\s*vram)"

_RE_MAX_PARAMS = re.compile(rf"{_MAX_WORDS}\s*(?:about\s+|around\s+)?{_PARAM}", re.I)
_RE_MIN_PARAMS = re.compile(rf"{_MIN_WORDS}\s*(?:about\s+|around\s+)?{_PARAM}", re.I)
_RE_PLUS_PARAMS = re.compile(rf"{_PARAM}\s*(?:\+|or\s+(?:more|bigger|larger|above))", re.I)
_RE_BETWEEN = re.compile(rf"between\s+{_PARAM}\s*(?:and|-|–|to)\s*{_PARAM}", re.I)
_RE_PARAM_ANY = re.compile(_PARAM, re.I)
_RE_VRAM = re.compile(_VRAM, re.I)
_RE_YEAR = re.compile(r"\b(20[2-9]\d)\b")
_RE_CONTEXT = re.compile(r"\b(\d+)\s*[km]\s*(?:token|ctx|context)|\bcontext\b|\blong[- ]context\b", re.I)


def parse_query(query: str) -> QueryFilters:
    """Turn a plain-English question into a :class:`QueryFilters`.

    Deterministic and offline. Understands size limits ("under 40B", "70B+",
    "between 7B and 30B"), memory limits ("fits 24GB", "on an A100"), the task
    vocabulary of :data:`TASKS`, recency ("2025", "latest"), licence words
    ("apache", "MIT", "permissive", "commercial"), gating ("ungated") and the
    objective adverbs ("cheapest", "smallest", "most downloaded"). Anything it
    does not recognise becomes search text for the Hub, minus stopwords.
    """

    q = (query or "").strip()
    f = QueryFilters()
    low = q.lower()

    # --- size ---------------------------------------------------------------
    m = _RE_BETWEEN.search(low)
    if m:
        lo, hi = sorted((float(m.group(1)), float(m.group(2))))
        f.min_params_b, f.max_params_b = lo, hi
    else:
        m = _RE_MAX_PARAMS.search(low)
        if m:
            f.max_params_b = float(m.group(1))
        m = _RE_MIN_PARAMS.search(low) or _RE_PLUS_PARAMS.search(low)
        if m:
            f.min_params_b = float(m.group(1))
    if f.max_params_b is None and f.min_params_b is None:
        bare = _RE_PARAM_ANY.search(low)
        if bare:
            f.about_params_b = float(bare.group(1))

    # --- memory / GPU -------------------------------------------------------
    vram = _RE_VRAM.search(low)
    if vram:
        f.max_vram_gb = float(vram.group(1))
    for name, gb in GPU_VRAM.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", low):
            f.gpu = name.upper()
            if f.max_vram_gb is None:
                f.max_vram_gb = float(gb)
            break

    # --- task ---------------------------------------------------------------
    for word in re.findall(r"[a-z][a-z0-9-]*", low):
        task = _TASK_WORDS.get(word)
        if task:
            f.task = task
            break

    # --- licence / gating ---------------------------------------------------
    if "apache" in low:
        f.license = "apache-2.0"
    elif re.search(r"\bmit\b", low):
        f.license = "mit"
    elif re.search(r"permissive|commercial(?:ly)?|for\s+commercial|no\s+licen[cs]e\s+restrictions", low):
        f.license = "permissive"
    if re.search(r"ungated|not\s+gated|no\s+gate|without\s+a?\s*(?:hf\s+)?token|no\s+(?:hf\s+)?token|no\s+licen[cs]e\s+click", low):
        f.exclude_gated = True

    # --- recency ------------------------------------------------------------
    year = _RE_YEAR.search(low)
    if year:
        f.since = int(year.group(1))
    if re.search(r"\blatest\b|\bnewest\b|\bmost recent\b|\brecent\b|\bnew(?:est)?\s+model", low):
        f.recency = True
        f.sort = "updated"

    # --- objective ----------------------------------------------------------
    if re.search(r"cheap(?:est|er)?|budget|afford|lowest\s+cost|least\s+expensive", low):
        f.objective = "cheapest"
    elif re.search(r"smallest|tiniest|lightest|most\s+compact", low):
        f.objective = "smallest"
    elif re.search(r"biggest|largest|most\s+capable|strongest|frontier", low):
        f.objective = "largest"
    elif re.search(r"popular|most\s+downloaded|widely\s+used|everyone", low):
        f.objective = "popular"
        f.sort = "downloads"
    elif f.recency:
        f.objective = "newest"

    # --- what to type into the Hub's own search box --------------------------
    f.text = _search_text(low, f)
    return f


def _search_text(low: str, f: QueryFilters) -> str:
    """Brand names win (they are what the Hub indexes well); otherwise the
    task's own Hub term; otherwise nothing, which gets us the trending list."""

    brands = [b for b in _BRANDS if b in low]
    if brands:
        return " ".join(brands[:2])
    if f.task:
        return TASKS[f.task][1]
    words = [w for w in re.findall(r"[a-z][a-z0-9.-]{2,}", low) if w not in _STOPWORDS]
    return " ".join(words[:2])


def describe(f: QueryFilters) -> str:
    """One line of what we understood — the ``interpretation`` field."""

    bits: list[str] = []
    head = {"coding": "coding models", "reasoning": "reasoning models",
            "vision": "vision-language models", "math": "maths models",
            "chat": "chat/instruct models", "multilingual": "multilingual models",
            "embedding": "embedding models", "agentic": "tool-using models"}
    bits.append(head.get(f.task or "", "open-weight text-generation models"))
    if f.min_params_b and f.max_params_b:
        bits.append(f"between {f.min_params_b:g}B and {f.max_params_b:g}B parameters")
    elif f.max_params_b:
        bits.append(f"under {f.max_params_b:g}B parameters")
    elif f.min_params_b:
        bits.append(f"at least {f.min_params_b:g}B parameters")
    elif f.about_params_b:
        bits.append(f"around {f.about_params_b:g}B parameters")
    if f.max_vram_gb:
        where = f"an {f.gpu}" if f.gpu else f"{f.max_vram_gb:g} GB of VRAM"
        bits.append(f"sized to fit {where}")
    if f.license == "permissive":
        bits.append("under a permissive licence")
    elif f.license:
        bits.append(f"licensed {f.license}")
    if f.exclude_gated:
        bits.append("no gated repos")
    if f.since:
        bits.append(f"updated in {f.since} or later")
    elif f.recency:
        bits.append("most recently updated first")
    if f.objective == "cheapest":
        bits.append("cheapest to serve first")
    elif f.objective == "smallest":
        bits.append("smallest first")
    elif f.objective == "largest":
        bits.append("largest first")
    elif f.objective == "popular":
        bits.append("most downloaded first")
    return ", ".join(bits)


def choose_columns(f: QueryFilters, *, has_provider: bool = False) -> list[str]:
    """Which of :data:`COLUMNS` matter for *this* query, in a stable order."""

    want = {"params", "vram"}
    if f.max_vram_gb or has_provider or f.objective in ("cheapest", "smallest"):
        want.add("dtype")
    if has_provider:
        want.update({"fit", "price"})
    elif f.max_vram_gb:
        want.add("fit")
    if f.license or f.exclude_gated:
        want.add("license")
    if f.since or f.recency:
        want.add("updated")
    if f.objective in ("best", "popular", "largest") or not (f.max_vram_gb or f.license):
        want.add("downloads")
    if f.task in ("agentic", "chat") or f.objective == "largest":
        want.add("context")
    return [c for c in COLUMNS if c in want]


def column_value(column: str, info: ModelInfo, extra: dict[str, Any] | None = None) -> str:
    """One cell, rendered the way both the CLI and the dashboard should show it."""

    extra = extra or {}
    if column == "params":
        return f"{info.params_b:g}B" if info.params_b else "?"
    if column == "dtype":
        return info.dtype or "?"
    if column == "vram":
        return f"{info.est_vram_gb:g} GB" if info.est_vram_gb else "?"
    if column == "context":
        return f"{int(info.context_len / 1024)}k" if info.context_len else "?"
    if column == "fit":
        return str(extra.get("fit") or "?").split(":", 1)[0]
    if column == "price":
        p = extra.get("price_per_hour")
        return f"${p:.2f}/h" if isinstance(p, (int, float)) else "-"
    if column == "license":
        return info.license or "?"
    if column == "downloads":
        return _short_num(info.downloads)
    if column == "updated":
        return str(extra.get("updated") or "")[:10] or "?"
    return ""


def _short_num(n: int | None) -> str:
    if not n:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


async def gather_candidates(
    f: QueryFilters,
    *,
    limit: int = 24,
    hf_token: str | None = None,
) -> tuple[list[ModelInfo], dict[str, dict[str, Any]], list[str]]:
    """Hub rows → ``(models, extras, notes)``, already filtered by ``f``.

    One search (a second only when the task term would look somewhere else),
    then everything else is decided locally from the metadata the Hub already
    returned — no per-model round trips, which is what keeps this fast enough
    to run on every keystroke-completed query.
    """

    from . import hf_hub, preflight  # noqa: PLC0415

    notes: list[str] = []
    want = max(limit * 3, 40)
    rows: list[dict[str, Any]] = list(await hf_hub.search(
        f.text, limit=min(want, 100), sort=f.sort, token=hf_token))
    task_term = TASKS[f.task][1] if f.task else ""
    if task_term and task_term != f.text and len(rows) < limit * 2:
        rows += list(await hf_hub.search(task_term, limit=min(want, 100), sort=f.sort, token=hf_token))

    infos: list[ModelInfo] = []
    extras: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    dropped_size = dropped_engine = dropped_gated = dropped_license = dropped_age = 0
    for row in rows:
        info = preflight.build_model_info(row)
        if not info.id or info.id.lower() in seen:
            continue
        seen.add(info.id.lower())
        params = info.params_b
        if f.max_params_b and params and params > f.max_params_b:
            dropped_size += 1
            continue
        if f.min_params_b and params and params < f.min_params_b:
            dropped_size += 1
            continue
        # A bare "8B" is a target, not a limit — keep a band around it rather
        # than pretending the user asked for exactly 8.0e9 parameters.
        if f.about_params_b and params and not (0.5 * f.about_params_b <= params <= 1.5 * f.about_params_b):
            dropped_size += 1
            continue
        if info.vllm_ok is False:
            dropped_engine += 1
            continue
        if f.exclude_gated and info.gated:
            dropped_gated += 1
            continue
        if f.license and not _license_ok(info.license, f.license):
            dropped_license += 1
            continue
        updated = str(row.get("lastModified") or row.get("last_modified") or "")
        if f.since and updated[:4].isdigit() and int(updated[:4]) < f.since:
            dropped_age += 1
            continue
        infos.append(info)
        extras[info.id] = {"updated": updated, "trending": row.get("trendingScore")}

    if dropped_size:
        notes.append(f"{dropped_size} model(s) outside the size range you asked for were dropped")
    if dropped_engine:
        notes.append(f"{dropped_engine} model(s) vLLM cannot serve (GGUF or a non-generative head) were dropped")
    if dropped_gated:
        notes.append(f"{dropped_gated} gated repo(s) were dropped — they need a Hugging Face token")
    if dropped_license:
        notes.append(f"{dropped_license} model(s) whose licence is not {f.license} were dropped")
    if dropped_age:
        notes.append(f"{dropped_age} model(s) last updated before {f.since} were dropped")
    if f.task == "embedding":
        notes.append("the deploy path serves text-generation repos, so embedding-only models are not in these results")
    return infos, extras, notes


def _license_ok(license_: str | None, want: str) -> bool:
    lic = (license_ or "").strip().lower()
    if not lic:
        return False
    if want == "permissive":
        return any(lic.startswith(p) for p in PERMISSIVE_LICENSES)
    return lic.startswith(want.lower())


async def attach_fit(
    infos: Iterable[ModelInfo],
    extras: dict[str, dict[str, Any]],
    *,
    provider_id: str | None = None,
    max_vram_gb: float | None = None,
    gpu_label: str | None = None,
) -> tuple[list[GpuSpec], list[str]]:
    """Fill ``extras[id]["fit" | "price_per_hour" | "gpu"]``.

    With a ``provider_id`` the real catalogue decides (cheapest card that
    actually fits); without one, a synthetic card of ``max_vram_gb`` stands in
    so "fits 24GB" still gets a verdict. Returns the GPU rows it used.
    """

    from . import preflight  # noqa: PLC0415

    notes: list[str] = []
    cards: list[GpuSpec] = []
    if provider_id:
        from .manager import gpus as _gpus  # noqa: PLC0415
        from .base import DeployError  # noqa: PLC0415

        try:
            cards = list(await _gpus(provider_id))
        except DeployError as e:
            notes.append(f"{provider_id}: GPU catalogue unavailable ({e}) — fit is sized against the model alone")
            cards = []
    if not cards and max_vram_gb:
        cards = [GpuSpec(provider_id=(gpu_label or f"{max_vram_gb:g}GB"), family="other",
                         vram_gb=int(max_vram_gb), label=gpu_label or f"{max_vram_gb:g} GB card")]
    if not cards:
        return [], notes
    for info in infos:
        verdicts = preflight.fit_verdicts(info, cards)
        best = next((gv for gv in verdicts if not gv[1].startswith("no")), None)
        entry = extras.setdefault(info.id, {})
        if best is None:
            entry["fit"] = verdicts[0][1] if verdicts else "no: no GPU catalogue"
            entry["price_per_hour"] = None
            entry["gpu"] = None
            continue
        entry["fit"] = best[1]
        entry["price_per_hour"] = best[0].price_per_hour
        entry["gpu"] = best[0].display
    return cards, notes


# ---------------------------------------------------------------------------
# ranking + grouping (the fixed strategy)
# ---------------------------------------------------------------------------


def _task_signal(info: ModelInfo, task: str) -> bool:
    signals = TASKS.get(task, ((), ""))[0]
    hay = info.id.lower() + " " + " ".join(t.lower() for t in info.tags)
    return any(s in hay for s in signals)


def _updated_year(extra: dict[str, Any] | None) -> int:
    head = str((extra or {}).get("updated") or "")[:4]
    return int(head) if head.isdigit() else 0


def rank(infos: list[ModelInfo], extras: dict[str, dict[str, Any]], f: QueryFilters) -> list[ModelInfo]:
    """Order by an explainable score: adoption (downloads, likes), recency,
    whether the repo *says* it targets the asked-for task, and the objective
    adverb. No benchmark enters this — there is none to enter."""

    def score(info: ModelInfo) -> tuple[float, str]:
        extra = extras.get(info.id, {})
        s = math.log10((info.downloads or 0) + 1)
        s += 0.5 * math.log10((info.likes or 0) + 1)
        year = _updated_year(extra)
        if year:
            s += max(0.0, year - 2023) * 0.6
        if f.task and _task_signal(info, f.task):
            s += 1.5
        if info.gated:
            s -= 0.5
        if f.objective == "cheapest":
            price = extra.get("price_per_hour")
            s -= 0.02 * (info.est_vram_gb or 0)
            if isinstance(price, (int, float)):
                s -= float(price)
        elif f.objective == "smallest":
            s -= 0.05 * (info.params_b or 0)
        elif f.objective == "largest":
            s += 0.02 * (info.params_b or 0)
        return (-s, info.id)

    return sorted(infos, key=score)


def group_models(
    infos: list[ModelInfo],
    extras: dict[str, dict[str, Any]],
    f: QueryFilters,
    *,
    limit: int = 24,
    gpu_label: str | None = None,
) -> list[SearchGroup]:
    """The fixed grouping strategy. Hardware first when hardware was asked
    about, then the task signal, then size bands. Gated repos always trail in
    their own section so nobody picks one without seeing the token cost."""

    ordered = rank(infos, extras, f)
    gated = [m for m in ordered if m.gated]
    main = [m for m in ordered if not m.gated]
    groups: list[SearchGroup] = []
    where = gpu_label or f.gpu or (f"{f.max_vram_gb:g} GB" if f.max_vram_gb else "your GPU")

    has_fit = any("fit" in extras.get(m.id, {}) for m in main)
    if has_fit:
        fits = [m for m in main if str(extras.get(m.id, {}).get("fit", "")).startswith("fits")]
        tight = [m for m in main if str(extras.get(m.id, {}).get("fit", "")).startswith("tight")]
        rest = [m for m in main if m not in fits and m not in tight]
        groups.append(SearchGroup(
            title=f"Fits {where} comfortably",
            reason=("estimated weights + KV cache stay under 90 % of the card — "
                    "mantis's own sizing, not a provider claim"),
            models=fits))
        groups.append(SearchGroup(
            title=f"Tight on {where}",
            reason="fits only with under 15 % headroom — shorten --max-model-len or take a bigger card",
            models=tight))
        groups.append(SearchGroup(
            title="Too big for that card",
            reason="listed so the option is visible: these need more VRAM, another card or a quantised checkpoint",
            models=rest, best=""))
    elif f.task:
        named = [m for m in main if _task_signal(m, f.task)]
        others = [m for m in main if m not in named]
        groups.append(SearchGroup(
            title=f"Repos that say they are {f.task} models",
            reason=(f"the repo id or its Hub tags name {f.task} — a name/tag signal, not a benchmark; "
                    "mantis does not run evals"),
            models=named))
        groups.append(SearchGroup(
            title="General models people actually run",
            reason=f"no {f.task} signal in the metadata, ranked by Hub downloads, likes and recency",
            models=others))
    else:
        bands = ((0.0, 10.0, "Small — one 24 GB card"), (10.0, 40.0, "Mid-size — one 48–80 GB card"),
                 (40.0, 1e9, "Large — multi-GPU or a quantised checkpoint"))
        for lo, hi, title in bands:
            picked = [m for m in main if m.params_b is not None and lo <= m.params_b < hi]
            groups.append(SearchGroup(
                title=title,
                reason="grouped by parameter count from the Hub's safetensors index, ranked by downloads and recency",
                models=picked))
        unknown = [m for m in main if m.params_b is None]
        groups.append(SearchGroup(
            title="Size not published",
            reason="the repo has no safetensors parameter index — `deploy inspect` before spending on a GPU",
            models=unknown, best=""))

    if gated:
        groups.append(SearchGroup(
            title="Gated — needs a Hugging Face token",
            reason="the licence has to be accepted on the Hub and HF_TOKEN set before any provider can pull the weights",
            models=gated, best=""))
    return _finish(groups, limit)


def _finish(groups: list[SearchGroup], limit: int) -> list[SearchGroup]:
    """Drop empty sections, cap the total, and make sure every surviving group
    names a ``best`` that is actually still in it. A group built with
    ``best=""`` is saying "nothing here is a recommendation" — the caveat
    sections do that, because crowning the least-bad model that does not fit
    would be exactly the kind of unearned claim this module refuses to make."""

    out: list[SearchGroup] = []
    used = 0
    for g in groups:
        if not g.models or used >= limit:
            continue
        room = limit - used
        g.models = g.models[:room]
        used += len(g.models)
        if g.best == "":
            g.best = None
        elif g.best is None or g.best not in {m.id for m in g.models}:
            g.best = g.models[0].id
        out.append(g)
    return out


# ---------------------------------------------------------------------------
# tier 2 — rules
# ---------------------------------------------------------------------------


async def rules_search(
    query: str,
    *,
    provider_id: str | None = None,
    limit: int = 24,
    hf_token: str | None = None,
    notes: list[str] | None = None,
    progress: Any = None,
) -> SmartSearchResult:
    """The deterministic tier: parse, filter, fit, group. No model involved."""

    from .manager import _say  # noqa: PLC0415

    f = parse_query(query)
    await _say(progress, f"Understood: {describe(f)}")
    infos, extras, gathered = await gather_candidates(f, limit=limit, hf_token=hf_token)
    all_notes = list(notes or []) + gathered
    _cards, fit_notes = await attach_fit(
        infos, extras, provider_id=provider_id, max_vram_gb=f.max_vram_gb, gpu_label=f.gpu)
    all_notes += fit_notes
    gpu_label = None
    if provider_id:
        gpu_label = f"{provider_id}'s GPUs"
    groups = group_models(infos, extras, f, limit=limit, gpu_label=gpu_label)
    if not groups:
        all_notes.append("nothing on the Hub matched those filters — loosen the size or licence constraint")
    return SmartSearchResult(
        query=query, interpretation=describe(f), groups=groups,
        columns=choose_columns(f, has_provider=bool(provider_id)),
        filters=f.as_dict(), source="rules", notes=all_notes,
        extras={m.id: extras.get(m.id, {}) for g in groups for m in g.models},
    )


# ---------------------------------------------------------------------------
# tier 1 — the agent
# ---------------------------------------------------------------------------


@dataclass
class _AgentGroup:
    title: str
    reason: str
    model_ids: list[str]
    best: str


@dataclass
class _AgentPlan:
    """The structured answer the agent must produce — no prose is ever parsed."""

    interpretation: str
    columns: list[str]
    groups: list[_AgentGroup]
    max_params_b: float
    min_params_b: float
    max_vram_gb: float
    notes: list[str]


_AGENT_SYSTEM = """\
You are the model-picker inside mantis, an SDK that deploys open-weight models on
the user's own GPU cloud. The user asked a question in plain English. You are given
the Hugging Face candidates that mantis already fetched, with the ONLY facts anyone
can verify: parameter count, dominant dtype, licence, gated flag, downloads, likes,
last-updated date, context length and mantis's own VRAM estimate (weights + KV cache).

Your job is interpretation and grouping, not retrieval and not evaluation.

Rules you must not break:
* Group ONLY ids from the candidate list (or ids returned by the search_hub tool).
  Never invent an id, a parameter count, a score or a benchmark result.
* mantis runs no evals. If the question asks for "best at X", group by what the
  metadata can defend — the repo id or tags naming X, adoption (downloads/likes),
  recency, size, licence — and say so in that group's reason.
* Every group needs a title and ONE short reason line that a sceptical engineer
  would accept from this metadata alone.
* 2 to 4 groups, most useful first, each with its strongest id in "best".
* "columns" must be a subset of: params, dtype, vram, context, fit, price,
  license, downloads, updated — pick the 3-6 that matter for THIS question.
* Numeric filters you inferred go in max_params_b / min_params_b / max_vram_gb
  (0 means "not constrained"). "notes" is for caveats the user should see.

Answer with the JSON object only.
"""


def configured_model() -> tuple[str, str | None] | None:
    """``(model, backend)`` mantis is currently connected to, or ``None``.

    The agent tier only runs on a model the user has *already* set up — the
    live process env first (that is what ``deploy connect`` exports), then the
    last model the terminal used, and only when that model's family has a
    configured auth method. Nothing here reaches the network.
    """

    import os  # noqa: PLC0415

    model = (os.environ.get("MANTIS_AGENT_MODEL") or "").strip()
    if model:
        return model, (os.environ.get("MANTIS_AGENT_BASE_URL") or "").strip() or None
    try:
        from .. import catalog  # noqa: PLC0415

        rec = catalog.get_last_model()
    except Exception:  # noqa: BLE001 — a broken store just means "no agent tier"
        return None
    if not rec or not rec.get("model"):
        return None
    model = str(rec["model"])
    backend = rec.get("backend") or None
    if backend:
        return model, str(backend)
    try:
        from .. import auth_methods  # noqa: PLC0415

        family = auth_methods.family_of_model(model)
        if auth_methods.configured_method(family):
            return model, None
    except Exception:  # noqa: BLE001
        return None
    return None


def _candidate_lines(infos: list[ModelInfo], extras: dict[str, dict[str, Any]]) -> str:
    out: list[str] = []
    for info in infos:
        e = extras.get(info.id, {})
        bits = [info.id]
        bits.append(f"{info.params_b:g}B" if info.params_b else "?B")
        bits.append(info.dtype or "?")
        bits.append(f"~{info.est_vram_gb:g}GB" if info.est_vram_gb else "~?")
        bits.append(info.license or "licence?")
        bits.append(f"dl={info.downloads or 0}")
        bits.append(f"likes={info.likes or 0}")
        bits.append(f"updated={str(e.get('updated') or '?')[:10]}")
        if info.context_len:
            bits.append(f"ctx={info.context_len}")
        if info.gated:
            bits.append("GATED")
        if e.get("fit"):
            bits.append(f"fit={str(e['fit']).split(':', 1)[0]}")
        if isinstance(e.get("price_per_hour"), (int, float)):
            bits.append(f"${e['price_per_hour']:.2f}/h")
        tags = [t for t in info.tags if ":" not in t][:5]
        if tags:
            bits.append("tags=" + ",".join(tags))
        out.append(" | ".join(bits))
    return "\n".join(out)


def _make_agent(model: str, backend: str | None, tools: list, response_format: dict[str, Any]):
    """Build the picker agent. A seam: tests swap this for a MockProvider."""

    from .. import Agent  # noqa: PLC0415

    return Agent(
        model=model, backend=backend, tools=tools, system=_AGENT_SYSTEM,
        response_format=response_format, max_steps=4, max_tokens=2000,
        temperature=0.0, persist=False, auto_compact=False,
        include_memory=False, include_env=False, include_recall=False,
    )


def _final_text(messages: list) -> str:
    from ..types import AssistantMessage, TextBlock  # noqa: PLC0415

    for msg in reversed(messages):
        if isinstance(msg, AssistantMessage):
            text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            if text.strip():
                return text
    return ""


async def agent_search(
    query: str,
    *,
    provider_id: str | None = None,
    limit: int = 24,
    hf_token: str | None = None,
    progress: Any = None,
) -> SmartSearchResult:
    """The agent tier. Raises :class:`RuntimeError` when it cannot produce an
    answer we are willing to stand behind — the caller falls back to rules."""

    from ..response_model import build_response_format, parse_response  # noqa: PLC0415
    from ..tools import tool  # noqa: PLC0415
    from .manager import _say  # noqa: PLC0415

    connected = configured_model()
    if not connected:
        raise RuntimeError("no model is configured")
    model, backend = connected

    f = parse_query(query)
    infos, extras, notes = await gather_candidates(f, limit=limit, hf_token=hf_token)
    _cards, fit_notes = await attach_fit(
        infos, extras, provider_id=provider_id, max_vram_gb=f.max_vram_gb, gpu_label=f.gpu)
    notes += fit_notes
    known: dict[str, ModelInfo] = {i.id: i for i in infos}
    if not known:
        raise RuntimeError("the Hub returned no candidates to group")

    budget = {"calls": 0}

    @tool
    async def search_hub(query: str, limit: int = 12) -> str:
        """Search the Hugging Face Hub for more text-generation repos. Use it at
        most twice, only when the candidate list is missing something obvious."""

        from . import hf_hub, preflight  # noqa: PLC0415

        budget["calls"] += 1
        if budget["calls"] > 2:
            return "search budget exhausted — group the candidates you already have"
        rows = await hf_hub.search(str(query or "")[:80], limit=max(1, min(int(limit), 20)), token=hf_token)
        found: list[ModelInfo] = []
        for row in rows:
            info = preflight.build_model_info(row)
            if info.id and info.id not in known:
                known[info.id] = info
                extras.setdefault(info.id, {"updated": str(row.get("lastModified") or "")})
                found.append(info)
        return _candidate_lines(found, extras) or "no new models"

    await _say(progress, f"Asking {model} to interpret and group {len(infos)} candidates…")
    agent = _make_agent(model, backend, [search_hub], build_response_format(_AgentPlan))
    from ..types import UserMessage  # noqa: PLC0415

    prompt = (
        f"Question: {query}\n\n"
        f"mantis parsed it as: {describe(f)}\n"
        f"Deploy target: {provider_id or 'no provider chosen yet'}\n\n"
        f"Candidates ({len(infos)}), one per line:\n{_candidate_lines(infos, extras)}\n"
    )
    messages = await agent.run([UserMessage(content=prompt)])
    plan = parse_response(_AgentPlan, _final_text(messages))

    groups: list[SearchGroup] = []
    unknown_ids = 0
    used: set[str] = set()
    for g in plan.groups or []:
        picked: list[ModelInfo] = []
        for mid in g.model_ids or []:
            info = known.get(str(mid))
            if info is None:
                unknown_ids += 1
                continue
            if info.id in used:
                continue
            used.add(info.id)
            picked.append(info)
        if not picked:
            continue
        best = str(g.best or "")
        groups.append(SearchGroup(
            title=str(g.title or "Models")[:80],
            reason=str(g.reason or "")[:200],
            models=picked,
            best=best if best in {m.id for m in picked} else picked[0].id,
        ))
    if not groups:
        raise RuntimeError("the model grouped no ids we could verify")

    groups = _finish(groups, limit)
    columns = [c for c in COLUMNS if c in {str(c2) for c2 in (plan.columns or [])}]
    if not columns:
        columns = choose_columns(f, has_provider=bool(provider_id))
    filters = f.as_dict()
    for key, val in (("max_params_b", plan.max_params_b), ("min_params_b", plan.min_params_b),
                     ("max_vram_gb", plan.max_vram_gb)):
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num > 0:
            filters[key] = num
    if unknown_ids:
        notes.append(f"{unknown_ids} id(s) the model named are not in the Hub results and were dropped")
    notes += [str(n)[:200] for n in (plan.notes or [])][:3]
    return SmartSearchResult(
        query=query, interpretation=str(plan.interpretation or describe(f))[:300],
        groups=groups, columns=columns, filters=filters, source="agent", notes=notes,
        extras={m.id: extras.get(m.id, {}) for g in groups for m in g.models},
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


async def find_models(
    query: str,
    *,
    provider_id: str | None = None,
    limit: int = 24,
    hf_token: str | None = None,
    use_agent: bool = True,
    progress: Any = None,
) -> SmartSearchResult:
    """Agent tier first, rule tier always. See :func:`agent_search` /
    :func:`rules_search`; :func:`~mantis_agent.deploy.manager.find_models` is
    the public wrapper."""

    from .manager import _say  # noqa: PLC0415

    limit = max(1, min(int(limit), 100))
    notes: list[str] = []
    if use_agent:
        try:
            return await agent_search(query, provider_id=provider_id, limit=limit,
                                      hf_token=hf_token, progress=progress)
        except Exception as e:  # noqa: BLE001 — every agent failure degrades to rules
            why = str(e) or type(e).__name__
            notes.append(f"answered by mantis's rule parser, not an agent: {why}")
            await _say(progress, f"Agent tier unavailable ({why}); using the rule parser")
    else:
        notes.append("agent tier disabled by the caller — answered by the rule parser")
    return await rules_search(query, provider_id=provider_id, limit=limit,
                              hf_token=hf_token, notes=notes, progress=progress)
