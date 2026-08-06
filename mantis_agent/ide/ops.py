"""IDE protocol operations — §6 of the IDE-integrations plan.

Encoding follows ``mantis_agent/activity/events.py`` exactly — ``msgspec.Struct``,
``frozen=True``, one struct per operation, discriminated by a tag field — because
these operations ride inside the *existing* session envelope (plan §6: "added to
the existing envelope with no new framing"). The tag field is ``op`` and its
values are the wire names the plan spells out (``ide.hello``,
``ide.context.update``, …), so the Python type name is free to be idiomatic
without the wire ever drifting from the document other editors will be built
against.

Scope: this module is the *foundation*. It carries the eight operations the
read-only context path and the first write path need —

    ide.hello  ide.context.update  ide.tabs.update  ide.diagnostics.update
    ide.diff.propose  ide.diff.respond  ide.permission.request  ide.reveal

— and nothing that requires a socket, a daemon, a lease, or an editor. The
remaining §6 operations (``ide.workspace.update``, ``ide.selection.explicit``,
``ide.diff.status``, ``ide.decorate``, ``ide.notify``, ``ide.activity``,
``ide.permission.respond``) are additive: a new struct plus a union member,
with no change to anything here.

Two deliberate decisions worth stating:

* **The error tree is complete.** §13 lists eleven error types; all eleven are
  defined here even though this foundation only raises four of them. They live
  at the bottom of the package's import order (this module imports nothing from
  the package) so that every later module — proposals, decorations, deep links —
  can raise the error the plan names without editing this file or inventing a
  parallel hierarchy.
* **Structs carry no validation.** A ``ContextUpdate`` will happily hold a path
  that escapes the workspace; refusing it is
  :mod:`mantis_agent.ide.workspace`'s job. Decoding and policy are separate so
  the policy can be applied once, at the trust boundary, rather than being
  scattered across constructors that a future op could forget to call.
"""

from __future__ import annotations

from typing import Union

import msgspec

from ..errors import AgentError

__all__ = [
    "ContextUpdate",
    "DECODE_OP",
    "ENCODE_OP",
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
    "Reveal",
    "SEVERITIES",
    "Tab",
    "TabsUpdate",
    "check_protocol_version",
    "decode_op",
    "encode_op",
    "op_name",
]

#: Bumped whenever an existing operation's payload changes shape. A new
#: operation does not bump it — capability negotiation, not the version number,
#: is what tells an older daemon it cannot serve a newer editor (§5).
PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# Errors (§13)
# ---------------------------------------------------------------------------


class IDEError(AgentError):
    """Base for every error the IDE bridge raises.

    It subclasses :class:`~mantis_agent.errors.AgentError` so an embedder that
    already brackets Mantis with ``except AgentError`` keeps working, and so
    §13's "every error is reportable to the editor as a notification" has a
    single type to catch at the bridge boundary.
    """


class IDEProtocolVersionError(IDEError):
    """Editor and daemon disagree about the envelope's shape.

    §5 requires this to be terminal and legible rather than a partial
    connection, so the message always names both versions.
    """


class IDECapabilityError(IDEError):
    """An operation that was not negotiated, or that does not exist.

    Also raised for a payload that does not typecheck: from the daemon's side
    both mean "this peer is not speaking the protocol I agreed to", and both
    resolve the same way — refuse the frame, keep the session.
    """


class IDEWorkspaceMismatchError(IDEError):
    """A well-formed path that resolves outside every declared workspace root.

    Distinct from :class:`IDEPathEscapeError` only in *when* the refusal
    happens: this one is decided after ``realpath``, so it is the error that
    catches traversal and symlinks. Both mean refused, never clamped.
    """


class IDEPathEscapeError(IDEError):
    """A path refused on inspection, before it is worth resolving.

    Empty, NUL-bearing, UNC (``\\\\server\\share``, ``//server/share``), or
    spelled with a Windows separator. These are refused rather than translated
    because translating an editor-supplied path is how a validator and a
    consumer end up disagreeing about what path was checked.
    """


class IDEStaleProposalError(IDEError):
    """The file changed on disk between proposal and response (§8)."""


class IDEProposalTimeoutError(IDEError):
    """An unanswered diff proposal aged out and resolved to reject (§8)."""


class IDEDirtyBufferError(IDEError):
    """An unsaved editor buffer blocks a read or an edit (§7 "Dirty buffers")."""


class IDEContextTooLargeError(IDEError):
    """Editor context exceeded its budget and the caller treats that as fatal.

    The default path *truncates and reports* (§7); this exists for the callers
    that would rather send nothing than send something misleadingly partial.
    """


class IDEDisconnectedError(IDEError):
    """The editor went away mid-operation."""


class IDELeaseRequiredError(IDEError):
    """A control operation attempted without the controller lease (§9)."""


class IDEDeepLinkInvalidError(IDEError):
    """A ``mantis://`` link that failed strict validation (§11)."""


# ---------------------------------------------------------------------------
# Shared payload shapes
# ---------------------------------------------------------------------------

#: Diagnostic severities, most severe first. The order *is* the policy: §7 caps
#: diagnostics "highest severity first", and :func:`budget_diagnostics` ranks by
#: this tuple's index. Names match LSP's, lowercased, because the editor side is
#: reading them straight off ``vscode.DiagnosticSeverity``.
SEVERITIES: tuple[str, ...] = ("error", "warning", "info", "hint")


class Position(msgspec.Struct, frozen=True, omit_defaults=True):
    """A zero-based ``(line, column)``, matching LSP and the VS Code API.

    Zero-based on the wire, one-based only where a human reads it. Converting
    at the boundary once is cheaper than an off-by-one that survives to a
    ``reveal`` landing on the wrong line.
    """

    line: int = 0
    column: int = 0


class Range(msgspec.Struct, frozen=True, omit_defaults=True):
    """A half-open ``[start, end)`` span, again matching LSP."""

    start: Position = msgspec.field(default_factory=Position)
    end: Position = msgspec.field(default_factory=Position)


class Tab(msgspec.Struct, frozen=True, omit_defaults=True):
    """One open editor tab. Paths only — §7 sends no tab contents."""

    path: str
    active: bool = False
    pinned: bool = False
    dirty: bool = False


class Diagnostic(msgspec.Struct, frozen=True, omit_defaults=True):
    """One language-server diagnostic.

    ``message`` is the untrusted surface the plan calls out in §20.4: it is
    produced by a language server from repository content, so it can carry
    anything a source file can carry. Nothing here neutralizes it — the struct
    is a transport shape — but every path that puts one into the model's
    context goes through :mod:`mantis_agent.ide.workspace`.
    """

    path: str
    severity: str = "error"
    message: str = ""
    range: Range | None = None
    source: str = ""
    code: str = ""


class Hunk(msgspec.Struct, frozen=True, omit_defaults=True):
    """One reviewable hunk of a proposed edit.

    ``hunk_id`` is what :class:`DiffRespond` selects on, so it must be stable
    for the life of the proposal; the editor echoes ids, never indices, because
    an index is ambiguous the moment a proposal is superseded.
    """

    hunk_id: str
    old_start: int = 0
    old_lines: int = 0
    new_start: int = 0
    new_lines: int = 0
    text: str = ""


# ---------------------------------------------------------------------------
# Operations (§6)
# ---------------------------------------------------------------------------


class IdeHello(
    msgspec.Struct,
    frozen=True,
    tag="ide.hello",
    tag_field="op",
    omit_defaults=True,
):
    """Editor → daemon handshake: who is attaching and what it can do.

    ``roots`` are the workspace roots the editor declares. They are the *only*
    thing that makes a later path check meaningful, which is why they arrive in
    the handshake rather than being inferred daemon-side from a cwd.
    """

    editor: str
    editor_version: str = ""
    extension_version: str = ""
    protocol_version: int = PROTOCOL_VERSION
    roots: tuple[str, ...] = ()
    caps: tuple[str, ...] = ()


class ContextUpdate(
    msgspec.Struct,
    frozen=True,
    tag="ide.context.update",
    tag_field="op",
    omit_defaults=True,
):
    """Editor → daemon: what the developer is looking at.

    Sent continuously and coalesced (§6, default 150 ms), so every field is
    cheap: ranges and a dirty flag, never buffer contents.

    ``selection_text`` is the exception and is empty by default — §7 sends
    selection *contents* on request only. When it is populated,
    ``selection_truncated_bytes`` states what the cap dropped, because a
    silently truncated selection is a selection the agent will reason about
    wrongly.

    ``explicit`` distinguishes the deliberate "explain this" gesture
    (``ide.selection.explicit`` in §6) from ordinary cursor drift; the agent
    weights the two differently.
    """

    active_file: str | None = None
    selection: Range | None = None
    cursor: Position | None = None
    visible: Range | None = None
    dirty: bool = False
    selection_text: str = ""
    selection_truncated_bytes: int = 0
    explicit: bool = False


class TabsUpdate(
    msgspec.Struct,
    frozen=True,
    tag="ide.tabs.update",
    tag_field="op",
    omit_defaults=True,
):
    """Editor → daemon: the open tab set.

    ``omitted`` lets the *editor* pre-trim (it knows the tab list is 400 long
    before serializing it) while still telling the truth about the count. The
    daemon caps again on ingest; the two numbers add up.
    """

    tabs: tuple[Tab, ...] = ()
    omitted: int = 0


class DiagnosticsUpdate(
    msgspec.Struct,
    frozen=True,
    tag="ide.diagnostics.update",
    tag_field="op",
    omit_defaults=True,
):
    """Editor → daemon: current diagnostics, severity-filtered by the editor."""

    diagnostics: tuple[Diagnostic, ...] = ()
    omitted: int = 0


class DiffPropose(
    msgspec.Struct,
    frozen=True,
    tag="ide.diff.propose",
    tag_field="op",
    omit_defaults=True,
):
    """Daemon → editor: review these hunks before they land.

    ``base_sha256`` is the staleness token of §8: the digest of the file as the
    agent read it. The response is refused unless the file on disk still
    hashes to this, which is what makes "never apply a stale hunk" a check
    rather than a hope.
    """

    edit_id: str
    path: str
    base_sha256: str = ""
    hunks: tuple[Hunk, ...] = ()


class DiffRespond(
    msgspec.Struct,
    frozen=True,
    tag="ide.diff.respond",
    tag_field="op",
    omit_defaults=True,
):
    """Editor → daemon: the developer's decision.

    ``decision`` is ``accept_all`` | ``reject_all`` | ``partial``. ``accepted``
    is meaningful only for ``partial`` and names ``Hunk.hunk_id`` values. The
    two fields are kept separate rather than collapsing "accept all" into "the
    full id list" so that a partial acceptance is always *visibly* partial in
    the record the agent is told about (§8).
    """

    edit_id: str
    decision: str = "reject_all"
    accepted: tuple[str, ...] = ()


class PermissionRequest(
    msgspec.Struct,
    frozen=True,
    tag="ide.permission.request",
    tag_field="op",
    omit_defaults=True,
):
    """Daemon → editor: render this permission prompt natively (§9).

    A flattened projection of the permission layer's decision record: the rule
    that matched, where the rule came from, and — for shell commands — the
    per-segment decomposition, so the editor shows the developer the same
    reasoning the terminal shows.

    This op is a *rendering* request. It is not an approval and it does not
    replace ``check_permission``; §8's "two gates that each think the other is
    authoritative is how both get bypassed" applies here too.
    """

    request_id: str
    tool: str = ""
    target: str = ""
    mode: str = ""
    rule: str = ""
    rule_source: str = ""
    segments: tuple[str, ...] = ()
    choices: tuple[str, ...] = ("allow_once", "allow_session", "deny")


class Reveal(
    msgspec.Struct,
    frozen=True,
    tag="ide.reveal",
    tag_field="op",
    omit_defaults=True,
):
    """Daemon → editor: put the cursor here.

    Reveal-only by construction: there is no field that can make the editor run
    anything, which is what lets §11 hand this op to a ``mantis://`` deep link.
    """

    path: str
    line: int = 0
    column: int = 0


#: The tagged union. ``msgspec.json.Decoder(IdeOp)`` dispatches on ``op``, so a
#: frame reader needs no peek-and-route helper.
IdeOp = Union[
    IdeHello,
    ContextUpdate,
    TabsUpdate,
    DiagnosticsUpdate,
    DiffPropose,
    DiffRespond,
    PermissionRequest,
    Reveal,
]

#: Every wire name this module implements, read off the structs themselves so
#: the set cannot drift from the union.
OP_NAMES: tuple[str, ...] = tuple(
    getattr(cls, "__struct_config__").tag for cls in IdeOp.__args__  # type: ignore[attr-defined]
)

# One encoder/decoder per process, mirroring ``activity.events`` — msgspec
# codecs are thread-safe and reusing them is measurably faster than building one
# per frame at context-update rates.
ENCODE_OP = msgspec.json.Encoder()
DECODE_OP = msgspec.json.Decoder(IdeOp)


def op_name(op: IdeOp) -> str:
    """The wire name of ``op`` (``"ide.hello"``, …)."""

    return str(getattr(type(op), "__struct_config__").tag)


def encode_op(op: IdeOp) -> bytes:
    """Encode one operation to a single frame's worth of bytes."""

    return ENCODE_OP.encode(op)


def decode_op(data: bytes | str) -> IdeOp:
    """Decode one operation, dispatching on its ``op`` tag.

    Every decode failure — unknown op, wrong payload type, malformed JSON —
    surfaces as :class:`IDECapabilityError`. The bridge's contract is "refuse
    the frame, keep the session", and a caller should not have to catch
    ``msgspec.ValidationError`` *and* ``msgspec.DecodeError`` *and* ours to
    honour it.
    """

    try:
        return DECODE_OP.decode(data)
    except msgspec.DecodeError as exc:  # ValidationError is a subclass
        raise IDECapabilityError(f"unusable IDE frame: {exc}") from exc


def check_protocol_version(hello: IdeHello) -> IdeHello:
    """Return ``hello`` if its protocol version is one we speak, else raise.

    Returns the handshake rather than a bool so the check can sit inline in the
    attach path and cannot be written as a discarded expression.
    """

    if hello.protocol_version != PROTOCOL_VERSION:
        raise IDEProtocolVersionError(
            f"IDE protocol version mismatch: editor speaks "
            f"{hello.protocol_version}, this daemon speaks {PROTOCOL_VERSION}. "
            f"Update the Mantis extension (or the CLI) so both agree."
        )
    return hello
