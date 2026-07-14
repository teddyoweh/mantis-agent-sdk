"""``mantis-agent setup-local`` — get a working CPU-runnable model in <2 min.

The CLI surface is ``mantis-agent setup-local`` (see :mod:`mantis_agent.cli`).
This module owns the actual logic: installer detection, the curated
CPU-friendly model list, the pull, and a smoke test.

Bar for "CPU-friendly" used here: runs at ≥ 3 tok/s on a 2020-era
laptop with 8 GB RAM and no discrete GPU. Models exceeding 7 B
parameters are excluded — they technically run on CPU but are too slow
to be useful for an agent loop.

The list is intentionally short. Users who want exotic models can run
``ollama pull <name>`` themselves; this helper is for the first-five-
minutes user who just wants something working.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

__all__ = [
    "LocalModel",
    "CPU_FRIENDLY_MODELS",
    "DEFAULT_RECOMMENDATION",
    "is_ollama_installed",
    "is_ollama_running",
    "start_ollama_server",
    "install_ollama",
    "install_ollama_windows",
    "WINDOWS_INSTALLER_URL",
    "WINDOWS_DEFAULT_INSTALL_DIR",
    "pull_model",
    "smoke_test",
    "run_setup_local",
]


# ---------------------------------------------------------------------------
# Windows install constants
# ---------------------------------------------------------------------------
#
# Ollama ships an Inno Setup .exe — these are the silent-install flags it
# documents:
#
#   /VERYSILENT        — no progress UI, no message boxes
#   /SUPPRESSMSGBOXES  — auto-answer Yes on any box that would still appear
#   /NORESTART         — never reboot afterwards (we don't ship reboot UX)
#   /SP-               — skip the "this will install … Do you want to
#                        continue?" preamble shown by /SILENT alone
#
# Together these are the same flags Microsoft Winget and Chocolatey use to
# automate Inno Setup installers.  See:
#   https://jrsoftware.org/ishelp/index.php?topic=setupcmdline

WINDOWS_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
WINDOWS_INSTALLER_FLAGS: tuple[str, ...] = (
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/SP-",
)
# Default user-mode install dir for OllamaSetup.exe. Prepended to PATH
# after a successful install so the *current* Python process can call
# ``ollama`` immediately (the installer's HKCU PATH edit only reaches
# *future* shells).
WINDOWS_DEFAULT_INSTALL_DIR = r"%LOCALAPPDATA%\Programs\Ollama"


# ---------------------------------------------------------------------------
# Curated CPU-friendly model list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalModel:
    """A model we recommend for CPU-only use, with the install one-liner."""

    tag: str                # Ollama tag — what `ollama pull X` accepts
    params: str             # Human-readable param count ("1.5B", "3B", ...)
    size_gb: float          # On-disk size after Q4 quantization
    min_ram_gb: int         # Minimum RAM to run without swapping
    family: str             # Llama / Qwen / DeepSeek / Phi / Gemma / TinyLlama
    tools: bool             # Does the model emit usable tool calls?
    thinking: bool          # Does it emit <think>...</think> reasoning blocks?
    notes: str              # One-line "when to use this"


# Ordered from smallest → largest. The default recommendation is the
# smallest that still has tool-use chops; the user can pick larger if
# their machine handles it.
CPU_FRIENDLY_MODELS: list[LocalModel] = [
    LocalModel(
        tag="smollm2:135m",
        params="135M",
        size_gb=0.27,
        min_ram_gb=2,
        family="SmolLM2",
        tools=False,
        thinking=False,
        notes="Tiny — chat only, no tool use. Sanity-check installs.",
    ),
    LocalModel(
        tag="qwen2.5:0.5b",
        params="0.5B",
        size_gb=0.4,
        min_ram_gb=2,
        family="Qwen",
        tools=True,
        thinking=False,
        notes="Smallest Qwen that does tool calls. Fast on anything.",
    ),
    LocalModel(
        tag="tinyllama:1.1b",
        params="1.1B",
        size_gb=0.64,
        min_ram_gb=2,
        family="TinyLlama",
        tools=False,
        thinking=False,
        notes="Famously light — chat only. Good RAM-constrained pick.",
    ),
    LocalModel(
        tag="qwen2.5:1.5b",
        params="1.5B",
        size_gb=1.0,
        min_ram_gb=4,
        family="Qwen",
        tools=True,
        thinking=False,
        notes="Best 1.5B all-rounder for agent loops.",
    ),
    LocalModel(
        tag="deepseek-r1:1.5b",
        params="1.5B",
        size_gb=1.1,
        min_ram_gb=4,
        family="DeepSeek",
        tools=True,
        thinking=True,
        notes="Reasoning model — emits <think> blocks. Slowest of the 1.5B class.",
    ),
    LocalModel(
        tag="llama3.2:1b",
        params="1.2B",
        size_gb=1.3,
        min_ram_gb=4,
        family="Llama",
        tools=True,
        thinking=False,
        notes="Meta's 1B — native tool calls, sharper than 0.5B Qwen.",
    ),
    LocalModel(
        tag="gemma2:2b",
        params="2B",
        size_gb=1.6,
        min_ram_gb=4,
        family="Gemma",
        tools=False,
        thinking=False,
        notes="Google's small one — chat only, very polished prose.",
    ),
    LocalModel(
        tag="qwen2.5:3b",
        params="3B",
        size_gb=1.9,
        min_ram_gb=6,
        family="Qwen",
        tools=True,
        thinking=False,
        notes="Same class as Llama 3.2 3B — pick whichever feels better.",
    ),
    LocalModel(
        tag="llama3.2:3b",
        params="3.2B",
        size_gb=2.0,
        min_ram_gb=6,
        family="Llama",
        tools=True,
        thinking=False,
        notes="Meta's 3B — solid default if you have 8 GB RAM.",
    ),
    LocalModel(
        tag="phi3.5:3.8b",
        params="3.8B",
        size_gb=2.2,
        min_ram_gb=6,
        family="Phi",
        tools=True,
        thinking=False,
        notes="MS Phi-3.5 — strong reasoning for size; tool calls work.",
    ),
    LocalModel(
        tag="qwen2.5:7b",
        params="7B",
        size_gb=4.7,
        min_ram_gb=8,
        family="Qwen",
        tools=True,
        thinking=False,
        notes="Ceiling for CPU — works, but slow without GPU/Apple Silicon.",
    ),
    LocalModel(
        tag="llama3.1:8b",
        params="8B",
        size_gb=4.9,
        min_ram_gb=8,
        family="Llama",
        tools=True,
        thinking=False,
        notes="At the edge of CPU usability — fine on M-series, slow elsewhere.",
    ),
]


# The default pick: small enough to run anywhere with 4 GB free RAM,
# big enough to do tool calls in our agent loop. Bias toward speed for
# the first-time user.
DEFAULT_RECOMMENDATION = "qwen2.5:1.5b"


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------


def is_ollama_installed() -> bool:
    """Is the ``ollama`` binary on PATH?"""
    return shutil.which("ollama") is not None


def is_ollama_running(base_url: str | None = None) -> bool:
    """Does the local Ollama HTTP server answer ``/api/version``? Honors
    ``$OLLAMA_HOST`` when no explicit ``base_url`` is given."""
    from .paths import ollama_base_url  # noqa: PLC0415
    base_url = base_url or ollama_base_url()
    try:
        with urllib.request.urlopen(f"{base_url}/api/version", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def start_ollama_server(
    *,
    base_url: str | None = None,
    timeout_s: float = 15.0,
    log_path: str | None = None,
) -> tuple[bool, str | None]:
    """Spawn ``ollama serve`` in the background and wait for it to answer.

    Returns ``(ok, log_path)``. If ollama is already running, returns
    ``(True, None)`` without spawning anything. If ollama is not on PATH,
    returns ``(False, None)``. On failure to come up within ``timeout_s``,
    returns ``(False, log_path)`` so callers can surface the captured log.

    The server is spawned **detached** from this process group so that the
    Python process exiting (e.g. ``setup-local`` finishing) does not kill
    the daemon the user is about to use. Stdout/stderr are tee'd to
    ``log_path`` (a tempfile by default) so the user can read what went
    wrong if the spawn fails.
    """
    from .paths import ollama_base_url  # noqa: PLC0415
    base_url = base_url or ollama_base_url()

    if is_ollama_running(base_url):
        return True, None

    if not is_ollama_installed():
        return False, None

    if log_path is None:
        fd, log_path = tempfile.mkstemp(prefix="mantis-agent-ollama-", suffix=".log")
        os.close(fd)

    # Detach so the daemon outlives this Python process. On POSIX we use
    # ``start_new_session`` (its own process group + session leader). On
    # Windows we use ``DETACHED_PROCESS`` + ``CREATE_NEW_PROCESS_GROUP``.
    # Popen dups this fd into the child, so the parent must close its own copy
    # afterward — otherwise every call leaks one open file handle.
    log_fh = open(log_path, "ab")
    popen_kwargs: dict[str, object] = {
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform.startswith("win"):
        # CREATE_NEW_PROCESS_GROUP = 0x00000200, DETACHED_PROCESS = 0x00000008
        popen_kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(["ollama", "serve"], **popen_kwargs)  # noqa: S603
    except (OSError, ValueError) as e:
        # Best-effort: stash the error so the caller can surface it.
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[mantis-agent] failed to spawn `ollama serve`: {e}\n")
        except OSError:
            pass
        return False, log_path
    finally:
        log_fh.close()

    # Poll until the server answers or we hit the deadline. We sleep in
    # small slices so a fast cold-start (~1s on warm caches) doesn't pay
    # the full timeout.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_ollama_running(base_url):
            return True, log_path
        time.sleep(0.4)

    return False, log_path


# ---------------------------------------------------------------------------
# Install + pull
# ---------------------------------------------------------------------------


def install_ollama(*, auto_confirm: bool = False) -> int:
    """Install Ollama via the official installer for the current platform.

    * Linux / macOS: fetches ``https://ollama.com/install.sh`` and runs it
      via ``sh``.
    * Windows: dispatches to :func:`install_ollama_windows` which
      downloads ``OllamaSetup.exe`` and runs it with Inno Setup's
      silent-install flags.

    Returns the exit code of the underlying installer. ``0`` = success.
    """

    if sys.platform.startswith("win"):
        return install_ollama_windows(auto_confirm=auto_confirm)

    if not auto_confirm:
        print("This will run: curl -fsSL https://ollama.com/install.sh | sh")
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 1
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    # We don't shell `curl | sh` directly because we want to surface
    # errors cleanly. Two-step: download, then exec.
    try:
        with urllib.request.urlopen(
            "https://ollama.com/install.sh", timeout=30
        ) as r:
            script = r.read().decode("utf-8")
    except urllib.error.URLError as e:
        print(f"Failed to fetch installer: {e}", file=sys.stderr)
        return 1

    return subprocess.call(["sh", "-c", script])


# ---------------------------------------------------------------------------
# Windows-specific installer
# ---------------------------------------------------------------------------


def _safe_unlink(path: str) -> None:
    """``os.unlink`` that swallows OSError — best-effort cleanup."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _prepend_to_process_path(directory: str) -> None:
    """Prepend ``directory`` to ``os.environ['PATH']`` for the current process.

    OllamaSetup.exe writes the install dir into HKCU\\Environment\\Path
    via the registry, which only reaches *new* shells. The Python process
    that just called ``install_ollama_windows`` keeps its old PATH, so a
    follow-up ``shutil.which('ollama')`` would still return None. We patch
    the in-process env so the rest of ``run_setup_local`` (pull, smoke
    test) can find the binary without re-execing.
    """

    expanded = os.path.expandvars(directory)
    if not expanded or not os.path.isdir(expanded):
        # Nothing to add — installer ran but the expected dir isn't there
        # (custom install path, sandbox quirk, etc.). Leave PATH alone.
        return
    sep = os.pathsep
    existing = os.environ.get("PATH", "")
    parts = existing.split(sep) if existing else []
    # Case-insensitive on Windows; canonical-form check.
    canonical = os.path.normcase(os.path.normpath(expanded))
    for p in parts:
        if os.path.normcase(os.path.normpath(p)) == canonical:
            return  # already on PATH
    os.environ["PATH"] = expanded + (sep + existing if existing else "")


def install_ollama_windows(
    *,
    auto_confirm: bool = False,
    download_url: str = WINDOWS_INSTALLER_URL,
    installer_path: str | None = None,
    timeout_s: float = 300.0,
    install_dir: str = WINDOWS_DEFAULT_INSTALL_DIR,
) -> int:
    """Install Ollama on Windows by running the official ``OllamaSetup.exe``.

    Downloads the installer from ``download_url`` to a temp file, runs it
    with :data:`WINDOWS_INSTALLER_FLAGS` (Inno Setup silent flags), and
    returns the installer's exit code. ``0`` is success.

    The installer's HKCU PATH edit doesn't reach the currently running
    Python process, so after a successful install this function also
    prepends ``install_dir`` (expanded) to ``os.environ['PATH']``. That
    way the rest of ``run_setup_local`` (which calls ``shutil.which`` and
    ``ollama pull``) sees the new binary without the user having to
    relaunch their shell.

    Parameters
    ----------
    auto_confirm:
        Skip the interactive "this will download X — proceed?" prompt.
        Matches the POSIX :func:`install_ollama` contract.
    download_url:
        Where to fetch ``OllamaSetup.exe``. Overridable so tests don't
        hit the network and so air-gapped users can point at a mirror.
    installer_path:
        Path to a pre-downloaded ``OllamaSetup.exe``. When set, the
        download step is skipped entirely. Useful for offline installs
        and for tests that don't want to mock urllib.
    timeout_s:
        How long to wait for the installer to finish. Default 5 minutes —
        Inno Setup silent installs on a fresh machine typically take
        20–60 seconds, but anti-virus scanning can stretch this.
    install_dir:
        Directory the installer drops ``ollama.exe`` into. Defaults to
        ``%LOCALAPPDATA%\\Programs\\Ollama``, the per-user default Ollama
        ships with. Pass an absolute path if the user picked ``/DIR=``
        when running interactively before.
    """

    if not auto_confirm:
        if installer_path:
            print(f"This will run: {installer_path}")
        else:
            print(f"This will download and run: {download_url}")
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 1
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    cleanup_path: str | None = None
    exe_path: str
    if installer_path is None:
        fd, tmp_path = tempfile.mkstemp(prefix="OllamaSetup-", suffix=".exe")
        os.close(fd)
        cleanup_path = tmp_path
        exe_path = tmp_path
        print(f"Downloading {download_url} …")
        try:
            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": "mantis-agent-sdk/setup-local"},
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = r.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
        except (urllib.error.URLError, OSError) as e:
            print(f"Failed to fetch installer: {e}", file=sys.stderr)
            _safe_unlink(cleanup_path)
            return 1
    else:
        exe_path = installer_path
        if not os.path.isfile(exe_path):
            print(
                f"Installer not found at {exe_path}. "
                "Pass --installer to point at a real OllamaSetup.exe.",
                file=sys.stderr,
            )
            return 1

    print(f"Running {exe_path} {' '.join(WINDOWS_INSTALLER_FLAGS)} …")
    try:
        # We deliberately do NOT pass shell=True: the args are a list and
        # the .exe is the program. shell=True would be a binhandling
        # liability on Windows (cmd.exe quoting rules).
        rc = subprocess.call(  # noqa: S603
            [exe_path, *WINDOWS_INSTALLER_FLAGS],
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Installer did not finish within {timeout_s:.0f}s. "
            "Run it manually if needed.",
            file=sys.stderr,
        )
        rc = 1
    except OSError as e:
        print(f"Failed to launch installer: {e}", file=sys.stderr)
        rc = 1
    finally:
        if cleanup_path is not None:
            _safe_unlink(cleanup_path)

    if rc == 0:
        _prepend_to_process_path(install_dir)
        print("Ollama installed.")
    else:
        print(
            f"Installer exited with code {rc}. "
            "Check the OllamaSetup.exe logs (Start → Run → %TEMP%) "
            "for details.",
            file=sys.stderr,
        )
    return rc


def pull_model(tag: str) -> int:
    """``ollama pull <tag>`` — stream output to the user's terminal."""

    if not is_ollama_installed():
        print(
            "ollama is not on PATH. Run `mantis-agent setup-local --install-ollama`.",
            file=sys.stderr,
        )
        return 1

    print(f"Pulling {tag} …")
    return subprocess.call(["ollama", "pull", tag])


def smoke_test(tag: str, base_url: str | None = None) -> bool:
    """Send a one-token prompt to confirm the model loads and responds."""
    from .paths import ollama_base_url  # noqa: PLC0415
    base_url = base_url or ollama_base_url()

    body = json.dumps(
        {
            "model": tag,
            "prompt": "Reply with one word: ok",
            "stream": False,
            "options": {"num_predict": 8},
        }
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read().decode())
            text = (payload.get("response") or "").strip()
            print(f"  smoke-test response: {text[:120]!r}")
            return bool(text)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  smoke-test failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Orchestrator (called by ``mantis-agent setup-local``)
# ---------------------------------------------------------------------------


def run_setup_local(
    *,
    model: str | None = None,
    install_ollama_if_missing: bool = False,
    skip_smoke_test: bool = False,
    base_url: str | None = None,
    auto_start_server: bool = True,
    start_timeout_s: float = 15.0,
) -> int:
    """End-to-end: install Ollama if needed → pull the model → smoke test.

    By default this command will also **start the Ollama server itself**
    when ollama is installed but the daemon isn't running yet. The whole
    point of ``setup-local`` is to get the user from zero to a working
    model with one command; making them go run ``ollama serve`` in a
    second terminal defeats the purpose. Pass ``auto_start_server=False``
    (CLI: ``--no-auto-start-server``) if you'd rather keep that behavior.
    """
    from .paths import ollama_base_url  # noqa: PLC0415
    base_url = base_url or ollama_base_url()

    if not is_ollama_installed():
        if not install_ollama_if_missing:
            print("Ollama is not installed.")
            print("Run: mantis-agent setup-local --install-ollama")
            return 1
        rc = install_ollama()
        if rc != 0:
            return rc

    if not is_ollama_running(base_url):
        if not auto_start_server:
            print(f"Ollama server not responding at {base_url}.")
            print("Start it with: `ollama serve` (or restart the Ollama app).")
            return 1

        print(f"Ollama server not running at {base_url} — starting it now …")
        ok, log_path = start_ollama_server(
            base_url=base_url, timeout_s=start_timeout_s
        )
        if ok:
            print("  ollama serve is up.")
        else:
            print(
                f"Failed to start `ollama serve` within {start_timeout_s:.0f}s.",
                file=sys.stderr,
            )
            if log_path:
                print(f"  server log: {log_path}", file=sys.stderr)
            print(
                "  Start it manually with `ollama serve` (or restart the Ollama app), "
                "then re-run `mantis-agent setup-local`.",
                file=sys.stderr,
            )
            return 1

    tag = model or DEFAULT_RECOMMENDATION
    # Validate the tag against our curated list when the user passed one,
    # but allow anything — we don't want to gate on a hardcoded allowlist.
    known = {m.tag: m for m in CPU_FRIENDLY_MODELS}
    if tag in known:
        m = known[tag]
        print(f"Selected: {m.tag}  ({m.params}, ~{m.size_gb:.1f} GB, RAM ≥ {m.min_ram_gb} GB)")
        print(f"  {m.notes}")
    else:
        print(f"Selected: {tag}  (not in curated list — proceeding anyway)")

    rc = pull_model(tag)
    if rc != 0:
        return rc

    if skip_smoke_test:
        print("Done.")
        return 0

    print("Verifying with a tiny prompt …")
    if not smoke_test(tag, base_url):
        print("Smoke test failed — pull succeeded but the model didn't respond.")
        return 1

    print()
    print(f"All set. Use {tag!r} in your code:")
    print()
    print("    from mantis_agent import query, MantisAgentOptions, tool")
    print("    async for msg in query(")
    print(f"        prompt=\"hi\", options=MantisAgentOptions(model={tag!r}),")
    print("    ):")
    print("        print(msg)")
    print()
    return 0


def print_model_table() -> None:
    """Pretty-print the CPU-friendly model catalog. Used by
    ``mantis-agent setup-local --list``."""

    name_w = max(len(m.tag) for m in CPU_FRIENDLY_MODELS) + 2
    fmt = (
        f"{{tag:<{name_w}}} {{params:>7}} {{size:>8}} {{ram:>7}} "
        "{tools:^7} {think:^7}  {notes}"
    )
    print(
        fmt.format(
            tag="MODEL", params="PARAMS", size="SIZE", ram="RAM",
            tools="TOOLS", think="THINK", notes="NOTES",
        )
    )
    print("-" * (name_w + 70))
    for m in CPU_FRIENDLY_MODELS:
        print(
            fmt.format(
                tag=m.tag,
                params=m.params,
                size=f"{m.size_gb:.1f}GB",
                ram=f"{m.min_ram_gb}GB+",
                tools="yes" if m.tools else "no",
                think="yes" if m.thinking else "no",
                notes=m.notes,
            )
        )
    print()
    print(f"Default recommendation: {DEFAULT_RECOMMENDATION}")
