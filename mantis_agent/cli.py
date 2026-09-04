"""``mantis-agent`` — diagnostic + quick-chat CLI.

The CLI is *not* meant to be a TUI agent runner. It exists for:

* probing a backend (does it speak OpenAI-compat? does it advertise tool use?)
* listing the bundled model capability table
* one-shot ``run`` for quick smoke tests of (model, backend) pairs
* an interactive ``chat`` loop for poking at a server

We deliberately depend only on ``argparse`` (stdlib) — no ``click`` / ``typer``.
Cold-start matters for ``mantis-agent --help`` to feel snappy.

Subcommands::

    mantis-agent version
    mantis-agent list-models
    mantis-agent probe   --backend http://localhost:11434
    mantis-agent run     "prompt..."   --model qwen2.5-7b-instruct --backend http://...
    mantis-agent chat                  --model qwen2.5-7b-instruct --backend http://...

``run`` and ``chat`` go through :func:`mantis_agent.query`. ``probe`` opens
the backend's models endpoint, reads any well-known signal, and prints a
capability summary.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import anyio

from . import __version__
from .capabilities import (
    HOSTED_PROFILES,
    BackendCapability,
    hosted_profile_from_url,
    lookup_model,
    resolve_tool_use_path,
)
from .events import ContentBlockDelta, TextDelta
from .providers.base import detect_provider, resolve

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mantis-agent",
        description=(
            "mantis-agent-sdk CLI — diagnostics + quick chat. "
            "For library use, import mantis_agent in Python instead."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    sub.add_parser("version", help="Print the SDK version and exit.")

    p_setup = sub.add_parser(
        "setup-local",
        help=(
            "Get a CPU-runnable model installed and verified in <2 minutes. "
            "Installs Ollama if missing, pulls a curated CPU-friendly model, "
            "and runs a smoke test."
        ),
    )
    p_setup.add_argument(
        "--model",
        default=None,
        help=(
            "Ollama tag to install (e.g. qwen2.5:1.5b). "
            "Defaults to the curated recommendation."
        ),
    )
    p_setup.add_argument(
        "--list",
        action="store_true",
        dest="list_models",
        help="List the curated CPU-friendly models and exit.",
    )
    p_setup.add_argument(
        "--install-ollama",
        action="store_true",
        help="Install Ollama via the official script if it's not on PATH.",
    )
    p_setup.add_argument(
        "--skip-smoke-test",
        action="store_true",
        help="Skip the post-pull verification request.",
    )
    p_setup.add_argument(
        "--base-url",
        default="http://localhost:11434",
        help="Override the Ollama base URL.",
    )
    p_setup.add_argument(
        "--no-auto-start-server",
        action="store_false",
        dest="auto_start_server",
        default=True,
        help=(
            "Don't spawn `ollama serve` if the daemon isn't running. "
            "Default: setup-local starts it for you — the whole point is "
            "zero-to-model in one command."
        ),
    )
    p_setup.add_argument(
        "--start-timeout",
        type=float,
        default=15.0,
        dest="start_timeout_s",
        help=(
            "Seconds to wait for `ollama serve` to start answering. Default 15."
        ),
    )

    p_setup_llamacpp = sub.add_parser(
        "setup-local-llamacpp",
        help=(
            "Alternative to `setup-local` for users who prefer llama.cpp. "
            "Downloads a curated GGUF from HuggingFace, prints the exact "
            "`llama-server` command to start, and (if a server is already "
            "up) runs a smoke test."
        ),
    )
    p_setup_llamacpp.add_argument(
        "--model",
        default=None,
        help=(
            "Curated GGUF tag (e.g. qwen2.5-1.5b-instruct-q4_k_m). "
            "Defaults to the recommended pick; use --list to see all options."
        ),
    )
    p_setup_llamacpp.add_argument(
        "--list",
        action="store_true",
        dest="list_models",
        help="List curated GGUF models and exit.",
    )
    p_setup_llamacpp.add_argument(
        "--models-dir",
        default=None,
        help=(
            "Directory to download GGUFs into. Defaults to "
            "$MANTIS_AGENT_MODELS_DIR or the platform XDG data dir."
        ),
    )
    p_setup_llamacpp.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port the llama.cpp server runs on (smoke-test target). Default 8080.",
    )
    p_setup_llamacpp.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host the llama.cpp server binds to. Default loopback for safety.",
    )
    p_setup_llamacpp.add_argument(
        "--skip-download",
        action="store_true",
        help="Don't download anything — just emit the launch command + smoke test.",
    )
    p_setup_llamacpp.add_argument(
        "--skip-smoke-test",
        action="store_true",
        help="Skip the post-download verification request.",
    )

    p_list = sub.add_parser(
        "list-models",
        help=(
            "List models. With --backend, probes the live server (Ollama "
            "/api/tags, OpenAI-compat /v1/models, etc.) and annotates each "
            "model with capability info. Without --backend, prints the "
            "bundled 30-model capability table."
        ),
    )
    p_list.add_argument(
        "--backend",
        default=None,
        help="Backend base URL (e.g. http://localhost:11434, https://api.together.xyz/v1).",
    )
    p_list.add_argument(
        "--api-key",
        default=None,
        help="Override API key (else uses env: TOGETHER_API_KEY, FIREWORKS_API_KEY, …).",
    )
    p_list.add_argument(
        "--all",
        action="store_true",
        help="With --backend, also append the bundled table for reference.",
    )

    p_probe = sub.add_parser(
        "probe", help="Hit a backend URL and report what we can detect."
    )
    p_probe.add_argument("--backend", required=True, help="Backend base URL.")

    p_run = sub.add_parser(
        "run", help="One-shot: send a prompt, print the final assistant response."
    )
    p_run.add_argument("prompt", help="The user prompt to send. Use '-' to read it from stdin (e.g. cat spec.md | mantis run --tools -).")
    _add_agent_flags(p_run)
    p_run.add_argument(
        "--output-format", choices=["text", "json"], default="text",
        help="text (default): print the assistant's reply. json: print one JSON "
             "object with result, is_error, num_turns, total_cost_usd, usage, "
             "session_id — for scripting / CI.",
    )
    p_run.add_argument(
        "--json", action="store_const", const="json", dest="output_format",
        help="Shorthand for --output-format json.",
    )

    p_chat = sub.add_parser(
        "chat", help="Interactive stdin chat loop. Streams tokens as they arrive."
    )
    _add_agent_flags(p_chat)
    p_chat.add_argument(
        "--system", default=None, help="System prompt for the chat session."
    )

    _add_deploy_parser(sub)

    return p


def _add_deploy_parser(sub: Any) -> None:
    """``mantis-agent deploy ...`` — bring-your-own GPU provider. Everything
    heavy (httpx, the provider adapters) is imported inside the handler so
    ``--help`` stays stdlib-only."""

    p_dep = sub.add_parser(
        "deploy",
        help="Deploy any open-weight model on your own GPU-cloud account as an "
             "OpenAI-compatible endpoint (RunPod, HF Endpoints, Modal, DeepInfra, "
             "Baseten, Vast.ai) and connect mantis to it.",
    )
    dsub = p_dep.add_subparsers(dest="deploy_cmd", required=True, metavar="ACTION")

    def _json(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    q = dsub.add_parser("providers", help="List deploy providers and whether each is configured.")
    _json(q)

    q = dsub.add_parser("creds", help="Show or save a provider's credentials (validated over the network).")
    q.add_argument("provider")
    q.add_argument("--set", dest="set_values", action="append", default=[], metavar="ENV=value",
                   help="Save a credential, e.g. --set RUNPOD_API_KEY=... (repeatable).")
    _json(q)

    q = dsub.add_parser("gpus", help="A provider's GPU catalogue with prices, cheapest first.")
    q.add_argument("provider")
    q.add_argument("--min-vram", type=int, default=None, metavar="GB", help="Only GPUs with at least this much total VRAM.")
    _json(q)

    q = dsub.add_parser("models", help="Search the Hugging Face Hub (curated list when no query).")
    q.add_argument("query", nargs="?", default="")
    q.add_argument("--sort", choices=["trending", "downloads", "likes"], default="trending")
    q.add_argument("--limit", type=int, default=25)
    _json(q)

    q = dsub.add_parser("inspect", help="Pre-flight one model: params, dtype, gated, vLLM support, VRAM estimate.")
    q.add_argument("model", help="HF id (org/name) or ollama:<tag>.")
    _json(q)

    q = dsub.add_parser("up", help="Deploy a model on a provider.")
    q.add_argument("provider")
    q.add_argument("model")
    q.add_argument("--gpu", required=True, help="Provider GPU id from `deploy gpus`.")
    q.add_argument("--engine", choices=["vllm", "sglang", "tgi", "llamacpp"], default="vllm")
    q.add_argument("--max-model-len", type=int, default=None)
    q.add_argument("--tp", type=int, default=None, help="Tensor parallel size (defaults to GPU count).")
    q.add_argument("--min", type=int, default=0, help="Min replicas (0 = scale to zero).")
    q.add_argument("--max", type=int, default=1, help="Max replicas.")
    q.add_argument("--idle", type=int, default=300, help="Idle seconds before scaling down.")
    q.add_argument("--name", default=None)
    q.add_argument("--served-name", default=None, help="Override what the endpoint answers to as `model=`.")
    q.add_argument("--quantization", default=None)
    q.add_argument("--trust-remote-code", action="store_true")
    q.add_argument("--hf-token", default=None, help="For gated repos (else $HF_TOKEN).")
    q.add_argument("--no-wait", action="store_true", help="Return as soon as the provider accepts the deployment.")
    q.add_argument("--force", action="store_true", help="Deploy even if pre-flight says it won't fit.")
    q.add_argument("--no-connect", action="store_true", help="Don't make it the current model once ready.")
    _json(q)

    q = dsub.add_parser("ls", help="Stored deployments.")
    q.add_argument("--refresh", action="store_true", help="Re-query every provider (adopts endpoints made elsewhere).")
    q.add_argument("--provider", default=None)
    _json(q)

    q = dsub.add_parser("status", help="One deployment, refreshed from the provider.")
    q.add_argument("id")
    q.add_argument("--no-refresh", action="store_true")
    _json(q)

    q = dsub.add_parser("logs", help="Recent log lines.")
    q.add_argument("id")
    q.add_argument("--tail", type=int, default=200)
    _json(q)

    q = dsub.add_parser("connect", help="Verify the endpoint answers and make it the current model.")
    q.add_argument("id")
    _json(q)

    q = dsub.add_parser("down", help="Delete a deployment on the provider.")
    q.add_argument("id")
    q.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt.")
    _json(q)


def _add_agent_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--model", required=True, help="Model slug (e.g. qwen2.5-7b-instruct)."
    )
    p.add_argument(
        "--backend",
        default=None,
        help="Backend base URL. Defaults to env-detected.",
    )
    p.add_argument("--api-key", default=None, help="Override API key (else env).")
    p.add_argument(
        "--max-tokens", type=int, default=1024, help="Max tokens per assistant turn."
    )
    p.add_argument("--temperature", type=float, default=None, help="Sampling temp.")
    p.add_argument(
        "--effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        default=None,
        help="Model reasoning effort. GPT-5.6 chat supports xhigh; max is Responses-only.",
    )
    p.add_argument(
        "--verbosity",
        choices=("low", "medium", "high"),
        default=None,
        help="GPT-5 verbosity control.",
    )
    p.add_argument(
        "--reasoning-mode",
        choices=("standard", "pro"),
        default=None,
        help="Reasoning mode hint; GPT-5.6 pro mode requires Responses API support.",
    )
    p.add_argument(
        "--max-turns",
        type=int,
        default=10,
        help="Maximum agent turns before stopping (default 10).",
    )
    p.add_argument(
        "--tools",
        action="store_true",
        help="Give the agent the coding tools (read/write/edit/bash/grep/glob/"
             "lsp/web) so a one-shot run can actually DO the work, not just chat. "
             "Non-dangerous tools run automatically (no prompt); dangerous shell "
             "commands are refused in this headless mode.",
    )
    p.add_argument(
        "--dangerously-skip-permissions",
        "--yes",
        dest="skip_permissions",
        action="store_true",
        help="Run EVERY tool without asking — including dangerous shell commands. "
             "There's no interactive approval in a headless run, so use this only "
             "for trusted automation/CI where the agent is allowed full autonomy.",
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"mantis-agent-sdk {__version__}")
    return 0


def _cmd_list_models(args: argparse.Namespace) -> int:
    """List models. With ``--backend``, queries the live server; without,
    prints the bundled capability table."""

    backend = getattr(args, "backend", None)
    if backend:
        return _list_backend_models(backend, args)

    return _list_bundled_table()


def _list_bundled_table() -> int:
    from .capabilities import _TABLE  # noqa: PLC0415 — intentional lazy import

    name_w = max(len(k) for k in _TABLE) + 2
    fmt = f"{{name:<{name_w}}} {{family:<10}} {{ctx:>8}} {{tools:^7}} {{think:^7}}"
    print(fmt.format(name="MODEL", family="FAMILY", ctx="CTX", tools="TOOLS", think="THINK"))
    print("-" * (name_w + 36))
    for key, cap in sorted(_TABLE.items()):
        print(
            fmt.format(
                name=key,
                family=cap.family,
                ctx=str(cap.context_window),
                tools="yes" if cap.supports_native_tools else "no",
                think="yes" if cap.emits_inline_thinking else "no",
            )
        )
    return 0


def _list_backend_models(backend: str, args: argparse.Namespace) -> int:
    """Probe a live backend and print every model it advertises.

    Routes by ``detect_provider``:
      * ``ollama``        → ``GET {base}/api/tags``
      * ``openai_compat`` → ``GET {base}/v1/models`` (with optional auth) —
        OpenAI, Gemini (generativelanguage.googleapis.com), xAI (api.x.ai),
        Together, Fireworks, Groq, … all speak this
      * ``anthropic_passthrough`` → ``GET {base}/v1/models`` with
        ``x-api-key`` + ``anthropic-version`` (``--backend anthropic`` works)
      * ``llamacpp``      → ``GET {base}/v1/models``
      * ``tgi``           → ``GET {base}/info`` (single-model server)

    Each row is annotated with what our capability table knows about it,
    so users see size/context/tool-support next to the live name.
    """

    import httpx  # noqa: PLC0415

    from .capabilities import lookup_model  # noqa: PLC0415

    kind = detect_provider(backend)
    base = backend.rstrip("/")
    api_key = args.api_key or _resolve_api_key(backend)

    # Build the live-models call per backend kind.
    models: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=10.0) as c:
            if kind == "ollama":
                r = c.get(f"{base}/api/tags")
                r.raise_for_status()
                models = [
                    {
                        "name": m.get("name", ""),
                        "size_gb": round((m.get("size") or 0) / 1e9, 2),
                        "family": (m.get("details") or {}).get("family", ""),
                        "params": (m.get("details") or {}).get("parameter_size", ""),
                        "quant": (m.get("details") or {}).get("quantization_level", ""),
                    }
                    for m in r.json().get("models", [])
                ]
            elif kind == "anthropic_passthrough":
                # Anthropic: x-api-key + anthropic-version (or a Bearer OAuth
                # token). ``--backend anthropic`` (the sentinel) is accepted.
                r = c.get(_anthropic_models_url(backend),
                          headers=_anthropic_probe_headers(api_key))
                r.raise_for_status()
                models = [
                    {"name": m.get("id", ""),
                     "owned_by": m.get("display_name", "anthropic")}
                    for m in r.json().get("data", [])
                ]
            elif kind == "tgi":
                r = c.get(f"{base}/info")
                r.raise_for_status()
                info = r.json()
                models = [
                    {
                        "name": info.get("model_id", "<tgi-served>"),
                        "params": "",
                        "quant": "",
                    }
                ]
            else:  # openai_compat, llamacpp (with --jinja)
                headers = (
                    {"Authorization": f"Bearer {api_key}"} if api_key else {}
                )
                # /v1/models lives under the base url; strip a trailing /v1 if user gave one.
                models_url = f"{base.removesuffix('/v1')}/v1/models"
                r = c.get(models_url, headers=headers)
                r.raise_for_status()
                data = r.json()
                models = [
                    {"name": m.get("id", ""), "owned_by": m.get("owned_by", "")}
                    for m in data.get("data", [])
                ]
    except httpx.HTTPError as e:
        print(f"error probing backend {base!r}: {e!r}")
        return 1

    if not models:
        print(f"{base}: no models reported")
        return 0

    # Pretty print: live name + size/params + our capability lookup
    name_w = max(len(str(m.get("name", ""))) for m in models) + 2
    fmt = (
        f"{{name:<{name_w}}} {{size:>10}}  {{params:>10}}  "
        "{tools:^7} {ctx:>8}"
    )
    print(f"{base}  ({kind}, {len(models)} model{'s' if len(models) != 1 else ''})")
    print(fmt.format(name="MODEL", size="SIZE", params="PARAMS", tools="TOOLS", ctx="CTX"))
    print("-" * (name_w + 44))
    for m in models:
        name = m.get("name", "")
        cap = lookup_model(name)
        size = (
            f"{m['size_gb']} GB" if m.get("size_gb") else ""
        )
        params = m.get("params") or m.get("owned_by", "")
        print(
            fmt.format(
                name=name,
                size=size,
                params=params,
                tools="yes" if cap.supports_native_tools else "no",
                ctx=str(cap.context_window),
            )
        )

    if getattr(args, "all", False):
        print()
        print("=== bundled capability table ===")
        _list_bundled_table()

    return 0


def _resolve_api_key(backend: str | None = None) -> str | None:
    """Best-effort: the provider key for ``backend`` from the environment.

    Host-aware — an ``api.anthropic.com`` (or ``anthropic`` sentinel) backend
    reads ``ANTHROPIC_API_KEY``, ``api.x.ai`` reads ``XAI_API_KEY`` /
    ``GROK_API_KEY``, Google reads ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` —
    before the generic chain, so a stale ``OPENAI_API_KEY`` in the shell can't
    outrank the vendor's own key. ``MANTIS_AGENT_API_KEY`` always wins.
    """

    import os  # noqa: PLC0415

    explicit = os.environ.get("MANTIS_AGENT_API_KEY")
    if explicit:
        return explicit
    if backend and detect_provider(backend) == "anthropic_passthrough":
        return os.environ.get("ANTHROPIC_API_KEY") or None
    from .providers.openai_compat import env_key_candidates  # noqa: PLC0415

    for var in env_key_candidates(backend):
        v = os.environ.get(var)
        if v:
            return v
    return None


def _anthropic_probe_headers(api_key: str | None) -> dict[str, str]:
    """Anthropic's ``/v1/models`` wants ``x-api-key`` + ``anthropic-version``
    (an OAuth/gateway token goes in ``Authorization: Bearer`` with the oauth
    beta instead)."""

    import os  # noqa: PLC0415

    token = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if token and not api_key:
        return {
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        }
    headers = {"anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _anthropic_models_url(backend: str) -> str:
    from .providers.anthropic_passthrough import (  # noqa: PLC0415
        ANTHROPIC_DEFAULT_BASE_URL,
        _normalize_base_url,
    )

    base = backend if backend.lower().startswith(("http://", "https://")) else ANTHROPIC_DEFAULT_BASE_URL
    return f"{_normalize_base_url(base)}/models"


def _cmd_probe(args: argparse.Namespace) -> int:
    url = args.backend
    print(f"backend url: {url}")

    name = detect_provider(url)
    print(f"detected provider: {name}")

    if name == "anthropic_passthrough":
        # Not a URL-matched profile when the sentinel was given, but the
        # adapter's profile is fixed — report it rather than "<none>".
        from .capabilities import HOSTED_PROFILES  # noqa: PLC0415
        _print_profile("matched hosted profile", HOSTED_PROFILES["anthropic"])
    else:
        profile = hosted_profile_from_url(url)
        if profile is not None:
            _print_profile("matched hosted profile", profile)
        else:
            print("matched hosted profile: <none — using adapter default>")

    # Live probe — only if the backend looks reachable. We don't want to hang
    # on unreachable URLs; httpx.get with a short timeout suffices.
    try:
        import httpx  # noqa: PLC0415

        api_key = getattr(args, "api_key", None) or _resolve_api_key(url)
        if name == "anthropic_passthrough":
            # Anthropic needs its own headers even to answer /v1/models; the
            # generic sweep below would only ever see a 401.
            try:
                with httpx.Client(timeout=4.0) as c:
                    r = c.get(_anthropic_models_url(url),
                              headers=_anthropic_probe_headers(api_key))
                if r.status_code < 500:
                    print(f"reachable: /v1/models -> HTTP {r.status_code}"
                          + ("" if api_key or r.status_code < 400
                             else "  (set ANTHROPIC_API_KEY to authenticate)"))
                else:
                    print(f"reachable: /v1/models -> HTTP {r.status_code}")
            except httpx.HTTPError:
                print("reachable: <api.anthropic.com did not respond under 4s>")
            return 0

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # Most servers expose /v1/models or /api/tags. Hosted OpenAI-compat
        # endpoints (OpenAI, Gemini, xAI) publish their base WITH the /v1
        # suffix, so probe ``/models`` relative to it too.
        base = url.rstrip("/")
        candidate_urls = (
            f"{base}/models",
            f"{base.removesuffix('/v1')}/v1/models",
            f"{base}/api/tags",
            f"{base}/api/version",
        )
        seen: set[str] = set()
        for candidate in candidate_urls:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                with httpx.Client(timeout=2.0) as c:
                    r = c.get(candidate, headers=headers)
                if r.status_code < 500:
                    path = candidate[len(base):] if candidate.startswith(base) else candidate
                    print(f"reachable: {path or candidate} -> HTTP {r.status_code}")
                    break
            except httpx.HTTPError:
                continue
        else:
            print("reachable: <no well-known endpoint responded under 2s>")
    except ImportError:  # pragma: no cover — httpx is a hard dep
        print("reachable: <httpx not importable, skipping live probe>")

    return 0


def _print_profile(label: str, profile: BackendCapability) -> None:
    print(f"{label}: {profile.provider_hint or profile.kind}")
    print(f"  native tools:  {'yes' if profile.supports_native_tools else 'no'}")
    print(f"  grammar:       {'yes' if profile.supports_grammar else 'no'}")
    print(f"  logprobs:      {'yes' if profile.supports_logprobs else 'no'}")
    print(f"  prefix cache:  {'yes' if profile.supports_prefix_caching else 'no'}")


def _resolve_prompt(prompt: str, read_stdin: Any) -> str:
    """When the prompt is ``-``, read it from stdin so a file/spec can be piped:
    ``cat feature.md | mantis run --tools -``. Otherwise return it verbatim."""
    if prompt.strip() == "-":
        return read_stdin().strip()
    return prompt


def _result_to_json(result_msg: Any, fallback_text: str = "") -> dict[str, Any]:
    """Serialize an ``SDKResultMessage`` to a JSON-ready dict for ``--json``. Falls
    back to the accumulated assistant text when the result field is empty."""
    import msgspec  # noqa: PLC0415

    obj = msgspec.to_builtins(result_msg)
    if not obj.get("result"):
        obj["result"] = fallback_text
    return obj


async def _cmd_run_async(args: argparse.Namespace) -> int:
    # Imported here so the CLI cold-start path (e.g. ``mantis-agent version``)
    # doesn't pay for the query module's transitive imports.
    from .query import (  # noqa: PLC0415
        SDKAssistantMessage,
        SDKResultMessage,
        query,
    )

    options = _build_options(args)
    prompt = _resolve_prompt(args.prompt, sys.stdin.read)
    if not prompt.strip():
        print("[error] empty prompt (nothing on stdin?)", file=sys.stderr)
        return 1
    json_mode = getattr(args, "output_format", "text") == "json"
    collected: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, SDKAssistantMessage):
            # SDKAssistantMessage wraps APIAssistantMessage under `.message`,
            # which carries the list of content blocks (TextBlock, ToolUseBlock,
            # ThinkingBlock, …). `content_blocks` doesn't exist on the struct —
            # going through `.message.content` is the right path.
            content = msg.message.content
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                text = getattr(block, "text", None)
                if text:
                    if json_mode:
                        collected.append(text)  # hold for the final JSON object
                    else:
                        print(text)
        elif isinstance(msg, SDKResultMessage):
            if json_mode:
                import json as _json  # noqa: PLC0415
                print(_json.dumps(_result_to_json(msg, "\n".join(collected)), default=str))
                return 1 if msg.is_error else 0
            if msg.is_error:
                print(f"[error] {msg.result}", file=sys.stderr)
                return 1
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    return anyio.run(_cmd_run_async, args)


async def _cmd_chat_async(args: argparse.Namespace) -> int:
    """Interactive REPL. Token-level streaming via ``Agent.stream``."""

    from .agent import Agent  # noqa: PLC0415
    from .tools import ToolRegistry  # noqa: PLC0415
    from .types import (  # noqa: PLC0415
        AssistantMessage,
        UserMessage,
    )

    provider = _build_provider_for_args(args)
    agent = Agent(
        model=args.model,
        provider=provider,
        system=args.system,
        tools=ToolRegistry(),
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        max_steps=args.max_turns,
        extra={
            k: v for k, v in {
                "effort": getattr(args, "effort", None),
                "verbosity": getattr(args, "verbosity", None),
                "reasoning_mode": getattr(args, "reasoning_mode", None),
            }.items() if v is not None
        },
    )

    print(f"mantis-agent chat — model={args.model} backend={args.backend or '<default>'}")
    print("type /exit to quit, /reset to clear history, blank line to send")
    messages: list[Any] = []
    try:
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line == "/exit":
                break
            if line == "/reset":
                messages = []
                print("(history cleared)")
                continue

            messages.append(UserMessage(content=line))
            print("agent> ", end="", flush=True)

            # Token-level stream. Build the final assistant message in parallel
            # so we can append it to history and loop.
            collected: list[str] = []
            async for ev in agent.stream(messages):
                if isinstance(ev, ContentBlockDelta) and isinstance(ev.delta, TextDelta):
                    print(ev.delta.text, end="", flush=True)
                    collected.append(ev.delta.text)
            print()
            # Naive append — we lose tool_use blocks here. The interactive REPL
            # is for smoke testing; complex tool loops should use run() in code.
            messages.append(
                AssistantMessage(content=[_text_block("".join(collected))])
            )
    finally:
        await agent.aclose()
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    return anyio.run(_cmd_chat_async, args)


def _text_block(text: str) -> Any:
    from .types import TextBlock  # noqa: PLC0415

    return TextBlock(text=text)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_options(args: argparse.Namespace) -> dict[str, Any]:
    """Turn argparse output into a ``query()`` options dict."""

    out: dict[str, Any] = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "max_turns": args.max_turns,
    }
    if args.backend:
        out["backend"] = args.backend
    if args.temperature is not None:
        out["temperature"] = args.temperature
    extra: dict[str, Any] = {}
    if getattr(args, "effort", None):
        extra["effort"] = args.effort
    if getattr(args, "verbosity", None):
        extra["verbosity"] = args.verbosity
    if getattr(args, "reasoning_mode", None):
        extra["reasoning_mode"] = args.reasoning_mode
    if extra:
        out["extra"] = extra
    if args.api_key:
        out["api_key"] = args.api_key
    if getattr(args, "tools", False):
        from .builtin_tools import CODING_TOOLS, web_fetch, web_search  # noqa: PLC0415
        from .builtin_tools.codenav import lsp  # noqa: PLC0415
        out["tools"] = [*CODING_TOOLS, web_search, web_fetch, lsp]
    if getattr(args, "skip_permissions", False):
        out["permission_mode"] = "bypass"  # full autonomy — trusted automation
    return out


def _build_provider_for_args(args: argparse.Namespace) -> Any:
    """Provider constructor for ``chat`` — same logic as ``query._build_provider``
    but inlined so chat can stream without going through ``query()``."""

    name = detect_provider(args.backend or args.model)
    factory = resolve(name)
    kwargs: dict[str, Any] = {}
    if args.backend and args.backend.startswith(("http://", "https://")):
        kwargs["base_url"] = args.backend
    if args.api_key:
        kwargs["api_key"] = args.api_key
    try:
        return factory(**kwargs)
    except TypeError:
        return factory()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _cmd_setup_local(args: argparse.Namespace) -> int:
    from .setup_local import print_model_table, run_setup_local  # noqa: PLC0415

    if getattr(args, "list_models", False):
        print_model_table()
        return 0
    return run_setup_local(
        model=args.model,
        install_ollama_if_missing=args.install_ollama,
        skip_smoke_test=args.skip_smoke_test,
        base_url=args.base_url,
        auto_start_server=getattr(args, "auto_start_server", True),
        start_timeout_s=getattr(args, "start_timeout_s", 15.0),
    )


def _cmd_setup_local_llamacpp(args: argparse.Namespace) -> int:
    from .setup_local_llamacpp import (  # noqa: PLC0415
        print_gguf_model_table,
        run_setup_local_llamacpp,
    )

    if getattr(args, "list_models", False):
        print_gguf_model_table()
        return 0
    return run_setup_local_llamacpp(
        model=args.model,
        models_dir=args.models_dir,
        port=args.port,
        host=args.host,
        skip_download=args.skip_download,
        skip_smoke_test=args.skip_smoke_test,
    )


# ---------------------------------------------------------------------------
# deploy — bring-your-own GPU provider
# ---------------------------------------------------------------------------


def _deploy_json(obj: Any) -> Any:
    """Dataclasses / datetimes / nested containers → JSON-ready."""

    import dataclasses  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _deploy_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _deploy_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deploy_json(v) for v in obj]
    return obj


def _deploy_table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in cells:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    fmt = "  ".join("{:<%d}" % w for w in widths)
    lines = [fmt.format(*headers).rstrip(), fmt.format(*("-" * w for w in widths)).rstrip()]
    lines += [fmt.format(*r).rstrip() for r in cells]
    return "\n".join(lines)


def _deploy_launch_line(model: str, backend: str, api_key_env: str | None) -> str:
    key = f" MANTIS_AGENT_API_KEY=${api_key_env}" if api_key_env else ""
    return f"MANTIS_AGENT_MODEL={model} MANTIS_AGENT_BASE_URL={backend}{key} mantis"


def _fmt_price(p: Any) -> str:
    return f"${p:.2f}/h" if isinstance(p, (int, float)) else "-"


def _cmd_deploy(args: argparse.Namespace) -> int:
    import json as _json  # noqa: PLC0415

    from .deploy import DeployError, NotSupported  # noqa: PLC0415

    want_json = bool(getattr(args, "json", False))

    def out_json(payload: Any) -> int:
        print(_json.dumps(_deploy_json(payload), indent=2, default=str))
        return 0

    try:
        return _deploy_dispatch(args, want_json, out_json)
    except NotSupported as e:
        if want_json:
            out_json({"ok": False, "supported": False, "error": str(e), "hint": e.hint})
        else:
            print(f"not supported: {e}", file=sys.stderr)
            if e.hint:
                print(f"hint: {e.hint}", file=sys.stderr)
        return 2
    except DeployError as e:
        if want_json:
            out_json({"ok": False, "error": str(e), "hint": e.hint, "provider": e.provider})
        else:
            print(f"error: {e}", file=sys.stderr)
            if e.hint:
                print(f"hint: {e.hint}", file=sys.stderr)
        return 1


def _deploy_dispatch(args: argparse.Namespace, want_json: bool, out_json: Any) -> int:
    from .deploy import DeployOpts, manager  # noqa: PLC0415

    cmd = args.deploy_cmd

    def progress(line: str) -> None:
        if not want_json:
            print(f"  · {line}", flush=True)

    if cmd == "providers":
        provs = anyio.run(manager.providers)
        if want_json:
            return out_json({"ok": True, "providers": provs})
        rows = [[p["id"], p["display_name"], "yes" if p["configured"] else "no",
                 ",".join(p["engines"]), "yes" if p["scale_to_zero"] else "no",
                 " ".join(f.env for f in p["credential_fields"])] for p in provs]
        print(_deploy_table(["id", "provider", "configured", "engines", "scale-to-0", "credentials"], rows))
        print("\nSave a key:  mantis-agent deploy creds <id> --set ENV=value")
        return 0

    if cmd == "creds":
        values: dict[str, str] = {}
        for item in args.set_values:
            k, sep, v = item.partition("=")
            if not sep or not k.strip():
                raise _deploy_usage("--set expects ENV=value")
            values[k.strip()] = v
        if values:
            acct = anyio.run(manager.save_credentials, args.provider, values)
        else:
            acct = anyio.run(manager.validate, args.provider)
        provs = {p["id"]: p for p in anyio.run(manager.providers)}
        p = provs.get(args.provider) or {}
        payload = {"ok": acct.ok, "account": acct, "configured": bool(p.get("configured")),
                   "fields": p.get("credential_fields", [])}
        if want_json:
            return out_json(payload)
        for f in p.get("credential_fields", []):
            state = "set" if os.environ.get(f.env) else "missing"
            print(f"  {f.env:<24} {state:<8} {f.help}")
        print(("ok: " if acct.ok else "not ok: ") + (acct.message or "validated")
              + (f" (user {acct.user})" if acct.user else ""))
        return 0 if acct.ok else 1

    if cmd == "gpus":
        rows_g = anyio.run(lambda: manager.gpus(args.provider, min_vram_gb=args.min_vram))
        if want_json:
            return out_json({"ok": True, "provider": args.provider, "gpus": rows_g})
        print(_deploy_table(
            ["id", "family", "vram", "price", "avail", "label"],
            [[g.provider_id, g.family, f"{g.total_vram_gb} GB" + (f" ({g.count}x)" if g.count > 1 else ""),
              _fmt_price(g.price_per_hour), {True: "yes", False: "no"}.get(g.available, "?"), g.label]
             for g in rows_g]))
        return 0

    if cmd == "models":
        infos = anyio.run(lambda: manager.search_models(args.query, limit=args.limit, sort=args.sort))
        if want_json:
            return out_json({"ok": True, "models": infos})
        print(_deploy_table(
            ["model", "params", "dtype", "gated", "vllm", "~vram", "downloads"],
            [[m.id, f"{m.params_b:g}B" if m.params_b else "?", m.dtype or "?", "yes" if m.gated else "",
              {True: "yes", False: "no"}.get(m.vllm_ok, "?"),
              f"{m.est_vram_gb:g} GB" if m.est_vram_gb else "?", m.downloads or ""] for m in infos]))
        return 0

    if cmd == "inspect":
        info = anyio.run(lambda: manager.inspect_model(args.model))
        if want_json:
            return out_json({"ok": True, "model": info})
        print(f"{info.id}")
        print(f"  architectures : {', '.join(info.architectures) or '?'}")
        print(f"  params        : {f'{info.params_b:g}B' if info.params_b else '?'}  dtype: {info.dtype or '?'}")
        print(f"  context       : {info.context_len or '?'}   license: {info.license or '?'}   gated: {'yes' if info.gated else 'no'}")
        vllm_txt = {True: "yes", False: "no"}.get(info.vllm_ok, "unknown")
        print(f"  vLLM          : {vllm_txt}" + (f" — {info.reason}" if info.reason else ""))
        print(f"  est. VRAM     : {f'{info.est_vram_gb:g} GB' if info.est_vram_gb else '?'} (weights + KV cache)")
        return 0

    if cmd == "up":
        opts = DeployOpts(
            name=args.name, hf_token=args.hf_token, served_model_name=args.served_name,
            max_model_len=args.max_model_len, tensor_parallel=args.tp, quantization=args.quantization,
            min_replicas=args.min, max_replicas=args.max, idle_timeout_s=args.idle,
            trust_remote_code=args.trust_remote_code, extra={"force": bool(args.force)},
        )
        dep = anyio.run(lambda: manager.deploy(
            args.provider, args.model, gpu=args.gpu, engine=args.engine, opts=opts,
            wait=not args.no_wait, progress=progress))
        connected = None
        if not args.no_wait and not args.no_connect and dep.endpoint_url:
            connected = anyio.run(lambda: manager.connect(dep.id))
        if want_json:
            return out_json({"ok": True, "deployment": dep, "connect": connected})
        print(f"\n{dep.provider}:{dep.id}  {dep.status}  {dep.model}")
        if dep.endpoint_url:
            print(f"  endpoint : {dep.endpoint_url}")
            print(f"  model=   : {dep.served_model_name}")
        if connected:
            print("\nConnected. Launch the terminal on it with:\n  "
                  + _deploy_launch_line(connected["model"], connected["backend"], connected.get("api_key_env")))
        elif args.no_wait:
            print(f"\nWhen it is up:  mantis-agent deploy connect {dep.id}")
        return 0

    if cmd == "ls":
        deps = anyio.run(lambda: manager.list_deployments(refresh=args.refresh, provider_id=args.provider))
        if want_json:
            return out_json({"ok": True, "deployments": deps})
        if not deps:
            print("no deployments (mantis-agent deploy up <provider> <model> --gpu <id>)")
            return 0
        print(_deploy_table(
            ["id", "provider", "model", "status", "gpu", "endpoint"],
            [[d.id, d.provider, d.model, d.status, d.gpu.display, d.endpoint_url or "-"] for d in deps]))
        return 0

    if cmd == "status":
        dep = anyio.run(lambda: manager.status(args.id, refresh=not args.no_refresh))
        cost = None
        try:
            cost = anyio.run(lambda: manager.cost(dep.id))
        except Exception:  # noqa: BLE001 — cost is decoration
            cost = None
        if want_json:
            return out_json({"ok": True, "deployment": dep, "cost": cost})
        print(f"{dep.provider}:{dep.id}  {dep.status}")
        print(f"  model    : {dep.model}  (model= {dep.served_model_name})")
        print(f"  engine   : {dep.engine}   gpu: {dep.gpu.display} ({dep.gpu.total_vram_gb} GB)")
        print(f"  endpoint : {dep.endpoint_url or '-'}")
        print(f"  replicas : min {dep.opts.min_replicas} / max {dep.opts.max_replicas}, idle {dep.opts.idle_timeout_s}s")
        if cost is not None:
            print(f"  cost     : {_fmt_price(cost.per_hour_usd)} running, {_fmt_price(cost.idle_per_hour_usd)} idle — {cost.basis}")
        if dep.message:
            print(f"  note     : {dep.message}")
        print(f"  created  : {dep.created_at.isoformat()}")
        return 0

    if cmd == "logs":
        async def collect() -> list[str]:
            out: list[str] = []
            async for line in manager.logs(args.id, tail=args.tail):
                out.append(line)
                if len(out) >= args.tail:
                    break
            return out

        lines = anyio.run(collect)
        if want_json:
            return out_json({"ok": True, "id": args.id, "lines": lines})
        for line in lines:
            print(line)
        return 0

    if cmd == "connect":
        info_c = anyio.run(lambda: manager.connect(args.id))
        if want_json:
            return out_json({"ok": True, **info_c})
        print(f"connected: model={info_c['model']} backend={info_c['backend']}")
        print("Launch the terminal on it with:\n  "
              + _deploy_launch_line(info_c["model"], info_c["backend"], info_c.get("api_key_env")))
        return 0

    if cmd == "down":
        if not args.yes and not want_json:
            try:
                answer = input(f"Delete deployment {args.id} on its provider? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        anyio.run(lambda: manager.teardown(args.id, progress=progress))
        if want_json:
            return out_json({"ok": True, "id": args.id, "deleted": True})
        return 0

    raise _deploy_usage(f"unknown deploy action {cmd!r}")


def _deploy_usage(msg: str) -> Exception:
    from .deploy import DeployError  # noqa: PLC0415

    return DeployError(msg, hint="see `mantis-agent deploy --help`")


_HANDLERS: dict[str, Any] = {
    "version": _cmd_version,
    "deploy": _cmd_deploy,
    "list-models": _cmd_list_models,
    "probe": _cmd_probe,
    "run": _cmd_run,
    "chat": _cmd_chat,
    "setup-local": _cmd_setup_local,
    "setup-local-llamacpp": _cmd_setup_local_llamacpp,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _HANDLERS[args.cmd]
    try:
        rc = handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return int(rc or 0)


if __name__ == "__main__":  # pragma: no cover — entry-point shim
    sys.exit(main())


__all__ = ["main"]


# Silence unused import warnings for symbols we re-expose for tests.
_ = (lookup_model, resolve_tool_use_path, HOSTED_PROFILES, os)
