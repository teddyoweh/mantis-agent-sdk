"""Where MCP OAuth credentials live, and when they need refreshing.

The plan's rule, in one sentence: *credentials never touch the MCP config
files*. ``settings.json`` and ``.mcp.json`` describe **which** servers exist;
this module owns the tokens, in a separate store that config-reading and
config-writing code has no path to. That is what makes the existing
``redact_mcp_entry`` machinery unnecessary for OAuth servers rather than
merely careful — there is nothing in the entry to redact.

Two decisions are load-bearing.

**The key is ``(server, issuer, resource)``, hashed.** Re-pointing a server at
a different host, or an authorization server changing identity, produces a
different key and therefore a miss — the user re-authenticates instead of
silently reusing a credential minted for somewhere else. Editing a server's
URL invalidates its stored credential *by construction*, not by remembering to
invalidate it. The hash also means a hostile server name cannot become a path.

**Refresh is proactive, at 80% of lifetime.** Refreshing on failure means a
long tool call discovers the expiry mid-flight and fails in a way the model
cannot interpret. :meth:`OAuthCredential.is_refresh_due` is the predicate the
single-flight refresh (a later phase) will schedule against.

Storage is a per-credential ``0o600`` JSON file in a ``0o700`` directory,
written atomically. :class:`CredentialBackend` is the seam an OS keychain
backend (macOS ``security``, libsecret) drops into without any caller
changing — see the note on :func:`default_backend`.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import time
from pathlib import Path
from typing import Any, Optional, Protocol

import msgspec

from ..paths import get_mantis_agent_dir
from .oauth import TokenResponse, canonical_resource

__all__ = [
    "CREDENTIAL_VERSION",
    "DEFAULT_REFRESH_FRACTION",
    "REDACTED",
    "CredentialBackend",
    "CredentialStore",
    "FileCredentialBackend",
    "OAuthCredential",
    "credential_key",
    "credentials_dir",
    "default_backend",
]

#: Bumped when the stored shape changes incompatibly. A record whose version we
#: do not understand is treated as a miss (re-login) rather than guessed at.
CREDENTIAL_VERSION = 1

#: Refresh at this fraction of the token's lifetime. 0.8 of an hour-long token
#: is a 12-minute cushion — longer than any tool call we let run.
DEFAULT_REFRESH_FRACTION = 0.8

#: What a secret becomes in a display. Same spelling as ``job_records.REDACTED``
#: and ``workflow_store.redact_inputs`` so the three artifacts read alike.
REDACTED = "[redacted]"

_DIR_MODE = 0o700
_FILE_MODE = 0o600

# Scratch-file marker, mirroring ``job_records``: the writer builds temp names
# around it and every reader skips anything carrying it, so a crash between
# "written" and "renamed" leaves litter that no listing can mistake for a
# credential.
_TMP_MARK = ".tmp-"
_TMP_SEQ = itertools.count()

_ENCODER = msgspec.json.Encoder()


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def credential_key(server: str, issuer: str, resource: str) -> str:
    """A stable, opaque key for one ``(server, issuer, resource)`` triple.

    Components are canonicalized (so ``:443`` or a trailing slash does not
    strand a credential) and then **length-prefixed** before hashing, so
    ``("a", "b", "c")`` and ``("ab", "", "c")`` cannot collide — an ambiguous
    key is a way to make one server's credential answer for another's, which is
    precisely what keying by the triple exists to prevent.

    Truncated to 128 bits: this is an index, not a MAC, and 32 hex characters
    keeps the filename readable while leaving collision odds negligible."""

    parts = (
        str(server or "").strip(),
        canonical_resource(issuer),
        canonical_resource(resource),
    )
    h = hashlib.sha256()
    for part in parts:
        raw = part.encode("utf-8")
        h.update(str(len(raw)).encode("ascii"))
        h.update(b"\x00")
        h.update(raw)
    return h.hexdigest()[:32]


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


class OAuthCredential(msgspec.Struct):
    """One server's OAuth credential, as it survives the session that got it.

    Mutable rather than frozen because refresh rewrites the token in place in
    the manager's cache; :meth:`rotated` returns a new value for callers that
    prefer the immutable spelling.

    ``client_secret`` is present because dynamic client registration (RFC 7591)
    may issue one for a confidential client. It is a secret like any other and
    is covered by :meth:`secret_values` and :meth:`redacted`."""

    server: str
    issuer: str
    resource: str
    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    scope: str = ""
    client_id: str = ""
    client_secret: str = ""
    obtained_at: float = msgspec.field(default_factory=time.time)
    expires_at: float | None = None
    version: int = CREDENTIAL_VERSION

    # -- identity ----------------------------------------------------------

    @property
    def key(self) -> str:
        return credential_key(self.server, self.issuer, self.resource)

    # -- timing ------------------------------------------------------------

    @property
    def lifetime_s(self) -> float | None:
        """Seconds between issue and expiry, or ``None`` for a token with no
        stated lifetime."""

        if self.expires_at is None:
            return None
        return self.expires_at - self.obtained_at

    def is_expired(self, now: float | None = None) -> bool:
        """True once the token is past its stated expiry. A token with no
        expiry never reads as expired — we cannot know, and guessing would
        force a re-login on a credential that still works."""

        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at

    def refresh_at(self, fraction: float = DEFAULT_REFRESH_FRACTION) -> float | None:
        """The wall-clock moment proactive refresh should fire.

        ``obtained_at + fraction * lifetime``. ``None`` when the token has no
        expiry: there is no lifetime to take a fraction of, so refresh is
        reactive (a 401) for those servers."""

        if not isinstance(fraction, float) or not 0.0 < fraction <= 1.0:
            raise ValueError("refresh fraction must be a float in (0, 1]")
        lifetime = self.lifetime_s
        if lifetime is None:
            return None
        return self.obtained_at + fraction * lifetime

    def is_refresh_due(self, now: float | None = None,
                       fraction: float = DEFAULT_REFRESH_FRACTION) -> bool:
        """Whether to refresh *before* using this token.

        The boundary is inclusive — at exactly 80% of lifetime the answer is
        yes. A server that hands back an already-expired token (``expires_at``
        at or before ``obtained_at``) is due immediately rather than reading as
        eternally fresh, which is what a naive ``now >= obtained + 0.8 * (neg)``
        would produce."""

        moment = self.refresh_at(fraction)
        if moment is None:
            return False
        current = now if now is not None else time.time()
        # Expired is always due, whatever the fraction computed. With a
        # backwards expiry the fraction lands *after* the expiry, and refusing
        # to refresh a token that is already dead is the worst possible answer.
        return current >= moment or self.is_expired(current)

    # -- rotation ----------------------------------------------------------

    @classmethod
    def from_token_response(
        cls,
        token: TokenResponse,
        *,
        server: str,
        issuer: str,
        resource: str,
        client_id: str = "",
        client_secret: str = "",
        now: float | None = None,
    ) -> OAuthCredential:
        """Build a credential from a validated token response.

        The clock is read *once*, here, and stored — ``expires_in`` is relative
        and becomes meaningless the moment it is written down without the
        instant it was relative to."""

        obtained = now if now is not None else time.time()
        return cls(
            server=server,
            issuer=issuer,
            resource=resource,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            token_type=token.token_type or "Bearer",
            scope=token.scope,
            client_id=client_id,
            client_secret=client_secret,
            obtained_at=obtained,
            expires_at=(obtained + token.expires_in) if token.expires_in else None,
        )

    def rotated(self, token: TokenResponse, *, now: float | None = None) -> OAuthCredential:
        """This credential, refreshed.

        Identity (``server``/``issuer``/``resource``) and client registration
        carry over unchanged, so the key is stable across a refresh. A rotated
        refresh token replaces the old one; RFC 6749 §6 lets the response omit
        one, and omission means *keep using the current one* — dropping it
        would turn a successful refresh into a forced re-login next time."""

        return OAuthCredential(
            server=self.server,
            issuer=self.issuer,
            resource=self.resource,
            access_token=token.access_token,
            refresh_token=token.refresh_token or self.refresh_token,
            token_type=token.token_type or self.token_type,
            scope=token.scope or self.scope,
            client_id=self.client_id,
            client_secret=self.client_secret,
            obtained_at=now if now is not None else time.time(),
            expires_at=((now if now is not None else time.time()) + token.expires_in)
            if token.expires_in else None,
        )

    # -- leak control ------------------------------------------------------

    def authorization_header(self) -> str:
        """The header value to attach per request, composed with any static
        ``headers`` the server config already carries."""

        return f"{self.token_type or 'Bearer'} {self.access_token}"

    def secret_values(self) -> tuple[str, ...]:
        """Every string in this credential that must never be displayed.

        Handed to the session redactor on load, per §5 of the plan, so a token
        echoed back by a tool result or a trace is scrubbed at the sink rather
        than depending on every sink remembering."""

        return tuple(v for v in (self.access_token, self.refresh_token, self.client_secret) if v)

    def redacted(self) -> dict[str, Any]:
        """A dict safe to log, print, or put in a status row.

        Secrets are replaced, not truncated: a prefix of a token is still a
        prefix of a token. What survives is the part a user debugging a login
        actually needs — which server, which issuer, which audience, when it
        expires, and whether a refresh token exists at all."""

        return {
            "server": self.server,
            "issuer": self.issuer,
            "resource": self.resource,
            "key": self.key,
            "client_id": self.client_id,
            "scope": self.scope,
            "token_type": self.token_type,
            "obtained_at": self.obtained_at,
            "expires_at": self.expires_at,
            "access_token": REDACTED if self.access_token else "",
            "refresh_token": REDACTED if self.refresh_token else "",
            "client_secret": REDACTED if self.client_secret else "",
            "version": self.version,
        }

    def __repr__(self) -> str:
        """Never the generated one.

        msgspec's default repr prints every field, so a traceback carrying
        locals, a ``print`` in a debug path, or a struct nested inside a logged
        error would emit the bearer token verbatim. "No credential appears in
        any log or trace" cannot depend on nobody ever repr'ing this object."""

        expiry = "never" if self.expires_at is None else f"{self.expires_at:.0f}"
        return (f"OAuthCredential(server={self.server!r}, issuer={self.issuer!r}, "
                f"resource={self.resource!r}, scope={self.scope!r}, "
                f"expires_at={expiry}, access_token={REDACTED}, "
                f"refresh_token={REDACTED if self.refresh_token else ''!r})")


_DECODER = msgspec.json.Decoder(OAuthCredential)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class CredentialBackend(Protocol):
    """A place credentials can live: bytes in, bytes out, keyed by string.

    Deliberately dumb — no knowledge of OAuth, expiry, or the credential
    struct — so a keychain implementation is a few subprocess calls and not a
    second copy of the serialization and validation logic."""

    name: str

    def get(self, key: str) -> Optional[bytes]: ...
    def put(self, key: str, blob: bytes) -> None: ...
    def delete(self, key: str) -> bool: ...
    def keys(self) -> list[str]: ...


def credentials_dir() -> Path:
    """``$MANTIS_MCP_CREDENTIALS_DIR`` or ``$MANTIS_AGENT_HOME/mcp/credentials``.

    Not created here — creation is lazy, per :func:`mantis_agent.paths.ensure_dir`'s
    convention, so importing this module never touches the filesystem."""

    override = os.environ.get("MANTIS_MCP_CREDENTIALS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return get_mantis_agent_dir() / "mcp" / "credentials"


class FileCredentialBackend:
    """``0o600`` JSON files in a ``0o700`` directory.

    The fallback in the plan's storage section, and the only backend that works
    everywhere. Each credential is its own file: deletion is an ``unlink`` that
    cannot lose a sibling, and two servers being written concurrently never
    touch the same bytes."""

    name = "file"

    def __init__(self, directory: Path | str | None = None) -> None:
        self._dir = Path(directory) if directory is not None else None

    @property
    def directory(self) -> Path:
        """Resolved late so a test (or a user) changing the environment between
        construction and use is honored, matching how the rest of the SDK reads
        ``$MANTIS_AGENT_HOME``."""

        return self._dir if self._dir is not None else credentials_dir()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def _ensure_dir(self) -> Path:
        d = self.directory
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(_DIR_MODE)
        except OSError:  # pragma: no cover — Windows / network filesystems
            pass
        return d

    def get(self, key: str) -> Optional[bytes]:
        try:
            return self._path(key).read_bytes()
        except OSError:
            return None

    def put(self, key: str, blob: bytes) -> None:
        """Atomic, private write.

        The file is created ``0o600`` by :func:`os.open` rather than chmod'ed
        afterwards: there must be no window, however short, in which a bearer
        token is world-readable. The scratch file is a sibling, so the rename is
        same-filesystem and cannot degrade into a copy."""

        self._ensure_dir()
        target = self._path(key)
        tmp = target.with_name(f"{target.name}{_TMP_MARK}{os.getpid()}-{next(_TMP_SEQ)}")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        try:
            try:
                fh = os.fdopen(fd, "wb")
            except BaseException:  # pragma: no cover — only a bad fd gets here
                os.close(fd)
                raise
            with fh:
                fh.write(blob)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:  # pragma: no cover — tmpfs / pseudo-fs
                    pass
            os.replace(str(tmp), str(target))
        except BaseException:
            # Never leave litter — including on KeyboardInterrupt, which is the
            # "crash mid-write" case this dance exists for.
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink()
            return True
        except OSError:
            return False

    def keys(self) -> list[str]:
        d = self.directory
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json") if _TMP_MARK not in p.name)


def default_backend() -> CredentialBackend:
    """The backend to use when a caller does not name one.

    Today: always the file backend. The plan prefers an OS keychain where one
    is available (macOS Keychain, libsecret on Linux); those backends are
    subprocess-driven and belong to the wiring step, and they slot in here
    without any caller changing because everything above talks to
    :class:`CredentialBackend`. The fallback is not a degraded mode — a
    ``0o700``/``0o600`` file under the user's home is the same protection
    boundary the rest of mantis's state already relies on."""

    return FileCredentialBackend()


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class CredentialStore:
    """Typed access to stored credentials, over any backend.

    Every read re-checks that the stored blob's own ``(server, issuer,
    resource)`` derives the key it was filed under. Without that, moving one
    file over another's name would hand a server a token minted for somewhere
    else — the same threat :func:`~mantis_agent.mcp.oauth.validate_token_audience`
    covers on the wire, one layer down, and cheap enough to check on every
    load."""

    def __init__(self, backend: CredentialBackend | None = None) -> None:
        self.backend = backend if backend is not None else default_backend()

    # -- read --------------------------------------------------------------

    def load_key(self, key: str) -> OAuthCredential | None:
        """The credential filed under ``key``, or ``None``.

        A miss, a corrupt blob, an unknown version, and a blob whose identity
        disagrees with its key are all the same answer: ``None``, meaning
        "log in again". A credential store that raises on a damaged file would
        take the whole session down over something a re-login fixes."""

        blob = self.backend.get(key)
        if not blob:
            return None
        try:
            cred = _DECODER.decode(blob)
        except (msgspec.DecodeError, msgspec.ValidationError, ValueError):
            return None
        if cred.version != CREDENTIAL_VERSION:
            return None
        if not cred.access_token or cred.key != key:
            return None
        return cred

    def load(self, server: str, issuer: str, resource: str) -> OAuthCredential | None:
        return self.load_key(credential_key(server, issuer, resource))

    def all(self) -> list[OAuthCredential]:
        """Every readable credential, newest-obtained last. Unreadable entries
        are skipped silently — this feeds status displays, not forensics."""

        creds = [c for c in (self.load_key(k) for k in self.backend.keys()) if c is not None]
        creds.sort(key=lambda c: (c.obtained_at, c.server))
        return creds

    def secret_values(self) -> tuple[str, ...]:
        """Every secret across every stored credential, for the redactor."""

        out: list[str] = []
        for cred in self.all():
            out.extend(cred.secret_values())
        return tuple(dict.fromkeys(out))

    # -- write -------------------------------------------------------------

    def save(self, cred: OAuthCredential) -> str:
        """Persist ``cred``; returns the key it was filed under.

        ``version`` is normalized on the way out: this writer writes this
        format, whatever the struct happened to be carrying."""

        cred.version = CREDENTIAL_VERSION
        key = cred.key
        self.backend.put(key, _ENCODER.encode(cred))
        return key

    def delete(self, server: str, issuer: str, resource: str) -> bool:
        """Remove one credential. ``False`` when there was nothing to remove.

        ``logout`` calls this *after* attempting revocation and regardless of
        whether revocation succeeded — a failed revocation must not leave a
        credential on disk."""

        return self.backend.delete(credential_key(server, issuer, resource))
