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

These ship as core dependencies, so ``pip install mantis-agent-sdk`` gives you a working terminal out of the box::

    pip install mantis-agent-sdk

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
    "/models": "browse & pick a model (local · API · self-host)",
    "/model": "switch / pick a model",
    "/resume": "resume a past conversation",
    "/branch": "fork this conversation into a new session",
    "/rewind": "rewind the conversation to an earlier message",
    "/enable": "turn on a hosted provider (saves its API key)",
    "/disable": "forget a provider's saved key",
    "/connect": "point at your own self-hosted server",
    "/context": "show context-window usage",
    "/vim": "toggle vim editing mode",
    "/help": "show available commands",
    "/clear": "clear the conversation history",
    "/cwd": "show the working directory",
    "/exit": "quit mantis",
    "/quit": "quit mantis",
}

# Friendly verbs for tool calls, Claude-Code-style (e.g. ``Read foo.py``). The
# value is (verb, primary-arg-keys-in-priority-order) — the first present key is
# shown as the target after the verb.
TOOL_VERBS = {
    "bash": ("Run", ("command",)),
    "read_file": ("Read", ("path", "file_path")),
    "write_file": ("Write", ("path", "file_path")),
    "edit_file": ("Edit", ("path", "file_path")),
    "multi_edit": ("Edit", ("path", "file_path")),
    "ls": ("List", ("path",)),
    "glob": ("Find", ("pattern", "path")),
    "grep": ("Search", ("pattern", "query")),
    "web_search": ("Search web", ("query",)),
    "web_fetch": ("Fetch", ("url",)),
    "todo_write": ("Plan", ()),
}

# File extension → pygments lexer name, for syntax-highlighting diff bodies.
_EXT_LANG = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".json": "json",
    ".md": "markdown", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin", ".c": "c",
    ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".rb": "ruby",
    ".php": "php", ".html": "html", ".css": "css", ".scss": "scss",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".sql": "sql",
    ".swift": "swift", ".lua": "lua", ".r": "r", ".ex": "elixir", ".exs": "elixir",
}


def _lang_from_path(path: str | None) -> str | None:
    if not path:
        return None
    return _EXT_LANG.get(Path(path).suffix.lower())


def _word_diff_spans(old: str, new: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Char-level diff of two lines → (old_changed_spans, new_changed_spans),
    where each span is a ``(start, end)`` char range that differs. Used to
    brighten just the changed part of a modified line. Returns empty spans when
    the lines share too little (a wholesale rewrite — the row colour already
    tells that story, so word-highlighting adds only noise)."""
    import difflib  # noqa: PLC0415

    if not old or not new:
        return [], []
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    equal = sum(a2 - a1 for tag, a1, a2, b1, b2 in sm.get_opcodes() if tag == "equal")
    # Too little in common → treat as unrelated; don't word-highlight.
    if equal < 0.3 * max(len(old), len(new)):
        return [], []
    old_spans: list[tuple[int, int]] = []
    new_spans: list[tuple[int, int]] = []
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op in ("replace", "delete") and a2 > a1:
            old_spans.append((a1, a2))
        if op in ("replace", "insert") and b2 > b1:
            new_spans.append((b1, b2))
    return old_spans, new_spans


# Permission-mode footer, cycled with shift+tab. The agent's tools (bash, write,
# edit) DO execute against the real machine; the footer is still cosmetic for now
# (no per-tool gating wired up yet) but it matches the Claude Code UX.
MODES = [
    ("default", "", "ansibrightblack"),
    ("accept edits on", "⏵⏵ ", "ansigreen"),
    ("plan mode on", "⏸ ", "ansicyan"),
    ("bypass permissions on", "⏵⏵ ", "ansired"),
]

# Substrings that mark a /v1/models entry as NOT a chat model — embeddings,
# audio, image, moderation, legacy base-completion engines. Used to keep the
# live model list clean (OpenAI's endpoint returns ~50 of these).
_NONCHAT_MARKERS = (
    "embed", "tts", "whisper", "audio", "speech", "transcrib", "dall", "-image",
    "moderation", "rerank", "guard", "similar", "-search", "realtime", "sora",
    "clip", "-edit", "ada-", "babbage", "curie", "davinci", "ocr", "-voice",
    "video", "stable-diffusion", "flux",
    # responses-only OpenAI models — these 400 on /v1/chat/completions ("use the
    # v1/responses endpoint instead"), which mantis doesn't speak, so hide them
    # from the picker: the -codex coding models, computer-use, and -pro reasoners.
    "codex", "computer-use", "-pro",
    # legacy / date-stamped OpenAI engines (so modern flagships survive the cap)
    "gpt-3.5", "gpt-4-0", "gpt-4-1", "-0613", "-0314", "-0301", "-1106", "-0125", "-16k",
)


def _is_chat_model(model_id: str) -> bool:
    return not any(m in model_id.lower() for m in _NONCHAT_MARKERS)


# Open-weight model families — the only ones you can actually self-host (run the
# published weights on your own GPU). Everything else (gpt-5.x, gpt-4o, o3/o4,
# gemini, claude, qwen-max/plus, glm-4-plus/air/flash, moonshot-v1) is
# proprietary: the provider's API is the only way to run it.
_OPEN_WEIGHT_MARKERS = (
    "gpt-oss", "llama", "qwen2", "qwen3", "qwen-2", "qwen-3", "qwq",
    "deepseek", "glm-4.5", "glm-4.6", "glm-4.7", "glm4", "zai-glm",
    "kimi-k2", "kimi-k1", "mistral", "mixtral", "magistral", "gemma",
    "phi-", "phi3", "phi4", "olmo", "yi-", "internlm", "command-r",
    "falcon", "smollm", "granite", "nemotron",
)


def _is_open_weight(model_id: str) -> bool:
    return any(m in model_id.lower() for m in _OPEN_WEIGHT_MARKERS)

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

    def stop_sync(self) -> None:
        """Cancel + clear the line without awaiting — callable from a sync
        callback (e.g. the live token-stream sink). The cancelled task tears
        itself down on the next loop tick."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        sys.stdout.write(_CLEAR_LINE)
        sys.stdout.flush()


_MD_PATCHED = False


def _compact_markdown(text: str) -> Any:
    """Build a tight rich ``Markdown``: code blocks with no surrounding padding
    or grey box, so replies don't get the big vertical margins rich adds by
    default. Patches ``Markdown.elements`` once (lazily, so importing the library stays cheap)."""
    global _MD_PATCHED
    from rich.markdown import CodeBlock, Markdown  # noqa: PLC0415
    from rich.syntax import Syntax  # noqa: PLC0415

    if not _MD_PATCHED:
        class _TightCodeBlock(CodeBlock):
            def __rich_console__(self, console: Any, options: Any) -> Any:  # noqa: ANN401
                code = str(self.text).rstrip()
                yield Syntax(
                    code, self.lexer_name, theme=self.theme,
                    background_color="default", word_wrap=True, padding=0,
                )

        Markdown.elements["fence"] = _TightCodeBlock
        Markdown.elements["code_block"] = _TightCodeBlock
        _MD_PATCHED = True

    return Markdown(text, code_theme="ansi_dark")


def _missing_deps_message() -> str:
    return (
        "The `mantis` terminal needs the optional CLI dependencies.\n\n"
        "    pip install mantis-agent-sdk\n\n"
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
    """Print the banner; return the number of terminal lines it actually occupied.

    The height is *measured* (not estimated) via ``render_lines`` at the current
    width, so wrapping on narrow terminals can't throw off the caller's
    bottom-padding math and clip the mascot.
    """
    from rich.console import Group  # noqa: PLC0415
    from rich.table import Table  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    mascot = _mascot_lines(Text)

    title = Text()
    title.append("Mantis", style=f"bold {BODY}")
    title.append(" Code ", style="bold white")
    title.append(f"v{__version__}", style="bright_black")

    where = "Ollama (local)" if "localhost" in backend or "127.0.0.1" in backend else backend
    sub = Text()
    sub.append(model, style="white")
    sub.append("  ·  ", style="bright_black")
    sub.append(where, style="bright_black")

    cwd = Text(_short_cwd(), style="bright_black")

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

    tip = Text(
        " tip: type a request and press Enter · /help for commands · /exit to quit",
        style="bright_black",
    )
    body = Group(grid, Text(""), tip)

    # Measure the real rendered height at this width (handles wrapping).
    opts = console.options.update(height=None)
    height = len(console.render_lines(body, opts, pad=False))

    console.print()
    console.print(body)
    console.print()
    return height + 2  # the two blank lines we print around the body


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
        # --godmode / --dangerously-skip-permissions: force the permission
        # engine to Allow every tool (bypass), overriding even the dangerous-
        # command safety prompt. Set by main() from the CLI flag.
        self.force_bypass = False
        # Vim editing mode for the input line (toggled with /vim).
        self.vim_mode = os.environ.get("MANTIS_VIM") == "1"
        self.messages: list[Any] = []
        self.agent: Any = None
        # Live todo list, mutated in place by the bound ``todo_write`` tool and
        # re-rendered by the TUI whenever the agent updates it.
        self.todos: list[dict] = []
        self._todos_shown: list[dict] = []
        # Pending paste attachments (images/files) for the next message. Each is
        # ``(placeholder, content_block)``; flushed into the user turn on submit.
        self.pending_attachments: list[tuple[str, Any]] = []
        # True while the current assistant turn's text is being streamed live.
        self._turn_streamed = False
        # The on-disk transcript for /resume + /branch (created in run()).
        self.transcript: Any = None
        # Selected row in the live slash-command menu (arrow-key navigable).
        self._slash_sel = 0
        # Direct reference to the prompt's input Buffer, set in _build_session.
        # Reading ``self._input_buffer.text`` at toolbar-render time is the one
        # reliable way to know what's typed (get_app() returns a dummy mid-render
        # and on_text_changed can lag the repaint).
        self._input_buffer: Any = None

        from rich.console import Console  # noqa: PLC0415
        from rich.theme import Theme  # noqa: PLC0415

        # Render inline `code` and code blocks as plain colored text — no grey
        # highlight box (rich's default reverse/background for markdown code,
        # which reads as a confusing selection-style highlight in chat).
        self.console = Console(theme=Theme({
            "markdown.code": "green",
            "markdown.code_block": "green",
        }))

    # -- provider / agent wiring (mirrors cli._build_provider_for_args) ------

    def _build_agent(self) -> Any:
        from .agent import Agent  # noqa: PLC0415
        from .builtin_tools import CODING_TOOLS, web_fetch, web_search  # noqa: PLC0415
        from .builtin_tools.ask import make_ask_user_question  # noqa: PLC0415
        from .builtin_tools.memory_tool import remember  # noqa: PLC0415
        from .builtin_tools.todo import make_todo_write  # noqa: PLC0415
        from .permissions import PermissionContext  # noqa: PLC0415
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
        # a real tool belt — shell + filesystem + web — so it can actually *do*
        # things instead of describing them. Without these the agent loop has
        # nothing to call and every turn collapses to a single chat completion.
        registry = ToolRegistry()
        registry.add(*CODING_TOOLS)
        registry.add(web_search, web_fetch)
        registry.add(make_todo_write(self.todos))
        registry.add(remember)  # write path into persistent memory (recall is automatic)
        registry.add(make_ask_user_question(self._ask_user_question))  # ask the user
        from .builtin_tools.plan import make_exit_plan_mode  # noqa: PLC0415
        registry.add(make_exit_plan_mode(self._exit_plan_mode))  # plan approval handoff
        from .builtin_tools.skill_tool import load_skill  # noqa: PLC0415
        registry.add(load_skill)  # progressive-disclosure skill loading

        # Wire the shift+tab footer modes to the real permission system so they
        # actually gate execution (Claude-Code parity), not just decorate the
        # footer. The callback reads ``self.mode_idx`` live, so toggling the mode
        # changes behavior on the very next tool call.
        return Agent(
            model=self.model,
            provider=provider,
            system=self.system or self._default_system(),
            tools=registry,
            permissions=PermissionContext(
                # godmode → engine-level bypass (Allow everything, even dangerous
                # shell commands, no prompt). Otherwise "default" and _permit
                # drives the live shift+tab mode.
                mode="bypass" if self.force_bypass else "default",
                can_use_tool=self._permit,
                asker=self._ask_permission,
                rules=self._load_permission_rules(),
            ),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            max_steps=self.max_turns,
        )

    async def _permit(self, tool: Any, tool_input: dict, ctx: Any) -> Any:
        """Permission decision keyed off the live shift+tab footer mode.

        * ``bypass permissions on`` — allow everything.
        * read-only tools — always allowed (reads never prompt).
        * ``plan mode on`` — mutating tools denied so the model researches/plans
          without touching the machine (Claude Code's plan mode).
        * ``accept edits on`` — auto-allow file edits; still ask for bash/other.
        * ``default`` — ask the human for every mutating tool.

        Any ``Ask`` returned here is resolved by ``check_permission`` through
        the context's ``asker`` (the interactive prompt). Reaching ``_permit``
        already means the call wasn't session-allowed or rule-covered.
        """
        from .permissions import (  # noqa: PLC0415
            Allow,
            Ask,
            Deny,
            _format_prompt,
            _is_edit_tool,
        )

        mode = MODES[self.mode_idx][0]
        if mode == "bypass permissions on":
            return Allow()
        if getattr(tool, "is_read_only", False):
            return Allow()
        if mode == "plan mode on":
            return Deny(
                reason=(
                    f"plan mode is on — `{tool.name}` is blocked. Research read-only, "
                    f"then call `exit_plan_mode` with your plan to get approval "
                    f"before making any changes."
                )
            )
        if mode == "accept edits on" and _is_edit_tool(tool):
            return Allow()
        return Ask(prompt=_format_prompt(tool, tool_input))

    async def _ask_permission(self, tool: Any, tool_input: dict, prompt: str) -> str:
        """Base interactive asker for the classic REPL. The full-screen app
        replaces this with an in-pane prompt (see tui_fullscreen). Returns
        ``allow_once`` / ``allow_session`` / ``deny``. Denies when there's no
        TTY (headless / piped) so an unattended run never auto-runs bash."""
        import sys  # noqa: PLC0415

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return "deny"
        try:
            self.console.print(
                f"[ansiyellow]?[/] allow [bold]{prompt}[/]  "
                f"[ansibrightblack][y]es once · [s]ession · [n]o[/]"
            )
            ans = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "deny"
        if ans in ("s", "session"):
            return "allow_session"
        if ans in ("y", "yes", ""):
            return "allow_once"
        return "deny"

    async def _ask_user_question(self, questions: list[dict]) -> list[dict]:
        """Route AskUserQuestion to the interactive picker. The full-screen app
        installs ``self._fs_ask``; otherwise fall back to a numbered prompt in
        the classic REPL (or skip when there's no TTY)."""
        import sys  # noqa: PLC0415

        fs = getattr(self, "_fs_ask", None)
        if fs is not None:
            return await fs(questions)

        # Classic REPL / no full-screen app: a simple numbered prompt.
        results: list[dict] = []
        tty = sys.stdin.isatty() and sys.stdout.isatty()
        for q in questions:
            answers: list[str] = []
            if tty:
                self.console.print(f"\n[bold]{q['question']}[/]")
                for i, o in enumerate(q["options"], 1):
                    self.console.print(
                        f"  [white]{i}[/] {o['label']}  [ansibrightblack]{o['description']}[/]")
                self.console.print("  [white]o[/] Other (type your own)")
                try:
                    raw = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    raw = ""
                if raw.lower() in ("o", "other"):
                    try:
                        answers = [input("  your answer: ").strip()]
                    except (EOFError, KeyboardInterrupt):
                        answers = []
                elif raw.isdigit() and 1 <= int(raw) <= len(q["options"]):
                    answers = [q["options"][int(raw) - 1]["label"]]
            results.append({"question": q["question"], "header": q["header"], "answers": answers})
        return results

    async def _exit_plan_mode(self, plan: str) -> str:
        """Present a plan for approval. The full-screen app installs
        ``self._fs_plan`` (renders the plan + an approve/keep-planning picker and
        flips the mode); otherwise fall back to a simple prompt."""
        import sys  # noqa: PLC0415

        if MODES[self.mode_idx][0] != "plan mode on":
            return "You are not in plan mode — just proceed with the implementation."

        fs = getattr(self, "_fs_plan", None)
        if fs is not None:
            return await fs(plan)

        # Classic REPL fallback.
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return "Non-interactive — proceeding with the plan."
        self.console.print(f"\n[bold]Plan[/]\n{plan}\n")
        try:
            ans = input("  Proceed with this plan? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans in ("y", "yes"):
            self.mode_idx = 0  # lift plan mode → default
            return "Plan approved. Plan mode is now OFF — proceed with the implementation."
        return "The user did not approve the plan. Stay in plan mode and revise it."

    def _load_permission_rules(self) -> Any:
        """Build a PermissionRuleSet from settings.json ``permissions`` rules
        (Claude-style ``Bash(rm -rf*)`` / ``Read(...)`` entries). None if no
        rules are configured."""
        import re  # noqa: PLC0415

        from .permissions import PermissionRule, PermissionRuleSet  # noqa: PLC0415

        try:
            from .settings import SETTING_SOURCES, load_settings  # noqa: PLC0415
            loaded = load_settings(SETTING_SOURCES) or {}
        except Exception:  # noqa: BLE001 — missing/broken settings: no rules
            return None
        perms = (loaded.get("permissions") if isinstance(loaded, dict) else None) or {}

        def parse(entry: str, action: str) -> Any:
            m = re.fullmatch(r"\s*([A-Za-z0-9_]+)\s*\((.*)\)\s*", entry)
            if m:
                tool_name, inner = m.group(1), m.group(2)
                return PermissionRule(pattern=f"*{inner}*", action=action, tool_name=tool_name)
            return PermissionRule(pattern="*", action=action, tool_name=entry.strip())

        rs = PermissionRuleSet()
        for e in perms.get("deny") or []:
            rs.deny.append(parse(e, "deny"))
        for e in perms.get("allow") or []:
            rs.allow.append(parse(e, "allow"))
        for e in perms.get("ask") or []:
            rs.ask.append(parse(e, "ask"))
        return rs if (rs.allow or rs.deny or rs.ask) else None

    def _default_system(self) -> str:
        """The agent system prompt — what makes the model behave like a real
        coding agent (act via tools, minimal diffs, verify, stay terse) rather
        than a generic chat assistant. Environment/git facts are injected
        separately as the ``<env>`` context head (see include_env), so they're
        deliberately not repeated here."""
        return (
            "You are Mantis, an interactive coding agent running in the user's "
            "terminal. You complete software-engineering tasks — fixing bugs, "
            "adding features, refactoring, explaining code, running commands — by "
            "CALLING TOOLS, not by describing what to do.\n\n"
            "# Acting\n"
            "- Act immediately. If the task can be done with a tool, call it on "
            "the first message — don't explain the command first, don't print it "
            "in a code block, don't wait for 'run it'. The user asking IS the "
            "permission; they're watching the output.\n"
            "- read_file, write_file, edit_file, ls, glob, grep are TOOLS — call "
            "them directly. They are not shell programs; never run them inside "
            "bash (`bash(\"edit_file ...\")` fails). Change files with edit_file/"
            "write_file, never an interactive editor (nano/vim).\n"
            "- 'find/show/list/check X' means call the tool now and report the "
            "REAL result — never answer from memory or guess. Never tell the user "
            "to run a command themselves; run it and show the actual output.\n"
            "- Anything expressible as code, you can do: write a script (browser "
            "automation, scraping, API calls, tests), `pip install` what it needs, "
            "run it, report the output. Never refuse or moralize about a normal "
            "engineering task — if it can be scripted, write it and run it. No "
            "disclaimers, no lectures.\n\n"
            "# Doing tasks well\n"
            "- Read before you change. Don't edit code you haven't read; match the "
            "existing conventions, naming, and style.\n"
            "- Make the smallest change that does the job. Don't add features, "
            "refactor, or 'improve' beyond what was asked; no speculative "
            "abstractions, no error handling for cases that can't happen, no "
            "backwards-compat shims. Three similar lines beat a premature "
            "abstraction. Delete code you're sure is unused rather than leaving "
            "shims.\n"
            "- Don't add comments unless the WHY is non-obvious. Never add "
            "docstrings/comments/type hints to code you didn't change.\n"
            "- Prefer editing an existing file to creating a new one; don't create "
            "files unless necessary.\n"
            "- If an approach fails, read the error and diagnose before switching "
            "tactics — don't blindly retry the identical action, and don't abandon "
            "a viable approach after one failure. Never repeat a tool call you "
            "already made; if the result wasn't useful, change the arguments.\n"
            "- Verify before reporting done: run the test, execute the script, "
            "check the output. If you can't verify, say so. Report outcomes "
            "faithfully — never claim tests pass when output shows failures; state "
            "plainly when something is done, without hedging.\n\n"
            "# Acting with care\n"
            "- Local, reversible actions (editing files, running tests) are free — "
            "just do them.\n"
            "- For actions that are hard to reverse or affect shared state, confirm "
            "first unless told to operate autonomously: rm -rf, deleting "
            "files/branches, dropping tables, force-push, git reset --hard, "
            "removing dependencies, pushing code, opening/closing PRs, sending "
            "messages, uploading to third-party services. Approval once ≠ approval "
            "always. Don't use destructive shortcuts (e.g. --no-verify) to bypass "
            "an obstacle; fix root causes.\n\n"
            "# Output\n"
            "- Lead with the answer or result, not the reasoning. Be brief and "
            "direct; skip preamble and filler. If one sentence does it, don't write "
            "three. After a tool runs, a short summary of what it means is enough — "
            "let the output speak.\n"
            "- Reference code as file_path:line_number so the user can click to it.\n"
            "- Only use emojis if the user asks. Don't put a colon right before a "
            "tool call.\n"
            "- Stop when you have the answer — write the final answer with no "
            "further tool calls."
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
        """Choose the best installed model to stand in for ``model``.

        ``mantis`` is an *agent* — it lives or dies on tool calling, so the pick
        is biased hard toward models that support native function-calling (via
        :func:`capabilities.lookup_model`), then toward the requested family,
        coder/instruct tunes, and larger parameter counts. This stops a bare
        ``mantis`` from silently downgrading to a tiny non-agentic model just
        because it shares a name prefix.
        """
        if model in available:
            return model

        import re  # noqa: PLC0415

        from .capabilities import lookup_model  # noqa: PLC0415

        base = model.split(":")[0].lower()

        def size_b(name: str) -> float:
            m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", name.lower())
            return float(m.group(1)) if m else 0.0

        def score(name: str) -> float:
            s = 0.0
            if lookup_model(name).supports_native_tools:
                s += 100  # the thing that actually matters for an agent
            if name.split(":")[0].lower() == base:
                s += 20
            if any(k in name.lower() for k in ("coder", "instruct", "chat")):
                s += 5
            s += min(size_b(name), 72) / 10.0  # nudge toward bigger, capped
            return s

        return max(available, key=score)

    def _restore_last_model(self) -> None:
        """If the user didn't pick a model (no --model, no $MANTIS_AGENT_MODEL),
        reopen on the one they left off with last session — wiring its backend +
        key from the catalog. Skipped silently if that provider is no longer
        enabled, so we never restore a model that would just error."""
        from . import catalog  # noqa: PLC0415

        if os.environ.get("MANTIS_AGENT_MODEL") or self.model != "qwen2.5-7b-instruct":
            return  # an explicit choice — respect it
        last = catalog.get_last_model()
        if not last:
            return
        model = last["model"]
        prov = catalog.provider_for_model(model)
        if prov is not None:
            key = catalog.api_key_for(prov)
            if not key:
                return  # provider disabled since — don't restore a dead model
            self.model, self.backend, self.api_key = model, prov.base_url, key
        else:
            self.model = model
            if last.get("backend"):
                self.backend = last["backend"]

    def _resolve_model(self) -> None:
        """Point ``self.model`` at something that actually exists, or explain how
        to get it. Called once at startup, before the banner is drawn."""
        from . import catalog  # noqa: PLC0415

        # Hosted models are served by their provider, not Ollama — don't let the
        # local-model resolver below swap them out for an installed Ollama model.
        if catalog.provider_for_model(self.model):
            return
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
        from prompt_toolkit.formatted_text import HTML  # noqa: PLC0415
        from prompt_toolkit.key_binding import KeyBindings  # noqa: PLC0415
        from prompt_toolkit.styles import Style  # noqa: PLC0415

        kb = KeyBindings()

        @kb.add("s-tab")
        def _cycle_mode(event: Any) -> None:  # noqa: ANN401
            self.mode_idx = (self.mode_idx + 1) % len(MODES)
            event.app.invalidate()

        @kb.add("c-v")
        def _paste_attachment(event: Any) -> None:  # noqa: ANN401
            """Ctrl+V: pull an image (or copied file) off the system clipboard,
            attach it to the next message, and drop a ``[Image #N]`` placeholder
            into the line — Claude-Code-style multimodal paste."""
            placeholder = self._capture_clipboard_attachment()
            if placeholder:
                event.app.current_buffer.insert_text(placeholder)

        # Explicit exit bindings so Ctrl+C / Ctrl+D always quit the prompt (the
        # run loop catches these and prints "bye"). Ctrl+C aborts even mid-edit;
        # Ctrl+D exits on an empty line and deletes-forward otherwise.
        @kb.add("c-c")
        def _ctrl_c(event: Any) -> None:  # noqa: ANN401
            event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

        @kb.add("c-d")
        def _ctrl_d(event: Any) -> None:  # noqa: ANN401
            buf = event.app.current_buffer
            if buf.text:
                buf.delete()
            else:
                event.app.exit(exception=EOFError, style="class:aborting")

        @kb.add("c-o")
        def _expand_transcript(event: Any) -> None:  # noqa: ANN401
            """Ctrl+O: open the full conversation in the pager with every tool
            output untruncated — Claude Code's `app:toggleTranscript`."""
            from prompt_toolkit.application import run_in_terminal  # noqa: PLC0415

            run_in_terminal(self._show_transcript)

        # Navigable slash-command menu. These fire ONLY while the menu is open
        # (text is a slash command in progress) — otherwise the defaults (history,
        # tab-complete, submit) apply untouched.
        from prompt_toolkit.filters import Condition  # noqa: PLC0415

        _slash_open = Condition(self._slash_menu_open)

        @kb.add("down", filter=_slash_open)
        def _slash_down(event: Any) -> None:  # noqa: ANN401
            self._slash_sel += 1
            event.app.invalidate()

        @kb.add("up", filter=_slash_open)
        def _slash_up(event: Any) -> None:  # noqa: ANN401
            self._slash_sel -= 1
            event.app.invalidate()

        @kb.add("tab", filter=_slash_open)
        def _slash_tab(event: Any) -> None:  # noqa: ANN401
            self._slash_accept(event)

        @kb.add("enter", filter=_slash_open)
        def _slash_enter(event: Any) -> None:  # noqa: ANN401
            self._slash_accept(event, submit_if_exact=True)

        from prompt_toolkit.completion import Completer, Completion  # noqa: PLC0415

        installed_models, _ = self._available_models()

        class _MantisCompleter(Completer):
            def get_completions(inner, document, complete_event):  # noqa: N805
                from . import catalog  # noqa: PLC0415

                text = document.text_before_cursor
                if not text.startswith("/"):
                    return
                if " " not in text:
                    # Command names are shown in the reliable toolbar menu (see
                    # _slash_menu_lines), not the float — don't double up here.
                    return
                cmd, _, rest = text.partition(" ")
                if cmd == "/model":  # suggest model ids
                    seen = set()
                    for m in installed_models:
                        if rest.lower() in m.lower():
                            seen.add(m)
                            yield Completion(m, start_position=-len(rest),
                                             display=m, display_meta="ollama · local")
                    for prov in catalog.CATALOG:
                        for m in prov.models:
                            if m not in seen and rest.lower() in m.lower():
                                seen.add(m)
                                yield Completion(m, start_position=-len(rest),
                                                 display=m, display_meta=prov.label)
                elif cmd in ("/enable", "/disable"):  # suggest provider ids
                    for prov in catalog.CATALOG:
                        if prov.id.startswith(rest.lower()):
                            yield Completion(prov.id, start_position=-len(rest),
                                             display=prov.id, display_meta=prov.label)

        completer = _MantisCompleter()

        def bottom_toolbar() -> Any:
            import shutil  # noqa: PLC0415

            label, symbol, color = MODES[self.mode_idx]
            if self.mode_idx == 0:
                left = ""  # default mode: no footer hint
            else:
                left = f"  {symbol}{label} (shift+tab to cycle)"
            right = f"{self.model} "
            pad = " " * max(1, 70 - len(left) - len(right))
            # A dim rule on the first line frames the input from below (the run
            # loop prints a matching rule above it), Claude-Code style.
            width = shutil.get_terminal_size((80, 24)).columns
            rule = "─" * width

            # Live slash-command menu — rendered HERE in the toolbar (which always
            # paints reliably) rather than relying on prompt_toolkit's completion
            # float, which fights the framed multi-line prompt. When the line is a
            # slash command in progress, show the matching commands + descriptions.
            menu = self._slash_menu_lines(rule)
            if menu is not None:
                return HTML(menu)

            return HTML(
                f'<style fg="ansibrightblack">{rule}</style>\n'
                f'<style fg="{self._toolbar_fg()}">{left}</style>'
                f'{pad}<style fg="ansibrightblack">{right}</style>'
            )

        # Dark completion menu (no default white background): a near-black green
        # panel, dim text, and a bright-green selected row — matching the rest
        # of the mantis palette. Hex colors are downconverted to 256 on
        # Terminal.app automatically.
        style = Style.from_dict({
            "prompt": BODY,
            "placeholder": "ansibrightblack",
            # Toolbar with no reverse/white background — just plain text on the
            # terminal's own background.
            "bottom-toolbar": "noreverse bg:default",
            "completion-menu": "bg:#11160c",
            "completion-menu.completion": "bg:#11160c #93a081",
            "completion-menu.completion.current": "bg:#26340f #c6e79a bold",
            "completion-menu.meta.completion": "bg:#11160c #5f6b54",
            "completion-menu.meta.completion.current": "bg:#26340f #aacb7d",
            "scrollbar.background": "bg:#11160c",
            "scrollbar.button": "bg:#3a4a26",
        })

        from prompt_toolkit.shortcuts import CompleteStyle  # noqa: PLC0415

        sess = PromptSession(
            key_bindings=kb,
            completer=completer,
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            bottom_toolbar=bottom_toolbar,
            style=style,
            multiline=False,
            # Erase the whole framed prompt (top rule + input + bottom rule +
            # footer) on submit; the run loop then echoes a clean "› message"
            # so only the live input is ever framed, never past turns.
            erase_when_done=True,
            reserve_space_for_menu=6,
        )

        # Hold a direct reference to the input buffer so the toolbar slash-menu
        # can read exactly what's typed at render time, and reset the selection
        # when the line stops being a slash command.
        self._input_buffer = sess.default_buffer

        def _on_change(buf: Any) -> None:
            if not buf.text.startswith("/"):
                self._slash_sel = 0

        sess.default_buffer.on_text_changed += _on_change
        return sess

    def _toolbar_fg(self) -> str:
        return MODES[self.mode_idx][2]

    # -- live slash-command menu (rendered in the toolbar, navigable) --------

    def _slash_current_matches(self) -> list[tuple[str, str]]:
        """Commands matching the in-progress slash line, or [] if not applicable.
        Reads the live input Buffer directly — the only reliable source mid-render."""
        text = self._input_buffer.text if self._input_buffer is not None else ""
        if not text.startswith("/") or " " in text:
            return []
        return [(c, d) for c, d in SLASH_COMMANDS.items() if c.startswith(text)]

    def _slash_menu_open(self) -> bool:
        return bool(self._slash_current_matches())

    def _slash_accept(self, event: Any, *, submit_if_exact: bool = False) -> None:
        """Fill the selected command into the line. If it's already the exact
        command and ``submit_if_exact`` (Enter), submit it."""
        matches = self._slash_current_matches()
        if not matches:
            return
        cmd = matches[self._slash_sel % len(matches)][0]
        buf = event.app.current_buffer
        if submit_if_exact and buf.text == cmd:
            buf.validate_and_handle()
            return
        buf.text = cmd + " "
        buf.cursor_position = len(buf.text)
        self._slash_sel = 0

    def _slash_menu_lines(self, rule: str) -> str | None:
        """The toolbar HTML for the slash menu (rule + rows, selected row
        highlighted), or ``None`` when no menu should show."""
        from html import escape  # noqa: PLC0415

        matches = self._slash_current_matches()
        if not matches:
            self._slash_sel = 0
            return None
        sel = self._slash_sel % len(matches)
        rows = [f'<style fg="ansibrightblack">{rule}</style>']
        for i, (cmd, desc) in enumerate(matches[:8]):
            c, d = escape(cmd), escape(desc)
            if i == sel:
                rows.append(
                    f'  <style fg="#0b1605" bg="{BODY}"> {c} </style>'
                    f'  <style fg="ansibrightblack">{d}</style>'
                )
            else:
                rows.append(
                    f'  <style fg="{BODY}">{c}</style>'
                    f'  <style fg="ansibrightblack">{d}</style>'
                )
        return "\n".join(rows)

    def _placeholder(self) -> Any:
        from prompt_toolkit.formatted_text import HTML  # noqa: PLC0415
        from html import escape  # noqa: PLC0415

        prompt = random.choice(EXAMPLE_PROMPTS)
        return HTML(f'<style fg="ansibrightblack">Try "{escape(prompt)}"</style>')

    # -- paste attachments (images / files) ---------------------------------

    def _capture_clipboard_attachment(self) -> str | None:
        """Grab an image (or copied file) off the clipboard into
        ``pending_attachments``. Returns the ``[Image #N]`` / ``[File: x]``
        placeholder to echo in the input, or ``None`` if the clipboard had no
        attachable content."""
        try:
            from . import clipboard  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None

        block = None
        label = "Image"
        if clipboard.has_clipboard_image():
            block = clipboard.grab_clipboard_image()
        else:
            path = clipboard.grab_clipboard_file_path()
            if path:
                try:
                    blocks = clipboard.file_to_blocks(path)
                    block = blocks[0] if blocks else None
                    label = "Image" if clipboard.is_image_path(path) else "File"
                except (OSError, ValueError):
                    return None
        if block is None:
            return None
        n = len(self.pending_attachments) + 1
        placeholder = f"[{label} #{n}]"
        self.pending_attachments.append((placeholder, block))
        return placeholder

    def _build_user_content(self, text: str) -> Any:
        """Combine the typed text with any pending paste attachments into a
        message body. Plain string when there are no attachments (keeps simple
        turns simple); a content-block list when images/files are attached."""
        from .types import TextBlock  # noqa: PLC0415

        # A bare path the user dragged in (no Ctrl+V) — attach it inline too.
        if not self.pending_attachments:
            from . import clipboard  # noqa: PLC0415
            p = clipboard.looks_like_path(text)
            if p:
                try:
                    return clipboard.file_to_blocks(p)
                except (OSError, ValueError):
                    pass
            return text

        blocks: list[Any] = self._strip_placeholders_to_text(text)
        blocks.extend(block for _, block in self.pending_attachments)
        self.pending_attachments = []
        return blocks or [TextBlock(text=text)]

    def _strip_placeholders_to_text(self, text: str) -> list:
        """Remove ``[Image #N]`` placeholders from the typed text and return it as
        a (possibly empty) ``[TextBlock]`` list."""
        from .types import TextBlock  # noqa: PLC0415

        cleaned = text
        for placeholder, _ in self.pending_attachments:
            cleaned = cleaned.replace(placeholder, "")
        cleaned = " ".join(cleaned.split()).strip()
        return [TextBlock(text=cleaned)] if cleaned else []

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

        from .events import (  # noqa: PLC0415
            ContentBlockDelta,
            MessageStart,
            TextDelta,
        )

        base = len(self.messages)
        self.messages.append(UserMessage(content=self._build_user_content(text)))

        self.console.print()  # breathing room above the loading spinner
        thinking = _Thinking()
        thinking.start()
        self._turn_streamed = False

        # Live token streaming: run_iter only yields whole messages, so we tap
        # the raw stream events to print assistant text token-by-token as it
        # generates. On the first token of a turn we kill the spinner and lay
        # down the "●" bullet; ``_render_assistant`` then skips re-printing that
        # text (it's already on screen) and renders only the tool calls.
        live = {"active": False}

        def _sink(ev: Any) -> None:
            if isinstance(ev, MessageStart):
                live["active"] = False
            elif isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, TextDelta):
                if not live["active"]:
                    thinking.stop_sync()
                    self.console.print(f"[{BODY}]●[/] ", end="")
                    live["active"] = True
                    self._turn_streamed = True
                self.console.print(
                    ev.delta.text, end="", markup=False, highlight=False
                )

        self.agent.on_event = _sink
        try:
            async for msg in self.agent.run_iter(self.messages):
                await thinking.stop()
                hugging = False
                if isinstance(msg, AssistantMessage):
                    # A tool call is immediately followed by its result; keep
                    # them hugged (no blank/spinner gap between call and result).
                    hugging = self._render_assistant(msg, ToolUseBlock)
                elif isinstance(msg, UserMessage) and not getattr(msg, "isMeta", False):
                    self._render_tool_results(msg, ToolResultBlock)
                if not hugging:
                    self.console.print()  # space above the next thinking spinner
                thinking.start()
            self._persist_messages(base)  # append this turn to the on-disk transcript
        except KeyboardInterrupt:
            del self.messages[base:]
            await thinking.stop()
            self.console.print("\n[ansibrightblack](interrupted)[/]")
            raise
        except Exception:
            del self.messages[base:]
            raise
        finally:
            self.agent.on_event = None
            await thinking.stop()

    def _persist_messages(self, base: int) -> None:
        """Append every message added this turn to the session transcript (the
        parent_uuid tree that /resume + /branch read back). Best-effort: a disk
        hiccup must never break the chat."""
        if self.transcript is None:
            return
        from .types import AssistantMessage, TextBlock, UserMessage  # noqa: PLC0415

        try:
            for m in self.messages[base:]:
                if isinstance(m, UserMessage) and not getattr(m, "isMeta", False):
                    self.transcript.append_message("user", m.content)
                    if base == len(self.messages) - len(self.messages[base:]):
                        # record the first user prompt for the resume picker
                        text = m.content if isinstance(m.content, str) else next(
                            (b.text for b in m.content if isinstance(b, TextBlock)), "")
                        if text:
                            self.transcript.record_last_prompt(text[:200])
                elif isinstance(m, AssistantMessage):
                    self.transcript.append_message("assistant", list(m.content))
        except Exception:  # noqa: BLE001
            pass

    # -- session commands: /resume /branch /rewind --------------------------

    async def _cmd_resume(self, arg: str) -> None:
        """List past sessions (or load one by number/id). ``/resume`` shows the
        picker; ``/resume <n>`` or ``/resume <id>`` loads it."""
        from .session_tree import (  # noqa: PLC0415
            SessionTranscript,
            list_sessions,
            load_for_resume,
        )

        sessions = [s for s in list_sessions()
                    if not (self.transcript and s.session_id == self.transcript.session_id)]
        if not sessions:
            self.console.print("[ansibrightblack]no past conversations to resume[/]")
            return
        if not arg:
            self.console.print("\n[bold]Resume a conversation[/]")
            for i, s in enumerate(sessions[:20], 1):
                title = s.title or s.first_prompt or "(untitled)"
                self.console.print(
                    f"  [white]{i:2}[/] {title[:60]}  "
                    f"[ansibrightblack]· {s.message_count} msgs[/]"
                )
            self.console.print("[ansibrightblack]→ /resume <number> to load[/]\n")
            return
        target = None
        if arg.isdigit() and 1 <= int(arg) <= len(sessions):
            target = sessions[int(arg) - 1]
        else:
            target = next((s for s in sessions if s.session_id.startswith(arg)), None)
        if target is None:
            self.console.print(f"[ansired]no session matching {arg!r}[/]")
            return
        self.messages = load_for_resume(target.session_id)
        self.transcript = SessionTranscript(target.session_id)
        self.console.print(
            f"[ansibrightblack]resumed[/] [white]{target.title or target.first_prompt or target.session_id[:8]}[/]"
            f" [ansibrightblack]({len(self.messages)} messages)[/]"
        )

    def _cmd_branch(self) -> None:
        """Fork the current conversation into a new session (the original stays
        resumable). Continues live in the new branch."""
        from .session_tree import SessionTranscript, branch_session  # noqa: PLC0415

        if self.transcript is None or not self.messages:
            self.console.print("[ansibrightblack]nothing to branch yet[/]")
            return
        try:
            fork_id = branch_session(self.transcript.session_id)
        except ValueError as e:
            self.console.print(f"[ansired]{e}[/]")
            return
        original = self.transcript.session_id
        self.transcript = SessionTranscript(fork_id)
        self.console.print(
            f"[ansibrightblack]branched → new session[/] [white]{fork_id[:8]}[/]"
            f"  [ansibrightblack](resume the original with[/] [white]/resume {original[:8]}[/][ansibrightblack])[/]"
        )

    def _cmd_rewind(self, arg: str) -> None:
        """Rewind the conversation to an earlier user message. ``/rewind`` lists
        them; ``/rewind <n>`` truncates to that point."""
        from .types import TextBlock, UserMessage  # noqa: PLC0415

        user_turns = [
            (i, m) for i, m in enumerate(self.messages)
            if isinstance(m, UserMessage) and not getattr(m, "isMeta", False)
            and isinstance(m.content, (str, list))
        ]
        # keep only real prompts (string content or has a TextBlock)
        prompts = []
        for i, m in user_turns:
            if isinstance(m.content, str):
                prompts.append((i, m.content))
            else:
                t = next((b.text for b in m.content if isinstance(b, TextBlock)), "")
                if t:
                    prompts.append((i, t))
        if not prompts:
            self.console.print("[ansibrightblack]nothing to rewind to[/]")
            return
        if not arg or not arg.isdigit():
            self.console.print("\n[bold]Rewind to[/]")
            for n, (_, text) in enumerate(prompts, 1):
                self.console.print(f"  [white]{n:2}[/] {text[:60]}")
            self.console.print("[ansibrightblack]→ /rewind <number>[/]\n")
            return
        n = int(arg)
        if not (1 <= n <= len(prompts)):
            self.console.print(f"[ansired]pick 1–{len(prompts)}[/]")
            return
        idx, _ = prompts[n - 1]
        self.messages = self.messages[:idx]
        self.console.print(
            f"[ansibrightblack]rewound to message {n} ({len(self.messages)} kept)[/]"
        )

    def _render_assistant(self, msg: Any, ToolUseBlock: type) -> bool:
        """Render an assistant message; return True if it emitted a tool call
        (so the caller can keep the call and its result visually hugged)."""
        from .types import TextBlock  # noqa: PLC0415

        # If this turn's text was streamed live (token-by-token), it's already on
        # screen — just close the line and skip re-printing it; still render the
        # tool calls below.
        streamed = self._turn_streamed
        self._turn_streamed = False
        if streamed:
            self.console.print()

        # No leading blank lines here: the single blank that separates blocks is
        # emitted before the (transient) thinking spinner in the run loop, and
        # the rendered content lands on the spinner's just-cleared line. Adding
        # blanks here too would double the spacing.
        had_tool_call = False
        for block in msg.content:
            if isinstance(block, TextBlock):
                if streamed:
                    continue  # already shown live via the streaming sink
                if block.text.strip():
                    self.console.print(f"[{BODY}]●[/] ", end="")
                    # Tight markdown: code fences, bold, lists, tables — without
                    # the big vertical margins rich adds around code by default.
                    self.console.print(_compact_markdown(block.text.strip()))
            elif isinstance(block, ToolUseBlock):
                from rich.text import Text as _T  # noqa: PLC0415

                had_tool_call = True
                verb, target = self._tool_label(block.name, block.input or {})
                line = _T()
                line.append("⚒ ", style=LEG)
                line.append(verb, style="bold white")
                if target:
                    line.append(" " + target, style="bright_black")
                self.console.print(line)
        return had_tool_call

    def _render_tool_results(self, msg: Any, ToolResultBlock: type) -> None:
        from rich.text import Text as _T  # noqa: PLC0415

        for block in msg.content:
            if not isinstance(block, ToolResultBlock):
                continue
            body = block.content if isinstance(block.content, str) else str(block.content)
            lines = body.strip().splitlines() or ["(no output)"]
            colour = "red" if block.is_error else "bright_black"

            rest = lines[1:]
            is_diff = any(ln.startswith("@@") for ln in rest)

            # First line hangs off a "└" branch; the rest is indented beneath it.
            head = _T()
            head.append("  └ ", style=LEG if not block.is_error else "red")
            if is_diff:
                # Claude-Code-style summary: "<file>  +N -M".
                import re as _re  # noqa: PLC0415

                added = sum(1 for ln in rest if ln.startswith("+"))
                removed = sum(1 for ln in rest if ln.startswith("-"))
                path = None
                pm = _re.search(r"(?:to|edited|wrote.*?to|edits to)\s+(\S+)", lines[0]) \
                    or _re.search(r"(\S+\.\w+)", lines[0])
                path = pm.group(1) if pm else lines[0]
                head.append(str(path), style="white")
                head.append("  +", style="bright_black")
                head.append(str(added), style="green")
                head.append(" -", style="bright_black")
                head.append(str(removed), style="red")
                self.console.print(head)
                self._render_diff(rest, path=str(path))
                continue

            head.append(lines[0][:200], style=colour)
            self.console.print(head)
            for ln in rest[:12]:
                self.console.print(_T("    " + ln[:200], style=colour))
            if len(rest) > 12:
                hint = _T(f"    … +{len(rest) - 12} more lines ", style="bright_black")
                hint.append("(ctrl+o to expand)", style="bright_black")
                self.console.print(hint)

    def _show_transcript(self) -> None:
        """Ctrl+O expand view: the whole conversation through the system pager
        with every tool output shown in FULL (no 12-line truncation)."""
        from rich.markup import escape as _esc  # noqa: PLC0415
        from rich.text import Text as _T  # noqa: PLC0415

        from .types import (  # noqa: PLC0415
            AssistantMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        if not self.messages:
            self.console.print("[bright_black](no conversation yet)[/]")
            return
        with self.console.pager(styles=True):
            self.console.print("[bold]Transcript[/]  [bright_black](q to close)[/]\n")
            for m in self.messages:
                if isinstance(m, UserMessage):
                    if getattr(m, "isMeta", False):
                        continue
                    if isinstance(m.content, str):
                        self.console.print(f"[bright_black]›[/] {_esc(m.content)}")
                    else:
                        for b in m.content:
                            if isinstance(b, ToolResultBlock):
                                body = b.content if isinstance(b.content, str) else str(b.content)
                                style = "red" if b.is_error else "bright_black"
                                for ln in body.rstrip().splitlines() or ["(no output)"]:
                                    self.console.print(_T("  " + ln, style=style))
                            elif isinstance(b, TextBlock):
                                self.console.print(f"[bright_black]›[/] {_esc(b.text)}")
                elif isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            self.console.print(f"[{BODY}]●[/] {_esc(b.text.strip())}")
                        elif isinstance(b, ToolUseBlock):
                            verb, target = self._tool_label(b.name, b.input or {})
                            self.console.print(
                                f"[{LEG}]⚒[/] [bold white]{verb}[/] "
                                f"[bright_black]{_esc(target)}[/]"
                            )
                self.console.print()

    def _hl_code(self, code: str, lang: str | None) -> Any:
        """Syntax-highlight one line of code → a rich ``Text`` with foreground-only
        token styles (backgrounds stripped, so a diff row's bg shows through).
        Falls back to plain text if no language or highlighting fails."""
        from rich.text import Text as _T  # noqa: PLC0415

        if not lang or not code.strip():
            return _T(code)
        try:
            from rich.style import Style  # noqa: PLC0415
            from rich.syntax import Syntax  # noqa: PLC0415
            from rich.text import Span  # noqa: PLC0415

            syn = self._syntax_cache.get(lang)
            if syn is None:
                syn = Syntax("", lang, theme="ansi_dark")
                self._syntax_cache[lang] = syn
            t = syn.highlight(code)
            t.rstrip()  # drop the trailing newline highlight() adds

            def _fg_only(style: Any) -> Any:
                s = Style.parse(style) if isinstance(style, str) else style
                return Style(color=s.color, bold=s.bold, italic=s.italic,
                             dim=s.dim, underline=s.underline)

            t.style = None
            t.spans = [Span(sp.start, sp.end, _fg_only(sp.style)) for sp in t.spans]
            return t
        except Exception:  # noqa: BLE001
            return _T(code)

    def _render_diff(self, diff_lines: list[str], path: str | None = None) -> None:
        """Render a unified diff like Claude Code — and a touch cleaner:
        FULL-WIDTH green/red background rows with **syntax-highlighted code**,
        a marker + right-aligned line-number gutter, and dim context lines.
        Additions show new-file line numbers, deletions show old-file numbers."""
        import re  # noqa: PLC0415

        from rich.text import Text as _T  # noqa: PLC0415

        if not hasattr(self, "_syntax_cache"):
            self._syntax_cache: dict[str, Any] = {}
        lang = _lang_from_path(path)

        # Claude Code's exact diff palette (theme.ts): diffAdded rgb(105,219,124)
        # and diffRemoved rgb(255,168,180) for the bright gutter marker/number;
        # those colours blended dark over black for the row fill; the "dimmed"
        # variants as the fallback code fg when a line isn't syntax-highlighted.
        ADD_BG, DEL_BG = "#15331b", "#3a2226"    # row fill (dark green / dark red)
        ADD_NUM, DEL_NUM = "#69db7c", "#ffa8b4"  # bright marker + line number
        ADD_FG, DEL_FG = "#c7e1cb", "#fdd2d8"    # dimmed fallback code fg
        ADD_WORD, DEL_WORD = "#2f9d44", "#d1454b"  # changed-word emphasis (theme.ts)
        width = max(40, self.console.width)
        indent = 2
        old_ln = new_ln = 0

        # Precompute changed-char spans for modified line pairs: within a change
        # block (a run of '-' lines then a run of '+' lines) pair the i-th of
        # each and word-diff them, so a one-char edit highlights one char.
        emphasis: dict[int, list[tuple[int, int]]] = {}
        i = 0
        while i < len(diff_lines):
            if diff_lines[i].startswith("-"):
                dels, j = [], i
                while j < len(diff_lines) and diff_lines[j].startswith("-"):
                    dels.append(j)
                    j += 1
                adds = []
                while j < len(diff_lines) and diff_lines[j].startswith("+"):
                    adds.append(j)
                    j += 1
                for di, ai in zip(dels, adds):
                    os_, ns_ = _word_diff_spans(diff_lines[di][1:], diff_lines[ai][1:])
                    if os_ or ns_:
                        emphasis[di], emphasis[ai] = os_, ns_
                i = j
            else:
                i += 1

        def _row(num: int, marker: str, code: str, bg: str, num_col: str, fg: str,
                 word_bg: str | None = None, spans: list[tuple[int, int]] | None = None) -> None:
            gutter = f"{num:>4}  {marker} "
            avail = max(4, width - indent - len(gutter))
            code = code[:avail]
            row = _T(" " * indent)
            row.append(gutter, style=f"{num_col} on {bg}")
            ct = self._hl_code(code, lang)
            if not ct.spans:           # not syntax-highlighted → use the dimmed fg
                ct.stylize(fg)
            ct.stylize(f"on {bg}")     # layer the row background over the fg
            if word_bg and spans:      # brighten just the changed characters
                for s, e in spans:
                    if s < len(code):
                        ct.stylize(f"on {word_bg}", s, min(e, len(code)))
            row.append_text(ct)
            filled = indent + len(gutter) + len(ct)
            if filled < width:
                row.append(" " * (width - filled), style=f"on {bg}")  # fill to edge
            self.console.print(row)

        for idx, ln in enumerate(diff_lines):
            if ln.startswith("@@"):
                m = re.match(r"@@ -(\d+)\D.*\+(\d+)", ln) or re.match(r"@@ -(\d+) \+(\d+)", ln)
                if m:
                    old_ln, new_ln = int(m.group(1)), int(m.group(2))
                continue
            if ln.startswith("+"):
                _row(new_ln, "+", ln[1:], ADD_BG, ADD_NUM, ADD_FG, ADD_WORD, emphasis.get(idx))
                new_ln += 1
            elif ln.startswith("-"):
                _row(old_ln, "-", ln[1:], DEL_BG, DEL_NUM, DEL_FG, DEL_WORD, emphasis.get(idx))
                old_ln += 1
            else:  # context line (leading space) — dim, no background block
                code = ln[1:] if ln.startswith(" ") else ln
                self.console.print(_T(f"{' ' * indent}{new_ln:>4}    {code}"[:width], style="bright_black"))
                old_ln += 1
                new_ln += 1

        # If the agent just updated its todos via ``todo_write``, draw the
        # checklist so the user can watch multi-step progress (Claude-Code style).
        if self.todos and self.todos != self._todos_shown:
            self._render_todos()
            self._todos_shown = [dict(t) for t in self.todos]

    def _render_todos(self) -> None:
        from rich.text import Text as _T  # noqa: PLC0415

        glyph = {"completed": ("✔", "green"),
                 "in_progress": ("▶", BODY),
                 "pending": ("○", "bright_black")}
        self.console.print()
        for t in self.todos:
            mark, colour = glyph.get(t.get("status", "pending"), ("○", "bright_black"))
            active = t.get("status") == "in_progress"
            label = t.get("activeForm" if active else "content", t.get("content", ""))
            row = _T()
            row.append(f"  {mark} ", style=colour)
            row.append(label, style=(f"bold {BODY}" if active else colour))
            self.console.print(row)

    @staticmethod
    def _tool_label(name: str, args: dict[str, Any]) -> tuple[str, str]:
        """Map a tool call to a friendly (verb, target) — e.g. ("Read", "foo.py")."""
        verb, keys = TOOL_VERBS.get(name, (name, ("path", "command", "query", "pattern", "url")))
        target = ""
        for k in keys:
            if args.get(k):
                target = str(args[k])
                break
        target = " ".join(target.split())  # collapse whitespace/newlines
        if len(target) > 64:
            target = target[:63] + "…"
        return verb, target

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
            # Blank the screen (and scrollback) and redraw the banner — a clean
            # fresh start, like relaunching mantis.
            self.messages = []
            try:
                sys.stdout.write("\033[2J\033[3J\033[H")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
            print_banner(self.console, self.model, self.backend)
            return True
        if cmd == "/cwd":
            self.console.print(f"[ansibrightblack]{Path.cwd()}[/]")
            return True
        if cmd == "/resume":
            await self._cmd_resume(arg)
            return True
        if cmd == "/branch":
            self._cmd_branch()
            return True
        if cmd == "/rewind":
            self._cmd_rewind(arg)
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
        """Inline picker with **type-to-filter**. ``items`` are header rows
        ({"kind":"header","text"}) or selectable rows
        ({"kind":"item","label","value","hint"?,"enabled"?}). Arrow/Ctrl-N/P
        move, typing narrows by label substring (headers with no match hide),
        Backspace/Ctrl-U edit the filter, Enter picks, Esc cancels. Returns the
        chosen item dict or None. Non-full-screen + erase_when_done."""
        from prompt_toolkit.application import Application  # noqa: PLC0415
        from prompt_toolkit.data_structures import Point  # noqa: PLC0415
        from prompt_toolkit.key_binding import KeyBindings  # noqa: PLC0415
        from prompt_toolkit.keys import Keys  # noqa: PLC0415
        from prompt_toolkit.layout import HSplit, Layout, Window  # noqa: PLC0415
        from prompt_toolkit.layout.controls import FormattedTextControl  # noqa: PLC0415
        from prompt_toolkit.layout.dimension import D  # noqa: PLC0415
        from prompt_toolkit.styles import Style  # noqa: PLC0415

        if not any(it.get("kind") == "item" for it in items):
            self.console.print("[ansibrightblack]nothing to pick[/]")
            return None
        st = {"sel": max(0, start_index), "filter": ""}

        def build() -> tuple[list, list]:
            """(rows, sel_rows) for the current filter. rows = [(item, is_item)];
            headers are kept only when ≥1 item under them survives the filter."""
            f = st["filter"].lower()
            rows: list = []
            pending: dict | None = None
            buf: list = []

            def flush() -> None:
                matches = [it for it in buf if not f or f in it["label"].lower()]
                if matches:
                    if pending is not None:
                        rows.append((pending, False))
                    rows.extend((it, True) for it in matches)

            for it in items:
                if it.get("kind") == "item":
                    buf.append(it)
                else:
                    flush()
                    pending, buf = it, []
            flush()
            sel_rows = [i for i, (_it, is_item) in enumerate(rows) if is_item]
            return rows, sel_rows

        def cur_state() -> tuple[list, list, int]:
            rows, sel_rows = build()
            if not sel_rows:
                return rows, sel_rows, -1
            st["sel"] = max(0, min(st["sel"], len(sel_rows) - 1))
            return rows, sel_rows, sel_rows[st["sel"]]

        def frags() -> list:
            rows, sel_rows, cur_row = cur_state()
            out: list = []
            if not sel_rows:
                return [("class:hint", "  no matches — backspace to widen\n")]
            for idx, (it, is_item) in enumerate(rows):
                if not is_item:
                    out.append(("class:hdr", it["text"] + "\n"))
                    continue
                ptr = "❯ " if idx == cur_row else "  "
                cls = "class:cur" if idx == cur_row else ("class:on" if it.get("enabled", True) else "class:off")
                out.append((cls, f"{ptr}{it['label']}"))
                if it.get("hint"):
                    out.append(("class:hint", f"   {it['hint']}"))
                out.append(("", "\n"))
            return out

        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _u(_e: Any) -> None:
            _, sel_rows = build()
            if sel_rows:
                st["sel"] = (st["sel"] - 1) % len(sel_rows)

        @kb.add("down")
        @kb.add("c-n")
        def _d(_e: Any) -> None:
            _, sel_rows = build()
            if sel_rows:
                st["sel"] = (st["sel"] + 1) % len(sel_rows)

        @kb.add("enter")
        def _ok(e: Any) -> None:
            rows, sel_rows, cur_row = cur_state()
            if sel_rows:
                e.app.exit(result=rows[cur_row][0])

        @kb.add("escape")
        @kb.add("c-c")
        def _no(e: Any) -> None:
            e.app.exit(result=None)

        @kb.add("backspace")
        def _bs(_e: Any) -> None:
            st["filter"] = st["filter"][:-1]
            st["sel"] = 0

        @kb.add("c-u")
        @kb.add("c-w")
        def _clr(_e: Any) -> None:
            st["filter"] = ""
            st["sel"] = 0

        @kb.add(Keys.Any)
        def _type(e: Any) -> None:
            ch = e.data
            if ch and len(ch) == 1 and ch.isprintable():
                st["filter"] += ch
                st["sel"] = 0

        control = FormattedTextControl(
            frags, focusable=True, show_cursor=False,
            get_cursor_position=lambda: Point(0, max(0, cur_state()[2])),
        )

        def title_text() -> list:
            f = st["filter"]
            return [("class:title", title),
                    ("class:filter", f"   ❯ {f}▏" if f else "")]

        head = Window(FormattedTextControl(title_text), height=1)
        body = Window(control, height=D(max=20), wrap_lines=False)
        style = Style.from_dict({
            "title": "#888888", "filter": "#c6e79a bold", "hdr": f"{BODY} bold",
            "cur": f"#0b1605 bg:{BODY} bold", "on": "#ffffff",
            "off": "#777777", "hint": "#777777",
        })
        app: Any = Application(
            layout=Layout(HSplit([head, body])), key_bindings=kb, style=style,
            full_screen=False, erase_when_done=True, mouse_support=True,
        )
        return await app.run_async()

    # -- model catalog & selection ------------------------------------------

    def _kick_prewarm(self) -> None:
        """Fire-and-forget refresh of enabled providers' live model lists, so the
        selector (which only reads the on-disk cache) opens instantly."""
        import asyncio  # noqa: PLC0415

        try:
            asyncio.ensure_future(self._prewarm_live_models())
        except RuntimeError:
            pass  # no running loop (e.g. unit test) — fine

    async def _prewarm_live_models(self) -> None:
        """Refresh stale/missing live model lists off the event loop (catalog
        does the HTTP + disk-persist with a 24h TTL)."""
        import anyio  # noqa: PLC0415

        from . import catalog  # noqa: PLC0415

        targets = [p for p in catalog.CATALOG
                   if catalog.is_enabled(p) and catalog.cached_live_models(p.id) is None]
        if not targets:
            return

        async def _go(prov: Any) -> None:
            try:
                await anyio.to_thread.run_sync(catalog.refresh_live_models, prov)
            except Exception:  # noqa: BLE001
                pass

        async with anyio.create_task_group() as tg:
            for prov in targets:
                tg.start_soon(_go, prov)

    def _catalog_rows(self) -> list[dict]:
        """Picker rows: local Ollama first, then every hosted provider. Enabled
        providers show their live model list; disabled ones the curated menu."""
        from . import catalog  # noqa: PLC0415

        installed, reachable = self._available_models()
        rows: list[dict] = []

        # Recent — a shortcut to the models you've used lately (they also appear
        # under their provider below; this just floats them to the top).
        recent = catalog.get_recent_models()
        if recent:
            rows.append({"kind": "header", "text": "  Recent"})
            for m in recent:
                prov = catalog.provider_for_model(m)
                hint = "← current" if m == self.model else (prov.label if prov else "local")
                rows.append({"kind": "item", "label": m, "enabled": True,
                             "value": {"model": m, "provider": prov}, "hint": hint})

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
            extra = "  · enabled" if on else "  · disabled — enter to set up"
            rows.append({"kind": "header", "text": f"  {prov.label}{extra}"})
            # Curated flagship models first (clean + current); then append any
            # extra *chat* models the live endpoint reports, junk filtered out.
            models = list(prov.models)
            if on:
                live = catalog.cached_live_models(prov.id)
                if live:
                    for m in live:
                        if _is_chat_model(m) and m not in models:
                            models.append(m)
            models = models[:12]
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
        set it up. Proprietary models go straight to the provider key;
        open-weight models also offer self-host."""
        from . import catalog  # noqa: PLC0415

        if prov is None:
            await self._apply(model, DEFAULT_BACKEND, None, "")
            return
        if catalog.is_enabled(prov):
            await self._apply(model, prov.base_url, catalog.api_key_for(prov),
                              f" [ansibrightblack]via {prov.label}[/]")
            return

        mode = "api"
        if _is_open_weight(model):
            choice = await self._pick(
                f"run {model} — how?",
                [
                    {"kind": "header", "text": f"  {model}  ·  open-weight"},
                    {"kind": "item", "label": f"{prov.label} API", "value": "api",
                     "hint": f"paste {prov.api_key_env} · {prov.label} runs it"},
                    {"kind": "item", "label": "Self-host", "value": "selfhost",
                     "hint": "run the open weights on your own server (vLLM/llama.cpp/TGI)"},
                    {"kind": "item", "label": "Cancel", "value": "cancel"},
                ],
            )
            if not choice or choice["value"] == "cancel":
                self.console.print("[ansibrightblack](cancelled)[/]")
                return
            mode = choice["value"]

        if mode == "selfhost":
            url = await self._prompt_text(
                f"self-host URL serving {model}", "url › ", "http://localhost:8000/v1")
            if not url or not url.startswith(("http://", "https://")):
                self.console.print("[ansibrightblack](cancelled — need an http(s) url)[/]")
                return
            await self._apply(model, url, self.api_key or "sk-noauth",
                              " [ansibrightblack]· self-hosted[/]")
            return

        # API path — proprietary models always land here; open-weight if chosen.
        key = await self._prompt_secret(
            f"paste {prov.label} API key ({prov.api_key_env})", "key › ")
        if not key:
            self.console.print("[ansibrightblack](cancelled)[/]")
            return
        catalog.set_key(prov.id, key)
        self._kick_prewarm()
        self.console.print(
            f"[ansigreen]✓[/] enabled [bold]{prov.label}[/] "
            "[ansibrightblack](saved to ~/.mantis-agent/models.json, chmod 600)[/]")
        await self._validate_and_report(prov)
        await self._apply(model, prov.base_url, key,
                          f" [ansibrightblack]via {prov.label}[/]")

    async def _validate_and_report(self, prov: Any) -> None:
        """Check a freshly-saved key off-thread and report pass/fail inline."""
        import anyio  # noqa: PLC0415

        from . import catalog  # noqa: PLC0415

        ok, detail = await anyio.to_thread.run_sync(catalog.validate_provider, prov)
        if ok:
            self.console.print(f"  [ansigreen]✓[/] [ansibrightblack]{prov.label}: {detail}[/]")
        else:
            self.console.print(
                f"  [ansiyellow]![/] [ansibrightblack]{prov.label}: {detail} — "
                f"saved anyway; re-key with [white]/enable {prov.id}[/][/]")

    async def _apply(self, model: str, backend: str, api_key: str | None, where: str) -> None:
        """Point the live agent at (model, backend, key) and rebuild it."""
        from . import catalog  # noqa: PLC0415

        self.model = model
        self.backend = backend
        self.api_key = api_key
        if self.agent is not None:
            await self.agent.aclose()
        self.agent = self._build_agent()
        catalog.set_last_model(model, backend)  # reopen here next launch
        catalog.push_recent_model(model)  # float to the top of /models next time
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
                self._kick_prewarm()
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
        self._kick_prewarm()
        self.console.print(
            f"[ansigreen]✓[/] enabled [bold]{prov.label}[/] "
            f"[ansibrightblack](saved, chmod 600)[/]  "
            f"[ansibrightblack]try [white]/model {prov.models[0]}[/][/]")
        await self._validate_and_report(prov)

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
        self._restore_last_model()  # reopen on last session's model (if no override)
        self._resolve_model()  # so the banner + first turn use a model that exists
        # Full reset BEFORE drawing: clear the screen AND scrollback and home the
        # cursor, so the banner starts at the very top with nothing above it (the
        # shell prompt that launched us included). The input sits right beneath
        # the banner and the conversation flows downward from there. We do NOT
        # pad the prompt to the terminal floor: that buries the first message in
        # a wall of blank lines when output scrolls. (True always-bottom input
        # needs a full-screen app — see the REPL notes.)
        try:
            sys.stdout.write("\033[2J\033[3J\033[H")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001 - non-tty / dumb terminal
            pass
        banner_h = print_banner(self.console, self.model, self.backend)
        # Push the first input toward the bottom so its framing rules + footer
        # hug it instead of the toolbar floating to the screen floor. Safe now
        # that erase_when_done wipes the frame and we echo "› message" in place,
        # so the first message scrolls naturally rather than getting buried.
        import shutil  # noqa: PLC0415

        rows = shutil.get_terminal_size((80, 24)).lines
        for _ in range(max(0, rows - banner_h - 3)):  # 3 = top rule + input + footer-ish
            self.console.print()
        session = self._build_session()
        self.session = session
        self.agent = self._build_agent()
        if self.transcript is None:
            from .session_tree import SessionTranscript, new_session_id  # noqa: PLC0415
            self.transcript = SessionTranscript(new_session_id())
        self._kick_prewarm()

        from prompt_toolkit.formatted_text import HTML  # noqa: PLC0415
        from rich.markup import escape as _esc  # noqa: PLC0415

        try:
            while True:
                try:
                    # Frame ONLY the live input: a rule above it (in the prompt
                    # message) and a rule below it (the toolbar's first line),
                    # all inside prompt_toolkit's render. erase_when_done wipes
                    # the whole frame on submit, then we echo a clean "› message"
                    # — so past turns keep no rules, only the current input does.
                    rule = "─" * self.console.width
                    message = HTML(
                        f"<ansibrightblack>{rule}</ansibrightblack>\n"
                        f"<ansibrightblack>›</ansibrightblack> "
                    )
                    line = await session.prompt_async(
                        message, placeholder=self._placeholder()
                    )
                except (EOFError, KeyboardInterrupt):
                    self.console.print("[ansibrightblack]bye 👋[/]")
                    break

                line = (line or "").strip()
                if not line:
                    continue
                # Echo the submitted line into the transcript (the framed input
                # itself was erased).
                self.console.print(f"[ansibrightblack]›[/] {_esc(line)}")
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

    if argv is None:
        argv = sys.argv[1:]

    # `mantis setup [...]` → the first-run wizard (detect machine, pick & pull a
    # coding model, set it as default). Everything else launches the terminal.
    if argv and argv[0] == "setup":
        from .setup_wizard import run_setup  # noqa: PLC0415

        return run_setup(argv[1:])

    p = argparse.ArgumentParser(
        prog="mantis",
        description="Mantis — interactive agent terminal. Run `mantis setup` first to install a model.",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help="Model slug.")
    p.add_argument("--backend", default=DEFAULT_BACKEND, help="Backend base URL.")
    p.add_argument("--api-key", default=os.environ.get("MANTIS_AGENT_API_KEY"),
                   help="API key (else env MANTIS_AGENT_API_KEY).")
    p.add_argument("--system", default=None, help="System prompt.")
    # Generous default: a 2048 cap truncates a single-tool-call file write
    # mid-content (the JSON never closes → "unterminated tool_call" → null
    # content → the model loops). 8192 lets it write a real file in one shot.
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--max-turns", type=int, default=20)
    # Start in bypass-permissions mode: every tool runs with NO confirmation
    # prompt (including dangerous shell commands). --godmode is a friendly alias.
    p.add_argument(
        "--dangerously-skip-permissions", action="store_true",
        help="Run every tool with no permission prompt (bypass mode). Dangerous.",
    )
    p.add_argument(
        "--godmode", action="store_true",
        help="Alias for --dangerously-skip-permissions.",
    )
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

    # Start in bypass-permissions mode if requested. force_bypass makes the
    # permission engine itself return Allow for EVERY tool (even dangerous shell
    # commands), so nothing prompts — true godmode. Warn loudly.
    if args.dangerously_skip_permissions or args.godmode:
        tui.force_bypass = True
        tui.mode_idx = [m[0] for m in MODES].index("bypass permissions on")
        print(
            "\033[31m⏵⏵ bypass permissions on — tools run with NO confirmation "
            "(godmode). Ctrl+C to quit.\033[0m",
            file=sys.stderr,
        )

    # Full-screen mode (input pinned to the bottom, always visible) is the
    # default; MANTIS_CLASSIC=1 forces the classic scrolling REPL. If full-screen
    # fails for any reason, fall back to classic so mantis is never broken.
    classic = os.environ.get("MANTIS_CLASSIC") == "1"
    if not classic:
        try:
            from .tui_fullscreen import run_fullscreen  # noqa: PLC0415

            return anyio.run(run_fullscreen, tui)
        except KeyboardInterrupt:
            return 130
        except Exception as e:  # noqa: BLE001
            print(
                f"[mantis] full-screen mode failed ({e!r}); falling back to the "
                f"classic REPL. Set MANTIS_CLASSIC=1 to skip full-screen.",
                file=sys.stderr,
            )
    try:
        return anyio.run(tui.run)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
