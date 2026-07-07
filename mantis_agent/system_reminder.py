"""``<system-reminder>`` — Claude Code's context-injection mechanism.

Audited from the Claude Agent SDK source (``src/utils/api.ts:463`` for
``prependUserContext``, ``src/utils/messages.ts:3098`` for the canonical
wrapper, and ``src/context.ts`` for the user/system context split). This
module ports the convention 1:1 so any tool that reads / writes Claude
SDK transcripts treats ours as equivalent.

The mechanism in one paragraph
------------------------------

Claude doesn't paste persistent context (the ``MEMORY.md`` index, project
``CLAUDE.md``, etc.) into the *system prompt*. It injects a synthetic
**user** message wrapped in ``<system-reminder>...</system-reminder>``
tags at the very head of the conversation, with ``isMeta=true`` so it
doesn't count toward visible turn counts. The same mechanism is reused
for: live-context-at-turn-start ("[Live context — at turn start]"),
file-change notifications, side-question wrappers, attachment surfacing,
and idle-time prompts.

The model is trained to treat ``<system-reminder>`` content as ambient
context it MAY use, not as the user's actual request. The canonical
preamble line is::

    As you answer the user's questions, you can use the following context:

…followed by ``# {key}\\n{value}`` sections, then a closing instruction::

    IMPORTANT: this context may or may not be relevant to your tasks.
    You should not respond to this context unless it is highly relevant
    to your task.

Why a separate module
---------------------

Two reasons: (1) downstream consumers want stable APIs for detecting +
stripping these wrappers (e.g. transcript search), and (2) the agent
loop needs to prepend context per-session without polluting the system
prompt the user passed in.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from .types import UserMessage

__all__ = [
    "OPEN_TAG",
    "CLOSE_TAG",
    "USER_CONTEXT_PREAMBLE",
    "USER_CONTEXT_TRAILER",
    "build_env_context_block",
    "build_git_context",
    "build_live_context_block",
    "is_system_reminder",
    "render_environment_context",
    "prepend_user_context",
    "render_user_context",
    "strip_system_reminders",
    "wrap_system_reminder",
]


# ---------------------------------------------------------------------------
# Constants (verbatim from Claude SDK source)
# ---------------------------------------------------------------------------

OPEN_TAG = "<system-reminder>"
CLOSE_TAG = "</system-reminder>"

USER_CONTEXT_PREAMBLE = (
    "As you answer the user's questions, you can use the following context:"
)

# Whitespace before "IMPORTANT" is verbatim from upstream — kept so token-
# level diff tooling matches Claude's output byte-for-byte.
USER_CONTEXT_TRAILER = (
    "      IMPORTANT: this context may or may not be relevant to your tasks. "
    "You should not respond to this context unless it is highly relevant to "
    "your task."
)


# Detect entire-message-is-a-reminder (matches Claude's
# ``betaSessionTracing.ts:143`` regex).
_FULL_REMINDER_RE = re.compile(
    r"^<system-reminder>\n?([\s\S]*?)\n?</system-reminder>$"
)

# Strip embedded reminders from a text block (matches
# ``queryHelpers.ts:432`` regex).
_EMBEDDED_REMINDER_RE = re.compile(
    r"<system-reminder>[\s\S]*?</system-reminder>"
)


# ---------------------------------------------------------------------------
# Wrap / detect / strip
# ---------------------------------------------------------------------------


def wrap_system_reminder(content: str) -> str:
    """Wrap ``content`` in ``<system-reminder>...</system-reminder>``.

    Newlines around the content match Claude's canonical form:
    ``<system-reminder>\\ncontent\\n</system-reminder>``.
    """

    if not content:
        return ""
    return f"{OPEN_TAG}\n{content.rstrip()}\n{CLOSE_TAG}"


def is_system_reminder(text: str) -> bool:
    """True if ``text`` is *entirely* a system-reminder block."""

    if not text:
        return False
    return _FULL_REMINDER_RE.match(text.strip()) is not None


def strip_system_reminders(text: str) -> str:
    """Remove all ``<system-reminder>...</system-reminder>`` blocks from ``text``.

    Mirrors ``queryHelpers.ts:432`` and ``transcriptSearch.ts:117``.
    Used by transcript-search / token-counting code that should not see
    ambient context as user-authored content.
    """

    if not text:
        return ""
    return _EMBEDDED_REMINDER_RE.sub("", text)


# ---------------------------------------------------------------------------
# User-context rendering (the canonical claudeMd / memory injection format)
# ---------------------------------------------------------------------------


def render_user_context(context: dict[str, str]) -> str:
    """Render a context dict to the canonical ``<system-reminder>`` body.

    ``context`` is ``{key: value}`` — e.g.
    ``{"memory": "...", "claudeMd": "...", "skills": "..."}``. Empty dict
    returns an empty string (caller decides whether to skip the wrap).
    """

    if not context:
        return ""
    sections = "\n".join(
        f"# {key}\n{value}" for key, value in context.items() if value
    )
    if not sections:
        return ""
    return f"{USER_CONTEXT_PREAMBLE}\n{sections}\n\n{USER_CONTEXT_TRAILER}"


def prepend_user_context(
    messages: list[UserMessage] | list,
    context: dict[str, str],
    *,
    in_place: bool = False,
) -> list:
    """Prepend a synthetic ``isMeta=True`` user message carrying the wrapped
    context. No-op when ``context`` is empty.

    Returns a new list (or mutates ``messages`` and returns it when
    ``in_place=True``). The synthetic message has ``isMeta=True`` so
    transcript readers can skip it when counting visible turns.
    """

    body = render_user_context(context)
    if not body:
        return messages if in_place else list(messages)

    wrapped = wrap_system_reminder(body)
    synthetic = UserMessage(content=wrapped, isMeta=True)
    if in_place:
        messages.insert(0, synthetic)
        return messages
    return [synthetic, *messages]


# ---------------------------------------------------------------------------
# Live-context-at-turn-start (the "[Live context — at turn start]" block)
# ---------------------------------------------------------------------------


def build_live_context_block(
    *,
    local_time: datetime | None = None,
    timezone_name: str | None = None,
    location: str | None = None,
    locale: str | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """Build the ``[Live context — at turn start]`` body that Claude's
    ``UserPromptSubmit`` hook prepends to each fresh user turn.

    Wrap the returned body in ``wrap_system_reminder()`` if you want to
    append it as a ``<system-reminder>`` block, or use it raw — Claude
    uses the literal ``[Live context — at turn start]`` header inside a
    system-reminder for the harness-injected variant.
    """

    parts: list[str] = ["[Live context — at turn start]"]

    if local_time is not None:
        # Match Claude's harness format: "Saturday, May 16, 2026 at 9:17 AM"
        formatted = local_time.strftime("%A, %B %d, %Y at %-I:%M %p")
        suffix = f" ({timezone_name})" if timezone_name else ""
        parts.append(f"- User's local time: {formatted}{suffix}")
    if location:
        parts.append(f"- User's location: {location}")
    if locale:
        parts.append(f"- User's locale: {locale}")
    if extra:
        for k, v in extra.items():
            parts.append(f"- {k}: {v}")

    parts.append(
        "\nUse this for reasoning (greetings, deadlines, local references), "
        "but don't restate it unless it's directly relevant to your reply."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Environment / git / directory snapshot — session-start facts the model needs
# to feel oriented in the repo. Composed into the isMeta context head (built
# once per session, so the prompt-cache prefix stays stable). Ports Claude
# Code's ``<env>`` block + git-status snapshot (constants/prompts.ts,
# context.ts:getGitStatus) closely, including the "snapshot in time" disclaimer.
# ---------------------------------------------------------------------------

_GIT_STATUS_MAX_CHARS = 2000


def build_env_context_block(
    *,
    cwd: str | None = None,
    now: datetime | None = None,
    platform_name: str | None = None,
    os_version: str | None = None,
    dir_entries: list[str] | None = None,
    is_git: bool | None = None,
    max_dir_entries: int = 40,
    model: str | None = None,
    backend: str | None = None,
) -> str:
    """Pure(ish). The ``<env>`` block: working directory, platform, OS version,
    today's date, and a shallow (non-recursive) directory listing. Every input
    is injectable so this is deterministic in tests; omitted values default to
    the real environment."""
    import os  # noqa: PLC0415
    import platform as _pf  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    now = now or datetime.now()
    platform_name = platform_name or _pf.system().lower()
    os_version = os_version or _pf.platform()

    if dir_entries is None:
        try:
            names: list[str] = []
            with os.scandir(cwd_path) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    names.append(e.name + ("/" if e.is_dir() else ""))
            dir_entries = sorted(names)[:max_dir_entries]
        except OSError:
            dir_entries = []

    lines = ["<env>", f"Working directory: {cwd_path}"]
    if is_git is not None:
        lines.append(f"Is directory a git repo: {'Yes' if is_git else 'No'}")
    lines += [
        f"Platform: {platform_name}",
        f"OS Version: {os_version}",
        f"Today's date: {now.strftime('%Y-%m-%d')}",
    ]
    # Identity: lets any model answer "what model are you / what backend" from
    # context instead of guessing or flailing at tools.
    if model:
        lines.append(f"You are the model: {model}" + (f" (served via {backend})" if backend else ""))
    lines.append("</env>")
    block = "\n".join(lines)
    if dir_entries:
        listing = "\n".join(f"- {n}" for n in dir_entries)
        block += f"\nDirectory (shallow):\n{listing}"
    return block


def _run_git(args: list[str], cwd: object) -> str | None:
    """The single monkeypatchable IO seam. Runs ``git <args>`` in ``cwd`` with a
    short timeout. Returns stripped stdout, or ``None`` on non-zero exit, missing
    git, not-a-repo, or timeout — so callers degrade gracefully."""
    import subprocess  # noqa: PLC0415

    try:
        r = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def build_git_context(cwd: str | None = None, *, run: object = None) -> str:
    """The git snapshot: branch, git user, short status (truncated), and the
    last 5 commits. Returns ``""`` when ``cwd`` isn't a git repo (first probe
    returns ``None``). ``run`` overrides the IO seam for tests."""
    from pathlib import Path  # noqa: PLC0415

    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    g = run or _run_git

    branch = g(["rev-parse", "--abbrev-ref", "HEAD"], cwd_path)
    if branch is None:
        return ""  # not a git repo / git unavailable

    parts = [
        "This is the git status at the start of the conversation. Note that "
        "this status is a snapshot in time, and will not update during the "
        "conversation.",
        f"Current branch: {branch}",
    ]
    main = g(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd_path)
    if main:
        parts.append(f"Main branch (usually for PRs): {main.removeprefix('origin/')}")
    user = g(["config", "user.name"], cwd_path)
    if user:
        email = g(["config", "user.email"], cwd_path)
        parts.append(f"Git user: {user} <{email}>" if email else f"Git user: {user}")
    status = g(["status", "--porcelain"], cwd_path)
    if status is not None:
        s = status if status.strip() else "(clean)"
        if len(s) > _GIT_STATUS_MAX_CHARS:
            s = s[:_GIT_STATUS_MAX_CHARS] + '\n... (truncated — run `git status` for the rest)'
        parts.append(f"Status:\n{s}")
    log = g(["log", "--oneline", "-5"], cwd_path)
    if log:
        parts.append(f"Recent commits:\n{log}")
    return "\n\n".join(parts)


def render_environment_context(
    cwd: str | None = None, now: datetime | None = None,
    model: str | None = None, backend: str | None = None,
) -> str:
    """Compose the ``<env>`` block + git snapshot into the single string that
    becomes the ``environment`` context key. Never raises — best-effort; always
    returns at least the ``<env>`` block even with no git."""
    try:
        git = build_git_context(cwd=cwd)
    except Exception:  # noqa: BLE001
        git = ""
    try:
        env = build_env_context_block(cwd=cwd, now=now, is_git=bool(git),
                                      model=model, backend=backend)
    except Exception:  # noqa: BLE001
        env = "<env>\n</env>"
    return env + (f"\n\n{git}" if git else "")
