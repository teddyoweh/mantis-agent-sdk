"""@-mentioning a binary/image file notes it instead of dumping decoded garbage."""

from __future__ import annotations

from pathlib import Path

from mantis_agent.tui import _looks_binary, resolve_file_mentions

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 40


def test_image_mention_noted(tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(_PNG)
    res = resolve_file_mentions("look at @pic.png", tmp_path)
    assert len(res) == 1
    body = res[0][1]
    assert "binary/image" in body and "read_file" in body
    assert "\x89" not in body                          # no raw bytes leaked


def test_null_byte_binary_noted(tmp_path: Path) -> None:
    (tmp_path / "data.dat").write_bytes(b"header\x00\x00payload")
    res = resolve_file_mentions("check @data.dat", tmp_path)
    assert "binary/image" in res[0][1]


def test_text_still_inlined(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')")
    res = resolve_file_mentions("explain @app.py", tmp_path)
    assert res[0][1] == "print('hi')"


def test_text_with_unusual_extension_inlined(tmp_path: Path) -> None:
    (tmp_path / "notes.xyz").write_text("plain notes here")
    res = resolve_file_mentions("@notes.xyz", tmp_path)
    assert res[0][1] == "plain notes here"


def test_looks_binary_helper(tmp_path: Path) -> None:
    assert _looks_binary(b"anything", Path("x.png")) is True     # extension
    assert _looks_binary(b"has\x00null", Path("x.txt")) is True  # nul sniff
    assert _looks_binary(b"clean text", Path("x.txt")) is False
