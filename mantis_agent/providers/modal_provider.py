"""Modal serverless adapter — talk to LLMs deployed on modal.com.

Why a dedicated adapter
-----------------------
Modal-served LLMs almost universally expose an OpenAI-compatible
``/v1/chat/completions`` endpoint (vLLM, TGI, or sglang behind a Modal web
function). So the *wire* is the same as ``OpenAICompatProvider``. What's
different and worth a thin adapter:

1. **URL construction.** Modal web-function URLs follow a strict naming
   pattern: ``https://{workspace}--{app}-{function}{label}.modal.run``.
   Building that by hand is tedious and easy to typo. We accept either a
   full URL OR ``(workspace, app, function)`` and assemble it.

2. **Token-pair auth.** Modal uses **two** secret values
   (``MODAL_TOKEN_ID`` + ``MODAL_TOKEN_SECRET``) sent on every request as
   ``Modal-Key`` / ``Modal-Secret`` headers — not a single bearer token.
   OpenAI-compat's env-key chain doesn't speak that pattern, so we don't
   want users to pretend Modal is just another ``OPENAI_API_KEY`` style
   endpoint.

3. **Sensible default capability.** Modal-hosted vLLM is the dominant
   shape, so default to the ``modal`` ``BackendCapability`` (native
   tools + grammar) without the URL heuristic getting fooled.

4. **Friendly model spec.** A model string of the form
   ``modal:workspace/app[/function][@served-model]`` is parsed into a
   URL + an inner model name with zero extra config — drops in nicely
   with the auto-router.

Once the adapter has resolved a URL + headers + auth, everything
downstream — message encoding, tool-use path selection, SSE parsing,
chunk translation — is identical to ``OpenAICompatProvider``. We
*compose* one (rather than subclass) to keep the contract explicit and
to make the underlying client obvious in tests.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Iterable
from typing import Any

from ..capabilities import HOSTED_PROFILES, BackendCapability, ModelCapability
from ..events import StreamEvent
from ..types import Message
from .openai_compat import OpenAICompatProvider

__all__ = [
    "MODAL_HOST_SUFFIX",
    "ModalProvider",
    "ModalProviderError",
    "build_modal_url",
    "parse_modal_model_spec",
]


# Every Modal web function lives under this hostname. Hard-coded because
# Modal owns the domain — the day Modal renames it we'll have bigger
# problems than a string constant.
MODAL_HOST_SUFFIX = "modal.run"

# Slug rules per Modal's deploy CLI: lowercase alphanumeric, dashes,
# underscores. We don't enforce the full validator (Modal will reject
# anything they don't like) but we do reject obviously broken input —
# whitespace and the segment-separator chars we use in the URL.
_VALID_SLUG_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


class ModalProviderError(ValueError):
    """Raised when ``ModalProvider`` can't figure out where to send the call.

    Common causes: missing workspace/app combo, malformed model spec,
    Modal-Key without Modal-Secret (or vice versa). The message names the
    missing field so users fix it without spelunking the source.
    """


# ---------------------------------------------------------------------------
# URL + spec parsing
# ---------------------------------------------------------------------------


def build_modal_url(
    workspace: str,
    app: str,
    function: str | None = None,
    *,
    label: str | None = None,
    api_path: str = "/v1",
) -> str:
    """Assemble the public URL of a Modal web function.

    Pattern: ``https://{workspace}--{app}[-{function}][-{label}].modal.run``.
    The ``label`` is Modal's deploy label (e.g. ``-staging``); most users
    leave it unset and let Modal default to ``main``. ``api_path`` is
    appended unchanged — set it to ``""`` if your function mounts its
    OpenAI router at the root.

    Validates each segment so we surface mistakes early. A Modal URL that
    points at nothing produces a confusing 404 with no body — better to
    raise here than to bounce through HTTP.
    """

    for label_str, value in (
        ("workspace", workspace),
        ("app", app),
    ):
        if not value:
            raise ModalProviderError(f"Modal {label_str} required")
        if not _VALID_SLUG_RE.match(value):
            raise ModalProviderError(
                f"Modal {label_str} {value!r} is not a valid slug "
                "(letters, digits, dash, underscore)"
            )
    if function is not None and function != "":
        if not _VALID_SLUG_RE.match(function):
            raise ModalProviderError(
                f"Modal function {function!r} is not a valid slug"
            )
    if label is not None and label != "":
        if not _VALID_SLUG_RE.match(label):
            raise ModalProviderError(f"Modal label {label!r} is not a valid slug")

    parts = [app]
    if function:
        parts.append(function)
    if label:
        parts.append(label)
    host = f"{workspace}--{'-'.join(parts)}.{MODAL_HOST_SUFFIX}"
    if api_path and not api_path.startswith("/"):
        api_path = "/" + api_path
    # Tolerate a trailing slash on api_path — common copy-paste.
    if api_path.endswith("/") and api_path != "/":
        api_path = api_path[:-1]
    return f"https://{host}{api_path}"


def parse_modal_model_spec(spec: str) -> dict[str, str | None]:
    """Decompose a ``modal:`` model spec into its parts.

    Accepted shapes::

        modal:alice/my-llm                       # workspace, app
        modal:alice/my-llm/serve                 # + function name
        modal:alice/my-llm@meta-llama/Llama-3   # + served model name
        modal:alice/my-llm/serve@Qwen/Qwen2.5   # all three

    Returns ``{"workspace", "app", "function", "served_model"}`` — any
    field may be ``None`` if absent. Raises :class:`ModalProviderError`
    when the spec doesn't begin with ``modal:`` or when the
    workspace/app pair is missing.
    """

    if not spec.lower().startswith("modal:"):
        raise ModalProviderError(
            f"Modal model spec must start with 'modal:' (got {spec!r})"
        )
    body = spec[len("modal:"):]
    # @ splits inner model from path.
    served_model: str | None = None
    if "@" in body:
        body, served_model = body.split("@", 1)
        served_model = served_model.strip() or None
    segments = [s for s in body.split("/") if s]
    if len(segments) < 2:
        raise ModalProviderError(
            f"Modal model spec {spec!r} needs at least workspace/app "
            "(e.g. modal:alice/my-llm or modal:alice/my-llm/serve@Qwen/Q3)"
        )
    workspace = segments[0]
    app = segments[1]
    function = segments[2] if len(segments) >= 3 else None
    # Anything beyond function is ignored — Modal URLs don't allow deeper
    # nesting, but we don't want to be silently wrong about names that
    # happen to include slashes (caller likely meant to put them after @).
    return {
        "workspace": workspace,
        "app": app,
        "function": function,
        "served_model": served_model,
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ModalProvider:
    """Adapter for LLMs deployed on Modal.

    Wraps :class:`OpenAICompatProvider` (Modal-hosted vLLM / TGI / sglang
    all speak OpenAI-compat) and adds Modal-specific URL construction,
    token-pair auth, and the ``modal`` backend capability profile.

    Three construction shapes:

    1. ``ModalProvider(workspace="alice", app="my-llm", function="serve")``
       — assembles the URL for you.
    2. ``ModalProvider(base_url="https://alice--my-llm.modal.run/v1")``
       — for users who already know the URL.
    3. ``ModalProvider.from_model_spec("modal:alice/my-llm/serve")``
       — convenience for the auto-router; returns a provider whose
       ``inner_model`` reflects the ``@served-model`` part of the spec
       if any.

    Auth comes from constructor args or, falling back, the standard
    ``MODAL_TOKEN_ID`` + ``MODAL_TOKEN_SECRET`` env vars. Either both or
    neither — passing only one raises ``ModalProviderError`` so you don't
    deploy to prod with a half-configured proxy.
    """

    name = "modal"
    backend_capability: BackendCapability

    def __init__(
        self,
        *,
        base_url: str | None = None,
        workspace: str | None = None,
        app: str | None = None,
        function: str | None = None,
        label: str | None = None,
        api_path: str = "/v1",
        token_id: str | None = None,
        token_secret: str | None = None,
        default_headers: dict[str, str] | None = None,
        backend_capability: BackendCapability | None = None,
        model_capability: ModelCapability | None = None,
        inner_model: str | None = None,
    ) -> None:
        # Resolve URL: explicit base_url wins; else build from
        # workspace/app/function.
        if base_url:
            url = base_url
        else:
            if not workspace or not app:
                raise ModalProviderError(
                    "ModalProvider needs either base_url or (workspace, app[, function])"
                )
            url = build_modal_url(
                workspace=workspace,
                app=app,
                function=function,
                label=label,
                api_path=api_path,
            )

        # Token-pair auth. Either both or neither — never just one.
        tid = token_id if token_id is not None else os.environ.get("MODAL_TOKEN_ID")
        tsec = token_secret if token_secret is not None else os.environ.get("MODAL_TOKEN_SECRET")
        if (tid is None) != (tsec is None):
            raise ModalProviderError(
                "Modal proxy auth needs BOTH MODAL_TOKEN_ID and "
                "MODAL_TOKEN_SECRET (or both constructor args); got one without "
                "the other"
            )

        headers: dict[str, str] = {}
        if tid and tsec:
            headers["Modal-Key"] = tid
            headers["Modal-Secret"] = tsec
        if default_headers:
            headers.update(default_headers)

        # Compose an OpenAI-compat provider. We force the backend
        # capability to Modal's profile rather than letting the OpenAI
        # heuristic pick — Modal URLs don't match Together / Fireworks /
        # etc., so without this it would default to the generic vLLM
        # profile (which is fine but loses the ``modal`` provider hint).
        chosen_cap = backend_capability or HOSTED_PROFILES["modal"]
        self.backend_capability = chosen_cap

        self._inner = OpenAICompatProvider(
            base_url=url,
            # Modal doesn't use a Bearer token — pass api_key=None so the
            # env-key fallback chain doesn't accidentally attach an
            # unrelated OPENAI_API_KEY to a Modal request.
            api_key=None,
            default_headers=headers,
            backend_capability=chosen_cap,
            model_capability=model_capability,
        )
        # Owned URL + the optional served-model name selected at construct
        # time. ``inner_model`` lets ``from_model_spec`` carry the
        # ``@served-model`` part through so the auto-router can pass
        # something sensible as ``model=`` without the caller restating it.
        self.base_url = url
        self.inner_model = inner_model

    # ------------------------------------------------------------------
    # Construction shortcuts
    # ------------------------------------------------------------------

    @classmethod
    def from_model_spec(
        cls,
        spec: str,
        *,
        label: str | None = None,
        api_path: str = "/v1",
        token_id: str | None = None,
        token_secret: str | None = None,
        default_headers: dict[str, str] | None = None,
        backend_capability: BackendCapability | None = None,
        model_capability: ModelCapability | None = None,
    ) -> "ModalProvider":
        """Build a ``ModalProvider`` from a ``modal:workspace/app[/fn][@model]`` spec.

        Useful as the body of an auto-router branch: the caller checks
        ``spec.startswith("modal:")``, hands it here, and uses
        ``provider.inner_model`` as the model name on ``stream()``.
        """

        parts = parse_modal_model_spec(spec)
        return cls(
            workspace=parts["workspace"],  # type: ignore[arg-type]
            app=parts["app"],  # type: ignore[arg-type]
            function=parts["function"],
            label=label,
            api_path=api_path,
            token_id=token_id,
            token_secret=token_secret,
            default_headers=default_headers,
            backend_capability=backend_capability,
            model_capability=model_capability,
            inner_model=parts["served_model"],
        )

    # ------------------------------------------------------------------
    # Streaming — delegate to the inner OpenAI-compat provider
    # ------------------------------------------------------------------

    async def stream(
        self,
        *,
        model: str,
        messages: Iterable[Message],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
        temperature: float | None = None,
        extra: dict[str, Any] | None = None,
        model_capability: ModelCapability | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Forward a streaming chat-completion to the underlying Modal endpoint.

        If the caller didn't pass an explicit ``model`` but the provider
        was constructed with an ``inner_model`` (from a ``modal:`` spec
        with ``@served-name``), use that. Otherwise pass through whatever
        the caller sent — vLLM is fine accepting any model name your
        deployed image actually serves.
        """

        target_model = model or self.inner_model or ""
        if not target_model:
            raise ModalProviderError(
                "ModalProvider.stream needs a model name — pass model=... or "
                "construct via from_model_spec('modal:.../...@served-name')"
            )
        async for ev in self._inner.stream(
            model=target_model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            extra=extra,
            model_capability=model_capability,
        ):
            yield ev

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""

        await self._inner.aclose()

    # ------------------------------------------------------------------
    # Introspection helpers (tests + debug)
    # ------------------------------------------------------------------

    @property
    def inner_provider(self) -> OpenAICompatProvider:
        """Return the composed ``OpenAICompatProvider`` for inspection.

        Tests reach for this to look at the underlying ``httpx`` client's
        headers and base_url; callers rarely need it at runtime.
        """

        return self._inner
