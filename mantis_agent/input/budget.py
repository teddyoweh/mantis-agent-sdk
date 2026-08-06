"""The per-turn aggregate budget — the limit that does not exist today.

`clipboard.py` caps one image at 10 MB and one inlined text file at 256 KB, and
both caps are correct. Neither of them stops five 10 MB images going into a
single turn, because nothing in the codebase has ever held all the attachments
of a turn at once to add them up. That is the gap this module closes, and it is
the one worth closing first: it needs no new input source to be valuable, and
the failure it prevents (a 50 MB request that is rejected by the provider, or
worse, accepted and billed) is the one users hit by accident.

Two objects. :class:`AttachmentBudget` is the configuration from §8 — six
numbers, four of them aggregate and two of them the per-item caps that already
existed. :class:`TurnBudget` is the running ledger for one turn: what is staged,
what it weighs, and whether one more thing fits.

The interesting decision is what "does not fit" means. :meth:`TurnBudget.admit`
answers without side effects and without raising — it returns an
:class:`Admission` naming every limit that would break *and a list of concrete
choices* (drop this specific attachment, truncate that one, attach it by
reference instead). §8 asks for exactly that: "exceeding a limit offers choices
— remove something, truncate, or attach by reference — rather than failing
outright." :meth:`TurnBudget.add` is the convenience wrapper that commits, and
when it cannot it raises an error *carrying the same admission*, so even the
caller who did not think about choices can still present them.

Estimates are approximate and are always rendered as such. Text is the standard
four-characters-per-token heuristic. Images are priced from their pixel
dimensions rather than their byte size — a 4 MB photo and a 40 KB screenshot of
the same dimensions cost the same in context — using the published
``(width × height) / 750`` relation, after the downscale every provider applies
to oversized images. When dimensions cannot be read we assume a full-size image
rather than a small one; a budget that under-estimates is not a budget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterator, List, Mapping, Optional, Tuple

from .attachment import (
    Attachment,
    AttachmentBudgetExceededError,
    AttachmentUnsupportedError,
    human_bytes,
    image_dimensions,
    is_image_media_type,
)

__all__ = [
    "Admission",
    "AttachmentBudget",
    "Choice",
    "DEFAULT_BUDGET",
    "TurnBudget",
    "Violation",
    "estimate_image_tokens",
    "estimate_text_tokens",
    "estimate_tokens",
]


# ---------------------------------------------------------------------------
# Configuration (plan §8 / §10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentBudget:
    """Per-turn limits.

    ``max_image_bytes`` and ``max_text_bytes`` are deliberately the same numbers
    as ``clipboard._MAX_IMAGE_BYTES`` and ``clipboard._MAX_TEXT_BYTES``: this is
    a centralization, not a policy change, and today's behaviour has to survive
    it unchanged. The four aggregate limits above them are the new part.
    """

    max_attachments: int = 10
    max_total_bytes: int = 25 * 1024 * 1024      # 26214400
    max_total_tokens: int = 30_000
    max_images: int = 5
    max_image_bytes: int = 10 * 1024 * 1024      # clipboard._MAX_IMAGE_BYTES
    max_text_bytes: int = 256 * 1024             # clipboard._MAX_TEXT_BYTES

    @classmethod
    def from_settings(cls, data: Optional[Mapping[str, Any]]) -> "AttachmentBudget":
        """Read ``input.attachments`` from a settings mapping.

        Settings files are user-edited JSON, so every value here is hostile
        until proven otherwise: a string where a number belongs, a negative, a
        float, a key nobody recognizes. None of those is worth an exception at
        startup — a malformed limit falls back to the default and a negative
        clamps to zero (which reads as "attach nothing", the safe direction).
        Both camelCase (as documented in §10) and snake_case are accepted, since
        the rest of this SDK's settings tolerate the same.
        """

        if not data:
            return cls()

        def _pick(camel: str, snake: str, default: int) -> int:
            for key in (camel, snake):
                if key in data:
                    value = data[key]
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        return default
                    return max(0, int(value))
            return default

        base = cls()
        return cls(
            max_attachments=_pick("maxAttachments", "max_attachments", base.max_attachments),
            max_total_bytes=_pick("maxTotalBytes", "max_total_bytes", base.max_total_bytes),
            max_total_tokens=_pick("maxTotalTokens", "max_total_tokens", base.max_total_tokens),
            max_images=_pick("maxImages", "max_images", base.max_images),
            max_image_bytes=_pick("maxImageBytes", "max_image_bytes", base.max_image_bytes),
            max_text_bytes=_pick("maxTextBytes", "max_text_bytes", base.max_text_bytes),
        )


#: The shipped defaults, shared so every module that needs a budget without
#: being handed one uses the same object rather than a fresh equal copy.
DEFAULT_BUDGET = AttachmentBudget()


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

#: Characters per token. The usual English-prose approximation; code runs a
#: little denser, which errs toward over-estimating, which is the right way for
#: a budget to be wrong.
CHARS_PER_TOKEN = 4

#: Pixels per token, per the published image tokenization.
PIXELS_PER_TOKEN = 750

#: Providers downscale an image so its area fits this before tokenizing, so an
#: 8000×6000 photo does not cost 64k tokens. Pricing an image at its uploaded
#: size would over-charge the budget by two orders of magnitude.
MAX_IMAGE_PIXELS = 1_150_000

#: What an image with unreadable dimensions is assumed to cost: a full-size one.
#: Guessing small here is how a budget silently stops binding.
IMAGE_FALLBACK_TOKENS = 1600


def estimate_text_tokens(text: str) -> int:
    """Approximate tokens for a string, by the character heuristic."""

    return int(math.ceil(len(text) / float(CHARS_PER_TOKEN)))


def estimate_image_tokens(data: bytes) -> int:
    """Approximate tokens for image bytes, from its pixel dimensions."""

    dims = image_dimensions(data)
    if not dims:
        return IMAGE_FALLBACK_TOKENS
    width, height = dims
    area = float(width * height)
    if area <= 0:
        return IMAGE_FALLBACK_TOKENS
    if area > MAX_IMAGE_PIXELS:
        area = MAX_IMAGE_PIXELS
    return max(1, int(math.ceil(area / PIXELS_PER_TOKEN)))


def estimate_tokens(kind: str, media_type: str, content: Any) -> int:
    """The estimate for one attachment's content.

    A by-reference ``file`` still costs something — the header line naming it —
    which is small but not nothing, and counting it keeps ten referenced
    binaries from being free.
    """

    if kind == "image" or is_image_media_type(media_type):
        return estimate_image_tokens(content if isinstance(content, bytes) else b"")
    if isinstance(content, bytes):
        return 32 if not content else estimate_text_tokens(content.decode("utf-8", "replace"))
    return estimate_text_tokens(content)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One limit that a proposed attachment would break."""

    limit: str          # attachments | total_bytes | total_tokens | images | model_images
    limit_value: int
    would_be: int
    message: str


@dataclass(frozen=True)
class Choice:
    """Something the user can do about it.

    ``action`` is the machine-readable verb (``remove``, ``truncate``,
    ``reference``, ``raise_limit``) and ``target`` names the attachment it
    applies to, so a UI can render a real menu rather than a paragraph of
    advice. This is what "offers choices, not failure" has to mean to be worth
    anything.
    """

    action: str
    label: str
    target: Optional[str] = None


@dataclass(frozen=True)
class Admission:
    """The verdict on adding one attachment to a turn. Never raises, never
    mutates — a UI can call it on every keystroke to grey out a button."""

    ok: bool
    violations: Tuple[Violation, ...] = ()
    choices: Tuple[Choice, ...] = ()

    def message(self) -> str:
        return "; ".join(v.message for v in self.violations)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class TurnBudget:
    """Aggregate accounting for the attachments staged on one turn.

    Deliberately a plain mutable object rather than a frozen struct: it is the
    staging area's backing store, edited by add and remove between keystrokes,
    and threading a new immutable ledger through every edit would buy nothing.
    Ordering is insertion order, because the staged list is numbered in the UI
    (``/attach remove 2``) and those numbers must not move on their own.
    """

    def __init__(
        self,
        budget: AttachmentBudget = DEFAULT_BUDGET,
        *,
        model: Optional[str] = None,
        accepts_images: bool = True,
    ) -> None:
        self.budget = budget
        #: The active model, named in capability errors. Purely informational
        #: here — resolving it from the catalog is the caller's job, which keeps
        #: this module free of any dependency on model metadata.
        self.model = model
        self.accepts_images = accepts_images
        self._items: "dict[str, Attachment]" = {}

    # -- state ------------------------------------------------------------

    @property
    def items(self) -> Tuple[Attachment, ...]:
        return tuple(self._items.values())

    def __iter__(self) -> Iterator[Attachment]:
        return iter(self._items.values())

    def __contains__(self, att_id: object) -> bool:
        return att_id in self._items

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def total_bytes(self) -> int:
        return sum(a.payload_bytes for a in self._items.values())

    @property
    def total_tokens(self) -> int:
        return sum(a.est_tokens for a in self._items.values())

    @property
    def image_count(self) -> int:
        return sum(1 for a in self._items.values() if a.kind == "image")

    # -- decisions --------------------------------------------------------

    def admit(self, att: Attachment) -> Admission:
        """Would ``att`` fit? Reports every broken limit, not just the first.

        Naming only the first is how a user ends up removing an image to satisfy
        the count limit and hitting the token limit on the retry.
        """

        if att.id in self._items:
            # Already staged: re-adding it is a no-op, so it always fits.
            return Admission(ok=True)

        b = self.budget
        violations: List[Violation] = []

        if att.kind == "image" and not self.accepts_images:
            violations.append(Violation(
                limit="model_images",
                limit_value=0,
                would_be=1,
                message="%s does not accept images" % (self.model or "the active model"),
            ))

        if self.count + 1 > b.max_attachments:
            violations.append(Violation(
                limit="attachments", limit_value=b.max_attachments, would_be=self.count + 1,
                message="that would be %d attachments, over the limit of %d"
                        % (self.count + 1, b.max_attachments),
            ))

        would_bytes = self.total_bytes + att.payload_bytes
        if would_bytes > b.max_total_bytes:
            violations.append(Violation(
                limit="total_bytes", limit_value=b.max_total_bytes, would_be=would_bytes,
                message="that would be %s attached, over the limit of %s"
                        % (human_bytes(would_bytes), human_bytes(b.max_total_bytes)),
            ))

        would_tokens = self.total_tokens + att.est_tokens
        if would_tokens > b.max_total_tokens:
            violations.append(Violation(
                limit="total_tokens", limit_value=b.max_total_tokens, would_be=would_tokens,
                message="that would be ~%s tokens of context, over the budget of %s"
                        % (_short_count(would_tokens), _short_count(b.max_total_tokens)),
            ))

        if att.kind == "image" and self.image_count + 1 > b.max_images:
            violations.append(Violation(
                limit="images", limit_value=b.max_images, would_be=self.image_count + 1,
                message="that would be %d images, over the limit of %d"
                        % (self.image_count + 1, b.max_images),
            ))

        if not violations:
            return Admission(ok=True)
        return Admission(ok=False, violations=tuple(violations),
                         choices=self._choices(att, violations))

    def _choices(self, att: Attachment, violations: List[Violation]) -> Tuple[Choice, ...]:
        """Concrete ways out, most-effective first.

        "Remove something" is only useful if it says *what* to remove, so the
        biggest staged item by the limit that broke is offered by name. The
        model-capability violation is the one case with no way out except
        dropping the image or changing models, and it says so rather than
        offering a truncation that cannot help.
        """

        limits = {v.limit for v in violations}
        choices: List[Choice] = []

        if "model_images" in limits:
            return (
                Choice("remove", "don't attach %s" % att.display, att.id),
                Choice("switch_model", "switch to a model that accepts images"),
            )

        by_tokens = sorted(self._items.values(), key=lambda a: a.est_tokens, reverse=True)
        by_bytes = sorted(self._items.values(), key=lambda a: a.payload_bytes, reverse=True)
        biggest = by_tokens[0] if ("total_tokens" in limits and by_tokens) else (
            by_bytes[0] if by_bytes else None)
        if biggest is not None:
            choices.append(Choice(
                "remove", "remove %s (~%s tokens, %s)"
                % (biggest.display, _short_count(biggest.est_tokens),
                   human_bytes(biggest.payload_bytes)),
                biggest.id,
            ))
        choices.append(Choice("remove", "don't attach %s" % att.display, att.id))

        if att.kind == "text" and not att.truncated:
            choices.append(Choice("truncate", "attach only the first part of %s" % att.display,
                                  att.id))
        if att.kind in ("text", "file") and att.path:
            choices.append(Choice("reference", "mention %s by name and let the agent read it"
                                  % att.name, att.id))
        return tuple(choices)

    # -- mutation ---------------------------------------------------------

    def add(self, att: Attachment) -> Attachment:
        """Stage ``att``, or raise carrying the admission that refused it.

        Idempotent by attachment id: the same bytes arriving twice (a drop and
        an `@` mention of the same file) stage once and are charged once.
        """

        admission = self.admit(att)
        if not admission.ok:
            if any(v.limit == "model_images" for v in admission.violations):
                raise AttachmentUnsupportedError(
                    "%s cannot accept images — remove %s, or switch to a "
                    "vision-capable model." % (self.model or "the active model", att.display)
                )
            raise AttachmentBudgetExceededError(
                "%s cannot be attached: %s. %s"
                % (att.display, admission.message(), _choice_hint(admission)),
                admission,
            )
        self._items[att.id] = att
        return att

    def remove(self, att_id: str) -> bool:
        """Unstage by id. False when nothing was staged under it."""

        return self._items.pop(att_id, None) is not None

    def remove_index(self, index: int) -> bool:
        """Unstage by the 1-based number shown in the staged list."""

        items = self.items
        if 1 <= index <= len(items):
            return self.remove(items[index - 1].id)
        return False

    def clear(self) -> None:
        """Drop everything — ``/attach clear``, and what a send does."""

        self._items.clear()

    # -- presentation -----------------------------------------------------

    def summary(self) -> str:
        """The one-line total from §5's staging block."""

        return "total ~%s tokens of %s budget" % (
            _short_count(self.total_tokens), _short_count(self.budget.max_total_tokens))

    def lines(self) -> List[str]:
        """The staged list as §5 renders it, one numbered line per attachment.

        Rendering lives here rather than in the UI because the numbers, the
        estimates and the truncation notes all come from this ledger; the TUI's
        job is to place the strings, not to recompute them.
        """

        out: List[str] = []
        for i, att in enumerate(self.items, 1):
            line = "%2d  %-28s %9s  ~%s tokens" % (
                i, att.name[:28], human_bytes(att.size_bytes), _short_count(att.est_tokens))
            if att.truncated:
                line += "  truncated, %s omitted" % human_bytes(att.omitted_bytes)
            out.append(line)
        out.append(self.summary())
        return out


def _choice_hint(admission: Admission) -> str:
    if not admission.choices:
        return ""
    return "Options: " + "; ".join(c.label for c in admission.choices) + "."


def _short_count(n: int) -> str:
    """``13.2k`` / ``30k`` — how token counts read in the staging block."""

    if n < 1000:
        return str(n)
    value = n / 1000.0
    if value >= 100:
        return "%dk" % round(value)
    # A round number reads as "30k", not "30.0k" — the decimal is only there to
    # keep 13.2k from collapsing to 13k.
    return ("%.1fk" % value).replace(".0k", "k")
