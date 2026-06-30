"""``mantis`` — the interactive terminal UI.

This is the Claude-Code-style agent terminal: run ``mantis`` in any directory
and you get a banner (pixel mascot + version + model + cwd), an input line with
a rotating ``Try "…"`` placeholder, a mode footer you can cycle with
``shift+tab``, slash commands, an animated thinking spinner, and Markdown-
rendered replies from a real model.

Unlike ``mantis-agent`` (the stdlib-only diagnostics CLI), this module is a
*rich* experience and depends on two third-party libraries:

* ``prompt_toolkit`` — the input line (placeholder, key bindings, completion)
* ``rich`` — the banner, the mascot colors, and streamed Markdown-ish output

Those live behind the ``[cli]`` extra so the core SDK stays lean::

    pip install 'mantis-agent-sdk[cli]'

The whole REPL runs inside a single asyncio event loop (via ``anyio``) so the
provider's HTTP client and the prompt session share one loop: input is read
with ``PromptSession.prompt_async`` and the model is streamed with
``async for ev in agent.stream(...)`` in the same loop.

Configuration comes from the same env vars the rest of the SDK uses:

* ``MANTIS_AGENT_MODEL``     — default model slug (else ``qwen2.5-7b-instruct``)
* ``MANTIS_AGENT_BASE_URL``  — default backend (else Ollama at localhost:11434)
* ``MANTIS_AGENT_API_KEY``   — API key for hosted backends
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path
from typing import Any

from . import __version__

# ---------------------------------------------------------------------------
# Defaults (mirrors mantis-agent run/chat, sourced from the shared env vars)
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.environ.get("MANTIS_AGENT_MODEL", "qwen2.5-7b-instruct")
DEFAULT_BACKEND = os.environ.get("MANTIS_AGENT_BASE_URL", "http://localhost:11434")

# Mascot palette — a green praying mantis reared up in profile, facing right.
BODY = "#7cb342"  # mantis green
EYE_BG = "#0e1f08"  # near-black compound eye
ACCENT = "#9c6b3f"  # the reddish-brown antennae
LEG = "#6b9e35"  # green legs (a touch darker so they read apart from the body)
PALE = "#cddc9a"  # the pale inner face of the folded forearm

# The same example-prompt pool Claude Code samples for its placeholder.
EXAMPLE_PROMPTS = [
    "fix lint errors",
    "fix typecheck errors",
    "how does this project work?",
    "refactor this module",
    "how do I log an error?",
    "write a test for the parser",
    "explain the streaming dispatch",
    "create a util logging.py that...",
]

# Slash commands shown in the completion menu (command → one-line description).
SLASH_COMMANDS = {
    "/help": "show available commands",
    "/model": "switch the model for the next turns",
    "/clear": "clear the conversation history",
    "/cwd": "show the working directory",
    "/exit": "quit mantis",
    "/quit": "quit mantis",
}

# Permission-mode footer, cycled with shift+tab. The agent's tools (bash, write,
# edit) DO execute against the real machine; the footer is still cosmetic for now
# (no per-tool gating wired up yet) but it matches the Claude Code UX.
MODES = [
    ("default", "", "ansibrightblack"),
    ("accept edits on", "⏵⏵ ", "ansigreen"),
    ("plan mode on", "⏸ ", "ansicyan"),
    ("bypass permissions on", "⏵⏵ ", "ansired"),
]

# The "thinking" status line shown while the model works: a pulsing star, a
# whimsical gerund, and a live elapsed timer — e.g. ``✻ Undulating… (34s)``.
SPINNER_FRAMES = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"]  # a pulse
THINKING_WORDS = [
    "Thinking", "Pondering", "Cogitating", "Ruminating", "Percolating", "Musing",
    "Noodling", "Simmering", "Brewing", "Churning", "Conjuring", "Marinating",
    "Synthesizing", "Wrangling", "Vibing", "Computing", "Crunching", "Finagling",
    "Herding", "Incubating", "Manifesting", "Moseying", "Mulling", "Puttering",
    "Reticulating", "Spelunking", "Tinkering", "Transmuting", "Undulating",
    "Whirring", "Working", "Frolicking", "Honking", "Schlepping", "Smooshing",
    "Doodling", "Gallivanting", "Levitating", "Lollygagging", "Orbiting",
    "Pontificating", "Sublimating", "Swooping", "Whisking", "Zigzagging",
]
# ANSI 256-color so it renders the same in Terminal.app (no truecolor needed).
_SPIN_COL = "\033[38;5;113m"  # mantis green (matches the mascot)
_DIM_COL = "\033[38;5;240m"  # dim grey for the timer
_RESET = "\033[0m"
_CLEAR_LINE = "\r\033[K"  # carriage return + clear-to-end-of-line


class _Thinking:
    """Animated ``✻ Word… (Ns)`` status line, drawn on a transient terminal row.

    Runs as a detached asyncio task writing directly to stdout (rich strips the
    ``\\r`` we need), so it can be ``start()``/``stop()``-ed around each chunk of
    streamed output. The elapsed timer counts from the turn's first ``start()``;
    each ``start()`` picks a fresh gerund.
    """

    def __init__(self) -> None:
        self._task: Any = None
        self._started_at: float | None = None

    def start(self) -> None:
        import asyncio  # noqa: PLC0415
        import time  # noqa: PLC0415

        if self._task is not None and not self._task.done():
            return
        if self._started_at is None:
            self._started_at = time.monotonic()
        word = random.choice(THINKING_WORDS)
        self._task = asyncio.ensure_future(self._run(word))

    async def _run(self, word: str) -> None:
        import asyncio  # noqa: PLC0415
        import time  # noqa: PLC0415

        i = 0
        try:
            while True:
                frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
                elapsed = int(time.monotonic() - (self._started_at or 0))
                sys.stdout.write(
                    f"{_CLEAR_LINE}{_SPIN_COL}{frame} {word}…{_RESET} {_DIM_COL}({elapsed}s){_RESET}"
                )
                sys.stdout.flush()
                i += 1
                await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            # Don't clear here — stop() owns clearing, so there's no race where a
            # late teardown wipes the first line of freshly printed output.
            raise

    async def stop(self) -> None:
        import asyncio  # noqa: PLC0415

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        sys.stdout.write(_CLEAR_LINE)
        sys.stdout.flush()


def _missing_deps_message() -> str:
    return (
        "The `mantis` terminal needs the optional CLI dependencies.\n\n"
        "    pip install 'mantis-agent-sdk[cli]'\n\n"
        "(or: pip install prompt_toolkit rich)\n\n"
        "For a no-frills REPL with zero extra deps, use:  mantis-agent chat --model <m>"
    )


# ---------------------------------------------------------------------------
# Mascot + banner
# ---------------------------------------------------------------------------


def _mascot_lines(Text: Any) -> list[Any]:
    """Render a praying mantis reared up in profile (facing right) as Text rows.

    Drawn as a small pixel *bitmap* and rasterized with half-block glyphs
    (``▀``/``▄``/``█``) so each character cell packs two vertical pixels —
    doubling the vertical resolution and letting one cell carry two colors
    (``▀`` painted ``fg on bg``). That smoothness is what lets it read as a real
    insect rather than ASCII art. Pose mirrors the classic alert stance: the
    abdomen lies low to the left, the prothorax rears up to the right into a
    triangular head with a compound eye and long swept antennae, the raptorial
    forelegs fold in front in the "praying" pose, and it stands on bent legs.
    """
    BODYV, EYEV, ANTV, LEGV, PALEV = 1, 2, 3, 4, 5
    palette = {BODYV: BODY, EYEV: EYE_BG, ANTV: ACCENT, LEGV: LEG, PALEV: PALE}
    W, H = 22, 16
    grid = [[0] * W for _ in range(H)]

    def pt(x: float, y: float, v: int = BODYV) -> None:
        xi, yi = int(round(x)), int(round(y))
        if v and 0 <= yi < H and 0 <= xi < W:
            grid[yi][xi] = v

    def line(x0: float, y0: float, x1: float, y1: float, v: int = BODYV, t: int = 1) -> None:
        x0, y0, x1, y1 = (int(round(n)) for n in (x0, y0, x1, y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, x, y = dx + dy, x0, y0
        while True:
            for o in range(t):
                pt(x, y + o, v)
            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x += sx
            if e2 <= dx:
                err += dx
                y += sy

    def band(ctl: list[tuple]) -> None:
        """Thick tapering body stroke through (x, center-y, thickness) points."""
        for i in range(len(ctl) - 1):
            x0, y0, t0 = ctl[i]
            x1, y1, t1 = ctl[i + 1]
            lo, hi = (x1, x0) if x1 < x0 else (x0, x1)
            for x in range(lo, hi + 1):
                f = (x - x0) / (x1 - x0) if x1 != x0 else 0
                yc = y0 + (y1 - y0) * f
                th = t0 + (t1 - t0) * f
                for y in range(int(round(yc - th / 2)), int(round(yc + th / 2))):
                    pt(x, y)

    band([(2, 12, 2), (5, 11, 2), (8, 10, 3)])  # slim abdomen, low to the left
    line(8, 11, 14, 4, BODYV, 3)  # prothorax rearing up STEEPLY (not horizontal)

    # Small triangular head + compound eye at the top of the reared neck.
    line(14, 4, 18, 3, BODYV, 1)
    line(18, 3, 15, 6, BODYV, 1)
    line(14, 4, 15, 6, BODYV, 1)
    pt(15, 4, BODYV)
    pt(16, 5, BODYV)
    pt(16, 4, EYEV)

    line(17, 3, 20, 0, ANTV)  # long antennae swept up-right
    line(16, 3, 18, 0, ANTV)

    # The signature: bold raptorial forelegs folded in the "praying" pose,
    # held out in front — femur up to the head, pale forearm folded back.
    line(11, 10, 17, 5, BODYV, 2)
    line(17, 5, 12, 9, PALEV, 2)
    line(12, 9, 14, 10, BODYV, 1)  # the grasping tip

    for hx, kx, ky, fx, fy in ((7, 4, 14, 2, 15), (9, 8, 14, 7, 15), (10, 12, 14, 13, 15)):
        line(hx, 10, kx, ky, LEGV)  # thin femur to the knee
        line(kx, ky, fx, fy, LEGV)  # thin tibia to the foot

    rows: list[Any] = []
    for r in range(0, H, 2):
        t = Text()
        for x in range(W):
            top = grid[r][x]
            bot = grid[r + 1][x] if r + 1 < H else 0
            if not top and not bot:
                t.append(" ")
            elif top and not bot:
                t.append("▀", style=palette[top])
            elif bot and not top:
                t.append("▄", style=palette[bot])
            elif top == bot:
                t.append("█", style=palette[top])
            else:
                t.append("▀", style=f"{palette[top]} on {palette[bot]}")
        rows.append(t)
    return rows


def _short_cwd() -> str:
    cwd = Path.cwd()
    home = Path.home()
    try:
        return "~/" + str(cwd.relative_to(home))
    except ValueError:
        return str(cwd)


def print_banner(console: Any, model: str, backend: str) -> int:
    """Print the banner; return the number of terminal lines it occupied."""
    from rich.table import Table  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    mascot = _mascot_lines(Text)

    title = Text()
    title.append("Mantis", style=f"bold {BODY}")
    title.append(" Code ", style="bold white")
    title.append(f"v{__version__}", style="ansibrightblack")

    where = "Ollama (local)" if "localhost" in backend or "127.0.0.1" in backend else backend
    sub = Text()
    sub.append(model, style="white")
    sub.append("  ·  ", style="ansibrightblack")
    sub.append(where, style="ansibrightblack")

    cwd = Text(_short_cwd(), style="ansibrightblack")

    # Vertically center the 3 info lines against the (taller) mascot.
    blank = Text("")
    top_pad = max(0, (len(mascot) - 3) // 2)
    info = [blank] * top_pad + [title, sub, cwd]
    info += [blank] * (len(mascot) - len(info))

    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left")
    for i in range(len(mascot)):
        grid.add_row(mascot[i], info[i])

    console.print()
    console.print(grid)
    console.print()
    console.print(
        Text(' tip: type a request and press Enter · /help for commands · /exit to quit',
             style="ansibrightblack")
    )
    console.print()
    # blank + grid + blank + tip + blank
    return len(mascot) + 4


# ---------------------------------------------------------------------------
# The REPL
# ---------------------------------------------------------------------------


class MantisTUI:
    """Stateful interactive session. One Agent, one message history, one loop."""

    def __init__(self, *, model: str, backend: str, api_key: str | None,
                 system: str | None, max_tokens: int, temperature: float | None,
                 max_turns: int) -> None:
        self.model = model
        self.backend = backend
        self.api_key = api_key
        self.system = system
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_turns = max_turns
        self.mode_idx = 0
        self.messages: list[Any] = []
        self.agent: Any = None

        from rich.console import Console  # noqa: PLC0415

        self.console = Console()

    # -- provider / agent wiring (mirrors cli._build_provider_for_args) ------

    def _build_agent(self) -> Any:
        from .agent import Agent  # noqa: PLC0415
        from .builtin_tools import CODING_TOOLS  # noqa: PLC0415
        from .providers.base import detect_provider, resolve  # noqa: PLC0415
        from .tools import ToolRegistry  # noqa: PLC0415

        name = detect_provider(self.backend or self.model)
        factory = resolve(name)
        kwargs: dict[str, Any] = {}
        if self.backend and self.backend.startswith(("http://", "https://")):
            kwargs["base_url"] = self.backend
        if self.api_key:
            kwargs["api_key"] = self.api_key
        try:
            provider = factory(**kwargs)
        except TypeError:
            provider = factory()

        # The whole point of `mantis` (vs. the bare `chat` REPL): give the model
        # a real tool belt — shell + filesystem — so it can actually *do* things
        # instead of describing them. Without these the agent loop has nothing to
        # call and every turn collapses to a single chat completion.
        registry = ToolRegistry()
        registry.add(*CODING_TOOLS)

        return Agent(
            model=self.model,
            provider=provider,
            system=self.system or self._default_system(),
            tools=registry,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            max_steps=self.max_turns,
        )

    def _default_system(self) -> str:
        """The agent system prompt — what makes the model behave like a coding
        agent (use tools, act, stay terse) rather than a generic chat assistant."""
        import platform  # noqa: PLC0415

        return (
            "You are Mantis, an interactive coding agent running in the user's "
            "terminal. You have tools — bash, read_file, write_file, edit_file, "
            "ls, glob, grep — and you complete tasks by CALLING them, not by "
            "describing what to do.\n\n"
            "ACT IMMEDIATELY. On the very first message, if the task can be done "
            "with a tool, call the tool right away. Do NOT explain the command "
            "first, do NOT print a command in a code block, and do NOT wait for "
            "the user to say 'run it' or to confirm — just run it and then report "
            "the result. The user asking is the permission; they are watching the "
            "output.\n\n"
            "Rules:\n"
            "- Never tell the user to run a command themselves. Run it yourself "
            "with bash and show them the actual output.\n"
            "- 'find/show/list/check X' = call the tool now and report the real "
            "result. Never answer from memory or guess.\n"
            "- Only after a tool runs do you write prose, and keep it to a short "
            "summary of what the output means. Let the tool output speak.\n"
            "- Only the user's machine matters — never enumerate other operating "
            "systems, generic instructions, or hypotheticals.\n\n"
            "Example — user: 'find services on port 3000'. WRONG: explaining lsof "
            "in a code block. RIGHT: immediately call bash with "
            "`lsof -i :3000 -sTCP:LISTEN`, then summarize what's listening.\n\n"
            f"Environment: {platform.system()} ({platform.machine()}), "
            f"cwd = {Path.cwd()}."
        )

    # -- model resolution (so `mantis` "just works") -------------------------

    def _available_models(self) -> tuple[list[str], bool]:
        """Probe the backend for installed models.

        Returns ``(model_names, reachable)``. Ollama is asked via ``/api/tags``;
        anything else via the OpenAI-compat ``/v1/models``. Short timeout so a
        down/missing backend doesn't stall startup.
        """
        import httpx  # noqa: PLC0415

        from .providers.base import detect_provider  # noqa: PLC0415

        base = (self.backend or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            return [], False
        kind = detect_provider(self.backend or self.model)
        try:
            with httpx.Client(timeout=2.5) as c:
                if kind == "ollama":
                    r = c.get(f"{base}/api/tags")
                    r.raise_for_status()
                    return [m.get("name", "") for m in r.json().get("models", [])], True
                headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                r = c.get(f"{base.rstrip('/v1')}/v1/models", headers=headers)
                r.raise_for_status()
                return [m.get("id", "") for m in r.json().get("data", [])], True
        except Exception:  # noqa: BLE001 — unreachable / non-2xx / bad JSON
            return [], False

    def _pick_model(self, model: str, available: list[str]) -> str:
        """Choose the closest installed model to ``model``."""
        if model in available:
            return model
        base = model.split(":")[0]
        # Prefer same base (e.g. "qwen2.5-7b-instruct" → "qwen2.5:7b"), then
        # any instruct-tuned chat model, then just the first installed one.
        for cand in available:
            if cand.split(":")[0] == base:
                return cand
        for cand in available:
            if any(k in cand.lower() for k in ("instruct", "chat", "qwen", "llama")):
                return cand
        return available[0]

    def _resolve_model(self) -> None:
        """Point ``self.model`` at something that actually exists, or explain how
        to get it. Called once at startup, before the banner is drawn."""
        available, reachable = self._available_models()
        if self.model in available:
            return
        if not reachable:
            # Backend down or not an HTTP backend — leave the model as-is; the
            # first turn will surface the real connection error. Hint for Ollama.
            if "localhost" in (self.backend or "") or "127.0.0.1" in (self.backend or ""):
                self.console.print(
                    f"[ansiyellow]![/] [ansibrightblack]can't reach Ollama at "
                    f"{self.backend} — is it running? ([white]ollama serve[/])[/]"
                )
            return
        if not available:
            self.console.print(
                f"[ansiyellow]![/] [ansibrightblack]no models installed on "
                f"{self.backend}. Pull one:[/] [white]ollama pull {self.model}[/]"
            )
            return
        picked = self._pick_model(self.model, available)
        if picked != self.model:
            self.console.print(
                f"[ansibrightblack]([white]{self.model}[/] not installed — using "
                f"[white]{picked}[/]; override with [white]/model <name>[/] or "
                f"[white]--model[/])[/]"
            )
            self.model = picked

    # -- prompt session ------------------------------------------------------

    def _build_session(self) -> Any:
        from prompt_toolkit import PromptSession  # noqa: PLC0415
        from prompt_toolkit.completion import WordCompleter  # noqa: PLC0415
        from prompt_toolkit.formatted_text import HTML  # noqa: PLC0415
        from prompt_toolkit.key_binding import KeyBindings  # noqa: PLC0415
        from prompt_toolkit.styles import Style  # noqa: PLC0415

        kb = KeyBindings()

        @kb.add("s-tab")
        def _cycle_mode(event: Any) -> None:  # noqa: ANN401
            self.mode_idx = (self.mode_idx + 1) % len(MODES)
            event.app.invalidate()

        completer = WordCompleter(
            ["/help", "/clear", "/model", "/cwd", "/exit", "/quit"],
            sentence=True,
        )

        def bottom_toolbar() -> Any:
            label, symbol, color = MODES[self.mode_idx]
            if self.mode_idx == 0:
                left = "  ? for shortcuts"
            else:
                left = f"  {symbol}{label} (shift+tab to cycle)"
            right = f"{self.model} "
            pad = " " * max(1, 70 - len(left) - len(right))
            return HTML(
                f'<style fg="{self._toolbar_fg()}">{left}</style>'
                f'{pad}<style fg="ansibrightblack">{right}</style>'
            )

        style = Style.from_dict({
            "prompt": BODY,
            "placeholder": "ansibrightblack",
        })

        return PromptSession(
            key_bindings=kb,
            completer=completer,
            complete_while_typing=True,
            bottom_toolbar=bottom_toolbar,
            style=style,
            multiline=False,
        )

    def _toolbar_fg(self) -> str:
        return MODES[self.mode_idx][2]

    def _placeholder(self) -> Any:
        from prompt_toolkit.formatted_text import HTML  # noqa: PLC0415
        from html import escape  # noqa: PLC0415

        prompt = random.choice(EXAMPLE_PROMPTS)
        return HTML(f'<style fg="ansibrightblack">Try "{escape(prompt)}"</style>')

    # -- running a single turn (the real agent loop) ------------------------

    async def _run_turn(self, text: str) -> None:
        """Drive one user turn through the full agentic loop.

        Unlike a chat REPL, this uses :meth:`Agent.run_iter` — the loop that
        actually *executes* tool calls and feeds their results back to the
        model, iterating until the model stops asking for tools. We render each
        message as it finalizes: assistant prose, the tool calls it requested,
        and the (truncated) tool results.

        ``run_iter`` mutates ``self.messages`` in place, so on interrupt/error
        we rewind to ``base`` — dropping the whole half-finished turn (including
        the user message) so the conversation never carries a dangling
        ``tool_use`` with no matching ``tool_result``.
        """
        from .types import (  # noqa: PLC0415
            AssistantMessage,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        base = len(self.messages)
        self.messages.append(UserMessage(content=text))

        thinking = _Thinking()
        thinking.start()
        try:
            async for msg in self.agent.run_iter(self.messages):
                await thinking.stop()
                if isinstance(msg, AssistantMessage):
                    self._render_assistant(msg, ToolUseBlock)
                elif isinstance(msg, UserMessage) and not getattr(msg, "isMeta", False):
                    self._render_tool_results(msg, ToolResultBlock)
                thinking.start()
        except KeyboardInterrupt:
            del self.messages[base:]
            await thinking.stop()
            self.console.print("\n[ansibrightblack](interrupted)[/]")
            raise
        except Exception:
            del self.messages[base:]
            raise
        finally:
            await thinking.stop()

    def _render_assistant(self, msg: Any, ToolUseBlock: type) -> None:
        from rich.markdown import Markdown  # noqa: PLC0415

        from .types import TextBlock  # noqa: PLC0415

        for block in msg.content:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    self.console.print()
                    self.console.print(f"[{BODY}]●[/] ", end="")
                    # Render the assistant's markdown (code fences, bold, lists,
                    # tables). `ansi_dark` keeps code highlighting to ANSI colors
                    # so it looks right in Terminal.app (no truecolor needed).
                    self.console.print(Markdown(block.text.strip(), code_theme="ansi_dark"))
            elif isinstance(block, ToolUseBlock):
                self.console.print(
                    f"[{LEG}]⚒[/] [white]{block.name}[/]"
                    f"[ansibrightblack]({self._fmt_args(block.input)})[/]"
                )

    def _render_tool_results(self, msg: Any, ToolResultBlock: type) -> None:
        for block in msg.content:
            if not isinstance(block, ToolResultBlock):
                continue
            body = block.content if isinstance(block.content, str) else str(block.content)
            body = body.strip()
            preview = "\n".join(body.splitlines()[:12])
            if body.count("\n") >= 12:
                preview += "\n  …"
            indented = "\n".join("  " + ln for ln in preview.splitlines()) or "  (no output)"
            style = "ansired" if block.is_error else "ansibrightblack"
            self.console.print(f"[{style}]{indented}[/]", markup=False if block.is_error else True)
            if block.is_error:
                self.console.print("[ansired]  ↑ error[/]")

    @staticmethod
    def _fmt_args(args: dict[str, Any]) -> str:
        """One-line summary of a tool call's input, truncated for display."""
        parts = []
        for k, v in args.items():
            s = v if isinstance(v, str) else repr(v)
            s = s.replace("\n", " ")
            if len(s) > 60:
                s = s[:57] + "…"
            parts.append(f"{k}={s}")
        out = ", ".join(parts)
        return out[:120] + "…" if len(out) > 120 else out

    # -- slash commands ------------------------------------------------------

    async def _handle_slash(self, line: str) -> bool:
        """Return True if the line was a command (loop should continue)."""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/exit", "/quit"):
            raise EOFError
        if cmd == "/help":
            self.console.print(
                "\n[bold]commands[/]\n"
                "  [white]/models[/]               local + self-host + hosted catalog\n"
                "  [white]/model[/] <id>           switch to a model\n"
                "  [white]/enable[/] <provider>    turn on a hosted provider (saves API key)\n"
                "  [white]/disable[/] <provider>   forget a provider's saved key\n"
                "  [white]/connect[/] <url> [model] point at your own server (vLLM/llama.cpp/TGI)\n"
                "  [white]/clear[/]                clear conversation history\n"
                "  [white]/cwd[/]                  show working directory\n"
                "  [white]/exit[/]                 quit (also Ctrl+D)\n"
                "\n[ansibrightblack]shift+tab cycles the mode footer · Ctrl+C stops a running reply[/]\n"
            )
            return True
        if cmd == "/clear":
            self.messages = []
            self.console.print("[ansibrightblack](history cleared)[/]")
            return True
        if cmd == "/cwd":
            self.console.print(f"[ansibrightblack]{Path.cwd()}[/]")
            return True
        if cmd == "/models":
            await self._select_model()
            return True
        if cmd in ("/enable", "/disable"):
            await self._cmd_enable(cmd, arg)
            return True
        if cmd == "/connect":
            await self._cmd_connect(arg)
            return True
        if cmd == "/model":
            if arg:
                await self._switch_model(arg)
            else:
                await self._select_model()
            return True
        # Unknown slash command - let it fall through as a normal prompt.
        return False

    # -- model catalog -------------------------------------------------------

    def _show_models(self) -> None:
        """Render the catalog: local (Ollama), self-host, hosted providers."""
        from . import catalog  # noqa: PLC0415

        installed, reachable = self._available_models()
        installed_set = set(installed)
        c = self.console

        # 1. Local (Ollama)
        c.print()
        status = "[ansigreen]●[/]" if reachable else "[ansibrightblack]○ not running[/]"
        c.print(f"{status} [bold]Ollama[/] [ansibrightblack]— local, free, no key[/]")
        for m in installed:
            mark = "[ansigreen]›[/]" if m == self.model else " "
            c.print(f"  {mark} [white]{m}[/] [ansibrightblack][installed][/]")
        for p in catalog.SUGGESTED_PULLS:
            if p.tag in installed_set:
                continue
            c.print(
                f"    [ansibrightblack]{p.tag:<22}[/] [ansibrightblack]{p.note}[/]"
            )
        if not installed and not reachable:
            c.print("    [ansibrightblack](start it with [white]ollama serve[/])[/]")
        c.print("  [ansibrightblack]pull any with [white]ollama pull <name>[/][/]")

        # 2. Self-host
        c.print("\n[bold]Self-host[/] [ansibrightblack]— your own GPU[/]")
        c.print(f"  [ansibrightblack]{catalog.SELF_HOST_NOTE}[/]")

        # 3. Hosted APIs
        c.print("\n[bold]Hosted APIs[/] [ansibrightblack]— full models, need a key[/]")
        for prov in catalog.CATALOG:
            on = catalog.is_enabled(prov)
            dot = "[ansigreen]●[/]" if on else "[ansibrightblack]○[/]"
            head = f"{dot} [bold]{prov.label}[/]"
            if on:
                head += "  [ansigreen]enabled[/]"
            else:
                head += f"  [ansibrightblack]/enable {prov.id}[/]"
            if prov.note:
                head += f"  [ansibrightblack]— {prov.note}[/]"
            c.print(head)
            for m in prov.models:
                mark = "[ansigreen]›[/]" if m == self.model else " "
                color = "white" if on else "ansibrightblack"
                c.print(f"  {mark} [{color}]{m}[/]")
        c.print(
            "\n[ansibrightblack]switch with [white]/model <id>[/] · "
            "enable with [white]/enable <provider>[/][/]\n"
        )

    # -- interactive model selector -----------------------------------------

    # -- generic arrow-key picker (inline, self-erasing) ---------------------

    async def _pick(self, title: str, items: list[dict], start_index: int = 0) -> dict | None:
        """Inline picker. ``items`` are header rows ({"kind":"header","text"}) or
        selectable rows ({"kind":"item","label","value","hint"?,"enabled"?}).
        Returns the chosen item dict, or None on Esc. Non-full-screen +
        erase_when_done so it cleanly disappears and the chat continues."""
        from prompt_toolkit.application import Application  # noqa: PLC0415
        from prompt_toolkit.data_structures import Point  # noqa: PLC0415
        from prompt_toolkit.key_binding import KeyBindings  # noqa: PLC0415
        from prompt_toolkit.layout import HSplit, Layout, Window  # noqa: PLC0415
        from prompt_toolkit.layout.controls import FormattedTextControl  # noqa: PLC0415
        from prompt_toolkit.layout.dimension import D  # noqa: PLC0415
        from prompt_toolkit.styles import Style  # noqa: PLC0415

        selectable = [i for i, it in enumerate(items) if it.get("kind") == "item"]
        if not selectable:
            self.console.print("[ansibrightblack]nothing to pick[/]")
            return None
        st = {"sel": max(0, min(start_index, len(selectable) - 1))}

        def frags() -> list:
            cur = selectable[st["sel"]]
            out: list = []
            for i, it in enumerate(items):
                if it.get("kind") != "item":
                    out.append(("class:hdr", it["text"] + "\n"))
                    continue
                ptr = "❯ " if i == cur else "  "
                line = f"{ptr}{it['label']}"
                hint = it.get("hint", "")
                cls = "class:cur" if i == cur else ("class:on" if it.get("enabled", True) else "class:off")
                out.append((cls, line))
                if hint:
                    out.append(("class:hint", f"   {hint}"))
                out.append(("", "\n"))
            return out

        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _u(_e: Any) -> None:
            st["sel"] = (st["sel"] - 1) % len(selectable)

        @kb.add("down")
        @kb.add("c-n")
        def _d(_e: Any) -> None:
            st["sel"] = (st["sel"] + 1) % len(selectable)

        @kb.add("enter")
        def _ok(e: Any) -> None:
            e.app.exit(result=items[selectable[st["sel"]]])

        @kb.add("escape")
        @kb.add("c-c")
        @kb.add("q")
        def _no(e: Any) -> None:
            e.app.exit(result=None)

        control = FormattedTextControl(
            frags, focusable=True, show_cursor=False,
            get_cursor_position=lambda: Point(0, selectable[st["sel"]]),
        )
        head = Window(FormattedTextControl(lambda: [("class:title", title + "\n")]), height=1)
        body = Window(control, height=D(max=20), wrap_lines=False)
        style = Style.from_dict({
            "title": "#888888", "hdr": f"{BODY} bold",
            "cur": f"#0b1605 bg:{BODY} bold", "on": "#ffffff",
            "off": "#777777", "hint": "#777777",
        })
        app: Any = Application(
            layout=Layout(HSplit([head, body])), key_bindings=kb, style=style,
            full_screen=False, erase_when_done=True, mouse_support=True,
        )
        return await app.run_async()

    # -- model catalog & selection ------------------------------------------

    def _provider_live_models(self, prov: Any) -> list[str] | None:
        """Live ``/v1/models`` for an enabled provider (cached, best-effort)."""
        from . import catalog  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        cache = self.__dict__.setdefault("_live_models", {})
        if prov.id in cache:
            return cache[prov.id]
        key = catalog.api_key_for(prov)
        out: list[str] | None = None
        try:
            with httpx.Client(timeout=2.5) as c:
                r = c.get(f"{prov.base_url.rstrip('/')}/models",
                          headers={"Authorization": f"Bearer {key}"} if key else {})
                r.raise_for_status()
                ids = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
                out = ids or None
        except Exception:  # noqa: BLE001
            out = None
        cache[prov.id] = out
        return out

    def _catalog_rows(self) -> list[dict]:
        """Picker rows: local Ollama first, then every hosted provider. Enabled
        providers show their live model list; disabled ones the curated menu."""
        from . import catalog  # noqa: PLC0415

        installed, reachable = self._available_models()
        rows: list[dict] = []

        tag = "" if reachable else "  (ollama not running)"
        rows.append({"kind": "header", "text": f"  Ollama · local, free{tag}"})
        if installed:
            for m in installed:
                rows.append({"kind": "item", "label": m, "enabled": True,
                             "value": {"model": m, "provider": None},
                             "hint": "← current" if m == self.model else ""})
        else:
            rows.append({"kind": "header", "text": "    (none pulled — ollama pull <name>)"})

        for prov in catalog.CATALOG:
            on = catalog.is_enabled(prov)
            extra = "  · enabled" if on else "  · enter → API key or self-host"
            rows.append({"kind": "header", "text": f"  {prov.label}{extra}"})
            live = self._provider_live_models(prov) if on else None
            models = (live[:14] if live else list(prov.models))
            for m in models:
                rows.append({"kind": "item", "label": m, "enabled": on,
                             "value": {"model": m, "provider": prov},
                             "hint": "← current" if m == self.model else ""})
        return rows

    async def _select_model(self) -> None:
        rows = self._catalog_rows()
        sel_models = [r for r in rows if r.get("kind") == "item"]
        start = 0
        for j, r in enumerate(sel_models):
            if r["value"]["model"] == self.model:
                start = j
                break
        choice = await self._pick(
            "select a model   ↑↓ move · enter pick · esc cancel", rows, start)
        if not choice:
            self.console.print("[ansibrightblack](cancelled)[/]")
            return
        await self._activate(choice["value"]["model"], choice["value"]["provider"])

    async def _activate(self, model: str, prov: Any) -> None:
        """Run ``model``: local immediately; hosted-enabled via API; otherwise
        offer the run-mode choice (provider API key OR self-host)."""
        from . import catalog  # noqa: PLC0415

        if prov is None:
            await self._apply(model, DEFAULT_BACKEND, None, "")
            return
        if catalog.is_enabled(prov):
            await self._apply(model, prov.base_url, catalog.api_key_for(prov),
                              f" [ansibrightblack]via {prov.label}[/]")
            return

        mode = await self._pick(
            f"run {model} — how?",
            [
                {"kind": "header", "text": f"  {prov.label} · {model}"},
                {"kind": "item", "label": f"{prov.label} API",
                 "value": "api", "hint": f"paste {prov.api_key_env} · pay {prov.label}"},
                {"kind": "item", "label": "Self-host",
                 "value": "selfhost", "hint": "your own server (vLLM / llama.cpp / TGI)"},
                {"kind": "item", "label": "Cancel", "value": "cancel"},
            ],
        )
        if not mode or mode["value"] == "cancel":
            self.console.print("[ansibrightblack](cancelled)[/]")
            return

        if mode["value"] == "api":
            key = await self._prompt_secret(
                f"paste {prov.label} API key ({prov.api_key_env})", "key › ")
            if not key:
                self.console.print("[ansibrightblack](cancelled)[/]")
                return
            catalog.set_key(prov.id, key)
            self.console.print(
                f"[ansigreen]✓[/] enabled [bold]{prov.label}[/] "
                "[ansibrightblack](saved to ~/.mantis-agent/models.json, chmod 600)[/]")
            self.__dict__.get("_live_models", {}).pop(prov.id, None)
            await self._apply(model, prov.base_url, key,
                              f" [ansibrightblack]via {prov.label}[/]")
        else:
            url = await self._prompt_text(
                f"self-host URL serving {model}", "url › ", "http://localhost:8000/v1")
            if not url or not url.startswith(("http://", "https://")):
                self.console.print("[ansibrightblack](cancelled — need an http(s) url)[/]")
                return
            await self._apply(model, url, self.api_key or "sk-noauth",
                              " [ansibrightblack]· self-hosted[/]")

    async def _apply(self, model: str, backend: str, api_key: str | None, where: str) -> None:
        """Point the live agent at (model, backend, key) and rebuild it."""
        self.model = model
        self.backend = backend
        self.api_key = api_key
        if self.agent is not None:
            await self.agent.aclose()
        self.agent = self._build_agent()
        self.console.print(f"[ansibrightblack]model →[/] [white]{model}[/]{where}")

    async def _prompt_secret(self, msg: str, prompt: str) -> str:
        self.console.print(f"[ansibrightblack]{msg} — input hidden, Enter to cancel[/]")
        try:
            v = await self.session.prompt_async(prompt, is_password=True)
        except (EOFError, KeyboardInterrupt):
            v = ""
        return (v or "").strip()

    async def _prompt_text(self, msg: str, prompt: str, default: str = "") -> str:
        self.console.print(f"[ansibrightblack]{msg} — Enter to accept default / cancel[/]")
        try:
            v = await self.session.prompt_async(prompt, default=default)
        except (EOFError, KeyboardInterrupt):
            v = ""
        return (v or "").strip()

    async def _cmd_enable(self, cmd: str, arg: str) -> None:
        from . import catalog  # noqa: PLC0415

        parts = arg.split(maxsplit=1)
        pid = parts[0].lower() if parts else ""
        inline_key = parts[1].strip() if len(parts) > 1 else ""

        prov = catalog.BY_ID.get(pid)
        if not prov:
            ids = ", ".join(p.id for p in catalog.CATALOG)
            self.console.print(
                f"[ansired]unknown provider[/] [white]{pid or '<none>'}[/]  "
                f"[ansibrightblack](try: {ids})[/]")
            return

        if cmd == "/disable":
            if catalog.clear_key(prov.id):
                self.__dict__.get("_live_models", {}).pop(prov.id, None)
                self.console.print(f"[ansibrightblack]forgot saved key for {prov.label}[/]")
            else:
                self.console.print(f"[ansibrightblack]no saved key for {prov.label}[/]")
            return

        key = inline_key or await self._prompt_secret(
            f"paste {prov.label} API key ({prov.api_key_env})", "key › ")
        if not key:
            self.console.print("[ansibrightblack](cancelled)[/]")
            return
        catalog.set_key(prov.id, key)
        self.__dict__.get("_live_models", {}).pop(prov.id, None)
        self.console.print(
            f"[ansigreen]✓[/] enabled [bold]{prov.label}[/] "
            f"[ansibrightblack](saved, chmod 600)[/]  "
            f"[ansibrightblack]try [white]/model {prov.models[0]}[/][/]")

    async def _cmd_connect(self, arg: str) -> None:
        """Self-host: /connect <url> [model]."""
        parts = arg.split()
        if not parts or not parts[0].startswith(("http://", "https://")):
            self.console.print(
                "[ansibrightblack]usage: [white]/connect <url> [model][/] — e.g. "
                "[white]/connect http://gpu-box:8000/v1 openai/gpt-oss-120b[/][/]")
            return
        model = parts[1] if len(parts) > 1 else self.model
        await self._apply(model, parts[0], self.api_key or "sk-noauth",
                          " [ansibrightblack]· self-hosted[/]")

    async def _switch_model(self, model_id: str) -> None:
        """`/model <id>` — switch, auto-wiring backend + key from the catalog."""
        from . import catalog  # noqa: PLC0415

        prov = catalog.provider_for_model(model_id)
        await self._activate(model_id, prov)

    # -- main loop -----------------------------------------------------------

    async def run(self) -> int:
        self._resolve_model()  # so the banner + first turn use a model that exists
        # Clear to a fresh screen, print the banner at the top, and let the
        # input sit right beneath it. We deliberately do NOT pad down to the
        # bottom of the terminal: padding overflows on tall/narrow windows (the
        # banner text wraps, the math under-counts) and scrolls the mascot off
        # the top, leaving a huge void with a stranded prompt. Banner-then-input
        # is robust at every size; the conversation fills downward from there.
        try:
            self.console.clear()
        except Exception:  # noqa: BLE001 - non-tty / dumb terminal
            pass
        print_banner(self.console, self.model, self.backend)
        session = self._build_session()
        self.session = session
        self.agent = self._build_agent()

        try:
            while True:
                try:
                    line = await session.prompt_async(
                        "› ", placeholder=self._placeholder()
                    )
                except (EOFError, KeyboardInterrupt):
                    self.console.print("\n[ansibrightblack]bye 👋[/]")
                    break

                line = (line or "").strip()
                if not line:
                    continue
                if line.startswith("/") and await self._handle_slash(line):
                    continue

                try:
                    # _run_turn already rewinds self.messages on interrupt/error,
                    # so history stays coherent without extra cleanup here.
                    await self._run_turn(line)
                except KeyboardInterrupt:
                    continue
                except Exception as e:  # noqa: BLE001
                    self.console.print(f"\n[ansired]error:[/] {e}")
                    msg = str(e).lower()
                    if "not found" in msg or "pull" in msg:
                        self.console.print(
                            f"[ansibrightblack]→ install it:[/] [white]ollama pull "
                            f"{self.model}[/]  [ansibrightblack]or pick another with[/] "
                            f"[white]/model <name>[/]"
                        )
        finally:
            if self.agent is not None:
                await self.agent.aclose()
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse  # noqa: PLC0415

    p = argparse.ArgumentParser(
        prog="mantis",
        description="Mantis — interactive agent terminal (Claude-Code-style TUI).",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help="Model slug.")
    p.add_argument("--backend", default=DEFAULT_BACKEND, help="Backend base URL.")
    p.add_argument("--api-key", default=os.environ.get("MANTIS_AGENT_API_KEY"),
                   help="API key (else env MANTIS_AGENT_API_KEY).")
    p.add_argument("--system", default=None, help="System prompt.")
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--max-turns", type=int, default=20)
    args = p.parse_args(argv)

    # Dependency preflight with a friendly message.
    try:
        import prompt_toolkit  # noqa: F401, PLC0415
        import rich  # noqa: F401, PLC0415
    except ModuleNotFoundError:
        print(_missing_deps_message(), file=sys.stderr)
        return 1

    import anyio  # noqa: PLC0415

    tui = MantisTUI(
        model=args.model,
        backend=args.backend,
        api_key=args.api_key,
        system=args.system,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        max_turns=args.max_turns,
    )
    try:
        return anyio.run(tui.run)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
