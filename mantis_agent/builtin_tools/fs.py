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
from pathlib import Path

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
async def bash(command: str, timeout: int = 120) -> str:
    """Run a shell command and return its combined stdout + stderr.

    Use this to inspect the system, run builds/tests, git, grep, find, etc.
    Runs through ``bash -lc`` in the current working directory.

    Args:
        command: The shell command line to execute.
        timeout: Hard timeout in seconds (default 120). The command is killed
            if it exceeds this.
    """

    # Models pass loose values — strings, 0, absurd numbers. Clamp to a sane
    # window so e.g. ``timeout: 0`` doesn't either fire instantly or hang.
    timeout = _coerce_int(timeout, default=120, lo=1, hi=600)

    try:
        with anyio.fail_after(timeout):
            result = await anyio.run_process(
                ["bash", "-lc", command], check=False, input=b""
            )
    except TimeoutError:
        raise TimeoutError(f"command timed out after {timeout}s: {command}") from None

    out = result.stdout.decode("utf-8", "replace")
    err = result.stderr.decode("utf-8", "replace")
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
# Files
# ---------------------------------------------------------------------------


@tool(is_read_only=True)
async def read_file(path: str, offset: int = 1, limit: int = _MAX_READ_LINES) -> str:
    """Read a text file and return it with 1-based line numbers (``cat -n`` style).

    Args:
        path: File to read (absolute or relative to the working directory).
        offset: 1-based line number to start from (default 1).
        limit: Maximum number of lines to return (default 2000).
    """

    offset = _coerce_int(offset, default=1, lo=1)
    limit = _coerce_int(limit, default=_MAX_READ_LINES, lo=1, hi=50_000)

    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {path}")
    if p.is_dir():
        raise IsADirectoryError(f"{path} is a directory — use ls instead")

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

    p = Path(path).expanduser()

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")

    await anyio.to_thread.run_sync(_write)
    n = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"wrote {len(content)} bytes ({n} lines) to {p}"


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
        raise ValueError(f"old_string not found in {path}")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string is not unique in {path} ({count} matches) — add more "
            f"context or pass replace_all=true"
        )
    updated = text.replace(old_string, new_string)
    await anyio.to_thread.run_sync(lambda: p.write_text(updated, "utf-8"))
    return f"edited {p} ({count} replacement{'s' if count != 1 else ''})"


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
    pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False
) -> str:
    """Search file contents for a regex pattern and return matching ``file:line:text``
    rows. Prefers ripgrep (``rg``) and falls back to a Python walk.

    Args:
        pattern: Regular expression to search for.
        path: File or directory to search (default: working directory).
        glob: Optional filename glob to restrict the search (e.g. ``*.py``).
        ignore_case: Case-insensitive match.
    """

    rg = await _have_rg()
    if rg:
        cmd = ["rg", "--line-number", "--no-heading", "--color=never", "-m", "50"]
        if ignore_case:
            cmd.append("-i")
        if glob:
            cmd += ["--glob", glob]
        cmd += ["--", pattern, path]
        result = await anyio.run_process(cmd, check=False, input=b"")
        out = result.stdout.decode("utf-8", "replace").rstrip()
        if result.returncode == 1 and not out:
            return f"no matches for {pattern!r} in {path}"
        if result.returncode > 1:
            err = result.stderr.decode("utf-8", "replace").strip()
            raise ValueError(err or f"grep failed (exit {result.returncode})")
        return _truncate(out, _MAX_OUTPUT)

    return await anyio.to_thread.run_sync(
        _py_grep, pattern, path, glob, ignore_case
    )


async def _have_rg() -> bool:
    try:
        r = await anyio.run_process(["rg", "--version"], check=False, input=b"")
        return r.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _py_grep(pattern: str, path: str, glob: str | None, ignore_case: bool) -> str:
    import re

    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(pattern, flags)
    base = Path(path).expanduser()
    files: list[Path]
    if base.is_file():
        files = [base]
    else:
        files = [p for p in base.rglob(glob or "*") if p.is_file()]
    hits: list[str] = []
    for f in files:
        if ".git" + os.sep in str(f):
            continue
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{f}:{n}:{line.rstrip()[:_MAX_LINE]}")
                        if len(hits) >= _MAX_MATCHES:
                            hits.append("… [more matches truncated]")
                            return "\n".join(hits)
        except OSError:
            continue
    return "\n".join(hits) if hits else f"no matches for {pattern!r} in {path}"


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

CODING_TOOLS: tuple[Tool, ...] = (
    bash,
    read_file,
    write_file,
    edit_file,
    ls,
    glob,
    grep,
)

__all__ = [
    "CODING_TOOLS",
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "ls",
    "glob",
    "grep",
]
