"""``consult_advisor`` — escalate the hard moments to a stronger model.

Most turns of a long task are routine; a handful decide whether it works. The
advisor pairs a cheap main model with a stronger second one that the agent
consults at exactly those moments: before committing to an approach, when the
same error keeps coming back, before declaring a task done. The advisor reads
the conversation so far and answers with guidance the agent applies before
continuing.

The reason this belongs in mantis specifically: **the advisor doesn't have to
live on the same provider as the main model.** Run Qwen on your own box and
escalate three decisions an hour to Opus; run DeepSeek locally with a hosted
Sonnet checking its plans. The advisor resolves its own provider, base URL and
key from the catalog, independently of whatever the session is running, which a
single-vendor implementation can't do.

Configure it with ``/advisor <model>``, ``--advisor <model>``, the
``advisorModel`` setting, or ``MANTIS_ADVISOR``. ``/advisor off`` clears it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AdvisorConfig",
    "advisor_status",
    "clear_advisor_model",
    "make_advisor_tool",
    "resolve_advisor",
    "save_advisor_model",
]

_ENV = "MANTIS_ADVISOR"
_SETTING = "advisorModel"

# The advisor is a consultant, not a doer. It gets no tools: its whole job is
# to read what happened and say what to do next, and a tool-using advisor would
# just be a second agent racing the first one on the same files.
_ADVISOR_SYSTEM = """\
You are the advisor: a senior engineer a working agent consults at decision \
points. You are reading a transcript of that agent's session — the user's \
request, what it has done so far, and what it is about to do.

You do not have tools and you do not act. You give judgement.

Answer with:
1. The call. What should it do next, concretely? Take a position.
2. Why, in one or two sentences.
3. What it seems to be missing, if anything — a wrong assumption, an untested \
claim, a simpler route it walked past, a risk it hasn't named.

Be short and specific. If the plan is sound, say so plainly and stop; padding a \
"looks good" into five paragraphs wastes the escalation. If you can't tell from \
the transcript, say what evidence would settle it.\
"""

# How much of the conversation the advisor sees. Enough for the shape of the
# work, capped so escalating from a 200k-context main model to a smaller
# advisor doesn't blow its window.
_MAX_TRANSCRIPT_CHARS = 60_000


@dataclass
class AdvisorConfig:
    """A resolved advisor: which model, reached how."""

    model: str
    backend: str | None = None
    api_key: str | None = None
    label: str | None = None          # provider label, for the status line
    source: str = "settings"          # where the choice came from

    @property
    def cross_provider(self) -> bool:
        return bool(self.label)


def _configured_model() -> tuple[str, str] | None:
    """``(model, source)`` from env or settings, highest priority first."""
    env = (os.environ.get(_ENV) or "").strip()
    if env:
        return (env, "env") if env.lower() not in ("off", "none", "0") else None
    try:
        from .settings import SETTING_SOURCES, load_settings  # noqa: PLC0415

        val = (load_settings(SETTING_SOURCES) or {}).get(_SETTING)
        if isinstance(val, str) and val.strip():
            return val.strip(), "settings"
    except Exception:  # noqa: BLE001 — a bad settings file must not block launch
        pass
    return None


def resolve_advisor(model: str | None = None) -> AdvisorConfig | None:
    """Resolve the advisor for this session, or ``None`` when unset.

    ``model`` (from ``--advisor`` or ``/advisor``) wins over the environment,
    which wins over settings. The model id is resolved through the catalog the
    same way ``/model`` resolves one, so ``opus`` or ``sonnet`` work as aliases
    and the provider's saved key comes along with it.
    """
    source = "flag"
    if not model:
        found = _configured_model()
        if not found:
            return None
        model, source = found
    model = model.strip()
    if not model or model.lower() in ("off", "none"):
        return None

    cfg = AdvisorConfig(model=model, source=source)
    try:
        from . import catalog  # noqa: PLC0415

        prov = catalog.provider_for_model(cfg.model)
        if prov is None:
            # Not a known model id — try it as a query, so "opus", "sonnet",
            # "claude" and "best" all name an advisor the way /model accepts.
            res = catalog.resolve_model_query(model)
            if res.model:
                cfg.model = res.model
                prov = (catalog.BY_ID.get(getattr(res, "provider_id", "") or "")
                        or catalog.provider_for_model(cfg.model))
        if prov is not None:
            cfg.backend = prov.base_url
            cfg.label = prov.label
            cfg.api_key = catalog.api_key_for(prov)
    except Exception:  # noqa: BLE001 — an unknown model just runs on the
        pass          # session's own backend, which is a reasonable fallback
    return cfg


def save_advisor_model(model: str) -> None:
    """Persist the choice to user settings so it survives the session."""
    from .settings import update_setting_source  # noqa: PLC0415

    update_setting_source("user", {_SETTING: model})


def clear_advisor_model() -> None:
    from .settings import update_setting_source  # noqa: PLC0415

    update_setting_source("user", {_SETTING: ""})


def advisor_status(cfg: AdvisorConfig | None, main_model: str = "") -> dict[str, Any]:
    """A description of the pairing, for ``/advisor`` and ``/status``."""
    if cfg is None:
        return {"on": False}
    reachable = bool(cfg.api_key or cfg.backend is None)
    return {
        "on": True,
        "model": cfg.model,
        "via": cfg.label or "this session's backend",
        "backend": cfg.backend,
        "source": cfg.source,
        "reachable": reachable,
        "same_as_main": bool(main_model) and cfg.model == main_model,
        # The pairing worth flagging: a local main model escalating to a hosted
        # advisor is the point of the feature, not a misconfiguration.
        "cross_provider": cfg.cross_provider,
    }


def _render_transcript(messages: list[Any], limit: int = _MAX_TRANSCRIPT_CHARS) -> str:
    """The conversation as plain text for the advisor to read.

    Trimmed from the FRONT when it's too long: the advisor is being asked about
    what's happening now, and the recent turns are what make that legible.
    """
    from .types import (  # noqa: PLC0415
        AssistantMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    def render_block(block: Any) -> str:
        # Blocks carry their kind in a msgspec tag, not an attribute, so match
        # on the classes. Dicts (raw wire blocks) are handled alongside.
        if isinstance(block, TextBlock):
            return block.text
        if isinstance(block, ToolUseBlock):
            return f"[called {block.name}]"
        if isinstance(block, ToolResultBlock):
            return f"[result] {str(block.content)[:1200]}"
        if isinstance(block, dict):
            kind = block.get("type")
            if kind == "text":
                return str(block.get("text") or "")
            if kind == "tool_use":
                return f"[called {block.get('name') or 'tool'}]"
            if kind == "tool_result":
                return f"[result] {str(block.get('content') or '')[:1200]}"
        return ""                     # thinking and anything else: the advisor
                                      # forms its own view rather than reading
                                      # the main model's

    lines: list[str] = []
    for m in messages:
        role = "user" if isinstance(m, UserMessage) else (
            "assistant" if isinstance(m, AssistantMessage) else "system")
        content = getattr(m, "content", "")
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(p for p in (render_block(b) for b in content or []) if p)
        if text.strip():
            lines.append(f"### {role}\n{text.strip()}")

    body = "\n\n".join(lines)
    if len(body) > limit:
        body = "…(earlier turns trimmed)…\n\n" + body[-limit:]
    return body or "(the conversation is empty so far)"


def make_advisor_tool(
    cfg: AdvisorConfig,
    *,
    messages: list[Any],
    provider: Any = None,
    on_consult: Any = None,
    max_tokens: int = 2000,
) -> Any:
    """Build the ``consult_advisor`` tool bound to a live conversation.

    ``messages`` is the caller's running message list — the TUI passes its own,
    so the advisor always reads the real transcript rather than a copy made
    when the agent was built. ``on_consult(model, question)`` fires when a
    consultation starts, so a UI can show it happening.
    """
    from .tools import tool  # noqa: PLC0415

    @tool(name="consult_advisor", is_read_only=True, is_concurrency_safe=False)
    async def consult_advisor(question: str) -> str:
        """Ask a stronger model for judgement at a decision point.

        Consult before committing to an approach, when the same failure keeps
        recurring, or before declaring the task finished. The advisor reads the
        whole conversation, so describe what you want judged rather than
        re-explaining the context.

        Args:
            question: What you want decided or checked — the actual question,
                e.g. "About to refactor auth to use middleware. Sound, or is
                there a simpler route?" Not a summary of the session.
        """
        q = (question or "").strip()
        if not q:
            return "consult_advisor: ask an actual question."
        if on_consult is not None:
            try:
                on_consult(cfg.model, q)
            except Exception:  # noqa: BLE001 — UI callback, never fatal
                pass

        from .agent import Agent  # noqa: PLC0415
        from .providers.base import detect_provider, resolve  # noqa: PLC0415
        from .types import UserMessage  # noqa: PLC0415

        # Build the advisor's OWN provider when it lives somewhere else — this
        # is what lets a local main model escalate to a hosted one.
        adv_provider = provider
        if cfg.backend:
            try:
                factory = resolve(detect_provider(cfg.backend or cfg.model))
                kwargs: dict[str, Any] = {"base_url": cfg.backend}
                if cfg.api_key:
                    kwargs["api_key"] = cfg.api_key
                try:
                    adv_provider = factory(**kwargs)
                except TypeError:
                    adv_provider = factory()
            except Exception as e:  # noqa: BLE001
                return (f"consult_advisor: couldn't reach {cfg.model} "
                        f"({type(e).__name__}: {e}). Carry on without it.")

        transcript = _render_transcript(messages)
        prompt = (
            f"Here is the session so far.\n\n{transcript}\n\n"
            f"---\n\nThe agent asks: {q}"
        )
        advisor = Agent(
            model=cfg.model,
            provider=adv_provider,
            backend=cfg.backend,
            system=_ADVISOR_SYSTEM,
            max_tokens=max_tokens,
            max_steps=1,          # judgement, not a second agent loop
            include_recall=False,
            include_env=False,
        )
        try:
            out = await advisor.run([UserMessage(content=prompt)])
        except Exception as e:  # noqa: BLE001 — a failed consult is advice
            return (f"consult_advisor: {cfg.model} didn't answer "
                    f"({type(e).__name__}: {e}). Proceed on your own judgement.")
        finally:
            close = getattr(adv_provider, "aclose", None)
            if close is not None and adv_provider is not provider:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

        from .subagent import _extract_final_text  # noqa: PLC0415

        reply = _extract_final_text(out)
        if not reply:
            return f"consult_advisor: {cfg.model} returned nothing."
        return f"[advisor · {cfg.model}]\n{reply}"

    return consult_advisor


def advisor_prompt_section(cfg: AdvisorConfig | None) -> str:
    """The system-prompt paragraph that tells the model when to escalate."""
    if cfg is None:
        return ""
    return (
        "\n\n# Advisor\n"
        f"You can consult **{cfg.model}** — a stronger model — with "
        "`consult_advisor`. It reads this whole conversation and returns "
        "judgement, not actions.\n"
        "Consult it at decision points, not routinely: before committing to an "
        "approach on a non-trivial task, when the same failure has recurred "
        "twice, and before declaring a hard task complete. Don't consult it for "
        "questions you can answer by reading the code — read the code.\n"
        "Weigh what it says against your own evidence: if a step it recommends "
        "fails when you try it, say so rather than repeating it."
    )
