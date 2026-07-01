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
import re
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


def context_breakdown(messages: list, system_text: str = "") -> dict:
    """Estimated token composition of the current context: system prompt,
    memory/env context head (isMeta messages), and conversation. Powers
    ``/context``. Uses the same coarse estimator as compaction."""
    from .compact import _message_token_estimate  # noqa: PLC0415
    from .types import UserMessage  # noqa: PLC0415

    head = convo = 0
    for m in messages:
        est = _message_token_estimate(m)
        if isinstance(m, UserMessage) and getattr(m, "isMeta", False):
            head += est
        else:
            convo += est
    system = len(system_text or "") // 4
    return {"system": system, "context": head, "conversation": convo,
            "total": system + head + convo}


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
    from prompt_toolkit.layout.processors import (  # noqa: PLC0415
        ConditionalProcessor,
        PasswordProcessor,
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
        "awaiting_key": None,
        "pending_question": None, "ctx_tokens": 0, "session_cost": 0.0,
    }

    def _ctx_window() -> int:
        cap = getattr(tui.agent, "model_capability", None) if tui.agent else None
        return getattr(cap, "context_window", 0) or 0

    def _ctx_status() -> str:
        """The footer 'context fill · session cost' indicator, e.g.
        '12k/32k 38% · $0.03'. Empty until we've seen a turn's usage."""
        from .tui import format_ctx_status  # noqa: PLC0415
        return format_ctx_status(
            state.get("ctx_tokens", 0), _ctx_window(), state.get("session_cost", 0.0))

    def _show_context() -> None:
        """Render the /context breakdown: window fill + estimated composition."""
        used = state.get("ctx_tokens", 0)
        win = _ctx_window()
        bd = context_breakdown(tui.messages, (tui.agent.system if tui.agent else "") or "")
        sys_tok, head_tok, convo_tok = bd["system"], bd["context"], bd["conversation"]

        def _fmt(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        tui.console.print(f"\n[bold]Context[/]  [ansibrightblack]{tui.model}[/]")
        if win:
            pct = min(100, round(used / win * 100)) if used else 0
            filled = round(pct / 5)
            bar = "█" * filled + "░" * (20 - filled)
            colr = "red" if pct >= 90 else ("yellow" if pct >= 75 else "green")
            tui.console.print(
                f"  [{colr}]{bar}[/]  {_fmt(used)} / {_fmt(win)} tokens  "
                f"[{colr}]{pct}%[/]  [ansibrightblack]({_fmt(win - used)} free)[/]")
        elif used:
            tui.console.print(f"  {_fmt(used)} tokens used (context window unknown)")
        else:
            tui.console.print("  [ansibrightblack](no turn yet — send a message first)[/]")
        tui.console.print(
            f"  [ansibrightblack]estimated split:[/] system {_fmt(sys_tok)} · "
            f"context/memory {_fmt(head_tok)} · conversation {_fmt(convo_tok)}")
        from .tui import format_cost  # noqa: PLC0415
        tui.console.print(
            f"  [ansibrightblack]session cost:[/] {format_cost(state.get('session_cost', 0.0))}\n")

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

    # Plan-mode approval handoff: render the plan, ask approve/keep-planning via
    # the same picker, and lift plan mode on approval.
    async def _fs_plan(plan: str) -> str:
        def _show() -> None:
            from .tui import _compact_markdown  # noqa: PLC0415
            tui.console.print("\n[bold #cddc39]▶ Plan[/]")
            try:
                tui.console.print(_compact_markdown(plan))
            except Exception:  # noqa: BLE001
                tui.console.print(plan)
            tui.console.print()
        await _print(_show)
        answers = await _ask_questions([{
            "question": "Proceed with this plan?",
            "header": "Plan",
            "options": [
                {"label": "Yes, proceed", "description": "Approve and start making changes"},
                {"label": "Keep planning", "description": "Refine the plan first"},
            ],
            "multiSelect": False,
        }])
        picked = answers[0]["answers"][0] if answers and answers[0]["answers"] else ""
        if picked.lower().startswith("yes"):
            tui.mode_idx = 0  # lift plan mode → default
            get_app().invalidate()
            return "Plan approved. Plan mode is now OFF — proceed with the implementation."
        note = f" ({picked})" if picked else ""
        return f"The user did not approve the plan yet{note}. Stay in plan mode and revise it."

    tui._fs_plan = _fs_plan

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

    _PICKER_ROWS = 11  # visible model/header rows in the picker window

    def _build_picker_items(flt: str) -> list[dict]:
        # GROUPED items: active backend first, then other ENABLED providers, then
        # DISABLED (dimmed + 🔒). ``flt`` is a case-insensitive substring filter
        # on the model id — groups with no surviving models are dropped.
        from .tui import _is_chat_model  # noqa: PLC0415
        from . import catalog  # noqa: PLC0415

        from .capabilities import lookup_model  # noqa: PLC0415

        fl = flt.lower().strip()

        def keep(m: str) -> bool:
            return not fl or fl in m.lower()

        def ctxlab(m: str) -> str:
            cw = lookup_model(m).context_window
            return f"{cw // 1000}k" if cw >= 1000 else ""

        items: list[dict] = []
        active_all = _chat_models()
        active_set = set(active_all)
        active = [m for m in active_all if keep(m)]
        if active:
            back = (tui.backend or "").rstrip("/")
            if "localhost" in back or "127.0.0.1" in back:
                where = "Ollama (local)"
            else:
                # Show the friendly provider name when the backend is a known one.
                where = next((p.label for p in catalog.CATALOG
                              if p.base_url.rstrip("/") == back), back or "backend")
            items.append({"kind": "header", "label": f"● active · {where}"})
            for m in active:
                items.append({"kind": "model", "model": m, "enabled": True,
                              "provider_id": None, "ctx": ctxlab(m)})
        try:
            for g in catalog.grouped_provider_models():
                models = [m for m in g["models"] if _is_chat_model(m) and m not in active_set and keep(m)]
                if not models:
                    continue
                tag = "" if g["enabled"] else "  · not enabled"
                items.append({"kind": "header", "label": f"{g['label']}{tag}"})
                for m in models:
                    items.append({"kind": "model", "model": m, "enabled": g["enabled"],
                                  "provider_id": g["provider_id"], "ctx": ctxlab(m)})
        except Exception:  # noqa: BLE001
            pass
        return items

    def _open_model_picker(flt: str = "") -> None:
        items = _build_picker_items(flt)
        sel = next((i for i, it in enumerate(items)
                    if it["kind"] == "model" and it["model"] == tui.model), None)
        if sel is None:
            sel = next((i for i, it in enumerate(items) if it["kind"] == "model"), 0)
        state["picking_model"] = {"items": items, "sel": sel, "filter": flt}
        get_app().invalidate()

    def _refilter_picker() -> None:
        p = state.get("picking_model")
        if not p:
            return
        items = _build_picker_items(p.get("filter", ""))
        p["items"] = items
        if not (0 <= p["sel"] < len(items) and items[p["sel"]]["kind"] == "model"):
            p["sel"] = next((i for i, it in enumerate(items) if it["kind"] == "model"), 0)

    def picker_ft() -> Any:
        p = state.get("picking_model")
        if not p:
            return ANSI("")
        items, sel = p["items"], p["sel"]
        n = len(items)
        lo = max(0, min(sel - _PICKER_ROWS // 2, n - _PICKER_ROWS))
        flt = p.get("filter", "")
        _hdr = f"{_GREEN}Pick a model{_RESET}  {_GREY}(↑/↓ · type to filter · enter · esc){_RESET}"
        if flt:
            _hdr += f"   {_DIM}filter: {flt}{_RESET}"

        def _ctx(it: dict) -> str:
            c = it.get("ctx")  # precomputed in _build_picker_items
            return f"  {c}" if c else ""

        rows = [_hdr]
        for i in range(lo, min(lo + _PICKER_ROWS, n)):
            it = items[i]
            if it["kind"] == "header":
                rows.append(f"{_DIM}\033[1m{it['label']}\033[0m{_RESET}")
                continue
            m = it["model"]
            cur = "  ← current" if m == tui.model else ""
            if not it["enabled"]:
                if i == sel:
                    rows.append(f"\033[30;48;5;179m {m} 🔒 \033[0m{_DIM}{_ctx(it)} · enter to enable{_RESET}")
                else:
                    rows.append(f"  {_DIM}{m} 🔒{_ctx(it)}{_RESET}")
            elif i == sel:
                rows.append(f"\033[30;48;5;113m {m} \033[0m{_DIM}{cur}{_ctx(it)}{_RESET}")
            else:
                rows.append(f"  {m}{_DIM}{cur}{_ctx(it)}{_RESET}")
        return ANSI("\n".join(rows))

    def _picker_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        p = state.get("picking_model")
        if not p:
            return Dimension.exact(0)
        return Dimension.exact(1 + min(len(p["items"]), _PICKER_ROWS))

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

    def _switch_model(model_id: str, *, provider_id: str | None = None) -> None:
        from . import catalog  # noqa: PLC0415

        # Cross-provider switch: if this model belongs to a *different* enabled
        # provider (e.g. a Claude model while on OpenAI), re-wire the backend +
        # key too — otherwise the new model would be sent to the old endpoint
        # and 404/auth-fail. Falls back to the current backend for local ids.
        # When the picker knows the exact provider (``provider_id``), trust it —
        # ids like ``openai/gpt-oss-120b`` live under several providers and the
        # prefix heuristic can't tell which group the user actually picked.
        prov = catalog.BY_ID.get(provider_id) if provider_id else catalog.provider_for_model(model_id)
        if prov is not None:
            key = catalog.api_key_for(prov)
            if key:
                tui.backend, tui.api_key = prov.base_url, key
            elif prov.id == "anthropic":
                import os  # noqa: PLC0415
                if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
                    # OAuth/gateway Bearer token — api_key_for returns None; switch
                    # with no api_key so the passthrough authenticates via env Bearer.
                    # Preserve an existing Anthropic backend (e.g. a Bedrock/Vertex
                    # gateway); only point at the default API from elsewhere.
                    from .providers.base import detect_provider  # noqa: PLC0415
                    if detect_provider(tui.backend or "") != "anthropic_passthrough":
                        tui.backend = prov.base_url
                    tui.api_key = None
        tui.model = model_id
        tui.agent = tui._build_agent()
        if tui.agent is not None and tui.agent.permissions is not None:
            tui.agent.permissions.asker = _ask_permission
        try:
            catalog.set_last_model(model_id, tui.backend)
        except Exception:  # noqa: BLE001
            pass

    async def _apply_key(pid: str, model: str, key: str) -> None:
        """Enable a provider inline (from the picker): save the pasted key,
        validate it, and switch to the chosen model — or report why it failed."""
        from . import catalog  # noqa: PLC0415

        prov = catalog.BY_ID.get(pid)
        if prov is None or not key:
            return
        await _announce(f"validating {prov.label} key…")
        catalog.set_key(pid, key)
        try:
            ok, detail = await asyncio.to_thread(catalog.validate_provider, prov)
        except Exception:  # noqa: BLE001
            ok, detail = False, "validation error"
        if not ok:
            catalog.clear_key(pid)
            await _announce(f"✗ {prov.label}: {detail} (key not saved)")
            return
        _switch_model(model, provider_id=pid)
        # Fetch the provider's real /v1/models now (off-thread) so the next
        # /models shows its full catalog, not just the flagship starter list.
        try:
            await asyncio.to_thread(catalog.refresh_live_models, prov)
            state.pop("model_cache", None)  # invalidate the active-backend cache
        except Exception:  # noqa: BLE001
            pass
        await _announce(f"✓ {prov.label} enabled · model → {model}")

    def _width() -> int:
        return shutil.get_terminal_size((80, 24)).columns

    def rule_ft() -> Any:
        return ANSI(f"{_GREY}{'─' * _width()}{_RESET}")

    def prompt_ft() -> Any:
        if state.get("awaiting_key") is not None:
            return ANSI(f"{_GREY}🔑{_RESET} ")  # inline key-entry mode
        return ANSI(f"{_GREEN}❯{_RESET} ")

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
        ctx = _ctx_status()
        ctx_seg = f"   {ctx}" if ctx else ""
        return ANSI(f"\033[{col}m{left}{_RESET}   {_GREY}{tui.model}{_RESET}{ctx_seg}")

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
        tui.console.print(f"[ansibrightblack]❯[/] {_esc(t)}")
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

        from .tui import expand_slash_prompt  # noqa: PLC0415
        expanded = expand_slash_prompt(text)
        if expanded is not None:
            text = expanded  # e.g. /init → canned prompt; runs as a normal turn
        elif text.startswith("/") and await _slash(text):
            get_app().invalidate()
            return

        base = len(tui.messages)
        tui.messages.append(UserMessage(content=text))
        # @-file-mentions → inject the referenced files' contents so the model
        # has them inline (no extra read_file round-trip). isMeta, so the visible
        # transcript keeps the user's clean message.
        try:
            import os  # noqa: PLC0415

            from .tui import render_mention_block, resolve_file_mentions  # noqa: PLC0415
            _mentions = resolve_file_mentions(text, os.getcwd())
            if _mentions:
                tui.messages.append(
                    UserMessage(content=render_mention_block(_mentions), isMeta=True))
        except Exception:  # noqa: BLE001 — mention expansion is best-effort
            pass
        state.update(working=True, started=time.monotonic(),
                     word=random.choice(THINKING_WORDS), task=asyncio.current_task())
        get_app().invalidate()
        try:
            async for msg in tui.agent.run_iter(tui.messages):
                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "usage", None) is not None:
                        # input+output of the latest turn ≈ current context fill.
                        state["ctx_tokens"] = (msg.usage.input_tokens or 0) + (msg.usage.output_tokens or 0)
                        # Accumulate USD across turns (each call re-bills the full
                        # prompt, so cost is summed per turn, not from token totals).
                        from .budget import estimate_cost  # noqa: PLC0415
                        c = estimate_cost(msg.usage, tui.model,
                                          getattr(tui.agent, "_provider_hint", None))
                        if c:
                            state["session_cost"] += c
                    await _print(lambda m=msg: _assist(m))
                elif isinstance(msg, UserMessage) and not getattr(msg, "isMeta", False):
                    await _print(lambda m=msg: _result(m))
        except asyncio.CancelledError:
            # Keep the work done so far; just close any tool_use left unanswered
            # by the interrupt so the next turn's request stays well-formed and
            # the user can redirect or continue.
            from .agent import close_open_tool_calls  # noqa: PLC0415
            close_open_tool_calls(tui.messages)
            await _print(lambda: tui.console.print(
                "[ansibrightblack](interrupted — you can continue or redirect)[/]"))
        except Exception as e:  # noqa: BLE001
            del tui.messages[base:]

            def _show_err(e: Any = e) -> None:
                tui.console.print(f"[ansired]error:[/] {e}")
                from .tui import error_hint  # noqa: PLC0415
                hint = error_hint(e, tui.backend)
                if hint:
                    styled = re.sub(r"`([^`]+)`", r"[white]\1[/]", hint)  # `cmd` → styled
                    tui.console.print(f"[ansibrightblack]  → {styled}[/]")
            await _print(_show_err)
        finally:
            state.update(working=False, task=None)
            get_app().invalidate()

    async def _slash(text: str) -> bool:
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("/resume", "/branch", "/rewind"):
            # Session commands live on MantisTUI (shared with the classic REPL);
            # they print via self.console, so run them inside in_terminal so the
            # output scrolls above the pinned prompt. Without this wiring these
            # advertised commands fell through and were sent to the model as text.
            from prompt_toolkit.application.run_in_terminal import in_terminal  # noqa: PLC0415
            async with in_terminal():
                if cmd == "/resume":
                    await tui._cmd_resume(arg)
                elif cmd == "/branch":
                    tui._cmd_branch()
                else:
                    tui._cmd_rewind(arg)
            get_app().invalidate()
            return True
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
        if cmd == "/compact":
            if tui.agent is None or len(tui.messages) < 2:
                await _print(lambda: tui.console.print(
                    "[ansibrightblack](nothing to compact yet)[/]"))
                return True
            from .compact import run_manual_compaction  # noqa: PLC0415
            await _print(lambda: tui.console.print(
                "[ansibrightblack](compacting the conversation…)[/]"))
            try:
                new_msgs, note = await run_manual_compaction(
                    tui.messages, tui.agent._summarize, focus=arg.strip())
                tui.messages[:] = new_msgs
            except Exception as e:  # noqa: BLE001
                note = f"compaction failed: {e}"
            await _print(lambda n=note: tui.console.print(f"[ansibrightblack]({n})[/]"))
            return True
        if cmd == "/context":
            await _print(lambda: _show_context())
            return True
        if cmd == "/copy":
            from .types import AssistantMessage, TextBlock  # noqa: PLC0415
            last = ""
            for m in reversed(tui.messages):
                if isinstance(m, AssistantMessage):
                    last = "\n\n".join(b.text for b in m.content if isinstance(b, TextBlock)).strip()
                    if last:
                        break
            if not last:
                await _print(lambda: tui.console.print("[ansibrightblack](nothing to copy yet)[/]"))
                return True
            from .clipboard import copy_to_clipboard  # noqa: PLC0415
            ok = copy_to_clipboard(last)
            msg = "copied last reply to clipboard" if ok else "no clipboard tool found (pbcopy/xclip/wl-copy)"
            await _print(lambda m=msg: tui.console.print(f"[ansibrightblack]({m})[/]"))
            return True
        if cmd == "/export":
            from pathlib import Path  # noqa: PLC0415

            from .tui import render_transcript  # noqa: PLC0415
            text = render_transcript(tui.messages)
            dest = Path(arg).expanduser() if arg else Path.cwd() / "mantis-conversation.md"
            try:
                dest.write_text(text, encoding="utf-8")
                note = f"exported {len(tui.messages)} messages → {dest}"
            except OSError as e:
                note = f"export failed: {e}"
            await _print(lambda n=note: tui.console.print(f"[ansibrightblack]({n})[/]"))
            return True
        if cmd == "/diff":
            import subprocess  # noqa: PLC0415

            from .tui import split_git_diff  # noqa: PLC0415
            try:
                r = subprocess.run(  # noqa: S603
                    ["git", "diff", "HEAD"], capture_output=True, text=True, timeout=8,  # noqa: S607
                )
                untracked = subprocess.run(  # noqa: S603
                    ["git", "ls-files", "--others", "--exclude-standard"],  # noqa: S607
                    capture_output=True, text=True, timeout=8,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                await _print(lambda: tui.console.print("[ansibrightblack](not a git repo — /diff needs git)[/]"))
                return True
            files = split_git_diff(r.stdout)

            def _show_diff() -> None:
                if not files and not untracked:
                    tui.console.print("[ansibrightblack](no changes vs HEAD)[/]")
                    return
                for path, lines in files:
                    tui.console.print(f"\n[bold]{path}[/]")
                    tui._render_diff(lines, path=path)
                if untracked:
                    tui.console.print("\n[bold]new files (untracked)[/]")
                    for f in untracked.splitlines():
                        tui.console.print(f"  [green]+ {f}[/]")
            await _print(_show_diff)
            return True
        if cmd == "/memory":
            import os as _os  # noqa: PLC0415
            import subprocess  # noqa: PLC0415
            from pathlib import Path  # noqa: PLC0415

            from .paths import get_mantis_agent_dir  # noqa: PLC0415
            from .tui import resolve_memory_target  # noqa: PLC0415
            dest = resolve_memory_target(arg, Path.cwd(), get_mantis_agent_dir())
            editor = _os.environ.get("EDITOR") or _os.environ.get("VISUAL") or "vi"
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                await _print(lambda: tui.console.print(
                    f"[ansibrightblack](memory file: {dest} — set $EDITOR to edit)[/]"))
                return True
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    dest.write_text(f"# {dest.name}\n\nProject/agent instructions for mantis.\n")
            except OSError as e:
                await _print(lambda e=e: tui.console.print(f"[ansired]memory error:[/] {e}"))
                return True
            await run_in_terminal(lambda: subprocess.call([editor, str(dest)]))
            # Force the env/context head to rebuild so edits apply next turn.
            if tui.agent is not None:
                tui.agent._env_context = None
            await _print(lambda: tui.console.print(f"[ansibrightblack](edited {dest})[/]"))
            return True
        if cmd == "/vim":
            from prompt_toolkit.enums import EditingMode  # noqa: PLC0415
            tui.vim_mode = not getattr(tui, "vim_mode", False)
            get_app().editing_mode = EditingMode.VI if tui.vim_mode else EditingMode.EMACS
            on = "on" if tui.vim_mode else "off"
            await _print(lambda: tui.console.print(f"[ansibrightblack](vim mode {on})[/]"))
            return True
        if cmd in ("/model", "/models"):
            if arg and cmd == "/model":
                # /model <id|number> → direct switch (exact).
                models = _chat_models()
                if arg.isdigit() and models and 1 <= int(arg) <= len(models):
                    picked = models[int(arg) - 1]
                else:
                    picked = arg
                _switch_model(picked)
                await _print(lambda p=picked: tui.console.print(
                    f"[ansibrightblack](model → [white]{p}[/])[/]"))
            else:
                # /models [partial] → picker overlay, pre-filtered if given.
                _open_model_picker(arg or "")
            return True
        if cmd == "/disable":
            from . import catalog  # noqa: PLC0415

            enabled = [p.id for p in catalog.CATALOG if catalog.is_enabled(p)]
            pid = arg.strip().lower()
            if not pid:
                await _print(lambda e=enabled: tui.console.print(
                    "[ansibrightblack]usage: [white]/disable <provider>[/]  · enabled: "
                    f"[white]{', '.join(e) or 'none'}[/][/]"))
                return True
            prov = catalog.BY_ID.get(pid)
            if prov is None:
                await _print(lambda: tui.console.print(
                    f"[ansibrightblack](unknown provider [white]{pid}[/] — try one of: "
                    f"{', '.join(p.id for p in catalog.CATALOG)})[/]"))
                return True
            removed = catalog.clear_key(pid)
            if pid == "anthropic":
                import os as _os  # noqa: PLC0415
                _os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
                removed = True
            state.pop("model_cache", None)  # picker reflects the change
            await _print(lambda r=removed, lbl=prov.label: tui.console.print(
                f"[ansibrightblack]({'forgot ' + lbl if r else 'no saved key for ' + lbl})[/]"))
            return True
        if cmd == "/enable":
            from . import catalog  # noqa: PLC0415

            pid = arg.strip().lower()
            prov = catalog.BY_ID.get(pid)
            if prov is None:
                await _print(lambda: tui.console.print(
                    "[ansibrightblack]usage: [white]/enable <provider>[/]  · providers: "
                    f"[white]{', '.join(p.id for p in catalog.CATALOG)}[/][/]"))
                return True
            # Reuse the picker's inline key-entry: prompt (masked) for the key,
            # then validate + enable + switch to the provider's flagship model.
            state["awaiting_key"] = {"provider_id": pid, "model": prov.models[0]}
            input_buffer.reset()
            await _announce(f"paste your {prov.api_key_env} to enable {pid} · enter to confirm · esc to cancel")
            return True
        if cmd == "/connect":
            parts = arg.split()
            if not parts or not parts[0].startswith(("http://", "https://")):
                await _print(lambda: tui.console.print(
                    "[ansibrightblack]usage: [white]/connect <url> [model][/] — e.g. "
                    "[white]/connect http://localhost:8000/v1 qwen2.5-coder:7b[/][/]"))
                return True
            url = parts[0].rstrip("/")
            model = parts[1] if len(parts) > 1 else tui.model
            tui.backend, tui.model = url, model
            tui.agent = tui._build_agent()
            if tui.agent is not None and tui.agent.permissions is not None:
                tui.agent.permissions.asker = _ask_permission
            try:
                from . import catalog  # noqa: PLC0415
                catalog.set_last_model(model, url)
            except Exception:  # noqa: BLE001
                pass
            state.pop("model_cache", None)
            await _print(lambda u=url, m=model: tui.console.print(
                f"[ansibrightblack](connected · [white]{m}[/] @ [white]{u}[/] · self-hosted)[/]"))
            return True
        if cmd == "/help":
            def _help() -> None:
                from .tui import build_help_lines  # noqa: PLC0415
                w, d = "white", "ansibrightblack"
                tui.console.print("\n[bold]commands[/]")
                last_cat = None
                for cat, command, desc in build_help_lines(SLASH_COMMANDS):
                    label = cat if cat != last_cat else ""
                    last_cat = cat
                    tui.console.print(f"  [{d}]{label:<8}[/] [{w}]{command}[/]  [{d}]{desc}[/]")
                tui.console.print(
                    f"  [{d}]quit    [/] [{w}]/exit[/]  [{d}](or Ctrl+D · Ctrl+C when idle)[/]")
                tui.console.print(
                    f"  [{d}]keys    [/] [{d}]@file/@dir attaches its content · shift+tab cycles mode "
                    f"· esc/Ctrl+C interrupts a running reply[/]\n")
            await _print(_help)
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

    def _picker_move(delta: int) -> None:
        p = state.get("picking_model")
        if not p:
            return
        items = p["items"]
        n = len(items)
        i = p["sel"]
        for _ in range(n):  # step over header rows to the next model
            i = (i + delta) % n
            if items[i]["kind"] == "model":
                p["sel"] = i
                return

    @kb.add("up", filter=_picker_open)
    @kb.add("c-p", filter=_picker_open)
    def _(event: Any) -> None:
        _picker_move(-1)
        event.app.invalidate()

    @kb.add("down", filter=_picker_open)
    @kb.add("c-n", filter=_picker_open)
    def _(event: Any) -> None:
        _picker_move(1)
        event.app.invalidate()

    # Type-to-filter the picker. A printable char narrows the list; backspace
    # widens it. Specific bindings (up/down/enter/esc) take precedence over Any.
    from prompt_toolkit.keys import Keys  # noqa: PLC0415

    @kb.add(Keys.Any, filter=_picker_open)
    def _(event: Any) -> None:
        ch = event.data
        if ch and len(ch) == 1 and ch.isprintable():
            p = state["picking_model"]
            p["filter"] = p.get("filter", "") + ch
            _refilter_picker()
            event.app.invalidate()

    @kb.add("backspace", filter=_picker_open)
    def _(event: Any) -> None:
        p = state["picking_model"]
        if p.get("filter"):
            p["filter"] = p["filter"][:-1]
            _refilter_picker()
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
        # Inline provider-enable: the line holds a pasted API key for a locked
        # provider picked from /models. Validate + enable + switch.
        ak = state.get("awaiting_key")
        if ak is not None:
            key = input_buffer.text.strip()
            input_buffer.reset()
            state["awaiting_key"] = None
            if key:
                event.app.create_background_task(_apply_key(ak["provider_id"], ak["model"], key))
            else:
                event.app.create_background_task(_announce("cancelled — no key entered"))
            event.app.invalidate()
            return
        # Model picker open → act on the highlighted row and close it.
        p = state.get("picking_model")
        if p is not None:
            items = p["items"]
            it = items[p["sel"]] if 0 <= p["sel"] < len(items) else None
            state["picking_model"] = None
            if it and it["kind"] == "model":
                if it["enabled"]:
                    _switch_model(it["model"], provider_id=it.get("provider_id"))
                    event.app.create_background_task(_announce(f"model → {it['model']}"))
                else:
                    # Locked provider → ask for its key inline, then enable+switch.
                    pid = it.get("provider_id")
                    if pid:
                        from . import catalog  # noqa: PLC0415
                        prov = catalog.BY_ID.get(pid)
                        env = prov.api_key_env if prov else "API key"
                        state["awaiting_key"] = {"provider_id": pid, "model": it["model"]}
                        input_buffer.reset()
                        event.app.create_background_task(
                            _announce(f"paste your {env} to enable {pid} · enter to confirm · esc to cancel"))
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
        # Esc cancels an inline key entry (don't save the pasted key).
        if state.get("awaiting_key") is not None:
            state["awaiting_key"] = None
            input_buffer.reset()
            event.app.invalidate()
            return
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

    @kb.add("c-x", "c-e")
    def _(event: Any) -> None:
        # Compose a long / multi-line prompt in $EDITOR (like the shell's C-x C-e).
        try:
            input_buffer.open_in_editor(event.app)
        except Exception:  # noqa: BLE001 — no editor / spawn failed: ignore
            pass

    input_window = Window(
        BufferControl(
            buffer=input_buffer,
            # Mask the line (••••) ONLY while pasting an API key for a locked
            # provider — normal chat input stays visible.
            input_processors=[ConditionalProcessor(
                PasswordProcessor(),
                Condition(lambda: state.get("awaiting_key") is not None),
            )],
        ),
        height=1, wrap_lines=False,
    )
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
    from prompt_toolkit.enums import EditingMode  # noqa: PLC0415
    app = Application(
        layout=layout, key_bindings=kb, full_screen=False, erase_when_done=True,
        editing_mode=EditingMode.VI if getattr(tui, "vim_mode", False) else EditingMode.EMACS,
    )

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
