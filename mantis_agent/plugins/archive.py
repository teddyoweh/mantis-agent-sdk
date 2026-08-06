"""Safe archive extraction — the highest-severity component of the plugin plan.

An archive is attacker-controlled input in the most literal sense: someone else
wrote every byte of it, including the *file names*. ``tarfile.extractall`` and
``ZipFile.extractall`` are, historically, remote-file-write primitives — the
member name is used as a path, so ``../../../.ssh/authorized_keys`` is a valid
entry and both stdlib extractors happily wrote it for twenty-odd years.
Python 3.12 added ``tarfile``'s ``data`` filter for exactly this, but it is not
available across the versions this SDK supports, it does not cover zip, and it
does not enforce the size ceilings a package installer needs. So this module
does the work itself.

Design, in the order the decisions matter:

**Decide on headers, write afterwards.** :func:`inspect_archive` reads only
metadata and either returns a complete, validated member list or raises. No
file is opened for writing until every entry has been judged. "Extract then
check where it landed" is the same bug with extra steps — by the time you look,
the write has happened.

**Judge the member set, not each member.** An in-root symlink is fine. A file
written under a directory that another entry made a symlink is not, and neither
entry is suspicious alone. Containment is therefore evaluated across the whole
listing, which is only possible because inspection completes first.

**Never trust a declared size.** A zip's central directory can claim 32 bytes
for an entry whose stream delivers four megabytes. Sizes are sanity-checked
against the compressed size at inspection, and counted again while writing.

**Temp directory, then rename.** Extraction populates a sibling temp directory
and ``os.replace``s it into place only once everything succeeded. A failure —
any failure, including an interrupt — removes the temp tree and leaves the
destination exactly as it was. There is no state in which a plugin is half
installed.

**Normalize permissions.** Archive modes are attacker input too. Files land
0o600 and directories 0o700; setuid/setgid entries are refused outright rather
than stripped, because a package that ships one is not a package to fix up
quietly.

Limits come from ``settings.plugins.install`` (see the plan's §13) and are
passed in as :class:`ExtractLimits`; the defaults here match those documented
values so a caller that has no settings still gets the intended ceilings.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple, Union

from . import ArchiveTooLargeError, UnsafeArchiveError

__all__ = [
    "DEFAULT_LIMITS",
    "ArchiveMember",
    "ExtractLimits",
    "ExtractResult",
    "inspect_archive",
    "safe_extract",
    "safe_member_path",
]

#: Member kinds. Only ``file`` and ``dir`` are ever written without an opt-in.
FILE = "file"
DIR = "dir"
SYMLINK = "symlink"
HARDLINK = "hardlink"

_CHUNK = 64 * 1024

#: ``C:\`` / ``C:/`` — absolute on Windows, a relative path with a funny name
#: on POSIX. Refused on both: the store is portable, and a rule that only
#: applies on one platform is a hole on the other.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ExtractLimits:
    """Ceilings applied to one archive. Defaults mirror ``plugins.install``.

    ``max_compression_ratio`` only applies once the expansion exceeds
    ``ratio_floor_bytes``: a 300-byte manifest in a 200-byte gzip is a ratio of
    1.5 on numbers too small to mean anything, and a rule that fires on healthy
    inputs gets turned off. The floor is what keeps the bomb check credible.
    """

    max_archive_bytes: int = 50 * 1024 * 1024
    max_uncompressed_bytes: int = 200 * 1024 * 1024
    max_files: int = 5000
    max_compression_ratio: int = 100
    ratio_floor_bytes: int = 1024 * 1024
    max_name_length: int = 200
    max_depth: int = 16
    #: Links are refused entirely unless a caller opts in. Plugin content is
    #: markdown and JSON; nothing in the format needs a link, so the safe
    #: default is the one that needs no reasoning about targets at all.
    allow_links: bool = False


DEFAULT_LIMITS = ExtractLimits()


@dataclass(frozen=True)
class ArchiveMember:
    """One validated entry: ``name`` is already normalized and known safe.

    ``raw`` keeps the name exactly as the archive spelled it, because that is
    the key the underlying reader needs to hand back a stream — normalizing
    ``./skills/x.md`` for our own use must not change which entry we read.
    """

    name: str
    kind: str
    size: int = 0
    mode: int = 0o644
    target: Optional[str] = None
    raw: str = ""

    @property
    def lookup_name(self) -> str:
        return self.raw or self.name


@dataclass(frozen=True)
class ExtractResult:
    """What landed, for the installer's log and the approval prompt."""

    root: Path
    file_count: int
    total_bytes: int
    members: Tuple[ArchiveMember, ...]


# ---------------------------------------------------------------------------
# Member names
# ---------------------------------------------------------------------------


def safe_member_path(
    name: str,
    *,
    max_length: int = DEFAULT_LIMITS.max_name_length,
    max_depth: int = DEFAULT_LIMITS.max_depth,
) -> str:
    """Normalize one archive member name, or raise :class:`UnsafeArchiveError`.

    Returns a relative POSIX path with ``.`` and empty segments collapsed. The
    normalization happens *before* the ``..`` check, because ``a/b/../../..``
    is a traversal that a naive "does it start with ``..``" test waves through.
    """
    if not isinstance(name, str):
        raise UnsafeArchiveError("bad-encoding", None, "member name is not text")
    try:
        # tarfile decodes names with surrogateescape, so undecodable bytes
        # survive as lone surrogates. They cannot be written portably and are
        # a classic way to smuggle a name past a string comparison.
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise UnsafeArchiveError("bad-encoding", None, "member name is not valid UTF-8") from None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise UnsafeArchiveError("control-char", name, "control character in member name")
    if "\\" in name:
        raise UnsafeArchiveError("backslash", name, "backslash is a path separator on Windows")
    if _DRIVE_RE.match(name):
        raise UnsafeArchiveError("drive-letter", name, "Windows drive-absolute path")
    if name.startswith("/"):
        raise UnsafeArchiveError("absolute-path", name, "member escapes any extraction root")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchiveError("parent-traversal", name, "member walks above the root")
    if not parts:
        raise UnsafeArchiveError("empty-name", name, "member has no path")
    normalized = "/".join(parts)
    if len(normalized) > max_length:
        raise UnsafeArchiveError("name-too-long", name, f"{len(normalized)} > {max_length}")
    if len(parts) > max_depth:
        raise UnsafeArchiveError("too-deep", name, f"{len(parts)} > {max_depth} components")
    return normalized


def _link_stays_inside(link_name: str, target: str) -> bool:
    """Would following ``target`` from ``link_name`` stay under the root?

    Resolved lexically and deliberately: the archive has not been written yet,
    so there is nothing on disk to ``realpath``. An absolute target is out by
    definition; a relative one is walked from the link's own directory.
    """
    if not target or target.startswith("/") or _DRIVE_RE.match(target) or "\\" in target:
        return False
    depth = link_name.split("/")[:-1]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not depth:
                return False  # walked above the extraction root
            depth.pop()
        else:
            depth.append(part)
    return True


# ---------------------------------------------------------------------------
# Reading headers
# ---------------------------------------------------------------------------


def _tar_raw(path: Path) -> Iterator[ArchiveMember]:
    """Yield raw tar members one header at a time.

    ``next()`` rather than ``getmembers()`` so a caller can stop at the first
    violation: for a gzipped tar, skipping to the following header means
    decompressing the current member, and a 10 GB entry announced in its header
    should never be walked past just to build a complete listing.
    """
    with tarfile.open(str(path), mode="r:*") as tf:
        while True:
            ti = tf.next()
            if ti is None:
                return
            if ti.isdir():
                kind = DIR
            elif ti.issym():
                kind = SYMLINK
            elif ti.islnk():
                kind = HARDLINK
            elif ti.isreg():
                kind = FILE
            else:
                # Character/block devices, FIFOs, sockets, contiguous and
                # sparse entries. A plugin is markdown and JSON; anything else
                # is either a mistake or an attempt.
                raise UnsafeArchiveError(
                    "special-entry", ti.name, f"tar type {ti.type!r} is not a file or directory"
                )
            yield ArchiveMember(
                name=ti.name,
                kind=kind,
                size=int(ti.size) if kind == FILE else 0,
                mode=stat.S_IMODE(ti.mode),
                target=ti.linkname or None,
            )


def _zip_raw(path: Path) -> Iterator[ArchiveMember]:
    """Yield raw zip members from the central directory.

    A zip carries the unix mode in the high half of ``external_attr`` when it
    was produced on unix — which is how a symlink or a setuid bit rides inside
    an archive whose format has no concept of either.
    """
    with zipfile.ZipFile(str(path)) as zf:
        for zi in zf.infolist():
            unix_mode = (zi.external_attr >> 16) & 0xFFFF
            mode = stat.S_IMODE(unix_mode) if unix_mode else 0o644
            # Plenty of writers store permission bits with no file-type bits at
            # all; absent an S_IFMT the entry is whatever the name says it is.
            ftype = stat.S_IFMT(unix_mode)
            if ftype == stat.S_IFLNK:
                kind: str = SYMLINK
            elif zi.is_dir():
                kind = DIR
            elif ftype and ftype != stat.S_IFREG:
                raise UnsafeArchiveError(
                    "special-entry", zi.filename, f"unix mode {oct(unix_mode)} is not a plain file"
                )
            else:
                kind = FILE
            if kind == FILE:
                _check_declared_zip_size(zi)
            target = None
            if kind == SYMLINK:
                # A zip symlink stores its target as the entry's *content*.
                with zf.open(zi) as fh:
                    target = fh.read(4096).decode("utf-8", "replace")
            yield ArchiveMember(
                name=zi.filename,
                kind=kind,
                size=int(zi.file_size) if kind == FILE else 0,
                mode=mode,
                target=target,
            )


def _check_declared_zip_size(zi: zipfile.ZipInfo) -> None:
    """Refuse an entry whose headers under-report its uncompressed size.

    ``ZipExtFile`` stops reading at the declared size, so a lie here would
    silently truncate — and every ceiling computed from declared sizes (total
    bytes, compression ratio) would be computed from the attacker's numbers.
    Deflate never *expands* input by more than a fraction of a percent, so a
    compressed size meaningfully larger than the claimed uncompressed size is
    not a compressor artifact; it is a claim that does not add up.
    """
    declared, packed = int(zi.file_size), int(zi.compress_size)
    if packed > declared + 64 and packed > declared * 1.05:
        raise UnsafeArchiveError(
            "size-mismatch",
            zi.filename,
            f"declares {declared} uncompressed bytes but stores {packed} compressed",
        )


def _detect(path: Path) -> str:
    """``"zip"`` or ``"tar"`` by content, never by extension — the suffix is
    part of the untrusted input."""
    try:
        if zipfile.is_zipfile(str(path)):
            return "zip"
        if tarfile.is_tarfile(str(path)):
            return "tar"
    except (OSError, ValueError) as exc:  # pragma: no cover — unreadable/odd file
        raise UnsafeArchiveError("unknown-format", None, str(exc)) from exc
    raise UnsafeArchiveError("unknown-format", None, "not a tar or zip archive")


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def inspect_archive(
    archive: Union[str, Path], *, limits: ExtractLimits = DEFAULT_LIMITS
) -> Tuple[ArchiveMember, ...]:
    """Validate every member of ``archive`` and return the normalized listing.

    Raises before touching the filesystem. This is the whole security decision;
    :func:`safe_extract` is the same rules applied a second time while writing.
    """
    path = Path(archive)
    try:
        archive_bytes = path.stat().st_size
    except OSError as exc:
        raise UnsafeArchiveError("unreadable", None, str(exc)) from exc
    if archive_bytes > limits.max_archive_bytes:
        raise ArchiveTooLargeError(
            "archive-cap", None, f"{archive_bytes} > {limits.max_archive_bytes} bytes"
        )

    kind = _detect(path)
    raw = _tar_raw(path) if kind == "tar" else _zip_raw(path)

    members: list[ArchiveMember] = []
    seen: dict[str, str] = {}
    link_names: set[str] = set()
    total = 0
    try:
        for m in raw:
            if len(members) >= limits.max_files:
                raise ArchiveTooLargeError(
                    "too-many-files", m.name, f"more than {limits.max_files} entries"
                )
            name = safe_member_path(
                m.name, max_length=limits.max_name_length, max_depth=limits.max_depth
            )
            # Case-folded, because the store must behave the same on a
            # case-insensitive filesystem, where two "distinct" entries are one
            # file and the later one silently wins.
            key = name.lower()
            if key in seen:
                raise UnsafeArchiveError(
                    "duplicate-entry", m.name, f"collides with {seen[key]!r}"
                )
            seen[key] = name
            if m.mode & (stat.S_ISUID | stat.S_ISGID):
                raise UnsafeArchiveError("setuid", name, f"mode {oct(m.mode)} sets uid/gid")
            if m.kind in (SYMLINK, HARDLINK):
                if not limits.allow_links:
                    raise UnsafeArchiveError(
                        "link-not-allowed", name, f"{m.kind} entries are refused by policy"
                    )
                if not _link_stays_inside(name, m.target or ""):
                    raise UnsafeArchiveError(
                        "link-escape", name, f"{m.kind} target {m.target!r} leaves the root"
                    )
                link_names.add(name)
            elif m.kind == FILE:
                total += m.size
                if total > limits.max_uncompressed_bytes:
                    raise ArchiveTooLargeError(
                        "uncompressed-cap",
                        name,
                        f"{total} > {limits.max_uncompressed_bytes} uncompressed bytes",
                    )
            members.append(ArchiveMember(name, m.kind, m.size, m.mode, m.target, raw=m.name))
    except (tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
        raise UnsafeArchiveError("corrupt", None, str(exc)) from exc

    # Whole-listing checks: these are the ones a per-member loop cannot make.
    if link_names:
        for m in members:
            parents = m.name.split("/")[:-1]
            for i in range(len(parents)):
                ancestor = "/".join(parents[: i + 1])
                if ancestor in link_names:
                    raise UnsafeArchiveError(
                        "link-in-path", m.name, f"written through the link {ancestor!r}"
                    )
    if total > limits.ratio_floor_bytes and archive_bytes > 0:
        ratio = total / archive_bytes
        if ratio > limits.max_compression_ratio:
            raise ArchiveTooLargeError(
                "compression-ratio",
                None,
                f"{total} bytes from {archive_bytes} ({ratio:.0f}x > "
                f"{limits.max_compression_ratio}x)",
            )
    return tuple(members)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def safe_extract(
    archive: Union[str, Path], dest: Union[str, Path], *, limits: ExtractLimits = DEFAULT_LIMITS
) -> ExtractResult:
    """Extract ``archive`` into ``dest``, or raise having written nothing there.

    ``dest`` must not exist: a store entry is immutable and created once, and
    extracting *into* an existing directory is how a half-updated plugin comes
    about. The work happens in a sibling temp directory that is renamed into
    place as the final step, so the only two observable states are "absent" and
    "complete".
    """
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"refusing to extract over existing path: {dest}")
    members = inspect_archive(archive, limits=limits)

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Same directory as dest, so the final rename is atomic (a cross-filesystem
    # rename is a copy, and a copy can be interrupted halfway).
    tmp_root = Path(tempfile.mkdtemp(prefix=".mantis-plugin-", dir=str(dest.parent)))
    try:
        written, total = _write_members(Path(archive), tmp_root, members, limits)
        _assert_contained(tmp_root, members)
        os.replace(str(tmp_root), str(dest))
    except BaseException:
        # Every failure path, including KeyboardInterrupt: an abandoned temp
        # tree next to the store is residue, and residue gets garbage-collected
        # by somebody's shell one day.
        shutil.rmtree(str(tmp_root), ignore_errors=True)
        raise
    return ExtractResult(root=dest, file_count=written, total_bytes=total, members=members)


def _write_members(
    archive: Path, root: Path, members: Sequence[ArchiveMember], limits: ExtractLimits
) -> Tuple[int, int]:
    kind = _detect(archive)
    if kind == "tar":
        opener = tarfile.open(str(archive), mode="r:*")
    else:
        opener = zipfile.ZipFile(str(archive))
    written = 0
    total = 0
    try:
        with opener as handle:
            streams = _stream_lookup(handle, kind)
            for m in members:
                target_path = root / m.name
                if m.kind == DIR:
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if m.kind == SYMLINK:
                    os.symlink(m.target or "", str(target_path))
                    continue
                if m.kind == HARDLINK:
                    source = root / safe_member_path(m.target or "")
                    if not source.is_file():
                        raise UnsafeArchiveError(
                            "link-target-missing", m.name, f"hardlink to {m.target!r}"
                        )
                    os.link(str(source), str(target_path))
                    continue
                total += _write_file(streams(m), target_path, m, total, limits)
                written += 1
    except (tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
        raise UnsafeArchiveError("corrupt", None, str(exc)) from exc
    # Directories last: they are created 0o700 above, but a member may have
    # been written into a directory created implicitly by ``mkdir(parents=)``.
    for d in sorted(root.rglob("*"), reverse=True):
        if d.is_dir() and not d.is_symlink():
            os.chmod(str(d), 0o700)
    os.chmod(str(root), 0o700)
    return written, total


def _stream_lookup(handle, kind: str):
    """One accessor over both archive types, returning a binary stream."""
    if kind == "tar":

        def open_member(m: ArchiveMember):
            fh = handle.extractfile(m.lookup_name)
            if fh is None:  # pragma: no cover — only for non-regular members
                raise UnsafeArchiveError("special-entry", m.name, "no readable content")
            return fh

    else:

        def open_member(m: ArchiveMember):
            return handle.open(m.lookup_name)

    return open_member


def _write_file(
    stream, path: Path, member: ArchiveMember, already: int, limits: ExtractLimits
) -> int:
    """Copy one member to ``path``, counting bytes rather than believing them.

    ``O_EXCL`` is a second line under the duplicate-entry check: if two members
    ever normalize to the same path, the write fails loudly instead of the
    later entry quietly replacing the one a reviewer read.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    count = 0
    try:
        with os.fdopen(fd, "wb") as out, stream:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                count += len(chunk)
                if count > member.size:
                    raise UnsafeArchiveError(
                        "size-mismatch",
                        member.name,
                        f"stream exceeds the declared {member.size} bytes",
                    )
                if already + count > limits.max_uncompressed_bytes:
                    raise ArchiveTooLargeError(
                        "uncompressed-cap",
                        member.name,
                        f"beyond {limits.max_uncompressed_bytes} uncompressed bytes",
                    )
                out.write(chunk)
    finally:
        os.chmod(str(path), 0o600)
    if count != member.size:
        raise UnsafeArchiveError(
            "size-mismatch", member.name, f"declared {member.size} bytes, delivered {count}"
        )
    return count


def _assert_contained(root: Path, members: Sequence[ArchiveMember]) -> None:
    """Belt to the header checks' braces: every path that now exists resolves
    inside ``root``. Cheap, and the one check that would notice a bug in all
    the reasoning above."""
    base = os.path.realpath(str(root))
    prefix = base + os.sep
    for m in members:
        p = root / m.name
        for candidate in (os.path.realpath(str(p.parent)), os.path.realpath(str(p))):
            if candidate != base and not candidate.startswith(prefix):
                raise UnsafeArchiveError("escaped-root", m.name, f"resolved to {candidate}")
