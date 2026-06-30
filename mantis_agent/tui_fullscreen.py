"""Full-screen ``mantis``: the input is pinned to the bottom of the terminal and
stays visible at all times — including while the agent is working — with the
conversation scrolling above it (the Claude Code layout).

It uses prompt_toolkit's ``full_screen=False`` Application together with the
"print above a pinned prompt" pattern: the bottom region (rule + input + rule +
footer) is a tiny always-on app, and every piece of conversation output is
printed *above* it via ``run_in_terminal``. That means all of the existing rich
rendering in :mod:`mantis_agent.tui` (banner, markdown, diffs, tool calls) is
reused verbatim — we just print it above the live prompt.

If anything here fails (older prompt_toolkit, odd terminal), ``main()`` falls
back to the classic scrolling REPL, so ``mantis`` never ends up broken.
"""

from __future__ import annotations

import asyncio
import random
import shutil
import sys
import time
from typing import Any

from .tui import MODES, SPINNER_FRAMES, THINKING_WORDS, print_banner

# ANSI 256/standard colors (work in Terminal.app — no truecolor needed).
_GREEN = "\033[38;5;113m"
_DIM = "\033[38;5;240m"
_GREY = "\033[90m"
_RESET = "\033[0m"
_MODE_ANSI = {
    "ansibrightblack": "90", "ansigreen": "32", "ansicyan": "36", "ansired": "31",
}


async def run_fullscreen(tui: Any) -> int:
    from prompt_toolkit.application import Application, get_app  # noqa: PLC0415
    from prompt_toolkit.application.run_in_terminal import run_in_terminal  # noqa: PLC0415
    from prompt_toolkit.buffer import Buffer  # noqa: PLC0415
    from prompt_toolkit.formatted_text import ANSI  # noqa: PLC0415
    from prompt_toolkit.key_binding import KeyBindings  # noqa: PLC0415
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window  # noqa: PLC0415
    from prompt_toolkit.layout.controls import (  # noqa: PLC0415
        BufferControl,
        FormattedTextControl,
    )
    from rich.markup import escape as _esc  # noqa: PLC0415

    from .types import (  # noqa: PLC0415
        AssistantMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    # -- setup (mirror MantisTUI.run) ---------------------------------------
    tui._restore_last_model()
    tui._resolve_model()
    try:
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    print_banner(tui.console, tui.model, tui.backend)
    tui.agent = tui._build_agent()
    tui._kick_prewarm()

    state: dict[str, Any] = {
        "working": False, "started": 0.0, "word": "", "frame": 0, "task": None,
    }
    input_buffer = Buffer(multiline=False)

    def _width() -> int:
        return shutil.get_terminal_size((80, 24)).columns

    def rule_ft() -> Any:
        return ANSI(f"{_GREY}{'─' * _width()}{_RESET}")

    def prompt_ft() -> Any:
        return ANSI(f"{_GREEN}›{_RESET} ")

    def footer_ft() -> Any:
        if state["working"]:
            el = int(time.monotonic() - state["started"])
            frame = SPINNER_FRAMES[state["frame"] % len(SPINNER_FRAMES)]
            return ANSI(
                f"{_GREEN}{frame} {state['word']}…{_RESET} "
                f"{_DIM}({el}s · esc to interrupt){_RESET}"
            )
        label, symbol, color = MODES[tui.mode_idx]
        left = "" if tui.mode_idx == 0 else f"{symbol}{label} (shift+tab to cycle)"
        col = _MODE_ANSI.get(color, "90")
        return ANSI(f"\033[{col}m{left}{_RESET}   {_GREY}{tui.model}{_RESET}")

    async def _print(fn: Any) -> None:
        await run_in_terminal(fn)

    async def _handle(text: str) -> None:
        await _print(lambda: tui.console.print(f"\n[ansibrightblack]›[/] {_esc(text)}"))

        if text.startswith("/") and await _slash(text):
            get_app().invalidate()
            return

        base = len(tui.messages)
        tui.messages.append(UserMessage(content=text))
        state.update(working=True, started=time.monotonic(),
                     word=random.choice(THINKING_WORDS), task=asyncio.current_task())
        get_app().invalidate()
        try:
            async for msg in tui.agent.run_iter(tui.messages):
                if isinstance(msg, AssistantMessage):
                    await _print(lambda m=msg: tui._render_assistant(m, ToolUseBlock))
                elif isinstance(msg, UserMessage) and not getattr(msg, "isMeta", False):
                    await _print(lambda m=msg: tui._render_tool_results(m, ToolResultBlock))
        except asyncio.CancelledError:
            del tui.messages[base:]
            await _print(lambda: tui.console.print("[ansibrightblack](interrupted)[/]"))
        except Exception as e:  # noqa: BLE001
            del tui.messages[base:]
            await _print(lambda e=e: tui.console.print(f"[ansired]error:[/] {e}"))
        finally:
            state.update(working=False, task=None)
            get_app().invalidate()

    async def _slash(text: str) -> bool:
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("/exit", "/quit"):
            get_app().exit(result=0)
            return True
        if cmd == "/clear":
            tui.messages = []
            tui._todos_shown = []

            def _c() -> None:
                sys.stdout.write("\033[2J\033[3J\033[H")
                sys.stdout.flush()
                print_banner(tui.console, tui.model, tui.backend)
            await _print(_c)
            return True
        if cmd == "/cwd":
            from pathlib import Path  # noqa: PLC0415
            await _print(lambda: tui.console.print(f"[ansibrightblack]{Path.cwd()}[/]"))
            return True
        if cmd == "/model":
            if arg:
                tui.model = arg
                await _print(lambda: tui.console.print(f"[ansibrightblack](model → {arg})[/]"))
            else:
                await _print(lambda: tui.console.print(f"[ansibrightblack]model: {tui.model}[/]"))
            return True
        if cmd == "/help":
            await _print(lambda: tui.console.print(
                "\n[bold]commands[/]  [white]/model[/] <id> · [white]/clear[/] · "
                "[white]/cwd[/] · [white]/exit[/]\n"
                "[ansibrightblack]shift+tab cycles mode · esc/Ctrl+C interrupts a "
                "running reply · Ctrl+D quits[/]\n"))
            return True
        return False  # unknown → treat as a normal prompt

    kb = KeyBindings()

    @kb.add("enter")
    def _(event: Any) -> None:
        text = input_buffer.text.strip()
        input_buffer.reset()
        if text:
            event.app.create_background_task(_handle(text))

    @kb.add("c-d")
    def _(event: Any) -> None:
        if not state["working"]:
            event.app.exit(result=0)

    @kb.add("c-c")
    @kb.add("escape", eager=True)
    def _(event: Any) -> None:
        task = state.get("task")
        if state["working"] and task is not None:
            task.cancel()

    @kb.add("s-tab")
    def _(event: Any) -> None:
        tui.mode_idx = (tui.mode_idx + 1) % len(MODES)
        event.app.invalidate()

    input_window = Window(BufferControl(buffer=input_buffer), height=1, wrap_lines=False)
    layout = Layout(
        HSplit([
            Window(FormattedTextControl(rule_ft), height=1),
            VSplit([Window(FormattedTextControl(prompt_ft), width=2), input_window], height=1),
            Window(FormattedTextControl(rule_ft), height=1),
            Window(FormattedTextControl(footer_ft), height=1),
        ]),
        focused_element=input_window,
    )
    app = Application(layout=layout, key_bindings=kb, full_screen=False, erase_when_done=True)

    async def _animate() -> None:
        while True:
            if state["working"]:
                state["frame"] += 1
                app.invalidate()
            await asyncio.sleep(0.12)

    anim = asyncio.ensure_future(_animate())
    try:
        await app.run_async()
    finally:
        anim.cancel()
        if tui.agent is not None:
            await tui.agent.aclose()
    tui.console.print("[ansibrightblack]bye 👋[/]")
    return 0
