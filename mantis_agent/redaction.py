"""One answer to "does this name look like a credential?", and one safe
environment to hand a child process.

Three places in this codebase already decide, separately, what a secret is:
``serve.py`` (``key|token|secret|password|apikey``), ``mcp/manager.py``
(``…|passwd|auth|credential``) and ``workflow_store.py`` (a tuple that also
knows about cookies and session ids). Three lists is three chances to be the
one that missed ``AWS_SECRET_ACCESS_KEY`` after someone edited only two of
them, so :data:`SECRET_NAME_RE` is their union and the place to add the next
word.

The other half is :func:`build_child_env`. The sandbox in :mod:`.sandbox`
confines the *filesystem*, which is the wrong half of the threat if the child
still inherits ``ANTHROPIC_API_KEY`` — a process that can read ``os.environ``
and open a socket does not need to write to your disk to hurt you. So the child
environment is built by **allowlist**: name the handful of variables a program
needs to run at all, and everything else — including the variable nobody
thought to deny — is simply absent.

    from mantis_agent.redaction import build_child_env

    subprocess.run(argv, env=build_child_env())
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "SECRET_NAME_RE",
    "build_child_env",
    "is_secret_name",
    "redact_value",
]

# The union of serve.py's `_SECRET_KEY_RE`, mcp/manager.py's `_SECRETISH` and
# workflow_store.py's `_SECRET_HINTS`. Substring match, deliberately: the
# interesting names are ``ANTHROPIC_API_KEY`` and ``npm_config_…_apikey``, not
# bare ``key``, and anchoring would miss both.
SECRET_NAME_RE = re.compile(
    r"key|token|secret|password|passwd|apikey|auth|credential|cookie"
    r"|session_id|private",
    re.I,
)

# Redaction is a fixed-width mask rather than the real length: leaking "that
# key is 51 characters" is a small thing, but it is free not to leak it.
_MASK = "•" * 8


def is_secret_name(name: str) -> bool:
    """Whether ``name`` reads like it holds a credential.

    Errs toward yes. A false positive costs one redacted log line; a false
    negative costs an API key.
    """

    return bool(name) and SECRET_NAME_RE.search(str(name)) is not None


def redact_value(value: Any) -> str:
    """A credential rendered as fixed-width dots.

    Empty stays empty, so ``FOO=`` still reads as *unset* rather than as a
    value someone hid — the distinction matters when debugging why a tool
    can't authenticate.
    """

    s = "" if value is None else str(value)
    return _MASK if s else ""


# ---------------------------------------------------------------------------
# The child environment
# ---------------------------------------------------------------------------
#
# Everything below is an allowlist. The temptation is always to write a
# denylist ("drop *_KEY, *_TOKEN, …") because it keeps working programs
# working, but a denylist is only ever as good as the imagination of whoever
# wrote it, and the environment of a developer machine is a junk drawer:
# `AWS_SESSION_TOKEN`, `GH_TOKEN`, `NPM_TOKEN`, `VAULT_ADDR`, `KUBECONFIG`,
# `SSH_AUTH_SOCK`, and whatever the next vendor's CLI decides to export.

# Names any program is entitled to assume exist.
_ALLOW_EXACT = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TZ", "PWD",
    "TMPDIR", "TMP", "TEMP",
    # Windows can't start a process without these; COMSPEC is what `subprocess`
    # execs for `shell=True`, and SystemRoot is where the CRT finds its DLLs.
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
})

# ``LC_ALL``, ``LC_CTYPE``, … — locale is a family, not a name.
_ALLOW_PREFIXES = ("LC_",)

# The three spellings of "where scratch files go". They travel together: set
# only TMPDIR and a Windows-flavoured toolchain writes somewhere else entirely.
_TMP_NAMES = ("TMPDIR", "TMP", "TEMP")


def _allowed(name: str, extra: Iterable[str]) -> bool:
    # Windows env names are case-insensitive, so compare folded and keep the
    # caller's casing in the output.
    upper = name.upper()
    if upper in _ALLOW_EXACT or upper.startswith(_ALLOW_PREFIXES):
        return True
    return upper in extra


def build_child_env(
    base: Mapping[str, str] | None = None,
    *,
    extra_pass: Sequence[str] = (),
    tmpdir: str | None = None,
) -> dict[str, str]:
    """The environment to hand a subprocess: allowlisted names only.

    ``base`` defaults to :data:`os.environ`. ``extra_pass`` widens the
    allowlist for a specific call (``CI``, ``JAVA_HOME``, a build flag) —
    but *not* past :func:`is_secret_name`, which is checked last and wins
    unconditionally. An allowlist you can talk your way around is a denylist
    with extra steps.

    ``tmpdir``, when given, rewrites all three of ``TMPDIR``/``TMP``/``TEMP``,
    so a caller that hands the child a sandbox-writable scratch directory
    doesn't have to remember which spelling this particular tool reads.
    """

    src = os.environ if base is None else base
    extra = {str(n).upper() for n in extra_pass}

    out: dict[str, str] = {}
    for name, value in src.items():
        name = str(name)
        # The secret check comes last on purpose: it is the one rule that
        # `extra_pass` cannot override.
        if not _allowed(name, extra) or is_secret_name(name):
            continue
        out[name] = "" if value is None else str(value)

    # A child with no PATH fails to exec with a message about the binary, not
    # about the sandbox — which reads as a mystery bug rather than a policy.
    if not any(k.upper() == "PATH" and v for k, v in out.items()):
        out["PATH"] = os.defpath or "/usr/bin:/bin"

    if tmpdir:
        for name in _TMP_NAMES:
            out[name] = str(tmpdir)

    return out
