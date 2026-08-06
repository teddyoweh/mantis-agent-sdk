"""Rich input: one attachment model, one validator, one per-turn budget.

Mantis can already take an image off the clipboard, a file off a drag-and-drop,
and a path out of the middle of a sentence — but each of those routes builds
content blocks its own way, so there has never been an object meaning "the user
attached this thing". Without one there is nowhere to validate, price, dedupe,
preview or remove an attachment, and a fifth input path (IDE, headless, voice)
means a fifth implementation of all five.

This package is that object and the pipeline around it::

    source → acquire → detect → validate → budget → stage → preview → blocks

Three modules land first, and they are pure — no clipboard, no terminal, no
network, no provider:

* :mod:`~mantis_agent.input.attachment` — the :class:`Attachment` record, media
  detection from magic bytes, and the ``attach_*`` entry points.
* :mod:`~mantis_agent.input.validate` — path resolution (shared with the
  permission engine), root and protected-path policy, file-type checks, secret
  heuristics.
* :mod:`~mantis_agent.input.budget` — the per-turn aggregate accounting that
  §8 specifies and that does not exist anywhere in the codebase today.

``stage``/``preview`` are the UI's half and are not implemented here; the
sources (clipboard, drop, mention, CLI, IDE, voice) are adapters still to be
written on top. Nothing in ``clipboard.py`` changes: :func:`to_blocks` calls
back into its image encoder rather than growing a second one.

Typical use::

    from mantis_agent.input import AttachPolicy, TurnBudget, attach_path

    turn = TurnBudget()                       # one per user turn
    att = attach_path("logs/crash.log",
                      source="mention",
                      policy=AttachPolicy(root=workspace),
                      confirm=ask_the_user,
                      turn=turn)
    print(turn.summary())                     # "total ~13.2k tokens of 30k budget"
"""

from __future__ import annotations

from .attachment import (
    KINDS,
    SOURCES,
    Attachment,
    AttachmentBudgetExceededError,
    AttachmentDeniedError,
    AttachmentPathError,
    AttachmentSecretWarning,
    AttachmentTooLargeError,
    AttachmentTypeError,
    AttachmentUnsupportedError,
    InputError,
    attach_bytes,
    attach_path,
    attach_text,
    detect_media_type,
    human_bytes,
    image_dimensions,
    is_image_media_type,
    kind_for_media_type,
    to_blocks,
)
from .budget import (
    DEFAULT_BUDGET,
    Admission,
    AttachmentBudget,
    Choice,
    TurnBudget,
    Violation,
    estimate_image_tokens,
    estimate_text_tokens,
    estimate_tokens,
)
from .validate import (
    PROTECTED_DIRS,
    PROTECTED_NAMES,
    AttachPolicy,
    Confirmation,
    PathVerdict,
    check_file_type,
    check_path,
    protected_label,
    resolve,
    scan_secrets,
)

__all__ = [
    "Admission",
    "AttachPolicy",
    "Attachment",
    "AttachmentBudget",
    "AttachmentBudgetExceededError",
    "AttachmentDeniedError",
    "AttachmentPathError",
    "AttachmentSecretWarning",
    "AttachmentTooLargeError",
    "AttachmentTypeError",
    "AttachmentUnsupportedError",
    "Choice",
    "Confirmation",
    "DEFAULT_BUDGET",
    "InputError",
    "KINDS",
    "PROTECTED_DIRS",
    "PROTECTED_NAMES",
    "PathVerdict",
    "SOURCES",
    "TurnBudget",
    "Violation",
    "attach_bytes",
    "attach_path",
    "attach_text",
    "check_file_type",
    "check_path",
    "detect_media_type",
    "estimate_image_tokens",
    "estimate_text_tokens",
    "estimate_tokens",
    "human_bytes",
    "image_dimensions",
    "is_image_media_type",
    "kind_for_media_type",
    "protected_label",
    "resolve",
    "scan_secrets",
    "to_blocks",
]
