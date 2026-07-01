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
    p_run.add_argument("prompt", help="The user prompt to send.")
    _add_agent_flags(p_run)

    p_chat = sub.add_parser(
        "chat", help="Interactive stdin chat loop. Streams tokens as they arrive."
    )
    _add_agent_flags(p_chat)
    p_chat.add_argument(
        "--system", default=None, help="System prompt for the chat session."
    )

    return p


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
      * ``openai_compat`` → ``GET {base}/v1/models`` (with optional auth)
      * ``llamacpp``      → ``GET {base}/v1/models``
      * ``tgi``           → ``GET {base}/info`` (single-model server)

    Each row is annotated with what our capability table knows about it,
    so users see size/context/tool-support next to the live name.
    """

    import os  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    from .capabilities import lookup_model  # noqa: PLC0415

    kind = detect_provider(backend)
    base = backend.rstrip("/")
    api_key = args.api_key or _resolve_api_key()

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
                models_url = f"{base.rstrip('/v1')}/v1/models"
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


def _resolve_api_key() -> str | None:
    """Best-effort: return whichever provider key is set in env."""

    import os  # noqa: PLC0415

    for var in (
        "MANTIS_AGENT_API_KEY",
        "OPENAI_API_KEY",
        "TOGETHER_API_KEY",
        "FIREWORKS_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "DEEPINFRA_API_KEY",
        "CEREBRAS_API_KEY",
        "ANYSCALE_API_KEY",
        "MOONSHOT_API_KEY",
    ):
        v = os.environ.get(var)
        if v:
            return v
    return None


def _cmd_probe(args: argparse.Namespace) -> int:
    url = args.backend
    print(f"backend url: {url}")

    name = detect_provider(url)
    print(f"detected provider: {name}")

    profile = hosted_profile_from_url(url)
    if profile is not None:
        _print_profile("matched hosted profile", profile)
    else:
        print("matched hosted profile: <none — using adapter default>")

    # Live probe — only if the backend looks reachable. We don't want to hang
    # on unreachable URLs; httpx.get with a short timeout suffices.
    try:
        import httpx  # noqa: PLC0415

        # Most servers expose /v1/models or /api/tags.
        candidate_paths = ("/v1/models", "/api/tags", "/api/version")
        for path in candidate_paths:
            try:
                with httpx.Client(timeout=2.0) as c:
                    r = c.get(url.rstrip("/") + path)
                if r.status_code < 500:
                    print(f"reachable: {path} -> HTTP {r.status_code}")
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


async def _cmd_run_async(args: argparse.Namespace) -> int:
    # Imported here so the CLI cold-start path (e.g. ``mantis-agent version``)
    # doesn't pay for the query module's transitive imports.
    from .query import (  # noqa: PLC0415
        SDKAssistantMessage,
        SDKResultMessage,
        query,
    )

    options = _build_options(args)
    async for msg in query(prompt=args.prompt, options=options):
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
                    print(text)
        elif isinstance(msg, SDKResultMessage):
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


_HANDLERS: dict[str, Any] = {
    "version": _cmd_version,
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
