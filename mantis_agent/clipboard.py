"""Clipboard + file attachments — paste an image or a file into a turn.

A faithful port of Claude Code's ``utils/imagePaste.ts`` clipboard handling:
on paste we ask the OS clipboard whether it holds an image and, if so, dump it
to a temp PNG, read the bytes, and turn it into an Anthropic-shaped
``ImageBlock``. Platform commands:

* **macOS**  — ``osascript`` (``the clipboard as «class PNGf»`` to detect/save,
  ``«class furl»`` to read a copied file path).
* **Linux**  — ``xclip`` / ``wl-paste`` with ``image/png`` targets.
* **Windows** — PowerShell ``Get-Clipboard -Format Image``.

Files (a pasted/dragged path) become an ``ImageBlock`` when they're an image,
or get read inline as text otherwise — so "paste a file" Just Works.

Stdlib + subprocess only; no Pillow/native deps. Images larger than the model
cap are passed through (callers may downsample) but we refuse absurdly large
blobs so a stray paste can't blow up the request.
"""

from __future__ import annotations

import base64
import os
import platform
import subprocess
import tempfile
from pathlib import Path

from .types import ImageBlock, TextBlock

# Magic-number → media type. Order matters (check longest/most-specific first).
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # WEBP = "RIFF....WEBP"
    (b"BM", "image/bmp"),
)
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB — refuse anything bigger
_MAX_TEXT_BYTES = 256 * 1024  # inline text-file cap


def _detect_media_type(data: bytes) -> str | None:
    for magic, mtype in _IMAGE_MAGIC:
        if data.startswith(magic):
            if mtype == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mtype
    return None


def _image_block(data: bytes, media_type: str) -> ImageBlock:
    return ImageBlock(source={
        "type": "base64",
        "media_type": media_type,
        "data": base64.b64encode(data).decode("ascii"),
    })


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=10, check=False)


# ---------------------------------------------------------------------------
# Clipboard image
# ---------------------------------------------------------------------------


def has_clipboard_image() -> bool:
    """True if the OS clipboard currently holds an image (cheap probe)."""
    system = platform.system()
    try:
        if system == "Darwin":
            return _run(["osascript", "-e", "the clipboard as «class PNGf»"]).returncode == 0
        if system == "Linux":
            r = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
            if r.returncode == 0 and b"image/" in r.stdout:
                return True
            r = _run(["wl-paste", "-l"])
            return r.returncode == 0 and b"image/" in r.stdout
        if system == "Windows":
            r = _run(["powershell", "-NoProfile", "-Command",
                      "(Get-Clipboard -Format Image) -ne $null"])
            return b"True" in r.stdout
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return False


def grab_clipboard_image() -> ImageBlock | None:
    """Read an image off the clipboard as an ``ImageBlock``, or ``None``."""
    system = platform.system()
    # Unique, O_EXCL temp file per grab. A constant filename in a shared temp
    # dir is a symlink/TOCTOU target (an attacker pre-creating the path can
    # redirect the write) and lets two concurrent sessions clobber each other's
    # file mid-read. mkstemp creates a fresh, unpredictable, exclusive file.
    fd, tmp_name = tempfile.mkstemp(suffix=".png", prefix="mantis_clipboard_")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if system == "Darwin":
            save = (
                'set png_data to (the clipboard as «class PNGf»)\n'
                f'set fp to open for access POSIX file "{tmp}" with write permission\n'
                'write png_data to fp\n'
                'close access fp'
            )
            if _run(["osascript", "-e", save]).returncode != 0:
                return None
        elif system == "Linux":
            data = _run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]).stdout
            if not data:
                data = _run(["wl-paste", "--type", "image/png"]).stdout
            if not data:
                return None
            tmp.write_bytes(data)
        elif system == "Windows":
            cmd = (
                "$img = Get-Clipboard -Format Image; if ($img) "
                f"{{ $img.Save('{tmp}', [System.Drawing.Imaging.ImageFormat]::Png) }}"
            )
            _run(["powershell", "-NoProfile", "-Command", cmd])
        else:
            return None

        if not tmp.exists():
            return None
        data = tmp.read_bytes()
        if not data or len(data) > _MAX_IMAGE_BYTES:
            return None
        media_type = _detect_media_type(data) or "image/png"
        return _image_block(data, media_type)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def grab_clipboard_file_path() -> str | None:
    """If the clipboard holds a file reference (a copied/dragged file), return
    its path (macOS only via ``«class furl»``)."""
    if platform.system() != "Darwin":
        return None
    try:
        r = _run(["osascript", "-e", "get POSIX path of (the clipboard as «class furl»)"])
        if r.returncode == 0:
            p = r.stdout.decode("utf-8", "replace").strip()
            return p or None
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    return None


# ---------------------------------------------------------------------------
# File attachments
# ---------------------------------------------------------------------------


def is_image_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _IMAGE_EXTS


def file_to_blocks(path: str | Path) -> list:
    """Turn a file into content blocks: an ``ImageBlock`` for images, otherwise
    the file's text inline as a ``TextBlock``. Raises if the path is missing."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {path}")
    if is_image_path(p):
        data = p.read_bytes()
        if len(data) > _MAX_IMAGE_BYTES:
            raise ValueError(f"image too large ({len(data)} bytes): {path}")
        media_type = _detect_media_type(data) or "image/png"
        return [_image_block(data, media_type)]
    # Non-image: inline the text (truncated).
    raw = p.read_bytes()[:_MAX_TEXT_BYTES]
    text = raw.decode("utf-8", "replace")
    note = "" if len(raw) < _MAX_TEXT_BYTES else "\n… [truncated]"
    return [TextBlock(text=f"[Attached file: {p}]\n```\n{text}{note}\n```")]


def looks_like_path(text: str) -> str | None:
    """If ``text`` is (just) a filesystem path to an existing file — e.g. what a
    drag-and-drop pastes — return the cleaned path, else ``None``."""
    s = text.strip().strip("'\"")
    if not s or "\n" in s:
        return None
    # Drag-drop on macOS escapes spaces with backslashes.
    s = s.replace("\\ ", " ")
    if (s.startswith("/") or s.startswith("~") or s.startswith("./")) and len(s) < 4096:
        p = Path(s).expanduser()
        if p.is_file():
            return str(p)
    return None


__all__ = [
    "file_to_blocks",
    "grab_clipboard_file_path",
    "copy_to_clipboard",
    "grab_clipboard_image",
    "has_clipboard_image",
    "is_image_path",
    "looks_like_path",
]


def copy_to_clipboard(text: str) -> bool:
    """Copy ``text`` to the OS clipboard. Tries the platform tool
    (pbcopy / wl-copy / xclip / clip); returns True on success, False if no
    clipboard tool is available. Best-effort, never raises."""
    import shutil  # noqa: PLC0415

    candidates: list[list[str]]
    if platform.system() == "Darwin":
        candidates = [["pbcopy"]]
    elif platform.system() == "Windows":
        candidates = [["clip"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            p = subprocess.run(  # noqa: S603
                cmd, input=text.encode("utf-8"), timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if p.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
