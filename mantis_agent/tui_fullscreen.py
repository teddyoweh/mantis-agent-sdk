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


_MENTION_SCAN_CAP = 6000


def _walk_mention_files(root: str) -> list[str]:
    """All files under ``root`` (rel paths), skipping VCS/build dirs and
    dotfiles and capping the scan so a huge repo can't stall the walk. Kept
    separate from ranking so callers can cache this once and re-rank in-memory
    per keystroke (the walk is the expensive part) — see ``_rank_mentions``."""
    import os  # noqa: PLC0415

    rels: list[str] = []
    scanned = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _MENTION_IGNORE and not d.startswith(".")]
        for f in fns:
            if f.startswith("."):
                continue
            scanned += 1
            rels.append(os.path.relpath(os.path.join(dp, f), root))
            if scanned > _MENTION_SCAN_CAP:
                break
        if scanned > _MENTION_SCAN_CAP:
            break
    return rels


def _rank_mentions(rels: list[str], partial: str, *, limit: int = 8) -> list[str]:
    """Filter ``rels`` by ``partial`` (substring, case-insensitive) and rank
    basename-prefix-first then shortest path. Cheap, in-memory — safe per
    keystroke."""
    import os  # noqa: PLC0415

    pl = partial.lower()
    hits = [rel for rel in rels if not pl or pl in rel.lower()]

    def _key(rel: str) -> tuple:
        base = os.path.basename(rel).lower()
        return (0 if base.startswith(pl) else (1 if pl in base else 2), len(rel))

    hits.sort(key=_key)
    return hits[:limit]


def find_file_mentions(partial: str, root: str, *, limit: int = 8) -> list[str]:
    """Files under ``root`` matching ``partial`` (substring, case-insensitive),
    ranked basename-prefix-first then shortest path. Bounded (skips VCS/build
    dirs and dotfiles, caps the scan) so it stays snappy per keystroke on big
    repos. Powers the ``@``-file-mention completer."""
    return _rank_mentions(_walk_mention_files(root), partial, limit=limit)


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
    # Crash recovery: if the LAST session died mid-flight, offer its resume
    # line; then mark THIS session as in-flight (flipped to clean on exit).
    try:
        _hint = tui._check_unclean_exit()
        if _hint:
            tui.console.print(f"[ansiyellow]⚠[/] [ansibrightblack]{_hint}[/]\n")
        tui._mark_session_state(clean=False)
    except Exception:  # noqa: BLE001
        pass

    state: dict[str, Any] = {
        "working": False, "started": 0.0, "word": "", "frame": 0, "task": None,
        "slash_sel": 0, "pending_perm": None, "picking_model": None,
        "awaiting_key": None, "picking_effort": None,
        "pending_question": None, "ctx_tokens": 0, "session_cost": 0.0,
        "agent_inspector": None, "workflows": None,
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
        # Cache the repo walk per cwd — it is called several times per frame
        # while an @-mention token is active (menu_ft, _menu_height, the
        # _menu_open filter, _has_ghost), and an uncached os.walk each time
        # lagged input on large trees. Only the cheap in-memory rank runs
        # per keystroke; the walk happens once per working directory.
        import os  # noqa: PLC0415
        cwd = os.getcwd()
        cache = state.get("mention_files")
        if cache is None or cache[0] != cwd:
            cache = (cwd, _walk_mention_files(cwd))
            state["mention_files"] = cache
        return _rank_mentions(cache[1], partial)

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

    # Short, lowercase tab names for the picker tab bar. Provider/model
    # families are intentionally distinct: Claude is not an OpenAI model.
    _TAB_LABELS = {"anthropic": "claude", "moonshot": "kimi"}

    def _tab_label(provider_id: str) -> str:
        return _TAB_LABELS.get(provider_id, provider_id)

    def _picker_groups() -> list[dict]:
        """The model picker's groups in display order — active backend, local
        Ollama, then every provider (enabled first, disabled + 🔒 after). Each is
        ``{tab, tablabel, header, enabled, rows}`` where ``rows`` are the model
        items. Built once when the picker opens; the tab bar reads its counts and
        per-tab filtering slices by ``tab``. No text filter here — that's applied
        later in ``_items_for`` so counts stay stable while you type."""
        from .tui import _is_chat_model, _is_open_weight  # noqa: PLC0415
        from . import catalog  # noqa: PLC0415

        from .capabilities import lookup_model  # noqa: PLC0415

        def ctxlab(m: str) -> str:
            cw = lookup_model(m).context_window
            return f"{cw // 1000}k" if cw >= 1000 else ""

        groups: list[dict] = []
        active_all = _chat_models()
        active_set = set(active_all)
        if active_all:
            back = (tui.backend or "").rstrip("/")
            if "localhost" in back or "127.0.0.1" in back:
                where = "Ollama (local)"
            else:
                # Show the friendly provider name when the backend is a known one.
                where = next((p.label for p in catalog.CATALOG
                              if p.base_url.rstrip("/") == back), back or "backend")
            groups.append({
                "tab": "active", "tablabel": "active", "tab_hidden": True,
                "header": f"● active · {where}", "enabled": True,
                "rows": [{"kind": "model", "model": m, "enabled": True,
                          "provider_id": None, "ctx": ctxlab(m)} for m in active_all]})
        # Local Ollama models (FREE, open-source) — always shown when Ollama is
        # running, even while the active backend is a hosted API. Selecting one
        # switches the backend to localhost with no key.
        back_now = (tui.backend or "").rstrip("/")
        if "localhost" not in back_now and "127.0.0.1" not in back_now:
            from . import paths as _pp  # noqa: PLC0415
            local = [m for m in _ollama_models()
                     if _is_chat_model(m) and m not in active_set]
            if local:
                groups.append({
                    "tab": "local", "tablabel": "free.local",
                    "header": "Ollama · local · free", "enabled": True,
                    "rows": [{"kind": "model", "model": m, "enabled": True,
                              "provider_id": None, "ctx": ctxlab(m),
                              "backend": _pp.ollama_base_url()} for m in local]})
        open_rows: list[dict] = []
        seen_open: set[str] = set()
        prov_groups: list[dict] = []
        try:
            for g in catalog.grouped_provider_models():
                models = [m for m in g["models"]
                          if _is_chat_model(m) and m not in active_set]
                if not models:
                    continue
                rows = [{"kind": "model", "model": m, "enabled": g["enabled"],
                         "provider_id": g["provider_id"], "ctx": ctxlab(m)}
                        for m in models]
                for row in rows:
                    m = row["model"]
                    if not _is_open_weight(m):
                        continue
                    canon = m.rsplit("/", 1)[-1].lower()
                    if canon in seen_open:
                        continue
                    seen_open.add(canon)
                    open_rows.append(dict(row))
                tag = "" if g["enabled"] else "  · not enabled"
                prov_groups.append({
                    "tab": g["provider_id"], "tablabel": _tab_label(g["provider_id"]),
                    "header": f"{g['label']}{tag}", "enabled": g["enabled"],
                    "rows": rows})
        except Exception:  # noqa: BLE001
            pass
        if open_rows:
            groups.append({
                "tab": "open", "tablabel": "open",
                "header": "Open-weight · self-hostable", "enabled": True,
                "rows": open_rows})
        # Preferred tab order up front; any provider not listed keeps its
        # (enabled-first) catalog order after these. Stable sort makes it so, so
        # the bar reads: all · free.local · glm · qwen · kimi · openai · claude ·
        # gemini · <the rest>.
        _PREF = ("active", "local", "open", "openai", "anthropic", "moonshot", "glm", "qwen", "gemini")
        _rank = {pid: i for i, pid in enumerate(_PREF)}
        prov_groups.sort(key=lambda g: _rank.get(g["tab"], len(_PREF)))
        groups.extend(prov_groups)
        return groups

    def _items_for(groups: list[dict], flt: str, tab: str) -> list[dict]:
        """Flatten ``groups`` into header+model rows for the active ``tab``
        ("all" keeps every group), applying the case-insensitive text filter and
        dropping groups left with no surviving models."""
        fl = (flt or "").lower().strip()
        items: list[dict] = []
        for g in groups:
            if tab != "all" and g["tab"] != tab:
                continue
            rows = [r for r in g["rows"] if not fl or fl in r["model"].lower()]
            if not rows:
                continue
            items.append({"kind": "header", "label": g["header"]})
            items.extend(rows)
        return items

    def _build_picker_items(flt: str) -> list[dict]:
        # Back-compat thin wrapper: the whole catalog, no tab narrowing.
        return _items_for(_picker_groups(), flt, "all")

    def _open_model_picker(flt: str = "") -> None:
        groups = _picker_groups()
        tabs = [{"tab": "all", "label": "all",
                 "count": sum(len(g["rows"]) for g in groups)}]
        for g in groups:
            if g.get("tab_hidden"):  # e.g. the ● active group — shown only in "all"
                continue
            tabs.append({"tab": g["tab"], "label": g["tablabel"],
                         "count": len(g["rows"])})
        items = _items_for(groups, flt, "all")
        sel = next((i for i, it in enumerate(items)
                    if it["kind"] == "model" and it["model"] == tui.model), None)
        if sel is None:
            sel = next((i for i, it in enumerate(items) if it["kind"] == "model"), 0)
        state["picking_model"] = {"items": items, "sel": sel, "filter": flt,
                                  "groups": groups, "tabs": tabs, "tab": "all"}
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
        mode = p.get("mode")
        if mode == "session":
            items = _build_session_items(p.get("filter", ""))
        elif mode == "rewind":
            items = _build_rewind_items(p.get("filter", ""))
        else:  # model picker — reslice cached groups within the active tab
            items = _items_for(p.get("groups", []), p.get("filter", ""),
                               p.get("tab", "all"))
        p["items"] = items
        if not (0 <= p["sel"] < len(items) and items[p["sel"]]["kind"] in _SELECTABLE_KINDS):
            p["sel"] = next((i for i, it in enumerate(items)
                             if it["kind"] in _SELECTABLE_KINDS), 0)

    def effort_ft() -> Any:
        p = state.get("picking_effort")
        if not p:
            return ANSI("")
        items, sel = p["items"], p["sel"]
        rows = [f"{_GREEN}Select effort{_RESET}  {_GREY}↑/↓ · enter · esc{_RESET}"]
        for i, v in enumerate(items):
            if i == sel:
                rows.append(f"\033[30;48;5;113m ❯ {v} \033[0m")
            else:
                rows.append(f"  {_DIM}{v}{_RESET}")
        return ANSI("\n".join(rows))

    def picker_ft() -> Any:
        p = state.get("picking_model")
        if not p:
            return ANSI("")
        items, sel = p["items"], p["sel"]
        n = len(items)
        lo = max(0, min(sel - _PICKER_ROWS // 2, n - _PICKER_ROWS))
        flt = p.get("filter", "")
        title = {"session": "Resume a conversation",
                 "rewind": "Rewind to"}.get(p.get("mode"), "Select a model")
        tabs = p.get("tabs")
        nav = "↑/↓ · ←/→ tabs · type to filter · enter · esc" if tabs \
            else "↑/↓ · type to filter · enter · esc"
        _hdr = f"{_GREEN}{title}{_RESET}  {_GREY}({nav}){_RESET}"
        if flt:
            _hdr += f"   {_DIM}filter: {flt}{_RESET}"

        def _ctx(it: dict) -> str:
            c = it.get("ctx")  # precomputed in _build_picker_items
            return f"  {c}" if c else ""

        rows = [_hdr]
        # Tab bar — one chip per provider/family (all · openai · claude · …) with
        # a live model count; the active tab is highlighted. ←/→ switches tabs.
        if tabs:
            curtab = p.get("tab", "all")
            segs = []
            for t in tabs:
                lab = f"{t['label']}·{t['count']}"
                if t["tab"] == curtab:
                    segs.append(f"\033[30;48;5;113m {lab} \033[0m")
                else:
                    segs.append(f"{_DIM}{lab}{_RESET}")
            rows.append("  ".join(segs))
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
        extra = 1 if p.get("tabs") else 0  # tab bar row (model picker only)
        return Dimension.exact(1 + extra + min(len(p["items"]), _PICKER_ROWS))

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

        # Switching rebuilds the Agent with a fresh provider, which opens a new
        # httpx client + connection pool. Retire the OUTGOING provider's client
        # so its sockets/pool don't leak on every /model switch. aclose is async
        # and we're inside the running TUI app, so fire-and-forget on the loop.
        def _retire(old: Any, new: Any) -> None:
            if old is None or old is new:
                return
            aclose = getattr(old, "aclose", None)
            if aclose is None:
                return
            async def _run() -> None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
            try:
                asyncio.get_running_loop().create_task(_run())
            except RuntimeError:
                pass  # no running loop (shouldn't happen in the TUI)

        # Sanitize first: a picker filter / pasted id can carry whitespace or a
        # trailing newline, and a blank id must never blow away the active model.
        model_id = (model_id or "").strip()
        if not model_id:
            return
        if backend:
            # Explicit backend from the picker item (a local Ollama model while
            # on a hosted API): point straight at it, no key needed.
            _old = tui.agent.provider if tui.agent is not None else None
            tui.backend, tui.api_key = backend, None
            tui.model = model_id
            tui.agent = tui._build_agent()
            _retire(_old, tui.agent.provider if tui.agent is not None else None)
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
        _old = tui.agent.provider if tui.agent is not None else None
        tui.model = model_id
        tui.agent = tui._build_agent()
        _retire(_old, tui.agent.provider if tui.agent is not None else None)
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
        knobs = []
        if getattr(tui, "effort", None):
            knobs.append(f"effort={tui.effort}")
        if getattr(tui, "verbosity", None):
            knobs.append(f"verb={tui.verbosity}")
        if getattr(tui, "reasoning_mode", None):
            knobs.append(f"reasoning={tui.reasoning_mode}")
        knob_seg = f"   {' '.join(knobs)}" if knobs else ""
        return ANSI(f"\033[{col}m{left}{_RESET}   {_GREY}{tui.model}{_RESET}{knob_seg}{ctx_seg}")

    _LIVE_TODO_ROWS = 8

    def _live_agent_items() -> list[tuple[int, dict]]:
        return sorted((getattr(tui, "_live_subagents", None) or {}).items())

    def _open_agent_inspector() -> None:
        items = _live_agent_items()
        if not items:
            state["agent_inspector"] = None
            return
        cur = state.get("agent_inspector") or {}
        sel = min(max(int(cur.get("sel", 0)), 0), len(items) - 1)
        # "detail" = focused single-agent view (Enter drills in, ← goes back).
        state["agent_inspector"] = {"sel": sel, "detail": bool(cur.get("detail"))}

    def _agent_inspector_ft() -> Any:
        panel = state.get("agent_inspector")
        if not panel:
            return ANSI("")
        items = _live_agent_items()
        if not items:
            state["agent_inspector"] = None
            return ANSI("")
        from .tui import ellipsize  # noqa: PLC0415
        width = shutil.get_terminal_size((80, 24)).columns
        sel = int(panel.get("sel", 0)) % len(items)
        now = time.monotonic()
        rid, rec = items[sel]

        # -- detail mode: one agent, its full recent activity ----------------
        if panel.get("detail"):
            el = int(now - rec.get("started", now))
            last = rec.get("last_event") or "starting"
            rows = [f"\033[36mAgent #{rid} · {rec.get('type', 'task')}{_RESET} "
                    f"{_GREY}← back · Esc close{_RESET}"]
            desc = rec.get("desc", "")
            if desc:
                rows.append(f"{_DIM}{ellipsize(desc, max(width - 2, 20))}{_RESET}")
            rows.append(f"{_GREY}status: {rec.get('tools', 0)} tools · {el}s · {last}{_RESET}")
            events = list(rec.get("events") or [])[-10:]
            if events:
                rows.append(f"{_GREY}recent activity{_RESET}")
                for ts, text in events:
                    rows.append(f"{_GREY}  - {max(0, int(now - ts))}s ago · "
                                f"{ellipsize(text, max(width - 10, 20))}{_RESET}")
            else:
                rows.append(f"{_DIM}  (no tool activity yet){_RESET}")
            return ANSI("\n".join(rows))

        # -- list mode: all agents, recent activity of the selected one ------
        rows = [f"\033[36mLive agents{_RESET} {_GREY}↑/↓ move · Enter inspect · ← / Esc close{_RESET}"]
        for i, (r_id, r_rec) in enumerate(items[:12]):
            el = int(now - r_rec.get("started", now))
            mark = "❯" if i == sel else " "
            last = r_rec.get("last_event") or "starting"
            body = ellipsize(
                f"{mark} #{r_id} {r_rec.get('type', 'task')} · {r_rec.get('tools', 0)} tools · "
                f"{el}s · {last} · {r_rec.get('desc', '')}",
                max(width - 2, 20),
            )
            rows.append(f"{_GREEN if i == sel else _DIM}{body}{_RESET}")
        events = list(rec.get("events") or [])[-5:]
        if events:
            rows.append(f"{_GREY}recent #{rid}{_RESET}")
            for ts, text in events:
                rows.append(f"{_GREY}  - {max(0, int(now - ts))}s ago · "
                            f"{ellipsize(text, max(width - 10, 20))}{_RESET}")
        return ANSI("\n".join(rows))

    def _agent_inspector_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        panel = state.get("agent_inspector")
        if not panel:
            return Dimension.exact(0)
        items = _live_agent_items()
        if not items:
            return Dimension.exact(0)
        sel = int(panel.get("sel", 0)) % len(items)
        rec = items[sel][1]
        if panel.get("detail"):
            events = list(rec.get("events") or [])[-10:]
            n = 1                                   # header
            n += 1 if rec.get("desc") else 0        # desc line
            n += 1                                   # status line
            n += (1 + len(events)) if events else 1  # activity block / placeholder
            return Dimension.exact(n)
        events = list(rec.get("events") or [])[-5:]
        return Dimension.exact(1 + min(len(items), 12) + (1 + len(events) if events else 0))

    # -- /workflows overlay (structured multi-agent runs) --------------------
    # A two-pane single-Window overlay: LEFT a phase rail (glyph · title ·
    # done/total) for the selected run, RIGHT a scrolling list of agent rows
    # rendered with the shared ``workflow_view.format_agent_row`` so it matches
    # the classic-REPL viewer exactly. Enter drills into a run's agent detail;
    # x/p/s drive the live handle (stop/pause/save). Live runs are sourced from
    # ``tui._workflows`` (data) + ``tui._workflow_handles`` (live control),
    # populated by the workflow-engine integration.
    _WF_ROWS = 12       # visible agent rows in the right pane's viewport
    _WF_LEFT_W = 30     # phase-rail column width

    def _wf_pairs() -> list[tuple[Any, Any]]:
        """Flattened, input-ordered ``(run, agent)`` pairs across all runs."""
        pairs: list[tuple[Any, Any]] = []
        for run in list(getattr(tui, "_workflows", []) or []):
            try:
                for ag in run.all_agents():
                    pairs.append((run, ag))
            except Exception:  # noqa: BLE001 — a malformed run never breaks the list
                pass
        return pairs

    def _open_workflows() -> None:
        state["workflows"] = {"sel": 0, "detail": False}
        get_app().invalidate()

    def _wf_selected_run() -> Any:
        p = state.get("workflows")
        pairs = _wf_pairs()
        if not p or not pairs:
            return None
        sel = min(max(int(p.get("sel", 0)), 0), len(pairs) - 1)
        return pairs[sel][0]

    def _wf_handle(run: Any) -> Any:
        return (getattr(tui, "_workflow_handles", {}) or {}).get(getattr(run, "id", ""))

    def _wf_detail_lines(run: Any, ag: Any, now: float) -> list[str]:
        from .workflow_view import format_duration, format_number, total_tokens  # noqa: PLC0415
        label = getattr(ag, "label", "") or getattr(ag, "id", "?")
        lines = [f"{_GREEN}Workflow agent · {label}{_RESET}  {_GREY}← / esc back{_RESET}",
                 f"{_DIM}workflow: {getattr(run, 'name', '?')}{_RESET}"]
        phase = getattr(ag, "phase", "") or ""
        atype = getattr(ag, "agent_type", "") or ""
        meta = " · ".join(x for x in (phase, atype) if x)
        if meta:
            lines.append(f"{_DIM}phase: {meta}{_RESET}")
        dur = format_duration(ag.elapsed_ms(now)) if hasattr(ag, "elapsed_ms") else "?"
        lines.append(f"{_DIM}status: {getattr(ag, 'status', '?')} · {dur}{_RESET}")
        tok = total_tokens(getattr(ag, "usage", None))
        counts = (f"{getattr(ag, 'turns', 0)} turns · {getattr(ag, 'tool_count', 0)} "
                  f"tools · {format_number(tok)} tok")
        cost = getattr(ag, "cost_usd", 0.0) or 0.0
        if cost:
            counts += f" · ${cost:.4f}" if cost < 0.01 else f" · ${cost:.2f}"
        lines.append(f"{_DIM}progress: {counts}{_RESET}")
        model = getattr(ag, "model", "") or ""
        if model:
            lines.append(f"{_DIM}model: {model}{_RESET}")
        acts = list(getattr(ag, "recent_activities", []) or [])[-5:]
        if acts:
            lines.append(f"{_GREY}recent activity{_RESET}")
            lines += [f"{_GREY}  - {a}{_RESET}" for a in acts]
        err = getattr(ag, "error", None)
        if err:
            lines.append(f"\033[38;5;203merror: {str(err)[:200]}{_RESET}")
        return lines

    def _workflows_lines() -> list[str]:
        """All overlay lines (header + body + footer) for the current state.
        Both ``_workflows_ft`` and ``_workflows_height`` call this so their row
        counts can never drift apart."""
        p = state.get("workflows")
        if p is None:
            return []
        from .workflow import default_clock  # noqa: PLC0415
        from .workflow_view import (  # noqa: PLC0415
            _truncate_visible,
            _visible_len,
            format_agent_row,
            status_glyph,
        )
        width = shutil.get_terminal_size((80, 24)).columns
        pairs = _wf_pairs()
        if not pairs:
            return [f"{_GREEN}Workflows{_RESET}  {_GREY}(esc to close){_RESET}",
                    f"{_DIM}no active workflows yet — /swarm and the pipeline/parallel "
                    f"helpers register runs here{_RESET}"]
        sel = min(max(int(p.get("sel", 0)), 0), len(pairs) - 1)
        p["sel"] = sel
        now = default_clock()
        frame = state.get("frame", 0)
        if p.get("detail"):
            run, ag = pairs[sel]
            return [_truncate_visible(ln, width) for ln in _wf_detail_lines(run, ag, now)]
        # List view: left phase rail (selected run) + right scrolling agent rows.
        run = pairs[sel][0]
        left: list[str] = []
        for ph in getattr(run, "phases", []) or []:
            g = status_glyph(getattr(ph, "status", "queued"), color=True)
            ags = getattr(ph, "agents", []) or []
            done = sum(1 for a in ags if getattr(a, "status", "") in ("done", "cancelled"))
            left.append(f"{g} {getattr(ph, 'title', '?')} {_DIM}{done}/{len(ags)}{_RESET}")
        n = len(pairs)
        lo = max(0, min(sel - _WF_ROWS // 2, n - _WF_ROWS)) if n > _WF_ROWS else 0
        right_w = max(20, width - _WF_LEFT_W - 2)
        right: list[str] = []
        for i in range(lo, min(lo + _WF_ROWS, n)):
            right.append(format_agent_row(
                pairs[i][1], selected=(i == sel), width=right_w, now=now,
                frame=frame, show_model=False, color=True))

        def _padv(s: str, w: int) -> str:
            vl = _visible_len(s)
            return _truncate_visible(s, w) if vl >= w else s + " " * (w - vl)

        header = f"{_GREEN}Workflows{_RESET} {_GREY}· {getattr(run, 'name', '?')}{_RESET}"
        rows = [header]
        for r in range(max(len(left), len(right))):
            lft = left[r] if r < len(left) else ""
            rgt = right[r] if r < len(right) else ""
            rows.append(_padv(lft, _WF_LEFT_W) + "  " + rgt)
        rows.append(f"{_GREY}↑↓ select · enter/→ inspect · ←/esc back · "
                    f"x stop · p pause · s save{_RESET}")
        return rows

    def _workflows_ft() -> Any:
        lines = _workflows_lines()
        return ANSI("\n".join(lines)) if lines else ANSI("")

    def _workflows_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        n = len(_workflows_lines())
        return Dimension.exact(n) if n else Dimension.exact(0)

    def _wf_stop() -> None:
        run = _wf_selected_run()
        if run is None:
            return
        handle = _wf_handle(run)
        name = getattr(run, "name", "workflow")
        fn = getattr(handle, "stop", None) if handle is not None else None
        if not callable(fn):
            get_app().create_background_task(_announce(f"{name} is not live — nothing to stop"))
            return
        try:
            fn()
            get_app().create_background_task(_announce(f"⛌ stopping {name}"))
        except Exception as e:  # noqa: BLE001
            get_app().create_background_task(_announce(f"could not stop {name}: {e}"))
        get_app().invalidate()

    def _wf_toggle_pause() -> None:
        run = _wf_selected_run()
        if run is None:
            return
        handle = _wf_handle(run)
        name = getattr(run, "name", "workflow")
        if handle is None:
            get_app().create_background_task(_announce(f"{name} is not live — nothing to pause"))
            return
        try:
            if getattr(handle, "_paused", False):
                handle.resume()
                get_app().create_background_task(_announce(f"▶ resumed {name}"))
            else:
                handle.pause()
                get_app().create_background_task(_announce(f"⏸ paused {name}"))
        except Exception as e:  # noqa: BLE001
            get_app().create_background_task(_announce(f"could not pause {name}: {e}"))
        get_app().invalidate()

    def _wf_save() -> None:
        run = _wf_selected_run()
        if run is None:
            return
        handle = _wf_handle(run)
        fn = getattr(handle, "save", None) if handle is not None else None
        if callable(fn):
            try:
                path = fn()
                get_app().create_background_task(_announce(f"saved → {path}"))
            except Exception as e:  # noqa: BLE001
                get_app().create_background_task(_announce(f"could not save: {e}"))
        elif hasattr(tui, "_save_workflow"):
            get_app().create_background_task(_print(lambda r=run: tui._save_workflow(r)))
        else:
            get_app().create_background_task(_announce("nothing to save"))

    def _live_subagent_rows(width: int) -> list[str]:
        """'⎿ ◇ explore · 6 tools · 42s · tool grep — CPU analysis' rows for
        in-flight task-tool runs, capped at 10."""
        from .tui import ellipsize  # noqa: PLC0415
        rows: list[str] = []
        subs = getattr(tui, "_live_subagents", None) or {}
        for i, (rid, s) in enumerate(list(subs.items())[:10]):
            el = int(time.monotonic() - s.get("started", time.monotonic()))
            desc = f" — {s['desc']}" if s.get("desc") else ""
            last = s.get("last_event") or ""
            activity = f" · {last}" if last and last != "starting" else ""
            body = ellipsize(
                f"◇ #{rid} {s.get('type', '?')} · {s.get('tools', 0)} tools · {el}s{activity}{desc}",
                max(width - 6, 20))
            branch = "  ⎿ " if (i == 0 and not tui.todos) else "    "
            rows.append(f"{_DIM}{branch}\033[36m{body}\033[0m{_RESET}")
        return rows

    def live_todos_ft() -> Any:
        """The checklist + live subagent rows pinned under the spinner while
        the agent works. Collapses to nothing when idle."""
        subs = getattr(tui, "_live_subagents", None) or {}
        if not (state["working"] and (tui.todos or subs)):
            return ANSI("")
        from .tui import format_live_todo_rows  # noqa: PLC0415
        width = shutil.get_terminal_size((80, 24)).columns
        rows: list[str] = []
        if tui.todos:
            rows += format_live_todo_rows(tui.todos, width, max_rows=_LIVE_TODO_ROWS)
        # When the inspector or the /workflows overlay is open it already renders
        # the agent list (richer), so don't duplicate the rows down here.
        if state.get("agent_inspector") is None and state.get("workflows") is None:
            rows += _live_subagent_rows(width)
            if subs:  # hint that ↓ steps into the live subagent list
                rows.append(f"{_GREY}    ↓ to inspect subagents{_RESET}")
        if not rows:
            return ANSI("")
        return ANSI("\n".join(rows))

    def _live_todos_height() -> Any:
        from prompt_toolkit.layout.dimension import Dimension  # noqa: PLC0415
        subs = getattr(tui, "_live_subagents", None) or {}
        if not (state["working"] and (tui.todos or subs)):
            return Dimension.exact(0)
        n = min(len(tui.todos), _LIVE_TODO_ROWS) if tui.todos else 0
        n += (1 if len(tui.todos) > _LIVE_TODO_ROWS else 0)
        # Agent rows + hint are hidden while the inspector or /workflows overlay
        # is open (no dup).
        if state.get("agent_inspector") is None and state.get("workflows") is None:
            n += min(len(subs), 10)
            if subs:
                n += 1  # the "↓ inspect" hint line
        return Dimension.exact(n)

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
        elif text.startswith("/") and state["working"] and text.split(maxsplit=1)[0] in {
                "/agents", "/jobs", "/job", "/cost", "/status", "/effort", "/workflows"}:
            if text.split(maxsplit=1)[0] == "/agents" and "live" in text.split()[1:2]:
                _open_agent_inspector()
                get_app().invalidate()
            try:
                handled = await _slash(text)
            except Exception as e:  # noqa: BLE001
                await _announce(f"error: {str(e)}")
                handled = True
            if handled:
                get_app().invalidate()
                return
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
        # Reset the marker buffer at turn start so a GOAL COMPLETE/BLOCKED line
        # from a *previous* turn can never leak into this turn's autopilot check.
        state["last_text"] = ""
        tui._flush_job_context_backlog()
        tui._turn_active = True
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
                # esc PAUSES autopilot (keeps the goal + cycle state) rather than
                # discarding it — /goal resume picks up where it left off.
                state["goal"]["paused"] = True
                goal_note = " · autopilot paused (/goal resume to continue)"
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
            tui._turn_active = False
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
                _spawn_handle(nxt)
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

    def _looks_like_turn(text: str) -> bool:
        """True when ``text`` will run a model turn (not a ``!cmd``, ``#note``
        or slash command). Used to claim ``state['working']`` synchronously at
        spawn time — see ``_spawn_handle``."""
        t = text.strip()
        if not t:
            return False
        if t.startswith("!") and len(t) > 1:
            return False
        if t.startswith("#") and t.lstrip("#").strip():
            return False
        if t.startswith("/"):
            # Most slash commands are pure UI (no model turn), but some EXPAND
            # into a canned prompt that runs as a turn (/init, /learn, custom
            # .mantis/commands/*, skill commands). Those must claim the slot
            # synchronously too, or a second submit/tick can race a concurrent
            # run_iter on the shared Agent. ``expand_slash_prompt`` returning
            # non-None is exactly "this will run a turn".
            from .tui import expand_slash_prompt  # noqa: PLC0415
            try:
                return expand_slash_prompt(t) is not None
            except Exception:  # noqa: BLE001 — treat unknown as non-turn
                return False
        return True

    def _spawn_handle(text: str) -> None:
        """Spawn ``_handle`` for a real turn, claiming ``state['working']``
        SYNCHRONOUSLY first. The flag was previously only set inside _handle
        after an ``await`` (past every submit-time guard), so two sources that
        both checked ``state['working']`` during that window (the Enter handler,
        the agi/goal/watch loops, or the queue drain) could each see False and
        start a SECOND concurrent ``run_iter`` on the shared Agent/messages.
        Commands (``!``/``#``/``/…``) don't run a turn, so they never claim it."""
        if _looks_like_turn(text):
            state["working"] = True
        get_app().create_background_task(_handle(text))

    async def _agi_loop(rec: dict) -> None:
        from .tui import agi_cycle_prompt  # noqa: PLC0415

        while not rec["stopped"].is_set():
            # Optional USD budget: stop the loop once the session cost crosses
            # rec['max_usd'] (unset → no cap), mirroring the goal loop.
            max_usd = rec.get("max_usd")
            if max_usd and state.get("session_cost", 0.0) >= max_usd:
                rec["stopped"].set()
                state["agi"] = None
                await _announce(f"∞ agi: cost budget ${max_usd:.2f} reached — stopped")
                return
            if not state["working"] and state.get("pending_perm") is None \
                    and state.get("pending_question") is None \
                    and state.get("picking_model") is None \
                    and state.get("awaiting_key") is None:
                rec["cycle"] += 1
                await _announce(f"∞ agi cycle {rec['cycle']} · {rec['seed'][:70]}")
                _spawn_handle(agi_cycle_prompt(rec["seed"], rec["cycle"]))
            await asyncio.sleep(rec["interval"])

    def _advance_goal() -> None:
        """The autopilot state machine, called after each idle turn end:
        work → (todos all done) → verify → (GOAL COMPLETE) → reflect → finish.

        Exits: the BLOCKED/COMPLETE markers (matched per-line on the last text
        block), the cycle cap, an optional USD budget, or a stagnation guard
        (no plan progress for 3 cycles → replan once, then block). esc PAUSES
        (``g['paused']``) rather than clearing, so a paused goal simply idles
        here until ``/goal resume``. Continuation is budget-aware: while still
        under the context window and making progress, a "keep working" nudge is
        injected so the model doesn't wrap up early (Claude autopilot parity)."""
        from .tui import (  # noqa: PLC0415
            BUDGET_NUDGE,
            detect_goal_marker,
            goal_continue_prompt,
            goal_reflect_prompt,
            goal_replan_prompt,
            goal_should_nudge,
            goal_todo_snapshot,
            goal_verify_prompt,
        )
        g = state.get("goal")
        if not g or state["working"] or g.get("paused"):
            return
        last = state.get("last_text") or ""
        marker = detect_goal_marker(last)

        def fire(prompt: str) -> None:
            _spawn_handle(prompt)

        def note(msg: str) -> None:
            get_app().create_background_task(_announce(msg))

        # --- hard exits: explicit block, cost budget, reflect-done ----------
        if marker == "BLOCKED":
            state["goal"] = None
            note("⦿ autopilot: goal blocked — stopped")
            return
        max_usd = g.get("max_usd")
        if max_usd and state.get("session_cost", 0.0) >= max_usd:
            state["goal"] = None
            note(f"⦿ autopilot: cost budget ${max_usd:.2f} reached — stopped")
            return
        if g.get("phase") == "reflect":
            state["goal"] = None
            note(f"⦿ autopilot: goal complete after {g['cycles']} cycles ✓")
            return
        if g.get("phase") == "verify" and marker == "COMPLETE":
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

        # --- stagnation snapshot: (#todos, #done, plan hash) per cycle ------
        snap = goal_todo_snapshot(todos)
        if snap == g.get("_snap"):
            g["_stall"] = g.get("_stall", 0) + 1
        else:
            g["_stall"] = 0
            g["_snap"] = snap
            g["_replanned"] = False  # progress resumed → allow future replans

        # Force a REAL plan: in the work phase with no todos, the model must
        # decompose before it keeps "working" against an empty checklist.
        if g.get("phase") == "work" and not todos:
            g["cycles"] += 1
            if g["cycles"] > g["max"]:
                state["goal"] = None
                note(f"⦿ autopilot: cycle cap ({g['max']}) reached — stopped.")
                return
            note("⦿ autopilot: no plan yet — forcing a concrete todo breakdown")
            fire(goal_replan_prompt(g["text"]))
            return

        # Stagnation guard: unchanged for 3 cycles → replan once; if the replan
        # also fails to move anything, declare the goal blocked and stop.
        if g.get("_stall", 0) >= 3:
            if g.get("_replanned"):
                state["goal"] = None
                note("⦿ autopilot: no progress after replan — blocked, stopping")
                return
            g["_replanned"] = True
            g["_stall"] = 0
            note("⦿ autopilot: stalled 3 cycles — replanning")
            fire(goal_replan_prompt(g["text"]))
            return

        g["cycles"] += 1
        if g["cycles"] > g["max"]:
            state["goal"] = None
            note(f"⦿ autopilot: cycle cap ({g['max']}) reached — stopped. "
                 "Re-issue /goal to keep going.")
            return
        g["phase"] = "work"

        # Budget-aware continuation nudge (REF query/tokenBudget): keep working
        # while under ~90% of the context window AND not diminishing (≥3 cycles
        # with two consecutive sub-500-token deltas). ctx growth is the proxy
        # for per-turn progress here.
        tok = state.get("ctx_tokens", 0)
        delta = tok - g.get("_prev_tok", 0)
        g["_prev_tok"] = tok
        g["_cont"] = g.get("_cont", 0) + 1
        nudge = goal_should_nudge(
            tokens=tok, window=_ctx_window(), cont=g["_cont"], delta=delta,
            prev_delta=g.get("_last_delta", 10 ** 9))
        g["_last_delta"] = delta
        if nudge:
            tui.messages.append(UserMessage(content=BUDGET_NUDGE, isMeta=True))
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
                state["working"] = True  # claim the slot before _handle awaits
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

    async def _run_swarm(n: int, task_text: str) -> None:
        """Drive swarm mode: N parallel general-purpose child agents in git
        worktrees, twin-judge ranks the diffs, winner's patch lands in the
        real tree. Runs as its own background task with the spinner up."""
        import os as _os  # noqa: PLC0415

        from .subagent import _GENERAL_SYSTEM, _extract_final_text  # noqa: PLC0415
        from .swarm import run_swarm  # noqa: PLC0415
        from .tools import ToolRegistry  # noqa: PLC0415
        from .agent import Agent as _Agent  # noqa: PLC0415
        from .builtin_tools import CODING_TOOLS  # noqa: PLC0415
        from .types import UserMessage as _UM  # noqa: PLC0415

        repo = _os.getcwd()
        # Swarm workers run CODING_TOOLS (real shell + filesystem writes) against
        # live worktrees, so they MUST be gated by the SAME permission layer as
        # the main agent — never unrestricted. Capture the parent context up front
        # and fail CLOSED (refuse to run) if there isn't one, rather than spawning
        # workers that could execute unapproved commands.
        parent_perms = tui.agent.permissions if tui.agent is not None else None
        if parent_perms is None:
            await _announce("⛬ swarm: refused — no permission context available "
                            "(fail closed)")
            return
        state.update(working=True, started=time.monotonic(),
                     word="Swarming", task=asyncio.current_task())
        get_app().invalidate()
        await _announce(f"⛬ swarm: {n} parallel attempts · isolated worktrees · "
                        "judge applies the best")

        async def runner(worktree: str, task: str, index: int) -> str:
            reg = ToolRegistry()
            reg.add(*CODING_TOOLS)
            child = _Agent(
                model=tui.model, provider=tui.agent.provider if tui.agent else None,
                system=(_GENERAL_SYSTEM +
                        f"\n\nCRITICAL: you are attempt #{index + 1} of a swarm. Work "
                        f"ONLY inside {worktree} — use ABSOLUTE paths under it for every "
                        "file and shell operation; never touch files outside it. Take "
                        "your own distinct approach to the task."),
                tools=reg, max_steps=40, include_recall=False, include_env=False,
                permissions=parent_perms,
            )
            msgs = [_UM(content=f"In the repo copy at {worktree}: {task}")]
            await child.run(msgs)
            tui._subagent_progress({"id": 90000 + index, "phase": "end"})
            return _extract_final_text(msgs)

        # Judge budget/caps — generous enough for the judge to actually reason
        # over each candidate's report + diff, not skim the first few hundred
        # bytes. Bumped from the old 1-step/200-token skim.
        judge_report_cap = 2000
        judge_diff_cap = 8000

        async def judge(viable) -> tuple[int, str]:
            briefs = []
            for c in viable:
                diff = c.diff[:judge_diff_cap]
                trunc = " …[diff truncated]" if len(c.diff) > judge_diff_cap else ""
                briefs.append(
                    f"=== ATTEMPT {c.index} ===\n"
                    f"files changed: {c.files_changed} · diff size: {len(c.diff)} bytes\n"
                    f"report: {c.report[:judge_report_cap]}\n"
                    f"diff:\n{diff}{trunc}")
            child = _Agent(
                model=tui.model, provider=tui.agent.provider if tui.agent else None,
                system=(
                    "You are an adversarial code judge. Competing attempts each tried "
                    "to solve the SAME task in an isolated worktree; you pick the one to "
                    "merge into the real tree. Your job is not to reward the nicest-"
                    "looking diff — it is to find how each one BREAKS.\n\n"
                    "For every attempt, reason briefly about failure: Does the diff "
                    "actually accomplish the task, or only pretend to? Does it handle "
                    "edge cases and error paths? Does it silently break existing "
                    "behavior, delete needed code, or touch files it shouldn't? A diff "
                    "that would fail the project's tests MUST NOT win, no matter how "
                    "clean it reads. Treat an empty, trivial, or off-task diff as "
                    "disqualified.\n\n"
                    "Rank on: (1) CORRECTNESS — solves the task, handles edge cases, "
                    "breaks nothing; then (2) SIMPLICITY — smallest change that is "
                    "still correct; then (3) COMPLETENESS. Think through the candidates, "
                    "then END with EXACTLY one final line and nothing after it:\n"
                    "WINNER: <index> — <one-line justification>"),
                tools=ToolRegistry(), max_steps=4, max_tokens=1200,
                include_recall=False, include_env=False,
            )
            msgs = [_UM(content=f"Task: {task_text}\n\n" + "\n\n".join(briefs))]
            await child.run(msgs)
            text = _extract_final_text(msgs)
            # Take the LAST WINNER: line — the model reasons first, decides last.
            matches = re.findall(r"WINNER:\s*(\d+)", text)
            for raw in reversed(matches):
                if any(c.index == int(raw) for c in viable):
                    return int(raw), text.strip()[-200:]
            return viable[0].index, f"judge unparseable; defaulted ({text[:80]})"

        try:
            for i in range(n):
                tui._subagent_progress({"id": 90000 + i, "phase": "start",
                                        "type": f"swarm#{i + 1}", "desc": task_text[:40]})
            result = await run_swarm(task_text, n, repo,
                                     agent_runner=runner, judge=judge)
            if result.winner is None:
                await _announce(f"⛬ swarm: {result.reason}")
            else:
                ok = "applied ✓" if result.applied else "NOT applied"
                await _announce(f"⛬ swarm: attempt #{result.winner + 1} wins — "
                                f"{ok} · {result.reason[:100]}")
                await _print(lambda: tui.console.print(
                    "[ansibrightblack]review with /diff · undo with git checkout[/]"))
        except Exception as e:  # noqa: BLE001
            await _announce(f"⛬ swarm failed: {e}")
        finally:
            for i in range(n):
                tui._subagent_progress({"id": 90000 + i, "phase": "end"})
            state.update(working=False, task=None)
            get_app().invalidate()

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
            parts = arg.strip().split(maxsplit=1)
            if parts and parts[0] in {"live", "watch", "inspect", "running"}:
                await _print(lambda: tui._cmd_live_agents(parts[1] if len(parts) > 1 else parts[0]))
            else:
                await _print(lambda: tui._show_agents())
            return True
        if cmd == "/effort":
            if arg:
                await _print(lambda: tui._cmd_knobs(arg))
            else:
                state["picking_effort"] = {
                    "items": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "off"],
                    "sel": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "off"].index(tui.effort or "off"),
                }
                get_app().invalidate()
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
        if cmd == "/swarm":
            sub = arg.strip()
            m_n = re.match(r"^(\d+)\s+(.+)$", sub, re.DOTALL)
            if not m_n:
                await _announce("usage: /swarm <2-8> <task> — N parallel attempts "
                                "in isolated worktrees; a judge applies the best")
                return True
            n, task_text = int(m_n.group(1)), m_n.group(2).strip()
            get_app().create_background_task(_run_swarm(n, task_text))
            return True
        if cmd == "/agi":
            from .tui import AGI_DEFAULT_INTERVAL_S, format_loop_interval, parse_loop_interval  # noqa: PLC0415
            sub = arg.strip()
            rec = state.get("agi")
            if not sub or sub == "status":
                if rec:
                    await _announce(f"∞ agi · cycle {rec['cycle']} · every "
                                    f"{format_loop_interval(rec['interval'])} · "
                                    f"{rec['seed'][:70]}")
                else:
                    await _announce("no /agi loop — /agi <seed> or /agi 5m <seed>")
                return True
            if sub == "stop":
                if rec:
                    rec["stopped"].set()
                    state["agi"] = None
                    await _announce("∞ agi stopped")
                else:
                    await _announce("no /agi loop")
                return True
            parts = sub.split(maxsplit=1)
            interval = AGI_DEFAULT_INTERVAL_S
            seed = sub
            if len(parts) == 2:
                parsed = parse_loop_interval(parts[0])
                if parsed is not None:
                    interval, seed = parsed, parts[1].strip()
            if not seed:
                await _announce("usage: /agi [interval] <seed>")
                return True
            from .tui import autopilot_conflict  # noqa: PLC0415
            conflict = autopilot_conflict(
                goal_active=bool(state.get("goal")),
                agi_active=bool(state.get("agi")), starting="agi")
            if conflict:
                await _announce(conflict)
                return True
            if rec:
                rec["stopped"].set()
            rec = {"seed": seed, "cycle": 0, "interval": interval,
                   "stopped": asyncio.Event()}
            state["agi"] = rec
            await _announce(f"∞ agi started · every {format_loop_interval(interval)} · stop with /agi stop")
            get_app().create_background_task(_agi_loop(rec))
            return True
        if cmd == "/goal":
            from .tui import GOAL_MAX_CYCLES, goal_kickoff_prompt  # noqa: PLC0415
            sub = arg.strip()
            g = state.get("goal")
            if not sub or sub == "status":
                if g:
                    paused = " · PAUSED (/goal resume)" if g.get("paused") else ""
                    await _announce(f"⦿ autopilot · cycle {g['cycles']}/{g['max']} · "
                                    f"{g['phase']}{paused} · {g['text'][:60]}")
                else:
                    await _announce("no active goal — /goal <what you want done>")
                return True
            if sub == "stop":
                state["goal"] = None
                await _announce("⦿ autopilot stopped" if g else "no active goal")
                return True
            if sub == "resume":
                if g and g.get("paused"):
                    g["paused"] = False
                    await _announce("⦿ autopilot resumed")
                    _advance_goal()
                elif g:
                    await _announce("⦿ autopilot already running")
                else:
                    await _announce("no paused goal to resume")
                return True
            from .tui import autopilot_conflict  # noqa: PLC0415
            conflict = autopilot_conflict(
                goal_active=bool(state.get("goal")),
                agi_active=bool(state.get("agi")), starting="goal")
            if conflict:
                await _announce(conflict)
                return True
            tui.todos.clear()  # fresh plan; the todo tool mutates this list in place
            state["goal"] = {"text": sub, "cycles": 0, "max": GOAL_MAX_CYCLES,
                             "phase": "work"}
            await _announce(f"⦿ autopilot engaged · up to {GOAL_MAX_CYCLES} cycles · "
                            "esc stops it")
            _spawn_handle(goal_kickoff_prompt(sub))
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
                    for lid, loop in loops.items():
                        await _announce(
                            f"loop #{lid} · every {format_loop_interval(loop['interval'])} · "
                            f"{loop['fires']} fires · {loop['prompt'][:60]}")
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
                state["working"] = True  # claim the slot before any await below
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
        if cmd == "/workflows":
            _open_workflows()
            return True
        if cmd == "/job":
            await _print(lambda: tui._cmd_job(arg))
            return True
        if cmd == "/jobs":
            watch = arg.strip().split()[:1]
            if watch and watch[0] in {"watch", "live", "log", "logs"}:
                await _print(lambda: tui._cmd_jobs(arg))

                async def _refresh_jobs() -> None:
                    for _ in range(120):
                        await asyncio.sleep(2)
                        if input_buffer.text.strip() != "/jobs watch":
                            return
                        await _print(lambda: tui._cmd_jobs(arg))

                get_app().create_background_task(_refresh_jobs())
            else:
                await _print(lambda: tui._cmd_jobs(arg))
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
                from rich.markup import escape as _esc  # noqa: PLC0415

                from .tui import all_slash_commands, search_help_lines  # noqa: PLC0415
                w, d = "white", "ansibrightblack"
                rows = search_help_lines(all_slash_commands(), arg)
                title = "commands" if not arg else f"commands matching {_esc(arg)!r}"
                tui.console.print(f"\n[bold]{title}[/]")
                if not rows:
                    tui.console.print(
                        f"  [{d}]no matches · try [white]/help[/] for all commands or "
                        f"type [white]/[/] to browse[/]"
                    )
                last_cat = None
                for cat, command, desc in rows:
                    label = cat if cat != last_cat else ""
                    last_cat = cat
                    tui.console.print(f"  [{d}]{label:<8}[/] [{w}]{command}[/]  [{d}]{desc}[/]")
                tui.console.print(
                    f"  [{d}]quit    [/] [{w}]/exit[/]  [{d}](or Ctrl+D · Ctrl+C when idle)[/]")
                tui.console.print(
                    f"  [{d}]keys    [/] [{d}]@file/@dir attaches its content · shift+tab cycles mode "
                    f"· esc/Ctrl+C interrupts a running reply · [white]/help <term>[/] searches[/]\n")
            await _print(_help)
            return True
        return False  # unknown → treat as a normal prompt

    kb = KeyBindings()

    from prompt_toolkit.filters import Condition  # noqa: PLC0415

    _menu_open = Condition(lambda: bool(_menu_options()))
    _perm_open = Condition(lambda: state.get("pending_perm") is not None)
    _picker_open = Condition(lambda: state.get("picking_model") is not None)
    _effort_open = Condition(lambda: state.get("picking_effort") is not None)
    _agent_open = Condition(lambda: state.get("agent_inspector") is not None)
    _workflows_open = Condition(lambda: state.get("workflows") is not None)

    def _can_enter_agents_fn() -> bool:
        """True when ↓ from the prompt should step into the live subagent list.

        Only fires when there are running subagents, the inspector isn't already
        open, no other overlay owns the screen, and the user isn't composing a
        line — so normal editing / history navigation is never hijacked."""
        if state.get("agent_inspector") is not None:
            return False
        if any(state.get(k) for k in (
                "pending_perm", "pending_question", "picking_model",
                "picking_effort", "workflows", "awaiting_key")):
            return False
        if input_buffer.text.strip() or _menu_options():
            return False
        return bool(_live_agent_items())

    _can_enter_agents = Condition(_can_enter_agents_fn)
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

    for _digit in "123456789":
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

    def _picker_tab(delta: int) -> None:
        # Cycle the active provider tab (←/→) and reslice the list to it. No-op
        # for the session/rewind pickers, which carry no tabs.
        p = state.get("picking_model")
        if not p or not p.get("tabs"):
            return
        keys = [t["tab"] for t in p["tabs"]]
        try:
            i = keys.index(p.get("tab", "all"))
        except ValueError:
            i = 0
        p["tab"] = keys[(i + delta) % len(keys)]
        p["items"] = _items_for(p.get("groups", []), p.get("filter", ""), p["tab"])
        p["sel"] = next((j for j, it in enumerate(p["items"])
                         if it["kind"] in _SELECTABLE_KINDS), 0)

    @kb.add("up", filter=_effort_open)
    def _(event: Any) -> None:
        p = state["picking_effort"]
        p["sel"] = (p["sel"] - 1) % len(p["items"])
        event.app.invalidate()

    @kb.add("down", filter=_effort_open)
    def _(event: Any) -> None:
        p = state["picking_effort"]
        p["sel"] = (p["sel"] + 1) % len(p["items"])
        event.app.invalidate()

    @kb.add("escape", filter=_effort_open)
    def _(event: Any) -> None:
        state["picking_effort"] = None
        event.app.invalidate()

    @kb.add("enter", filter=_effort_open, eager=True)
    def _(event: Any) -> None:
        p = state.get("picking_effort")
        if not p:
            return
        value = p["items"][p["sel"]]
        state["picking_effort"] = None
        # Render the updated setting through the normal fullscreen output path;
        # never let Rich output overwrite the selector line.
        from prompt_toolkit.application.run_in_terminal import in_terminal  # noqa: PLC0415
        async def _apply_effort() -> None:
            async with in_terminal():
                tui._cmd_knobs(f"effort={value}")
        event.app.create_background_task(_apply_effort())
        event.app.invalidate()

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

    @kb.add("left", filter=_picker_open)
    def _(event: Any) -> None:
        _picker_tab(-1)
        event.app.invalidate()

    @kb.add("right", filter=_picker_open)
    def _(event: Any) -> None:
        _picker_tab(1)
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

    @kb.add("down", filter=_can_enter_agents, eager=True)
    def _(event: Any) -> None:
        # ↓ from an empty prompt steps into the live subagent list.
        _open_agent_inspector()
        event.app.invalidate()

    @kb.add("down", filter=_agent_open, eager=True)
    @kb.add("c-n", filter=_agent_open, eager=True)
    def _(event: Any) -> None:
        items = _live_agent_items()
        if items:
            state["agent_inspector"]["sel"] = (state["agent_inspector"].get("sel", 0) + 1) % len(items)
        event.app.invalidate()

    @kb.add("up", filter=_agent_open, eager=True)
    @kb.add("c-p", filter=_agent_open, eager=True)
    def _(event: Any) -> None:
        items = _live_agent_items()
        if items:
            panel = state["agent_inspector"]
            cur = int(panel.get("sel", 0))
            if cur <= 0 and not panel.get("detail"):
                state["agent_inspector"] = None  # ↑ past the top of the list → prompt
            else:
                panel["sel"] = (cur - 1) % len(items)
        event.app.invalidate()

    @kb.add("enter", filter=_agent_open, eager=True)
    @kb.add("right", filter=_agent_open, eager=True)
    def _(event: Any) -> None:
        # Drill into the selected agent's focused detail view.
        if _live_agent_items():
            state["agent_inspector"]["detail"] = True
        else:
            state["agent_inspector"] = None
        event.app.invalidate()

    @kb.add("left", filter=_agent_open, eager=True)
    @kb.add("backspace", filter=_agent_open, eager=True)
    def _(event: Any) -> None:
        # ← goes back: detail → list, list → close (return to the prompt).
        panel = state["agent_inspector"]
        if panel.get("detail"):
            panel["detail"] = False
        else:
            state["agent_inspector"] = None
        event.app.invalidate()

    @kb.add("escape", filter=_agent_open, eager=True)
    @kb.add("c-c", filter=_agent_open, eager=True)
    def _(event: Any) -> None:
        state["agent_inspector"] = None  # Esc always closes the whole overlay
        event.app.invalidate()

    # -- /workflows overlay navigation (gated + eager so keys never leak into
    # the input line while the overlay is up) --------------------------------
    def _wf_move(delta: int) -> None:
        p = state.get("workflows")
        pairs = _wf_pairs()
        if not p or not pairs:
            return
        p["sel"] = (int(p.get("sel", 0)) + delta) % len(pairs)

    @kb.add("down", filter=_workflows_open, eager=True)
    @kb.add("c-n", filter=_workflows_open, eager=True)
    def _(event: Any) -> None:
        _wf_move(1)
        event.app.invalidate()

    @kb.add("up", filter=_workflows_open, eager=True)
    @kb.add("c-p", filter=_workflows_open, eager=True)
    def _(event: Any) -> None:
        _wf_move(-1)
        event.app.invalidate()

    @kb.add("right", filter=_workflows_open, eager=True)
    def _(event: Any) -> None:
        # → drills into the selected agent's detail (same as Enter).
        p = state["workflows"]
        if not p.get("detail") and _wf_pairs():
            p["detail"] = True
        event.app.invalidate()

    @kb.add("left", filter=_workflows_open, eager=True)
    @kb.add("backspace", filter=_workflows_open, eager=True)
    def _(event: Any) -> None:
        # ← goes back: detail → list, list → close (the run keeps executing).
        p = state["workflows"]
        if p.get("detail"):
            p["detail"] = False
        else:
            state["workflows"] = None
        event.app.invalidate()

    @kb.add("x", filter=_workflows_open, eager=True)
    def _(event: Any) -> None:
        _wf_stop()

    @kb.add("p", filter=_workflows_open, eager=True)
    def _(event: Any) -> None:
        _wf_toggle_pause()

    @kb.add("s", filter=_workflows_open, eager=True)
    def _(event: Any) -> None:
        _wf_save()

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
        if state.get("agent_inspector") is not None:
            return
        # The /workflows overlay steals Enter: drill into the selected agent's
        # detail pane (a second layer inside the same overlay).
        if state.get("workflows") is not None:
            p = state["workflows"]
            if not p.get("detail") and _wf_pairs():
                p["detail"] = True
            event.app.invalidate()
            return
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
            # Mid-turn fast-path commands run IMMEDIATELY, not queued — the
            # whitelist here mirrors _handle's mid-turn branch, which needs the
            # command to actually reach it (queuing would defer it to after the
            # turn, making that branch dead).
            _cmd0 = text.split(maxsplit=1)[0] if text.startswith("/") else ""
            if _cmd0 in {"/agents", "/jobs", "/job", "/cost", "/status", "/effort", "/workflows"}:
                _spawn_handle(text)
                return
            # A turn is running: QUEUE the message (Claude Code behavior) —
            # it fires the moment this turn finishes. Esc-interrupt clears it.
            q = state.setdefault("queue", [])
            q.append(text)
            event.app.create_background_task(_announce(
                f"⧉ queued ({len(q)}) — sends when this turn finishes · esc clears"))
            return
        _spawn_handle(text)

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
        if state.get("agent_inspector") is not None:
            state["agent_inspector"] = None
            event.app.invalidate()
            return
        # /workflows overlay: esc backs out of detail → list → closed. Closing
        # never stops the run — it keeps executing in the background.
        if state.get("workflows") is not None:
            p = state["workflows"]
            if p.get("detail"):
                p["detail"] = False
            else:
                state["workflows"] = None
            event.app.invalidate()
            return
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
            from .tui import model_supports_vision  # noqa: PLC0415
            if placeholder.startswith("[Image") and not model_supports_vision(tui.model):
                event.app.create_background_task(_announce(
                    f"attached {placeholder} — ⚠ {tui.model} can't see images; "
                    "/model to switch to a vision model (e.g. gpt-5.4)"))
            else:
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
            Window(FormattedTextControl(effort_ft), height=Condition(
                lambda: (1 + len(state["picking_effort"]["items"]))
                if state.get("picking_effort") else 0)), 
            # Live agent inspector — arrow-key navigable while delegates run.
            Window(FormattedTextControl(_agent_inspector_ft), height=_agent_inspector_height),
            # /workflows overlay — two-pane structured multi-agent run viewer;
            # height 0 unless opened. Pass the height CALLABLE, never its call.
            Window(FormattedTextControl(_workflows_ft), height=_workflows_height),
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
            # Tick while a turn runs (spinner) OR the /workflows overlay is open,
            # so agent durations/spinners in the overlay recompute live even when
            # no turn is active in the foreground.
            if state["working"] or state.get("workflows") is not None:
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

    def _notify_job(job: Any) -> None:
        from .tui import format_job_completion_line  # noqa: PLC0415

        line = format_job_completion_line(job, width=shutil.get_terminal_size((80, 24)).columns)
        get_app().create_background_task(_announce(line))
        get_app().invalidate()

    tui._job_notify = _notify_job

    # Test hook: seed fake live subagents so the ↓-into-inspector path is
    # drivable in a PTY test without spawning a real model. Harmless in prod
    # (only fires when the env var is explicitly set).
    if _os.environ.get("MANTIS_FS_SEED_AGENTS"):
        _now = time.monotonic()
        tui._live_subagents = {
            1: {"type": "explore", "desc": "seed one", "tools": 3,
                "last_event": "tool grep", "started": _now,
                "events": [(_now, "grep foo")]},
            2: {"type": "explore", "desc": "seed two", "tools": 5,
                "last_event": "tool glob", "started": _now, "events": []},
        }

    anim = asyncio.ensure_future(_animate())
    mcp_boot = asyncio.ensure_future(_mcp_startup())
    try:
        await app.run_async()
    finally:
        _retry.notify = None
        # Collect every background task, request cancellation, THEN await them
        # so the cancellation actually propagates to each task's next await —
        # subprocess termination / transport close only happen there. Merely
        # calling .cancel() and returning leaves orphaned child processes.
        pending: list[Any] = [anim, mcp_boot]
        anim.cancel()
        mcp_boot.cancel()
        if state.get("agi"):
            state["agi"]["stopped"].set()
        for loop in (state.get("loops") or {}).values():  # stop /loop timers
            loop["stopped"].set()
            t = loop.get("task")
            if t is not None:
                t.cancel()
                pending.append(t)
        tui._jobs.cancel_all()  # background jobs die with the session
        for w in (state.get("watches") or {}).values():  # stop /watch sentinels
            w["stopped"].set()
            t = w.get("task")
            if t is not None:
                t.cancel()
                pending.append(t)
        models_task = getattr(tui, "_models_task", None)
        if models_task is not None:
            models_task.cancel()
            pending.append(models_task)
        # Await all cancellations (swallowing CancelledError + any teardown
        # error) so nothing is left half-torn-down when we return.
        try:
            await asyncio.gather(*pending, return_exceptions=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            await tui._close_mcp()
        except Exception:  # noqa: BLE001
            pass
        if tui.agent is not None:
            await tui.agent.aclose()
        try:
            tui._mark_session_state(clean=True)  # orderly exit — no crash hint
        except Exception:  # noqa: BLE001
            pass
    tui.console.print("[ansibrightblack]bye 👋[/]")
    return 0
