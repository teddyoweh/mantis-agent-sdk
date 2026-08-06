"""Plugin packages — the unit Mantis's extension points have never had.

Skills, personas, workflows, rules, MCP servers, and hooks each already load
from their own directory with their own parser. What is missing is a *bundle*:
a versioned, verifiable thing you can install, inspect, update, and remove.
This package is that bundle's foundation — the format and the two checks that
have to be right before anything else can be built on top:

``manifest``
    The schema and its validator, plus **capability conformance**. A manifest
    declaring ``shellBlocks: false`` whose skill declares a shell block fails.
    The declaration is a contract verified against content, which is the only
    reason an install prompt means anything.
``archive``
    ``safe_extract`` — extraction that refuses traversal, link escapes, device
    entries, setuid bits, and compression bombs *before writing*, then renames
    a fully verified temp directory into place so a failure can never leave a
    half-populated destination.

Two properties are load-bearing and stated here because everything downstream
assumes them:

* **Installation executes nothing.** There is no ``postinstall`` hook, no
  script, no import. Install is fetch → verify → extract → verify → activate.
  Plugin content runs only when the user later invokes it, through the
  permission layer that already governs that surface.
* **Capability disclosure is the security model.** A signature proves who
  published, not what is safe. What bounds the damage is a declared,
  *enforced* list of the executable surfaces a plugin touches.

The error tree below follows the plan's hierarchy. Only the members this
foundation can actually raise are defined; the store, installer, resolver, and
marketplace add their own leaves beside them (``ActivationError``,
``DependencyResolutionError``, …) as those land.
"""

from __future__ import annotations

from ..errors import AgentError


class PluginError(AgentError):
    """Base for every plugin packaging, verification, and install failure."""


class ManifestInvalidError(PluginError):
    """``mantis-plugin.json`` is missing, unparseable, or violates the schema."""


class ManifestVersionError(PluginError):
    """``schemaVersion`` is not one this build understands.

    Deliberately distinct from :class:`ManifestInvalidError`: "your Mantis is
    too old for this plugin" and "this plugin is malformed" send the user to
    completely different places."""


class CapabilityUndeclaredError(PluginError):
    """Content exceeds what the manifest declared.

    Raised when a plugin ships an executable surface — a shell block, a Python
    tool module, a stdio MCP server, a hook command — or extension content that
    the approval prompt would never have shown."""


class IntegrityError(PluginError):
    """An archive or per-file hash did not match what the manifest claimed."""


class _ArchiveProblem(PluginError):
    """Shared shape for extraction refusals: a machine-readable ``reason`` and
    the offending ``member``, because "unsafe archive" alone tells a plugin
    author nothing about which entry to fix."""

    __slots__ = ("reason", "member", "detail")

    def __init__(self, reason: str, member: str | None = None, detail: str = ""):
        parts = [reason]
        if member:
            parts.append(f"entry {member!r}")
        if detail:
            parts.append(detail)
        super().__init__(": ".join(parts))
        self.reason = reason
        self.member = member
        self.detail = detail


class UnsafeArchiveError(_ArchiveProblem):
    """An archive entry could write outside the extraction root, or is not a
    plain file or directory (link, device, FIFO, socket, setuid payload)."""


class ArchiveTooLargeError(_ArchiveProblem):
    """A size, count, or compression-ratio ceiling was exceeded — the archive
    is a resource-exhaustion payload, whether or not it means to be."""


__all__ = [
    "ArchiveTooLargeError",
    "CapabilityUndeclaredError",
    "IntegrityError",
    "ManifestInvalidError",
    "ManifestVersionError",
    "PluginError",
    "UnsafeArchiveError",
]
