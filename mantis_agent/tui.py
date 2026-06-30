"""``mantis`` — the interactive terminal UI.

This is the Claude-Code-style agent terminal: run ``mantis`` in any directory
and you get a banner (pixel mascot + version + model + cwd), a bordered input
box with a rotating ``Try "…"`` placeholder, a mode footer you can cycle with
``shift+tab``, slash commands, and token-level streaming from a real model.

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

# Mascot palette — a green praying mantis, side profile.
BODY = "#8bc34a"  # mantis green
EYE_BG = "#0c1a05"  # near-black eye
ACCENT = "#c0ca33"  # antennae (a warmer lime so they catch the eye)

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

# Permission-mode footer, cycled with shift+tab. Cosmetic in this terminal
# (there is no edit/exec sandbox here yet) but it matches the Claude Code UX.
MODES = [
    ("default", "", "ansibrightblack"),
    ("accept edits on", "⏵⏵ ", "ansigreen"),
    ("plan mode on", "⏸ ", "ansicyan"),
    ("bypass permissions on", "⏵⏵ ", "ansired"),
]


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
    """Render a side-profile praying mantis as rich ``Text`` rows.

    Drawn as a small pixel *bitmap* (body=green, eye=dark, antennae=lime) and
    rasterized with half-block glyphs (``▀``/``▄``/``█``) so each character cell
    packs two vertical pixels — doubling the vertical resolution and letting a
    single cell carry two colors (``▀`` with ``fg on bg``). That smoothness is
    what makes the silhouette read as an insect rather than ASCII art: a
    triangular head with a compound eye, swept antennae, the raptorial forelegs
    folded in the signature "praying" pose, an arched body, and three legs.
    """
    BODYV, EYEV, ANTV = 1, 2, 3
    palette = {BODYV: BODY, EYEV: EYE_BG, ANTV: ACCENT}
    W, H = 28, 18
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

    def filltri(p0: tuple, p1: tuple, p2: tuple, v: int = BODYV) -> None:
        ys = [p[1] for p in (p0, p1, p2)]
        for y in range(min(ys), max(ys) + 1):
            xi = []
            for (ax, ay), (bx, by) in ((p0, p1), (p1, p2), (p2, p0)):
                if ay <= y < by or by <= y < ay:
                    xi.append(ax + (bx - ax) * (y - ay) / (by - ay))
            if len(xi) >= 2:
                for x in range(int(round(min(xi))), int(round(max(xi))) + 1):
                    pt(x, y, v)

    # Arched body: control points (x, center-y, thickness), spans filled solid.
    ctl = [(8, 9, 4), (12, 8, 4), (17, 8, 4), (21, 7, 4), (25, 6, 2)]
    for i in range(len(ctl) - 1):
        x0, y0, t0 = ctl[i]
        x1, y1, t1 = ctl[i + 1]
        for x in range(x0, x1 + 1):
            f = (x - x0) / (x1 - x0) if x1 != x0 else 0
            yc = y0 + (y1 - y0) * f
            th = t0 + (t1 - t0) * f
            for y in range(int(round(yc - th / 2)), int(round(yc + th / 2))):
                pt(x, y)
    line(24, 5, 27, 3, BODYV, 2)  # abdomen tail curling up

    # Triangular head fused at the front, with a dark compound eye.
    filltri((3, 6), (9, 4), (9, 9))
    for ey in (5, 6):
        for ex in (5, 6):
            pt(ex, ey, EYEV)

    line(8, 4, 15, 0, ANTV)  # antennae swept up & back
    line(8, 4, 11, 0, ANTV)

    line(8, 9, 2, 5, BODYV, 2)  # raptorial femur (up-forward)
    line(3, 5, 7, 8, BODYV, 2)  # forearm folded back — the "praying" scythe

    for hx, kx, ky, fx, fy in ((12, 10, 13, 8, 17), (16, 16, 13, 19, 17), (20, 22, 13, 25, 17)):
        line(hx, 10, kx, ky)  # femur down to the knee
        line(kx, ky, fx, fy)  # tibia out to the foot

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


def print_banner(console: Any, model: str, backend: str) -> None:
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

        return Agent(
            model=self.model,
            provider=provider,
            system=self.system,
            tools=ToolRegistry(),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            max_steps=self.max_turns,
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

    # -- streaming a single turn --------------------------------------------

    async def _stream_turn(self, text: str) -> None:
        from .events import ContentBlockDelta, TextDelta  # noqa: PLC0415
        from .types import AssistantMessage, TextBlock, UserMessage  # noqa: PLC0415

        self.messages.append(UserMessage(content=text))

        # Assistant label, then live token stream.
        self.console.print()
        self.console.print(f"[{BODY}]●[/] ", end="")

        collected: list[str] = []
        try:
            async for ev in self.agent.stream(self.messages):
                if isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, TextDelta):
                    self.console.print(ev.delta.text, end="", markup=False, highlight=False)
                    collected.append(ev.delta.text)
        except KeyboardInterrupt:
            self.console.print("\n[ansibrightblack](interrupted)[/]")
            raise
        finally:
            self.console.print()

        self.messages.append(
            AssistantMessage(content=[TextBlock(text="".join(collected))])
        )

    # -- slash commands ------------------------------------------------------

    def _handle_slash(self, line: str) -> bool:
        """Return True if the line was a command (loop should continue)."""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/exit", "/quit"):
            raise EOFError
        if cmd == "/help":
            self.console.print(
                "\n[bold]commands[/]\n"
                "  [white]/help[/]            this message\n"
                "  [white]/model[/] <slug>    switch model for the next turns\n"
                "  [white]/clear[/]           clear conversation history\n"
                "  [white]/cwd[/]             show working directory\n"
                "  [white]/exit[/]            quit (also Ctrl+D)\n"
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
        if cmd == "/model":
            if arg:
                self.model = arg
                self.console.print(f"[ansibrightblack](model → {arg})[/]")
            else:
                self.console.print(f"[ansibrightblack]model: {self.model}[/]")
            return True
        # Unknown slash command — let it fall through as a normal prompt.
        return False

    # -- main loop -----------------------------------------------------------

    async def run(self) -> int:
        self._resolve_model()  # so the banner + first turn use a model that exists
        print_banner(self.console, self.model, self.backend)
        session = self._build_session()
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
                if line.startswith("/") and self._handle_slash(line):
                    continue

                try:
                    await self._stream_turn(line)
                except KeyboardInterrupt:
                    # Cancelled this reply: drop the dangling user turn (the
                    # assistant reply never landed) so history stays coherent.
                    from .types import UserMessage  # noqa: PLC0415

                    if self.messages and isinstance(self.messages[-1], UserMessage):
                        self.messages.pop()
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
                    # Drop the dangling user turn so a retry doesn't double it up.
                    from .types import UserMessage  # noqa: PLC0415

                    if self.messages and isinstance(self.messages[-1], UserMessage):
                        self.messages.pop()
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
