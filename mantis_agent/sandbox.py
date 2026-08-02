"""OS-level confinement for shell commands.

The permission system asks a human before a dangerous command runs. That works
when a human is watching — but `--godmode`, `/goal`, `mantis -p` in CI and
scheduled runs all exist precisely so nobody has to be. For those, "we asked"
is not a safety story; the safety has to be structural.

So: wrap the command in the OS's own sandbox. macOS has Seatbelt
(``sandbox-exec``), Linux has bubblewrap (``bwrap``). Both are already on the
box — no daemon, no container, no new dependency. The default policy lets a
command read the machine and write only where work belongs (the project, plus
temp), which is exactly the blast radius people assume they have.

    from mantis_agent.sandbox import SandboxPolicy, wrap_command

    argv = wrap_command(["bash", "-lc", "rm -rf /"], SandboxPolicy(), cwd=".")

Configure it in ``settings.json``::

    {"sandbox": {"enabled": true,
                 "writableRoots": ["/extra/path"],
                 "network": true,
                 "failIfUnavailable": false}}

``enabled`` defaults to off, because silently changing what a shell command can
do would be a nasty surprise; the terminal turns it on for unattended runs and
``/sandbox`` toggles it interactively.
"""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SandboxPolicy",
    "SandboxUnavailable",
    "available_backend",
    "load_policy",
    "sandbox_status",
    "wrap_command",
]


class SandboxUnavailable(RuntimeError):
    """No usable sandbox backend on this machine."""


@dataclass
class SandboxPolicy:
    """What a sandboxed command may do.

    ``writable_roots`` is the whole story for filesystem safety: everything
    else on the disk is readable but read-only. ``network`` is separate because
    turning it off breaks `pip install` and `git push`, which is a legitimate
    thing to want and a legitimate thing not to want.
    """

    enabled: bool = False
    writable_roots: list[str] = field(default_factory=list)
    network: bool = True
    fail_if_unavailable: bool = False

    def roots_for(self, cwd: str | None = None) -> list[str]:
        """Absolute, de-duplicated writable roots for a run in ``cwd``."""
        out: list[str] = []
        for raw in [cwd or os.getcwd(), *self.writable_roots,
                    tempfile.gettempdir(), "/dev/null"]:
            try:
                p = str(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError):
                continue
            if p not in out:
                out.append(p)
        return out


def _env_flag(name: str) -> bool | None:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return None
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_policy() -> SandboxPolicy:
    """The policy from settings.json (all layers), with safe defaults.

    ``MANTIS_SANDBOX`` / ``MANTIS_SANDBOX_NETWORK`` override the file, so a
    ``--sandbox`` flag applies to the whole process tree — subagents and
    background shells included — without threading a policy object through
    every call site.
    """
    raw: dict = {}
    try:
        from .settings import SETTING_SOURCES, load_settings  # noqa: PLC0415

        cfg = (load_settings(SETTING_SOURCES) or {}).get("sandbox")
        if isinstance(cfg, dict):
            raw = cfg
    except Exception:  # noqa: BLE001 — bad settings must not break the shell
        raw = {}
    roots = raw.get("writableRoots") or raw.get("writable_roots") or []
    if isinstance(roots, str):
        roots = [roots]
    enabled = _env_flag("MANTIS_SANDBOX")
    network = _env_flag("MANTIS_SANDBOX_NETWORK")
    return SandboxPolicy(
        enabled=bool(raw.get("enabled", False)) if enabled is None else enabled,
        writable_roots=[str(r) for r in roots if str(r).strip()],
        network=bool(raw.get("network", True)) if network is None else network,
        fail_if_unavailable=bool(raw.get("failIfUnavailable")
                                 or raw.get("fail_if_unavailable")),
    )


def available_backend() -> str | None:
    """``"seatbelt"``, ``"bubblewrap"``, or ``None`` on this machine."""
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if system == "Linux" and shutil.which("bwrap"):
        return "bubblewrap"
    return None


def unavailable_reason() -> str:
    """Why there's no sandbox here, phrased as something the user can act on."""
    system = platform.system()
    if system == "Darwin":
        return "sandbox-exec is missing from this macOS install"
    if system == "Linux":
        return ("bubblewrap is not installed — `apt install bubblewrap` or "
                "`dnf install bubblewrap`")
    return f"no sandbox backend for {system or 'this platform'}"


# ---------------------------------------------------------------------------
# macOS — Seatbelt
# ---------------------------------------------------------------------------
#
# Deny writes by default, then allow the roots back. Reads stay open: an agent
# that can't read /usr/lib or the Python install can't run anything useful, and
# reading was never the scary half.


def _quote_sb(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(policy: SandboxPolicy, cwd: str | None = None) -> str:
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
    ]
    for root in policy.roots_for(cwd):
        lines.append(f"(allow file-write* (subpath {_quote_sb(root)}))")
    # A shell needs its own scratch: ttys, /dev/null, and the process's own
    # temp files are writes too.
    lines.append('(allow file-write* (regex #"^/dev/(null|zero|tty|stdout|stderr|fd/.*)$"))')
    lines.append("(allow file-write-data (literal \"/dev/null\"))")
    if not policy.network:
        lines.append("(deny network*)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Linux — bubblewrap
# ---------------------------------------------------------------------------
#
# Bind the whole filesystem read-only, then bind the writable roots back
# read-write. --die-with-parent means a sandboxed process can't outlive the
# agent that started it.


def bubblewrap_argv(policy: SandboxPolicy, cwd: str | None = None) -> list[str]:
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--die-with-parent"]
    for root in policy.roots_for(cwd):
        if root == "/dev/null":
            continue
        if os.path.exists(root):
            argv += ["--bind", root, root]
    tmp = tempfile.gettempdir()
    if tmp not in policy.roots_for(cwd):
        argv += ["--bind", tmp, tmp]
    if not policy.network:
        argv.append("--unshare-net")
    if cwd:
        argv += ["--chdir", cwd]
    return argv


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def wrap_command(argv: list[str], policy: SandboxPolicy | None = None,
                 *, cwd: str | None = None) -> list[str]:
    """``argv`` wrapped so the OS confines it. Returns it unchanged when the
    policy is off.

    Raises :class:`SandboxUnavailable` when confinement was required but no
    backend exists — ``fail_if_unavailable`` is the switch that decides whether
    a missing sandbox is a warning or a refusal, and refusing loudly beats
    running unconfined while a config file claims otherwise.
    """
    policy = policy or load_policy()
    if not policy.enabled:
        return argv
    backend = available_backend()
    if backend is None:
        if policy.fail_if_unavailable:
            raise SandboxUnavailable(unavailable_reason())
        return argv
    if backend == "seatbelt":
        return ["sandbox-exec", "-p", seatbelt_profile(policy, cwd), *argv]
    return [*bubblewrap_argv(policy, cwd), *argv]


def sandbox_status(policy: SandboxPolicy | None = None,
                   cwd: str | None = None) -> dict[str, object]:
    """A description of what's in force, for ``/sandbox`` and ``/status``."""
    policy = policy or load_policy()
    backend = available_backend()
    return {
        "enabled": policy.enabled,
        "backend": backend,
        "active": bool(policy.enabled and backend),
        "reason": None if backend else unavailable_reason(),
        "writable": policy.roots_for(cwd),
        "network": policy.network,
        "fail_if_unavailable": policy.fail_if_unavailable,
    }
