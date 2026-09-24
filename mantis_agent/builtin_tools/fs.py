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

import asyncio
import functools
import logging
import os
import re
import shutil
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from ..tools import Tool, tool

_LOG = logging.getLogger("mantis_agent.builtin_tools.fs")

# Caps — keep tool output from swamping the model's context window.
_MAX_OUTPUT = 30_000  # chars of bash stdout/stderr returned
_MAX_READ_LINES = 2000  # default lines per read_file call
_MAX_READ_OUTPUT = 200_000  # total chars of file text returned by read_file
_MAX_LINE = 2000  # chars per line before truncation
_MAX_MATCHES = 200  # grep/glob hits returned
# Directories glob skips by default (dependency / VCS / build output), matching
# ripgrep's gitignore-aware behavior. Bypassed when a call explicitly targets one.
_GLOB_IGNORE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".next",
    "target", ".cache", "vendor", ".gradle", ".egg-info",
}
# Bounds for the glob tree walk — cap entries examined / matches gathered / wall
# time so a broad pattern on a huge tree can't peg a worker thread indefinitely.
_GLOB_MAX_SCAN = 100_000
_GLOB_MAX_COLLECT = 5_000
_GLOB_DEADLINE_S = 20.0


# ---------------------------------------------------------------------------
# Per-agent isolation. The read-guard, foreground bash cwd, and background
# shells are all conversation state — sharing them process-wide leaks across
# concurrently-running agents/subagents: a subagent's ``cd`` would move the
# parent's shell, one agent could kill another's background shells, and the
# read-before-write guard would be defeatable cross-agent. Each Agent run sets
# ``TOOL_SCOPE`` (a ContextVar) to a unique key for the duration of its run;
# these dicts are keyed by that scope. Direct callers (tests, ad-hoc use) fall
# back to the shared ``"__global__"`` scope, preserving old behaviour.
# ---------------------------------------------------------------------------
import contextvars  # noqa: E402

TOOL_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "mantis_tool_scope", default="__global__"
)


def _scope() -> str:
    return TOOL_SCOPE.get()


#: The agent's working directory, if it set one. Relative paths handed to any
#: file tool resolve against this instead of the host process's cwd.
#:
#: WHY THIS EXISTS: ``Agent(cwd=...)`` is reported to the model in its env
#: context block ("Working directory: /x"), so the model then emits relative
#: paths in good faith. Without this var, ``Path("out.py")`` resolved against
#: whatever directory the *host process* happened to be in — so the model was
#: told one directory and its tools wrote to another. Files landed outside the
#: intended tree, and the agent's own follow-up ``ls`` disagreed with its own
#: writes. A server running two agents over two projects had no way to keep
#: them apart short of ``os.chdir``, which is process-global and races.
#:
#: ``None`` (the default) means "use the process cwd" — identical to the old
#: behavior, so nothing changes for callers that don't set it.
AGENT_CWD: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mantis_agent_cwd", default=None
)


def agent_cwd() -> str | None:
    """The active agent working directory, or None for the process cwd."""
    return AGENT_CWD.get()


#: Opt-in live output for the foreground ``bash`` tool. When set, stdout and
#: stderr are read incrementally as the child produces them and every decoded,
#: control-stripped chunk is handed to the callback ``on_output(chunk: str)`` —
#: so a terminal can render a build's progress instead of a blank spinner until
#: the command exits. ``None`` (the default) keeps the buffered
#: ``proc.communicate()`` path byte-for-byte unchanged. A ContextVar, like the
#: scope/cwd above, so a UI can install it once in its main task (child tasks
#: inherit the context) or scope it to a single run with the returned token.
#: The tool's own result string is unaffected: same truncation, timeout, kill
#: and cwd-persistence semantics whether or not a sink is installed.
BashOutputSink = Callable[[str], Any]
BASH_OUTPUT_SINK: contextvars.ContextVar[BashOutputSink | None] = contextvars.ContextVar(
    "mantis_bash_output_sink", default=None
)


def set_bash_output_sink(on_output: BashOutputSink | None) -> contextvars.Token:
    """Install ``on_output`` as the live-output sink for foreground ``bash``
    calls made from this context (and tasks spawned after this call). Pass
    ``None`` to switch the streaming path off. Returns a token for
    :func:`reset_bash_output_sink`."""
    return BASH_OUTPUT_SINK.set(on_output)


def reset_bash_output_sink(token: contextvars.Token) -> None:
    """Restore the sink that was active before the matching ``set`` call."""
    BASH_OUTPUT_SINK.reset(token)


def bash_output_sink() -> BashOutputSink | None:
    """The live-output sink active in this context, or ``None``."""
    return BASH_OUTPUT_SINK.get()


def resolve_path(path: str | Path) -> Path:
    """Resolve a tool-supplied path the way the *model* expects it to resolve.

    Absolute paths and ``~`` are honored as given. A relative path is joined
    onto the agent's working directory when one is set, so what the model was
    told in its env block and what the tools actually touch are the same place.
    """

    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    base = AGENT_CWD.get()
    return (Path(base).expanduser() / p) if base else p


# Read-before-write guard (Claude Code's readFileState). Tracks the mtime of
# every file a tool has *seen* (read or written) this run. write_file then
# refuses to clobber an existing file the tools haven't seen, or one changed on
# disk since — so unseen/newer content is never silently destroyed.
# ``_FILE_READS`` is the default ("__global__") scope's dict — kept as a
# module attribute so direct callers (and tests) can reach it; other scopes get
# their own dict on demand.
_FILE_READS: dict[str, float] = {}
_FILE_READS_BY_SCOPE: dict[str, dict[str, float]] = {"__global__": _FILE_READS}


def _reads() -> dict[str, float]:
    return _FILE_READS_BY_SCOPE.setdefault(_scope(), {})


def _record_seen(p: Path) -> None:
    try:
        _reads()[str(p.resolve())] = p.stat().st_mtime
    except OSError:
        pass


def _check_write_guard(p: Path) -> None:
    if not p.exists() or not p.is_file():
        return  # new file — nothing to clobber
    try:
        if p.stat().st_size == 0:
            return  # empty file (e.g. just `touch`ed) — no unseen content to lose
    except OSError:
        pass
    seen = _reads().get(str(p.resolve()))
    if seen is None:
        raise ValueError(
            f"{p} already exists but hasn't been read this session. Read it first "
            f"so you don't overwrite content you haven't seen (write_file replaces "
            f"the ENTIRE file). Use edit_file for a targeted change."
        )
    try:
        if p.stat().st_mtime > seen + 1e-6:
            raise ValueError(
                f"{p} was modified on disk since you last read it. Read it again "
                f"before writing so you don't clobber the newer version."
            )
    except OSError:
        pass


def _human_size(n: int) -> str:
    """A compact human-readable byte size: ``512 B``, ``1.2 KB``, ``3.4 MB``."""
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def _executor_cap(default: int) -> int:
    """The char cap the executor will hold this call's result to (it shrinks
    with the model's context window), or ``default`` when the tool is called
    directly. Self-limiting to it lets a tool cut cleanly and say how to get
    the rest, instead of the executor's backstop eliding the middle."""
    from ..streaming.executor import tool_result_char_cap  # noqa: PLC0415

    return tool_result_char_cap() or default


# Shell output keeps more tail than head: the failing test summary, the last
# compiler error and the exit status are at the end.
_SHELL_HEAD_RATIO = 0.4
# Full outputs of truncated foreground commands, saved so the model can grep or
# read the elided middle. Oldest pruned past this many — per agent scope (see
# TOOL_SCOPE), so a subagent's churn can't delete a file the parent's model was
# just told about.
_MAX_SAVED_OUTPUTS = 20
_SAVED_OUTPUTS: list[str] = []
_SAVED_OUTPUTS_BY_SCOPE: dict[str, list[str]] = {"__global__": _SAVED_OUTPUTS}
# One spill file never grows past this; the cut is marked in the file.
_SPILL_MAX_BYTES = 20 * 1024 * 1024
# The spill directory: private (0700) and per process, under the mantis state
# dir — NOT the system temp dir, which the sandbox binds read-write into every
# sandboxed child. Created lazily, removed at exit.
_SPILL_DIR: list[str | None] = [None]


def _spill_session() -> str:
    return f"bash-output-{os.getpid()}"


def _spill_dir() -> str | None:
    d = _SPILL_DIR[0]
    if d and os.path.isdir(d):
        return d
    first = d is None
    try:
        from ..sandbox_tmpdir import private_tmpdir  # noqa: PLC0415

        d = str(private_tmpdir(_spill_session()))
    except Exception:  # noqa: BLE001 — fall back to a private mkdtemp
        import tempfile  # noqa: PLC0415

        try:
            d = tempfile.mkdtemp(prefix="mantis-bash-out-")
        except OSError:
            return None
    _SPILL_DIR[0] = d
    if first:
        import atexit  # noqa: PLC0415

        atexit.register(_cleanup_spills)
    return d


def _cleanup_spills() -> None:
    """Remove every saved output and the spill directory. Never raises."""
    for paths in _SAVED_OUTPUTS_BY_SCOPE.values():
        while paths:
            try:
                os.unlink(paths.pop())
            except OSError:
                pass
    d = _SPILL_DIR[0]
    if d:
        shutil.rmtree(d, ignore_errors=True)
        try:
            from ..sandbox_tmpdir import cleanup  # noqa: PLC0415

            cleanup(_spill_session())  # drops the owner record too
        except Exception:  # noqa: BLE001
            pass


def _new_spill_file(prefix: str) -> tuple[int, str] | None:
    """``(fd, path)`` of a fresh 0600 file in the private spill dir, or None."""
    import tempfile  # noqa: PLC0415

    d = _spill_dir()
    if d is None:
        return None
    try:
        return tempfile.mkstemp(prefix=prefix, suffix=".log", dir=d)
    except OSError:
        return None


def _register_saved_output(path: str) -> None:
    saved = _SAVED_OUTPUTS_BY_SCOPE.setdefault(_scope(), [])
    saved.append(path)
    while len(saved) > _MAX_SAVED_OUTPUTS:
        try:
            os.unlink(saved.pop(0))
        except OSError:
            pass


def _spill_cut_marker(dropped: int) -> bytes:
    return f"\n… [saved output cut at {_SPILL_MAX_BYTES:,} bytes — {dropped:,} more bytes not saved]\n".encode()


def _save_full_output(text: str) -> str | None:
    """Write ``text`` (capped at ``_SPILL_MAX_BYTES``) to a private spill file
    and return its path (None on any failure — saving is a convenience, never
    a reason to fail the command)."""
    made = _new_spill_file("mantis-bash-")
    if made is None:
        return None
    fd, path = made
    data = text.encode("utf-8", "replace")
    if len(data) > _SPILL_MAX_BYTES:
        data = data[:_SPILL_MAX_BYTES] + _spill_cut_marker(len(data) - _SPILL_MAX_BYTES)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    _register_saved_output(path)
    return path


def _truncate_shell(body: str, *, prefix: str = "", suffix: str = "",
                    full_output_path: str | None = None) -> str:
    """Head + tail truncation for shell output, sized to the executor's cap.

    ``prefix`` / ``suffix`` (a status header, the ``[exit code: N]`` line) are
    kept verbatim OUTSIDE the truncated body so they can never be elided. When
    the body is cut, the note names a file holding the full text: the caller's
    ``full_output_path`` (a background shell's log) or a freshly saved copy.
    The result fits the cap — the executor's backstop never has to re-cut it
    (which could elide the note naming that file)."""
    from ..streaming.executor import truncate_middle  # noqa: PLC0415

    room = _executor_cap(_MAX_OUTPUT) - len(prefix) - len(suffix)
    if len(body) <= room:
        return f"{prefix}{body}{suffix}"
    path = full_output_path or _save_full_output(body)
    hint = (
        f"Full output saved to {path} — grep it, or read_file it with offset/limit."
        if path
        else "Re-run with a filter (grep / head / tail) to see the middle."
    )
    note = f"[{len(body):,} characters of output elided. {hint}]"
    if room < len(note) + 400:
        # Too little room for head + tail + note: the pointer to the full
        # output is the one thing worth keeping.
        return f"{prefix}{note}{suffix}"
    kept = truncate_middle(body, room, head_ratio=_SHELL_HEAD_RATIO, hint=hint)
    return f"{prefix}{kept}{suffix}"


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    return f"{head}\n… [truncated {len(text) - limit} chars]"


def _path_suggestion(p: Path) -> str:
    """A ``. Did you mean <path>?`` hint when a near-name file exists in the same
    directory — so a model that guessed a slightly-wrong path self-corrects in one
    step instead of flailing."""
    import difflib  # noqa: PLC0415

    parent = p.parent
    try:
        if not parent.is_dir():
            return ""
        names = [e.name for e in parent.iterdir()]
    except OSError:
        return ""
    close = difflib.get_close_matches(p.name, names, n=1, cutoff=0.6)
    if close and close[0] != p.name:
        return f". Did you mean {parent / close[0]}?"
    return ""


def _missing_file_error(path: str, p: Path) -> FileNotFoundError:
    return FileNotFoundError(f"no such file: {path}{_path_suggestion(p)}")


# Spaces only (read_file right-justifies with spaces): ``\s`` would also eat
# newlines, merging a blank line into the next numbered one.
_LINE_NUM_PREFIX_RE = re.compile(r"(?m)^ *\d+\t")


def _strip_line_numbers(s: str) -> str:
    """Remove ``<num>\\t`` prefixes that ``read_file`` adds to each line — models
    constantly copy that numbered output straight into an edit's old_string.
    Only when EVERY non-empty line carries one: a partial hit is real content
    (TSV rows, ``1\\tfoo`` data), not a copied read_file excerpt."""
    lines = s.split("\n")
    if not all(_LINE_NUM_PREFIX_RE.match(ln) for ln in lines if ln.strip()):
        return s
    return _LINE_NUM_PREFIX_RE.sub("", s)


def _reconcile_old_string(old_string: str, text: str) -> str:
    """If ``old_string`` isn't in ``text`` but its line-number-stripped form is,
    return the stripped form (auto-fixing the copied-read-output mistake).
    Otherwise return ``old_string`` unchanged."""
    if old_string in text:
        return old_string
    stripped = _strip_line_numbers(old_string)
    if stripped != old_string and stripped in text:
        return stripped
    return old_string


def _numbered_block(lines: list[str], first: int) -> str:
    """``lines`` rendered in read_file's ``<num>\\t<text>`` format."""
    width = len(str(first + len(lines) - 1))
    return "\n".join(f"{str(first + i).rjust(width)}\t{ln[:_MAX_LINE]}"
                     for i, ln in enumerate(lines))


def _not_found_hint(old_string: str, text: str, path: str) -> str:
    """An *actionable* edit-miss error. A model that gets only 'not found' tends
    to retry blindly; pointing it at the likely cause (stale/auto-formatted text,
    whitespace) and the closest real block — numbered exactly like read_file, so
    it can be copied verbatim — lets it self-correct in one step."""
    import difflib

    hint = (
        f"old_string not found in {path}. The file's text differs from what you "
        f"expected (whitespace, or it changed). Read the file again to copy the "
        f"exact current text before editing."
    )
    old_lines = _old_lines(old_string)[0]
    if not any(ln.strip() for ln in old_lines):
        return hint
    file_lines = text.split("\n")
    # The numbered closest-block hint only for modest old_strings: past ~40 lines
    # a block hint is too long to be useful and the search isn't worth it.
    if len(file_lines) <= _FUZZY_MAX_LINES and len(old_lines) <= _HINT_MAX_OLD_LINES:
        scores = _window_scores(file_lines, old_lines, 0.5)
        if scores:
            start = scores[0][1]
            block = file_lines[start:start + min(len(old_lines), 40)]
            return (
                f"{hint} Closest block in the file (lines {start + 1}-"
                f"{start + len(block)}; copy the text after each number+tab "
                f"exactly):\n{_numbered_block(block, start + 1)}"
            )
    probe = next((ln.strip() for ln in old_lines if ln.strip()), "")[:_SIM_LINE_CAP]
    near = difflib.get_close_matches(
        probe, [ln.strip()[:_SIM_LINE_CAP] for ln in file_lines[:_FUZZY_MAX_LINES * 4]],
        n=1, cutoff=0.6)
    if near:
        hint += f" Closest line in the file is: {near[0]!r}"
    return hint


# -- tolerant edit matching ---------------------------------------------------
# Small open models constantly get an edit's whitespace slightly wrong (tabs vs
# spaces, one indent level off, trailing blanks) or paraphrase one token. Rather
# than bounce the edit and force a re-read, ``_apply_edit`` runs a cascade of
# progressively looser matchers; each stage is accepted ONLY when it finds
# exactly one match (or replace_all is set, for the non-fuzzy stages), and the
# result names the rule that fired so the behaviour stays transparent.

_FUZZY_MAX_LINES = 5000     # fuzzy / closest-block search only on files this size
_FUZZY_THRESHOLD = 0.9      # min similarity for a fuzzy block match
_FUZZY_MARGIN = 0.05        # best must beat any other (non-overlapping) block by this
_FUZZY_EXACT_LINES = 0.7    # fuzzy also needs this share of lines identical (stripped)
_FUZZY_PEAKS = 6            # candidate blocks scored char-level after the token prefilter
_FUZZY_BUDGET_S = 0.5       # wall-clock cap on the char-level scoring
_HINT_MAX_OLD_LINES = 40    # numbered closest-block hint only for old_strings this size
_SIM_LINE_CAP = 400         # chars per line fed to char-level similarity


def _old_lines(old: str) -> tuple[list[str], bool]:
    """``old`` split into lines, plus whether it ended with a newline (the final
    newline is part of the matched region, not an extra empty line)."""
    ends_nl = old.endswith("\n")
    return (old[:-1] if ends_nl else old).split("\n"), ends_nl


def _match_line_numbers(text: str, needle: str, limit: int = 10) -> list[int]:
    out: list[int] = []
    start = 0
    while len(out) < limit:
        i = text.find(needle, start)
        if i < 0:
            break
        out.append(text.count("\n", 0, i) + 1)
        start = i + len(needle)
    return out


def _ambiguous(path: str, count: int, lines: list[int], how: str = "") -> ValueError:
    at = ", ".join(map(str, lines[:10])) + (", …" if count > len(lines[:10]) else "")
    return ValueError(
        f"old_string is not unique in {path}{how} ({count} matches, at lines {at}) "
        f"— add more surrounding context or pass replace_all=true"
    )


def _line_matches(file_lines: list[str], old_lines: list[str],
                  key: Callable[[str], str]) -> list[int]:
    """Start indexes of non-overlapping whole-line matches of ``old_lines`` in
    ``file_lines`` under the normalisation ``key``."""
    want = [key(ln) for ln in old_lines]
    keys = [key(ln) for ln in file_lines]
    n = len(want)
    out: list[int] = []
    i = 0
    while i <= len(keys) - n:
        if keys[i] == want[0] and keys[i:i + n] == want:
            out.append(i)
            i += n
        else:
            i += 1
    return out


def _splice_lines(text: str, starts: list[int], n: int, ends_nl: bool,
                  new_for: Callable[[int], str]) -> str:
    """Replace the ``n``-line blocks beginning at each of ``starts``."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for ln in text.split("\n"):
        spans.append((pos, pos + len(ln)))
        pos += len(ln) + 1
    for s in sorted(starts, reverse=True):
        a, b = spans[s][0], spans[s + n - 1][1]
        if ends_nl and b < len(text):
            b += 1  # old_string ended with a newline — it's part of the region
        text = text[:a] + new_for(s) + text[b:]
    return text


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _ws_convert(old_ws: list[str], block_ws: list[str]) -> Callable[[str], str]:
    """How to re-express the model's leading whitespace in the file's: a
    tabs↔spaces conversion ONLY when it's unambiguous — ``old_string`` indents
    uniformly with one kind and the matched block uniformly with the other, and
    the spaces side has a clear unit (≥2). Anything else is left alone (the
    caller then just shifts by a constant prefix); guessing an indent unit from
    content lines is what used to mangle docstrings, hanging indents and YAML."""
    import math  # noqa: PLC0415

    old_ws = [w for w in old_ws if w]
    block_ws = [w for w in block_ws if w]
    if not old_ws or not block_ws:
        return lambda w: w

    def _unit(ws: list[str]) -> int:
        u = 0
        for w in ws:
            u = math.gcd(u, len(w))
        return u

    def _all(ws: list[str], ch: str) -> bool:
        return all(set(w) == {ch} for w in ws)

    if _all(old_ws, " ") and _all(block_ws, "\t") and (u := _unit(old_ws)) >= 2:
        def _to_tabs(w: str) -> str:
            if set(w) != {" "}:
                return w
            return "\t" * (len(w) // u) + " " * (len(w) % u)
        return _to_tabs
    if _all(old_ws, "\t") and _all(block_ws, " ") and (u := _unit(block_ws)) >= 2:
        def _to_spaces(w: str) -> str:
            body = w.lstrip("\t")
            return " " * (u * (len(w) - len(body))) + body
        return _to_spaces
    return lambda w: w


def _reindent(new: str, old_lines: list[str], block: list[str]) -> str:
    """Shift every non-blank line of ``new`` by the CONSTANT prefix delta between
    the first matched ``old_lines`` line's leading whitespace and the file
    ``block``'s (after an unambiguous tabs↔spaces conversion, see
    ``_ws_convert``). Relative indentation inside ``new`` — string bodies,
    hanging indents, odd YAML scalars — is preserved exactly. A no-op when the
    indentation already agrees."""
    pairs = [(o, f) for o, f in zip(old_lines, block, strict=False) if o.strip() and f.strip()]
    if not pairs or all(_leading_ws(o) == _leading_ws(f) for o, f in pairs):
        return new
    conv = _ws_convert([_leading_ws(o) for o, _ in pairs], [_leading_ws(f) for _, f in pairs])
    o_ws, f_ws = conv(_leading_ws(pairs[0][0])), _leading_ws(pairs[0][1])
    n = 0  # length of the common prefix
    while n < min(len(o_ws), len(f_ws)) and o_ws[n] == f_ws[n]:
        n += 1
    cut, add = len(o_ws) - n, f_ws[n:]
    out = []
    for ln in new.split("\n"):
        if not ln.strip():
            out.append(ln)
            continue
        ws = conv(_leading_ws(ln))
        body = ln.lstrip(" \t")
        if ws.startswith(o_ws):
            ws = f_ws + ws[len(o_ws):]
        else:  # shallower than the anchor: strip what's there, down to 0
            ws = add + ws[min(cut, len(ws)):]
        out.append(ws + body)
    return "\n".join(out)


_TOKEN_RE = re.compile(r"\w+|[^\w\s]+")


def _block_similarity(a: list[str], b: list[str]) -> float:
    """Char-level similarity of two equal-length line blocks, computed line by
    line (whitespace-stripped, length-weighted). Per-line ``ratio()`` stays
    cheap where one ratio over the joined block is quadratic in its size."""
    import difflib  # noqa: PLC0415

    sm = difflib.SequenceMatcher(None, autojunk=False)
    num = den = 0.0
    for x, y in zip(a, b, strict=False):
        x, y = x.strip()[:_SIM_LINE_CAP], y.strip()[:_SIM_LINE_CAP]
        w = len(x) + len(y)
        if not w:
            continue
        den += w
        if x == y:
            num += w
        else:
            sm.set_seqs(x, y)
            num += sm.ratio() * w
    return num / den if den else 0.0


def _exact_line_share(a: list[str], b: list[str]) -> tuple[float, int]:
    """``(share, differing)``: how many non-blank line pairs are identical after
    ``strip()``, and how many differ."""
    pairs = [(x.strip(), y.strip()) for x, y in zip(a, b, strict=False) if x.strip() or y.strip()]
    diff = sum(1 for x, y in pairs if x != y)
    return ((len(pairs) - diff) / len(pairs) if pairs else 0.0), diff


def _window_scores(file_lines: list[str], old_lines: list[str],
                   floor: float) -> list[tuple[float, int]]:
    """``(similarity, start)`` for the best ``len(old_lines)``-line windows of
    the file scoring ≥ ``floor`` (whitespace ignored), best first.

    Two-phase so it stays linear on big files: a token-bag overlap computed for
    EVERY window with a sliding counter, then the char-level
    ``_block_similarity`` only around the few best non-overlapping peaks —
    under a wall-clock budget (this runs off the event loop, but still)."""
    n = len(old_lines)
    total = len(file_lines) - n + 1
    if n == 0 or total <= 0:
        return []
    want: dict[str, int] = {}
    for ln in old_lines:
        for t in _TOKEN_RE.findall(ln[:_SIM_LINE_CAP]):
            want[t] = want.get(t, 0) + 1
    want_total = sum(want.values())
    if not want_total:
        return []
    toks = [_TOKEN_RE.findall(ln[:_SIM_LINE_CAP]) for ln in file_lines]
    have: dict[str, int] = {}
    overlap = size = 0

    def _add(line: list[str], d: int) -> None:
        nonlocal overlap, size
        for t in line:
            c = have.get(t, 0)
            if d > 0 and c < want.get(t, 0):
                overlap += 1
            elif d < 0 and c <= want.get(t, 0):
                overlap -= 1
            have[t] = c + d
        size += d * len(line)

    for ln in toks[:n - 1]:
        _add(ln, 1)
    token_score: list[float] = []
    for i in range(total):
        _add(toks[i + n - 1], 1)
        token_score.append(2 * overlap / (size + want_total))
        _add(toks[i], -1)

    # Peaks: best token score first, suppressing windows overlapping a pick.
    peaks: list[int] = []
    for i in sorted(range(total), key=lambda k: (-token_score[k], k)):
        if len(peaks) >= _FUZZY_PEAKS or token_score[i] < floor / 2:
            break
        if all(abs(i - p) >= n for p in peaks):
            peaks.append(i)

    deadline = time.monotonic() + _FUZZY_BUDGET_S
    best: dict[int, float] = {}
    for p in peaks:
        for i in range(max(0, p - 2), min(total, p + 3)):
            if i in best:
                continue
            if time.monotonic() > deadline:
                break
            best[i] = _block_similarity(file_lines[i:i + n], old_lines)
    out = [(r, i) for i, r in best.items() if r >= floor]
    out.sort(key=lambda t: (-t[0], t[1]))
    return out


def _apply_edit(text: str, old: str, new: str, replace_all: bool,
                path: str) -> tuple[str, str]:
    """Apply one edit to LF-normalised ``text`` via the matching cascade.
    Returns ``(updated_text, rule_note)``; the note is "" for an exact match.
    Raises ``ValueError`` (with an actionable hint) on a miss or ambiguity."""
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")

    # 1. exact  2. read_file line-number prefixes stripped
    candidate, note = old, ""
    count = text.count(old)
    stripped = _strip_line_numbers(old)
    if count == 0 and stripped != old and stripped in text:
        candidate, count, note = stripped, text.count(stripped), "matched after stripping copied line numbers"
    if count:
        if count > 1 and not replace_all:
            raise _ambiguous(path, count, _match_line_numbers(text, candidate))
        return text.replace(candidate, new), note

    # Line-based stages work on the numbered-prefix-free form when every line
    # carried one (a copied read_file excerpt with the whitespace also off).
    # (``_strip_line_numbers`` only strips when every line carried one.)
    old_lines, ends_nl = _old_lines(stripped)
    if not any(ln.strip() for ln in old_lines):
        raise ValueError(_not_found_hint(old, text, path))
    file_lines = text.split("\n")
    n = len(old_lines)

    # 3. trailing whitespace ignored  4. indentation ignored (new re-indented)
    for key, how, reindent in ((str.rstrip, "ignoring trailing whitespace", False),
                               (str.strip, "ignoring indentation", True)):
        starts = _line_matches(file_lines, old_lines, key)
        if not starts:
            continue
        if len(starts) > 1 and not replace_all:
            raise _ambiguous(path, len(starts), [s + 1 for s in starts], f" when {how}")

        def _new_for(s: int, _re: bool = reindent) -> str:
            return _reindent(new, old_lines, file_lines[s:s + n]) if _re else new

        suffix = f", {len(starts)} places" if len(starts) > 1 else ""
        return _splice_lines(text, starts, n, ends_nl, _new_for), f"matched {how}{suffix}"

    # 5. unique fuzzy block — never for replace_all (it could hit near-misses).
    if (not replace_all and len(file_lines) <= _FUZZY_MAX_LINES
            and sum(1 for ln in old_lines if ln.strip()) >= 2):
        scores = _window_scores(file_lines, old_lines, _FUZZY_THRESHOLD - _FUZZY_MARGIN)
        if scores and scores[0][0] >= _FUZZY_THRESHOLD:
            best_r, best = scores[0]
            rival = next(((r, i) for r, i in scores[1:] if abs(i - best) >= n), None)
            if rival and rival[0] >= best_r - _FUZZY_MARGIN:
                raise ValueError(
                    f"old_string not found exactly in {path}, and it is ambiguous: "
                    f"similar blocks at lines {best + 1} and {rival[1] + 1}. Read the "
                    f"file and copy the exact text of the one you mean."
                )
            # Char similarity alone lets a stale/rewritten block through (every
            # line one typo off still scores high); most lines must be identical.
            share, differing = _exact_line_share(file_lines[best:best + n], old_lines)
            if share >= _FUZZY_EXACT_LINES:
                updated = _splice_lines(
                    text, [best], n, ends_nl,
                    lambda s: _reindent(new, old_lines, file_lines[s:s + n]))
                return updated, (f"fuzzy-matched lines {best + 1}-{best + n}, {best_r:.0%} "
                                 f"similar, {differing} line{'s' * (differing != 1)} differed")

    raise ValueError(_not_found_hint(old, text, path))


# -- line endings / BOM -------------------------------------------------------
_UTF8_BOM = b"\xef\xbb\xbf"


def _read_for_edit(p: Path) -> tuple[str, str, bool]:
    """``(lf_text, newline, has_bom)``: the file decoded with its line endings
    normalised to LF for matching, plus what to restore on write."""
    raw = p.read_bytes()
    bom = raw.startswith(_UTF8_BOM)
    s = raw[len(_UTF8_BOM):].decode("utf-8", "replace") if bom else raw.decode("utf-8", "replace")
    crlf = s.count("\r\n")
    lf = s.count("\n") - crlf
    if crlf > lf:
        return s.replace("\r\n", "\n"), "\r\n", bom
    if lf == 0 and crlf == 0 and "\r" in s:  # classic-Mac CR-only file
        return s.replace("\r", "\n"), "\r", bom
    return s.replace("\r\n", "\n"), "\n", bom


def _encode_for_write(text: str, newline: str, bom: bool) -> bytes:
    if newline != "\n":
        text = text.replace("\n", newline)
    return (_UTF8_BOM if bom else b"") + text.encode("utf-8")


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


def _is_secret_env(name: str) -> bool:
    """Is this environment variable a credential we must NOT hand to a
    model-driven shell command? Matches the usual secret-bearing name shapes
    (API keys, tokens, passwords, access keys). Fails closed — matches broadly.

    This is the *unsandboxed* filter (and the one :mod:`mantis_agent.watch`
    reuses). It is a denylist, so it only ever catches the names someone
    thought of: ``MY_DB_DSN=postgres://u:pw@host`` and ``SSH_AUTH_SOCK`` sail
    straight through it. When the sandbox is on, ``_child_env`` below throws it
    away and uses the allowlist instead."""
    n = name.upper()
    if any(s in n for s in (
        "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
        "PRIVATE_KEY", "ACCESS_KEY", "API_KEY", "APIKEY",
    )):
        return True
    return n == "TOKEN" or n.endswith(("_TOKEN", "_KEY", "_KEY_ID"))


# Names the allowlist in :mod:`mantis_agent.redaction` doesn't know about but
# that a real build/test command needs: where the toolchain lives and where its
# caches go. Two of them are load-bearing for correctness rather than
# convenience — ``MANTIS_SANDBOX*`` is how confinement reaches a nested
# ``mantis``, and a nested agent that lost the flag would spawn *its* shells
# unconfined, which is the exact opposite of what the operator asked for.
#
# Nothing secret-shaped can be smuggled in here: ``build_child_env`` re-checks
# ``is_secret_name`` after ``extra_pass`` and drops the name anyway.
_SHELL_PASSTHROUGH: tuple[str, ...] = (
    # the sandbox's own switches — a policy must be inherited, not re-decided
    "MANTIS_SANDBOX", "MANTIS_SANDBOX_NETWORK", "MANTIS_SANDBOX_SCRUB_ENV",
    "MANTIS_AGENT_HOME",
    # python
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE",
    "PYENV_ROOT", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
    "UV_CACHE_DIR", "UV_PYTHON", "UV_PROJECT_ENVIRONMENT", "PIP_CACHE_DIR",
    # node / go / rust / jvm
    "NODE_ENV", "NODE_PATH", "NODE_OPTIONS", "NVM_DIR",
    "NPM_CONFIG_PREFIX", "NPM_CONFIG_CACHE",
    "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE", "GOFLAGS",
    "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME", "GRADLE_USER_HOME", "MAVEN_HOME",
    # platform, XDG, CI, and TLS trust stores (without these, https breaks in
    # exactly the confusing way that gets sandboxes disabled)
    "HOMEBREW_PREFIX", "HOMEBREW_CELLAR", "HOMEBREW_REPOSITORY",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    "CI", "SHLVL", "HOSTNAME", "COLORTERM", "MAKEFLAGS",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NIX_PATH", "PKG_CONFIG_PATH",
)

# Non-interactive by construction: models love to reach for ``nano``/``vim`` or
# commands that page (``git log``, ``less``) — those launch full-screen UIs that
# hang on the closed stdin or vomit terminal-control codes into the result.
# ``TERM=dumb`` + neutered pager/editor envs make them behave like a script.
_NONINTERACTIVE_ENV = {
    "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat",
    "EDITOR": "true", "VISUAL": "true",
    "GIT_TERMINAL_PROMPT": "0", "DEBIAN_FRONTEND": "noninteractive",
}


def _child_env(cwd: str | None = None) -> dict[str, str]:
    """The environment for a shell we are about to spawn — the single place
    that decides what a model-driven command is allowed to *know*.

    Two regimes, deliberately. **Sandboxed** (``MANTIS_SANDBOX=1`` or
    ``sandbox.enabled``): the allowlist behind
    :func:`mantis_agent.sandbox.child_env`, because confining the filesystem
    while the child still inherits the environment confines the wrong half —
    a process that can read ``os.environ`` and open a socket doesn't need your
    disk to hurt you, and a denylist is only ever as good as the imagination of
    whoever wrote it (``MY_DB_DSN``, ``SSH_AUTH_SOCK``).

    **Unsandboxed** (the default): the historical :func:`_is_secret_env`
    denylist. Someone who never asked for confinement should not discover that
    ``git push``, ``kubectl`` and ``docker`` stopped working — this is their own
    shell on their own machine, and the allowlist is a promise the sandbox
    makes, not a promise ``bash`` makes.
    """
    env: dict[str, str] | None = None
    try:
        from ..sandbox import child_env, load_policy  # noqa: PLC0415

        policy = load_policy()
        if policy.enabled:
            env = child_env(policy, cwd=cwd, extra_pass=_SHELL_PASSTHROUGH)
    except Exception:  # noqa: BLE001 — a broken policy must not break bash
        env = None
    if env is None:
        # Strip credential-bearing vars (API keys, tokens, passwords) before
        # spawning: an untrusted/prompt-injected model must not read the host's
        # secrets out of its own environment (`env`, `curl -d "$(env)"`).
        env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
        if cwd:
            env["PWD"] = str(cwd)
    env.update(_NONINTERACTIVE_ENV)
    return env


@tool(is_read_only=False, is_concurrency_safe=False, timeout_s=120.0)
async def bash(command: str, timeout: int = 120, stdin: str = "",
               run_in_background: bool = False) -> str:
    """Run a shell command and return its combined stdout + stderr.

    Use this for system commands and terminal operations: builds/tests, git,
    package managers, generated-code commands, and project CLIs. Prefer the
    dedicated tools for files and search: read_file (not cat/head/tail),
    edit_file/write_file (not sed/awk/echo heredocs), glob (not find/ls for file
    search), and grep (not grep/rg for content search). Output text directly to
    the user; don't use echo/printf as communication.

    This is also your capability escape hatch: when no tool does what you need,
    install the package and run a script you wrote yourself. Anything the machine
    can do, you can do from here.

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
            Ignored when ``run_in_background=True`` (background commands get an
            empty stdin).
    """

    # Models pass loose values — strings, 0, absurd numbers. Clamp to a sane
    # window so e.g. ``timeout: 0`` doesn't either fire instantly or hang.
    timeout = _coerce_int(timeout, default=120, lo=1, hi=600)
    # stdin is fed to the process, then closed — so a command that reads more
    # than we provided gets EOF and exits rather than blocking forever.
    stdin_bytes = (stdin if isinstance(stdin, str) else str(stdin)).encode("utf-8")

    # Persistent working directory: each foreground command starts where the
    # previous one ended, so `cd sub` then a later `ls` behaves like a real shell
    # (Claude Code parity) instead of resetting to the launch dir every call.
    cwd = _bash_cwd()["cwd"]
    if cwd is None:
        # First command of the run: start in the agent's working directory when
        # it set one, so `ls` sees what `write_file` just wrote. Without this,
        # bash began in the host process's cwd while the file tools resolved
        # against the agent's — the two disagreed and the model got confused by
        # its own output.
        cwd = agent_cwd()
    if cwd is not None and not os.path.isdir(cwd):
        cwd = _bash_cwd()["cwd"] = None  # the tracked dir vanished — fall back

    # What the child may know (secrets scrubbed) and how it must behave (no
    # pagers, no editors, no prompts). One helper for both spawn paths — see
    # `_child_env`; the env is computed after `cwd` so PWD can agree with it.
    env = _child_env(cwd)

    if run_in_background:
        return _start_background(command, env, cwd=cwd)

    # Append a marker that prints the final $PWD so we can carry it to the next
    # call. Runs after the command (preserving its exit code); skipped only if the
    # command exits the shell itself, in which case we simply keep the old cwd.
    wrapped = f"{command}\n__mrc=$?\nprintf '\\n{_CWD_MARKER}%s\\n' \"$PWD\"\nexit $__mrc"
    argv = _sandboxed(["bash", "-lc", wrapped], cwd)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    sink = BASH_OUTPUT_SINK.get()
    spills: list[str | None] = [None, None]
    try:
        if sink is None:
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout)
        else:
            # Opt-in live output: same process, same timeout, same kill —
            # only the read side differs (incremental, bounded, forwarded).
            stdout, stderr = await asyncio.wait_for(
                _pump_streams(proc, stdin_bytes, sink, spills), timeout
            )
    except (TimeoutError, asyncio.TimeoutError):
        await _kill_process_group(proc)
        raise TimeoutError(
            f"command timed out after {timeout}s (is it interactive, waiting "
            f"on input, or a long-running server? use run_in_background=True "
            f"for dev servers/watchers): {command}"
        ) from None

    body, new_cwd = _shell_body(stdout, stderr)
    if new_cwd:
        _bash_cwd()["cwd"] = new_cwd
    # The streaming path's buffers are bounded; if either stream overflowed,
    # rebuild the full output from its raw tee file so the saved copy is full.
    full_path = await asyncio.to_thread(_save_spilled_output, spills, stdout, stderr) \
        if any(spills) else None
    # The exit status goes after the (possibly truncated) body, never inside it.
    status = f"\n[exit code: {proc.returncode}]" if proc.returncode != 0 else ""
    return (_truncate_shell(body, suffix=status, full_output_path=full_path).lstrip()
            or f"(no output, exit code {proc.returncode})")


def _shell_body(stdout: bytes, stderr: bytes) -> tuple[str, str | None]:
    """Captured bytes → (model-facing body, final ``$PWD`` from the marker)."""
    raw_out, new_cwd = _extract_cwd_marker(stdout.decode("utf-8", "replace"))
    out = _strip_terminal_controls(raw_out)
    err = _strip_terminal_controls(stderr.decode("utf-8", "replace"))
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(err if not out else f"\n[stderr]\n{err}")
    return "".join(parts).rstrip(), new_cwd


def _save_spilled_output(spills: list[str | None], stdout: bytes, stderr: bytes) -> str | None:
    """Rebuild the full body from the streaming tee files (falling back to the
    in-memory bytes for a stream that never overflowed), save it, and delete
    the raw tee files. Returns the saved path, or None."""
    raw: list[bytes] = []
    for path, fallback in zip(spills, (stdout, stderr)):
        if not path:
            raw.append(fallback)
            continue
        try:
            with open(path, "rb") as fh:
                raw.append(fh.read())
        except OSError:
            raw.append(fallback)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    body, _ = _shell_body(raw[0], raw[1])
    return _save_full_output(body)


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the child's whole process group, escalate to SIGKILL after 2s,
    and reap it. Shared by the buffered and streaming foreground paths so a
    timeout behaves identically on both."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except (TimeoutError, asyncio.TimeoutError):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        await proc.wait()


# Bounds for the streaming path's in-memory copy of the output. ``communicate``
# buffers unboundedly; here we keep the first ``_STREAM_HEAD_BYTES`` (more than
# ``_truncate_shell`` will ever return) plus a tail big enough for its tail
# share (the failing summary and the trailing ``$PWD`` marker line must
# survive) and count what fell between.
_STREAM_HEAD_BYTES = _MAX_OUTPUT * 4
_STREAM_TAIL_BYTES = _MAX_OUTPUT * 2
_STREAM_READ_SIZE = 4096


class _BoundedBuf:
    """Head + tail byte buffer with a dropped-bytes counter.

    The first time bytes would be dropped, everything seen so far and every
    later chunk is also teed to a private spill file (capped at
    ``_SPILL_MAX_BYTES``), so "full output saved to …" is actually full."""

    __slots__ = ("_head", "_tail", "_dropped", "_spill_fd", "_spill_path",
                 "_spill_written", "_spill_skipped")

    def __init__(self) -> None:
        self._head = bytearray()
        self._tail = bytearray()
        self._dropped = 0
        self._spill_fd: int | None = None
        self._spill_path: str | None = None
        self._spill_written = 0
        self._spill_skipped = 0

    def _tee(self, chunk: bytes) -> None:
        if self._spill_fd is None or not chunk:
            return
        take = min(len(chunk), _SPILL_MAX_BYTES - self._spill_written)
        try:
            if take > 0:
                os.write(self._spill_fd, chunk[:take])
                self._spill_written += take
        except OSError:
            self._close_spill_fd()
            self._drop_spill()
            return
        self._spill_skipped += len(chunk) - take

    def _start_spill(self) -> None:
        made = _new_spill_file("mantis-bash-stream-")
        if made is None:
            return
        self._spill_fd, self._spill_path = made
        self._tee(bytes(self._head))
        self._tee(bytes(self._tail))

    def append(self, chunk: bytes) -> None:
        self._tee(chunk)
        room = _STREAM_HEAD_BYTES - len(self._head)
        if room >= len(chunk):
            self._head += chunk
            return
        if self._spill_fd is None and self._spill_path is None:
            self._start_spill()
            self._tee(chunk)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        self._dropped += len(chunk)
        self._tail += chunk
        if len(self._tail) > _STREAM_TAIL_BYTES:
            del self._tail[: len(self._tail) - _STREAM_TAIL_BYTES]

    def _close_spill_fd(self) -> None:
        if self._spill_fd is not None:
            try:
                os.close(self._spill_fd)
            except OSError:
                pass
            self._spill_fd = None

    def _drop_spill(self) -> None:
        if self._spill_path is not None:
            try:
                os.unlink(self._spill_path)
            except OSError:
                pass
        self._spill_path = ""  # "" = tried and gave up; don't restart

    def finish_spill(self) -> str | None:
        """Close the spill file (marking a size cut) and return its path, or
        None when nothing was dropped / spilling failed. The caller owns it."""
        if self._spill_fd is not None and self._spill_skipped:
            try:
                os.write(self._spill_fd, _spill_cut_marker(self._spill_skipped))
            except OSError:
                pass
        self._close_spill_fd()
        return self._spill_path or None

    def value(self) -> bytes:
        if not self._dropped:
            return bytes(self._head)
        elided = self._dropped - len(self._tail)
        note = f"\n… [{elided} bytes elided while streaming]\n".encode()
        return bytes(self._head) + note + bytes(self._tail)


def _without_marker_lines(text: str) -> str:
    return "".join(
        ln for ln in text.splitlines(keepends=True) if not ln.startswith(_CWD_MARKER)
    )


async def _pump_streams(
    proc: asyncio.subprocess.Process, stdin_bytes: bytes, sink: BashOutputSink,
    spills: list[str | None] | None = None,
) -> tuple[bytes, bytes]:
    """Feed stdin, read stdout/stderr concurrently in chunks, forward each
    decoded chunk to ``sink`` and return the (bounded) captured bytes — the
    streaming counterpart of ``proc.communicate``.

    stdout is forwarded a whole line at a time so the internal ``$PWD``
    marker line the wrapper prints can be withheld; stderr chunks go straight
    through. A sink that raises is logged and ignored — live rendering must
    never change what the model gets back.
    """
    import codecs  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    async def emit(text: str) -> None:
        text = _strip_terminal_controls(text)
        if not text:
            return
        try:
            res = sink(text)
            if inspect.isawaitable(res):
                await res
        except Exception:  # noqa: BLE001 — a UI callback must not break the tool
            _LOG.debug("bash output sink raised", exc_info=True)

    async def feed_stdin() -> None:
        assert proc.stdin is not None
        try:
            if stdin_bytes:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # child exited before reading — same as communicate()
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

    async def pump(stream: asyncio.StreamReader, buf: _BoundedBuf, is_stdout: bool) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        held = ""  # stdout: partial trailing line, withheld until it completes
        while True:
            chunk = await stream.read(_STREAM_READ_SIZE)
            if not chunk:
                break
            buf.append(chunk)
            text = decoder.decode(chunk)
            if not is_stdout:
                await emit(text)
                continue
            held += text
            nl = held.rfind("\n")
            if nl == -1:
                continue
            ready, held = held[: nl + 1], held[nl + 1:]
            await emit(_without_marker_lines(ready))
        held += decoder.decode(b"", final=True)
        if held:
            await emit(_without_marker_lines(held) if is_stdout else held)

    out_buf, err_buf = _BoundedBuf(), _BoundedBuf()
    assert proc.stdout is not None and proc.stderr is not None
    try:
        await asyncio.gather(
            feed_stdin(),
            pump(proc.stdout, out_buf, True),
            pump(proc.stderr, err_buf, False),
        )
        await proc.wait()
    except BaseException:
        # Timeout / cancel: nobody will read the tee files.
        for buf in (out_buf, err_buf):
            path = buf.finish_spill()
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        raise
    # Raw (untruncated) copies of any stream that overflowed the buffers —
    # ``(stdout_path, stderr_path)``, None where the buffer kept everything.
    paths = (out_buf.finish_spill(), err_buf.finish_spill())
    if spills is not None:
        spills[:] = paths
    else:
        for path in paths:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    return out_buf.value(), err_buf.value()


# ---------------------------------------------------------------------------
# Background shells — for long-running commands (dev servers, watchers, builds).
# Detached, stdout+stderr streamed to a temp log; read later with bash_output.
# ---------------------------------------------------------------------------

# Background shells + foreground cwd are per-agent (see TOOL_SCOPE above): a
# subagent's cd must not move the parent's shell, and one agent must not be able
# to read/kill another's background shells. The module-level ``_BG_SHELLS`` /
# ``_BG_COUNTER`` / ``_BASH_CWD`` are the default ("__global__") scope's
# containers (reachable by direct callers/tests); other scopes get their own.
_BG_SHELLS: dict[str, dict[str, Any]] = {}
_BG_COUNTER = [0]
_BASH_CWD: dict[str, str | None] = {"cwd": None}
_BG_SHELLS_BY_SCOPE: dict[str, dict[str, dict[str, Any]]] = {"__global__": _BG_SHELLS}
_BG_COUNTER_BY_SCOPE: dict[str, list[int]] = {"__global__": _BG_COUNTER}
_BASH_CWD_BY_SCOPE: dict[str, dict[str, str | None]] = {"__global__": _BASH_CWD}


def _bg_shells() -> dict[str, dict[str, Any]]:
    return _BG_SHELLS_BY_SCOPE.setdefault(_scope(), {})


def _bg_counter() -> list[int]:
    return _BG_COUNTER_BY_SCOPE.setdefault(_scope(), [0])


def _bash_cwd() -> dict[str, str | None]:
    return _BASH_CWD_BY_SCOPE.setdefault(_scope(), {"cwd": None})


_CWD_MARKER = "__MANTIS_CWD_9f3a__:"


def _extract_cwd_marker(text: str) -> tuple[str, str | None]:
    """Pull the trailing ``$PWD`` marker line out of bash output → (clean, cwd)."""
    cwd: str | None = None
    kept: list[str] = []
    for ln in text.split("\n"):
        if ln.startswith(_CWD_MARKER):
            cwd = ln[len(_CWD_MARKER):].strip() or None
        else:
            kept.append(ln)
    return "\n".join(kept), cwd


def _sandboxed(argv: list[str], cwd: str | None) -> list[str]:
    """Wrap a shell invocation in the OS sandbox when one is configured.

    Off by default, so nothing changes for an interactive user who hasn't asked
    for it. When it IS on and the platform can't provide it, the policy decides
    whether that's a warning or a hard stop — a config that claims confinement
    must never silently run unconfined.
    """
    try:
        from ..sandbox import SandboxUnavailable, wrap_command  # noqa: PLC0415

        return wrap_command(argv, cwd=cwd)
    except SandboxUnavailable:
        raise
    except Exception:  # noqa: BLE001 — a broken policy must not break bash
        return argv


def _start_background(command: str, env: dict[str, str] | None = None, *,
                      cwd: str | None = None) -> str:
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    # `env` is normally the one `bash` already built, but a background shell
    # outlives the call that started it and is the last place that should be
    # trusted to inherit os.environ by accident: with no env passed, build the
    # same scrubbed one rather than falling through to the parent's.
    if env is None:
        env = _child_env(cwd)

    counter = _bg_counter()
    counter[0] += 1
    bid = f"bg_{counter[0]}"
    fd, log_path = tempfile.mkstemp(prefix="mantis-bg-", suffix=".log")
    try:
        proc = subprocess.Popen(  # noqa: S603
            _sandboxed(["bash", "-lc", command], cwd),  # noqa: S607
            stdout=fd, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, cwd=cwd, start_new_session=True,  # detach from our process group
        )
    finally:
        os.close(fd)
    _bg_shells()[bid] = {"proc": proc, "log": log_path, "cmd": command}
    return (
        f"Started in background as {bid} (pid {proc.pid}): {command}\n"
        f"Read its output with bash_output(bash_id=\"{bid}\")."
    )


def _unlink_bg_log(entry: dict[str, Any]) -> None:
    """Best-effort removal of a background shell's temp log file so backgrounded
    commands don't leave ``mantis-bg-*.log`` files behind for the process life."""
    log = entry.get("log")
    if log:
        try:
            os.unlink(log)
        except OSError:
            pass


def _signal_pg(proc: Any, sig: int) -> None:
    """Send ``sig`` to a detached background process's whole group, falling back
    to signalling the process directly if the process-group lookup fails."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill() if sig == signal.SIGKILL else proc.terminate()
        except OSError:
            pass


def terminate_background_shells() -> int:
    """Terminate every still-running background shell (started via
    ``bash(run_in_background=True)``) so they don't outlive the agent/session —
    a dev server or watcher shouldn't keep holding ports after ``mantis`` exits.
    Kills the whole process group (they're detached with ``start_new_session``),
    so forked children die too. Escalates SIGTERM→SIGKILL for processes that
    ignore the term signal, and reaps them so none linger as zombies. Returns
    the count terminated. Idempotent."""
    import signal  # noqa: PLC0415

    n = 0
    killed: list[Any] = []
    # Global cleanup: sweep EVERY scope, not just the current one, so no agent's
    # detached shells survive process exit.
    for shells in _BG_SHELLS_BY_SCOPE.values():
        for entry in list(shells.values()):
            proc = entry.get("proc")
            if proc is not None and proc.poll() is None:  # started and still running
                _signal_pg(proc, signal.SIGTERM)
                killed.append(proc)
                n += 1
            log = entry.get("log")
            if log:
                try:
                    os.unlink(log)
                except OSError:
                    pass
    # Give the terminated groups a moment to exit, then force-kill and reap any
    # that ignored SIGTERM so they can't survive as runaways holding ports/fds.
    deadline = time.monotonic() + 2.0
    while killed and time.monotonic() < deadline:
        killed = [p for p in killed if p.poll() is None]
        if not killed:
            break
        time.sleep(0.05)
    for proc in killed:
        _signal_pg(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
    for shells in _BG_SHELLS_BY_SCOPE.values():
        shells.clear()  # clear in place so each scope's dict identity is kept
    return n


@tool(name="bash_kill", is_read_only=False, is_concurrency_safe=True)
async def bash_kill(bash_id: str) -> str:
    """Terminate a background shell started with ``bash(run_in_background=True)``.

    Use this to stop a dev server, watcher, or other long-running background
    process you started once you're done with it. Kills the whole process group
    (so forked children die too).

    Args:
        bash_id: The id returned when the background command was started.
    """
    import signal  # noqa: PLC0415

    shells = _bg_shells()
    entry = shells.get(bash_id)
    if entry is None:
        running = ", ".join(shells) or "none"
        return f"no background shell {bash_id!r} (running: {running})"
    proc = entry["proc"]
    rc = proc.poll()
    if rc is not None:
        shells.pop(bash_id, None)
        _unlink_bg_log(entry)
        return f"{bash_id} had already exited (code {rc})"
    _signal_pg(proc, signal.SIGTERM)
    # Give it a moment to exit on SIGTERM, then force-kill and reap so a process
    # that traps/ignores SIGTERM can't survive holding its port/fds.
    deadline = time.monotonic() + 1.0
    while proc.poll() is None and time.monotonic() < deadline:
        await anyio.sleep(0.05)
    if proc.poll() is None:
        _signal_pg(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
    shells.pop(bash_id, None)
    _unlink_bg_log(entry)
    return f"terminated background shell {bash_id} ({entry['cmd']})"


@tool(name="monitor", is_read_only=True, is_concurrency_safe=True, timeout_s=630.0)
async def monitor(
    bash_id: str | None = None,
    until_pattern: str | None = None,
    path: str | None = None,
    port: int | None = None,
    timeout_s: float = 120.0,
    poll_s: float = 0.5,
) -> str:
    """Wait efficiently for a condition instead of looping sleep + check calls.
    Blocks (up to ``timeout_s``) until the FIRST of the given conditions fires,
    then reports what happened. Give at least one of:

    * ``bash_id`` + ``until_pattern`` — a background shell's output matches the
      regex (also returns early if the shell exits first).
    * ``bash_id`` alone — the background shell exits; reports its exit code.
    * ``path`` — the file/dir appears (or, if it already exists, changes).
    * ``port`` — localhost:port starts accepting TCP connections.

    Use after ``bash(run_in_background=True)`` to wait for "server started",
    a build to finish, a log line, a file to be produced, or a port to open.

    Args:
        bash_id: Background shell to watch (from bash(run_in_background=True)).
        until_pattern: Regex to wait for in the shell's output (needs bash_id).
        path: File or directory to wait on (appear, or change if it exists).
        port: TCP port on localhost to wait on.
        timeout_s: Give up after this many seconds (1–600, default 120).
        poll_s: Poll interval in seconds (0.1–10, default 0.5).
    """
    timeout_s = max(1.0, min(float(timeout_s or 120.0), 600.0))
    poll_s = max(0.1, min(float(poll_s or 0.5), 10.0))

    entry = None
    if bash_id is not None:
        shells = _bg_shells()
        entry = shells.get(bash_id)
        if entry is None:
            running = ", ".join(shells) or "none"
            return f"no background shell {bash_id!r} (running: {running})"
    if until_pattern and entry is None:
        return "monitor: until_pattern needs a bash_id to watch."
    if entry is None and path is None and port is None:
        return "monitor: give bash_id, path, or port — nothing to watch."

    rx = None
    if until_pattern:
        try:
            rx = re.compile(until_pattern)
        except re.error:
            rx = re.compile(re.escape(until_pattern))  # bad regex → literal

    p = resolve_path(path) if path else None
    existed = p.exists() if p is not None else False
    baseline = (p.stat().st_mtime_ns, p.stat().st_size) if (p is not None and existed and p.is_file()) else None
    # Scan from the log's start on the first monitor of this shell — a line
    # that already arrived counts as matched. Separate from bash_output's
    # read_pos so monitoring never swallows output the model hasn't seen.
    scan_pos = entry.get("monitor_pos", 0) if entry is not None else 0

    def _scan_log() -> str | None:
        nonlocal scan_pos
        try:
            with open(entry["log"], "rb") as fh:
                fh.seek(scan_pos)
                chunk = fh.read()
                scan_pos = fh.tell()
        except OSError:
            return None
        entry["monitor_pos"] = scan_pos
        if not chunk:
            return None
        for line in _strip_terminal_controls(chunk.decode("utf-8", "replace")).splitlines():
            if rx is not None and rx.search(line):
                return line.strip()
        return None

    def _port_open() -> bool:
        import socket  # noqa: PLC0415
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
                return True
        except OSError:
            return False

    start = time.monotonic()
    while True:
        if entry is not None:
            if rx is not None:
                hit = _scan_log()
                if hit is not None:
                    return f"matched after {time.monotonic() - start:.1f}s: {hit}"
            rc = entry["proc"].poll()
            if rc is not None:
                tail = _scan_log()  # flush remaining output through the pattern
                if rx is not None and tail is not None:
                    return f"matched after {time.monotonic() - start:.1f}s: {tail}"
                return (f"{bash_id} exited with code {rc} after "
                        f"{time.monotonic() - start:.1f}s"
                        + (" — pattern never matched" if rx is not None else "")
                        + f". Read remaining output with bash_output(bash_id=\"{bash_id}\").")
        if p is not None:
            if not existed and p.exists():
                return f"{p} appeared after {time.monotonic() - start:.1f}s"
            if baseline is not None and p.exists():
                st = p.stat()
                if (st.st_mtime_ns, st.st_size) != baseline:
                    return f"{p} changed after {time.monotonic() - start:.1f}s"
        if port is not None and _port_open():
            return f"port {port} is accepting connections (after {time.monotonic() - start:.1f}s)"
        if time.monotonic() - start >= timeout_s:
            what = until_pattern or (str(p) if p is not None else f"port {port}" if port else f"{bash_id} exit")
            return (f"timeout: {what!r} not seen within {timeout_s:.0f}s — still waiting? "
                    f"call monitor again, or check bash_output / the process directly.")
        await anyio.sleep(poll_s)


@tool(is_read_only=True)
async def bash_output(bash_id: str) -> str:
    """Read the accumulated output of a background shell started with
    ``bash(run_in_background=True)``, plus whether it's still running or has
    exited (with its code).

    Args:
        bash_id: The id returned when the background command was started.
    """
    shells = _bg_shells()
    entry = shells.get(bash_id)
    if entry is None:
        running = ", ".join(shells) or "none"
        return f"no background shell {bash_id!r} (running: {running})"
    proc = entry["proc"]
    rc = proc.poll()
    status = "running" if rc is None else f"exited with code {rc}"
    # Return only output written SINCE the last read (Claude's BashOutput
    # behavior) — polling a long-running process must not re-dump the whole log
    # into context every call. Track a byte offset per shell.
    pos = entry.get("read_pos", 0)
    try:
        with open(entry["log"], "rb") as fh:
            fh.seek(pos)
            new_bytes = fh.read()
            entry["read_pos"] = fh.tell()
        body = _strip_terminal_controls(new_bytes.decode("utf-8", "replace")).strip()
    except OSError:
        body = ""
    cmd = entry["cmd"]
    if len(cmd) > 200:  # the header is kept verbatim — don't let it eat the cap
        cmd = cmd[:200] + "…"
    header = f"[{bash_id} · {status}] {cmd}"
    if not body:
        return f"{header}\n{'(no new output)' if pos > 0 else '(no output yet)'}"
    return _truncate_shell(body, prefix=f"{header}\n", full_output_path=entry["log"])


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


def _edit_summary(verb: str, path: str, old: str, new: str, note: str = "") -> str:
    """A Claude-Code-style one-liner + diff: ``Updated foo.py · +3 -1`` then the
    unified diff so the UI can show exactly what changed. ``note`` (e.g. which
    tolerant-matching rule fired) is appended to the header in parentheses."""
    adds, removes = _diff_stat(old, new)
    stat = []
    if adds:
        stat.append(f"+{adds}")
    if removes:
        stat.append(f"-{removes}")
    head = f"{verb} {path}" + (f" · {' '.join(stat)}" if stat else "")
    if note:
        head += f" ({note})"
    diff = _unified_diff(old, new, path)
    return f"{head}\n{diff}" if diff else head


def _nb_source(src: Any) -> str:
    return "".join(src) if isinstance(src, list) else str(src or "")


@tool(name="notebook_edit", is_read_only=False)
async def notebook_edit(
    path: str,
    new_source: str = "",
    cell_number: int = 0,
    edit_mode: str = "replace",
    cell_type: str = "code",
) -> str:
    """Edit a Jupyter notebook (``.ipynb``) cell.

    Args:
        path: The notebook file.
        new_source: The new cell source (ignored for delete).
        cell_number: 0-based index of the cell to replace/delete, or the index
            to insert BEFORE.
        edit_mode: ``replace`` (default), ``insert``, or ``delete``.
        cell_type: For insert — ``code`` (default) or ``markdown``.
    """
    import json  # noqa: PLC0415

    p = resolve_path(path)
    if not p.exists():
        raise _missing_file_error(path, p)
    # No write-guard here: notebook_edit reads the whole notebook fresh below,
    # mutates one cell, and writes it back — a read-modify-write like edit_file,
    # so it never blind-clobbers unseen content. The guard is only for
    # write_file (whole-file replace from caller-supplied content).
    try:
        nb = json.loads(await anyio.to_thread.run_sync(lambda: p.read_text("utf-8")))
    except (ValueError, OSError) as e:
        raise ValueError(f"not a readable notebook: {e}") from None
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise ValueError(f"{path} is not a valid .ipynb (no 'cells' array)")

    cells: list = nb["cells"]
    n = _coerce_int(cell_number, default=0, lo=0)
    mode = edit_mode if edit_mode in ("replace", "insert", "delete") else "replace"
    src_lines = (new_source or "").splitlines(keepends=True)

    if mode == "delete":
        if not (0 <= n < len(cells)):
            raise ValueError(f"cell_number {n} out of range (0..{len(cells) - 1})")
        cells.pop(n)
        summary = f"deleted cell {n}"
    elif mode == "insert":
        ct = cell_type if cell_type in ("code", "markdown") else "code"
        new_cell: dict[str, Any] = {"cell_type": ct, "metadata": {}, "source": src_lines}
        if ct == "code":
            new_cell["outputs"] = []
            new_cell["execution_count"] = None
        cells.insert(min(n, len(cells)), new_cell)
        summary = f"inserted {ct} cell at {n}"
    else:  # replace
        if not (0 <= n < len(cells)):
            raise ValueError(f"cell_number {n} out of range (0..{len(cells) - 1})")
        cells[n]["source"] = src_lines
        if cells[n].get("cell_type") == "code":
            cells[n]["outputs"] = []          # source changed → stale outputs cleared
            cells[n]["execution_count"] = None
        summary = f"replaced cell {n}"

    body = json.dumps(nb, indent=1, ensure_ascii=False) + "\n"
    await anyio.to_thread.run_sync(lambda: p.write_text(body, encoding="utf-8"))
    _record_seen(p)  # we just wrote it — subsequent writes/edits are fine
    return f"{summary} in {path} ({len(cells)} cells total)"


def _notebook_output_text(out: dict) -> str:
    """Extract the text of one notebook output cell (stream / result / error)."""
    kind = out.get("output_type")
    if kind == "stream":
        return _nb_source(out.get("text", ""))
    if kind == "error":
        return f"{out.get('ename', 'Error')}: {out.get('evalue', '')}"
    data = out.get("data", {})
    if isinstance(data, dict):
        if data.get("text/plain"):
            return _nb_source(data["text/plain"])
        if data.get("image/png") or data.get("image/jpeg"):
            return "[image output]"
    return ""


def _render_notebook(text: str) -> str | None:
    """Render a ``.ipynb`` into readable cells (code + markdown + text outputs)
    instead of raw JSON. Returns ``None`` if it isn't valid notebook JSON so the
    caller falls back to plain text."""
    import json  # noqa: PLC0415

    try:
        nb = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(nb, dict) or "cells" not in nb:
        return None
    parts: list[str] = []
    for i, cell in enumerate(nb.get("cells", []), 1):
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("cell_type", "code")
        parts.append(f"# ── Cell {i} · {ctype} ──")
        src = _nb_source(cell.get("source", "")).rstrip()
        if src:
            parts.append(src)
        if ctype == "code":
            outs = [t for o in cell.get("outputs", []) if isinstance(o, dict)
                    and (t := _notebook_output_text(o).rstrip())]
            if outs:
                parts.append("# Output:\n" + "\n".join(outs))
    return "\n\n".join(parts) if parts else "(empty notebook)"


_IMAGE_READ_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_IMAGE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB — refuse absurd images


@tool(is_read_only=True)
async def read_file(path: str, offset: int = 1, limit: int = _MAX_READ_LINES) -> Any:
    """Read a file from the local filesystem. Use this instead of ``cat``,
    ``head``, ``tail``, or ``sed``. Text files come back with 1-based line
    numbers (``cat -n`` style): the number+tab prefix is not file content, so
    never include it in edit_file old_string/new_string. For long files, use
    ``offset`` and ``limit`` to read the relevant range. Images (png/jpg/gif/
    webp/bmp) come back as an image the model can see (if the backend model is
    vision-capable). Other binaries are noted, not dumped as mojibake.

    Args:
        path: File to read (absolute or relative to the working directory).
        offset: 1-based line number to start from (default 1). Text only.
        limit: Maximum number of lines to return (default 2000). Text only.
    """

    offset = _coerce_int(offset, default=1, lo=1)
    limit = _coerce_int(limit, default=_MAX_READ_LINES, lo=1, hi=50_000)

    p = resolve_path(path)
    if not p.exists():
        raise _missing_file_error(path, p)
    if p.is_dir():
        raise IsADirectoryError(f"{path} is a directory — use ls instead")

    _record_seen(p)  # the agent has now seen this file — write_file may touch it
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

    if suffix == ".ipynb":
        rendered = _render_notebook(text)
        if rendered is not None:
            return rendered  # readable cells instead of raw JSON
    lines = text.splitlines()
    start = max(1, offset)
    chunk = lines[start - 1 : start - 1 + max(1, limit)]
    if not chunk:
        return f"(file has {len(lines)} lines; offset {offset} is past the end)"
    width = len(str(start + len(chunk) - 1))
    # Cap total returned text on a line boundary: a file of many medium-length
    # lines can otherwise return tens of MB (limit × _MAX_LINE), and under an
    # executor the cap shrinks with the model's context window — a 2000-line
    # default read would be the whole window of an 8k model. The notice names
    # the exact offset to continue from.
    cap = _executor_cap(_MAX_READ_OUTPUT)
    budget = cap - 300  # room for the notice
    rows: list[str] = []
    used = 0
    for i, ln in enumerate(chunk):
        row = f"{str(start + i).rjust(width)}\t{ln[:_MAX_LINE]}"
        if rows and used + len(row) + 1 > budget:
            break
        rows.append(row)
        used += len(row) + 1
    out = "\n".join(rows)
    end = start + len(rows) - 1
    if len(rows) < len(chunk):
        out += (
            f"\n… [output capped at {cap:,} chars — showing lines {start}-{end} of "
            f"{len(lines)}. Continue with read_file(path, offset={end + 1}, "
            f"limit={len(rows)})]"
        )
    elif end < len(lines):
        out += f"\n… [{len(lines) - end} more lines — continue with offset={end + 1}]"
    return out


@tool(is_read_only=False, is_concurrency_safe=False)
async def write_file(path: str, content: str) -> str:
    """Write ``content`` to ``path``, creating parent directories and overwriting
    any existing file. Prefer edit_file/multi_edit for targeted changes to
    existing files; write_file replaces the ENTIRE file and is best for new files
    or full rewrites. Existing non-empty files must have been read this session
    to avoid clobbering unseen user work.

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

    p = resolve_path(path)
    _check_write_guard(p)  # don't blind-overwrite an unseen / externally-changed file
    old, newline, bom = "", "\n", False
    if p.exists() and p.is_file():
        old, newline, bom = await anyio.to_thread.run_sync(lambda: _read_for_edit(p))
    # Keep an existing CRLF / BOM file's conventions when the new content is
    # clean LF (models always emit LF) — don't silently flip every line ending.
    keep_style = "\r" not in content and (newline != "\n" or bom)
    bom = bom and not content.startswith("\ufeff")

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        if keep_style:
            p.write_bytes(_encode_for_write(content, newline, bom))
        else:
            p.write_text(content, "utf-8")

    await anyio.to_thread.run_sync(_write)
    _record_seen(p)  # we just wrote it — subsequent writes/edits are fine
    return _edit_summary("Wrote" if not old else "Updated", str(p), old, content)


@tool(is_read_only=False, is_concurrency_safe=False)
async def edit_file(
    path: str, old_string: str, new_string: str, replace_all: bool = False
) -> str:
    """Replace an exact substring in a file. Use this instead of shell ``sed`` or
    ``awk``. Read the file first, then copy exact current text for ``old_string``
    (without read_file's line-number prefix). ``old_string`` must appear exactly
    once unless ``replace_all`` is true; use the smallest clearly-unique context
    that identifies the target.

    Args:
        path: File to edit.
        old_string: Exact text to find (include enough surrounding context to be
            unique).
        new_string: Text to replace it with.
        replace_all: Replace every occurrence instead of requiring a unique match.
    """

    p = resolve_path(path)
    if not p.exists():
        raise _missing_file_error(path, p)
    if old_string == "":
        raise ValueError(
            "old_string must be non-empty — an empty old_string matches everywhere "
            "and would corrupt the file. Use write_file to replace the whole file."
        )

    # Operate in LF; write back with the file's own line ending + BOM.
    text, newline, bom = await anyio.to_thread.run_sync(lambda: _read_for_edit(p))
    # The fuzzy/closest-block search is CPU work — keep it off the event loop.
    updated, note = await anyio.to_thread.run_sync(
        lambda: _apply_edit(text, old_string, new_string, bool(replace_all), path))
    await anyio.to_thread.run_sync(lambda: p.write_bytes(_encode_for_write(updated, newline, bom)))
    _record_seen(p)
    return _edit_summary("Updated", str(p), text, updated, note)


@tool(is_read_only=False, is_concurrency_safe=False)
async def multi_edit(path: str, edits: list[dict]) -> str:
    """Apply several edits to one file in a single atomic pass. Prefer this when
    making multiple replacements in the same file. Edits run in order, each
    against the result of the previous one; if ANY edit fails to match, NONE are
    written (all-or-nothing), so the file never ends up half-edited. As with
    edit_file, copy exact current text from read_file without line-number
    prefixes.

    Args:
        path: File to edit.
        edits: A list of ``{"old_string": ..., "new_string": ..., "replace_all"?: bool}``
            objects, applied top to bottom.
    """

    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty list of edit objects")

    p = resolve_path(path)
    if not p.exists():
        raise _missing_file_error(path, p)

    # Operate in LF; write back with the file's own line ending + BOM.
    text, newline, bom = await anyio.to_thread.run_sync(lambda: _read_for_edit(p))
    original = text
    applied = 0
    notes: list[str] = []
    for i, e in enumerate(edits):
        if not isinstance(e, dict) or "old_string" not in e or "new_string" not in e:
            raise ValueError(f"edit #{i + 1} must have old_string and new_string")
        old, new = e["old_string"], e["new_string"]
        if old == "":
            raise ValueError(
                f"edit #{i + 1}: old_string must be non-empty — an empty old_string "
                f"matches everywhere and would corrupt the file. Use write_file to "
                f"replace the whole file."
            )
        replace_all = bool(e.get("replace_all", False))
        try:
            text, note = await anyio.to_thread.run_sync(
                lambda: _apply_edit(text, old, new, replace_all, path))  # noqa: B023
        except ValueError as exc:
            raise ValueError(f"edit #{i + 1}: {exc}") from None
        if note:
            notes.append(f"edit #{i + 1} {note}")
        applied += 1

    await anyio.to_thread.run_sync(lambda: p.write_bytes(_encode_for_write(text, newline, bom)))
    _record_seen(p)
    return _edit_summary("Updated", str(p), original, text, "; ".join(notes))


# ---------------------------------------------------------------------------
# Listing / searching
# ---------------------------------------------------------------------------


@tool(is_read_only=True)
async def ls(path: str = ".") -> str:
    """List directory entries (directories first, marked with a trailing ``/``).
    Use this for directory inspection; use read_file to read files and glob to
    find files by pattern across a tree.

    Args:
        path: Directory to list (default: current working directory).
    """

    p = resolve_path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such path: {path}")
    if not p.is_dir():
        return f"{p} (file, {p.stat().st_size} bytes)"

    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    if not entries:
        return f"{p} is empty"
    n_dir = sum(1 for e in entries if e.is_dir())
    n_file = len(entries) - n_dir

    def _row(e: Path) -> str:
        if e.is_dir():
            return f"{e.name}/"
        try:
            return f"{e.name} ({_human_size(e.stat().st_size)})"
        except OSError:
            return e.name

    lines = [_row(e) for e in entries[:_MAX_MATCHES]]
    header = f"{p}  ({n_dir} dir{'s' * (n_dir != 1)}, {n_file} file{'s' * (n_file != 1)})"
    out = header + "\n" + "\n".join(lines)
    if len(entries) > _MAX_MATCHES:
        out += f"\n… [{len(entries) - _MAX_MATCHES} more entries]"
    return out


@tool(is_read_only=True, timeout_s=30.0)
async def glob(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern (e.g. ``**/*.py``), most-recently-modified
    first. Use this instead of shell ``find`` or broad ``ls`` when searching for
    files. Dependency/VCS/build directories are skipped by default unless you
    explicitly target them.

    Args:
        pattern: Glob pattern, relative to ``path``. Use ``**`` to recurse.
        path: Base directory to search from (default: working directory).
    """

    base = resolve_path(path)

    # Skip dependency / VCS / build junk (like ripgrep's gitignore defaults) so a
    # broad ``**/*.py`` doesn't drown real files in .venv/node_modules matches —
    # UNLESS the caller explicitly targets such a dir (then honor the request).
    targeted = any(j in pattern for j in _GLOB_IGNORE) or any(
        part in _GLOB_IGNORE for part in base.parts
    )

    def _glob() -> tuple[list[Path], bool]:
        # Bound the walk: a `glob('**/*', '/')` on a huge tree / network mount
        # would otherwise scan and stat every entry with no time limit (the tool
        # timeout can't interrupt a running thread). Stop early on an entry or
        # wall-clock cap and flag the result as truncated.
        out: list[Path] = []
        scanned = 0
        truncated = False
        deadline = time.monotonic() + _GLOB_DEADLINE_S
        for m in base.glob(pattern):
            scanned += 1
            if scanned > _GLOB_MAX_SCAN or (
                scanned % 1000 == 0 and time.monotonic() > deadline
            ):
                truncated = True
                break
            if not m.is_file():
                continue
            if not targeted:
                try:
                    rel_parts = m.relative_to(base).parts
                except ValueError:
                    rel_parts = m.parts
                if any(part in _GLOB_IGNORE for part in rel_parts):
                    continue
            out.append(m)
            if len(out) >= _GLOB_MAX_COLLECT:
                truncated = True
                break
        return out, truncated

    matches, truncated = await anyio.to_thread.run_sync(_glob)
    if not matches:
        return f"no files matching {pattern!r} under {base}"
    # Stat only the bounded set of collected matches (not the whole tree).
    matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
    shown = matches[:_MAX_MATCHES]
    out = "\n".join(str(m) for m in shown)
    if len(matches) > _MAX_MATCHES:
        out += f"\n… [{len(matches) - _MAX_MATCHES} more matches]"
    if truncated:
        out += "\n… [search stopped early — tree too large; narrow the pattern/path]"
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
    fixed_strings: bool = False,
) -> str:
    """Search file contents for a regex pattern. Use this instead of shell
    ``grep``/``rg`` for codebase search; results are capped and formatted for the
    agent. Set ``fixed_strings=True`` when searching for literal code containing
    regex metacharacters. Prefers ripgrep (``rg``) and falls back to a Python
    walk.

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
        fixed_strings: Treat ``pattern`` as a LITERAL string, not a regex — use
            this when searching for code with regex metacharacters like
            ``config.get("x")`` or ``arr[0]`` so the ``.``/``(``/``[`` match
            literally instead of as regex operators.
    """

    mode = output_mode if output_mode in ("content", "files_with_matches", "count") else "content"
    limit = head_limit if head_limit > 0 else _MAX_MATCHES

    rg = await _have_rg()
    if rg:
        # --max-columns: a minified bundle / lockfile line can be megabytes;
        # rg then prints a "[… omitted]" preview instead of flooding the
        # result (the Python fallback caps each line at ``_MAX_LINE``).
        cmd = ["rg", "--color=never", "--max-columns", str(_RG_MAX_COLUMNS),
               "--max-columns-preview"]
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
        if fixed_strings:
            cmd.append("-F")
        if multiline:
            cmd += ["--multiline", "--multiline-dotall"]
        if glob:
            cmd += ["--glob", glob]
        if file_type:
            cmd += ["--type", file_type]
        # Resolve against the agent's cwd (like glob/read and the Python
        # fallback) — handing rg the raw relative path searched the *host
        # process's* cwd under ``Agent(cwd=...)``. Passing the resolved path
        # (rather than ``cwd=``) also makes rg print the same paths the
        # fallback does, so output looks alike whichever backend ran.
        cmd += ["--", pattern, str(resolve_path(path))]
        # rg matches a --glob containing a slash (``pkg/*.py``, ``!tests/**``)
        # relative to its OWN cwd, so it must run in the agent's cwd too —
        # launched from the host process's cwd those globs silently matched
        # nothing, unlike the Python fallback.
        run_cwd = agent_cwd()
        if run_cwd is not None:
            run_cwd = os.path.expanduser(run_cwd)
            if not os.path.isdir(run_cwd):
                run_cwd = None
        try:
            result = await anyio.run_process(cmd, check=False, input=b"", cwd=run_cwd)
        except (FileNotFoundError, NotADirectoryError):
            # rg vanished after the cached PATH lookup (or the cwd went away
            # mid-call) — the Python walk still answers.
            result = None
        if result is not None:
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
        file_type, limit, multiline, fixed_strings,
    )


def _head(text: str, limit: int) -> str:
    """Keep the first ``limit`` lines, noting how many were dropped."""
    lines = text.split("\n")
    if len(lines) <= limit:
        return text
    dropped = len(lines) - limit
    return "\n".join(lines[:limit]) + f"\n… [{dropped} more lines truncated]"


# Bounds for the rg-less Python fallback. A worker thread running a C-level
# ``re`` call can't be interrupted by the async tool timeout, so cap the input
# fed to the regex (per line / per file) and stop on a wall-clock deadline —
# otherwise a catastrophic-backtracking pattern pegs the thread pool long after
# the tool has reported a timeout.
_PY_GREP_DEADLINE_S = 10.0
_PY_GREP_MAX_FILE_BYTES = 5_000_000

# A single ``re.search`` on a pathological pattern (nested unbounded
# quantifiers, e.g. ``(a+)+``) can back off exponentially and never return —
# and because it's an uninterruptible C call in a worker thread, neither the
# tool timeout nor the per-file/per-line deadline above can cancel it once it
# starts. Since we can't kill it, we refuse it up front. Detects an
# unbounded-quantified group whose body is itself unbounded-quantified —
# the classic ReDoS shape — for both capturing and ``(?:…)`` groups.
_UNBOUNDED_QUANT = r"(?:[*+]|\{\d+,\})"
_REDOS_RE = re.compile(
    rf"\([^()]*{_UNBOUNDED_QUANT}[^()]*\)\??{_UNBOUNDED_QUANT}"
)


def _reject_catastrophic_regex(pattern: str) -> None:
    """Raise ``ValueError`` if ``pattern`` has a nested-unbounded-quantifier
    construct that can trigger catastrophic backtracking. Fail-closed: the
    Python fallback can't cancel an in-flight match, so we never run it."""
    if _REDOS_RE.search(pattern):
        raise ValueError(
            "pattern rejected: nested unbounded quantifiers (e.g. '(a+)+') can "
            "cause catastrophic backtracking that the search fallback cannot "
            "cancel. Rewrite the regex, pass fixed_strings=true for a literal "
            "match, or install ripgrep (rg)."
        )

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


_RG_MAX_COLUMNS = 500


@functools.cache
def _rg_on_path() -> bool:
    return shutil.which("rg") is not None


async def _have_rg() -> bool:
    """Whether ripgrep is installed. A PATH lookup, cached for the process —
    it used to spawn ``rg --version`` on every single grep call."""
    return _rg_on_path()


def _py_grep(
    pattern: str, path: str, glob: str | None, ignore_case: bool,
    mode: str = "content", context_lines: int = 0, file_type: str | None = None,
    limit: int = _MAX_MATCHES, multiline: bool = False, fixed_strings: bool = False,
) -> str:
    import re

    if not fixed_strings:
        # fixed_strings escapes the whole pattern, so it can't backtrack; only
        # screen real regexes.
        _reject_catastrophic_regex(pattern)
    flags = re.IGNORECASE if ignore_case else 0
    if multiline:
        flags |= re.DOTALL
    rx = re.compile(re.escape(pattern) if fixed_strings else pattern, flags)
    base = resolve_path(path)
    exts = _TYPE_EXTS.get(file_type or "", ())
    files: list[Path]
    if base.is_file():
        files = [base]
    else:
        files = [p for p in base.rglob(glob or "*") if p.is_file()]
    if exts:
        files = [f for f in files if f.suffix in exts]

    out: list[str] = []
    deadline = time.monotonic() + _PY_GREP_DEADLINE_S
    for f in files:
        if ".git" + os.sep in str(f):
            continue
        if time.monotonic() > deadline:
            out.append("… [search stopped early — took too long; install ripgrep (rg)]")
            break
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Cap the text handed to the regex so a huge file / long line can't drive
        # unbounded (or exponential) backtracking on a single uninterruptible call.
        if len(text) > _PY_GREP_MAX_FILE_BYTES:
            text = text[:_PY_GREP_MAX_FILE_BYTES]
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
        matched = []
        for n, ln in enumerate(lines):
            if n % 1000 == 0 and time.monotonic() > deadline:
                break
            if rx.search(ln[:_MAX_LINE]):
                matched.append(n)
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

@tool(is_read_only=True, is_concurrency_safe=True)
async def sleep(seconds: float = 5.0) -> str:
    """Wait for a fixed number of seconds, then return.

    Use this ONLY when you must wait for something external to make progress —
    a deploy to roll out, a CI run to advance, a background process (see
    ``bash_output``) to produce more output — before checking again. Don't sleep
    to pad time or in a tight poll loop. This is interruptible and holds no shell,
    so it's safe for longer waits than a ``bash`` ``sleep`` (which the command
    timeout would kill).

    Args:
        seconds: How long to wait (clamped to 0–600).
    """
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        s = 5.0
    s = max(0.0, min(s, 600.0))
    await anyio.sleep(s)
    return f"slept {s:g}s"


CODING_TOOLS: tuple[Tool, ...] = (
    bash,
    bash_output,
    bash_kill,
    monitor,
    read_file,
    write_file,
    edit_file,
    multi_edit,
    notebook_edit,
    ls,
    glob,
    grep,
    sleep,
)

__all__ = [
    "CODING_TOOLS",
    "bash",
    "bash_output",
    "bash_kill",
    "monitor",
    "read_file",
    "write_file",
    "edit_file",
    "multi_edit",
    "notebook_edit",
    "ls",
    "glob",
    "grep",
    "sleep",
]
