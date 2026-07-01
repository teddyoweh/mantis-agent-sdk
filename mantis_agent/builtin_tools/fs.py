"""Built-in *coding* tools — the filesystem + shell tools that turn ``mantis``
from a chat box into an actual agent harness.

These mirror Claude Code's core tool set (``Bash``, ``Read``, ``Write``,
``Edit``, ``LS``, ``Glob``, ``Grep``) closely enough that a model trained on
that surface knows how to drive them, but the names are lower-case to match the
Pythonic ``@tool`` style used elsewhere in the SDK.

Everything here is stdlib + ``anyio`` only (no third-party deps) so the tools
load whether or not the ``[cli]`` extra is installed. Tool bodies return plain
strings; the agent loop wraps them into ``ToolResultBlock``s and surfaces any
exception as ``is_error=True`` (see :mod:`mantis_agent.tools`), so we let bad
paths / non-zero exits raise rather than hand-formatting every failure.

Output is bounded — ``bash`` stdout, file reads, and grep hits are all capped
so a runaway ``find /`` or a huge log can't blow up the context window.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import anyio

from ..tools import Tool, tool

# Caps — keep tool output from swamping the model's context window.
_MAX_OUTPUT = 30_000  # chars of bash stdout/stderr returned
_MAX_READ_LINES = 2000  # default lines per read_file call
_MAX_LINE = 2000  # chars per line before truncation
_MAX_MATCHES = 200  # grep/glob hits returned


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    return f"{head}\n… [truncated {len(text) - limit} chars]"


def _not_found_hint(old_string: str, text: str, path: str) -> str:
    """An *actionable* edit-miss error. A model that gets only 'not found' tends
    to retry blindly; pointing it at the likely cause (stale/auto-formatted text,
    whitespace) and the nearest real line lets it self-correct in one step."""
    import difflib

    probe = next((ln.strip() for ln in old_string.splitlines() if ln.strip()), "")
    hint = (
        f"old_string not found in {path}. The file's text differs from what you "
        f"expected (whitespace, or it changed). Read the file again to copy the "
        f"exact current text before editing."
    )
    if probe:
        lines = text.splitlines()
        near = difflib.get_close_matches(probe, [ln.strip() for ln in lines], n=1, cutoff=0.6)
        if near:
            hint += f" Closest line in the file is: {near[0]!r}"
    return hint


def _coerce_int(value: object, *, default: int, lo: int | None = None,
                hi: int | None = None) -> int:
    """Best-effort int from whatever a model passed (``"2000"``, ``0``, ``None``,
    floats), clamped to ``[lo, hi]``. Tools take numbers as strings constantly on
    the native tool-calling path, so coerce instead of letting them TypeError."""
    try:
        n = int(float(value))  # handles "2000", "2000.0", 2000, 2000.0
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default if value in (0, "0", None) else lo
    if hi is not None and n > hi:
        return hi
    return n


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------


@tool(is_read_only=False, is_concurrency_safe=False, timeout_s=120.0)
async def bash(command: str, timeout: int = 120, stdin: str = "",
               run_in_background: bool = False) -> str:
    """Run a shell command and return its combined stdout + stderr.

    Use this to inspect the system, run builds/tests, git, grep, find, etc.
    Runs through ``bash -lc`` in the current working directory.

    Set ``run_in_background=True`` for a long-running command (a dev server, a
    file watcher, a slow build) — it starts detached, returns a background id
    immediately, and you read its accumulated output later with the
    ``bash_output`` tool. Don't background a command whose result you need now.

    The command runs NON-INTERACTIVELY (no terminal): there is no human to
    answer prompts. If a command needs input, either pass it via ``stdin``, or
    bake the answer into the command — pipe it (``echo y | rm -i x``), use a
    here-doc, or use a non-interactive flag (``-y``, ``--yes``, ``--no-input``).
    Never launch an interactive editor/pager (nano, vim, less, top); use the
    file tools or append ``| cat`` instead.

    Args:
        command: The shell command line to execute.
        timeout: Hard timeout in seconds (default 120). The command is killed
            if it exceeds this — usually a sign it is waiting on input.
        stdin: Text fed to the command's standard input. Use this for commands
            that read from stdin (e.g. answering a prompt: ``stdin="yes\\n"``).
    """

    # Models pass loose values — strings, 0, absurd numbers. Clamp to a sane
    # window so e.g. ``timeout: 0`` doesn't either fire instantly or hang.
    timeout = _coerce_int(timeout, default=120, lo=1, hi=600)
    # stdin is fed to the process, then closed — so a command that reads more
    # than we provided gets EOF and exits rather than blocking forever.
    stdin_bytes = (stdin if isinstance(stdin, str) else str(stdin)).encode("utf-8")

    # Non-interactive environment: models love to reach for ``nano``/``vim`` or
    # commands that page (``git log``, ``less``) — those launch full-screen UIs
    # that hang on the closed stdin or vomit terminal-control codes into the
    # result. ``TERM=dumb`` + neutered pager/editor envs make them behave like a
    # script would.
    env = dict(os.environ)
    env.update(
        TERM="dumb", PAGER="cat", GIT_PAGER="cat", EDITOR="true", VISUAL="true",
        GIT_TERMINAL_PROMPT="0", DEBIAN_FRONTEND="noninteractive",
    )

    if run_in_background:
        return _start_background(command, env)

    try:
        with anyio.fail_after(timeout):
            result = await anyio.run_process(
                ["bash", "-lc", command], check=False, input=stdin_bytes, env=env
            )
    except TimeoutError:
        raise TimeoutError(
            f"command timed out after {timeout}s (is it interactive or waiting "
            f"on input? pass it via the stdin argument): {command}"
        ) from None

    out = _strip_terminal_controls(result.stdout.decode("utf-8", "replace"))
    err = _strip_terminal_controls(result.stderr.decode("utf-8", "replace"))
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(err if not out else f"\n[stderr]\n{err}")
    body = "".join(parts).rstrip()
    if result.returncode != 0:
        body = f"{body}\n[exit code: {result.returncode}]".lstrip()
    return _truncate(body) or f"(no output, exit code {result.returncode})"


# ---------------------------------------------------------------------------
# Background shells — for long-running commands (dev servers, watchers, builds).
# Detached, stdout+stderr streamed to a temp log; read later with bash_output.
# ---------------------------------------------------------------------------

_BG_SHELLS: dict[str, dict[str, Any]] = {}
_BG_COUNTER = [0]


def _start_background(command: str, env: dict[str, str]) -> str:
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    _BG_COUNTER[0] += 1
    bid = f"bg_{_BG_COUNTER[0]}"
    fd, log_path = tempfile.mkstemp(prefix="mantis-bg-", suffix=".log")
    try:
        proc = subprocess.Popen(  # noqa: S603
            ["bash", "-lc", command],  # noqa: S607
            stdout=fd, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, start_new_session=True,  # detach from our process group
        )
    finally:
        os.close(fd)
    _BG_SHELLS[bid] = {"proc": proc, "log": log_path, "cmd": command}
    return (
        f"Started in background as {bid} (pid {proc.pid}): {command}\n"
        f"Read its output with bash_output(bash_id=\"{bid}\")."
    )


@tool(is_read_only=True)
async def bash_output(bash_id: str) -> str:
    """Read the accumulated output of a background shell started with
    ``bash(run_in_background=True)``, plus whether it's still running or has
    exited (with its code).

    Args:
        bash_id: The id returned when the background command was started.
    """
    entry = _BG_SHELLS.get(bash_id)
    if entry is None:
        running = ", ".join(_BG_SHELLS) or "none"
        return f"no background shell {bash_id!r} (running: {running})"
    proc = entry["proc"]
    rc = proc.poll()
    status = "running" if rc is None else f"exited with code {rc}"
    try:
        with open(entry["log"], encoding="utf-8", errors="replace") as fh:
            body = _strip_terminal_controls(fh.read()).strip()
    except OSError:
        body = ""
    header = f"[{bash_id} · {status}] {entry['cmd']}"
    return _truncate(f"{header}\n{body}" if body else f"{header}\n(no output yet)")


# ANSI/terminal control: CSI sequences, OSC strings, and the alt-screen /
# cursor escapes that full-screen programs (nano, vim, top) emit. Stripped from
# bash output so a stray interactive program can't pollute the model's context.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"      # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL/ST
    r"|\x1b[()][AB0-2]"               # charset selection
    r"|\x1b[=>NOc]"                   # misc single-char escapes
)


def _strip_terminal_controls(text: str) -> str:
    if "\x1b" not in text and "\r" not in text:
        return text
    text = _ANSI_RE.sub("", text)
    # Collapse carriage returns (progress bars) to keep only the final state.
    return "\n".join(seg.split("\r")[-1] for seg in text.split("\n"))


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _unified_diff(old: str, new: str, path: str, max_lines: int = 80) -> str:
    """A compact unified diff (no ``---/+++`` header) between two texts, or ""
    if identical. Returned by edit/write so the caller (and the TUI) can show
    exactly what changed."""
    import difflib  # noqa: PLC0415

    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=3,
    ))
    # Drop difflib's "--- " / "+++ " file header (first two lines); keep @@ hunks.
    if lines[:1] and lines[0].startswith("---"):
        lines = lines[2:]
    if not lines:
        return ""
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], f"… (+{len(lines) - max_lines} more diff lines)"]
    return "\n".join(lines)


def _diff_stat(old: str, new: str) -> tuple[int, int]:
    """``(additions, removals)`` between two texts — the line counts Claude Code
    shows as 'Added N lines / Removed M lines'."""
    import difflib  # noqa: PLC0415

    adds = removes = 0
    for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if ln.startswith("+") and not ln.startswith("+++"):
            adds += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removes += 1
    return adds, removes


def _edit_summary(verb: str, path: str, old: str, new: str) -> str:
    """A Claude-Code-style one-liner + diff: ``Updated foo.py · +3 -1`` then the
    unified diff so the UI can show exactly what changed."""
    adds, removes = _diff_stat(old, new)
    stat = []
    if adds:
        stat.append(f"+{adds}")
    if removes:
        stat.append(f"-{removes}")
    head = f"{verb} {path}" + (f" · {' '.join(stat)}" if stat else "")
    diff = _unified_diff(old, new, path)
    return f"{head}\n{diff}" if diff else head


_IMAGE_READ_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_IMAGE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB — refuse absurd images


@tool(is_read_only=True)
async def read_file(path: str, offset: int = 1, limit: int = _MAX_READ_LINES) -> Any:
    """Read a file. Text files come back with 1-based line numbers (``cat -n``
    style); image files (png/jpg/gif/webp/bmp) come back as an image the model
    can see (if the backend model is vision-capable). Other binaries are noted,
    not dumped as mojibake.

    Args:
        path: File to read (absolute or relative to the working directory).
        offset: 1-based line number to start from (default 1). Text only.
        limit: Maximum number of lines to return (default 2000). Text only.
    """

    offset = _coerce_int(offset, default=1, lo=1)
    limit = _coerce_int(limit, default=_MAX_READ_LINES, lo=1, hi=50_000)

    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {path}")
    if p.is_dir():
        raise IsADirectoryError(f"{path} is a directory — use ls instead")

    suffix = p.suffix.lower()
    if suffix in _IMAGE_READ_EXTS:
        import base64  # noqa: PLC0415

        from ..types import ImageBlock  # noqa: PLC0415
        data = await anyio.to_thread.run_sync(p.read_bytes)
        if len(data) > _MAX_IMAGE_BYTES:
            return f"[image {path} is {len(data) // 1024} KB — too large to inline]"
        return ImageBlock(source={
            "type": "base64",
            "media_type": _IMAGE_MEDIA.get(suffix, "image/png"),
            "data": base64.b64encode(data).decode("ascii"),
        })
    if suffix == ".pdf":
        return f"[{path} is a PDF — mantis can't render PDF pages yet; extract text with a tool like pdftotext]"

    text = await anyio.to_thread.run_sync(lambda: p.read_text("utf-8", "replace"))
    lines = text.splitlines()
    start = max(1, offset)
    chunk = lines[start - 1 : start - 1 + max(1, limit)]
    if not chunk:
        return f"(file has {len(lines)} lines; offset {offset} is past the end)"
    width = len(str(start + len(chunk) - 1))
    out = "\n".join(
        f"{str(start + i).rjust(width)}\t{ln[:_MAX_LINE]}" for i, ln in enumerate(chunk)
    )
    if start - 1 + len(chunk) < len(lines):
        out += f"\n… [{len(lines) - (start - 1 + len(chunk))} more lines]"
    return out


@tool(is_read_only=False, is_concurrency_safe=False)
async def write_file(path: str, content: str) -> str:
    """Write ``content`` to ``path``, creating parent directories and overwriting
    any existing file.

    Args:
        path: Destination file path.
        content: Full file contents to write.
    """

    # A truncated/cut-off tool call can arrive with content=None — fail with a
    # clear, recoverable message instead of crashing on ``None.write_text``.
    if content is None:
        raise ValueError(
            "content is required and must be a string. The previous write was "
            "likely cut off — write the file again (smaller, or in pieces)."
        )
    if not isinstance(content, str):
        content = str(content)

    p = Path(path).expanduser()
    old = ""
    if p.exists() and p.is_file():
        old = await anyio.to_thread.run_sync(lambda: p.read_text("utf-8", "replace"))

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")

    await anyio.to_thread.run_sync(_write)
    return _edit_summary("Wrote" if not old else "Updated", str(p), old, content)


@tool(is_read_only=False, is_concurrency_safe=False)
async def edit_file(
    path: str, old_string: str, new_string: str, replace_all: bool = False
) -> str:
    """Replace an exact substring in a file. ``old_string`` must appear exactly
    once unless ``replace_all`` is true.

    Args:
        path: File to edit.
        old_string: Exact text to find (include enough surrounding context to be
            unique).
        new_string: Text to replace it with.
        replace_all: Replace every occurrence instead of requiring a unique match.
    """

    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {path}")

    text = await anyio.to_thread.run_sync(lambda: p.read_text("utf-8", "replace"))
    count = text.count(old_string)
    if count == 0:
        raise ValueError(_not_found_hint(old_string, text, path))
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string is not unique in {path} ({count} matches) — add more "
            f"context or pass replace_all=true"
        )
    updated = text.replace(old_string, new_string)
    await anyio.to_thread.run_sync(lambda: p.write_text(updated, "utf-8"))
    return _edit_summary("Updated", str(p), text, updated)


@tool(is_read_only=False, is_concurrency_safe=False)
async def multi_edit(path: str, edits: list[dict]) -> str:
    """Apply several edits to one file in a single atomic pass. Edits run in
    order, each against the result of the previous one; if ANY edit fails to
    match, NONE are written (all-or-nothing), so the file never ends up
    half-edited.

    Args:
        path: File to edit.
        edits: A list of ``{"old_string": ..., "new_string": ..., "replace_all"?: bool}``
            objects, applied top to bottom.
    """

    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty list of edit objects")

    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {path}")

    text = await anyio.to_thread.run_sync(lambda: p.read_text("utf-8", "replace"))
    original = text
    applied = 0
    for i, e in enumerate(edits):
        if not isinstance(e, dict) or "old_string" not in e or "new_string" not in e:
            raise ValueError(f"edit #{i + 1} must have old_string and new_string")
        old, new = e["old_string"], e["new_string"]
        replace_all = bool(e.get("replace_all", False))
        count = text.count(old)
        if count == 0:
            raise ValueError(f"edit #{i + 1}: " + _not_found_hint(old, text, path))
        if count > 1 and not replace_all:
            raise ValueError(
                f"edit #{i + 1}: old_string not unique ({count} matches) — add "
                f"context or set replace_all"
            )
        text = text.replace(old, new)
        applied += 1

    await anyio.to_thread.run_sync(lambda: p.write_text(text, "utf-8"))
    return _edit_summary("Updated", str(p), original, text)


# ---------------------------------------------------------------------------
# Listing / searching
# ---------------------------------------------------------------------------


@tool(is_read_only=True)
async def ls(path: str = ".") -> str:
    """List directory entries (directories first, marked with a trailing ``/``).

    Args:
        path: Directory to list (default: current working directory).
    """

    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such path: {path}")
    if not p.is_dir():
        return f"{p} (file, {p.stat().st_size} bytes)"

    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    if not entries:
        return f"{p} is empty"
    lines = [f"{e.name}/" if e.is_dir() else e.name for e in entries[:_MAX_MATCHES]]
    out = "\n".join(lines)
    if len(entries) > _MAX_MATCHES:
        out += f"\n… [{len(entries) - _MAX_MATCHES} more entries]"
    return out


@tool(is_read_only=True)
async def glob(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern (e.g. ``**/*.py``), most-recently-modified
    first.

    Args:
        pattern: Glob pattern, relative to ``path``. Use ``**`` to recurse.
        path: Base directory to search from (default: working directory).
    """

    base = Path(path).expanduser()

    def _glob() -> list[Path]:
        return [m for m in base.glob(pattern) if m.is_file()]

    matches = await anyio.to_thread.run_sync(_glob)
    if not matches:
        return f"no files matching {pattern!r} under {base}"
    matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
    shown = matches[:_MAX_MATCHES]
    out = "\n".join(str(m) for m in shown)
    if len(matches) > _MAX_MATCHES:
        out += f"\n… [{len(matches) - _MAX_MATCHES} more matches]"
    return out


@tool(is_read_only=True, timeout_s=30.0)
async def grep(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    ignore_case: bool = False,
    output_mode: str = "content",
    context_lines: int = 0,
    file_type: str | None = None,
    head_limit: int = 0,
    multiline: bool = False,
) -> str:
    """Search file contents for a regex pattern. Prefers ripgrep (``rg``) and
    falls back to a Python walk.

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search (default: working directory).
        glob: Optional filename glob to restrict the search (e.g. ``*.py``).
        ignore_case: Case-insensitive match.
        output_mode: ``content`` → ``file:line:text`` rows (default);
            ``files_with_matches`` → just the matching file paths;
            ``count`` → ``file:count`` per file.
        context_lines: Lines of context to show around each match (like
            ``rg -C``). Only applies to ``content`` mode.
        file_type: Restrict to a language/type (``rg --type``), e.g. ``py``,
            ``rust``, ``js``. More convenient than a glob for a whole language.
        head_limit: Cap the number of output lines returned (0 = default cap).
        multiline: Let ``.`` and the pattern span line boundaries.
    """

    mode = output_mode if output_mode in ("content", "files_with_matches", "count") else "content"
    limit = head_limit if head_limit > 0 else _MAX_MATCHES

    rg = await _have_rg()
    if rg:
        cmd = ["rg", "--color=never"]
        if mode == "files_with_matches":
            cmd.append("--files-with-matches")
        elif mode == "count":
            cmd.append("--count")
        else:
            cmd += ["--line-number", "--no-heading"]
            if context_lines > 0:
                cmd += ["-C", str(context_lines)]
        if ignore_case:
            cmd.append("-i")
        if multiline:
            cmd += ["--multiline", "--multiline-dotall"]
        if glob:
            cmd += ["--glob", glob]
        if file_type:
            cmd += ["--type", file_type]
        cmd += ["--", pattern, path]
        result = await anyio.run_process(cmd, check=False, input=b"")
        out = result.stdout.decode("utf-8", "replace").rstrip()
        if result.returncode == 1 and not out:
            return f"no matches for {pattern!r} in {path}"
        if result.returncode > 1:
            err = result.stderr.decode("utf-8", "replace").strip()
            raise ValueError(err or f"grep failed (exit {result.returncode})")
        out = _head(out, limit)
        return _truncate(out, _MAX_OUTPUT)

    return await anyio.to_thread.run_sync(
        _py_grep, pattern, path, glob, ignore_case, mode, context_lines,
        file_type, limit, multiline,
    )


def _head(text: str, limit: int) -> str:
    """Keep the first ``limit`` lines, noting how many were dropped."""
    lines = text.split("\n")
    if len(lines) <= limit:
        return text
    dropped = len(lines) - limit
    return "\n".join(lines[:limit]) + f"\n… [{dropped} more lines truncated]"


# rg --type name → file-extension globs, for the Python fallback.
_TYPE_EXTS = {
    "py": (".py", ".pyi"), "python": (".py", ".pyi"),
    "js": (".js", ".jsx", ".mjs"), "ts": (".ts", ".tsx"),
    "rust": (".rs",), "go": (".go",), "java": (".java",), "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".hpp", ".h"), "rb": (".rb",), "ruby": (".rb",),
    "md": (".md", ".markdown"), "json": (".json",), "yaml": (".yaml", ".yml"),
    "toml": (".toml",), "sh": (".sh", ".bash"), "html": (".html", ".htm"),
    "css": (".css", ".scss"),
}


async def _have_rg() -> bool:
    try:
        r = await anyio.run_process(["rg", "--version"], check=False, input=b"")
        return r.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _py_grep(
    pattern: str, path: str, glob: str | None, ignore_case: bool,
    mode: str = "content", context_lines: int = 0, file_type: str | None = None,
    limit: int = _MAX_MATCHES, multiline: bool = False,
) -> str:
    import re

    flags = re.IGNORECASE if ignore_case else 0
    if multiline:
        flags |= re.DOTALL
    rx = re.compile(pattern, flags)
    base = Path(path).expanduser()
    exts = _TYPE_EXTS.get(file_type or "", ())
    files: list[Path]
    if base.is_file():
        files = [base]
    else:
        files = [p for p in base.rglob(glob or "*") if p.is_file()]
    if exts:
        files = [f for f in files if f.suffix in exts]

    out: list[str] = []
    for f in files:
        if ".git" + os.sep in str(f):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if multiline:
            if rx.search(text):
                if mode == "files_with_matches":
                    out.append(str(f))
                elif mode == "count":
                    out.append(f"{f}:{len(rx.findall(text))}")
                else:
                    out.append(f"{f}: (multiline match)")
            if len(out) >= limit:
                break
            continue
        lines = text.splitlines()
        matched = [n for n, ln in enumerate(lines) if rx.search(ln)]
        if not matched:
            continue
        if mode == "files_with_matches":
            out.append(str(f))
        elif mode == "count":
            out.append(f"{f}:{len(matched)}")
        else:
            shown: set[int] = set()
            for n in matched:
                lo, hi = max(0, n - context_lines), min(len(lines), n + context_lines + 1)
                for i in range(lo, hi):
                    if i in shown:
                        continue
                    shown.add(i)
                    sep = ":" if i == n else "-"
                    out.append(f"{f}:{i + 1}{sep}{lines[i][:_MAX_LINE]}")
                    if len(out) >= limit:
                        break
                if len(out) >= limit:
                    break
        if len(out) >= limit:
            out.append("… [more matches truncated]")
            break
    return "\n".join(out) if out else f"no matches for {pattern!r} in {path}"


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

CODING_TOOLS: tuple[Tool, ...] = (
    bash,
    bash_output,
    read_file,
    write_file,
    edit_file,
    multi_edit,
    ls,
    glob,
    grep,
)

__all__ = [
    "CODING_TOOLS",
    "bash",
    "bash_output",
    "read_file",
    "write_file",
    "edit_file",
    "multi_edit",
    "ls",
    "glob",
    "grep",
]
