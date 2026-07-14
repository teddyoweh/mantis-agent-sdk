"""Token + USD budget tracker.

The single accountant inside the agent. Every assistant turn finalizes by
calling :py:meth:`BudgetTracker.add_usage` with that turn's :class:`Usage`
and the model id, then :py:meth:`BudgetTracker.add_turn` to bump the turn
counter, then :py:meth:`BudgetTracker.check` to raise
:class:`BudgetExceededError` if any configured limit is now breached.

Pricing
-------
``PRICING_TABLE`` is keyed by ``(provider_hint, canonical_model_id)``.
Numbers are USD per **million** tokens, matching the unit every provider
publishes on their pricing pages. A ``Pricing`` of all-zero means
"compute is free at point of inference" — that's the right answer for
self-hosted servers (vLLM, llama.cpp, Ollama) where token usage costs
hardware-time, not API dollars.

We bucket per-provider because the same OSS model is priced very
differently across Together / Fireworks / Groq / DeepInfra. For
``openrouter``, where the price depends on the underlying upstream the
router picked at request time, we store sentinel zeros with a TODO — a
caller that wants accurate USD on OpenRouter should plug in a custom
``Pricing`` via :func:`lookup_pricing`'s output and override.

Cache pricing is optional — Anthropic-style cache reads/writes are
recorded on :class:`Usage` but the OSS world doesn't yet have a settled
discount schedule, so most entries leave ``cache_*_per_million`` ``None``
and we treat cache tokens as full-price prompt tokens for now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from .errors import BudgetExceededError
from .types import Usage

# ---------------------------------------------------------------------------
# Budget configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Budget:
    """Caps the agent is asked to respect.

    Any ``None`` field is "no limit". ``fallback_model`` is the cheaper
    model the loop should swap to *before* the budget is fully exhausted
    (e.g. for the compactor pass). Setting it here keeps that policy
    co-located with the limits it backs off from.
    """

    max_turns: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_usd: float | None = None
    fallback_model: str | None = None

    # Spend FLOORS (optional). Distinct from the ``max_*`` ceilings above: a
    # target is a "do at least this much work" goal the persistence loop uses
    # to decide whether to keep going after a natural stop. Where a ceiling
    # BRAKES (raise BudgetExceededError when crossed), a target MOTIVATES
    # (keep working while it's unmet). ``None`` = no floor. A target should be
    # set well below the matching ceiling; the ceiling always wins.
    target_usd: float | None = None
    target_total_tokens: int | None = None


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pricing:
    """Per-million-token USD pricing for one (provider, model) pair.

    ``cache_read_per_million`` / ``cache_write_per_million`` are optional —
    if ``None``, cache tokens are billed at the regular ``prompt_per_million``
    rate. We default to that conservative behaviour rather than free-tier-ing
    cache tokens, because not every provider exposes a cache discount.
    """

    prompt_per_million: float
    completion_per_million: float
    cache_read_per_million: float | None = None
    cache_write_per_million: float | None = None

    def cost(self, usage: Usage) -> float:
        """Compute USD for a single Usage record."""

        prompt = usage.input_tokens / 1_000_000.0
        completion = usage.output_tokens / 1_000_000.0
        total = prompt * self.prompt_per_million + completion * self.completion_per_million

        # Cache tokens are a *subset* of input_tokens (the providers this SDK
        # targets fold prompt_tokens_details.cached_tokens into prompt_tokens,
        # which _decode_usage maps to input_tokens). They're therefore already
        # billed at prompt_per_million above. When a distinct cache rate is
        # configured we apply only the *adjustment* — the difference between
        # the cache rate and the prompt rate already charged — so a discounted
        # cache rate reduces the total and a premium write rate adds to it.
        # When no cache rate is set we add nothing: billing at the prompt rate
        # is already correct.
        if usage.cache_read_input_tokens and self.cache_read_per_million is not None:
            adjustment = self.cache_read_per_million - self.prompt_per_million
            total += (usage.cache_read_input_tokens / 1_000_000.0) * adjustment
        if usage.cache_creation_input_tokens and self.cache_write_per_million is not None:
            adjustment = self.cache_write_per_million - self.prompt_per_million
            total += (usage.cache_creation_input_tokens / 1_000_000.0) * adjustment
        return total


# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------
#
# Keys are (provider_hint, canonical_model_id) with the model id normalized
# the same way capabilities.py does — lowercase, hyphenated, no provider
# prefix. ``provider_hint`` matches ``BackendCapability.provider_hint``
# so adapters can pass that through directly.
#
# Numbers are USD per million tokens. All May 2026 published rates.

_FREE = Pricing(prompt_per_million=0.0, completion_per_million=0.0)


PRICING_TABLE: Final[dict[tuple[str, str], Pricing]] = {
    # ------------------------------------------------------------------
    # DeepSeek (official API)
    # ------------------------------------------------------------------
    ("deepseek", "deepseek-v3"): Pricing(
        prompt_per_million=0.27,
        completion_per_million=1.10,
    ),
    ("deepseek", "deepseek-r1"): Pricing(
        prompt_per_million=0.55,
        completion_per_million=2.19,
    ),
    # ------------------------------------------------------------------
    # Together AI — canonical OSS routing prices (approximate May 2026)
    # ------------------------------------------------------------------
    ("together", "llama-3.3-70b-instruct"): Pricing(0.88, 0.88),
    ("together", "llama-3.1-70b-instruct"): Pricing(0.88, 0.88),
    ("together", "llama-3.1-405b-instruct"): Pricing(3.50, 3.50),
    ("together", "qwen2.5-72b-instruct"): Pricing(1.20, 1.20),
    ("together", "deepseek-v3"): Pricing(1.25, 1.25),
    ("together", "mistral-large-2"): Pricing(3.00, 9.00),
    # ------------------------------------------------------------------
    # Fireworks — flat-rate pricing across many OSS models
    # ------------------------------------------------------------------
    ("fireworks", "qwen2.5-72b-instruct"): Pricing(0.90, 0.90),
    ("fireworks", "llama-3.3-70b-instruct"): Pricing(0.90, 0.90),
    ("fireworks", "llama-3.1-70b-instruct"): Pricing(0.90, 0.90),
    ("fireworks", "deepseek-v3"): Pricing(0.90, 0.90),
    # ------------------------------------------------------------------
    # Groq — extremely low input price, slightly higher completion
    # ------------------------------------------------------------------
    ("groq", "llama-3.3-70b-instruct"): Pricing(0.59, 0.79),
    ("groq", "llama-3.1-70b-instruct"): Pricing(0.59, 0.79),
    ("groq", "mixtral-8x7b-instruct"): Pricing(0.24, 0.24),
    # ------------------------------------------------------------------
    # OpenRouter — varies per upstream model + dynamic routing. Stored
    # as sentinel zeros so the agent doesn't crash; callers wanting
    # accurate USD should fetch /api/v1/models at request time.
    # ------------------------------------------------------------------
    # TODO: look up at request time via OpenRouter /api/v1/models
    ("openrouter", "*"): Pricing(0.0, 0.0),
    # ------------------------------------------------------------------
    # Self-hosted — compute is paid elsewhere
    # ------------------------------------------------------------------
    ("ollama", "*"): _FREE,
    ("vllm", "*"): _FREE,
    ("llamacpp", "*"): _FREE,
    ("tgi", "*"): _FREE,
    ("modal", "*"): _FREE,
    ("mock", "*"): _FREE,
}


def _normalize_model(model_id: str) -> str:
    """Same normalization as capabilities._normalize, duplicated locally
    so this module has no cross-imports beyond errors/types."""

    key = model_id.strip().lower().replace("_", "-")
    if "/" in key:
        key = key.rsplit("/", 1)[-1]
    return key


def lookup_pricing(model_id: str, backend_hint: str | None = None) -> Pricing | None:
    """Resolve a (model_id, backend_hint) pair to a ``Pricing`` entry.

    Resolution order:
      1. Exact (provider, model) key.
      2. Provider wildcard ``(provider, "*")``.
      3. ``None`` — the caller can choose to fall back to "free" or to
         skip USD tracking entirely.
    """

    model = _normalize_model(model_id)
    hint = (backend_hint or "").strip().lower()

    if hint:
        # Exact (provider, model)
        hit = PRICING_TABLE.get((hint, model))
        if hit is not None:
            return hit
        # Provider wildcard — covers OpenRouter / self-hosted families
        wild = PRICING_TABLE.get((hint, "*"))
        if wild is not None:
            return wild

    # Last-ditch: scan every provider for an exact model match. Useful when
    # the caller doesn't know which provider hosted the request (rare).
    for (_provider, m), pricing in PRICING_TABLE.items():
        if m == model:
            return pricing

    return None


def estimate_cost(
    usage: Usage, model_id: str, backend_hint: str | None = None
) -> float | None:
    """USD for a single ``Usage`` on ``model_id``, or ``None`` when the model has
    no known pricing (local / free models). Thin wrapper over ``lookup_pricing``
    + ``Pricing.cost`` for callers (e.g. the TUI cost readout) that don't run a
    full ``BudgetTracker``."""
    pricing = lookup_pricing(model_id, backend_hint)
    return pricing.cost(usage) if pricing is not None else None


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BudgetTracker:
    """Mutable running totals for one agent run.

    The agent loop holds a single tracker for the lifetime of a session and
    calls ``add_usage`` after every assistant turn finalizes. Limits are
    checked separately (``check()``) so callers can choose *when* to
    enforce — typically after a turn completes, so an in-flight tool call
    isn't stranded mid-execution.
    """

    budget: Budget
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_usd: float = 0.0
    # Per-model breakdown for observability (and so the fallback_model
    # swap can be triggered when the *primary* model is hot, even if a
    # cheaper model has been doing some of the work).
    by_model_usd: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def add_usage(
        self,
        usage: Usage,
        model: str,
        *,
        pricing: Pricing | None = None,
        backend_hint: str | None = None,
    ) -> None:
        """Accumulate a single turn's usage.

        ``pricing`` is optional — if not provided, we look it up via
        ``lookup_pricing(model, backend_hint)``. Missing pricing means
        USD stays flat for that turn (token caps still apply).
        """

        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_input_tokens
        self.cache_creation_tokens += usage.cache_creation_input_tokens

        if pricing is None:
            pricing = lookup_pricing(model, backend_hint)
        if pricing is not None:
            cost = pricing.cost(usage)
            self.total_usd += cost
            self.by_model_usd[model] = self.by_model_usd.get(model, 0.0) + cost

    def add_turn(self) -> None:
        """Bump the turn counter. Called once per assistant message."""

        self.turns += 1

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @property
    def total_tokens(self) -> int:
        """Input + output (cache tokens already counted in input by most providers)."""

        return self.input_tokens + self.output_tokens

    def runway(self) -> float | None:
        """Fraction of the tightest configured ceiling still available, in
        ``[0.0, 1.0]`` — ``1.0`` fresh, ``0.0`` exhausted. ``None`` when no
        ceiling is set (unbounded runway).

        Two-sided by design: the persistence loop reads it both to BRAKE (as it
        approaches ``0.0``, wrap up before ``check()`` raises) and to MOTIVATE
        (while it stays well above ``0.0``, keep pushing on unfinished work).
        Computed as ``1 - max(used_ratio)`` over the same limit set
        ``should_use_fallback`` watches, so the signal a run brakes on and the
        one it motivates on agree.
        """

        b = self.budget
        ratios: list[float] = []
        if b.max_turns is not None and b.max_turns > 0:
            ratios.append(self.turns / b.max_turns)
        if b.max_input_tokens is not None and b.max_input_tokens > 0:
            ratios.append(self.input_tokens / b.max_input_tokens)
        if b.max_output_tokens is not None and b.max_output_tokens > 0:
            ratios.append(self.output_tokens / b.max_output_tokens)
        if b.max_total_tokens is not None and b.max_total_tokens > 0:
            ratios.append(self.total_tokens / b.max_total_tokens)
        if b.max_usd is not None and b.max_usd > 0:
            ratios.append(self.total_usd / b.max_usd)
        if not ratios:
            return None
        remaining = 1.0 - max(ratios)
        if remaining < 0.0:
            return 0.0
        if remaining > 1.0:
            return 1.0
        return remaining

    def target_unmet(self) -> bool:
        """True when the caller set a spend FLOOR (``Budget.target_usd`` /
        ``Budget.target_total_tokens``) that has not yet been reached — the
        "keep working, there's a stated goal" signal persist mode honors. No
        target set → always ``False`` (nothing to chase)."""

        b = self.budget
        if b.target_usd is not None and self.total_usd < b.target_usd:
            return True
        if b.target_total_tokens is not None and self.total_tokens < b.target_total_tokens:
            return True
        return False

    def should_use_fallback(self, headroom: float = 0.85) -> bool:
        """Heuristic: are we within ``headroom`` of any limit?

        Used by the loop to decide whether the *next* turn should swap to
        ``Budget.fallback_model``. Default headroom is 85% of the cap.
        """

        b = self.budget
        if b.max_turns is not None and self.turns >= b.max_turns * headroom:
            return True
        if b.max_input_tokens is not None and self.input_tokens >= b.max_input_tokens * headroom:
            return True
        if b.max_output_tokens is not None and self.output_tokens >= b.max_output_tokens * headroom:
            return True
        if b.max_total_tokens is not None and self.total_tokens >= b.max_total_tokens * headroom:
            return True
        if b.max_usd is not None and self.total_usd >= b.max_usd * headroom:
            return True
        return False

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------

    def check(self) -> None:
        """Raise ``BudgetExceededError`` if any limit has been breached.

        Called after each turn finalizes, never mid-turn — the agent loop
        guarantees the in-flight tool call (if any) has produced its result
        before this is called.
        """

        # A resource *spend* (tokens / USD) STRICTLY over the cap is a breach —
        # spending exactly up to the cap is allowed (the cap is inclusive).
        # Using ``>=`` for those would be an off-by-one that rejects the
        # exact-limit case. ``max_turns`` is different: it's a completed-turn
        # counter, so reaching the cap must stop the *next* turn — allowing the
        # exact value would let one extra turn run and overshoot. It stays ``>=``.
        b = self.budget
        if b.max_turns is not None and self.turns >= b.max_turns:
            raise BudgetExceededError("turns", b.max_turns, self.turns)
        if b.max_input_tokens is not None and self.input_tokens > b.max_input_tokens:
            raise BudgetExceededError("input_tokens", b.max_input_tokens, self.input_tokens)
        if b.max_output_tokens is not None and self.output_tokens > b.max_output_tokens:
            raise BudgetExceededError("output_tokens", b.max_output_tokens, self.output_tokens)
        if b.max_total_tokens is not None and self.total_tokens > b.max_total_tokens:
            raise BudgetExceededError("total_tokens", b.max_total_tokens, self.total_tokens)
        if b.max_usd is not None and self.total_usd > b.max_usd:
            raise BudgetExceededError("usd", b.max_usd, self.total_usd)


__all__ = [
    "PRICING_TABLE",
    "estimate_cost",
    "Budget",
    "BudgetTracker",
    "Pricing",
    "lookup_pricing",
]
