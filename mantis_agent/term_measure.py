"""How much room a string takes on screen, and how to cut one safely.

``len(s)`` is the wrong answer to "will this fit in the panel?" in three
separate ways, all of which Mantis hits every frame:

* ``日`` occupies two cells, ``ｆ`` occupies two, and a run of CJK in a file
  path silently overshoots every box border drawn around it.
* ``e`` + U+0301 is two code points and one cell; a family emoji is seven code
  points and one glyph.
* the fullscreen UI hands ANSI-formatted strings to ``ANSI(...)``, so the
  strings being measured are full of ``\\x1b[38;5;113m`` runs that cost nothing
  to print.

The third one is not a layout nicety, it is a corruption bug. Slicing a painted
string with ``s[:n]`` can land in the middle of a sequence and emit ``38;5;113m``
as literal text while leaving the terminal in whatever state the fragment put it
in — every line printed afterwards is wrong, and the user sees garbage that has
nothing to do with the panel that caused it. :func:`truncate` exists so no call
site has to think about that: it cuts on grapheme-cluster and escape-sequence
boundaries only, and closes any styling or hyperlink the cut left open.

Companion to :mod:`mantis_agent.term_caps` (what the terminal can do) and
deliberately built the same way: pure stdlib, no imports from the rest of the
package, so the two very large TUI modules can use it without a cycle. The
width table is derived from :mod:`unicodedata` rather than a vendored copy of
``wcwidth`` — no new dependency, and it tracks whatever Unicode version the
running interpreter ships (3.9 carries Unicode 13, 3.14 carries 16, which in
practice differs only for emoji added in between; those measure as one cell on
the older interpreter, which is also what an older terminal will do with them).

Two judgement calls worth knowing about:

* **East Asian Ambiguous counts as one cell.** ``…``, ``●`` and ``─`` are all
  Ambiguous, and they are Mantis's own glyphs. Terminals render them
  double-width only under a CJK locale with an explicit setting; counting them
  as two would make every separator overshoot for every user.
* **A grapheme cluster costs what its base costs.** A ZWJ emoji sequence is
  two cells, not two per joined part, because that is what the terminals we
  target draw. Terminals genuinely disagree here — which is why §6 of the plan
  says to prefer ASCII markers where the answer matters — but a stable, honest
  approximation beats per-call-site guessing.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Iterator, List, Optional, Tuple

__all__ = [
    "char_width",
    "display_width",
    "fit",
    "grapheme_clusters",
    "pad",
    "truncate",
]

# Same grammar as ``term_caps._ANSI_RE``: CSI (``ESC [ … final``), OSC
# (``ESC ] … BEL`` or ``… ST``), and the single-character Fe escapes. Kept
# local rather than imported so this module stays free of package imports; if
# the two ever drift, escapes leak, so the wiring step should collapse them
# into one definition that ``term_caps`` re-exports.
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"             # CSI: params, intermediates, final byte
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: terminated by BEL or ST
    r"|[@-Z\\-_]"                      # Fe: ESC 7, ESC M, ESC =, …
    r")"
)

# SGR is the only CSI whose state outlives the string, so it is the only one we
# track in order to close it at a cut.
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m\Z")
# OSC 8 hyperlink: ``ESC ] 8 ; params ; URI ST``. An empty URI is the closer.
_OSC8_RE = re.compile(r"\x1b\]8;[^;]*;([^\x07\x1b]*)(?:\x07|\x1b\\)\Z")

_SGR_RESET = "\x1b[0m"
_OSC8_CLOSE = "\x1b]8;;\x1b\\"

_ZWJ = "‍"
_VS15 = "︎"  # text presentation: one cell
_VS16 = "️"  # emoji presentation: two cells
_RI_FIRST, _RI_LAST = 0x1F1E6, 0x1F1FF  # regional indicators (flags)

# Marks and formats that hang off the preceding base character. ``Cf`` is a
# deliberate over-approximation of UAX #29 Extend: it sweeps in ZWJ, the tag
# characters behind subdivision flags (🏴󠁧󠁢󠁳󠁣󠁴󠁿), ZWSP and SOFT HYPHEN. All of
# them are zero-width, so the only consequence is that a zero-width character
# joins the cluster in front of it instead of standing alone.
_EXTEND_CATEGORIES = frozenset({"Mn", "Me", "Mc", "Cf"})
_ZERO_CATEGORIES = frozenset({"Mn", "Me", "Cf"})

# Hangul jamo that compose onto a preceding lead: medial vowels and trailing
# consonants advance the cursor by nothing of their own.
_ZERO_RANGES = (
    (0x1160, 0x11FF),  # Jungseong (V) + Jongseong (T)
    (0xD7B0, 0xD7C6),  # Jungseong extended-B
    (0xD7CB, 0xD7FB),  # Jongseong extended-B
)


# -- per-code-point width ----------------------------------------------------


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    """Cells a single code point advances the cursor by: 0, 1 or 2.

    This is the ``wcwidth`` rule, re-derived from :mod:`unicodedata`. Note it
    answers for a *code point*, not a user-perceived character — combining
    marks and joiners are zero here and the cluster they belong to carries the
    width. Use :func:`display_width` for anything user-facing.
    """
    if len(ch) != 1:
        raise ValueError("char_width() takes a single character")
    cp = ord(ch)
    # C0 and C1 controls, including ESC itself. They print nothing (or move the
    # cursor in ways no layout can model), so they contribute nothing.
    if cp < 0x20 or 0x7F <= cp < 0xA0:
        return 0
    if cp < 0x7F:  # the ASCII fast path, which is almost every character
        return 1
    if unicodedata.category(ch) in _ZERO_CATEGORIES:
        return 0
    for low, high in _ZERO_RANGES:
        if low <= cp <= high:
            return 0
    # W (Wide) and F (Fullwidth) are the two double-width classes. A (Ambiguous)
    # is deliberately narrow — see the module docstring.
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


# -- grapheme clustering (a pragmatic subset of UAX #29) ---------------------


def _is_extend(ch: str) -> bool:
    return unicodedata.category(ch) in _EXTEND_CATEGORIES


def _jamo_class(cp: int) -> Optional[str]:
    """Hangul syllable class, or None. Conjoining jamo form one cluster
    (L* V* T*), and so does a precomposed syllable followed by trailing jamo."""
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    return None


# Which classes may follow which, per GB6–GB8.
_JAMO_FOLLOW = {
    "L": frozenset({"L", "V", "LV", "LVT"}),
    "V": frozenset({"V", "T"}),
    "LV": frozenset({"V", "T"}),
    "T": frozenset({"T"}),
    "LVT": frozenset({"T"}),
}


def _cluster_end(s: str, i: int) -> int:
    """Index just past the grapheme cluster starting at ``i``."""
    n = len(s)
    ch = s[i]
    cp = ord(ch)

    # GB3: CRLF is one cluster. GB4/GB5: every other control stands alone, so a
    # stray ESC or newline can never absorb the text after it.
    if ch == "\r":
        return i + 2 if i + 1 < n and s[i + 1] == "\n" else i + 1
    if cp < 0x20 or 0x7F <= cp < 0xA0:
        return i + 1

    j = i + 1

    # GB12/GB13: regional indicators pair up into a flag, and only ever in
    # twos — 🇺🇸🇬🇧 is two flags, not one four-letter cluster.
    if _RI_FIRST <= cp <= _RI_LAST:
        if j < n and _RI_FIRST <= ord(s[j]) <= _RI_LAST:
            j += 1
    else:
        state = _jamo_class(cp)
        while state is not None and j < n:
            nxt = _jamo_class(ord(s[j]))
            if nxt is None or nxt not in _JAMO_FOLLOW[state]:
                break
            state, j = nxt, j + 1

    # GB9/GB9a: trailing marks and formats. GB11 in simplified form: a ZWJ
    # always pulls in whatever follows it, rather than only Extended_Pictographic
    # — the property table isn't in unicodedata, and the difference only shows
    # up for a ZWJ used between two ordinary letters, where joining is also the
    # rendering the terminal performs.
    while j < n:
        c = s[j]
        if c == _ZWJ:
            j += 1
            if j < n and not (ord(s[j]) < 0x20 or 0x7F <= ord(s[j]) < 0xA0):
                j += 1
            continue
        if _is_extend(c):
            j += 1
            continue
        break
    return j


def _text_clusters(s: str) -> Iterator[str]:
    i, n = 0, len(s)
    while i < n:
        end = _cluster_end(s, i)
        yield s[i:end]
        i = end


def grapheme_clusters(s: str) -> Iterator[str]:
    """The user-perceived characters of ``s``, escape sequences removed.

    ``"é"`` (e + U+0301), a ZWJ family, and a two-code-point flag each come
    back as one item, which is the unit truncation is allowed to cut between.
    """
    for kind, text in _tokens(s):
        if kind == "text":
            for cluster in _text_clusters(text):
                yield cluster


def _cluster_width(cluster: str) -> int:
    """Cells a whole grapheme cluster advances by."""
    if not cluster:
        return 0
    cp = ord(cluster[0])
    if _RI_FIRST <= cp <= _RI_LAST:
        return 2  # a flag, whole or half, draws in two cells
    width = char_width(cluster[0])
    if len(cluster) > 1:
        # A variation selector is an explicit request for one presentation or
        # the other, and terminals honour it: ❤ is one cell, ❤️ is two.
        if _VS16 in cluster:
            return 2
        if _VS15 in cluster and width:
            return 1
    return width


# -- escape-aware tokenizing -------------------------------------------------


def _tokens(s: str) -> Iterator[Tuple[str, str]]:
    """``("esc", sequence)`` and ``("text", run)`` in source order.

    Only *complete* sequences are recognised. A fragment left by a stream cut
    mid-escape stays in a text run, where its ESC measures zero and its
    remaining bytes measure as the literal characters a terminal would
    eventually give up and print — the important part being that a fragment can
    never be mistaken for something safe to keep.
    """
    if not s:
        return
    if "\x1b" not in s:
        yield ("text", s)
        return
    pos = 0
    for match in _ANSI_RE.finditer(s):
        if match.start() > pos:
            yield ("text", s[pos:match.start()])
        yield ("esc", match.group())
        pos = match.end()
    if pos < len(s):
        yield ("text", s[pos:])


def display_width(s: str) -> int:
    """Number of terminal cells ``s`` occupies when printed.

    Zero for embedded escapes and combining marks, two for East Asian Wide and
    Fullwidth characters and for emoji-presentation glyphs. Assumes ``s`` is a
    single line: ``\\n`` and ``\\t`` measure zero because their effect on the
    cursor is not a width, so expand tabs before measuring.
    """
    if not s:
        return 0
    # Overwhelmingly the common case, and worth not walking code point by code
    # point: printable ASCII has no escapes, no marks and no wide characters.
    if s.isascii() and s.isprintable():
        return len(s)
    total = 0
    for kind, text in _tokens(s):
        if kind == "text":
            for cluster in _text_clusters(text):
                total += _cluster_width(cluster)
    return total


def _closers(kept_escapes: List[str]) -> str:
    """The sequences needed to undo state the kept prefix left open.

    Cutting inside ``\\x1b[31m…\\x1b[0m`` keeps the opener and drops the reset,
    so every subsequent line the terminal prints comes out red. Same for an
    OSC 8 hyperlink, where the leak turns unrelated later output into part of
    the link.
    """
    style_open = False
    link_open = False
    for seq in kept_escapes:
        sgr = _SGR_RE.match(seq)
        if sgr is not None:
            params = sgr.group(1)
            style_open = params not in ("", "0")
            continue
        osc8 = _OSC8_RE.match(seq)
        if osc8 is not None:
            link_open = bool(osc8.group(1))
    return (_SGR_RESET if style_open else "") + (_OSC8_CLOSE if link_open else "")


def truncate(s: str, width: int, ellipsis: str = "…") -> str:
    """``s`` shortened to at most ``width`` cells, cut only where it is safe.

    Never splits a grapheme cluster (so an accent keeps its base and a family
    emoji is kept or dropped whole) and never splits an escape sequence (so no
    fragment reaches the terminal). Escapes belonging to text that was dropped
    are dropped with it, and any styling or hyperlink the cut left open is
    closed after the ellipsis.

    Returns ``s`` itself when it already fits, so the overwhelmingly common
    call is free and byte-for-byte unchanged. When ``width`` is too small to
    hold even the ellipsis, the ellipsis is omitted rather than overflowing.
    """
    if width <= 0:
        return ""
    if display_width(s) <= width:
        return s

    ell_width = display_width(ellipsis)
    if ell_width > width:
        ellipsis, ell_width = "", 0
    budget = width - ell_width

    kept: List[str] = []
    kept_escapes: List[str] = []
    used = 0
    for kind, text in _tokens(s):
        if used >= budget:
            # Budget spent. Everything from here belongs to the dropped tail —
            # including its escapes, which would otherwise paint the ellipsis
            # in a colour meant for text nobody will see.
            break
        if kind == "esc":
            kept.append(text)
            kept_escapes.append(text)
            continue
        for cluster in _text_clusters(text):
            cluster_width = _cluster_width(cluster)
            if used >= budget or used + cluster_width > budget:
                break
            kept.append(cluster)
            used += cluster_width
        else:
            continue
        break

    return "".join(kept) + ellipsis + _closers(kept_escapes)


def pad(s: str, width: int, align: str = "left", fill: str = " ") -> str:
    """``s`` padded with ``fill`` to ``width`` cells of *display* width.

    Longer input is returned unchanged — padding does not truncate, because a
    caller that wants both should say so with :func:`fit` and get the escape
    handling that comes with it.
    """
    if display_width(fill) != 1:
        raise ValueError("pad() fill must be exactly one cell wide")
    gap = width - display_width(s)
    if gap <= 0:
        return s
    if align == "left":
        return s + fill * gap
    if align == "right":
        return fill * gap + s
    if align == "center":
        # An odd cell goes to the right, matching str.center.
        left = gap // 2
        return fill * left + s + fill * (gap - left)
    raise ValueError("pad() align must be 'left', 'right' or 'center'")


def fit(s: str, width: int, ellipsis: str = "…", align: str = "left",
        fill: str = " ") -> str:
    """``s`` forced to exactly ``width`` cells — truncated, then padded.

    The column primitive: every cell of a table row built with this lines up,
    whatever mix of CJK, emoji and colour the row contains.
    """
    return pad(truncate(s, width, ellipsis), width, align=align, fill=fill)
