"""``mantis-agent setup-local-llamacpp`` — the llama.cpp alternative.

The Ollama path (:mod:`mantis_agent.setup_local`) is the default for
first-time users — it's a one-binary install with its own model registry
and the smoothest "from zero to working agent in 2 minutes" experience.

This module ships the *other* path. Some users would rather:

* Run :program:`llama-server` directly without an extra daemon.
* Pull GGUFs straight from HuggingFace instead of through Ollama's
  re-hosted catalog (newer quants, more variety).
* Match a setup their company already standardized on.

So we mirror the same lifecycle — detect → install instructions →
download → start the server → smoke test — but pointed at llama.cpp.
We deliberately keep the API parallel to :mod:`setup_local` so the CLI
glue is symmetric and either path is a one-flag swap.

What's NOT here
---------------

* No model-building, no tokenizer compilation, no source builds. The
  install path is "use the prebuilt :program:`llama-server` binary
  from your platform package manager (or the upstream releases page)".
  We're aiming at users who want a fast, working agent — not at
  someone bootstrapping a llama.cpp dev environment.
* No subprocess management beyond launching the server detached. We
  point the user at the right command line; supervising the server
  is the user's job. Same stance as :mod:`setup_local`, which never
  daemonizes :command:`ollama serve`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

__all__ = [
    "CPU_FRIENDLY_GGUF_MODELS",
    "DEFAULT_LLAMACPP_PORT",
    "DEFAULT_GGUF_RECOMMENDATION",
    "GGUFModel",
    "build_llamacpp_server_command",
    "download_gguf",
    "default_models_dir",
    "huggingface_gguf_url",
    "install_llamacpp_instructions",
    "is_llamacpp_running",
    "is_llamacpp_server_installed",
    "print_gguf_model_table",
    "run_setup_local_llamacpp",
    "smoke_test_llamacpp",
]


# ---------------------------------------------------------------------------
# Curated GGUF catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GGUFModel:
    """A GGUF file we recommend for CPU-only use with llama.cpp.

    Fields
    ------
    tag:        short human handle (e.g. ``qwen2.5-1.5b-instruct-q4_k_m``)
    hf_repo:    HuggingFace repo path (``Qwen/Qwen2.5-1.5B-Instruct-GGUF``)
    hf_filename: exact GGUF filename inside the repo
    params:     human-readable param count (``1.5B``)
    size_gb:    on-disk size of the GGUF file
    min_ram_gb: minimum RAM to load without thrashing
    family:     Llama / Qwen / DeepSeek / Phi / Gemma / SmolLM2 / TinyLlama
    tools:      can the model emit usable tool calls (with ``--jinja``)?
    thinking:   does it emit ``<think>`` reasoning blocks?
    chat_template_flag: extra ``llama-server`` flag for the chat template
                — most modern GGUFs ship the template in metadata so this
                is ``""``, but a few legacy ones need ``--chat-template``.
    notes:      one-line "when to use this"
    """

    tag: str
    hf_repo: str
    hf_filename: str
    params: str
    size_gb: float
    min_ram_gb: int
    family: str
    tools: bool
    thinking: bool
    chat_template_flag: str
    notes: str


# Ordered smallest → largest, same convention as the Ollama catalog so
# CLI consumers can iterate either list with the same expectations.
CPU_FRIENDLY_GGUF_MODELS: list[GGUFModel] = [
    GGUFModel(
        tag="smollm2-135m-instruct-q4_k_m",
        hf_repo="HuggingFaceTB/SmolLM2-135M-Instruct-GGUF",
        hf_filename="smollm2-135m-instruct-q4_k_m.gguf",
        params="135M",
        size_gb=0.10,
        min_ram_gb=2,
        family="SmolLM2",
        tools=False,
        thinking=False,
        chat_template_flag="",
        notes="Tiny — chat only. Smoke-test downloads in seconds.",
    ),
    GGUFModel(
        tag="qwen2.5-0.5b-instruct-q4_k_m",
        hf_repo="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        hf_filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        params="0.5B",
        size_gb=0.40,
        min_ram_gb=2,
        family="Qwen",
        tools=True,
        thinking=False,
        chat_template_flag="",
        notes="Smallest Qwen with tool calls. Fast on anything.",
    ),
    GGUFModel(
        tag="tinyllama-1.1b-chat-v1.0-q4_k_m",
        hf_repo="TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        hf_filename="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
        params="1.1B",
        size_gb=0.64,
        min_ram_gb=2,
        family="TinyLlama",
        tools=False,
        thinking=False,
        chat_template_flag="",
        notes="Light chat-only model. Useful when RAM is the bottleneck.",
    ),
    GGUFModel(
        tag="llama-3.2-1b-instruct-q4_k_m",
        hf_repo="bartowski/Llama-3.2-1B-Instruct-GGUF",
        hf_filename="Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        params="1.2B",
        size_gb=0.81,
        min_ram_gb=4,
        family="Llama",
        tools=True,
        thinking=False,
        chat_template_flag="",
        notes="Meta's 1B — native tool calls; sharper than 0.5B Qwen.",
    ),
    GGUFModel(
        tag="qwen2.5-1.5b-instruct-q4_k_m",
        hf_repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        hf_filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        params="1.5B",
        size_gb=0.99,
        min_ram_gb=4,
        family="Qwen",
        tools=True,
        thinking=False,
        chat_template_flag="",
        notes="Best 1.5B all-rounder for agent loops. The default pick.",
    ),
    GGUFModel(
        tag="deepseek-r1-distill-qwen-1.5b-q4_k_m",
        hf_repo="bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF",
        hf_filename="DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf",
        params="1.5B",
        size_gb=1.12,
        min_ram_gb=4,
        family="DeepSeek",
        tools=False,
        thinking=True,
        chat_template_flag="",
        notes="Reasoning model — emits <think> blocks. Slowest of the 1.5B class.",
    ),
    GGUFModel(
        tag="gemma-2-2b-it-q4_k_m",
        hf_repo="bartowski/gemma-2-2b-it-GGUF",
        hf_filename="gemma-2-2b-it-Q4_K_M.gguf",
        params="2B",
        size_gb=1.71,
        min_ram_gb=4,
        family="Gemma",
        tools=False,
        thinking=False,
        chat_template_flag="",
        notes="Google's small one — chat only, very polished prose.",
    ),
    GGUFModel(
        tag="qwen2.5-3b-instruct-q4_k_m",
        hf_repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
        hf_filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        params="3B",
        size_gb=1.93,
        min_ram_gb=6,
        family="Qwen",
        tools=True,
        thinking=False,
        chat_template_flag="",
        notes="Same class as Llama 3.2 3B — pick whichever feels better.",
    ),
    GGUFModel(
        tag="llama-3.2-3b-instruct-q4_k_m",
        hf_repo="bartowski/Llama-3.2-3B-Instruct-GGUF",
        hf_filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        params="3.2B",
        size_gb=2.02,
        min_ram_gb=6,
        family="Llama",
        tools=True,
        thinking=False,
        chat_template_flag="",
        notes="Meta's 3B — solid default once you have 8 GB RAM.",
    ),
    GGUFModel(
        tag="phi-3.5-mini-instruct-q4_k_m",
        hf_repo="bartowski/Phi-3.5-mini-instruct-GGUF",
        hf_filename="Phi-3.5-mini-instruct-Q4_K_M.gguf",
        params="3.8B",
        size_gb=2.39,
        min_ram_gb=6,
        family="Phi",
        tools=True,
        thinking=False,
        chat_template_flag="",
        notes="MS Phi-3.5 — strong reasoning for size; tool calls work.",
    ),
    GGUFModel(
        tag="qwen2.5-7b-instruct-q4_k_m",
        hf_repo="Qwen/Qwen2.5-7B-Instruct-GGUF",
        hf_filename="qwen2.5-7b-instruct-q4_k_m.gguf",
        params="7B",
        size_gb=4.68,
        min_ram_gb=8,
        family="Qwen",
        tools=True,
        thinking=False,
        chat_template_flag="",
        notes="Ceiling for CPU — works, but slow without GPU/Apple Silicon.",
    ),
    GGUFModel(
        tag="llama-3.1-8b-instruct-q4_k_m",
        hf_repo="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        hf_filename="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        params="8B",
        size_gb=4.92,
        min_ram_gb=8,
        family="Llama",
        tools=True,
        thinking=False,
        chat_template_flag="",
        notes="At the edge of CPU usability — fine on M-series, slow elsewhere.",
    ),
]


# The default pick: matches the Ollama default (qwen2.5:1.5b) — same
# parameter count, same family, so users swapping paths get the same
# behavioral target.
DEFAULT_GGUF_RECOMMENDATION = "qwen2.5-1.5b-instruct-q4_k_m"

# Standard llama.cpp server port. The CLI -p flag overrides; we pick
# 8080 because that's what every llama.cpp doc references.
DEFAULT_LLAMACPP_PORT = 8080


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------


def is_llamacpp_server_installed() -> bool:
    """Is the :program:`llama-server` binary on PATH?

    Older builds shipped it as :program:`server`; we accept either name
    so brew-installed-via-llama.cpp users aren't told to reinstall.
    """

    return shutil.which("llama-server") is not None or shutil.which("server") is not None


def is_llamacpp_running(port: int = DEFAULT_LLAMACPP_PORT, host: str = "127.0.0.1") -> bool:
    """Does a llama.cpp HTTP server answer at ``host:port``?

    Probes the OpenAI-compat ``/v1/models`` endpoint with a 2 s timeout.
    A 2xx response (or 4xx — server is up, route just refused) counts as
    "running"; only connection errors are treated as "not running".
    """

    url = f"http://{host}:{port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            # llama-server returns 200; treat any HTTP response (even
            # error JSON) as "the process is up".
            return 200 <= r.status < 500
    except urllib.error.HTTPError as e:
        # Got an HTTP-level error — that means the socket accepted us,
        # so the server *is* alive. Counts as running.
        return e.code < 500
    except (urllib.error.URLError, OSError):
        return False


# ---------------------------------------------------------------------------
# Install path
# ---------------------------------------------------------------------------


def install_llamacpp_instructions() -> str:
    """Return a platform-appropriate install hint as plain text.

    We don't run the install for the user — llama.cpp ships through
    several different channels (brew, apt, pacman, source builds,
    upstream prebuilt releases) and each one has its own caveats. A
    correct copy-pasteable hint is better than a half-working `subprocess`.
    """

    if sys.platform.startswith("darwin"):
        return (
            "macOS: `brew install llama.cpp` (universal binary; includes "
            "llama-server). After install, `llama-server --version` should "
            "print a build hash."
        )
    if sys.platform.startswith("linux"):
        return (
            "Linux: prebuilt releases live at "
            "https://github.com/ggerganov/llama.cpp/releases — grab the "
            "`llama-server` binary for your arch and put it on PATH. "
            "Distro packages are available too (Arch: `pacman -S llama.cpp`; "
            "Debian/Ubuntu: see the release page for .deb)."
        )
    if sys.platform.startswith("win"):
        return (
            "Windows: download the latest release ZIP from "
            "https://github.com/ggerganov/llama.cpp/releases and add "
            "`llama-server.exe` to PATH. (The mantis-agent Windows installer "
            "wrapper covers this once it lands.)"
        )
    # Fallback for other Unixes (BSDs, etc.) — point at upstream.
    return (
        "See https://github.com/ggerganov/llama.cpp for build / install "
        "instructions on your platform."
    )


# ---------------------------------------------------------------------------
# GGUF download
# ---------------------------------------------------------------------------


def default_models_dir() -> str:
    """Where we save downloaded GGUFs by default.

    Mirrors the XDG-ish convention: ``$MANTIS_AGENT_MODELS_DIR`` wins,
    else ``$XDG_DATA_HOME/mantis-agent/models``, else
    ``~/.local/share/mantis-agent/models`` (or
    ``~/Library/Application Support/mantis-agent/models`` on macOS,
    ``%APPDATA%/mantis-agent/models`` on Windows).
    """

    env_override = os.environ.get("MANTIS_AGENT_MODELS_DIR")
    if env_override:
        return env_override
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "mantis-agent", "models")
    if sys.platform.startswith("darwin"):
        return os.path.expanduser(
            "~/Library/Application Support/mantis-agent/models"
        )
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(appdata, "mantis-agent", "models")
    return os.path.expanduser("~/.local/share/mantis-agent/models")


def huggingface_gguf_url(repo: str, filename: str) -> str:
    """Build the public HuggingFace ``resolve/main`` URL for a GGUF file.

    We use the ``resolve/main`` redirect URL because it's the form CDNs
    cache and doesn't require auth for public repos. If a repo is gated
    or private the download surfaces an :class:`urllib.error.HTTPError`
    that ``download_gguf`` raises rather than silently writing the
    HuggingFace HTML login page to disk.
    """

    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


def download_gguf(
    model: GGUFModel,
    *,
    models_dir: str | None = None,
    force: bool = False,
    show_progress: bool = True,
) -> str:
    """Download ``model``'s GGUF file. Returns the absolute on-disk path.

    Idempotent — if the file already exists at the expected size and
    ``force`` is False, we return its path without re-downloading. This
    lets users re-run :command:`mantis-agent setup-local-llamacpp` to
    re-verify or change models without re-pulling gigabytes.
    """

    target_dir = models_dir or default_models_dir()
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, model.hf_filename)

    if not force and os.path.exists(target_path):
        existing = os.path.getsize(target_path)
        # We use the catalog size as a sanity check — a partial download
        # shows up as a wildly smaller file and we re-pull. Allow a 5%
        # tolerance because Q4_K_M GGUFs vary slightly between builds.
        expected_bytes = int(model.size_gb * (1024**3))
        if existing >= int(expected_bytes * 0.95):
            if show_progress:
                print(
                    f"Already downloaded: {target_path} "
                    f"({existing / (1024**3):.2f} GB)"
                )
            return target_path
        if show_progress:
            print(
                f"Partial file detected at {target_path} "
                f"({existing / (1024**3):.2f} GB vs expected ~{model.size_gb:.2f} GB), "
                f"re-downloading"
            )

    url = huggingface_gguf_url(model.hf_repo, model.hf_filename)
    if show_progress:
        print(f"Downloading {model.tag} (~{model.size_gb:.2f} GB) from {url}")

    # We stream to a temp file so an interrupted download doesn't leave
    # a corrupt GGUF at the canonical path (which we'd then trust on the
    # next run because the size check would pass).
    tmp_path = target_path + ".partial"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "mantis-agent-sdk setup-local-llamacpp"},
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    url, response.status, "non-2xx", response.headers, None
                )
            total = int(response.headers.get("Content-Length", "0") or 0)
            written = 0
            with open(tmp_path, "wb") as fh:
                while True:
                    chunk = response.read(1 << 20)  # 1 MB
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if show_progress and total:
                        pct = 100.0 * written / total
                        bar_w = 24
                        filled = int(bar_w * written / total)
                        bar = "█" * filled + "·" * (bar_w - filled)
                        # \r to keep the progress line in place; the
                        # final newline lands after the loop.
                        sys.stdout.write(
                            f"\r  [{bar}] {pct:5.1f}%  "
                            f"({written / (1024**3):.2f} / {total / (1024**3):.2f} GB)"
                        )
                        sys.stdout.flush()
        if show_progress and total:
            sys.stdout.write("\n")
            sys.stdout.flush()
        os.replace(tmp_path, target_path)
    except Exception:
        # Best-effort cleanup; ignore failures because the user already
        # has a real error to deal with.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    return target_path


# ---------------------------------------------------------------------------
# Server command-line builder + smoke test
# ---------------------------------------------------------------------------


def build_llamacpp_server_command(
    model_path: str,
    *,
    port: int = DEFAULT_LLAMACPP_PORT,
    host: str = "127.0.0.1",
    n_ctx: int = 4096,
    n_threads: int | None = None,
    jinja: bool = True,
    chat_template_flag: str = "",
    binary: str = "llama-server",
) -> list[str]:
    """Build the :command:`llama-server` argv that boots the model.

    Defaults:

    * ``--jinja`` is on — without it, llama-server doesn't take
      OpenAI-compat ``tools[]`` calls, and the mantis-agent capability
      profile says it does. We want path A working out of the box.
    * ``--host 127.0.0.1`` (loopback) — a setup-local helper must not
      expose an unauthenticated LLM to the LAN by default.
    * Threads default to whatever llama-server picks (one per logical
      core). Pass ``n_threads`` only if the user overrides.

    Returns the argv as a list, suitable for either ``subprocess`` or
    printing to the user as a copy-pasteable command.
    """

    cmd = [
        binary,
        "-m", model_path,
        "--host", host,
        "--port", str(port),
        "-c", str(n_ctx),
    ]
    if n_threads is not None:
        cmd += ["-t", str(n_threads)]
    if jinja:
        cmd.append("--jinja")
    if chat_template_flag:
        cmd += ["--chat-template", chat_template_flag]
    return cmd


def smoke_test_llamacpp(
    model_tag: str,
    *,
    port: int = DEFAULT_LLAMACPP_PORT,
    host: str = "127.0.0.1",
    timeout_s: float = 60.0,
) -> bool:
    """One-shot OpenAI-compat completion against the running server.

    Returns ``True`` if the server replied with non-empty text. We send
    the absolute minimum payload so the test isn't sensitive to model
    quirks: a one-token nudge with a tight :literal:`max_tokens` cap.
    """

    body = json.dumps({
        "model": model_tag,
        "messages": [{"role": "user", "content": "Reply with one word: ok"}],
        "max_tokens": 8,
        "stream": False,
    }).encode()
    url = f"http://{host}:{port}/v1/chat/completions"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            payload = json.loads(r.read().decode())
            choices = payload.get("choices") or []
            if not choices:
                print("  smoke-test: server returned no choices")
                return False
            msg = (choices[0].get("message") or {})
            text = (msg.get("content") or "").strip()
            print(f"  smoke-test response: {text[:120]!r}")
            return bool(text)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  smoke-test failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Orchestrator (called by ``mantis-agent setup-local-llamacpp``)
# ---------------------------------------------------------------------------


def run_setup_local_llamacpp(
    *,
    model: str | None = None,
    models_dir: str | None = None,
    port: int = DEFAULT_LLAMACPP_PORT,
    host: str = "127.0.0.1",
    skip_smoke_test: bool = False,
    skip_download: bool = False,
) -> int:
    """End-to-end: check llama-server → download GGUF → emit launch cmd → smoke test.

    Unlike the Ollama orchestrator we do NOT start the server in this
    process. The llama-server lifecycle (which model, which port, GPU
    layers, embedded vs. distinct host) varies wildly per user; the
    right UX is to download the model, hand back the exact command to
    run, and — if the server is already up — run the smoke test against
    it. Same shape as ``pip install`` followed by user-run scripts.
    """

    known = {m.tag: m for m in CPU_FRIENDLY_GGUF_MODELS}
    tag = model or DEFAULT_GGUF_RECOMMENDATION
    if tag in known:
        m = known[tag]
        print(
            f"Selected: {m.tag}  ({m.params}, ~{m.size_gb:.1f} GB, "
            f"RAM ≥ {m.min_ram_gb} GB, family {m.family})"
        )
        print(f"  {m.notes}")
    else:
        print(
            f"Selected: {tag!r} is not in the curated GGUF list. "
            "Either pass --list to see what's curated or supply "
            "--hf-repo + --hf-filename to download a custom GGUF."
        )
        return 2

    if not is_llamacpp_server_installed():
        print()
        print("`llama-server` is not on PATH.")
        print(install_llamacpp_instructions())
        # Don't hard-fail — the user can still download the GGUF and
        # install the server afterwards. We surface the issue but keep
        # going so we don't waste the download trip.

    target_path: str | None = None
    if not skip_download:
        try:
            target_path = download_gguf(m, models_dir=models_dir)
        except urllib.error.HTTPError as e:
            print(
                f"Download failed: HTTP {e.code} fetching "
                f"{huggingface_gguf_url(m.hf_repo, m.hf_filename)}",
                file=sys.stderr,
            )
            if e.code in (401, 403):
                print(
                    "  (the repo is gated or private — try `huggingface-cli "
                    "login` then re-run, or pick a non-gated tag)",
                    file=sys.stderr,
                )
            return 1
        except (urllib.error.URLError, OSError) as e:
            print(f"Download failed: {e}", file=sys.stderr)
            return 1

    print()
    if target_path:
        cmd = build_llamacpp_server_command(
            target_path,
            port=port,
            host=host,
            chat_template_flag=m.chat_template_flag,
        )
        print("To start the server:")
        print(f"    {' '.join(cmd)}")
    else:
        print("(Skipped download — supply your own GGUF path to llama-server.)")

    if skip_smoke_test:
        print("Done.")
        return 0

    print()
    if not is_llamacpp_running(port=port, host=host):
        print(
            f"Server not detected at http://{host}:{port}. "
            "Start it with the command above, then re-run with --skip-download "
            "to just smoke-test."
        )
        return 0  # Not an error — the download succeeded.

    print(f"Server is up at http://{host}:{port} — verifying with a tiny prompt …")
    if not smoke_test_llamacpp(m.tag, port=port, host=host):
        print(
            "Smoke test failed — the model is downloaded but the server didn't "
            "respond as expected. Check that the server is loading the right "
            "model and that --jinja is enabled if you want tool calls."
        )
        return 1

    print()
    print(f"All set. Point your code at http://{host}:{port}/v1:")
    print()
    print("    from mantis_agent import query, MantisAgentOptions")
    print("    async for msg in query(")
    print(
        f"        prompt=\"hi\", options=MantisAgentOptions("
        f"model={m.tag!r}, base_url=\"http://{host}:{port}/v1\"),"
    )
    print("    ):")
    print("        print(msg)")
    print()
    return 0


def print_gguf_model_table() -> None:
    """Pretty-print the GGUF catalog. Used by
    ``mantis-agent setup-local-llamacpp --list``."""

    name_w = max(len(m.tag) for m in CPU_FRIENDLY_GGUF_MODELS) + 2
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
    for m in CPU_FRIENDLY_GGUF_MODELS:
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
    print(f"Default recommendation: {DEFAULT_GGUF_RECOMMENDATION}")


# Silence an unused-import warning for the optional ``subprocess`` import
# kept for users who reach into this module to call ``subprocess.Popen``
# with the command we return. Pylint / ruff occasionally flag it.
_ = subprocess
