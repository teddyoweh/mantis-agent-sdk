"""Watch — a background command whose every stdout line becomes an event.

``bash(run_in_background=True)`` is a *pull* model: the command's output piles up
in a log and the model reads it later with ``bash_output``. A watch is the
*push* model — it stays alive and each line its script prints arrives in the
conversation as a notification, so the agent learns about a failing test, a new
PR comment, or a file change without having to remember to go looking.

This mirrors Claude Code's Monitor tool (named ``watch`` here because mantis
already has a ``monitor`` — the *blocking wait-for-one-condition* tool in
``builtin_tools.fs``, which covers the case Claude's Monitor docs explicitly
send elsewhere). Ported along with the parts that are less obvious than "stream
some lines":

* **Batching.** Lines printed within 200ms of each other coalesce into one
  notification, so a multi-line event (a traceback) stays a single message.
* **No terminal status mid-stream.** Stream events go through
  ``JobManager.emit``; only the final summary flows through ``on_event``. A
  progress ping must never be mistaken for the job closing.
* **Rate limiting.** A watch that fires too fast is stopped rather than
  allowed to flood the context — every event is a message, and a firehose
  costs the same as a person pasting a log file every second.
* **Stdout is the event stream; stderr is not.** Stderr lands in the log file
  (readable afterwards) but never notifies, matching Claude's contract so a
  chatty tool's warnings don't become conversation traffic.
* **Persistent watches** opt out of the timeout entirely and live until the
  session ends or someone stops them.

The process is spawned detached (``start_new_session``) and killed by process
group, so a shell pipeline's children die with it instead of leaking.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections import deque
from typing import Any

from .tools import Tool, tool

__all__ = ["make_watch_stop_tool", "make_watch_tool", "run_watch"]

# Stdout lines arriving within this window coalesce into one notification.
_BATCH_WINDOW_S = 0.2
# Hard ceiling on a single batch, so a burst of thousands of lines can't build
# one enormous message while we wait for the 200ms window to go quiet.
_MAX_BATCH_LINES = 40
_MAX_EVENT_CHARS = 4000

_DEFAULT_TIMEOUT_MS = 300_000
_MAX_TIMEOUT_MS = 3_600_000

# Flood control: more than _RATE_MAX_EVENTS notifications inside _RATE_WINDOW_S
# means the filter is too loose. We stop the watch and say so, rather than
# silently dropping events (which would look like "nothing is happening").
_RATE_WINDOW_S = 10.0
_RATE_MAX_EVENTS = 20


def _coerce_timeout_ms(value: Any) -> int:
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_MS
    return max(1000, min(ms, _MAX_TIMEOUT_MS))


def _watch_env() -> dict[str, str]:
    """Same hardened environment ``bash`` uses: no credentials, no pager, no
    interactive editor. A watch script is model-authored and long-lived, so it
    gets the strictest of the two treatments, not a looser one."""
    from .builtin_tools.fs import _is_secret_env  # noqa: PLC0415

    env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
    env.update(
        TERM="dumb", PAGER="cat", GIT_PAGER="cat", EDITOR="true", VISUAL="true",
        GIT_TERMINAL_PROMPT="0", DEBIAN_FRONTEND="noninteractive",
    )
    return env


def _watch_cwd() -> str | None:
    """Start where ``bash`` currently is, so ``watch`` and ``bash`` agree on
    what a relative path means."""
    from .builtin_tools.fs import _bash_cwd  # noqa: PLC0415

    cwd = _bash_cwd()["cwd"]
    return cwd if cwd is not None and os.path.isdir(cwd) else None


async def _terminate(proc: Any) -> None:
    """Stop the watch's whole process group and reap it, escalating TERM→KILL.

    Watchs are almost always pipelines (``tail -f x | grep y``); signalling
    only the shell would leave the pipeline's members running and holding the
    file open."""
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            return
    try:
        await asyncio.wait_for(proc.wait(), 2.0)
        return
    except (TimeoutError, asyncio.TimeoutError):
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            return
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001
        pass


async def run_watch(command: str, description: str, *, emit: Any,
                      timeout_ms: int = _DEFAULT_TIMEOUT_MS,
                      persistent: bool = False,
                      log_path: str | None = None) -> str:
    """Run ``command``, pushing each batch of stdout lines through ``emit``.

    ``emit(text)`` is awaited once per batch. Returns the terminal summary — the
    string the job's completion notification carries, phrased the way Claude
    phrases it: a watch's script exiting means *the stream ended*, not that a
    condition was met.
    """
    from .builtin_tools.fs import _strip_terminal_controls  # noqa: PLC0415

    err_fd = None
    if log_path:
        err_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    proc = None
    events = 0
    recent: deque[float] = deque()
    stopped_reason = ""

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=err_fd if err_fd is not None else asyncio.subprocess.DEVNULL,
            env=_watch_env(),
            cwd=_watch_cwd(),
            start_new_session=True,
        )
    finally:
        if err_fd is not None:
            os.close(err_fd)

    pending: list[str] = []

    async def flush() -> bool:
        """Emit the buffered lines as one event. False → the watch must stop."""
        nonlocal events, stopped_reason
        if not pending:
            return True
        text = "\n".join(pending).rstrip()
        pending.clear()
        if not text:
            return True
        if len(text) > _MAX_EVENT_CHARS:
            text = text[:_MAX_EVENT_CHARS] + "\n…(event truncated)"
        await emit(text)
        events += 1
        now = time.monotonic()
        recent.append(now)
        while recent and now - recent[0] > _RATE_WINDOW_S:
            recent.popleft()
        if len(recent) > _RATE_MAX_EVENTS:
            stopped_reason = (
                f"emitted {len(recent)} events in {_RATE_WINDOW_S:.0f}s — the filter is "
                f"too loose. Restart with a tighter grep if you still need this watch."
            )
            return False
        return True

    async def pump() -> str:
        """Read stdout until EOF, batching on a 200ms quiet window."""
        assert proc is not None and proc.stdout is not None
        while True:
            # Only bound the read once something is buffered: with an empty
            # buffer there is nothing to flush, so we wait indefinitely rather
            # than spinning a timer against an idle stream.
            try:
                if pending:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(), _BATCH_WINDOW_S)
                else:
                    raw = await proc.stdout.readline()
            except (TimeoutError, asyncio.TimeoutError):
                if not await flush():
                    return "rate-limited"
                continue
            if not raw:  # EOF — the script closed stdout / exited
                await flush()
                return "eof"
            line = _strip_terminal_controls(
                raw.decode("utf-8", "replace")).rstrip("\n")
            pending.append(line)
            if len(pending) >= _MAX_BATCH_LINES and not await flush():
                return "rate-limited"

    outcome = "eof"
    try:
        if persistent:
            outcome = await pump()
        else:
            async with asyncio.timeout(timeout_ms / 1000.0):
                outcome = await pump()
    except (TimeoutError, asyncio.TimeoutError):
        outcome = "timeout"
    finally:
        # Whatever happened — EOF, timeout, rate limit, or the job being
        # cancelled out from under us — the process group must not survive.
        await _terminate(proc)

    code = proc.returncode if proc is not None else None
    tail = f" ({events} event{'s' if events != 1 else ''})"
    if outcome == "rate-limited":
        return f'Watch "{description}" stopped: {stopped_reason}{tail}'
    if outcome == "timeout":
        return (f'Watch "{description}" timed out after '
                f'{timeout_ms / 1000:.0f}s and was stopped{tail}')
    if code:
        return f'Watch "{description}" script failed (exit {code}){tail}'
    return f'Watch "{description}" stream ended{tail}'


_WATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": ("Shell command or script. Each stdout line is an event; "
                            "exit ends the watch."),
        },
        "description": {
            "type": "string",
            "description": ("Short description of what is being watched — it appears "
                            "in every notification, so be specific "
                            "(\"errors in deploy.log\", not \"watching logs\")."),
        },
        "timeout_ms": {
            "type": "integer",
            "description": ("Kill the watch after this deadline (default 300000, "
                            "max 3600000). Ignored when persistent is true."),
        },
        "persistent": {
            "type": "boolean",
            "description": ("Run for the lifetime of the session with no timeout. Use "
                            "for session-length watches like log tails."),
        },
    },
    "required": ["command", "description"],
}

_WATCH_DESCRIPTION = (
    "Start a background watch that streams events from a long-running script. "
    "Each stdout line is an event — you keep working and notifications arrive in "
    "the conversation.\n\n"
    "Pick by how many notifications you need:\n"
    "- ONE (\"tell me when the server is ready / the build finishes\") → use the "
    "monitor tool, which blocks until one condition fires and returns. Do NOT use "
    "watch for this: `tail -f` and `while true` never exit on their own, so the "
    "watch stays armed long after the event fired.\n"
    "- ONE PER OCCURRENCE (\"tell me every time an ERROR line appears\") → watch "
    "with an unbounded command.\n"
    "- ONE PER OCCURRENCE UNTIL A KNOWN END (\"emit each CI step, stop when the run "
    "completes\") → watch with a command that emits lines and then exits.\n\n"
    "Script quality matters:\n"
    "- Every pipe stage must flush per line or matches sit in its buffer unseen: "
    "grep needs --line-buffered, awk needs fflush(). `head` cannot flush at all.\n"
    "- Silence is not success. If the watched process crashed right now, would your "
    "filter emit anything? If not, widen it — match failure signatures "
    "(Traceback|Error|FAILED|Killed|OOM) alongside the happy path, or a crashloop "
    "looks identical to 'still running'.\n"
    "- Poll loops: 30s+ for remote APIs, and tolerate transient failures "
    "(`curl ... || true`) so one bad request doesn't kill the watch.\n"
    "- Only stdout notifies. Merge stderr with 2>&1 if a command's failures print "
    "there.\n\n"
    "Lines printed within 200ms batch into one notification. Watches that emit too "
    "fast are stopped automatically — filter to the lines you would act on. Stop one "
    "early with watch_stop."
)


def make_watch_tool(jobs: Any) -> Tool:
    """Build ``watch``: watch a long-running script, one notification per event."""

    @tool(name="watch", is_read_only=False, is_concurrency_safe=True,
          input_schema=_WATCH_SCHEMA)
    async def watch(args: dict) -> str:
        args = args or {}
        command = str(args.get("command") or "").strip()
        description = str(args.get("description") or "").strip()
        if not command:
            return "watch: a non-empty 'command' is required."
        if not description:
            return ("watch: a 'description' is required — it labels every "
                    "notification this watch produces.")
        persistent = bool(args.get("persistent"))
        timeout_ms = _coerce_timeout_ms(args.get("timeout_ms", _DEFAULT_TIMEOUT_MS))

        import tempfile  # noqa: PLC0415

        fd, log_path = tempfile.mkstemp(prefix="mantis-watch-", suffix=".log")
        os.close(fd)

        # The Job has to exist before run_watch can emit against it, but
        # spawn() is what assigns the id — so the coroutine closes over a
        # holder the spawn call fills in. emit() is only reachable once the
        # event loop starts the coroutine, which is strictly after spawn
        # returns, so the holder is always populated by then.
        holder: dict[str, Any] = {}

        async def _emit(text: str) -> None:
            job = holder.get("job")
            if job is not None:
                await jobs.emit(job, text)

        async def _run() -> str:
            return await run_watch(
                command, description, emit=_emit, timeout_ms=timeout_ms,
                persistent=persistent, log_path=log_path)

        job = jobs.spawn(
            _run(), desc=description, kind="watch",
            # A persistent watch's whole purpose is to outlive the backstop;
            # it ends with the session (cancel_all) or via watch_stop.
            max_runtime_s=None if persistent else (timeout_ms / 1000.0) + 30.0)
        holder["job"] = job

        window = "no timeout (persistent)" if persistent else f"{timeout_ms / 1000:.0f}s"
        return (
            f'Watching "{description}" as job #{job.id} ({window}).\n'
            f"Events will arrive as notifications while you keep working — do not "
            f"poll for them. Stderr goes to {log_path} (read it if the script "
            f"seems broken). Stop early with watch_stop(job_id={job.id})."
        )

    watch.description = _WATCH_DESCRIPTION
    return watch


_WATCH_STOP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "integer",
            "description": "The watch's job id, as returned by watch.",
        },
    },
    "required": ["job_id"],
}


def make_watch_stop_tool(jobs: Any) -> Tool:
    """Build ``watch_stop``: end a running watch (or any background job)."""

    @tool(name="watch_stop", is_read_only=False, is_concurrency_safe=True,
          input_schema=_WATCH_STOP_SCHEMA)
    async def watch_stop(args: dict) -> str:
        try:
            jid = int((args or {}).get("job_id"))
        except (TypeError, ValueError):
            return "watch_stop: an integer 'job_id' is required."
        job = jobs.get(jid)
        if job is None:
            known = ", ".join(str(j.id) for j in jobs.all()) or "none"
            return f"no job #{jid} (known jobs: {known})"
        if job.status != "running":
            return f"job #{jid} is already {job.status}."
        jobs.cancel(jid)
        return (f"Stopping watch #{jid} ({job.desc}) after "
                f"{job.stream_count} event{'s' if job.stream_count != 1 else ''}.")

    watch_stop.description = (
        "Stop a running watch started with the watch tool. Use this once a "
        "watch has told you what you needed — a watch left running keeps "
        "sending notifications."
    )
    return watch_stop
