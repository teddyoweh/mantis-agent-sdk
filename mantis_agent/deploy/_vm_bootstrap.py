"""Raw-VM bootstrap shared by marketplace/VM providers (Vast.ai today, Lambda later).

A "raw VM" provider gives us a box with a GPU and a Docker daemon (or an
image + args), nothing more. Everything above that — the vLLM/SGLang launch
line, the ``onstart`` shell script, the generated inference key and the
"is ``/v1/models`` answering yet?" poll — is provider-independent, so it
lives here and both adapters (and the Modal template) import it.

Nothing in this module talks to a provider API; the only network call is
:func:`wait_for_openai`, which polls the deployed endpoint itself.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import time
from typing import Any

import httpx

from . import _http
from ._http import slugify
from .base import DeployError, DeployOpts, Engine, NotSupported

__all__ = [
    "DEFAULT_SGLANG_IMAGE",
    "DEFAULT_VLLM_VERSION",
    "auth_env_name",
    "build_engine_args",
    "build_onstart_script",
    "build_vllm_command",
    "generate_api_key",
    "resolve_env_refs",
    "sglang_image",
    "slugify",
    "vllm_image",
    "wait_for_openai",
]

# The vLLM release the official Modal recipe pins (Sept 2026 survey). Both
# the Modal template (``uv_pip_install("vllm==<v>")``) and the Docker image
# tag (``vllm/vllm-openai:v<v>``) derive from it; ``DeployOpts.engine_version``
# overrides per deployment.
DEFAULT_VLLM_VERSION = "0.21.0"
DEFAULT_SGLANG_IMAGE = "lmsysorg/sglang:latest"

DEFAULT_PORT = 8000


# ---------------------------------------------------------------------------
# Names and secrets
# ---------------------------------------------------------------------------


# ``slugify`` is re-exported from ``_http`` so every adapter derives the same
# ``mantis-<slug>`` name from a model id.

def auth_env_name(slug: str) -> str:
    """The env var that holds a deployment's generated inference key:
    ``MANTIS_DEPLOY_<SLUG>_KEY``. Only the *name* is ever persisted."""

    return f"MANTIS_DEPLOY_{slug.upper().replace('-', '_')}_KEY"


def generate_api_key() -> str:
    """A fresh bearer key for ``--api-key``. Never logged, never written to
    disk — it goes into ``os.environ[auth_env]`` and to the provider."""

    return "sk-mantis-" + secrets.token_urlsafe(32)


def resolve_env_refs(headers: dict[str, str]) -> dict[str, str]:
    """Turn ``{"Modal-Key": "${MODAL_PROXY_TOKEN_ID}"}`` into real values
    from ``os.environ``. Headers whose env var is unset are dropped so a
    half-configured pair never sends an empty header."""

    out: dict[str, str] = {}
    for k, v in headers.items():
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", v or "")
        if m:
            val = os.environ.get(m.group(1))
            if val:
                out[k] = val
        elif v:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Engine launch lines
# ---------------------------------------------------------------------------


def vllm_image(opts: DeployOpts | None = None) -> str:
    """``vllm/vllm-openai:v<version>`` honouring ``opts.engine_version``."""

    version = (opts.engine_version if opts and opts.engine_version else DEFAULT_VLLM_VERSION)
    if version in ("latest", "nightly"):
        return f"vllm/vllm-openai:{version}"
    return f"vllm/vllm-openai:v{version.lstrip('v')}"


def sglang_image(opts: DeployOpts | None = None) -> str:
    if opts and opts.engine_version:
        return f"lmsysorg/sglang:{opts.engine_version}"
    return DEFAULT_SGLANG_IMAGE


def _served_name(model: str, opts: DeployOpts) -> str:
    return opts.served_model_name or model


def build_engine_args(
    model: str,
    engine: Engine,
    opts: DeployOpts,
    *,
    api_key: str | None = None,
    port: int = DEFAULT_PORT,
) -> list[str]:
    """The flags after the model for ``engine`` (no executable, no model).

    Shared by :func:`build_vllm_command` (``vllm serve <model> <flags>``),
    the Vast.ai ``args_str`` (``--model <model> <flags>``) and the Modal
    template. ``api_key`` becomes ``--api-key`` — pass ``None`` when the
    provider fronts the endpoint with its own auth.
    """

    tp = opts.tensor_parallel or 1
    served = _served_name(model, opts)
    if engine == "vllm":
        args = [
            "--served-model-name", served,
            "--host", "0.0.0.0",
            "--port", str(port),
            "--tensor-parallel-size", str(tp),
        ]
        if opts.max_model_len:
            args += ["--max-model-len", str(opts.max_model_len)]
        if opts.quantization:
            args += ["--quantization", opts.quantization]
        if opts.trust_remote_code:
            args.append("--trust-remote-code")
        if api_key:
            args += ["--api-key", api_key]
    elif engine == "sglang":
        args = [
            "--served-model-name", served,
            "--host", "0.0.0.0",
            "--port", str(port),
            "--tp", str(tp),
        ]
        if opts.max_model_len:
            args += ["--context-length", str(opts.max_model_len)]
        if opts.quantization:
            args += ["--quantization", opts.quantization]
        if opts.trust_remote_code:
            args.append("--trust-remote-code")
        if api_key:
            args += ["--api-key", api_key]
    else:
        raise NotSupported(
            f"engine {engine!r} is not supported on the raw-VM path",
            hint="use engine='vllm' or engine='sglang'",
        )
    args += list(opts.extra_engine_args or [])
    return args


def build_vllm_command(
    model: str,
    engine: Engine,
    opts: DeployOpts,
    *,
    api_key: str | None = None,
    port: int = DEFAULT_PORT,
) -> list[str]:
    """Full argv that starts the OpenAI server for ``engine``.

    * vllm   → ``vllm serve <model> --served-model-name … --api-key …``
    * sglang → ``python -m sglang.launch_server --model-path <model> …``

    ``tgi`` / ``llamacpp`` raise :class:`NotSupported`.
    """

    flags = build_engine_args(model, engine, opts, api_key=api_key, port=port)
    if engine == "vllm":
        return ["vllm", "serve", model, *flags]
    return ["python", "-m", "sglang.launch_server", "--model-path", model, *flags]


def build_onstart_script(
    model: str,
    engine: Engine,
    opts: DeployOpts,
    *,
    api_key: str | None = None,
    port: int = DEFAULT_PORT,
    hf_token_env: str = "HF_TOKEN",
    log_path: str = "/var/log/mantis-serve.log",
) -> str:
    """A bash script that launches the engine in the background.

    Runs as a VM/container ``onstart`` hook (Vast.ai) or over SSH/cloud-init
    (Lambda). It expects ``HF_TOKEN`` to already be in the environment — the
    caller plumbs it through the provider's env mechanism so the token never
    appears in the script text.
    """

    cmd = build_vllm_command(model, engine, opts, api_key=api_key, port=port)
    quoted = " ".join(shlex.quote(a) for a in cmd)
    return "\n".join(
        [
            "#!/bin/bash",
            "# generated by mantis-agent-sdk deploy — do not edit",
            "set -u",
            f'export {hf_token_env}="${{{hf_token_env}:-}}"',
            "export HF_HUB_ENABLE_HF_TRANSFER=1",
            f"echo 'mantis: starting {engine} on port {port}' >> {log_path}",
            f"nohup {quoted} >> {log_path} 2>&1 &",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

# ``_clock`` is the deadline source for every polling loop in the deploy
# adapters; sleeping goes through ``_http.sleep``. Tests monkeypatch
# ``_vm_bootstrap._clock`` and ``_http.sleep`` and cover both adapters at once.
_clock = time.monotonic

_RETRY_STATUSES = frozenset({502, 503, 504})


async def wait_for_openai(
    url: str,
    headers: dict[str, str] | None = None,
    timeout_s: int = 1200,
    *,
    provider: str | None = None,
    interval_s: float = 3.0,
    max_interval_s: float = 20.0,
) -> dict[str, Any]:
    """Poll ``GET {url}/models`` until it answers 200 and return its JSON.

    ``url`` is the OpenAI base **including** ``/v1``. Connection errors,
    timeouts and 502/503/504 (cold start, engine still loading weights)
    are retried with capped exponential backoff; 401/403 fail fast because
    waiting won't fix a wrong key.
    """

    base = url.rstrip("/")
    hdrs = resolve_env_refs(headers or {})
    deadline = _clock() + timeout_s
    delay = interval_s
    last = "no response yet"
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        while True:
            try:
                r = await client.get(f"{base}/models", headers=hdrs)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        return {"raw": r.text}
                if r.status_code in (401, 403):
                    raise DeployError(
                        f"endpoint rejected our credentials ({r.status_code}) at {base}/models",
                        hint="the inference key/headers stored for this deployment don't match "
                        "what the server expects — re-run deploy or fix the env var",
                        provider=provider,
                    )
                if r.status_code not in _RETRY_STATUSES and r.status_code != 404:
                    raise DeployError(
                        f"unexpected {r.status_code} from {base}/models: {r.text[:200]}",
                        provider=provider,
                    )
                last = f"HTTP {r.status_code}"
            if _clock() >= deadline:
                raise DeployError(
                    f"endpoint {base} not ready after {timeout_s}s ({last})",
                    hint="check the deployment logs — weights may still be downloading, "
                    "or the engine crashed on start (OOM → pick a bigger GPU or lower --max-model-len)",
                    provider=provider,
                )
            await _http.sleep(min(delay, max(0.0, deadline - _clock())))
            delay = min(delay * 1.6, max_interval_s)
