"""IDE bridge — the editor-facing half of the session protocol.

The plan (`new-features/r_ide_integrations.md`) builds an editor integration as
a *client of the existing session protocol* rather than as a second protocol
with an editor-shaped transport. This package is the Python side of that: the
operation vocabulary, and the rules that make editor-supplied data safe to act
on. No editor SDK, no socket, no daemon — those live in ``daemon/`` and in the
TypeScript extension respectively.

Two modules land first, and they are pure — no I/O beyond ``realpath``, no
asyncio, no UI:

* :mod:`~mantis_agent.ide.ops` — the §6 operations as ``msgspec`` structs
  riding the existing envelope, plus §13's error tree.
* :mod:`~mantis_agent.ide.workspace` — §7's trust boundary: workspace-root path
  validation (resolve, then check, then *refuse* — never clamp), the context
  budget with its omitted counts, and neutralization of editor-supplied strings
  through the shared :mod:`mantis_agent.child_report` pipeline.

The rest of the plan's file map (``context.py``, ``proposals.py``,
``decorations.py``, ``deeplinks.py``) builds on these two and adds nothing they
need to know about. Importing this package costs nothing at runtime beyond
``msgspec``, which the SDK already imports, and nothing in it starts a task or
opens a connection — §2's "zero cost when no IDE is attached" starts here.
"""

from __future__ import annotations

from .ops import (
    OP_NAMES,
    PROTOCOL_VERSION,
    SEVERITIES,
    ContextUpdate,
    Diagnostic,
    DiagnosticsUpdate,
    DiffPropose,
    DiffRespond,
    Hunk,
    IDECapabilityError,
    IDEContextTooLargeError,
    IDEDeepLinkInvalidError,
    IDEDirtyBufferError,
    IDEDisconnectedError,
    IDEError,
    IDELeaseRequiredError,
    IDEPathEscapeError,
    IDEProposalTimeoutError,
    IDEProtocolVersionError,
    IDEStaleProposalError,
    IDEWorkspaceMismatchError,
    IdeHello,
    IdeOp,
    PermissionRequest,
    Position,
    Range,
    Reveal,
    Tab,
    TabsUpdate,
    check_protocol_version,
    decode_op,
    encode_op,
    op_name,
)
from .workspace import (
    DEFAULT_BUDGETS,
    BudgetReport,
    Budgets,
    ResolvedPath,
    WorkspaceRoots,
    budget_diagnostics,
    budget_selection,
    budget_tabs,
    neutralize_editor_text,
    render_context_envelope,
)

__all__ = [
    "BudgetReport",
    "Budgets",
    "ContextUpdate",
    "DEFAULT_BUDGETS",
    "Diagnostic",
    "DiagnosticsUpdate",
    "DiffPropose",
    "DiffRespond",
    "Hunk",
    "IDECapabilityError",
    "IDEContextTooLargeError",
    "IDEDeepLinkInvalidError",
    "IDEDirtyBufferError",
    "IDEDisconnectedError",
    "IDEError",
    "IDELeaseRequiredError",
    "IDEPathEscapeError",
    "IDEProposalTimeoutError",
    "IDEProtocolVersionError",
    "IDEStaleProposalError",
    "IDEWorkspaceMismatchError",
    "IdeHello",
    "IdeOp",
    "OP_NAMES",
    "PROTOCOL_VERSION",
    "PermissionRequest",
    "Position",
    "Range",
    "ResolvedPath",
    "Reveal",
    "SEVERITIES",
    "Tab",
    "TabsUpdate",
    "WorkspaceRoots",
    "budget_diagnostics",
    "budget_selection",
    "budget_tabs",
    "check_protocol_version",
    "decode_op",
    "encode_op",
    "neutralize_editor_text",
    "op_name",
    "render_context_envelope",
]
