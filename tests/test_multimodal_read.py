"""Multimodal read_file (T2): images come back as ImageBlocks, text unchanged,
and the executor passes rich content through instead of stringifying it."""

from __future__ import annotations

import base64
from pathlib import Path

import anyio

from mantis_agent.builtin_tools.fs import read_file
from mantis_agent.streaming.executor import _as_block_content
from mantis_agent.types import ImageBlock, TextBlock

_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_image_read_returns_image_block(tmp_path: Path) -> None:
    p = tmp_path / "pic.png"
    p.write_bytes(_PNG_1x1)
    out = anyio.run(lambda: read_file.fn(path=str(p)))
    assert isinstance(out, ImageBlock)
    assert out.source["media_type"] == "image/png"
    assert base64.b64decode(out.source["data"]) == _PNG_1x1


def test_jpg_media_type(tmp_path: Path) -> None:
    p = tmp_path / "pic.jpg"
    p.write_bytes(_PNG_1x1)  # content irrelevant; extension drives media type
    out = anyio.run(lambda: read_file.fn(path=str(p)))
    assert isinstance(out, ImageBlock)
    assert out.source["media_type"] == "image/jpeg"


def test_text_read_still_line_numbered(tmp_path: Path) -> None:
    p = tmp_path / "a.py"
    p.write_text("import os\nprint('hi')\n")
    out = anyio.run(lambda: read_file.fn(path=str(p)))
    assert isinstance(out, str)
    assert "1\timport os" in out and "2\tprint('hi')" in out


def test_pdf_note(tmp_path: Path) -> None:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 ...")
    out = anyio.run(lambda: read_file.fn(path=str(p)))
    assert isinstance(out, str) and "PDF" in out


def test_oversized_image_noted(tmp_path: Path, monkeypatch) -> None:
    import mantis_agent.builtin_tools.fs as fs
    monkeypatch.setattr(fs, "_MAX_IMAGE_BYTES", 10)
    p = tmp_path / "big.png"
    p.write_bytes(_PNG_1x1)
    out = anyio.run(lambda: read_file.fn(path=str(p)))
    assert isinstance(out, str) and "too large" in out


def test_executor_passthrough_helper() -> None:
    img = ImageBlock(source={"type": "base64", "media_type": "image/png", "data": "x"})
    assert _as_block_content(img) == [img]
    assert _as_block_content([img, TextBlock(text="cap")]) == [img, TextBlock(text="cap")]
    assert _as_block_content("plain text") is None
    assert _as_block_content(42) is None
    assert _as_block_content([]) is None
