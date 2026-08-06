"""CI code review — the pure core.

What is here today is the part of the plan that has no dependencies and no
credentials: what a finding *is* (:mod:`.findings`) and how it is identified
across runs (:mod:`.fingerprint`). Both are ordinary functions over strings —
no forge client, no network, no model, no environment. That is deliberate, and
it is the order the plan asks for: a local review of a diff delivers real value
while carrying none of the credential risk, and the schema, ranking and
fingerprinting it exercises are what everything later is built on.

Still to come, each landing in its own module so this one stays importable
without a token: ``review`` (the workflow), ``untrusted`` (ingest
neutralization for forge content), ``render`` (comment templates),
``publish`` (idempotent, fingerprint-matched publication), ``preflight``
(the write-token guard), ``forge/`` (the GitHub and GitLab adapters),
``triage`` and ``autofix``.

Two invariants set here hold for all of it:

* **A finding cannot exist unsanitized.** The check lives in
  ``Finding.__post_init__``, not in the renderer, so no future publication path
  can forget it and post an attacker's image URL under the repository's name.
* **Identity is content, never position.** A fingerprint excludes the line
  number, so a rebase does not turn fifteen comments into thirty.
"""

from __future__ import annotations

from .findings import (
    KNOWN_CATEGORIES,
    MAX_BODY,
    MAX_FINDINGS,
    MAX_PER_FILE,
    MAX_TITLE,
    SEVERITIES,
    CapReport,
    CIError,
    Finding,
    FindingsSchemaError,
    cap_findings,
    dedupe_findings,
    fence_for,
    finding_from_dict,
    make_finding,
    rank_findings,
    sanitize_code,
    sanitize_text,
)
from .fingerprint import (
    FINGERPRINT_VERSION,
    extract_marker,
    finding_fingerprint,
    marker,
)

__all__ = [
    "FINGERPRINT_VERSION",
    "KNOWN_CATEGORIES",
    "MAX_BODY",
    "MAX_FINDINGS",
    "MAX_PER_FILE",
    "MAX_TITLE",
    "SEVERITIES",
    "CIError",
    "CapReport",
    "Finding",
    "FindingsSchemaError",
    "cap_findings",
    "dedupe_findings",
    "extract_marker",
    "fence_for",
    "finding_fingerprint",
    "finding_from_dict",
    "make_finding",
    "marker",
    "rank_findings",
    "sanitize_code",
    "sanitize_text",
]
