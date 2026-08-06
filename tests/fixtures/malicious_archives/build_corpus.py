"""Generator for the malicious-archive corpus.

The archives in this directory are *hostile inputs*, committed as bytes so the
extraction tests exercise the real parsers on the real wire format rather than
on whatever a mock felt like producing. Nothing here is ever extracted by
anything except :func:`mantis_agent.plugins.archive.safe_extract`, which is the
code under test and is expected to refuse every one of them.

They are generated rather than hand-authored because several cases (a hardlink
escape, a character-device entry, a zip whose central directory *lies* about an
entry's uncompressed size) cannot be produced by tarring a directory — they
need headers written directly. ``TarInfo``/``ZipInfo`` let us do exactly that
without ever creating a device node or a dangling symlink on a real filesystem.

Output is byte-deterministic (fixed mtimes, uid/gid, and zip timestamps) so
``test_plugin_archive.py`` can assert the committed files still match what this
script produces. A corpus that silently drifts from its generator is a corpus
nobody trusts.

Regenerate with::

    uv run python tests/fixtures/malicious_archives/build_corpus.py
"""

from __future__ import annotations

import gzip
import io
import struct
import tarfile
import zipfile
from pathlib import Path
from typing import Callable

# Every entry gets the same fixed metadata so the bytes are reproducible.
_EPOCH_TAR = 0
_EPOCH_ZIP = (1980, 1, 1, 0, 0, 0)

#: Plausible-looking plugin content, so a rejection is clearly about the
#: *shape* of the archive and not about the payload being obvious garbage.
_SKILL = b"---\nname: py-style\ndescription: Python conventions\n---\n\nUse ruff.\n"
_MANIFEST = (
    b'{"schemaVersion": 1, "name": "python-pack", "version": "1.4.2",\n'
    b' "description": "Python conventions"}\n'
)


# ---------------------------------------------------------------------------
# tar helpers
# ---------------------------------------------------------------------------


def _tar_info(
    name: str,
    *,
    kind: bytes = tarfile.REGTYPE,
    size: int = 0,
    mode: int = 0o644,
    target: str = "",
) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.type = kind
    ti.size = size
    ti.mode = mode
    ti.mtime = _EPOCH_TAR
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.linkname = target
    return ti


def _tar(entries: list[tuple[tarfile.TarInfo, bytes]], *, gz: bool = False) -> bytes:
    """Build a tar (optionally gzipped) from explicit headers + payloads."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for ti, payload in entries:
            tf.addfile(ti, io.BytesIO(payload) if payload else None)
    data = raw.getvalue()
    if not gz:
        return data
    out = io.BytesIO()
    # mtime=0 and no filename field: gzip headers otherwise embed the clock.
    with gzip.GzipFile(filename="", fileobj=out, mode="wb", mtime=0) as gf:
        gf.write(data)
    return out.getvalue()


def _plain_file(name: str, payload: bytes = _SKILL, mode: int = 0o644) -> tuple:
    return (_tar_info(name, size=len(payload), mode=mode), payload)


# ---------------------------------------------------------------------------
# zip helpers
# ---------------------------------------------------------------------------


def _zip(entries: list[tuple[str, bytes, int]], *, compress: bool = False) -> bytes:
    """Build a zip. ``entries`` is ``(name, payload, unix_mode)``; the mode is
    stored the way Info-ZIP does it (``st_mode`` in the high half of
    ``external_attr``), which is how symlink and setuid bits ride in a zip."""
    out = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(out, "w", compression=method) as zf:
        for name, payload, mode in entries:
            zi = zipfile.ZipInfo(name, date_time=_EPOCH_ZIP)
            zi.create_system = 3  # unix — required for external_attr to mean st_mode
            zi.external_attr = mode << 16
            zi.compress_type = method
            zf.writestr(zi, payload)
    return out.getvalue()


def _zip_with_lying_size(name: str, payload: bytes, declared: int) -> bytes:
    """A zip whose headers under-report an entry's uncompressed size.

    This is the case that decides whether extraction trusts metadata or counts
    bytes as it writes them. ``zipfile`` always writes truthful sizes, so the
    two size fields (local header + central directory) are patched afterwards.
    A single stored entry with no extra fields keeps both offsets fixed.
    """
    # Deflated so the committed fixture stays a few KB while the *stream* it
    # produces is megabytes — which is the whole point of the case.
    data = bytearray(_zip([(name, payload, 0o644)], compress=True))
    packed = struct.pack("<I", declared)
    # Local file header: sig(4) ver(2) flag(2) method(2) time(2) date(2)
    # crc(4) csize(4) usize(4) -> uncompressed size at offset 22.
    assert bytes(data[0:4]) == b"PK\x03\x04"
    data[22:26] = packed
    # Central directory header: sig(4) vermade(2) verneed(2) flag(2) method(2)
    # time(2) date(2) crc(4) csize(4) usize(4) -> uncompressed size at +24.
    cd = data.find(b"PK\x01\x02")
    assert cd > 0
    data[cd + 24 : cd + 28] = packed
    return bytes(data)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------
#
# Each entry is (filename, builder). The name states the attack; the docstring
# of the matching test states why refusing it matters.


def _c_absolute_path() -> bytes:
    return _tar([_plain_file("/etc/mantis-owned.md")])


def _c_absolute_path_zip() -> bytes:
    return _zip([("/etc/mantis-owned.md", _SKILL, 0o644)])


def _c_parent_traversal() -> bytes:
    return _tar([_plain_file("../escaped.md")])


def _c_parent_traversal_nested() -> bytes:
    return _tar([_plain_file("skills/../../../escaped.md")])


def _c_parent_traversal_zip() -> bytes:
    return _zip([("../escaped.md", _SKILL, 0o644)])


def _c_backslash_traversal() -> bytes:
    # Harmless on POSIX, a traversal on Windows. Rejected on both, because the
    # store is portable and a per-platform rule is a per-platform hole.
    return _tar([_plain_file("..\\..\\escaped.md")])


def _c_control_char_name() -> bytes:
    return _tar([_plain_file("skills/py\nstyle.md")])


def _c_symlink_escape() -> bytes:
    return _tar([(_tar_info("link.md", kind=tarfile.SYMTYPE, target="../../../etc/passwd"), b"")])


def _c_symlink_absolute() -> bytes:
    return _tar([(_tar_info("link.md", kind=tarfile.SYMTYPE, target="/etc/passwd"), b"")])


def _c_symlink_inside() -> bytes:
    # Points *inside* the root, so it is only rejected by the default
    # no-links-at-all policy — the case that proves the policy is the gate.
    return _tar(
        [
            _plain_file("skills/py-style.md"),
            (_tar_info("alias.md", kind=tarfile.SYMTYPE, target="skills/py-style.md"), b""),
        ]
    )


def _c_symlink_dir_then_write() -> bytes:
    # Two steps that are each individually fine: a symlink to a directory
    # *inside* the root, then a member written through it. The write lands
    # somewhere the listing never said it would — and if the link target were
    # swapped between the check and the write, somewhere else again.
    return _tar(
        [
            (_tar_info("assets/", kind=tarfile.DIRTYPE, mode=0o755), b""),
            (_tar_info("skills", kind=tarfile.SYMTYPE, target="assets"), b""),
            _plain_file("skills/owned.md"),
        ]
    )


def _c_hardlink_escape() -> bytes:
    return _tar([(_tar_info("link.md", kind=tarfile.LNKTYPE, target="../../etc/passwd"), b"")])


def _c_symlink_zip() -> bytes:
    return _zip([("link.md", b"/etc/passwd", 0o120777)])


def _c_device_char() -> bytes:
    return _tar([(_tar_info("dev/urandom", kind=tarfile.CHRTYPE, mode=0o666), b"")])


def _c_device_block() -> bytes:
    return _tar([(_tar_info("dev/sda", kind=tarfile.BLKTYPE, mode=0o660), b"")])


def _c_fifo() -> bytes:
    return _tar([(_tar_info("pipe", kind=tarfile.FIFOTYPE, mode=0o644), b"")])


def _c_setuid() -> bytes:
    return _tar([_plain_file("tools/helper", mode=0o4755)])


def _c_setgid() -> bytes:
    return _tar([_plain_file("tools/helper", mode=0o2755)])


def _c_setuid_zip() -> bytes:
    return _zip([("tools/helper", b"#!/bin/sh\n", 0o104755)])


def _c_duplicate_entries() -> bytes:
    # Two headers for one path: the second silently overwrites the first, so a
    # reviewer reading entry one is not reading what lands on disk.
    return _tar([_plain_file("skills/py-style.md"), _plain_file("skills/py-style.md", b"evil")])


def _c_duplicate_case() -> bytes:
    # Same collision, spelled to survive a case-sensitive listing but not a
    # case-insensitive filesystem (macOS, Windows).
    return _tar([_plain_file("skills/py-style.md"), _plain_file("skills/PY-Style.md", b"evil")])


def _c_too_many_files() -> bytes:
    return _tar([_plain_file("skills/s%03d.md" % i, b"x") for i in range(64)])


def _c_too_large() -> bytes:
    # 64 KiB, tested against a 32 KiB cap: the cap is a parameter, so the
    # fixture does not need to be 200 MB to prove the arithmetic.
    return _tar([_plain_file("skills/big.md", b"A" * (64 * 1024))])


def _c_gzip_bomb() -> bytes:
    # 8 MiB of zeros in a few KB of gzip: the ratio, not the size, is the tell.
    return _tar([_plain_file("skills/bomb.md", b"\0" * (8 * 1024 * 1024))], gz=True)


def _c_zip_bomb() -> bytes:
    return _zip([("skills/bomb.md", b"\0" * (8 * 1024 * 1024), 0o644)], compress=True)


def _c_zip_lying_size() -> bytes:
    # Headers claim 32 bytes; the stream delivers 4 MiB.
    return _zip_with_lying_size("skills/liar.md", b"B" * (4 * 1024 * 1024), 32)


def _c_benign() -> bytes:
    """The control case. Everything the corpus rejects, this one must accept —
    a suite that only proves refusals can be passed by ``raise`` on line one."""
    return _tar(
        [
            _plain_file("mantis-plugin.json", _MANIFEST),
            (_tar_info("skills/", kind=tarfile.DIRTYPE, mode=0o755), b""),
            _plain_file("skills/py-style.md"),
        ],
        gz=True,
    )


CASES: dict[str, Callable[[], bytes]] = {
    "absolute_path.tar": _c_absolute_path,
    "absolute_path.zip": _c_absolute_path_zip,
    "parent_traversal.tar": _c_parent_traversal,
    "parent_traversal_nested.tar": _c_parent_traversal_nested,
    "parent_traversal.zip": _c_parent_traversal_zip,
    "backslash_traversal.tar": _c_backslash_traversal,
    "control_char_name.tar": _c_control_char_name,
    "symlink_escape.tar": _c_symlink_escape,
    "symlink_absolute.tar": _c_symlink_absolute,
    "symlink_inside.tar": _c_symlink_inside,
    "symlink_dir_then_write.tar": _c_symlink_dir_then_write,
    "hardlink_escape.tar": _c_hardlink_escape,
    "symlink.zip": _c_symlink_zip,
    "device_char.tar": _c_device_char,
    "device_block.tar": _c_device_block,
    "fifo.tar": _c_fifo,
    "setuid.tar": _c_setuid,
    "setgid.tar": _c_setgid,
    "setuid.zip": _c_setuid_zip,
    "duplicate_entries.tar": _c_duplicate_entries,
    "duplicate_case.tar": _c_duplicate_case,
    "too_many_files.tar": _c_too_many_files,
    "too_large.tar": _c_too_large,
    "gzip_bomb.tar.gz": _c_gzip_bomb,
    "zip_bomb.zip": _c_zip_bomb,
    "zip_lying_size.zip": _c_zip_lying_size,
    "benign.tar.gz": _c_benign,
}

#: The one archive in here that must *succeed*.
BENIGN = "benign.tar.gz"


def build_all(dest: Path) -> dict[str, Path]:
    """Write the whole corpus into ``dest`` and return ``{name: path}``."""
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for name, builder in CASES.items():
        p = dest / name
        p.write_bytes(builder())
        out[name] = p
    return out


if __name__ == "__main__":  # pragma: no cover — authoring tool
    written = build_all(Path(__file__).resolve().parent)
    for name in sorted(written):
        print(f"{written[name].stat().st_size:>9}  {name}")
