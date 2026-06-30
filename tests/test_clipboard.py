"""Clipboard + file attachments — media-type detection, file→blocks conversion,
and drag-drop path detection. (The live OS-clipboard grab is system-dependent
and not asserted here; we test it stays a safe no-op when tools are absent.)"""

from __future__ import annotations

import base64

import pytest

from mantis_agent.clipboard import (
    _detect_media_type,
    file_to_blocks,
    has_clipboard_image,
    is_image_path,
    looks_like_path,
)
from mantis_agent.types import ImageBlock, TextBlock

# 1x1 red PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_detect_media_type():
    assert _detect_media_type(_PNG) == "image/png"
    assert _detect_media_type(b"\xff\xd8\xff\xe0junk") == "image/jpeg"
    assert _detect_media_type(b"GIF89a...") == "image/gif"
    assert _detect_media_type(b"RIFF1234WEBPxx") == "image/webp"
    assert _detect_media_type(b"RIFF1234AVIxx") is None  # RIFF but not WEBP
    assert _detect_media_type(b"plain text") is None


def test_file_to_blocks_image(tmp_path):
    f = tmp_path / "pic.png"
    f.write_bytes(_PNG)
    blocks = file_to_blocks(f)
    assert len(blocks) == 1 and isinstance(blocks[0], ImageBlock)
    assert blocks[0].source["media_type"] == "image/png"
    assert base64.b64decode(blocks[0].source["data"]) == _PNG


def test_file_to_blocks_text(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello world")
    blocks = file_to_blocks(f)
    assert isinstance(blocks[0], TextBlock)
    assert "hello world" in blocks[0].text
    assert str(f) in blocks[0].text


def test_file_to_blocks_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        file_to_blocks(tmp_path / "nope.txt")


def test_is_image_path():
    assert is_image_path("/a/b.PNG")
    assert is_image_path("photo.jpeg")
    assert not is_image_path("/a/b.txt")
    assert not is_image_path("/a/b")


def test_looks_like_path(tmp_path):
    f = tmp_path / "real file.png"  # spaces, drag-drop style
    f.write_bytes(_PNG)
    escaped = str(f).replace(" ", "\\ ")
    assert looks_like_path(escaped) == str(f)
    assert looks_like_path(f"  '{f}'  ") == str(f)  # quoted + padded
    assert looks_like_path("just some words") is None
    assert looks_like_path(str(tmp_path / "missing.png")) is None
    assert looks_like_path("multi\nline") is None


def test_has_clipboard_image_never_raises():
    # Must degrade to False, not explode, when clipboard tools are missing.
    assert isinstance(has_clipboard_image(), bool)
