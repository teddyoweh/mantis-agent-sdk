"""``mantis setup`` — the first-run experience.

Detects your machine, recommends the best *coding* model that fits, pulls it
through Ollama, verifies it, and sets it as the default so ``mantis`` opens
straight into a working agent. The model catalog is coding-first: the
Qwen2.5-Coder family (the strongest open coding models) plus DeepSeek-R1 for
step-by-step code reasoning.

    mantis setup            # detect hardware, pick a model, pull it
    mantis setup --auto     # no prompts — grab the best that fits
    mantis setup --model qwen2.5-coder:7b
    mantis setup --list     # just print the catalog
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class CodingModel:
    tag: str          # `ollama pull <tag>`
    label: str        # human name
    min_ram_gb: int   # memory to run comfortably (Q4)
    kind: str         # "coder" | "think"
    blurb: str        # one-line "why this one"
    size_gb: float    # rough on-disk download size


# Ranked most-capable → smallest. Coding-first: Qwen2.5-Coder is the SOTA open
# coder; DeepSeek-R1 brings code reasoning (it shows its <think> work).
CODING_MODELS: list[CodingModel] = [
    CodingModel("qwen2.5-coder:32b", "Qwen2.5-Coder 32B", 22, "coder", "SOTA open coder — GPT-4o-class on code", 20),
    CodingModel("qwen2.5-coder:14b", "Qwen2.5-Coder 14B", 11, "coder", "Excellent coder, fits a 16 GB machine", 9),
    CodingModel("deepseek-r1:14b",   "DeepSeek-R1 14B",   11, "think", "Code reasoning — thinks step by step", 9),
    CodingModel("qwen2.5-coder:7b",  "Qwen2.5-Coder 7B",   6, "coder", "The laptop sweet spot for coding", 4.7),
    CodingModel("deepseek-r1:8b",    "DeepSeek-R1 8B",     6, "think", "Reasoning that fits a laptop", 4.9),
    CodingModel("qwen2.5-coder:3b",  "Qwen2.5-Coder 3B",   4, "coder", "Capable coder on modest RAM", 1.9),
    CodingModel("qwen2.5-coder:1.5b","Qwen2.5-Coder 1.5B", 3, "coder", "Tiny coder — runs almost anywhere", 1.0),
    CodingModel("qwen2.5-coder:0.5b","Qwen2.5-Coder 0.5B", 2, "coder", "Smallest coder with tool calls", 0.5),
]


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------


def _total_ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, AttributeError, OSError):
        pass
    if platform.system() == "Darwin":
        try:
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 1e9  # noqa: S603,S607
        except Exception:  # noqa: BLE001
            pass
    if platform.system() == "Windows":
        try:
            import ctypes  # noqa: PLC0415

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return ms.ullTotalPhys / 1e9
        except Exception:  # noqa: BLE001
            pass
    return 8.0


def _nvidia_vram_gb() -> float | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.check_output(  # noqa: S603
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],  # noqa: S607
            text=True, timeout=4,
        )
        return max(int(x) for x in out.split()) / 1024
    except Exception:  # noqa: BLE001
        return None


def detect_hardware() -> tuple[float, str]:
    """Return (usable_gb_budget, human_label)."""
    ram = _total_ram_gb()
    apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    vram = _nvidia_vram_gb()
    if vram:
        return vram, f"NVIDIA GPU · {vram:.0f} GB VRAM"
    if apple:
        return ram * 0.7, f"Apple Silicon · {ram:.0f} GB unified memory"
    return ram * 0.6, f"CPU · {ram:.0f} GB RAM  (no GPU detected — smaller models run faster)"


def recommend(budget_gb: float) -> CodingModel:
    fits = [m for m in CODING_MODELS if m.min_ram_gb <= budget_gb]
    return fits[0] if fits else CODING_MODELS[-1]


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


# Providers with a well-known no-card free tier (used by `mantis setup --free`
# and the "Free hosted" menu entry). The signup URL is in each provider's note.
FREE_PROVIDER_IDS = frozenset({"groq", "cerebras", "gemini", "openrouter"})


def _ollama_base() -> str:
    """The Ollama base URL, honoring ``$OLLAMA_HOST``. Delegates to the shared
    :func:`mantis_agent.paths.ollama_base_url` so setup + launch agree."""
    from .paths import ollama_base_url  # noqa: PLC0415
    return ollama_base_url()


def run_setup(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    p = argparse.ArgumentParser(prog="mantis setup", description="Set up a model for mantis — local, hosted, or free.")
    p.add_argument("--auto", action="store_true", help="No prompts — pull the best local model that fits.")
    p.add_argument("--model", default=None, help="Pull a specific Ollama tag (local).")
    p.add_argument("--list", action="store_true", dest="list_only", help="Just print the local catalog.")
    p.add_argument("--local", action="store_true", help="Go straight to the local (Ollama) flow.")
    p.add_argument("--hosted", action="store_true", help="Go straight to the hosted-API flow (paste a key).")
    p.add_argument("--free", action="store_true", help="Go straight to the free-hosted-tier flow.")
    p.add_argument("--selfhost", action="store_true", help="Go straight to the self-host (custom URL) flow.")
    p.add_argument("--status", action="store_true", help="Show the current model + enabled providers and exit.")
    args = p.parse_args(argv)

    c = Console()
    green, dim = "#7cb342", "bright_black"

    c.print()
    c.print(Text("  🦗  mantis setup", style=f"bold {green}"))

    if args.status:
        _print_status(c)
        return 0

    c.print(Text("  Let's get you a working model.", style=dim))
    _print_status(c)  # orient the user: what's already configured
    c.print()

    # -- top-level: how do you want to run models? -------------------------
    # Explicit flags / the local-only options skip the menu.
    local_flags = args.list_only or args.model or args.auto or args.local
    if args.hosted:
        return _run_hosted(c, free_only=False)
    if args.free:
        return _run_hosted(c, free_only=True)
    if args.selfhost:
        return _run_selfhost(c)
    if not local_flags:
        choice = _choose_path(c)
        if choice is None:
            c.print(Text("\n  Cancelled.", style=dim))
            return 1
        if choice == "hosted":
            return _run_hosted(c, free_only=False)
        if choice == "free":
            return _run_hosted(c, free_only=True)
        if choice == "selfhost":
            return _run_selfhost(c)
        # choice == "local" → fall through to the Ollama flow below

    return _run_local(c, args)


def _run_local(c: Any, args: Any) -> int:
    from rich.text import Text  # noqa: PLC0415

    from . import catalog, setup_local  # noqa: PLC0415

    green, dim, gold = "#7cb342", "bright_black", "#cddc39"

    budget, label = detect_hardware()
    c.print(Text(f"  Detected:  {label}", style="white"))
    rec = recommend(budget)

    if args.list_only:
        _print_catalog(c, budget, rec)
        return 0

    # -- pick the model ----------------------------------------------------
    if args.model:
        chosen_tag = args.model
        c.print(Text(f"  Model:     {chosen_tag}\n", style="white"))
    elif args.auto:
        chosen_tag = rec.tag
        c.print(Text(f"  Auto-pick: {rec.label}  ({rec.blurb})\n", style="white"))
    else:
        chosen_tag = _interactive_pick(c, budget, rec)
        if chosen_tag is None:
            c.print(Text("\n  Cancelled.", style=dim))
            return 1

    # -- ensure Ollama -----------------------------------------------------
    if not setup_local.is_ollama_installed():
        c.print(Text("  Ollama isn't installed — installing it (one-time)…", style=gold))
        rc = setup_local.install_ollama(auto_confirm=args.auto)
        if rc != 0 or not setup_local.is_ollama_installed():
            c.print(Text("  ✗ Ollama install failed. See https://ollama.com/download", style="red"))
            return 1

    ok, _log = setup_local.start_ollama_server()
    if not ok:
        c.print(Text("  ✗ Couldn't start the Ollama server (`ollama serve`).", style="red"))
        return 1

    # -- pull --------------------------------------------------------------
    c.print(Text(f"\n  Pulling {chosen_tag} …", style=f"bold {green}"))
    c.print(Text("  (first time downloads weights; grab a coffee ☕)\n", style=dim))
    rc = subprocess.call(["ollama", "pull", chosen_tag])  # noqa: S603,S607 — shows ollama's own progress
    if rc != 0:
        c.print(Text(f"\n  ✗ `ollama pull {chosen_tag}` failed (rc={rc}).", style="red"))
        return 1

    # -- verify + persist as default --------------------------------------
    installed = _ollama_has(chosen_tag)
    catalog.set_last_model(chosen_tag, _ollama_base())

    c.print()
    c.print(Text(f"  ✓ {chosen_tag} is ready{'' if installed else ' (pulled)'} and set as your default.",
                 style=f"bold {green}"))
    c.print(Text("\n  Start coding:", style="white"))
    c.print(Text("      mantis", style=f"bold {gold}"))
    c.print(Text("  Switch models any time inside mantis with  /model  or  /models.\n", style=dim))
    return 0


def _ollama_has(tag: str) -> bool:
    try:
        out = subprocess.check_output(["ollama", "list"], text=True, timeout=8)  # noqa: S603,S607
    except Exception:  # noqa: BLE001
        return False
    base = tag.split(":")[0]
    return any(base in line for line in out.splitlines()[1:])


def _print_catalog(c: object, budget: float, rec: object) -> None:
    from rich.text import Text  # noqa: PLC0415

    c.print(Text("  Coding models  (↑ more capable · ✓ fits your machine)\n", style="bright_black"))
    for i, m in enumerate(CODING_MODELS, 1):
        fits = m.min_ram_gb <= budget
        star = "★" if m is rec else " "
        mark = "✓" if fits else "·"
        line = Text()
        line.append(f"  {star} {i:>2}  ", style="#cddc39" if m is rec else "bright_black")
        line.append(f"{mark} ", style="#7cb342" if fits else "bright_black")
        line.append(f"{m.label:<20}", style="white" if fits else "bright_black")
        line.append(f"{m.blurb:<42}", style="bright_black")
        line.append(f"~{m.size_gb:g} GB", style="bright_black")
        c.print(line)


def _interactive_pick(c: object, budget: float, rec: object) -> str | None:
    from rich.text import Text  # noqa: PLC0415

    # Arrow-key menu (pre-selecting the ★ recommendation). Rows show fit + size.
    rows: list[tuple[str, str]] = []
    for m in CODING_MODELS:
        fits = "✓ fits" if m.min_ram_gb <= budget else "needs more RAM"
        star = "★ " if m is rec else "  "
        rows.append((f"{star}{m.label:<20}", f"{m.blurb}  · ~{m.size_gb:g} GB · {fits}"))
    start = CODING_MODELS.index(rec) if rec in CODING_MODELS else 0  # type: ignore[arg-type]
    idx = _arrow_select("Coding models  (↑ more capable)", rows, start=start)
    if idx is None:
        return None
    if idx >= 0:
        return CODING_MODELS[idx].tag

    # numeric fallback (non-TTY)
    c.print()
    _print_catalog(c, budget, rec)
    c.print(Text(f"\n  ★ recommended for your machine: {rec.label}", style="#cddc39"))  # type: ignore[attr-defined]
    try:
        raw = input("  Pick a number, or Enter for the recommended: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return rec.tag  # type: ignore[attr-defined]
    if raw.isdigit() and 1 <= int(raw) <= len(CODING_MODELS):
        return CODING_MODELS[int(raw) - 1].tag
    c.print(Text(f"  (didn't recognise {raw!r} — using the recommended)", style="bright_black"))
    return rec.tag  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Path chooser + hosted-provider flow (reuses catalog.py — same store the
# in-app /enable command writes, so a model set up here Just Works on launch).
# ---------------------------------------------------------------------------


def _print_status(c: Any) -> None:
    """Show the current default model + backend and which hosted providers are
    enabled, so `mantis setup` orients the user in what's already configured."""
    from rich.text import Text  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    green, dim = "#7cb342", "bright_black"
    last = catalog.get_last_model()
    if last and last.get("model"):
        backend = last.get("backend") or ""
        if not backend:  # older saves stored no backend — resolve it from the model
            prov = catalog.provider_for_model(last["model"])
            backend = prov.base_url if prov else ""
        where = ("Ollama (local)" if ("localhost" in backend or "127.0.0.1" in backend)
                 else (backend or "default backend"))
        c.print(Text(f"  Current:  {last['model']}  ·  {where}", style="white"))
    else:
        c.print(Text("  Current:  nothing configured yet", style=dim))
    enabled = []
    for p in catalog.CATALOG:
        if not catalog.is_enabled(p):
            continue
        label = p.label
        # Anthropic can be key- or token-authed — show which.
        if p.id == "anthropic" and not catalog.api_key_for(p) and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            label += " (token)"
        enabled.append(label)
    if enabled:
        c.print(Text("  Enabled:  " + "  ·  ".join(enabled), style=green))


def _arrow_select(title: str, rows: list[tuple[str, str]], *, start: int = 0) -> int | None:
    """Beautiful inline arrow-key menu (prompt_toolkit). ``rows`` is a list of
    ``(label, hint)``. Returns the picked index, or None if cancelled. Returns
    the sentinel ``-1`` when stdin isn't a TTY (pipe / test) so the caller can
    fall back to numeric ``input()``."""
    import sys  # noqa: PLC0415

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return -1
    try:
        from prompt_toolkit import Application  # noqa: PLC0415
        from prompt_toolkit.formatted_text import ANSI  # noqa: PLC0415
        from prompt_toolkit.key_binding import KeyBindings  # noqa: PLC0415
        from prompt_toolkit.layout import HSplit, Layout, Window  # noqa: PLC0415
        from prompt_toolkit.layout.controls import FormattedTextControl  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return -1

    state = {"sel": max(0, min(start, len(rows) - 1))}

    def render() -> Any:
        out = [f"  \033[1m{title}\033[0m\n"]
        for i, (label, hint) in enumerate(rows):
            if i == state["sel"]:
                out.append(f"\033[30;48;5;150m ▸ {label} \033[0m  \033[90m{hint}\033[0m")
            else:
                out.append(f"    \033[97m{label}\033[0m  \033[90m{hint}\033[0m")
        out.append(f"\n  \033[90m↑/↓ · enter to pick · esc to cancel\033[0m")
        return ANSI("\n".join(out))

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _(_e: Any) -> None:
        state["sel"] = (state["sel"] - 1) % len(rows)

    @kb.add("down")
    @kb.add("c-n")
    def _(_e: Any) -> None:
        state["sel"] = (state["sel"] + 1) % len(rows)

    @kb.add("enter")
    def _(e: Any) -> None:
        e.app.exit(result=state["sel"])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _(e: Any) -> None:
        e.app.exit(result=None)

    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render), height=len(rows) + 3)])),
        key_bindings=kb,
        full_screen=False,
    )
    return app.run()


def _pick_model_id(c: Any, models: list[str], *, current: str | None = None) -> str | None:
    """Arrow-pick a model id from ``models`` (numeric fallback for non-TTY).
    The current default (if any) is pre-highlighted. Returns the id or None."""
    from rich.text import Text  # noqa: PLC0415

    if not models:  # a provider that returned nothing — nothing to pick
        return None
    shown = models[:30]
    rows = [(m, "← current" if m == current else "") for m in shown]
    start = shown.index(current) if current in shown else 0
    idx = _arrow_select("Pick a model", rows, start=start)
    if idx is None:
        return None
    if idx >= 0:
        return shown[idx]
    # numeric fallback (non-TTY)
    c.print(Text("\n  Models:\n", style="bright_black"))
    for i, m in enumerate(shown[:20], 1):
        mark = "  ← current" if m == current else ""
        c.print(Text(f"   {i:>2}  {m}{mark}", style="white"))
    try:
        raw = input(f"\n  Pick [1-{min(len(shown), 20)}, Enter={shown[0]}], or type an id: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return shown[0]
    if raw.isdigit() and 1 <= int(raw) <= min(len(shown), 20):
        return shown[int(raw) - 1]
    return raw  # exact id the user typed


def _choose_path(c: Any) -> str | None:
    """Top-level menu: local (Ollama) / hosted API / free hosted / self-host.
    Returns 'local' | 'hosted' | 'free' | 'selfhost', or None if cancelled.
    Arrow-key navigable, with a numeric fallback for pipes/tests."""
    from rich.text import Text  # noqa: PLC0415

    keys = ["local", "hosted", "free", "selfhost"]
    rows = [
        ("Local      ", "free, on your machine (Ollama) — private, offline"),
        ("Hosted API ", "OpenAI · DeepSeek · Kimi · Claude · … (paste a key)"),
        ("Free hosted", "Groq · Cerebras · Gemini · OpenRouter free tiers"),
        ("Self-host  ", "your own endpoint — vLLM · TGI · llama.cpp · a GPU box"),
    ]
    idx = _arrow_select("How do you want to run models?", rows)
    if idx is None:
        return None
    if idx >= 0:
        return keys[idx]
    # numeric fallback (non-TTY)
    c.print(Text("  How do you want to run models?\n", style="white"))
    for i, (label, hint) in enumerate(rows, 1):
        c.print(Text(f"   {i}  {label.strip():<11}  {hint}", style="white"))
    try:
        raw = input("\n  Pick [1-4, Enter=1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return {"": "local", "1": "local", "2": "hosted", "3": "free", "4": "selfhost"}.get(raw)


def _run_hosted(c: Any, *, free_only: bool) -> int:
    """Pick a hosted provider, paste + validate a key, choose a model, and save
    it as the default. Saved via catalog.set_key / set_last_model, so mantis
    restores the backend + key automatically on next launch."""
    import getpass  # noqa: PLC0415

    from rich.text import Text  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    green, dim, gold, red = "#7cb342", "bright_black", "#cddc39", "red"

    providers = [
        p for p in catalog.CATALOG
        if not free_only or p.id in FREE_PROVIDER_IDS
    ]
    title = "Free hosted tiers" if free_only else "Hosted API providers"

    def _row(p: Any) -> tuple[str, str]:
        tag = "  ✓ key saved" if catalog.saved_key(p.id) else (
            "  · free tier" if (p.id in FREE_PROVIDER_IDS and not free_only) else "")
        return (f"{p.label:<20}", f"{p.note}{tag}")

    rows = [_row(p) for p in providers]
    idx = _arrow_select(title, rows)
    if idx is None:
        c.print(Text("  Cancelled.", style=dim))
        return 1
    if idx >= 0:
        prov = providers[idx]
    else:
        # numeric fallback (non-TTY)
        c.print(Text(f"\n  {title}\n", style=dim))
        for i, (label, hint) in enumerate(rows, 1):
            c.print(Text(f"  {i:>2}  {label}{hint}", style="white"))
        try:
            raw = input("\n  Pick a provider number: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if not (raw.isdigit() and 1 <= int(raw) <= len(providers)):
            c.print(Text("  Cancelled.", style=dim))
            return 1
        prov = providers[int(raw) - 1]

    # Anthropic has several auth styles (API key / OAuth token / cloud gateway) —
    # hand it to a dedicated sub-flow instead of the generic x-api-key path.
    if prov.id == "anthropic":
        return _run_anthropic(c, prov)

    # -- key ---------------------------------------------------------------
    existing = catalog.saved_key(prov.id)
    if existing:
        c.print(Text(f"\n  {prov.label} already has a saved key.", style=green))
        try:
            ans = input("  Reuse it? [Y/n] (or paste a new key): ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        key = existing if ans.lower() in ("", "y", "yes") else ans
    else:
        c.print(Text(f"\n  Get a key: {prov.note.split('·')[0].strip()}", style=dim))
        c.print(Text(f"  Paste your {prov.api_key_env} (input hidden):", style="white"))
        try:
            key = getpass.getpass("  key> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not key:
        c.print(Text("  No key entered — cancelled.", style=dim))
        return 1

    # -- validate (store first so the probe uses it; roll back on failure) --
    c.print(Text("  Validating…", style=dim))
    catalog.set_key(prov.id, key)
    ok, msg = catalog.validate_provider(prov)
    if not ok:
        catalog.clear_key(prov.id)
        c.print(Text(f"  ✗ {msg}", style=red))
        c.print(Text("  (key not saved) — double-check it and re-run  mantis setup.", style=dim))
        return 1
    c.print(Text("  ✓ key works", style=green))

    # -- pick a model (live list, chat-filtered; falls back to flagships) ---
    live = catalog.refresh_live_models(prov) or []
    try:
        from .tui import _is_chat_model  # noqa: PLC0415
        chat = [m for m in live if _is_chat_model(m)]
    except Exception:  # noqa: BLE001
        chat = live
    models = chat or list(prov.models)
    # "Free hosted" on OpenRouter should actually be free — its zero-cost models
    # carry a ":free" suffix, so surface only those (fall back to all if the live
    # list didn't include any). Groq/Cerebras/Gemini free tiers use normal ids.
    if free_only and prov.id == "openrouter":
        free = [m for m in models if m.endswith(":free")]
        if free:
            models = free
    model = _pick_model_id(c, models)
    if model is None:
        c.print(Text("  Cancelled.", style=dim))
        return 1

    # Confirm the exact model answers before committing it as the default.
    # _confirm_model routes Anthropic → /v1/messages, everyone else → /chat/completions.
    if not _confirm_model(c, prov.base_url, model, key):
        c.print(Text("  Not saved — re-run  mantis setup  to pick another.", style=dim))
        return 1

    catalog.set_last_model(model, prov.base_url)
    c.print()
    c.print(Text(f"  ✓ {prov.label} enabled · default model  {model}", style=f"bold {green}"))
    c.print(Text("\n  Start coding:", style="white"))
    c.print(Text("      mantis", style=f"bold {gold}"))
    c.print(Text("  Switch models any time with  /models  ·  add more with  mantis setup.\n", style=dim))
    return 0


def _run_oauth_login(c: Any) -> str | None:
    """Interactive Claude subscription (Pro/Max) OAuth login. Opens the authorize
    page, takes the pasted code, exchanges it for an access token. Returns the
    token, or None if cancelled/failed."""
    import getpass  # noqa: PLC0415
    import webbrowser  # noqa: PLC0415

    from rich.text import Text  # noqa: PLC0415

    from . import anthropic_oauth as oa  # noqa: PLC0415

    # Quick path: most people already have a token (`claude setup-token`, a
    # subscription token, a gateway token). Paste it and we're done — the browser
    # sign-in is only for people who DON'T have one yet (press Enter for it).
    c.print(Text("\n  Paste an OAuth / auth token if you have one, or press Enter to sign in:", style="white"))
    try:
        existing = getpass.getpass("  token › ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if existing:
        return existing

    verifier, challenge = oa.make_pkce()
    state = oa.make_state()
    url = oa.build_authorize_url(code_challenge=challenge, state=state)

    c.print(Text("\n  Sign in with your Claude subscription (Pro/Max) — opening your browser…", style="white"))
    c.print(Text(f"  If it doesn't open, paste this URL:\n  {url}\n", style="bright_black"))
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    c.print(Text("  After approving, copy the code shown and paste it here (empty to cancel).", style="white"))
    try:
        code = input("  code › ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not code:
        return None
    c.print(Text("  Exchanging code for a token…", style="bright_black"))
    try:
        tokens = oa.exchange_code(code, code_verifier=verifier, state=state)
    except Exception as e:  # noqa: BLE001
        c.print(Text(f"  ✗ token exchange failed: {e}", style="red"))
        return None
    token = tokens.get("access_token")
    if not token:
        c.print(Text("  ✗ no access_token in the response", style="red"))
        return None
    return str(token)


def _run_anthropic(c: Any, prov: Any) -> int:
    """Claude auth chooser: direct API key (x-api-key), OAuth / auth token
    (Bearer — a subscription login or `claude setup-token`), or a Bedrock /
    Vertex / Azure gateway (custom base URL + Bearer token). Saves so mantis
    restores it on launch."""
    import getpass  # noqa: PLC0415

    from rich.text import Text  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415
    from .settings import update_setting_source  # noqa: PLC0415

    green, dim, gold, red = "#7cb342", "bright_black", "#cddc39", "red"

    keys = ["apikey", "oauth", "gateway"]
    rows = [
        ("API key      ", "a direct Anthropic key (console.anthropic.com) — x-api-key"),
        ("OAuth token  ", "paste a Claude token — or sign in with your Pro/Max subscription"),
        ("Cloud gateway", "Bedrock · Vertex · Azure via a proxy URL + Bearer token"),
    ]
    idx = _arrow_select("How do you authenticate with Claude?", rows)
    if idx is None:
        c.print(Text("  Cancelled.", style=dim))
        return 1
    if idx < 0:
        c.print(Text("\n  Claude auth:", style=dim))
        for i, (label, hint) in enumerate(rows, 1):
            c.print(Text(f"   {i}  {label.strip():<13}  {hint}", style="white"))
        try:
            raw = input("\n  Pick [1-3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if not (raw.isdigit() and 1 <= int(raw) <= 3):
            c.print(Text("  Cancelled.", style=dim))
            return 1
        idx = int(raw) - 1
    method = keys[idx]

    base_url = prov.base_url
    if method == "gateway":
        c.print(Text("\n  Point at your Anthropic-compatible gateway "
                     "(LiteLLM / Bedrock Access Gateway / Azure Foundry /anthropic).", style=dim))
        try:
            entered = input("  Gateway base URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if not entered:
            c.print(Text("  Cancelled.", style=dim))
            return 1
        base_url = entered if entered.startswith(("http://", "https://")) else "https://" + entered

    # -- credential --------------------------------------------------------
    if method == "oauth":
        cred = _run_oauth_login(c)
        if not cred:
            c.print(Text("  Cancelled — no token obtained.", style=dim))
            return 1
    else:
        label = "ANTHROPIC_API_KEY" if method == "apikey" else "Bearer token"
        c.print(Text(f"\n  Paste your {label} (input hidden):", style="white"))
        try:
            cred = getpass.getpass("  › ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if not cred:
            c.print(Text("  No credential entered — cancelled.", style=dim))
            return 1

    # -- validate (ping /v1/messages with the chosen auth) -----------------
    c.print(Text("  Validating…", style=dim))
    if method == "apikey":
        ok, detail = _ping_anthropic_model(base_url, prov.models[0], cred)
    else:
        ok, detail = _ping_anthropic_bearer(base_url, prov.models[0], cred)
    if not ok:
        c.print(Text(f"  ✗ {detail}", style=red))
        c.print(Text("  (nothing saved) — double-check the credential and re-run  mantis setup.", style=dim))
        return 1
    c.print(Text("  ✓ credential works", style=green))

    # -- pick a model ------------------------------------------------------
    if method == "gateway":
        # Gateways use provider-specific model ids (Bedrock
        # 'anthropic.claude-…-v2:0', Vertex 'claude-…@20240620'), NOT the direct
        # Anthropic flagship names — so take the exact id the gateway expects.
        c.print(Text("\n  Enter the model id your gateway serves "
                     "(e.g. anthropic.claude-sonnet-4-5-v1:0):", style="white"))
        try:
            model = input("  model › ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if not model:
            c.print(Text("  Cancelled.", style=dim))
            return 1
    else:
        model = _pick_model_id(c, list(prov.models))
        if model is None:
            c.print(Text("  Cancelled.", style=dim))
            return 1

    # -- persist -----------------------------------------------------------
    if method == "apikey":
        catalog.set_key("anthropic", cred)
    else:
        # Bearer token → settings.env; mantis loads it into os.environ at launch
        # and the passthrough sends it as Authorization: Bearer.
        update_setting_source("user", {"env": {"ANTHROPIC_AUTH_TOKEN": cred}})
    catalog.set_last_model(model, base_url)

    c.print()
    c.print(Text(f"  ✓ Claude enabled ({method}) · default model  {model}", style=f"bold {green}"))
    c.print(Text("\n  Start coding:", style="white"))
    c.print(Text("      mantis", style=f"bold {gold}"))
    c.print(Text("  Switch models any time with  /models.\n", style=dim))
    return 0


def _anthropic_ping(base_url: str, model: str, auth: dict[str, str], *, timeout: float = 10.0) -> tuple[bool, str]:
    """Core Claude credential check via ``/v1/messages`` with a 1-token request.
    ``auth`` supplies the credential header (x-api-key OR authorization: Bearer).

    We fail ONLY on a real auth failure (HTTP 401 or an ``authentication_error``)
    — a model-permission / not-found error means the credential authenticated
    fine (the account just lacks *that* flagship), so we still let the user pick a
    model. A network blip → (True, "") so a save is never blocked on a hiccup."""
    try:
        import httpx  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return True, ""
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01", **auth}
    payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/messages", headers=headers, json=payload, timeout=timeout)
    except Exception:  # noqa: BLE001
        return True, ""
    if r.status_code == 200:
        return True, "ok"
    etype, detail = "", f"HTTP {r.status_code}"
    try:
        err = r.json().get("error", {})
        if isinstance(err, dict):
            etype = str(err.get("type", ""))
            if err.get("message"):
                detail = str(err["message"])[:200]
    except Exception:  # noqa: BLE001
        pass
    if r.status_code == 401 or etype == "authentication_error":
        return False, detail
    return True, f"credential OK ({detail})"  # authenticated; model/quota issue only


def _ping_anthropic_bearer(base_url: str, model: str, token: str, *, timeout: float = 10.0) -> tuple[bool, str]:
    """Claude credential check with an OAuth/gateway Bearer token."""
    return _anthropic_ping(base_url, model, {"authorization": f"Bearer {token}"}, timeout=timeout)


def _ping_chat_model(base_url: str, model: str, key: str, *, timeout: float = 10.0) -> tuple[bool, str]:
    """Confirm a model actually answers on ``/chat/completions`` with a 1-token
    request — catches responses-only / wrong-endpoint / decommissioned models
    BEFORE we save them as the default. Returns ``(ok, detail)``. Best-effort:
    a network flake returns ``(True, "")`` so we never block a save on a blip."""
    try:
        import httpx  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return True, ""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # OpenAI's gpt-5.x / o-series reject the legacy max_tokens (they want
    # max_completion_tokens) — mirror the provider so validating those models in
    # setup doesn't 400 on a valid model. (temperature isn't sent, so no clash.)
    bare = model.split("/")[-1].lower()
    token_field = "max_completion_tokens" if bare.startswith(("gpt-5", "o1", "o3", "o4")) else "max_tokens"
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], token_field: 1}
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/chat/completions",
                       headers=headers, json=payload, timeout=timeout)
    except Exception:  # noqa: BLE001 — connect/timeout → don't block the save
        return True, ""
    if r.status_code == 200:
        return True, "ok"
    detail = f"HTTP {r.status_code}"
    try:
        err = r.json().get("error", {})
        if isinstance(err, dict) and err.get("message"):
            detail = str(err["message"])[:200]
    except Exception:  # noqa: BLE001
        pass
    # A truncation from our 1-token probe cap ("could not finish the message …")
    # means the model + key WORK — reasoning models just spend the budget on
    # hidden reasoning. That's a pass, not a validation failure.
    low = detail.lower()
    if "could not finish" in low or "reduce the length" in low:
        return True, "ok"
    return False, detail


def _ping_anthropic_model(base_url: str, model: str, key: str, *, timeout: float = 10.0) -> tuple[bool, str]:
    """Claude credential check with a direct x-api-key. Anthropic's API isn't
    OpenAI-compatible (``/v1/messages``, x-api-key + anthropic-version)."""
    return _anthropic_ping(base_url, model, {"x-api-key": key} if key else {}, timeout=timeout)


def _confirm_model(c: Any, base_url: str, model: str, key: str) -> bool:
    """Ping the model; if it fails, show why and ask whether to save anyway.
    Returns True to proceed with saving, False to abort. Routes Anthropic to its
    Messages API, everything else to /chat/completions."""
    from rich.text import Text  # noqa: PLC0415

    c.print(Text("  Testing the model…", style="bright_black"))
    if "anthropic.com" in base_url:
        ok, detail = _ping_anthropic_model(base_url, model, key)
    else:
        ok, detail = _ping_chat_model(base_url, model, key)
    if ok:
        c.print(Text("  ✓ model responds", style="#7cb342"))
        return True
    c.print(Text(f"  ⚠ {model} didn't answer: {detail}", style="#cddc39"))
    try:
        ans = input("  Save it anyway? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _probe_openai_models(url: str, key: str) -> list[str] | None:
    """GET ``{url}/models`` (OpenAI shape). Returns the id list, [] if reachable
    but empty, or None if unreachable. Best-effort — never raises."""
    try:
        import httpx  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = httpx.get(f"{url.rstrip('/')}/models", headers=headers, timeout=4.0)
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        return [m["id"] for m in data if isinstance(m, dict) and "id" in m]
    except Exception:  # noqa: BLE001 — network/JSON/anything
        return None


def _run_selfhost(c: Any) -> int:
    """Point mantis at any OpenAI-compatible endpoint you run yourself (vLLM,
    TGI, llama.cpp, LM Studio, a remote GPU box). Saves backend + model as the
    default; the API key (if the server needs one) goes to user settings."""
    import getpass  # noqa: PLC0415

    from rich.text import Text  # noqa: PLC0415

    from . import catalog  # noqa: PLC0415

    green, dim, gold, red = "#7cb342", "bright_black", "#cddc39", "red"

    c.print(Text("\n  Self-host — point mantis at your own OpenAI-compatible endpoint", style="white"))
    c.print(Text("  vLLM · TGI · llama.cpp · LM Studio · a remote GPU box — anything that speaks /v1\n", style=dim))
    try:
        url = input("  Base URL [Enter for http://localhost:8000/v1]: ").strip() or "http://localhost:8000/v1"
    except (EOFError, KeyboardInterrupt):
        return 1
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        key = getpass.getpass("  API key (Enter if none — most local servers need none): ").strip()
    except (EOFError, KeyboardInterrupt):
        return 1

    c.print(Text("  Probing endpoint…", style=dim))
    models = _probe_openai_models(url, key)
    # Most OpenAI-compat servers (vLLM, Ollama, llama.cpp, LM Studio) serve under
    # /v1. If the user pasted a bare host and the probe missed, retry with /v1 and
    # adopt it — so the saved backend hits /v1/chat/completions, not /chat/…​.
    if models is None and "/v1" not in url:
        alt = url.rstrip("/") + "/v1"
        alt_models = _probe_openai_models(alt, key)
        if alt_models is not None:
            url, models = alt, alt_models
            c.print(Text(f"  (found the API under {alt})", style=dim))
    if models:
        c.print(Text(f"  ✓ reachable — {len(models)} model(s)", style=green))
        model = _pick_model_id(c, models)
        if model is None:
            c.print(Text("  Cancelled.", style=dim))
            return 1
    else:
        c.print(Text("  (couldn't list /v1/models — enter the model id your server serves)", style=dim))
        try:
            model = input("  Model id: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
        if not model:
            c.print(Text("  Cancelled.", style=dim))
            return 1

    if not _confirm_model(c, url, model, key):
        c.print(Text("  Not saved — re-run  mantis setup.", style=dim))
        return 1

    catalog.set_last_model(model, url)
    if key:
        try:
            from .settings import update_setting_source  # noqa: PLC0415
            update_setting_source("user", {"env": {"MANTIS_AGENT_API_KEY": key}})
        except Exception:  # noqa: BLE001
            c.print(Text("  (couldn't persist the key — set MANTIS_AGENT_API_KEY yourself)", style=red))

    c.print()
    c.print(Text(f"  ✓ connected · {model}  @  {url}", style=f"bold {green}"))
    c.print(Text("\n  Start coding:", style="white"))
    c.print(Text("      mantis", style=f"bold {gold}"))
    c.print(Text("  Switch models any time with  /models  or  /connect.\n", style=dim))
    return 0
