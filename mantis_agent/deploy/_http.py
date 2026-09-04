"""Shared HTTP plumbing for the deploy adapters.

Every provider request goes through :class:`DeployHttp` so the behaviour a
user notices — timeouts, ``429`` backoff that honours ``Retry-After``, the
JSON error message dug out of whatever envelope the vendor uses, and the
``401 → "check $X_API_KEY"`` hints — is decided once, here, rather than four
slightly different ways.

Cold-start ``503`` is deliberately **not** retried by :meth:`DeployHttp.request`
by default: on Modal / HF Endpoints / RunPod a 503 from the *inference* URL
means "a replica is booting", and the caller (``wait_ready`` / ``connect``)
wants to see it and keep polling with its own budget rather than have the
transport eat two minutes silently. Pass ``retry_statuses`` to opt in.

Nothing here imports a vendor SDK; ``httpx`` is the only dependency.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

import anyio
import httpx

from ..redaction import is_secret_name, redact_value
from .base import DeployError

__all__ = [
    "DeployHttp",
    "env_expand",
    "error_message",
    "mask_secrets",
    "poll_until",
    "raise_for",
    "require_env",
    "sleep",
    "slugify",
]

#: Control-plane requests are small JSON round-trips; ``read`` is generous
#: because a few providers hold the connection while they act (Baseten's
#: prepare-upload, RunPod's endpoint create).
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

#: Statuses that are safe to replay for an idempotent GET/DELETE.
RETRY_STATUSES: frozenset[int] = frozenset({408, 425, 429, 502, 504})

_RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


async def sleep(seconds: float) -> None:
    """Module-level so tests can monkeypatch the wait away."""

    if seconds > 0:
        await anyio.sleep(seconds)


def require_env(*names: str) -> bool:
    """All of ``names`` are set and non-blank in ``os.environ``."""

    return all((os.environ.get(n) or "").strip() for n in names)


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def env_expand(value: str) -> str:
    """Expand ``${VAR}`` references from ``os.environ`` (missing → empty)."""

    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


_SLUG_BAD = re.compile(r"[^a-z0-9-]+")


def slugify(text: str, *, max_len: int = 40) -> str:
    """``"Qwen/Qwen3-8B"`` → ``"qwen-qwen3-8b"`` — a name every provider accepts."""

    s = _SLUG_BAD.sub("-", text.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s[:max_len].rstrip("-")) or "mantis"


def mask_secrets(obj: Any) -> Any:
    """Recursively redact values under credential-looking keys so a provider's
    raw response can be persisted to ``deployments.json`` without leaking the
    ``HF_TOKEN`` we sent it."""

    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and is_secret_name(k) and isinstance(v, (str, int, float)):
                out[k] = redact_value(v)
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(obj, list):
        return [mask_secrets(v) for v in obj]
    if isinstance(obj, tuple):
        return [mask_secrets(v) for v in obj]
    return obj


def error_message(response: httpx.Response) -> str:
    """Best-effort human message from a vendor error body."""

    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — not JSON
        text = (response.text or "").strip()
        return text[:300] or response.reason_phrase or f"HTTP {response.status_code}"
    return _message_from_body(body) or response.reason_phrase or f"HTTP {response.status_code}"


def _message_from_body(body: Any) -> str | None:
    if isinstance(body, str):
        return body[:300]
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("detail") or err.get("type")
    if isinstance(err, str):
        return err
    for key in ("message", "detail", "msg", "description"):
        v = body.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, list) and v and isinstance(v[0], dict):
            m = v[0].get("msg") or v[0].get("message")
            if m:
                return str(m)
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return first.get("message") or first.get("detail")
        return str(first)
    return None


def raise_for(
    response: httpx.Response,
    *,
    provider: str,
    auth_env: str | Iterable[str] = (),
    console_url: str = "",
    what: str = "",
) -> None:
    """Turn an error response into a :class:`DeployError` with a hint that
    tells the user what to *do* — the status code alone never does."""

    status = response.status_code
    if status < 400:
        return
    envs = [auth_env] if isinstance(auth_env, str) else list(auth_env)
    env_txt = " / ".join(f"${e}" for e in envs) or "the API key"
    msg = error_message(response)
    where = f" ({what})" if what else ""
    hint: str | None
    if status in (401, 403):
        hint = (
            f"{env_txt} was rejected. Re-check the key"
            + (f" at {console_url}" if console_url else "")
            + f" and save it again with `mantis-agent deploy creds {provider} --set {envs[0]}=...`"
            if envs else f"the request was refused ({status}); check your credentials"
        )
    elif status == 402:
        hint = (
            "the provider reports a billing / quota problem — add credit or raise "
            "the GPU quota" + (f" at {console_url}" if console_url else "")
        )
    elif status == 404:
        hint = "not found on the provider — it may have been deleted from the console; `mantis-agent deploy ls --refresh` resyncs"
    elif status == 429:
        hint = "rate limited by the provider; wait a moment and retry"
    elif status >= 500:
        hint = "provider-side error; retry in a minute, then check the console status page"
    else:
        hint = None
    raise DeployError(f"{provider}: {msg}{where} [HTTP {status}]", hint=hint, provider=provider)


class DeployHttp:
    """A thin, retrying JSON client bound to one provider.

    ``headers`` are sent on every request. ``request`` returns the response
    (already checked unless ``raise_on_error=False``); ``json`` returns the
    decoded body.
    """

    def __init__(
        self,
        *,
        provider: str,
        base_url: str = "",
        headers: Mapping[str, str] | None = None,
        auth_env: str | Iterable[str] = (),
        console_url: str = "",
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        attempts: int = 4,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.auth_env = auth_env
        self.console_url = console_url
        self.timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self.attempts = max(1, attempts)

    # ------------------------------------------------------------------

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}" if self.base_url else path

    def _client(self, timeout: httpx.Timeout | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout or self.timeout, follow_redirects=True)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
        retry_statuses: Iterable[int] = RETRY_STATUSES,
        raise_on_error: bool = True,
        what: str = "",
    ) -> httpx.Response:
        merged = {**self.headers, **(headers or {})}
        retry_on = frozenset(retry_statuses)
        to = httpx.Timeout(timeout) if timeout is not None else self.timeout
        last_exc: Exception | None = None
        response: httpx.Response | None = None
        for attempt in range(self.attempts):
            try:
                async with self._client(to) as client:
                    response = await client.request(
                        method, self.url(path), json=json, params=params,
                        headers=merged, content=content,
                    )
            except _RETRY_EXCEPTIONS as e:
                last_exc = e
                if attempt + 1 >= self.attempts:
                    break
                await sleep(min(2.0 * (2 ** attempt), 20.0))
                continue
            if response.status_code in retry_on and attempt + 1 < self.attempts:
                await sleep(_retry_after(response, attempt))
                continue
            break
        if response is None:
            raise DeployError(
                f"{self.provider}: could not reach {self.url(path)}: {last_exc}",
                hint="check your network / the provider's status page",
                provider=self.provider,
            ) from last_exc
        if raise_on_error:
            raise_for(
                response, provider=self.provider, auth_env=self.auth_env,
                console_url=self.console_url, what=what,
            )
        return response

    async def json(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self.request(method, path, **kw)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise DeployError(
                f"{self.provider}: non-JSON reply from {self.url(path)}: {resp.text[:200]!r}",
                provider=self.provider,
            ) from None

    async def stream_lines(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        limit: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield text lines (SSE ``data:`` payloads are unwrapped) from a
        streaming endpoint, stopping after ``limit`` lines."""

        merged = {**self.headers, **(headers or {})}
        count = 0
        async with self._client(httpx.Timeout(timeout)) as client:
            async with client.stream(method, self.url(path), params=params, headers=merged) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise_for(resp, provider=self.provider, auth_env=self.auth_env,
                              console_url=self.console_url, what="logs")
                async for raw in resp.aiter_lines():
                    line = raw.rstrip("\r\n")
                    if not line or line.startswith((":", "event:", "id:", "retry:")):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                        if line in ("", "[DONE]"):
                            continue
                    yield line
                    count += 1
                    if limit is not None and count >= limit:
                        return


def _retry_after(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            v = float(header.strip())
            if v >= 0:
                return min(v, 30.0)
        except ValueError:
            pass
    return min(1.0 * (2 ** attempt), 20.0)


async def poll_until(
    check: Callable[[], Awaitable[tuple[bool, Any]]],
    *,
    timeout_s: float,
    interval_s: float = 5.0,
    what: str = "ready",
    provider: str | None = None,
) -> Any:
    """Call ``check`` until it returns ``(True, value)`` or the budget runs
    out. ``check`` may raise :class:`DeployError` to fail early."""

    deadline = anyio.current_time() + max(0.0, timeout_s)
    while True:
        done, value = await check()
        if done:
            return value
        if anyio.current_time() >= deadline:
            raise DeployError(
                f"timed out after {int(timeout_s)}s waiting for {what}",
                hint="the deployment may still come up — check `mantis-agent deploy status <id>` and the provider console",
                provider=provider,
            )
        await sleep(interval_s)
