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


def run_setup(argv: list[str]) -> int:
    import argparse  # noqa: PLC0415

    from rich.console import Console  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    from . import catalog, setup_local  # noqa: PLC0415

    p = argparse.ArgumentParser(prog="mantis setup", description="Set up a local coding model for mantis.")
    p.add_argument("--auto", action="store_true", help="No prompts — pull the best model that fits.")
    p.add_argument("--model", default=None, help="Pull a specific Ollama tag.")
    p.add_argument("--list", action="store_true", dest="list_only", help="Just print the catalog.")
    args = p.parse_args(argv)

    c = Console()
    green, dim, gold = "#7cb342", "bright_black", "#cddc39"

    c.print()
    c.print(Text("  🦗  mantis setup", style=f"bold {green}"))
    c.print(Text("  Let's get you a local coding agent.\n", style=dim))

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
    catalog.set_last_model(chosen_tag, "http://localhost:11434")

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
