"""The attachment foundation: model, detection, validation, aggregate budget.

Everything under test here is pure — no clipboard, no terminal, no network — so
each rule in the plan's §5/§7/§8 is asserted directly rather than through a UI.
The suite is organized the way the pipeline runs: detect, then validate (paths,
file types, secrets), then the per-turn budget, then blocks.
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

from mantis_agent.input import (
    Attachment,
    AttachmentBudget,
    AttachmentBudgetExceededError,
    AttachmentDeniedError,
    AttachmentPathError,
    AttachmentTooLargeError,
    AttachmentTypeError,
    AttachmentUnsupportedError,
    AttachPolicy,
    TurnBudget,
    attach_bytes,
    attach_path,
    attach_text,
    detect_media_type,
    estimate_image_tokens,
    estimate_text_tokens,
    image_dimensions,
    scan_secrets,
    to_blocks,
)
from mantis_agent.input.validate import check_file_type, check_path, protected_label
from mantis_agent.types import ImageBlock, TextBlock

# ---------------------------------------------------------------------------
# Byte-level fixtures — real headers, built by hand so dimension parsing has
# something honest to read. CRCs are not checked by the detector (nothing here
# decodes an image), so they are left as zeros.
# ---------------------------------------------------------------------------


def _png(width: int = 4, height: int = 3) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def _jpeg(width: int = 8, height: int = 6) -> bytes:
    return (
        b"\xff\xd8\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )


def _gif(width: int = 12, height: int = 9) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _bmp(width: int = 5, height: int = 7) -> bytes:
    return (
        b"BM"
        + struct.pack("<I", 70)
        + b"\x00\x00\x00\x00"
        + struct.pack("<I", 54)
        + struct.pack("<I", 40)
        + struct.pack("<ii", width, height)
        + b"\x01\x00\x18\x00"
    )


def _webp_vp8(width: int = 20, height: int = 10) -> bytes:
    body = (
        b"VP8 "
        + struct.pack("<I", 20)
        + b"\x00\x00\x00"
        + b"\x9d\x01\x2a"
        + struct.pack("<HH", width, height)
        + b"\x00" * 8
    )
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def _webp_vp8x(width: int = 300, height: int = 200) -> bytes:
    body = (
        b"VP8X"
        + struct.pack("<I", 10)
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


# ---------------------------------------------------------------------------
# detect — magic bytes, never the extension
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_png(), "image/png"),
        (_jpeg(), "image/jpeg"),
        (_gif(), "image/gif"),
        (b"GIF87a" + b"\x00" * 8, "image/gif"),
        (_bmp(), "image/bmp"),
        (_webp_vp8(), "image/webp"),
        (_webp_vp8x(), "image/webp"),
        (_MP4, "video/mp4"),
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "application/pdf"),
        (b"PK\x03\x04" + b"\x00" * 16, "application/zip"),
        (b"\x1f\x8b\x08\x00" + b"\x00" * 8, "application/gzip"),
        (b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8, "application/x-executable"),
        (b"OggS\x00\x02" + b"\x00" * 16, "audio/ogg"),
        (b"SQLite format 3\x00" + b"\x00" * 16, "application/vnd.sqlite3"),
    ],
)
def test_detect_media_type_from_magic(data: bytes, expected: str) -> None:
    assert detect_media_type(data) == expected


def test_riff_is_not_blindly_webp() -> None:
    # "RIFF" alone covers WAV and AVI too; the container tag at byte 8 decides.
    wav = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt "
    avi = b"RIFF" + struct.pack("<I", 36) + b"AVI LIST"
    assert detect_media_type(wav) == "audio/wav"
    assert detect_media_type(avi) == "video/x-msvideo"


def test_text_and_binary_sniffing() -> None:
    assert detect_media_type(b"def f():\n    return 1\n") == "text/plain"
    assert detect_media_type("héllo wörld\n".encode("utf-8")) == "text/plain"
    assert detect_media_type(b"\xef\xbb\xbfwith bom") == "text/plain"
    # A NUL byte is the classic "this is not text" tell.
    assert detect_media_type(b"abc\x00def") == "application/octet-stream"
    assert detect_media_type(b"") == "application/octet-stream"


def test_mismatched_extension_is_caught_by_magic(tmp_path) -> None:
    """A .png that is really a video is detected as a video and attached by
    reference — its bytes never enter context."""

    p = tmp_path / "screenshot.png"
    p.write_bytes(_MP4)
    att = attach_path(p, policy=AttachPolicy(root=str(tmp_path)))
    assert att.media_type == "video/mp4"
    assert att.kind == "file"          # not "image" — the extension lied
    assert att.content == b""          # by reference
    assert att.payload_bytes == 0
    assert any("video/mp4" in w and ".png" in w for w in att.warnings)


def test_image_dimensions_are_parsed_per_format() -> None:
    assert image_dimensions(_png(640, 480)) == (640, 480)
    assert image_dimensions(_jpeg(800, 600)) == (800, 600)
    assert image_dimensions(_gif(120, 90)) == (120, 90)
    assert image_dimensions(_bmp(64, 48)) == (64, 48)
    assert image_dimensions(_webp_vp8(320, 240)) == (320, 240)
    assert image_dimensions(_webp_vp8x(1920, 1080)) == (1920, 1080)
    assert image_dimensions(_MP4) is None


# ---------------------------------------------------------------------------
# validate — path resolution before root checks
# ---------------------------------------------------------------------------


def test_inside_workspace_needs_no_confirmation(tmp_path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    verdict = check_path("notes.txt", root=str(tmp_path))
    assert verdict.inside_root is True
    assert verdict.needs_confirmation is False
    assert verdict.resolved == os.path.realpath(str(f))


def test_traversal_escape_is_caught_after_resolution(tmp_path) -> None:
    ws = tmp_path / "ws" / "docs"
    ws.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    verdict = check_path("docs/../../secret.txt", root=str(tmp_path / "ws"))
    assert verdict.inside_root is False
    assert verdict.needs_confirmation is True
    assert verdict.resolved == os.path.realpath(str(outside))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_symlink_escape_is_caught_after_resolution(tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = tmp_path / "outside" / "credentials"
    secret.parent.mkdir()
    secret.write_text("aws stuff")
    link = ws / "innocent.txt"
    link.symlink_to(secret)

    verdict = check_path(str(link), root=str(ws))
    assert verdict.inside_root is False
    assert verdict.resolved == os.path.realpath(str(secret))

    # And the pipeline refuses it outright when nobody can be asked.
    with pytest.raises(AttachmentDeniedError) as exc:
        attach_path(link, policy=AttachPolicy(root=str(ws)))
    assert os.path.realpath(str(secret)) in str(exc.value)


def test_outside_root_deny_policy_raises(tmp_path) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(AttachmentPathError):
        check_path(str(outside), root=str(ws), outside_root="deny")


def test_outside_root_allow_policy_attaches(tmp_path) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("plain text file")
    ws = tmp_path / "ws"
    ws.mkdir()
    att = attach_path(outside, policy=AttachPolicy(root=str(ws), outside_root="allow"))
    assert att.kind == "text"


def test_protected_paths_require_confirmation(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("PORT=8080\n")
    policy = AttachPolicy(root=str(tmp_path))

    verdict = check_path(str(env), root=str(tmp_path))
    assert verdict.protected is not None
    assert verdict.needs_confirmation is True   # even though it is inside the root

    with pytest.raises(AttachmentDeniedError):
        attach_path(env, policy=policy)

    asked: list[str] = []

    def _confirm(c) -> bool:
        asked.append(c.kind)
        return True

    att = attach_path(env, policy=policy, confirm=_confirm)
    assert "protected-path" in asked
    assert any("protected" in w for w in att.warnings)


def test_ssh_key_is_a_protected_path() -> None:
    home = os.path.expanduser("~")
    assert protected_label(os.path.join(home, ".ssh", "id_rsa")) is not None
    assert protected_label(os.path.join(home, ".aws", "credentials")) is not None
    assert protected_label(os.path.join(home, ".mantis", "settings.json")) is not None
    assert protected_label("/tmp/project/README.md") is None
    # An example env file is a template, not a credential.
    assert protected_label("/tmp/project/.env") is not None
    assert protected_label("/tmp/project/.env.production") is not None
    assert protected_label("/tmp/project/.env.example") is None
    # Case folding is explicit, so a case-insensitive filesystem cannot be used
    # to spell a protected name past the check.
    assert protected_label("/tmp/project/.ENV") is not None
    assert protected_label(os.path.join(home, ".ssh", "ID_RSA")) is not None
    assert protected_label("/tmp/project/server.PEM") is not None


def test_traversal_to_ssh_key_resolves_and_flags(tmp_path) -> None:
    ws = tmp_path / "ws" / "docs"
    ws.mkdir(parents=True)
    rel = os.path.relpath(os.path.expanduser("~/.ssh/id_rsa"), str(ws))
    verdict = check_path(rel, root=str(ws))
    assert verdict.protected is not None
    assert verdict.needs_confirmation is True


# ---------------------------------------------------------------------------
# validate — file types
# ---------------------------------------------------------------------------


def test_directory_as_file_is_rejected(tmp_path) -> None:
    with pytest.raises(AttachmentTypeError) as exc:
        check_file_type(str(tmp_path))
    assert "directory" in str(exc.value)
    with pytest.raises(AttachmentTypeError):
        attach_path(tmp_path, policy=AttachPolicy(root=str(tmp_path)))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_fifo_is_rejected_without_opening_it(tmp_path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(str(fifo))
    # If this ever opened the FIFO the test would hang forever, which is the
    # point: rejection happens on stat, before any read.
    with pytest.raises(AttachmentTypeError) as exc:
        attach_path(fifo, policy=AttachPolicy(root=str(tmp_path)))
    assert "FIFO" in str(exc.value)


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs /dev/null")
def test_device_is_rejected() -> None:
    with pytest.raises(AttachmentTypeError) as exc:
        check_file_type("/dev/null")
    assert "device" in str(exc.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_open_regular_rechecks_through_the_descriptor(tmp_path) -> None:
    """The stat-then-open window: whatever the name became, the descriptor is
    what gets checked, and a FIFO cannot make the open hang."""

    from mantis_agent.input.validate import open_regular

    good = tmp_path / "ok.txt"
    good.write_text("fine")
    with open_regular(str(good)) as fh:
        assert fh.read() == b"fine"

    fifo = tmp_path / "pipe2"
    os.mkfifo(str(fifo))
    with pytest.raises((AttachmentTypeError, AttachmentPathError)):
        open_regular(str(fifo))


def test_missing_file_is_a_path_error(tmp_path) -> None:
    with pytest.raises(AttachmentPathError):
        attach_path(tmp_path / "nope.txt", policy=AttachPolicy(root=str(tmp_path)))


# ---------------------------------------------------------------------------
# validate — secret heuristics
# ---------------------------------------------------------------------------


def test_secret_scanning_finds_real_shapes() -> None:
    assert scan_secrets("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    assert scan_secrets("aws_access_key_id = AKIAIOSFODNN7EXAMPLE")
    assert scan_secrets('ANTHROPIC_API_KEY="sk-ant-api03-Zx9Kq2mVbN4pLr7TsWc1"')
    assert scan_secrets("export GITHUB_TOKEN=ghp_" + "a1B2" * 9)


def test_secret_scanning_leaves_ordinary_config_alone() -> None:
    assert scan_secrets("PORT=8080\nDEBUG=true\n") == ()
    assert scan_secrets("API_KEY=\nSECRET_TOKEN=your-key-here\n") == ()
    assert scan_secrets('password = os.environ["PW"]') == ()
    assert scan_secrets("# set your api key in the dashboard") == ()


def test_secret_findings_ride_along_as_warnings(tmp_path) -> None:
    f = tmp_path / "config.ini"
    f.write_text("aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n")
    att = attach_path(f, policy=AttachPolicy(root=str(tmp_path)))
    assert any("secret" in w.lower() or "key" in w.lower() for w in att.warnings)
    assert att.kind == "text"   # a warning, not a block


# ---------------------------------------------------------------------------
# per-item caps
# ---------------------------------------------------------------------------


def test_oversized_image_is_refused_with_a_next_step(tmp_path) -> None:
    p = tmp_path / "big.png"
    p.write_bytes(_png(64, 64) + b"\x00" * 4096)
    policy = AttachPolicy(root=str(tmp_path), budget=AttachmentBudget(max_image_bytes=256))
    with pytest.raises(AttachmentTooLargeError) as exc:
        attach_path(p, policy=policy)
    assert "256" in str(exc.value) or "B" in str(exc.value)


def test_oversized_text_truncates_and_reports_omitted_bytes(tmp_path) -> None:
    p = tmp_path / "crash.log"
    p.write_text("x" * 5000)
    policy = AttachPolicy(root=str(tmp_path), budget=AttachmentBudget(max_text_bytes=1000))
    att = attach_path(p, policy=policy)
    assert att.truncated is True
    assert att.omitted_bytes == 4000
    assert att.size_bytes == 5000
    assert len(att.content) == 1000
    assert any("omitted" in w for w in att.warnings)


def test_truncation_never_splits_a_utf8_character(tmp_path) -> None:
    p = tmp_path / "unicode.txt"
    p.write_bytes("é".encode("utf-8") * 100)         # 200 bytes, 2 per char
    policy = AttachPolicy(root=str(tmp_path), budget=AttachmentBudget(max_text_bytes=101))
    att = attach_path(p, policy=policy)
    assert att.truncated is True
    assert att.content == "é" * 50                    # the split byte was dropped


def test_binary_is_attached_by_reference(tmp_path) -> None:
    p = tmp_path / "bundle.zip"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 500)
    att = attach_path(p, policy=AttachPolicy(root=str(tmp_path)))
    assert att.kind == "file"
    assert att.content == b""
    assert att.size_bytes == 504
    assert "reference" in att.display


# ---------------------------------------------------------------------------
# token estimation
# ---------------------------------------------------------------------------


def test_text_token_estimate_is_the_character_heuristic() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("a" * 4000) == 1000


def test_image_token_estimate_uses_dimensions() -> None:
    # The published formula: (width * height) / 750.
    assert estimate_image_tokens(_png(750, 100)) == 100
    # Oversized images are downscaled before pricing, so the estimate saturates.
    huge = estimate_image_tokens(_png(8000, 6000))
    assert huge <= 1600
    assert huge >= 1400
    # Unknown dimensions fall back to the conservative full-size number.
    assert estimate_image_tokens(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4) == 1600


def test_attachment_carries_an_estimate(tmp_path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("y" * 400)
    att = attach_path(p, policy=AttachPolicy(root=str(tmp_path)))
    assert att.est_tokens == 100


# ---------------------------------------------------------------------------
# §8 — the per-turn aggregate budget
# ---------------------------------------------------------------------------


def _img(n: int = 1, size: int = 1000) -> Attachment:
    return attach_bytes(_png(30, 25) + b"\x00" * size, source="clipboard", name=f"s{n}.png")


def _txt(n: int = 1, chars: int = 100) -> Attachment:
    return attach_text("z" * chars, source="stdin", name=f"t{n}.txt")


def test_attachment_count_cap() -> None:
    turn = TurnBudget(AttachmentBudget(max_attachments=2))
    turn.add(_txt(1))
    turn.add(_txt(2))
    admission = turn.admit(_txt(3))
    assert admission.ok is False
    assert [v.limit for v in admission.violations] == ["attachments"]
    assert admission.choices                       # choices, not a dead end
    with pytest.raises(AttachmentBudgetExceededError) as exc:
        turn.add(_txt(3))
    assert exc.value.admission.choices
    assert turn.count == 2                         # the failed add did not land


def test_total_bytes_cap() -> None:
    turn = TurnBudget(AttachmentBudget(max_total_bytes=2500, max_images=99))
    turn.add(_img(1, size=1000))
    assert turn.admit(_img(2, size=1000)).ok is True
    turn.add(_img(2, size=1000))
    over = turn.admit(_img(3, size=1000))
    assert over.ok is False
    assert [v.limit for v in over.violations] == ["total_bytes"]


def test_total_tokens_cap() -> None:
    turn = TurnBudget(AttachmentBudget(max_total_tokens=50))
    turn.add(_txt(1, chars=160))                   # ~40 tokens
    over = turn.admit(_txt(2, chars=160))
    assert over.ok is False
    assert [v.limit for v in over.violations] == ["total_tokens"]
    assert any(c.action == "remove" for c in over.choices)


def test_image_count_cap() -> None:
    turn = TurnBudget(AttachmentBudget(max_images=2, max_total_bytes=10**9))
    turn.add(_img(1))
    turn.add(_img(2))
    over = turn.admit(_img(3))
    assert over.ok is False
    assert [v.limit for v in over.violations] == ["images"]
    # Text still fits — only the image dimension is exhausted.
    assert turn.admit(_txt(9)).ok is True


def test_five_ten_megabyte_images_do_not_fit_in_one_turn() -> None:
    """The gap §1 names: individual caps pass, the aggregate does not."""

    turn = TurnBudget()
    big = attach_bytes(_png(100, 100) + b"\x00" * (10 * 1024 * 1024 - 40),
                       source="drop", name="big.png")
    assert big.payload_bytes <= AttachmentBudget().max_image_bytes
    turn.add(big)
    turn.add(attach_bytes(_png(101, 100) + b"\x00" * (10 * 1024 * 1024 - 40),
                          source="drop", name="big2.png"))
    third = attach_bytes(_png(102, 100) + b"\x00" * (10 * 1024 * 1024 - 40),
                         source="drop", name="big3.png")
    assert turn.admit(third).ok is False


def test_removing_an_attachment_frees_its_budget() -> None:
    turn = TurnBudget(AttachmentBudget(max_attachments=1))
    a = _txt(1)
    turn.add(a)
    assert turn.admit(_txt(2)).ok is False
    assert turn.remove(a.id) is True
    assert turn.admit(_txt(2)).ok is True
    assert turn.total_tokens == 0
    turn.add(_txt(2))
    turn.clear()
    assert turn.count == 0 and turn.total_bytes == 0


def test_adding_the_same_attachment_twice_counts_once() -> None:
    turn = TurnBudget(AttachmentBudget(max_attachments=1))
    a = _txt(1)
    turn.add(a)
    turn.add(a)                                   # same content, same id
    assert turn.count == 1


def test_image_on_a_text_only_model_fails_early_naming_the_model() -> None:
    turn = TurnBudget(model="qwen2.5-coder-32b", accepts_images=False)
    with pytest.raises(AttachmentUnsupportedError) as exc:
        turn.add(_img(1))
    assert "qwen2.5-coder-32b" in str(exc.value)
    assert turn.admit(_txt(1)).ok is True         # text is unaffected


def test_pipeline_admits_into_the_turn_budget(tmp_path) -> None:
    p = tmp_path / "note.txt"
    p.write_text("hello " * 100)
    turn = TurnBudget(AttachmentBudget(max_attachments=1))
    att = attach_path(p, policy=AttachPolicy(root=str(tmp_path)), turn=turn)
    assert turn.count == 1 and turn.total_tokens == att.est_tokens
    q = tmp_path / "note2.txt"
    q.write_text("more")
    with pytest.raises(AttachmentBudgetExceededError):
        attach_path(q, policy=AttachPolicy(root=str(tmp_path)), turn=turn)
    assert turn.count == 1


def test_budget_summary_reads_like_the_plan() -> None:
    turn = TurnBudget(AttachmentBudget(max_total_tokens=30000))
    turn.add(_txt(1, chars=40000))
    assert "of 30k budget" in turn.summary()


def test_budget_from_settings_reads_the_documented_json() -> None:
    b = AttachmentBudget.from_settings({
        "maxAttachments": 3,
        "maxTotalBytes": 1024,
        "maxTotalTokens": 500,
        "maxImages": 1,
        "maxImageBytes": 2048,
        "maxTextBytes": 128,
        "unknownKey": "ignored",
    })
    assert (b.max_attachments, b.max_total_bytes, b.max_total_tokens) == (3, 1024, 500)
    assert (b.max_images, b.max_image_bytes, b.max_text_bytes) == (1, 2048, 128)
    # snake_case, junk values and negatives all degrade to something sane.
    assert AttachmentBudget.from_settings({"max_images": 2}).max_images == 2
    assert AttachmentBudget.from_settings({"maxImages": -4}).max_images == 0
    assert AttachmentBudget.from_settings({"maxImages": "lots"}).max_images == 5
    assert AttachmentBudget.from_settings(None) == AttachmentBudget()


# ---------------------------------------------------------------------------
# model + blocks
# ---------------------------------------------------------------------------


def test_attachment_is_frozen_and_identifies_by_content() -> None:
    a = attach_text("same", source="stdin", name="a.txt")
    b = attach_text("same", source="stdin", name="a.txt")
    assert a.id == b.id
    assert a.id != attach_text("other", source="stdin", name="a.txt").id
    with pytest.raises(Exception):
        a.kind = "image"        # type: ignore[misc]


def test_file_contents_are_untrusted_and_typed_text_is_not(tmp_path) -> None:
    p = tmp_path / "readme.md"
    p.write_text("# hi")
    from_disk = attach_path(p, policy=AttachPolicy(root=str(tmp_path)))
    assert from_disk.trusted is False
    assert attach_text("what I typed", source="voice").trusted is True


def test_to_blocks_produces_the_existing_block_shapes(tmp_path) -> None:
    import base64

    data = _png(4, 3)
    img = attach_bytes(data, source="clipboard", name="shot.png")
    blocks = to_blocks(img)
    assert len(blocks) == 1 and isinstance(blocks[0], ImageBlock)
    assert blocks[0].source["media_type"] == "image/png"
    assert base64.b64decode(blocks[0].source["data"]) == data

    p = tmp_path / "x.py"
    p.write_text("print(1)")
    text_blocks = to_blocks(attach_path(p, policy=AttachPolicy(root=str(tmp_path))))
    assert isinstance(text_blocks[0], TextBlock)
    body = text_blocks[0].text
    assert "print(1)" in body
    assert "untrusted" in body.lower()          # provenance travels with content
    assert str(p) in body

    # A transcript is the user speaking: no untrusted framing, no fence.
    spoken = to_blocks(attach_text("fix the crash", source="voice", kind="transcript"))
    assert spoken[0].text == "fix the crash"


def test_display_is_human_readable(tmp_path) -> None:
    p = tmp_path / "screenshot.png"
    p.write_bytes(_png(4, 3) + b"\x00" * 421_000)
    att = attach_path(p, policy=AttachPolicy(root=str(tmp_path)))
    assert att.display.startswith("screenshot.png (")
    assert "KB" in att.display


def test_source_and_kind_vocabularies_are_validated() -> None:
    with pytest.raises(ValueError):
        attach_text("x", source="telepathy")
    with pytest.raises(ValueError):
        attach_text("x", source="stdin", kind="hologram")


def test_module_is_python_39_parseable() -> None:
    import ast
    import pathlib

    import mantis_agent.input as pkg

    for path in sorted(pathlib.Path(pkg.__file__).parent.glob("*.py")):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
