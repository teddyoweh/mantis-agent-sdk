"""``mantis -p`` — the terminal without the terminal.

One prompt in, the final answer out, exit. The point of having this on
``mantis`` (rather than only on ``mantis-agent run``) is that it resolves the
model the same way an interactive session does — the one you last used, with
its provider key and backend already wired — so a script doesn't have to
repeat ``--model`` and ``--backend`` on every call.

The stream shapes and flag semantics deliberately match Claude Code's print
mode, because that's the vocabulary CI scripts are already written against:

    mantis -p "fix the failing test"
    mantis -p "summarize this repo" --output-format json | jq -r .result
    mantis -p "refactor" --output-format stream-json --verbose | while read l; …
    cat spec.md | mantis -p --godmode

Rules copied verbatim from that contract: ``stream-json`` requires
``--verbose``; ``json`` prints the single result object (the whole message
array with ``--verbose``); text prints the result with a trailing newline;
the exit code is 1 exactly when the result says ``is_error``.
"""

from __future__ import annotations

import json as _json
import os
import sys
from typing import Any

# stdin is a pipe from who-knows-what; a runaway producer shouldn't be able to
# make us buffer the machine to death. Claude Code caps print-mode stdin the
# same way.
_STDIN_LIMIT = 10 * 1024 * 1024

_ERROR_TEXT = {
    "error_max_turns": "Error: Reached max turns ({max_turns})",
    "error_max_budget_usd": "Error: Exceeded USD budget",
    "error_max_structured_output_retries":
        "Error: Failed to provide valid structured output after maximum retries",
}


def _read_stdin() -> str:
    """The piped prompt, or "" when stdin is a terminal (nothing piped)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
    except (ValueError, OSError):       # closed / detached stdin
        return ""
    try:
        return sys.stdin.read(_STDIN_LIMIT)
    except (OSError, UnicodeDecodeError):
        return ""


def resolve_prompt(parts: list[str] | None) -> str:
    """CLI words joined, or stdin when the prompt is absent or a bare ``-``.

    ``mantis -p "a" "b"`` reads as one prompt, so an unquoted sentence still
    works instead of silently dropping every word after the first."""
    text = " ".join(parts or []).strip()
    if not text or text == "-":
        return _read_stdin().strip()
    return text


def _split_tool_list(raw: str | None) -> list[str] | None:
    """``--allowed-tools "Bash,Read"`` → ``["Bash", "Read"]``. Repeats and
    spaces are tolerated because both spellings are in the wild."""
    if not raw:
        return None
    out = [t.strip() for chunk in raw.split(",") for t in chunk.split() if t.strip()]
    return out or None


def _tui_resolved_model(args: Any) -> tuple[str, str, str | None]:
    """Ask the terminal itself which model/backend/key a session would use.

    Instantiating ``MantisTUI`` is pure state assignment — nothing is drawn and
    no I/O happens — so this reuses the real resolution (last-used model, its
    provider's saved key, self-host normalization) instead of a second copy of
    it that could drift."""
    from .tui import MantisTUI  # noqa: PLC0415

    tui = MantisTUI(
        model=args.model, backend=args.backend, api_key=args.api_key,
        system=args.system, max_tokens=args.max_tokens,
        temperature=args.temperature, max_turns=args.max_turns,
        effort=getattr(args, "effort", None),
        verbosity=getattr(args, "verbosity", None),
        reasoning_mode=getattr(args, "reasoning_mode", None),
    )
    try:
        tui._restore_last_model()
        tui._resolve_model()
    except Exception:  # noqa: BLE001 — a resolver hiccup must not kill the run
        pass
    return tui.model, tui.backend, tui.api_key


def build_query_options(args: Any, transcript: list[Any] | None = None) -> dict[str, Any]:
    """CLI args → the options dict ``query()`` runs one headless turn from.

    Returns the dict rather than the ``MantisAgentOptions`` dataclass because
    the resolved provider key has to ride along, and that isn't a field on the
    Claude-compatible options object (the SDK reads keys from the environment;
    the terminal resolves one for you).

    ``transcript`` is a list the caller keeps appending the running conversation
    to; pass one to enable the advisor, which needs to read it. Without it the
    advisor is left off rather than paired with an empty transcript."""
    from .builtin_tools import CODING_TOOLS, web_fetch, web_search  # noqa: PLC0415
    from .builtin_tools.codenav import lsp  # noqa: PLC0415
    from .claude_compat import MantisAgentOptions  # noqa: PLC0415

    model, backend, api_key = _tui_resolved_model(args)

    # Permissions: --godmode/--dangerously-skip-permissions is full autonomy.
    # Otherwise the engine's headless posture applies — non-dangerous tools run
    # unattended, dangerous shell commands are refused (there is nobody to ask,
    # and a prompt that can't be answered must not become an implicit yes).
    mode = "default"
    if getattr(args, "dangerously_skip_permissions", False) or getattr(args, "godmode", False):
        mode = "bypass"
    elif getattr(args, "permission_mode", None) in ("bypass", "acceptEdits", "plan"):
        mode = {"bypass": "bypass", "acceptEdits": "auto", "plan": "default"}[args.permission_mode]

    extra: dict[str, Any] = {}
    for key in ("effort", "verbosity", "reasoning_mode"):
        val = getattr(args, key, None)
        if val:
            extra[key] = val

    # The advisor is worth more headless than interactive: nobody is watching
    # the run, so "check this before you commit to it" is the only review it
    # gets. Off unless a transcript is available for it to read.
    belt = [*CODING_TOOLS, web_search, web_fetch, lsp]
    advisor_cfg = None
    if transcript is not None:
        from .advisor import make_advisor_tool, resolve_advisor  # noqa: PLC0415

        advisor_cfg = resolve_advisor(getattr(args, "advisor", None))
        if advisor_cfg is not None:
            belt.append(make_advisor_tool(advisor_cfg, messages=transcript))

    opts = MantisAgentOptions(
        model=model,
        max_turns=args.max_turns,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        permission_mode=mode,
        # The whole terminal tool belt: a print run that can't edit or run
        # anything is just a chat completion, which is not what -p is for.
        tools=belt,
        allowed_tools=_split_tool_list(getattr(args, "allowed_tools", None)),
        disallowed_tools=_split_tool_list(getattr(args, "disallowed_tools", None)),
        continue_conversation=bool(getattr(args, "continue_session", False)),
        resume=getattr(args, "resume_id", None) or None,
        session_id=getattr(args, "session_id", None) or None,
        # Where the run happens, stated explicitly so the init event reports it
        # rather than an empty string a consumer has to guess about.
        cwd=os.getcwd(),
        extra=extra,
    )
    if backend:
        opts.backend = backend
    if args.system:
        opts.system_prompt = args.system
    append = getattr(args, "append_system_prompt", None)
    if append:
        opts.system_prompt = (
            f"{opts.system_prompt}\n\n{append}" if opts.system_prompt else append
        )
    if advisor_cfg is not None:
        from .advisor import advisor_prompt_section  # noqa: PLC0415

        # Appended last: the model has to be told the tool exists and when to
        # reach for it, or a paired advisor just sits in the belt unused.
        opts.system_prompt = (opts.system_prompt or "") + advisor_prompt_section(advisor_cfg)
    out = opts.to_query_options()
    if api_key:
        out["api_key"] = api_key
    return out


def resolve_session(args: Any) -> tuple[str | None, list[Any]]:
    """``(session_id, prior messages)`` for this run.

    Print runs live in the SAME session store the interactive terminal uses, so
    ``mantis -p`` in CI and ``mantis -c`` on your laptop are looking at one
    history: resume a session a print run started, or hand a session you were
    working on to a script."""
    from . import session_tree  # noqa: PLC0415

    sid = getattr(args, "session_id", None) or None
    want_resume = getattr(args, "resume_id", None)
    if want_resume:
        sid = want_resume
    elif getattr(args, "continue_session", False):
        recent = session_tree.list_sessions(cwd=os.getcwd())
        if recent:
            sid = recent[0].session_id
        else:
            print("Warning: no previous session in this directory — starting a new one",
                  file=sys.stderr)
    if not sid:
        return None, []
    try:
        return sid, list(session_tree.load_for_resume(sid, cwd=os.getcwd()))
    except Exception as e:  # noqa: BLE001 — an unreadable session isn't fatal
        print(f"Warning: could not load session {sid}: {e}", file=sys.stderr)
        return sid, []


def _dump(obj: Any) -> str:
    import msgspec  # noqa: PLC0415

    return _json.dumps(msgspec.to_builtins(obj), default=str)


def _final_text(result: Any, args: Any) -> str:
    """What text mode prints for a given result message."""
    subtype = getattr(result, "subtype", "success")
    if subtype == "success":
        text = getattr(result, "result", "") or ""
        return text if text.endswith("\n") else text + "\n"
    template = _ERROR_TEXT.get(subtype)
    if template:
        return template.format(max_turns=args.max_turns) + "\n"
    errs = getattr(result, "errors", None) or []
    return (errs[0] if errs else "Execution error") + "\n"


async def _run(args: Any, prompt: str) -> int:
    from . import session_tree  # noqa: PLC0415
    from .query import (  # noqa: PLC0415
        SDKAssistantMessage,
        SDKResultMessage,
        SDKUserMessage,
        query,
    )
    from .types import AssistantMessage, UserMessage  # noqa: PLC0415

    fmt = getattr(args, "output_format", "text") or "text"
    verbose = bool(getattr(args, "verbose", False))
    stream = fmt == "stream-json"

    # The running conversation in plain message form. The advisor tool holds a
    # reference to this list, so anything appended below is visible to it the
    # next time the model escalates.
    convo: list[Any] = []

    options = build_query_options(args, transcript=convo)
    sid, history = resolve_session(args)
    sid = sid or session_tree.new_session_id()
    options["session_id"] = sid
    if history:
        options["messages"] = history
        convo.extend(history)

    # Record into the terminal's own store, so a headless run shows up in
    # `mantis --resume` next to the sessions you drove by hand.
    transcript: Any = None
    try:
        transcript = session_tree.SessionTranscript(sid, cwd=os.getcwd())
        transcript.record_last_prompt(prompt[:200])
    except Exception:  # noqa: BLE001 — never let bookkeeping fail the run
        transcript = None

    def record(role: str, content: Any, meta: bool = False) -> None:
        if transcript is None:
            return
        try:
            transcript.append_message(role, content, is_meta=meta)
        except Exception:  # noqa: BLE001
            pass

    collected: list[Any] = []
    result: Any = None

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, SDKUserMessage):
            record("user", msg.message.content, bool(getattr(msg, "isSynthetic", False)))
            convo.append(UserMessage(content=msg.message.content))
        elif isinstance(msg, SDKAssistantMessage):
            record("assistant", list(msg.message.content))
            convo.append(AssistantMessage(content=list(msg.message.content)))
        if stream:
            # NDJSON as it happens: one object per line, flushed immediately so
            # a consumer piping us through `while read` sees progress rather
            # than a burst at exit.
            sys.stdout.write(_dump(msg) + "\n")
            sys.stdout.flush()
        elif fmt == "json" and verbose:
            collected.append(msg)
        if isinstance(msg, SDKResultMessage):
            result = msg

    if result is None:                       # stream ended without a result
        print("Error: no result was produced", file=sys.stderr)
        return 1
    if fmt == "json":
        payload = collected if verbose else result
        sys.stdout.write(_dump(payload) + "\n")
    elif not stream:
        sys.stdout.write(_final_text(result, args))
    sys.stdout.flush()
    return 1 if result.is_error else 0


def run_print(args: Any) -> int:
    """Entry point for ``mantis -p``. Returns the process exit code."""
    import anyio  # noqa: PLC0415

    fmt = getattr(args, "output_format", "text") or "text"
    if fmt == "stream-json" and not getattr(args, "verbose", False):
        # Claude Code's rule, kept identical: streaming diagnostics without
        # asking for them turns a quiet pipe into a firehose.
        print("Error: When using --print, --output-format=stream-json requires --verbose",
              file=sys.stderr)
        return 1

    prompt = resolve_prompt(getattr(args, "prompt", None))
    if not prompt:
        print("Error: Input must be provided either through stdin or as a prompt "
              "argument when using --print", file=sys.stderr)
        return 1

    try:
        return anyio.run(_run, args, prompt)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `mantis -p … | head` closes the pipe on us. That's the consumer
        # saying "enough", not an error — exit quietly instead of dumping a
        # traceback into their terminal.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except Exception as e:  # noqa: BLE001 — a crash still has to be a clean exit code
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


__all__ = ["build_query_options", "resolve_prompt", "run_print"]
