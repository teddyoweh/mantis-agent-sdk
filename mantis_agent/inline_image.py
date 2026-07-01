"""Inline image rendering for terminals that speak the iTerm2 image protocol
(iTerm2 and WezTerm). Lets the ``mantis`` terminal actually *show* an image the
agent read — the counterpart to multimodal ``read_file`` (which lets the model
see it). Terminals without support get a text note instead, so nothing breaks.

Kitty uses a different protocol and is intentionally not claimed here.
"""

from __future__ import annotations

import base64
import os
from typing import Any

_OSC = "\033]"
_BEL = "\a"


def supports_inline_images(env: dict[str, str] | None = None) -> bool:
    """True when the current terminal understands the iTerm2 inline-image escape."""
    e = os.environ if env is None else env
    if e.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm"):
        return True
    return e.get("LC_TERMINAL") == "iTerm2"


def iterm2_image_escape(
    data: bytes,
    *,
    name: str | None = None,
    width: int | str | None = None,
    height: int | str | None = None,
    preserve_aspect: bool = True,
    tmux: bool = False,
) -> str:
    """Build the iTerm2 ``OSC 1337;File=...`` inline-image escape for ``data``.

    ``width``/``height`` accept iTerm2 units: an int (character cells), ``"Npx"``,
    ``"N%"``, or ``"auto"``. When ``tmux`` is set the sequence is wrapped in the
    tmux passthrough envelope (ESC doubled) so it survives a multiplexer.
    """
    b64 = base64.b64encode(data).decode("ascii")
    args = ["inline=1", f"size={len(data)}"]
    if name:
        args.append("name=" + base64.b64encode(name.encode("utf-8")).decode("ascii"))
    if width is not None:
        args.append(f"width={width}")
    if height is not None:
        args.append(f"height={height}")
    if preserve_aspect:
        args.append("preserveAspectRatio=1")
    seq = f"{_OSC}1337;File={';'.join(args)}:{b64}{_BEL}"
    if tmux:
        inner = seq.replace("\033", "\033\033")
        seq = f"\033Ptmux;{inner}\033\\"
    return seq


def image_note(block: Any) -> str:
    """A short human label for an ImageBlock — media type + size."""
    src = getattr(block, "source", None) or {}
    media = src.get("media_type", "image") if isinstance(src, dict) else "image"
    try:
        n = len(base64.b64decode(src.get("data", "")))
        size = f"{n // 1024} KB" if n >= 1024 else f"{n} B"
    except Exception:  # noqa: BLE001
        size = "?"
    return f"[{media}, {size}]"


def image_block_to_inline(
    block: Any, *, env: dict[str, str] | None = None, max_width_cells: int = 40
) -> str | None:
    """Escape sequence to render an ImageBlock inline, or ``None`` when the
    terminal can't show it or the block isn't a base64 image."""
    src = getattr(block, "source", None)
    if not isinstance(src, dict) or src.get("type") != "base64":
        return None
    if not supports_inline_images(env):
        return None
    try:
        data = base64.b64decode(src.get("data", ""))
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    tmux = bool((os.environ if env is None else env).get("TMUX"))
    return iterm2_image_escape(data, width=max_width_cells, tmux=tmux)
