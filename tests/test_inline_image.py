"""Inline image rendering — iTerm2/WezTerm escape encoder + terminal detection."""

from __future__ import annotations

import base64

from mantis_agent.inline_image import (
    image_block_to_inline,
    image_note,
    iterm2_image_escape,
    supports_inline_images,
)
from mantis_agent.types import ImageBlock

_DATA = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20


def _blk(data: bytes = _DATA, media: str = "image/png") -> ImageBlock:
    return ImageBlock(source={
        "type": "base64", "media_type": media,
        "data": base64.b64encode(data).decode("ascii"),
    })


def test_escape_shape() -> None:
    esc = iterm2_image_escape(_DATA, width=40)
    assert esc.startswith("\033]1337;File=")
    assert "inline=1" in esc
    assert f"size={len(_DATA)}" in esc
    assert "width=40" in esc
    assert "preserveAspectRatio=1" in esc
    assert esc.endswith("\a")
    # the payload round-trips
    payload = esc.split(":", 1)[1][:-1]
    assert base64.b64decode(payload) == _DATA


def test_tmux_wrapping() -> None:
    esc = iterm2_image_escape(_DATA, tmux=True)
    assert esc.startswith("\033Ptmux;")
    assert esc.endswith("\033\\")
    assert "\033\033]1337" in esc          # ESC doubled inside the envelope


def test_name_is_base64() -> None:
    esc = iterm2_image_escape(_DATA, name="pic.png")
    assert "name=" + base64.b64encode(b"pic.png").decode() in esc


def test_detection() -> None:
    assert supports_inline_images({"TERM_PROGRAM": "iTerm.app"})
    assert supports_inline_images({"TERM_PROGRAM": "WezTerm"})
    assert supports_inline_images({"LC_TERMINAL": "iTerm2"})
    assert not supports_inline_images({"TERM_PROGRAM": "Apple_Terminal"})
    assert not supports_inline_images({})


def test_block_to_inline_gated_on_support() -> None:
    assert image_block_to_inline(_blk(), env={"TERM_PROGRAM": "iTerm.app"}) is not None
    assert image_block_to_inline(_blk(), env={"TERM_PROGRAM": "xterm"}) is None


def test_block_to_inline_rejects_non_image() -> None:
    class NotImg:
        source = {"type": "url", "url": "http://x"}
    assert image_block_to_inline(NotImg(), env={"TERM_PROGRAM": "iTerm.app"}) is None


def test_note() -> None:
    assert image_note(_blk(media="image/jpeg")).startswith("[image/jpeg, ")
    assert "B]" in image_note(_blk()) or "KB]" in image_note(_blk())


def test_tmux_env_triggers_wrap() -> None:
    esc = image_block_to_inline(_blk(), env={"TERM_PROGRAM": "iTerm.app", "TMUX": "/tmp/x"})
    assert esc is not None and esc.startswith("\033Ptmux;")
