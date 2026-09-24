"""The live reply preview for the full-screen terminal.

``run_iter`` only yields *finalized* messages, so without a tap on the raw
stream the full-screen UI shows a spinner until the whole reply (thinking,
text, tool call) is done and then drops it all at once. :class:`LivePreview`
is that tap's buffer: token deltas are appended as-is (no per-token markdown,
no per-token joins) and the pinned preview window renders only the last few
rows. The finished message still prints through the normal markdown path,
which clears this buffer in the same step — the preview is a stand-in, never
part of the transcript.

Stdlib-only on purpose: the renderer hands back ``(kind, line)`` rows and the
caller maps kinds onto its own styles.
"""

from __future__ import annotations

import time

__all__ = ["LivePreview"]

TEXT = "text"
THINKING = "thinking"


class LivePreview:
    """Append-only buffer of streamed text / thinking with a tail renderer.

    Consecutive deltas of the same kind share a *segment*; a kind switch
    (thinking → text) opens a new segment, which always starts on a new row.
    ``due()`` throttles repaints to ``max_hz`` so a fast model can't turn every
    token into a full redraw.
    """

    def __init__(self, max_hz: float = 25.0) -> None:
        self._min_interval = 1.0 / max_hz if max_hz > 0 else 0.0
        self._last_paint = 0.0
        self._segments: list[list] = []  # [kind, [chunk, ...]]
        self._visible = False  # any non-whitespace yet (O(1) truthiness)
        # tail() is asked twice per frame (window height, then content); the
        # cache keys on a version bumped by every feed/reset so both share one
        # computation and an idle repaint does no work at all.
        self._version = 0
        self._tail_key: tuple[int, int, int] | None = None
        self._tail_rows: list[tuple[str, str]] = []

    # -- feeding -------------------------------------------------------------

    def feed(self, kind: str, chunk: str) -> None:
        if not chunk:
            return
        self._version += 1
        if not self._visible and chunk.strip():
            self._visible = True
        if self._segments and self._segments[-1][0] == kind:
            self._segments[-1][1].append(chunk)
        else:
            self._segments.append([kind, [chunk]])

    def feed_text(self, chunk: str) -> None:
        self.feed(TEXT, chunk)

    def feed_thinking(self, chunk: str) -> None:
        self.feed(THINKING, chunk)

    def reset(self) -> None:
        """Drop everything — the message finalized, the turn ended, or the
        stream restarted (a truncation retry's second ``MessageStart``)."""
        self._segments = []
        self._visible = False
        self._version += 1

    def __bool__(self) -> bool:
        return self._visible

    # -- throttle ------------------------------------------------------------

    def due(self, now: float | None = None) -> bool:
        """True at most ``max_hz`` times a second; the caller repaints only
        then (a periodic ticker picks up whatever arrived in between)."""
        now = time.monotonic() if now is None else now
        if now - self._last_paint >= self._min_interval:
            self._last_paint = now
            return True
        return False

    # -- rendering -----------------------------------------------------------

    def tail(self, width: int, max_rows: int) -> list[tuple[str, str]]:
        """The last ``max_rows`` display rows as ``(kind, row)``, hard-wrapped
        at ``width`` so the caller can size its window exactly.

        Walks segments from the end and stops once enough rows are collected,
        and only ever looks at the last ``~2 * max_rows * width`` characters of
        a segment, so the per-repaint cost is bounded by the window size — not
        by the answer's length or by one enormous unbroken line. The result is
        cached until the next ``feed``/``reset``."""
        if max_rows <= 0:
            return []
        width = max(width, 8)
        key = (width, max_rows, self._version)
        if key == self._tail_key:
            return list(self._tail_rows)
        rows: list[tuple[str, str]] = []
        budget = max_rows * width  # the most one line can ever contribute
        for seg in reversed(self._segments):
            kind, chunks = seg
            if len(chunks) > 1:  # compact so the next repaint doesn't re-join
                chunks[:] = ["".join(chunks)]
            text = _tail_slice(chunks[0], 2 * budget, width)
            text = text.expandtabs(4).replace("\r\n", "\n").replace("\r", "\n")
            text = text.strip("\n")
            if not text.strip():
                continue
            seg_rows: list[tuple[str, str]] = []
            lines = text.split("\n")
            # Only the tail of a long segment can ever be shown.
            for line in lines[-max_rows:]:
                line = _tail_slice(line, budget, width)
                line = "".join(ch for ch in line if ch.isprintable())  # no raw escapes
                if not line:
                    seg_rows.append((kind, ""))
                    continue
                for i in range(0, len(line), width):
                    seg_rows.append((kind, line[i:i + width]))
            rows[:0] = seg_rows
            if len(rows) >= max_rows:
                break
        rows = rows[-max_rows:]
        self._tail_key, self._tail_rows = key, rows
        return list(rows)


def _tail_slice(text: str, limit: int, width: int) -> str:
    """The last ~``limit`` characters of ``text``, cut on a wrap boundary of
    the line the cut falls in, so the kept rows wrap exactly as they would
    had the whole text been wrapped (tabs / stripped escapes aside)."""
    cut = len(text) - limit
    if cut <= 0:
        return text
    line_start = text.rfind("\n", 0, cut) + 1
    return text[line_start + (cut - line_start) // width * width:]
