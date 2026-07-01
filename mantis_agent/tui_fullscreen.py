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

_MENTION_IGNORE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".mypy_cache",
    ".pytest_cache", "build", ".ruff_cache", ".tox", ".idea", ".egg-info",
}


def find_file_mentions(partial: str, root: str, *, limit: int = 8) -> list[str]:
    """Files under ``root`` matching ``partial`` (substring, case-insensitive),
    ranked basename-prefix-first then shortest path. Bounded (skips VCS/build
    dirs and dotfiles, caps the scan) so it stays snappy per keystroke on big
    repos. Powers the ``@``-file-mention completer."""
    import os  # noqa: PLC0415

    pl = partial.lower()
    hits: list[str] = []
    scanned = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _MENTION_IGNORE and not d.startswith(".")]
        for f in fns:
            if f.startswith("."):
                continue
            scanned += 1
            rel = os.path.relpath(os.path.join(dp, f), root)
            if not pl or pl in rel.lower():
                hits.append(rel)
            if scanned > 6000 or len(hits) > 400:
                break
        if scanned > 6000 or len(hits) > 400:
            break

    def _key(rel: str) -> tuple:
        base = os.path.basename(rel).lower()
        return (0 if base.startswith(pl) else (1 if pl in base else 2), len(rel))

    hits.sort(key=_key)
    return hits[:limit]


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
        "slash_sel": 0, "pending_perm": None, "picking_model": None,
        "pending_question": None,
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

    # Interactive AskUserQuestion picker: same Future-bridge pattern. Loops over
    # the agent's questions, one in-pane picker at a time.
    async def _ask_questions(questions: list[dict]) -> list[dict]:
        results: list[dict] = []
        for q in questions:
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            state["pending_question"] = {"q": q, "sel": 0, "selected": set(),
                                         "typing": False, "future": fut}
            get_app().invalidate()
            try:
                answers = await fut
            except asyncio.CancelledError:
                answers = []
            finally:
                state["pending_question"] = None
                get_app().invalidate()
            results.append({"question": q["question"], "header": q["header"],
                            "answers": answers})
        return results

    tui._fs_ask = _ask_questions

    input_buffer = Buffer(multiline=False)

    # Reset the slash-menu selection whenever the line stops being a slash cmd.
    def _on_text(_buf: Any) -> None:
        if not input_buffer.text.startswith("/"):
            state["slash_sel"] = 0
    input_buffer.on_text_changed += _on_text

    # -- live menu: slash commands OR a model picker (a real layout window) ---

    def _chat_models() -> list[str]:
        # Cache the backend's model list — _available_models() is a *synchronous*
        # HTTP probe, and this is called on every render/keystroke while the
        # picker is open, so without a cache it froze the event loop each frame.
        # Cache is keyed by backend so switching providers refetches.
        cache = state.get("model_cache")
        if cache is not None and cache.get("backend") == tui.backend:
            return cache["models"]
        from .tui import _is_chat_model  # noqa: PLC0415
        avail, _ = tui._available_models()
        chat = [m for m in (avail or []) if _is_chat_model(m)]
        models = chat or list(avail or [])  # if filtering nukes everything, show all
        state["model_cache"] = {"backend": tui.backend, "models": models}
        return models

    def _file_matches(partial: str) -> list[str]:
        import os  # noqa: PLC0415
        return find_file_mentions(partial, os.getcwd())

    def _menu_options() -> list[tuple[str, str, str]]:
        """Type-ahead menu rows for the in-progress line: ``@``-file-mentions
        anywhere in the line, or slash commands at the start. The model picker
        is a separate state-driven overlay (see picker_ft)."""
        t = input_buffer.text
        # @-file-mention: the last whitespace-delimited token starts with @.
        word = t.rsplit(" ", 1)[-1] if t else ""
        if word.startswith("@"):
            return [("file", p, "") for p in _file_matches(word[1:])]
        if not t.startswith("/") or " " in t:
            return []
        return [("cmd", c, d) for c, d in SLASH_COMMANDS.items() if c.startswith(t)]

    # -- model picker overlay (state-driven, like the permission prompt) ------

    def _open_model_picker() -> None:
        models = _chat_models()
        cur = models.index(tui.model) if tui.model in models else 0
        state["picking_model"] = {"models": models, "sel": cur}
        get_app().invalidate()

    def picker_ft() -> Any:
        p = state.get("picking_model")
        if not p:
            return ANSI("")
        models, sel = p["models"], p["sel"] % max(1, len(p["models"]))
        # Window of up to 8 rows centered on the selection.
        lo = max(0, min(sel - 3, len(models) - 8))
        rows = [f"{_GREEN}Pick a model{_RESET}  {_GREY}(↑/↓ · enter · esc){_RESET}"]
        for i in range(lo, min(lo + 7, len(models))):
            m = models[i]
            mark = "  ← current" if m == tui.model else ""
            if i == sel:
                rows.append(f"\033[30;48;5;113m {m} \033[0m{_DIM}{mark}{_RESET}")
            else:
                rows.append(f"  {m}{_DIM}{mark}{_RESET}")
        return ANSI("\n".join(rows))

    def _picker_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        p = state.get("picking_model")
        return Dimension.exact(1 + min(len(p["models"]), 7)) if p else Dimension.exact(0)

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

    def question_ft() -> Any:
        p = state.get("pending_question")
        if not p:
            return ANSI("")
        q = p["q"]
        opts = q["options"]
        multi = bool(q.get("multiSelect"))
        sel, selset = p["sel"], p["selected"]
        rows = [f"{_GREEN}?{_RESET} {q['question']}  {_DIM}[{q['header']}]{_RESET}"]
        for i, o in enumerate(opts):
            box = (("● " if i in selset else "○ ") if multi else "")
            line = f" {i + 1} {box}{o['label']}  {_DIM}{o['description']}{_RESET}"
            rows.append(f"\033[30;48;5;113m{line} \033[0m" if i == sel else f" {line}")
        other = " o  Other…"
        rows.append(f"\033[30;48;5;113m{other} \033[0m" if sel == len(opts) else f" {other}")
        if p["typing"]:
            rows.append(f"{_GREY}type your answer in the input line, then Enter{_RESET}")
        else:
            hint = "space toggles · enter confirms" if multi else "number or o · enter"
            rows.append(f"{_GREY}({hint}){_RESET}")
        return ANSI("\n".join(rows))

    def _question_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        p = state.get("pending_question")
        if not p:
            return Dimension.exact(0)
        return Dimension.exact(len(p["q"]["options"]) + 3)  # question + opts + Other + hint

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
            if arg:
                # Direct switch by id (or number into the chat-model list).
                models = _chat_models()
                if arg.isdigit() and models and 1 <= int(arg) <= len(models):
                    picked = models[int(arg) - 1]
                else:
                    picked = arg
                _switch_model(picked)
                await _print(lambda p=picked: tui.console.print(
                    f"[ansibrightblack](model → [white]{p}[/])[/]"))
            else:
                # No arg → open the interactive picker overlay (↑/↓ · enter · esc).
                _open_model_picker()
            return True
        if cmd == "/help":
            await _print(lambda: tui.console.print(
                "\n[bold]commands[/]  [white]/model[/] <id> · [white]/clear[/] · "
                "[white]/cwd[/] · [white]/exit[/]\n"
                "[ansibrightblack]@file to attach a path · shift+tab cycles mode · esc/Ctrl+C interrupts a "
                "running reply (Ctrl+C also quits when idle) · Ctrl+D quits[/]\n"))
            return True
        return False  # unknown → treat as a normal prompt

    kb = KeyBindings()

    from prompt_toolkit.filters import Condition  # noqa: PLC0415

    _menu_open = Condition(lambda: bool(_menu_options()))
    _perm_open = Condition(lambda: state.get("pending_perm") is not None)
    _picker_open = Condition(lambda: state.get("picking_model") is not None)
    _q_open = Condition(
        lambda: state.get("pending_question") is not None
        and not state["pending_question"]["typing"]
    )

    def _q_resolve(answers: list) -> None:
        p = state.get("pending_question")
        if p and not p["future"].done():
            p["future"].set_result(answers)

    def _q_nrows() -> int:
        p = state["pending_question"]
        return len(p["q"]["options"]) + 1  # options + Other

    @kb.add("up", filter=_q_open)
    def _(event: Any) -> None:
        p = state["pending_question"]
        p["sel"] = (p["sel"] - 1) % _q_nrows()
        event.app.invalidate()

    @kb.add("down", filter=_q_open)
    def _(event: Any) -> None:
        p = state["pending_question"]
        p["sel"] = (p["sel"] + 1) % _q_nrows()
        event.app.invalidate()

    @kb.add("space", filter=_q_open)
    def _(event: Any) -> None:
        p = state["pending_question"]
        if p["q"].get("multiSelect") and p["sel"] < len(p["q"]["options"]):
            p["selected"].symmetric_difference_update({p["sel"]})
            event.app.invalidate()

    for _digit in "1234":
        @kb.add(_digit, filter=_q_open)
        def _(event: Any, d: str = _digit) -> None:
            p = state["pending_question"]
            opts = p["q"]["options"]
            i = int(d) - 1
            if i >= len(opts):
                return
            if p["q"].get("multiSelect"):
                p["selected"].symmetric_difference_update({i})
                event.app.invalidate()
            else:
                _q_resolve([opts[i]["label"]])

    @kb.add("o", filter=_q_open)
    def _(event: Any) -> None:
        state["pending_question"]["typing"] = True
        event.app.invalidate()

    @kb.add("up", filter=_picker_open)

    @kb.add("up", filter=_picker_open)
    def _(event: Any) -> None:
        state["picking_model"]["sel"] -= 1
        event.app.invalidate()

    @kb.add("down", filter=_picker_open)
    def _(event: Any) -> None:
        state["picking_model"]["sel"] += 1
        event.app.invalidate()

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
        if kind == "file":
            # Replace the trailing @<partial> token with the chosen path.
            t = input_buffer.text
            idx = t.rfind("@")
            new = (t[:idx] if idx >= 0 else t) + value + " "
            input_buffer.text = new
            input_buffer.cursor_position = len(new)
            state["slash_sel"] = 0
            event.app.invalidate()
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
        # An AskUserQuestion picker steals Enter.
        pq = state.get("pending_question")
        if pq is not None:
            q, opts = pq["q"], pq["q"]["options"]
            if pq["typing"]:
                ans = input_buffer.text.strip()
                input_buffer.reset()
                _q_resolve([ans] if ans else [])
            elif pq["sel"] == len(opts):  # "Other" row
                pq["typing"] = True
                event.app.invalidate()
            elif q.get("multiSelect"):
                picks = [opts[i]["label"] for i in sorted(pq["selected"])]
                if not picks and pq["sel"] < len(opts):
                    picks = [opts[pq["sel"]]["label"]]
                _q_resolve(picks)
            else:
                _q_resolve([opts[pq["sel"]]["label"]])
            return
        # Model picker open → switch to the highlighted model and close it.
        p = state.get("picking_model")
        if p is not None:
            models = p["models"]
            if models:
                chosen = models[p["sel"] % len(models)]
                _switch_model(chosen)
                event.app.create_background_task(_announce(f"model → {chosen}"))
            state["picking_model"] = None
            event.app.invalidate()
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
        # Esc closes the model picker without switching.
        if state.get("picking_model") is not None:
            state["picking_model"] = None
            event.app.invalidate()
            return
        # Esc during a permission prompt = deny (don't run the tool).
        if state.get("pending_perm") is not None:
            _resolve_perm("deny")
            return
        # Esc during a question: cancel typing, else skip the question.
        pq = state.get("pending_question")
        if pq is not None:
            if pq["typing"]:
                pq["typing"] = False
                input_buffer.reset()
                event.app.invalidate()
            else:
                _q_resolve([])
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
            # AskUserQuestion picker — height 0 unless a question is pending.
            Window(FormattedTextControl(question_ft), height=_question_height),
            # Model picker overlay — height 0 unless /models opened it.
            Window(FormattedTextControl(picker_ft), height=_picker_height),
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
