"""Inbound channels — the authenticated front door for external events.

``new-features/s_channels_and_reactive_operation.md``. Mantis is reactive in one
direction only today: it reacts to things it decided to watch. Nothing external
can cause it to act. Closing that gap means opening an inbound path on a
developer's machine, which is a fundamentally different security posture from
anything else the SDK does — so the plan's own recommended order puts the gate
first and the useful parts after it.

This package currently contains **only the pure core**, in pipeline order:

* :mod:`~mantis_agent.channels.verify` — §7. Per-provider signature and token
  verification over the raw bytes, ``hmac.compare_digest`` throughout, Slack's
  freshness window, bounded replay defense, and :func:`verify_then_parse`, the
  one function that is allowed to hand a channel payload to a parser.
* :mod:`~mantis_agent.channels.normalize` — §6. Verified bytes → a
  :class:`ChannelEvent` whose ``fields`` hold only whitelisted typed scalars
  and whose ``subject``/``body`` are neutralized with the shared scrubber.

Deliberately absent: the listener, the journal, the SQLite queue, routing,
policy, replies, and the pollers. Every one of those is a thing that *acts* on
an event, and §21 is explicit that verification has to be right before anything
executes. Importing this package starts nothing, opens nothing, and reads no
configuration.

The one-line version of the threat model, worth repeating wherever this code is
touched: **a channel event is data, never an instruction.** It does not become a
prompt, it does not select a workflow by name, and a routing rule may not match
against its free text. What it may do is supply validated scalars to a named,
pre-approved workflow.

One naming wart to know about: ``verify`` is both the submodule and the
dispatcher function re-exported from it, and the function wins as the package
attribute. ``from mantis_agent.channels.verify import ReplayGuard`` works as
expected; ``import mantis_agent.channels.verify as m`` gets the *function*. Use
``importlib.import_module("mantis_agent.channels.verify")`` when the module
object is what you want.
"""

from __future__ import annotations

from .normalize import (
    KNOWN_KINDS,
    MAX_ACTOR_CHARS,
    MAX_BODY_CHARS,
    MAX_FIELD_CHARS,
    MAX_FIELDS,
    MAX_SUBJECT_CHARS,
    NON_ROUTABLE_KEYS,
    PROVIDER_ACTOR_SIGNED,
    ChannelEvent,
    FieldSpec,
    actor_is_routable,
    assert_routable_key,
    clean_field_value,
    extract_fields,
    is_routable_key,
    neutralize_text,
    normalize,
    quote_untrusted,
)
from .verify import (
    ALLOWED_ALGORITHMS,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_REPLAY_ENTRIES,
    DEFAULT_REPLAY_TTL_SECONDS,
    GITHUB_DELIVERY_HEADER,
    GITHUB_SIGNATURE_HEADER,
    GITLAB_EVENT_ID_HEADER,
    GITLAB_TOKEN_HEADER,
    METHODS,
    PROVIDER_METHODS,
    SLACK_FRESHNESS_SECONDS,
    SLACK_SIGNATURE_HEADER,
    SLACK_TIMESTAMP_HEADER,
    VERIFY_TIERS,
    ChannelConfigError,
    ChannelError,
    ChannelNoVerificationError,
    ChannelRateLimitedError,
    PayloadMalformedError,
    PayloadTooLargeError,
    ReplayDetectedError,
    ReplayGuard,
    SignatureInvalidError,
    SignatureStaleError,
    VerifiedPayload,
    VerifyConfig,
    body_hash_id,
    verify,
    verify_generic,
    verify_github,
    verify_gitlab,
    verify_slack,
    verify_then_parse,
)

__all__ = [
    "ALLOWED_ALGORITHMS",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_REPLAY_ENTRIES",
    "DEFAULT_REPLAY_TTL_SECONDS",
    "GITHUB_DELIVERY_HEADER",
    "GITHUB_SIGNATURE_HEADER",
    "GITLAB_EVENT_ID_HEADER",
    "GITLAB_TOKEN_HEADER",
    "KNOWN_KINDS",
    "MAX_ACTOR_CHARS",
    "MAX_BODY_CHARS",
    "MAX_FIELDS",
    "MAX_FIELD_CHARS",
    "MAX_SUBJECT_CHARS",
    "METHODS",
    "NON_ROUTABLE_KEYS",
    "PROVIDER_ACTOR_SIGNED",
    "PROVIDER_METHODS",
    "SLACK_FRESHNESS_SECONDS",
    "SLACK_SIGNATURE_HEADER",
    "SLACK_TIMESTAMP_HEADER",
    "VERIFY_TIERS",
    "ChannelConfigError",
    "ChannelError",
    "ChannelEvent",
    "ChannelNoVerificationError",
    "ChannelRateLimitedError",
    "FieldSpec",
    "PayloadMalformedError",
    "PayloadTooLargeError",
    "ReplayDetectedError",
    "ReplayGuard",
    "SignatureInvalidError",
    "SignatureStaleError",
    "VerifiedPayload",
    "VerifyConfig",
    "actor_is_routable",
    "assert_routable_key",
    "body_hash_id",
    "clean_field_value",
    "extract_fields",
    "is_routable_key",
    "neutralize_text",
    "normalize",
    "quote_untrusted",
    "verify",
    "verify_generic",
    "verify_github",
    "verify_gitlab",
    "verify_slack",
    "verify_then_parse",
]
