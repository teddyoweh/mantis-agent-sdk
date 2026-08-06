"""``mantis update`` — bring this install up to the latest published release.

The hard part isn't the upgrade, it's knowing *which* upgrade. mantis can be
installed four ways and running the wrong updater is worse than running none:

===========  ==============================================  ====================
Install      How it's recognised                             How it updates
===========  ==============================================  ====================
``source``   ``direct_url.json`` says ``editable``, or the    ``git pull`` — by
             package root holds ``.git`` + ``pyproject.toml`` hand, never by us
``uv``       interpreter lives under a uv tools dir           ``uv tool upgrade``
``pipx``     interpreter lives under a pipx venvs dir         ``pipx upgrade``
``pip``      anything else                                    ``pip install -U``
===========  ==============================================  ====================

An editable install is somebody's working checkout, and this project's checkout
routinely carries a large uncommitted tree. Running ``git pull`` on it from a
tool invocation could conflict against unstaged work, so :func:`run_update`
prints the two commands and stops rather than touching the repo. That refusal is
the feature — the other three modes update in place without asking, because the
command name is the consent.

Nothing here is Anthropic-specific: the version comes from PyPI's JSON API and
the comparison is a self-contained PEP 440 subset, so ``mantis update`` works on
a box with no ``packaging`` installed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess  # noqa: S404 — the whole module is "run the right installer"
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Install",
    "current_version",
    "detect_install",
    "is_newer",
    "latest_version",
    "parse_version",
    "run_update",
]

#: The distribution name on PyPI. The import package is ``mantis_agent``; the
#: two differ and passing the wrong one to pip installs nothing at all.
DIST_NAME = "mantis-agent-sdk"

#: What we ask pip/uv for. The ``cli`` extra is what makes the `mantis` terminal
#: work, so an upgrade that dropped it would "succeed" and leave a worse install.
DIST_SPEC = "mantis-agent-sdk[cli]"

PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def current_version() -> str:
    """The running mantis version, or ``"0"`` if metadata is unreadable."""

    try:
        from . import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:  # noqa: BLE001 — a broken install must still be updatable
        return "0"


def parse_version(v: str) -> tuple:
    """A sortable key for a version string — a small PEP 440 subset.

    Handles ``2.61.0``, ``2.61``, ``2.61.0rc1``, ``2.61.0.post1``. The release
    segment sorts numerically (so 2.9 < 2.61, which a string compare gets
    backwards — the bug that would make an update look unnecessary), and a
    prerelease sorts *below* the same release, so 2.61.0rc1 < 2.61.0.

    ``packaging`` does this properly and is used when present; this exists
    because it is not one of mantis's declared dependencies.
    """

    try:
        from packaging.version import Version  # noqa: PLC0415

        return (1, Version(v))
    except Exception:  # noqa: BLE001 — no packaging, or unparseable → fallback
        pass

    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)(.*)$", str(v))
    if not m:
        return (0, (), 0, ())
    release = tuple(int(x) for x in m.group(1).split("."))
    suffix = m.group(2).lower().strip()
    # 0 = prerelease (sorts first), 1 = final, 2 = post-release.
    if re.match(r"^[-._]?(a|b|c|rc|alpha|beta|pre|dev)", suffix):
        rank = 0
    elif re.match(r"^[-._]?post", suffix):
        rank = 2
    else:
        rank = 1
    nums = tuple(int(x) for x in re.findall(r"\d+", suffix))
    return (0, release, rank, nums)


def is_newer(candidate: str, than: str) -> bool:
    """True when ``candidate`` is a strictly later version than ``than``."""

    try:
        return parse_version(candidate) > parse_version(than)
    except TypeError:
        # Mixed fallback/packaging keys can't compare — treat as "no update"
        # rather than pushing a reinstall the user didn't need.
        return False


def latest_version(*, timeout: float = 10.0) -> tuple[str | None, str]:
    """Ask PyPI for the newest release. Returns ``(version, detail)``.

    ``version`` is None on any failure, with ``detail`` naming the reason —
    being offline is a normal outcome for this command, not a crash.
    """

    try:
        import httpx  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None, "httpx is not available to query PyPI"
    try:
        r = httpx.get(PYPI_JSON_URL, timeout=timeout,
                      headers={"accept": "application/json"})
    except Exception as e:  # noqa: BLE001 — offline / DNS / TLS
        return None, f"could not reach PyPI ({type(e).__name__})"
    if r.status_code != 200:
        return None, f"PyPI returned HTTP {r.status_code}"
    try:
        version = str(r.json()["info"]["version"])
    except Exception:  # noqa: BLE001
        return None, "PyPI response was not in the expected shape"
    return version, ""


# ---------------------------------------------------------------------------
# Install detection
# ---------------------------------------------------------------------------


@dataclass
class Install:
    """How this copy of mantis got here, and what would update it.

    ``command`` is None for a source checkout — see the module docstring for why
    that case is deliberately not automated.
    """

    kind: str                    # "source" | "uv" | "pipx" | "pip"
    label: str                   # human phrasing for the report line
    command: list[str] | None    # the updater to run, or None
    root: Path | None = None     # the checkout, for "source"


def _editable_root() -> Path | None:
    """The source checkout backing an editable install, if this is one."""

    try:
        from importlib.metadata import distribution  # noqa: PLC0415

        raw = distribution(DIST_NAME).read_text("direct_url.json")
        if raw:
            import json  # noqa: PLC0415

            data = json.loads(raw)
            if (data.get("dir_info") or {}).get("editable"):
                url = str(data.get("url") or "")
                if url.startswith("file://"):
                    from urllib.parse import unquote, urlparse  # noqa: PLC0415

                    return Path(unquote(urlparse(url).path))
    except Exception:  # noqa: BLE001 — no metadata → fall through to the layout check
        pass
    # No usable metadata: a package sitting next to a .git + pyproject.toml is a
    # checkout however it got onto sys.path.
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def detect_install() -> Install:
    """Classify this install and pick its updater."""

    root = _editable_root()
    if root is not None:
        return Install("source", f"editable checkout at {root}", None, root)

    # uv and pipx both put the tool in a venv under a predictable directory, so
    # the interpreter path is the tell. Check both the real prefix and the
    # executable, since a shim can live elsewhere.
    probe = f"{sys.prefix}\n{sys.executable}".replace(os.sep, "/").lower()
    if "/uv/tools/" in probe or "/uv/tool/" in probe:
        return Install("uv", "uv tool install", ["uv", "tool", "upgrade", DIST_NAME])
    if "/pipx/venvs/" in probe:
        return Install("pipx", "pipx install", ["pipx", "upgrade", DIST_NAME])

    # A uv-created venv normally has no pip in it at all, so `-m pip` would die
    # with "No module named pip" and strand the user on a correct-but-useless
    # error. uv can install into that same interpreter, so prefer it when pip is
    # genuinely absent. `--python` targets this interpreter explicitly rather
    # than relying on $VIRTUAL_ENV being set.
    if not _has_pip() and shutil.which("uv"):
        return Install(
            "uv-pip", "venv without pip (using uv)",
            ["uv", "pip", "install", "--python", sys.executable, "-U", DIST_SPEC],
        )
    return Install(
        "pip", "pip install",
        [sys.executable, "-m", "pip", "install", "-U", DIST_SPEC],
    )


def _has_pip() -> bool:
    """Whether ``python -m pip`` will work in *this* interpreter."""

    try:
        import importlib.util  # noqa: PLC0415

        return importlib.util.find_spec("pip") is not None
    except Exception:  # noqa: BLE001
        return False


def _installed_version_fresh() -> str | None:
    """Re-read the installed version in a *new* interpreter.

    The running process imported mantis before the upgrade, so its own
    ``__version__`` still reports the old number — asking it would make every
    successful update look like it did nothing.
    """

    try:
        r = subprocess.run(  # noqa: S603
            [sys.executable, "-c",
             "import importlib.metadata as m;print(m.version('mantis-agent-sdk'))"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001
        return None
    out = (r.stdout or "").strip()
    return out or None


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def run_update(argv: list[str]) -> int:
    """``mantis update`` — check for a newer release and install it.

    Exit codes: 0 = already current, or updated; 1 = an update is available but
    was not applied (source checkout, or the updater failed).
    """

    ap = argparse.ArgumentParser(
        prog="mantis update",
        description="Update mantis to the latest published release.")
    ap.add_argument("--check", action="store_true",
                    help="report the available version and exit without installing")
    ap.add_argument("--force", action="store_true",
                    help="run the updater even when already on the latest version")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="seconds to wait on PyPI (default 10)")
    args = ap.parse_args(argv)

    cur = current_version()
    install = detect_install()
    latest, detail = latest_version(timeout=args.timeout)

    print(f"mantis {cur}  ·  {install.label}")

    if latest is None:
        print(f"  could not determine the latest version — {detail}")
        # Not knowing is not the same as being current; --force still works so a
        # user on a flaky network can push the upgrade through anyway.
        if not args.force:
            return 1
    elif is_newer(latest, cur):
        print(f"  update available: {cur} → {latest}")
    elif is_newer(cur, latest):
        # A local build ahead of PyPI — saying "already on the latest release
        # (2.61.0)" while running 2.62.0 reads like the check misfired.
        print(f"  ahead of the published release ({latest}) — nothing to update")
        if not args.force:
            return 0
    else:
        print(f"  already on the latest release ({latest})")
        if not args.force:
            return 0

    if args.check:
        return 0 if latest is None or not is_newer(latest, cur) else 1

    if install.kind == "source":
        # Deliberately not automated — this checkout may carry uncommitted work.
        print("\n  this is an editable install from a source checkout, so mantis")
        print("  won't touch it. Update it yourself with:")
        print(f"    git -C {install.root} pull")
        print(f"    uv tool install --force --editable '{install.root}[cli]'")
        return 1

    assert install.command is not None  # noqa: S101 — only "source" has None
    print(f"\n  $ {' '.join(install.command)}")
    try:
        r = subprocess.run(install.command, timeout=600)  # noqa: S603
    except FileNotFoundError:
        print(f"\n  {install.command[0]} is not on PATH — install it, or update "
              f"manually with:\n    pip install -U '{DIST_SPEC}'")
        return 1
    except subprocess.TimeoutExpired:
        print("\n  the updater timed out after 10 minutes")
        return 1
    if r.returncode != 0:
        print(f"\n  updater exited {r.returncode} — nothing was changed")
        return 1

    now = _installed_version_fresh()
    if now and is_newer(now, cur):
        print(f"\n  updated {cur} → {now} · restart mantis to pick it up")
    elif now:
        print(f"\n  now on {now}")
    else:
        print("\n  update finished · restart mantis to pick it up")
    return 0
