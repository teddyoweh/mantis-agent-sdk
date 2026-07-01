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

from .tui import MODES, SLASH_COMMANDS, SPINNER_FRAMES, THINKING_WORDS, print_banner

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
        "slash_sel": 0, "pending_perm": None,
    }

    # Interactive permission asker: render an in-pane Allow/Deny prompt and
    # bridge the (serial) tool-dispatch coroutine to a keypress via a Future.
    # The agent's permission layer calls this whenever a decision lands on Ask.
    async def _ask_permission(tool: Any, tool_input: dict, prompt_text: str) -> str:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        state["pending_perm"] = {"future": fut, "prompt": prompt_text, "sel": 0}
        get_app().invalidate()
        try:
            return await fut  # yields to the loop; the app keeps redrawing
        except asyncio.CancelledError:
            return "deny"
        finally:
            state["pending_perm"] = None
            get_app().invalidate()

    if tui.agent is not None and tui.agent.permissions is not None:
        tui.agent.permissions.asker = _ask_permission

    input_buffer = Buffer(multiline=False)

    # Reset the slash-menu selection whenever the line stops being a slash cmd.
    def _on_text(_buf: Any) -> None:
        if not input_buffer.text.startswith("/"):
            state["slash_sel"] = 0
    input_buffer.on_text_changed += _on_text

    # -- live menu: slash commands OR a model picker (a real layout window) ---

    def _chat_models() -> list[str]:
        from .tui import _is_chat_model  # noqa: PLC0415
        avail, _ = tui._available_models()
        chat = [m for m in (avail or []) if _is_chat_model(m)]
        return chat or list(avail or [])  # if filtering nukes everything, show all

    def _menu_options() -> list[tuple[str, str, str]]:
        """Returns ``(kind, value, meta)`` rows. kind is 'cmd' or 'model'."""
        t = input_buffer.text
        if not t.startswith("/"):
            return []
        # Model picker: '/models' or '/model <partial>' → selectable chat models.
        if t == "/models" or t.startswith("/model "):
            partial = t[7:].strip().lower() if t.startswith("/model ") else ""
            models = [m for m in _chat_models() if partial in m.lower()]
            return [("model", m, "← current" if m == tui.model else "") for m in models[:8]]
        # Command menu (still typing the command name).
        if " " not in t:
            return [("cmd", c, d) for c, d in SLASH_COMMANDS.items() if c.startswith(t)]
        return []

    def menu_ft() -> Any:
        opts = _menu_options()
        if not opts:
            return ANSI("")
        sel = state["slash_sel"] % len(opts)
        rows = []
        for i, (_kind, value, meta) in enumerate(opts[:8]):
            metatxt = f"  {_DIM}{meta}{_RESET}" if meta else ""
            if i == sel:
                rows.append(f"\033[30;48;5;113m {value} \033[0m{metatxt}")
            else:
                rows.append(f"  {_GREEN}{value}{_RESET}{metatxt}")
        return ANSI("\n".join(rows))

    def _menu_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        n = min(len(_menu_options()), 8)
        return Dimension.exact(n) if n else Dimension.exact(0)

    def _switch_model(model_id: str) -> None:
        tui.model = model_id
        tui.agent = tui._build_agent()
        if tui.agent is not None and tui.agent.permissions is not None:
            tui.agent.permissions.asker = _ask_permission
        try:
            from . import catalog  # noqa: PLC0415
            catalog.set_last_model(model_id)
        except Exception:  # noqa: BLE001
            pass

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

    _PERM_OPTS = ["allow once", "allow for session", "deny"]
    _PERM_OPTS_VALUES = ["allow_once", "allow_session", "deny"]

    def perm_ft() -> Any:
        p = state.get("pending_perm")
        if not p:
            return ANSI("")
        sel = p["sel"] % 3
        head = f"{_GREEN}Allow?{_RESET} {_DIM}{p['prompt']}{_RESET}"
        row = "   ".join(
            (f"\033[30;48;5;113m {i + 1} {o} \033[0m" if i == sel
             else f"{_DIM}{i + 1} {o}{_RESET}")
            for i, o in enumerate(_PERM_OPTS)
        )
        return ANSI(head + "\n" + row + f"   {_GREY}(y/s/n · enter){_RESET}")

    def _perm_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        return Dimension.exact(2) if state.get("pending_perm") else Dimension.exact(0)

    async def _print(fn: Any) -> None:
        await run_in_terminal(fn)

    # Spacing model: every block prints a TRAILING blank line (so the separation
    # is part of the block's own run_in_terminal call and can't be dropped),
    # EXCEPT a tool call — which prints none, so its result hugs it directly.

    def _echo(t: str) -> None:
        tui.console.print(f"[ansibrightblack]›[/] {_esc(t)}")
        tui.console.print()

    def _assist(m: Any) -> None:
        had_tool_call = tui._render_assistant(m, ToolUseBlock)
        if not had_tool_call:  # text block → blank below; tool call → hug result
            tui.console.print()

    def _result(m: Any) -> None:
        tui._render_tool_results(m, ToolResultBlock)
        tui.console.print()

    async def _handle(text: str) -> None:
        await _print(lambda: _echo(text))

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
                    await _print(lambda m=msg: _assist(m))
                elif isinstance(msg, UserMessage) and not getattr(msg, "isMeta", False):
                    await _print(lambda m=msg: _result(m))
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
        if cmd in ("/model", "/models"):
            available, reachable = tui._available_models()
            if arg:
                # Pick by list number or by id.
                if arg.isdigit() and available and 1 <= int(arg) <= len(available):
                    picked = available[int(arg) - 1]
                else:
                    picked = arg
                tui.model = picked
                # REBUILD the agent so the new model actually takes effect — just
                # setting tui.model did nothing before (the live agent kept the
                # old model). Persist it as the last-used model too.
                tui.agent = tui._build_agent()
                try:
                    from . import catalog  # noqa: PLC0415
                    catalog.set_last_model(picked)
                except Exception:  # noqa: BLE001
                    pass

                def _ok(p: str = picked) -> None:
                    tui.console.print(f"[ansibrightblack](model → [white]{p}[/])[/]")
                await _print(_ok)
            else:
                def _list() -> None:
                    tui.console.print(f"\n[bold]Models[/] [ansibrightblack]· {tui.backend}[/]")
                    if not available:
                        tui.console.print(
                            "  [ansibrightblack](none found — switch with[/] "
                            "[white]/model <id>[/][ansibrightblack])[/]")
                    for i, m in enumerate(available[:30], 1):
                        mark = "  [white]← current[/]" if m == tui.model else ""
                        tui.console.print(f"  [white]{i:2}[/] {m}{mark}")
                    tui.console.print(
                        "[ansibrightblack]→ [white]/model <number>[/] or "
                        "[white]/model <id>[/][/]\n")
                await _print(_list)
            return True
        if cmd == "/help":
            await _print(lambda: tui.console.print(
                "\n[bold]commands[/]  [white]/model[/] <id> · [white]/clear[/] · "
                "[white]/cwd[/] · [white]/exit[/]\n"
                "[ansibrightblack]shift+tab cycles mode · esc/Ctrl+C interrupts a "
                "running reply (Ctrl+C also quits when idle) · Ctrl+D quits[/]\n"))
            return True
        return False  # unknown → treat as a normal prompt

    kb = KeyBindings()

    from prompt_toolkit.filters import Condition  # noqa: PLC0415

    _menu_open = Condition(lambda: bool(_menu_options()))
    _perm_open = Condition(lambda: state.get("pending_perm") is not None)

    async def _announce(msg: str) -> None:
        await _print(lambda: tui.console.print(f"[ansibrightblack]{msg}[/]"))
        get_app().invalidate()

    def _accept_menu(event: Any) -> bool:
        """Act on the highlighted menu row. Returns True if it consumed the key
        (so the caller shouldn't also submit). Models switch immediately;
        commands fill into the line (one more Enter submits)."""
        opts = _menu_options()
        if not opts:
            return False
        kind, value, _meta = opts[state["slash_sel"] % len(opts)]
        if kind == "model":
            _switch_model(value)
            input_buffer.reset()
            state["slash_sel"] = 0
            event.app.create_background_task(_announce(f"model → {value}"))
            return True
        # command: fill it (unless already exact)
        if input_buffer.text != value:
            input_buffer.text = value + " "
            input_buffer.cursor_position = len(input_buffer.text)
            state["slash_sel"] = 0
            event.app.invalidate()
            return True
        return False

    def _resolve_perm(choice: str) -> None:
        p = state.get("pending_perm")
        if p and not p["future"].done():
            p["future"].set_result(choice)

    @kb.add("1", filter=_perm_open)
    @kb.add("y", filter=_perm_open)
    def _(event: Any) -> None:
        _resolve_perm("allow_once")

    @kb.add("2", filter=_perm_open)
    @kb.add("s", filter=_perm_open)
    def _(event: Any) -> None:
        _resolve_perm("allow_session")

    @kb.add("3", filter=_perm_open)
    @kb.add("n", filter=_perm_open)
    @kb.add("d", filter=_perm_open)
    def _(event: Any) -> None:
        _resolve_perm("deny")

    @kb.add("up", filter=_perm_open)
    def _(event: Any) -> None:
        state["pending_perm"]["sel"] -= 1
        event.app.invalidate()

    @kb.add("down", filter=_perm_open)
    def _(event: Any) -> None:
        state["pending_perm"]["sel"] += 1
        event.app.invalidate()

    @kb.add("down", filter=_menu_open)
    def _(event: Any) -> None:
        state["slash_sel"] += 1
        event.app.invalidate()

    @kb.add("up", filter=_menu_open)
    def _(event: Any) -> None:
        state["slash_sel"] -= 1
        event.app.invalidate()

    @kb.add("tab", filter=_menu_open)
    def _(event: Any) -> None:
        _accept_menu(event)

    @kb.add("enter")
    def _(event: Any) -> None:
        # A permission prompt steals Enter: submit the highlighted choice.
        if state.get("pending_perm") is not None:
            _resolve_perm(_PERM_OPTS_VALUES[state["pending_perm"]["sel"] % 3])
            return
        # Menu open → act on the highlighted row (switch model / fill command).
        if _accept_menu(event):
            return
        text = input_buffer.text.strip()
        input_buffer.reset()
        if text:
            event.app.create_background_task(_handle(text))

    @kb.add("c-c")
    def _(event: Any) -> None:
        # Interrupt a running reply; if idle, quit (the usual terminal Ctrl+C).
        task = state.get("task")
        if state["working"] and task is not None:
            task.cancel()
        else:
            event.app.exit(result=0)

    @kb.add("c-d")
    def _(event: Any) -> None:
        if not state["working"]:
            event.app.exit(result=0)

    @kb.add("escape", eager=True)
    def _(event: Any) -> None:
        # Esc during a permission prompt = deny (don't run the tool).
        if state.get("pending_perm") is not None:
            _resolve_perm("deny")
            return
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
            # The slash-command menu lives here — its height collapses to 0 when
            # the line isn't a slash command, so the footer normally hugs the rule.
            Window(FormattedTextControl(menu_ft), height=_menu_height),
            # Interactive permission prompt — height 0 unless an Ask is pending.
            Window(FormattedTextControl(perm_ft), height=_perm_height),
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
