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

# Mascot palette — a green praying mantis.
BODY = "#8bc34a"  # mantis green
FACE_BG = "#2e5e16"  # darker green: the head's shaded interior
EYE_BG = "#13270a"  # near-black band the eyes sit on
ACCENT = "#aed581"  # antennae + the two big compound eyes

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
    """Build the praying-mantis pixel mascot as rich ``Text`` rows.

    Five rows, 9 columns, centered: antennae, a triangular head shaded with a
    background fill, two big compound eyes flanking a dark face band, the head
    narrowing to a chin, and the raptorial forelegs folded in the signature
    "praying" pose around a slim body.
    """
    body = f"{BODY}"
    head = f"{BODY} on {FACE_BG}"
    face = f"{EYE_BG} on {BODY}"

    r0 = Text("  ╲   ╱  ", style=ACCENT)

    r1 = Text()
    r1.append("  ▟", style=body)
    r1.append("███", style=head)
    r1.append("▙  ", style=body)

    r2 = Text()
    r2.append(" ◉", style=ACCENT)
    r2.append("█", style=body)
    r2.append("▀█▀", style=face)
    r2.append("█", style=body)
    r2.append("◉ ", style=ACCENT)

    r3 = Text()
    r3.append("  ▜", style=body)
    r3.append("███", style=head)
    r3.append("▛  ", style=body)

    r4 = Text("  ╱▐█▌╲  ", style=body)
    return [r0, r1, r2, r3, r4]


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

    # Pad the 3 info lines with blanks so they sit vertically centered against
    # the 5-row mascot.
    blank = Text("")
    info = [blank, title, sub, cwd, blank]

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
