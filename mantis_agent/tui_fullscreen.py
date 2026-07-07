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
        AppendAutoSuggestion,
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
    from .tui import set_terminal_title  # noqa: PLC0415
    if not tui.messages:  # a resumed session already set its own title
        import os as _os  # noqa: PLC0415
        set_terminal_title(f"mantis · {_os.path.basename(_os.getcwd()) or 'home'}")
    if tui.messages:
        # --continue / --resume preloaded a conversation: replay it under the
        # banner so the user sees exactly what they're resuming.
        try:
            tui._replay_transcript()
        except Exception:  # noqa: BLE001 — replay is cosmetic, never fatal
            pass
    tui.agent = tui._build_agent()
    tui._kick_prewarm()
    # Start an on-disk session so this conversation is persisted per turn and
    # /resume, /branch, /rewind have data (the fullscreen path never did this).
    if getattr(tui, "transcript", None) is None:
        try:
            from .session_tree import SessionTranscript, new_session_id  # noqa: PLC0415
            tui.transcript = SessionTranscript(new_session_id())
        except Exception:  # noqa: BLE001 — persistence is best-effort
            tui.transcript = None

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
        total = len(questions)
        for n, q in enumerate(questions, 1):
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            state["pending_question"] = {"q": q, "sel": 0, "selected": set(),
                                         "typing": False, "future": fut,
                                         "index": n, "total": total}
            get_app().invalidate()
            try:
                answers = await fut
            except asyncio.CancelledError:
                answers = []
            finally:
                state["pending_question"] = None
                get_app().invalidate()
            # Echo the pick into scrollback so the decision stays visible after
            # the overlay closes (the tool-result line alone is easy to miss).
            picked = ", ".join(answers) if answers else "(skipped)"
            await _print(lambda h=q["header"], pk=picked: tui.console.print(
                f"[ansigreen]?[/] [ansibrightblack]{h} →[/] [white]{pk}[/]"))
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

    # Prompt history persists across sessions (~/.mantis-agent/history) — up/down
    # recall past prompts, exactly like a shell. Best-effort: an unwritable home
    # falls back to in-memory history rather than breaking the input.
    try:
        from prompt_toolkit.history import FileHistory  # noqa: PLC0415

        from .paths import ensure_dir as _ensure_dir  # noqa: PLC0415
        from .paths import get_mantis_agent_dir as _home  # noqa: PLC0415
        _ensure_dir(_home())
        _hist: Any = FileHistory(str(_home() / "history"))
    except Exception:  # noqa: BLE001
        _hist = None

    # Next-prompt ghost text: after each turn a tiny model call proposes the
    # likely follow-up; it renders dim after the cursor and tab/→ accepts it.
    from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion  # noqa: PLC0415

    class _NextPromptSuggest(AutoSuggest):
        def get_suggestion(self, buffer: Any, document: Any) -> Any:
            s = state.get("suggested_prompt") or ""
            t = document.text
            if s and len(t) < len(s) and s.lower().startswith(t.lower()):
                return Suggestion(s[len(t):])
            return None

    _kw: dict[str, Any] = {"multiline": False, "auto_suggest": _NextPromptSuggest()}
    if _hist:
        _kw["history"] = _hist
    input_buffer = Buffer(**_kw)

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
        # Built-ins + user/project .mantis/commands/*.md, cached per session —
        # discovery hits the filesystem and this runs every keystroke.
        cmds = state.get("all_commands")
        if cmds is None:
            from .tui import all_slash_commands  # noqa: PLC0415
            try:
                cmds = all_slash_commands()
            except Exception:  # noqa: BLE001
                cmds = dict(SLASH_COMMANDS)
            state["all_commands"] = cmds
        return [("cmd", c, d) for c, d in cmds.items() if c.startswith(t)]

    # -- model picker overlay (state-driven, like the permission prompt) ------

    _PICKER_ROWS = 11  # visible model/header rows in the picker window

    def _ollama_models() -> list[str]:
        """Installed local Ollama models (free). Cached ~60s so the picker's
        per-keystroke refilter never re-probes; one 1.5s probe max on open."""
        cache = state.get("ollama_models")
        if cache is not None and time.monotonic() - cache[0] < 60:
            return cache[1]
        models: list[str] = []
        try:
            import httpx  # noqa: PLC0415

            from . import paths as _pp  # noqa: PLC0415
            r = httpx.get(f"{_pp.ollama_base_url()}/api/tags", timeout=1.5)
            if r.status_code == 200:
                models = [m.get("name", "") for m in r.json().get("models", [])
                          if m.get("name")]
        except Exception:  # noqa: BLE001 — Ollama not running: just no group
            pass
        state["ollama_models"] = (time.monotonic(), models)
        return models

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
        # Local Ollama models (FREE, open-source) — always shown when Ollama is
        # running, even while the active backend is a hosted API. Selecting one
        # switches the backend to localhost with no key.
        back_now = (tui.backend or "").rstrip("/")
        if "localhost" not in back_now and "127.0.0.1" not in back_now:
            from . import paths as _pp  # noqa: PLC0415
            local = [m for m in _ollama_models()
                     if _is_chat_model(m) and m not in active_set and keep(m)]
            if local:
                items.append({"kind": "header", "label": "Ollama · local · free"})
                for m in local:
                    items.append({"kind": "model", "model": m, "enabled": True,
                                  "provider_id": None, "ctx": ctxlab(m),
                                  "backend": _pp.ollama_base_url()})
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

    # -- session picker (same overlay, mode="session") ------------------------

    def _build_session_items(flt: str) -> list[dict]:
        from .tui import ellipsize, time_ago  # noqa: PLC0415
        from .session_tree import list_sessions  # noqa: PLC0415
        fl = flt.lower().strip()
        cur = tui.transcript.session_id if getattr(tui, "transcript", None) else None
        items: list[dict] = [{"kind": "header", "label": "Resume a conversation"}]
        for s in list_sessions()[:50]:
            if s.session_id == cur:
                continue
            title = ellipsize(s.title or s.first_prompt or "(untitled)", 48)
            label = f"{title} · {s.message_count} msgs · {time_ago(s.modified_at)}"
            if fl and fl not in label.lower():
                continue
            items.append({"kind": "session", "model": label, "enabled": True,
                          "provider_id": None, "ctx": "", "session_id": s.session_id})
        return items

    def _build_rewind_items(flt: str) -> list[dict]:
        """Past user messages, newest last — esc-esc opens this to jump back,
        restore the code state, and edit the message you jumped to."""
        from .tui import ellipsize  # noqa: PLC0415
        from .types import TextBlock as _TB, UserMessage as _UM  # noqa: PLC0415
        fl = flt.lower().strip()
        items: list[dict] = [{"kind": "header", "label": "Rewind to (edits + files restored)"}]
        for i, m in enumerate(tui.messages):
            if not isinstance(m, _UM) or getattr(m, "isMeta", False):
                continue
            text = m.content if isinstance(m.content, str) else next(
                (b.text for b in m.content if isinstance(b, _TB)), "")
            if not text.strip():
                continue
            label = ellipsize(text, 60)
            if fl and fl not in label.lower():
                continue
            items.append({"kind": "rewind", "model": label, "enabled": True,
                          "provider_id": None, "ctx": "", "msg_index": i,
                          "text": text})
        return items

    def _open_rewind_picker() -> None:
        items = _build_rewind_items("")
        rows = [i for i, it in enumerate(items) if it["kind"] == "rewind"]
        if not rows:
            get_app().create_background_task(_announce("nothing to rewind to"))
            return
        state["picking_model"] = {"items": items, "sel": rows[-1], "filter": "",
                                  "mode": "rewind"}
        get_app().invalidate()

    def _open_session_picker(flt: str = "") -> None:
        items = _build_session_items(flt)
        if len(items) <= 1:
            get_app().create_background_task(_announce("no past conversations to resume"))
            return
        sel = next((i for i, it in enumerate(items) if it["kind"] == "session"), 0)
        state["picking_model"] = {"items": items, "sel": sel, "filter": flt,
                                  "mode": "session"}
        get_app().invalidate()

    _SELECTABLE_KINDS = ("model", "session", "rewind")

    def _refilter_picker() -> None:
        p = state.get("picking_model")
        if not p:
            return
        build = {"session": _build_session_items,
                 "rewind": _build_rewind_items}.get(p.get("mode"), _build_picker_items)
        items = build(p.get("filter", ""))
        p["items"] = items
        if not (0 <= p["sel"] < len(items) and items[p["sel"]]["kind"] in _SELECTABLE_KINDS):
            p["sel"] = next((i for i, it in enumerate(items)
                             if it["kind"] in _SELECTABLE_KINDS), 0)

    def picker_ft() -> Any:
        p = state.get("picking_model")
        if not p:
            return ANSI("")
        items, sel = p["items"], p["sel"]
        n = len(items)
        lo = max(0, min(sel - _PICKER_ROWS // 2, n - _PICKER_ROWS))
        flt = p.get("filter", "")
        title = {"session": "Resume a conversation",
                 "rewind": "Rewind to"}.get(p.get("mode"), "Pick a model")
        _hdr = f"{_GREEN}{title}{_RESET}  {_GREY}(↑/↓ · type to filter · enter · esc){_RESET}"
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

    def _switch_model(model_id: str, *, provider_id: str | None = None,
                      backend: str | None = None) -> None:
        from . import catalog  # noqa: PLC0415

        # Sanitize first: a picker filter / pasted id can carry whitespace or a
        # trailing newline, and a blank id must never blow away the active model.
        model_id = (model_id or "").strip()
        if not model_id:
            return
        if backend:
            # Explicit backend from the picker item (a local Ollama model while
            # on a hosted API): point straight at it, no key needed.
            tui.backend, tui.api_key = backend, None
            tui.model = model_id
            tui.agent = tui._build_agent()
            if tui.agent is not None and tui.agent.permissions is not None:
                tui.agent.permissions.asker = _ask_permission
            try:
                catalog.set_last_model(model_id, backend)
                catalog.push_recent_model(model_id)
            except Exception:  # noqa: BLE001
                pass
            return
        # Cross-provider switch: if this model belongs to a *different* enabled
        # provider (e.g. a Claude model while on OpenAI), re-wire the backend +
        # key too — otherwise the new model would be sent to the old endpoint
        # and 404/auth-fail. Falls back to the current backend for local ids.
        # When the picker knows the exact provider (``provider_id``), trust it —
        # ids like ``openai/gpt-oss-120b`` live under several providers and the
        # prefix heuristic can't tell which group the user actually picked.
        prov = catalog.BY_ID.get(provider_id) if provider_id else catalog.provider_for_model(model_id)
        if prov is not None:
            # NOTE: unlike the startup auto-wire, do NOT fall back to tui.api_key
            # (the generic key) here — mid-session it holds the *current* backend's
            # key, which may be a gateway's or another provider's, and sending it to
            # prov.base_url would auth-fail. Only the provider's own key is safe.
            key = catalog.api_key_for(prov)
            if key:
                tui.backend, tui.api_key = prov.base_url, key
            else:
                # Anthropic OAuth/gateway Bearer token (api_key_for → None): wire the
                # backend with no api_key so the passthrough uses the env Bearer,
                # preserving an existing gateway. None → not applicable.
                wired = catalog.anthropic_bearer_backend(prov, tui.backend)
                if wired is not None:
                    tui.backend, tui.api_key = wired, None
        tui.model = model_id
        tui.agent = tui._build_agent()
        if tui.agent is not None and tui.agent.permissions is not None:
            tui.agent.permissions.asker = _ask_permission
        try:
            catalog.set_last_model(model_id, tui.backend)
            catalog.push_recent_model(model_id)
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
            # A transient connection problem shows as ONE in-place note on the
            # spinner line (set by the retry hook) — not printed lines tearing
            # through the frame. It disappears once its window passes.
            note = state.get("retry_note")
            extra = ""
            if note and time.monotonic() < note[1]:
                extra = f"  \033[33m{note[0]}\033[0m"
            nq = len(state.get("queue") or [])
            queued = f" {_DIM}· {nq} queued{_RESET}" if nq else ""
            return ANSI(
                f"{_GREEN}{frame} {state['word']}…{_RESET} "
                f"{_DIM}({el}s · esc to interrupt){_RESET}{queued}{extra}"
            )
        label, symbol, color = MODES[tui.mode_idx]
        left = "" if tui.mode_idx == 0 else f"{symbol}{label} (shift+tab to cycle)"
        col = _MODE_ANSI.get(color, "90")
        ctx = _ctx_status()
        ctx_seg = f"   {ctx}" if ctx else ""
        return ANSI(f"\033[{col}m{left}{_RESET}   {_GREY}{tui.model}{_RESET}{ctx_seg}")

    _LIVE_TODO_ROWS = 8

    def live_todos_ft() -> Any:
        """The checklist pinned under the spinner while the agent works —
        Claude Code's `⎿ □ current task / ✔ struck-done` block. Collapses to
        nothing when idle (the scrollback render covers history)."""
        if not (state["working"] and tui.todos):
            return ANSI("")
        from .tui import format_live_todo_rows  # noqa: PLC0415
        width = shutil.get_terminal_size((80, 24)).columns
        return ANSI("\n".join(format_live_todo_rows(tui.todos, width, max_rows=_LIVE_TODO_ROWS)))

    def _live_todos_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        if not (state["working"] and tui.todos):
            return Dimension.exact(0)
        n = min(len(tui.todos), _LIVE_TODO_ROWS)
        return Dimension.exact(n + (1 if len(tui.todos) > _LIVE_TODO_ROWS else 0))

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
        from .tui import format_question_rows  # noqa: PLC0415
        width = shutil.get_terminal_size((80, 24)).columns
        rows = format_question_rows(
            p["q"], p["sel"], p["selected"], p["typing"], width,
            index=p.get("index", 1), total=p.get("total", 1))
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
        from .tui import echo_user_message  # noqa: PLC0415
        echo_user_message(tui.console, t)  # grey bar, Claude-Code style
        tui.console.print()

    def _assist(m: Any) -> None:
        had_tool_call = tui._render_assistant(m, ToolUseBlock)
        if not had_tool_call:  # text block → blank below; tool call → hug result
            tui.console.print()

    def _result(m: Any) -> None:
        tui._render_tool_results(m, ToolResultBlock)
        tui.console.print()

    async def _handle(text: str) -> None:
        state["suggested_prompt"] = None  # a submitted turn invalidates the ghost
        input_buffer.suggestion = None
        await _print(lambda: _echo(text))

        # ``!cmd`` → run the shell command NOW, print its output, and inject it
        # into context as a meta message (Claude Code's ! prefix). No model turn.
        if text.startswith("!") and len(text) > 1:
            from rich.markup import escape as _resc  # noqa: PLC0415

            from .tui import bang_context_block, run_bang_command  # noqa: PLC0415
            cmd = text[1:].strip()
            out = await asyncio.to_thread(run_bang_command, cmd)
            await _print(lambda o=out: tui.console.print(
                f"[ansibrightblack]{_resc(o)}[/]"))
            tui.messages.append(UserMessage(content=bang_context_block(cmd, out), isMeta=True))
            get_app().invalidate()
            return
        # ``# note`` → quick-save to persistent memory (Claude Code's # prefix).
        if text.startswith("#") and text.lstrip("#").strip():
            from .tui import quick_memory_note  # noqa: PLC0415
            p = quick_memory_note(text.lstrip("#").strip())
            await _announce(f"saved to memory → {p.name}")
            return

        from .tui import expand_slash_prompt  # noqa: PLC0415
        expanded = expand_slash_prompt(text)
        if expanded is not None:
            text = expanded  # e.g. /init → canned prompt; runs as a normal turn
        elif text.startswith("/"):
            # A command that throws (e.g. /twin with the network down) must end
            # in ONE clean error line + hint — never a raw traceback screen.
            try:
                handled = await _slash(text)
            except Exception as e:  # noqa: BLE001
                state.update(working=False, task=None)  # a command may have set it

                def _show_cmd_err(e: Any = e) -> None:
                    tui.console.print(f"[ansired]error:[/] {e}")
                    from .tui import error_hint  # noqa: PLC0415
                    hint = error_hint(e, tui.backend)
                    if hint:
                        styled = re.sub(r"`([^`]+)`", r"[white]\1[/]", hint)
                        tui.console.print(f"[ansibrightblack]  → {styled}[/]")
                    tui.console.print()
                await _print(_show_cmd_err)
                handled = True
            if handled:
                get_app().invalidate()
                return

        base = len(tui.messages)
        # _build_user_content flushes pending Ctrl+V attachments (images/files)
        # into real content blocks; plain turns stay plain strings.
        tui.messages.append(UserMessage(content=tui._build_user_content(text)))
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
                    _txt = "".join(getattr(b, "text", "") for b in msg.content
                                   if type(b).__name__ == "TextBlock")
                    if _txt.strip():
                        state["last_text"] = _txt  # goal engine reads markers
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
            # the user can redirect or continue. An interrupt also drops any
            # queued messages — the user is taking the wheel back.
            dropped = len(state.get("queue") or [])
            state.get("queue", []).clear()
            goal_note = ""
            if state.get("goal"):
                state["goal"] = None  # interrupt = the human takes the wheel
                goal_note = " · autopilot stopped"
            from .agent import close_open_tool_calls  # noqa: PLC0415
            close_open_tool_calls(tui.messages)
            await _print(lambda d=dropped, gn=goal_note: tui.console.print(
                "[ansibrightblack](interrupted — you can continue or redirect"
                + (f" · {d} queued message{'s' if d != 1 else ''} dropped" if d else "")
                + gn + ")[/]"))
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
            tui._persist_messages(base)  # save this turn for /resume + /branch
            elapsed = time.monotonic() - state.get("started", time.monotonic())
            state.update(working=False, task=None)
            from .tui import notify_turn_done  # noqa: PLC0415
            notify_turn_done(elapsed)  # bell after a long turn (settings: notifChannel)
            get_app().invalidate()
            # Session title after the first completed turn (cheap, background).
            get_app().create_background_task(tui._maybe_autotitle())
            # Fire the next queued message, if any (one at a time, in order).
            q = state.get("queue") or []
            if q:
                nxt = q.pop(0)
                get_app().create_background_task(_handle(nxt))
            elif state.get("goal"):
                _advance_goal()
            else:
                # Idle again → propose the next prompt as ghost text (tab/→).
                async def _ghost() -> None:
                    s = await tui._suggest_next_prompt()
                    if s and not state["working"] and not input_buffer.text:
                        from prompt_toolkit.auto_suggest import Suggestion as _S  # noqa: PLC0415
                        state["suggested_prompt"] = s
                        input_buffer.suggestion = _S(s)
                        get_app().invalidate()
                get_app().create_background_task(_ghost())

    def _advance_goal() -> None:
        """The autopilot state machine, called after each idle turn end:
        work → (todos all done) → verify → (GOAL COMPLETE) → reflect → finish.
        Blocked/complete markers and the cycle cap are the exits; esc clears."""
        from .tui import (  # noqa: PLC0415
            GOAL_BLOCKED_MARKER,
            GOAL_COMPLETE_MARKER,
            goal_continue_prompt,
            goal_reflect_prompt,
            goal_verify_prompt,
        )
        g = state.get("goal")
        if not g or state["working"]:
            return
        last = state.get("last_text") or ""

        def fire(prompt: str) -> None:
            get_app().create_background_task(_handle(prompt))

        def note(msg: str) -> None:
            get_app().create_background_task(_announce(msg))

        if GOAL_BLOCKED_MARKER in last:
            state["goal"] = None
            note("⦿ autopilot: goal blocked — stopped")
            return
        if g.get("phase") == "reflect":
            state["goal"] = None
            note(f"⦿ autopilot: goal complete after {g['cycles']} cycles ✓")
            return
        if g.get("phase") == "verify" and GOAL_COMPLETE_MARKER in last:
            g["phase"] = "reflect"
            fire(goal_reflect_prompt(g["text"]))
            return
        todos = tui.todos
        all_done = bool(todos) and all(t.get("status") == "completed" for t in todos)
        if all_done and g.get("phase") != "verify":
            g["phase"] = "verify"
            note("⦿ autopilot: plan finished — verifying adversarially…")
            fire(goal_verify_prompt(g["text"]))
            return
        g["cycles"] += 1
        if g["cycles"] > g["max"]:
            state["goal"] = None
            note(f"⦿ autopilot: cycle cap ({g['max']}) reached — stopped. "
                 "Re-issue /goal to keep going.")
            return
        g["phase"] = "work"
        fire(goal_continue_prompt(g["text"], g["cycles"], g["max"]))

    async def _watch_loop(wid: int, cmdline: str, interval: float, rec: dict) -> None:
        """Sentinel: run ``cmdline`` every ``interval``s. Edge-triggered — the
        ok→FAIL transition wakes the agent with the failure output; FAIL→ok just
        announces green. Never fires while a turn/overlay is active."""
        import subprocess  # noqa: PLC0415
        prev_ok: bool | None = None
        while not rec["stopped"].is_set():
            def run() -> tuple[bool, str]:
                try:
                    r = subprocess.run(cmdline, shell=True, capture_output=True,
                                       text=True, timeout=300)
                    return r.returncode == 0, ((r.stdout or "") + (r.stderr or ""))[-4000:]
                except Exception as e:  # noqa: BLE001
                    return False, f"(watch runner error: {e})"
            ok, out = await asyncio.to_thread(run)
            rec["state"] = "green" if ok else "red"
            rec["runs"] = rec.get("runs", 0) + 1
            if prev_ok is True and not ok:
                await _announce(f"⦿ watch #{wid} went RED — waking the agent")
                while ((state["working"] or state.get("pending_perm")
                        or state.get("pending_question")) and not rec["stopped"].is_set()):
                    await asyncio.sleep(1.0)
                if rec["stopped"].is_set():
                    return
                await _handle(
                    f"[watch #{wid}] `{cmdline}` just started FAILING:\n{out}\n\n"
                    "Diagnose and fix the root cause, then rerun the command to "
                    "confirm it passes.")
            elif prev_ok is False and ok:
                await _announce(f"⦿ watch #{wid} green again ✓")
            prev_ok = ok
            waited = 0.0
            while waited < interval and not rec["stopped"].is_set():
                await asyncio.sleep(min(1.0, interval - waited))
                waited += 1.0

    async def _slash(text: str) -> bool:
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd == "/resume" and not arg:
            # Arrow-key picker overlay (like /models) — type to filter, enter
            # to resume, esc to close. `/resume <n|id>` still loads directly.
            _open_session_picker()
            return True
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
                # /model <query> → resolve a number / id / provider / alias /
                # fuzzy fragment to a REAL model. Never send a raw string to the
                # backend: an unrecognized or ambiguous query opens the picker,
                # pre-filtered, so the user lands on something that exists.
                from . import catalog  # noqa: PLC0415
                res = catalog.resolve_model_query(arg, _chat_models())
                if res.model:
                    _switch_model(res.model, provider_id=res.provider_id)
                    await _print(lambda m=res.model: tui.console.print(
                        f"[ansibrightblack](model → [white]{m}[/])[/]"))
                elif res.candidates:
                    await _print(lambda n=len(res.candidates): tui.console.print(
                        f"[ansibrightblack]{n} models match [white]{arg}[/] — pick one:[/]"))
                    _open_model_picker(arg)
                else:
                    await _print(lambda: tui.console.print(
                        f"[ansiyellow]![/] [ansibrightblack]no model matches "
                        f"[white]{arg}[/] — showing the full list[/]"))
                    _open_model_picker(arg)
            else:
                # /models [partial] → picker overlay, pre-filtered if given.
                _open_model_picker(arg or "")
            return True
        if cmd == "/agents":
            await _print(lambda: tui._show_agents())
            return True
        if cmd == "/twin":
            if tui._pair_tool is None:
                await _announce("no agent yet — send a message first")
                return True
            if not arg.strip() or arg.split()[0] in ("list", "reset"):
                await _print(lambda: tui._twin_admin(arg))  # no LLM — inline
                return True
            # A real exchange runs the twin's full agent loop (LLM + read
            # tools) — drive the spinner so the wait reads as work, not a hang.
            from .tui import MantisTUI as _M  # noqa: PLC0415
            peer, msg = _M.parse_twin_arg(arg)
            state.update(working=True, started=time.monotonic(),
                         word="Conferring", task=asyncio.current_task())
            get_app().invalidate()
            try:
                body = await tui._twin_talk(peer, msg)
            finally:
                state.update(working=False, task=None)
            await _print(lambda: tui._twin_render(peer, body))
            get_app().invalidate()
            return True
        if cmd == "/goal":
            from .tui import GOAL_MAX_CYCLES, goal_kickoff_prompt  # noqa: PLC0415
            sub = arg.strip()
            g = state.get("goal")
            if not sub or sub == "status":
                if g:
                    await _announce(f"⦿ autopilot · cycle {g['cycles']}/{g['max']} · "
                                    f"{g['phase']} · {g['text'][:60]}")
                else:
                    await _announce("no active goal — /goal <what you want done>")
                return True
            if sub == "stop":
                state["goal"] = None
                await _announce("⦿ autopilot stopped" if g else "no active goal")
                return True
            tui.todos.clear()  # fresh plan; the todo tool mutates this list in place
            state["goal"] = {"text": sub, "cycles": 0, "max": GOAL_MAX_CYCLES,
                             "phase": "work"}
            await _announce(f"⦿ autopilot engaged · up to {GOAL_MAX_CYCLES} cycles · "
                            "esc stops it")
            get_app().create_background_task(_handle(goal_kickoff_prompt(sub)))
            return True
        if cmd == "/watch":
            import asyncio as _aio  # noqa: PLC0415

            from .tui import format_loop_interval, parse_loop_command  # noqa: PLC0415
            watches: dict[int, dict] = state.setdefault("watches", {})
            sub = arg.strip()
            if not sub or sub == "list":
                if not watches:
                    await _announce("no watches — /watch [interval] <command> "
                                    "(fires the agent when it starts failing)")
                for wid, w in watches.items():
                    await _announce(f"watch #{wid} · every {format_loop_interval(w['interval'])} "
                                    f"· {w.get('state', '?')} · {w['cmd'][:50]}")
                return True
            if sub.split()[0] == "stop":
                which = sub.split()[1] if len(sub.split()) > 1 else "all"
                targets = list(watches) if which == "all" else \
                    [int(which)] if which.isdigit() and int(which) in watches else []
                for wid in targets:
                    watches[wid]["stopped"].set()
                    watches.pop(wid, None)
                await _announce(f"stopped {len(targets)} watch(es)" if targets
                                else f"no watch {which!r}")
                return True
            parsed = parse_loop_command(sub)
            interval, cmdline = parsed if isinstance(parsed, tuple) else (30.0, sub)
            wid = state["watch_counter"] = state.get("watch_counter", 0) + 1
            rec = {"cmd": cmdline, "interval": interval, "stopped": _aio.Event(),
                   "state": "?"}
            watches[wid] = rec
            rec["task"] = _aio.ensure_future(_watch_loop(wid, cmdline, interval, rec))
            await _announce(f"⦿ watch #{wid} armed · `{cmdline[:50]}` every "
                            f"{format_loop_interval(interval)} — /watch stop {wid} to end")
            return True
        if cmd == "/loop":
            import asyncio as _aio  # noqa: PLC0415

            from .tui import (  # noqa: PLC0415
                format_loop_interval,
                parse_loop_command,
                run_prompt_loop,
            )
            loops: dict[int, dict] = state.setdefault("loops", {})
            sub = arg.strip()
            if not sub or sub == "list":
                if not loops:
                    await _announce("no active loops — /loop <interval> <prompt> to start one")
                else:
                    for lid, l in loops.items():
                        await _announce(
                            f"loop #{lid} · every {format_loop_interval(l['interval'])} · "
                            f"{l['fires']} fires · {l['prompt'][:60]}")
                    await _announce("stop with /loop stop <id> (or /loop stop all)")
                return True
            if sub.split()[0] == "stop":
                which = sub.split()[1] if len(sub.split()) > 1 else "all"
                targets = list(loops) if which == "all" else \
                    [int(which)] if which.isdigit() and int(which) in loops else []
                if not targets:
                    await _announce(f"no loop {which!r} — active: {', '.join(map(str, loops)) or 'none'}")
                    return True
                for lid in targets:
                    loops[lid]["stopped"].set()
                    loops.pop(lid, None)
                await _announce(f"stopped loop{'s' if len(targets) > 1 else ''} "
                                f"{', '.join(f'#{t}' for t in targets)}")
                return True
            parsed = parse_loop_command(sub)
            if isinstance(parsed, str):
                await _announce(parsed)
                return True
            interval, prompt = parsed
            lid = state["loop_counter"] = state.get("loop_counter", 0) + 1
            stopped = _aio.Event()
            rec = {"interval": interval, "prompt": prompt, "fires": 0, "stopped": stopped}
            loops[lid] = rec

            async def _fire(rec: dict = rec, lid: int = lid) -> None:
                rec["fires"] += 1
                await _announce(f"loop #{lid} · fire {rec['fires']}")
                await _handle(rec["prompt"])

            def _busy() -> bool:
                return bool(state["working"] or state.get("pending_perm")
                            or state.get("pending_question") or state.get("awaiting_key"))

            def _on_error(e: Exception, lid: int = lid) -> None:
                get_app().create_background_task(_announce(f"loop #{lid} fire failed: {e}"))

            task = _aio.ensure_future(run_prompt_loop(
                interval, _fire, is_busy=_busy, stopped=stopped, on_error=_on_error))
            rec["task"] = task
            await _announce(
                f"loop #{lid} started · every {format_loop_interval(interval)} · "
                f"{prompt[:60]} — /loop stop {lid} to end")
            return True
        if cmd == "/mcp":
            await _print(lambda: tui._show_mcp())
            return True
        if cmd == "/skills":
            await _print(lambda: tui._show_skills())
            return True
        if cmd == "/status":
            await _print(lambda: tui._show_status(state["ctx_tokens"], state["session_cost"]))
            return True
        if cmd == "/cost":
            await _print(lambda: tui._show_cost(state["ctx_tokens"], state["session_cost"]))
            return True
        if cmd == "/doctor":
            # Has short network probes (2s cap each) — render inside the
            # terminal handoff like every other block so output can't interleave
            # with the prompt_toolkit frame.
            await _print(tui._show_doctor)
            return True
        if cmd == "/permissions":
            await _print(lambda: tui._show_permissions())
            return True
        if cmd == "/release-notes":
            await _print(lambda: tui._show_release_notes())
            return True
        if cmd == "/update":
            await _announce("checking for updates…")
            msg = await asyncio.to_thread(tui._run_update)
            await _announce(msg)
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
                from .tui import all_slash_commands, build_help_lines  # noqa: PLC0415
                w, d = "white", "ansibrightblack"
                tui.console.print("\n[bold]commands[/]")
                last_cat = None
                for cat, command, desc in build_help_lines(all_slash_commands()):
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
        for _ in range(n):  # step over header rows to the next selectable row
            i = (i + delta) % n
            if items[i]["kind"] in _SELECTABLE_KINDS:
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
            if it and it["kind"] == "rewind":
                idx = it["msg_index"]
                tui.messages = tui.messages[:idx]
                restored = tui._restore_checkpoints(idx)
                # Put the chosen message back in the input for editing — the
                # esc-esc flow is "go back and say it differently".
                input_buffer.text = it["text"]
                input_buffer.cursor_position = len(it["text"])
                note = f"rewound · {len(tui.messages)} messages kept"
                if restored:
                    note += f" · {restored} file{'s' if restored != 1 else ''} restored"
                event.app.create_background_task(_announce(note))
                event.app.invalidate()
                return
            if it and it["kind"] == "session":
                from .session_tree import SessionTranscript, load_for_resume  # noqa: PLC0415
                try:
                    tui.messages = load_for_resume(it["session_id"])
                    tui.transcript = SessionTranscript(it["session_id"])

                    async def _show_resumed(label: str = it["model"]) -> None:
                        from .tui import set_terminal_title  # noqa: PLC0415
                        set_terminal_title(f"✳ {label.split(' · ')[0]}")
                        await _print(lambda: tui._replay_transcript())
                        await _announce(f"resumed · {label} ({len(tui.messages)} messages)")
                    event.app.create_background_task(_show_resumed())
                except Exception as e:  # noqa: BLE001
                    event.app.create_background_task(_announce(f"resume failed: {e}"))
                event.app.invalidate()
                return
            if it and it["kind"] == "model":
                if it["enabled"]:
                    _switch_model(it["model"], provider_id=it.get("provider_id"),
                                  backend=it.get("backend"))
                    where = " · local (free)" if it.get("backend") else ""
                    event.app.create_background_task(
                        _announce(f"model → {it['model']}{where}"))
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
        # Record the submitted line in the persistent history (up-arrow recall
        # across sessions). The API-key path above resets WITHOUT this — a
        # pasted secret must never land in ~/.mantis-agent/history.
        input_buffer.reset(append_to_history=True)
        if not text:
            return
        if state["working"]:
            # A turn is running: QUEUE the message (Claude Code behavior) —
            # it fires the moment this turn finishes. Esc-interrupt clears it.
            q = state.setdefault("queue", [])
            q.append(text)
            event.app.create_background_task(_announce(
                f"⧉ queued ({len(q)}) — sends when this turn finishes · esc clears"))
            return
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
        from .tui import esc_action  # noqa: PLC0415
        pq = state.get("pending_question")
        # Esc-Esc while idle (no overlays, nothing running) → the rewind picker:
        # jump back to an earlier message, restore files, edit and resend.
        now = time.monotonic()
        last = state.get("last_esc", 0.0)
        state["last_esc"] = now
        if (now - last < 0.6 and not state["working"]
                and state.get("picking_model") is None
                and state.get("pending_perm") is None and pq is None
                and state.get("awaiting_key") is None and tui.messages):
            _open_rewind_picker()
            return
        action = esc_action(
            awaiting_key=state.get("awaiting_key") is not None,
            picking_model=state.get("picking_model") is not None,
            pending_perm=state.get("pending_perm") is not None,
            question_open=pq is not None,
            question_typing=bool(pq and pq.get("typing")),
            working=bool(state["working"]),
            has_input=bool(input_buffer.text),
        )
        if action == "cancel_key":
            state["awaiting_key"] = None
            input_buffer.reset()
        elif action == "close_picker":
            state["picking_model"] = None
        elif action == "deny":
            _resolve_perm("deny")
            return
        elif action == "cancel_question_typing":
            pq["typing"] = False
            input_buffer.reset()
        elif action == "skip_question":
            _q_resolve([])
            return
        elif action == "interrupt":
            task = state.get("task")
            if task is not None:
                task.cancel()
            return
        elif action == "clear_input":
            input_buffer.reset()          # idle: Esc clears a half-typed line
        else:
            return
        event.app.invalidate()

    @kb.add("s-tab")
    def _(event: Any) -> None:
        tui.mode_idx = (tui.mode_idx + 1) % len(MODES)
        event.app.invalidate()

    # Accept the next-prompt ghost text. Tab (menu closed) or → at end-of-line.
    _has_ghost = Condition(lambda: bool(
        input_buffer.suggestion and input_buffer.suggestion.text
        and not _menu_options()))

    @kb.add("tab", filter=_has_ghost & ~_picker_open)
    def _(event: Any) -> None:
        input_buffer.insert_text(input_buffer.suggestion.text)
        event.app.invalidate()

    @kb.add("right", filter=_has_ghost & ~_picker_open)
    def _(event: Any) -> None:
        if input_buffer.cursor_position == len(input_buffer.text):
            input_buffer.insert_text(input_buffer.suggestion.text)
        else:
            input_buffer.cursor_right()
        event.app.invalidate()

    @kb.add("c-x", "c-e")
    def _(event: Any) -> None:
        # Compose a long / multi-line prompt in $EDITOR (like the shell's C-x C-e).
        try:
            input_buffer.open_in_editor(event.app)
        except Exception:  # noqa: BLE001 — no editor / spawn failed: ignore
            pass

    @kb.add("c-v")
    def _(event: Any) -> None:
        # Ctrl+V: attach an image (or copied file) from the system clipboard to
        # the next message — [Image #N] placeholder in the line, real content
        # block flushed on submit. (Was classic-REPL-only; now both UIs.)
        placeholder = tui._capture_clipboard_attachment()
        if placeholder:
            input_buffer.insert_text(placeholder + " ")
            event.app.create_background_task(_announce(
                f"attached {placeholder} — sends with your next message"))
        else:
            event.app.create_background_task(_announce(
                "no image or file on the clipboard"))
        event.app.invalidate()

    input_window = Window(
        BufferControl(
            buffer=input_buffer,
            # Mask the line (••••) ONLY while pasting an API key for a locked
            # provider — normal chat input stays visible.
            input_processors=[
                ConditionalProcessor(
                    PasswordProcessor(),
                    Condition(lambda: state.get("awaiting_key") is not None),
                ),
                AppendAutoSuggestion(),  # dim next-prompt ghost text
            ],
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
            # Live task checklist — pinned under the spinner while working
            # (Claude Code's ⎿ □/✔ block); height 0 when idle.
            Window(FormattedTextControl(live_todos_ft), height=_live_todos_height),
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

    async def _mcp_startup() -> None:
        # Connect configured MCP servers in the background so a slow server
        # never delays the first prompt. Tools land mid-session via
        # _connect_mcp (which also folds them into the live agent's registry).
        try:
            summary = await tui._connect_mcp()
        except Exception:  # noqa: BLE001 — MCP must never take the UI down
            return
        if summary:
            await _announce(f"mcp: {summary}")

    # Route HTTP-retry notices into the spinner line instead of raw log lines
    # (which tear through the prompt frame). The note self-expires.
    from . import retry as _retry  # noqa: PLC0415

    def _on_retry(info: dict) -> None:
        note = (f"⚠ {info['reason']} — retry {info['attempt']}/{info['attempts']} "
                f"in {info['sleep_s']:.1f}s")
        state["retry_note"] = (note, time.monotonic() + info["sleep_s"] + 15.0)
        try:
            get_app().invalidate()
        except Exception:  # noqa: BLE001
            pass

    _retry.notify = _on_retry

    anim = asyncio.ensure_future(_animate())
    mcp_boot = asyncio.ensure_future(_mcp_startup())
    try:
        await app.run_async()
    finally:
        _retry.notify = None
        anim.cancel()
        mcp_boot.cancel()
        for l in (state.get("loops") or {}).values():  # stop /loop timers
            l["stopped"].set()
            t = l.get("task")
            if t is not None:
                t.cancel()
        for w in (state.get("watches") or {}).values():  # stop /watch sentinels
            w["stopped"].set()
            t = w.get("task")
            if t is not None:
                t.cancel()
        try:
            await tui._close_mcp()
        except Exception:  # noqa: BLE001
            pass
        if tui.agent is not None:
            await tui.agent.aclose()
    tui.console.print("[ansibrightblack]bye 👋[/]")
    return 0
