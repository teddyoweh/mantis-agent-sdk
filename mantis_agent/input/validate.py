"""Validation: where a path resolves, what kind of file it is, what it contains.

Three checks, in a fixed order, because the order is the security property.

**Resolve first.** ``docs/../../.ssh/id_rsa`` is not a path inside ``docs/``,
and a symlink named ``notes.txt`` is not a text file in the workspace. Both walk
straight past any check performed on the string as written, so every path here
goes through :func:`resolve`, which is
:func:`mantis_agent.permission_grammar.resolve_path` — the *same* resolver the
permission engine matches its rules with. Two path validators that disagree is
a bug generator, and the disagreement is always discovered by the person who
found the way around one of them.

**Then check the file type**, by ``stat``, before anything opens it. A FIFO is
the reason: ``open()`` on a FIFO with no writer blocks forever, so a validator
that reads first and asks later can be made to hang by dropping a named pipe
into a prompt. Devices and sockets are refused for the same family of reasons.

**Then check the contents**, which is the only check that is advisory.
:func:`scan_secrets` reports credential-shaped text and the pipeline hangs the
finding on the attachment as a warning; §7 is explicit that this is a warning
rather than a block, because a user may legitimately need to share a config
file and only they can tell.

Root policy sits on top of resolution. Inside the workspace, attach freely.
Outside, confirm — showing the *resolved* path, since the resolved path is the
one thing the user has not already seen. Protected paths (``.env``, ``~/.ssh``,
``~/.aws``, ``~/.mantis`` and their neighbours) require confirmation even when
they are inside the workspace and even when the user named them directly: an
attachment is a copy of a credential into a model's context and out to a
provider, and "I typed the filename" is not evidence that its contents were
understood.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat as stat_mod
from dataclasses import dataclass, field
from typing import IO, List, Optional, Tuple

from ..permission_grammar import resolve_path
from ..redaction import is_secret_name
from .attachment import AttachmentPathError, AttachmentTypeError
from .budget import DEFAULT_BUDGET, AttachmentBudget

__all__ = [
    "AttachPolicy",
    "Confirmation",
    "PROTECTED_DIRS",
    "PROTECTED_NAMES",
    "PathVerdict",
    "check_file_type",
    "check_path",
    "open_regular",
    "protected_label",
    "resolve",
    "scan_secrets",
]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachPolicy:
    """How attaching behaves for one workspace.

    Mirrors ``input.attachments`` in §10. ``outside_root`` takes the three
    documented values — ``allow``, ``prompt``, ``deny`` — and defaults to
    ``prompt``, which is the setting that makes the feature usable without
    making it dangerous: reaching outside the workspace is normal (a log in
    ``/var/log``, a config in ``~``) and it is also exactly how a credential
    ends up attached by accident.
    """

    root: Optional[str] = None                    # None → the current directory
    outside_root: str = "prompt"                  # allow | prompt | deny
    scan_secrets: bool = True
    budget: AttachmentBudget = DEFAULT_BUDGET


@dataclass(frozen=True)
class Confirmation:
    """A question the pipeline needs answered before it reads something.

    Carries the resolved path rather than what the user typed, because the
    difference between the two is the entire content of the question.
    """

    kind: str                                     # outside-root | protected-path | secret
    path: Optional[str]
    message: str
    detail: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PathVerdict:
    """What resolution and the root policy concluded about one path."""

    raw: str
    resolved: str
    inside_root: bool
    protected: Optional[str] = None               # the matched protected-path label
    needs_confirmation: bool = False
    reasons: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------
#
# The list is deliberately a little wider than §7's four entries. A false
# positive costs one confirmation prompt on a file the user meant to attach; a
# false negative costs a private key uploaded to a model provider, where it
# cannot be recalled. The asymmetry decides every borderline case below.

#: Directories whose entire contents are credentials, ``~``-relative.
PROTECTED_DIRS: Tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.mantis",
    "~/.gnupg",
    "~/.kube",
    "~/.docker",
    "~/.config/gcloud",
    "~/.azure",
)

#: Filename globs, matched against the basename, case-insensitively (macOS and
#: Windows filesystems fold case, so a check that does not would be trivially
#: sidestepped by ``.ENV``).
PROTECTED_NAMES: Tuple[str, ...] = (
    ".env",
    ".env.*",
    ".netrc",
    "_netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    "credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
)

#: ``.env.example`` is a template that ships in the repo — the whole point of it
#: is that it holds no secrets. Prompting on it would train the user to say yes.
_ENV_TEMPLATE_SUFFIXES = ("example", "sample", "template", "dist", "defaults")


def protected_label(resolved: str) -> Optional[str]:
    """The reason ``resolved`` is protected, or ``None``.

    Returns a short human label (``~/.ssh``, ``.env``) rather than a bool so the
    confirmation prompt can say *which* rule fired — "this is under ~/.ssh"
    lands very differently from "this file is protected".
    """

    norm = os.path.normcase(os.path.normpath(resolved))
    for spec in PROTECTED_DIRS:
        base = os.path.normcase(os.path.normpath(os.path.expanduser(spec)))
        if norm == base or norm.startswith(base + os.sep):
            return spec

    # Folded explicitly rather than through ``normcase``, which only folds on
    # Windows — on macOS the filesystem is case-insensitive but ``normcase`` is
    # the identity, so ``.ENV`` and ``~/.ssh/ID_RSA`` would walk straight past a
    # normcase-only comparison on the platform most likely to produce them.
    name = os.path.basename(norm).lower()
    if _is_env_template(name):
        return None
    for pattern in PROTECTED_NAMES:
        if fnmatch.fnmatchcase(name, pattern):
            return pattern
    return None


def _is_env_template(name: str) -> bool:
    """``name`` is already folded to lower case by :func:`protected_label`."""

    if not name.startswith(".env"):
        return False
    return name[len(".env"):].lstrip(".") in _ENV_TEMPLATE_SUFFIXES


# ---------------------------------------------------------------------------
# Path resolution and the root policy
# ---------------------------------------------------------------------------


def resolve(raw: str, root: Optional[str] = None) -> str:
    """The absolute, symlink- and ``..``-free form of ``raw``.

    Delegates to the permission engine's resolver on purpose — see the module
    docstring. Relative paths are taken against ``root`` (the workspace), which
    is what makes ``@src/app.py`` mean the same file to the attachment pipeline
    and to a ``Read(src/**)`` rule.
    """

    return resolve_path(raw, root)


def check_path(
    raw: str,
    *,
    root: Optional[str] = None,
    outside_root: str = "prompt",
) -> PathVerdict:
    """Resolve ``raw`` and apply the root policy.

    Raises :class:`AttachmentPathError` only for ``outside_root="deny"``; every
    other outcome is reported on the verdict so the caller decides whether it
    has anyone to ask. The empty-path case raises too, since it is a bug at the
    call site rather than a user decision.
    """

    text = (raw or "").strip()
    if not text:
        raise AttachmentPathError("no path given — say which file to attach.")

    root_resolved = resolve(root or os.getcwd())
    resolved = resolve(text, root)

    inside = resolved == root_resolved or resolved.startswith(root_resolved + os.sep)
    protected = protected_label(resolved)
    reasons: List[str] = []
    needs_confirmation = False

    if not inside:
        if outside_root == "deny":
            raise AttachmentPathError(
                "%s is outside the workspace (%s) and input.attachments."
                "allowOutsideWorkspace is \"deny\" — copy it in, or change the setting."
                % (resolved, root_resolved)
            )
        if outside_root != "allow":
            needs_confirmation = True
            reasons.append("outside the workspace: %s" % resolved)

    if protected:
        # Protected wins over any root policy, including "allow": the point of
        # the list is that these files are dangerous *wherever* they live.
        needs_confirmation = True
        reasons.append("protected path (%s): %s" % (protected, resolved))

    return PathVerdict(
        raw=text,
        resolved=resolved,
        inside_root=inside,
        protected=protected,
        needs_confirmation=needs_confirmation,
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# File type
# ---------------------------------------------------------------------------


def check_file_type(resolved: str) -> os.stat_result:
    """Require a regular file, and return its ``stat``.

    ``stat`` rather than ``lstat``: the path has already been resolved, so there
    is no symlink left to follow and the two agree — but ``stat`` is what a
    subsequent ``open()`` will act on, which is the thing being authorized.

    Nothing here opens the file. That is the point: ``open()`` on a FIFO with no
    writer blocks until one appears, so a validator that opens before it checks
    can be hung by a named pipe.
    """

    try:
        st = os.stat(resolved)
    except FileNotFoundError:
        raise AttachmentPathError(
            "no such file: %s — check the path, or drag the file in again." % resolved
        ) from None
    except PermissionError:
        raise AttachmentPathError(
            "cannot read %s (permission denied)." % resolved
        ) from None
    except OSError as exc:
        raise AttachmentPathError("cannot read %s (%s)." % (resolved, exc)) from None

    mode = st.st_mode
    if stat_mod.S_ISDIR(mode):
        raise AttachmentTypeError(
            "%s is a directory — attach a file inside it, or mention the "
            "directory and let the agent list it." % resolved
        )
    if stat_mod.S_ISFIFO(mode):
        raise AttachmentTypeError(
            "%s is a FIFO (named pipe), not a file — redirect it to a file "
            "first, then attach that." % resolved
        )
    if stat_mod.S_ISSOCK(mode):
        raise AttachmentTypeError("%s is a socket, not a file." % resolved)
    if stat_mod.S_ISCHR(mode) or stat_mod.S_ISBLK(mode):
        raise AttachmentTypeError(
            "%s is a device, not a file — reading it has no end and no meaning "
            "in a prompt." % resolved
        )
    if not stat_mod.S_ISREG(mode):
        raise AttachmentTypeError("%s is not a regular file." % resolved)
    return st


def open_regular(resolved: str) -> "IO[bytes]":
    """Open a path for reading, re-checking the type through the descriptor.

    :func:`check_file_type` answers about the path; this answers about the file
    that was actually opened, and between those two moments the name can be
    replaced. Three flags make the difference matter:

    * ``O_NONBLOCK`` so that if the name has become a FIFO the open returns
      immediately instead of hanging until someone writes to it,
    * ``O_NOFOLLOW`` so a symlink swapped in after resolution fails the open
      rather than redirecting the read,
    * ``fstat`` on the resulting descriptor, which is the only check that
      describes the bytes we are about to read.

    All three degrade gracefully where the platform lacks them (Windows has
    neither flag), leaving the path-level check as the floor.
    """

    flags = os.O_RDONLY
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(resolved, flags)
    except FileNotFoundError:
        raise AttachmentPathError("no such file: %s" % resolved) from None
    except OSError as exc:
        raise AttachmentPathError("cannot open %s (%s)." % (resolved, exc)) from None

    try:
        if not stat_mod.S_ISREG(os.fstat(fd).st_mode):
            raise AttachmentTypeError(
                "%s stopped being a regular file while it was being attached." % resolved
            )
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


# ---------------------------------------------------------------------------
# Secret heuristics
# ---------------------------------------------------------------------------
#
# Two halves. The first is a set of *shapes* — vendor-issued credentials whose
# format is public and unmistakable, where a match is a finding on its own. The
# second is assignment matching: a name that reads like a credential (reusing
# `redaction.SECRET_NAME_RE`, which is already the union of this codebase's
# three previous secret-name lists) bound to a value that looks like an actual
# literal rather than a lookup or a placeholder.
#
# The value test is what keeps this usable. `password = os.environ["PW"]` and
# `API_KEY=your-key-here` are the two most common lines in the files people
# legitimately attach, and a scanner that flags both is a scanner people learn
# to ignore.

_SECRET_SHAPES: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("provider API key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.")),
)

#: ``NAME = value`` in the shapes config files and shell scripts use.
_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_.-]{2,64})[ \t]*[:=][ \t]*(\S.*)$",
    re.M,
)

#: A value that could be a literal credential: no punctuation that implies an
#: expression, long enough to be one, and mixing character classes the way
#: generated tokens do.
_LITERAL_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-./+=~]{12,}$")

_PLACEHOLDERS = (
    "example", "placeholder", "changeme", "change-me", "your-", "your_", "xxxx",
    "redacted", "todo", "none", "null", "insert", "here", "dummy", "sample",
    "<", ">", "${", "%(",
)


def scan_secrets(text: str, *, limit: int = 8) -> Tuple[str, ...]:
    """Credential-shaped things in attached text, as short human labels.

    Advisory: the pipeline turns each finding into a warning on the attachment,
    never a refusal. Deduplicated and capped at ``limit``, because a leaked
    ``.env`` produces forty findings and the fortieth persuades no one who was
    not persuaded by the first.
    """

    if not text:
        return ()

    found: List[str] = []

    def _note(label: str) -> None:
        if label not in found and len(found) < limit:
            found.append(label)

    for label, pattern in _SECRET_SHAPES:
        if pattern.search(text):
            _note(label)

    for match in _ASSIGNMENT_RE.finditer(text):
        name, value = match.group(1), match.group(2).strip()
        if not is_secret_name(name):
            continue
        if _looks_like_a_literal_secret(value):
            _note("%s looks like a credential" % name)
        if len(found) >= limit:
            break

    return tuple(found)


def _looks_like_a_literal_secret(value: str) -> bool:
    """Whether an assigned value is plausibly a real credential."""

    value = value.split("#", 1)[0].strip().strip("'\"").strip()
    if len(value) < 12:
        return False
    lowered = value.lower()
    if any(token in lowered for token in _PLACEHOLDERS):
        return False
    if not _LITERAL_VALUE_RE.match(value):
        # Anything with a bracket, a call, a space or a quote is an expression
        # or a sentence, not a key.
        return False
    classes = (
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
    )
    # A generated credential mixes at least two character classes; a filesystem
    # path or a version string usually does not.
    return sum(1 for present in classes if present) >= 2
