"""Named workflow definitions — the declarative layer over the engine.

:mod:`mantis_agent.workflow` is the *engine*: it knows how to run agents in
phases, fan them out, and report progress. It does not know what a "code
review" is. This module is the missing half — a **named, declarative
template** that says which phases exist, what fans out, and what verifies, so
a workflow can be invoked by name (``/workflows run review target=…``) instead
of being hand-assembled every time.

Why declarative and not a script
--------------------------------
Claude Code hands its model a JavaScript script it evaluates. Python has no
sandbox worth trusting, and ``exec()`` on model-authored text is not a
security model — so the definition here is **data**, not code: a phase list
the engine walks. Everything the script form buys you that matters (phases,
parallel fan-out, per-item pipelines, references between phases) is expressible
declaratively; what it loses is arbitrary control flow, which is exactly the
part you do not want a model writing into your process.

File format
-----------
One Markdown file per workflow — the same frontmatter + body shape as
``agents/*.md`` and ``skills/*/SKILL.md``, so there is one thing to learn::

    ---
    name: review
    description: Review a diff across dimensions, then verify each finding
    when_to_use: after a change is ready but before it ships
    ---

    Shared briefing prose. Prepended to every agent's prompt in this workflow.

    ```json
    {
      "inputs": [{"name": "target", "required": true,
                  "description": "what to review"}],
      "phases": [
        {"title": "Review", "mode": "parallel", "agents": [
          {"label": "bugs", "agent_type": "explore",
           "prompt": "Review {target} for correctness bugs."}
        ]},
        {"title": "Verify", "mode": "pipeline", "over": "phase:Review",
         "stages": [{"agent_type": "verify",
                     "prompt": "Try to refute:\\n{item}"}]},
        {"title": "Report", "mode": "sequential", "agents": [
          {"label": "synthesis", "prompt": "Synthesize:\\n{phase:Verify}"}
        ]}
      ]
    }
    ```

Discovery + precedence
----------------------
``builtin`` < ``user`` (``$MANTIS_AGENT_HOME/workflows/*.md``) < ``project``
(``./.mantis/workflows/*.md``) — later wins by name, so a project can override
a built-in template. Definition files are ``.md``; persisted *runs* are
``.json`` under ``workflows/runs/``, so the two never collide.

Templating
----------
``{name}`` placeholders resolve against inputs plus a few engine-provided keys:
``{item}`` / ``{index}`` / ``{prev}`` inside a pipeline, and ``{phase:Title}``
for the joined results of an earlier phase. Unknown placeholders are left
literal — a prompt containing JSON braces survives untouched.

This module imports NOTHING from ``prompt_toolkit`` and never runs a model.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

__all__ = [
    "AgentSpec",
    "InputSpec",
    "PhaseSpec",
    "WorkflowDefinition",
    "WorkflowDefinitionError",
    "BUILTIN_WORKFLOW_SOURCES",
    "MAX_DEFINITION_AGENTS",
    "builtin_definitions",
    "discover_workflow_definitions",
    "execute_definition",
    "load_workflow_definition",
    "parse_workflow_md",
    "render_template",
    "resolve_inputs",
    "validate_definition_data",
    "workflow_dirs",
]

# A definition that would spawn more agents than this is refused at load time.
# Not a policy statement about scale — a guard against a typo (a 500-item
# ``over`` list) turning into a five-figure token bill.
MAX_DEFINITION_AGENTS = 64

WORKFLOWS_SUBDIR = "workflows"

_MODES = ("parallel", "sequential", "pipeline")


class WorkflowDefinitionError(Exception):
    """A definition could not be parsed or is structurally invalid.

    ``errors`` holds every problem found (validation reports them all at once
    rather than one per fix-and-retry cycle)."""

    def __init__(self, message: str, errors: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.errors = list(errors)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """One child agent inside a phase (or one stage of a pipeline)."""

    prompt: str
    label: str = ""
    agent_type: str = "general-purpose"
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"prompt": self.prompt, "agent_type": self.agent_type}
        if self.label:
            d["label"] = self.label
        if self.model:
            d["model"] = self.model
        return d


@dataclass(frozen=True)
class PhaseSpec:
    """One phase: a barrier group (``parallel``), an ordered chain
    (``sequential``), or a per-item fan-out (``pipeline``)."""

    title: str
    mode: str = "parallel"
    detail: str = ""
    agents: tuple[AgentSpec, ...] = ()
    over: str = ""                      # pipeline only: "input:x" | "phase:Title"
    stages: tuple[AgentSpec, ...] = ()  # pipeline only

    @property
    def members(self) -> tuple[AgentSpec, ...]:
        return self.stages if self.mode == "pipeline" else self.agents

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"title": self.title, "mode": self.mode}
        if self.detail:
            d["detail"] = self.detail
        if self.mode == "pipeline":
            d["over"] = self.over
            d["stages"] = [a.to_dict() for a in self.stages]
        else:
            d["agents"] = [a.to_dict() for a in self.agents]
        return d


@dataclass(frozen=True)
class InputSpec:
    """A declared input. ``required`` inputs must be supplied at invocation."""

    name: str
    description: str = ""
    required: bool = False
    default: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "default": self.default,
        }


@dataclass(frozen=True)
class WorkflowDefinition:
    """A named, validated workflow template."""

    name: str
    description: str
    phases: tuple[PhaseSpec, ...]
    inputs: tuple[InputSpec, ...] = ()
    when_to_use: str = ""
    briefing: str = ""
    source: str = "builtin"   # builtin | user | project
    path: str = ""
    model: str = ""
    default_agent_type: str = "general-purpose"

    @property
    def min_agents(self) -> int:
        """Lower bound on agents this workflow spawns — pipelines multiply by
        their item count, which is only known at run time, so a pipeline
        contributes ``len(stages)`` here."""

        return sum(len(p.members) for p in self.phases)

    def required_input_names(self) -> list[str]:
        return [i.name for i in self.inputs if i.required]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "source": self.source,
            "path": self.path,
            "inputs": [i.to_dict() for i in self.inputs],
            "phases": [p.to_dict() for p in self.phases],
        }

    def summary_line(self) -> str:
        """One list-row line: ``review (project) — 3 phases · 5 agents``."""

        return (
            f"{self.name} ({self.source}) — {len(self.phases)} phase"
            f"{'s' if len(self.phases) != 1 else ''} · "
            f"{self.min_agents}+ agents · {self.description}"
        )


# ---------------------------------------------------------------------------
# Parsing + validation
# ---------------------------------------------------------------------------


_JSON_FENCE = re.compile(r"```(?:json|jsonc)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_json_block(body: str) -> tuple[dict[str, Any], str]:
    """Split a definition body into ``(graph, briefing)``.

    The FIRST fenced block that parses as a JSON object is the graph; every
    other line is the briefing. Raises :class:`WorkflowDefinitionError` when no
    block parses — a definition with no phases is not a workflow."""

    for m in _JSON_FENCE.finditer(body):
        raw = m.group(1)
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            briefing = (body[: m.start()] + body[m.end():]).strip()
            return data, briefing
    # No fence — maybe the whole body is JSON.
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError as e:
            raise WorkflowDefinitionError(
                f"workflow body is not valid JSON: {e}", [str(e)]
            ) from e
        if isinstance(data, dict):
            return data, ""
    raise WorkflowDefinitionError(
        "no workflow graph found — add a ```json fenced block with a "
        '"phases" list',
        ["missing json graph block"],
    )


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def validate_definition_data(name: str, data: dict[str, Any]) -> list[str]:
    """Structural validation of a parsed graph. Returns EVERY problem found.

    Checks: phases exist and are titled + uniquely named; modes are known;
    parallel/sequential phases have prompted agents; pipeline phases declare a
    resolvable ``over`` source and at least one stage; the whole thing stays
    under :data:`MAX_DEFINITION_AGENTS`."""

    errors: list[str] = []
    if not name or not str(name).strip():
        errors.append("missing 'name'")

    raw_inputs = data.get("inputs")
    input_names: set[str] = set()
    if raw_inputs is not None:
        items = raw_inputs if isinstance(raw_inputs, list) else (
            [{"name": k, **(v if isinstance(v, dict) else {})}
             for k, v in raw_inputs.items()] if isinstance(raw_inputs, dict) else None
        )
        if items is None:
            errors.append("'inputs' must be a list or an object")
        else:
            for i, item in enumerate(items):
                if isinstance(item, str):
                    input_names.add(item)
                    continue
                if not isinstance(item, dict):
                    errors.append(f"inputs[{i}] must be a string or object")
                    continue
                nm = str(item.get("name") or "").strip()
                if not nm:
                    errors.append(f"inputs[{i}] is missing 'name'")
                else:
                    input_names.add(nm)

    phases = data.get("phases")
    if not isinstance(phases, list) or not phases:
        errors.append("'phases' must be a non-empty list")
        return errors

    seen_titles: list[str] = []
    total_agents = 0
    for i, ph in enumerate(phases):
        where = f"phases[{i}]"
        if not isinstance(ph, dict):
            errors.append(f"{where} must be an object")
            continue
        title = str(ph.get("title") or "").strip()
        if not title:
            errors.append(f"{where} is missing 'title'")
        elif title in seen_titles:
            errors.append(f"{where} duplicates phase title {title!r}")
        mode = str(ph.get("mode") or "parallel").strip().lower()
        if mode not in _MODES:
            errors.append(f"{where} has unknown mode {mode!r} (use {', '.join(_MODES)})")
            mode = "parallel"

        if mode == "pipeline":
            over = str(ph.get("over") or "").strip()
            if not over:
                errors.append(f"{where} is a pipeline and must declare 'over'")
            elif over.startswith("phase:"):
                ref = over[len("phase:"):].strip()
                if ref not in seen_titles:
                    errors.append(
                        f"{where} pipes over phase {ref!r}, which is not an EARLIER phase"
                    )
            elif over.startswith("input:"):
                ref = over[len("input:"):].strip()
                if input_names and ref not in input_names:
                    errors.append(f"{where} pipes over undeclared input {ref!r}")
            else:
                errors.append(
                    f"{where} has an unsupported 'over' source {over!r} "
                    "(use 'phase:<Title>' or 'input:<name>')"
                )
            members = ph.get("stages")
            member_key = "stages"
        else:
            members = ph.get("agents")
            member_key = "agents"

        if not isinstance(members, list) or not members:
            errors.append(f"{where} must declare a non-empty '{member_key}' list")
            continue
        for j, ag in enumerate(members):
            if not isinstance(ag, dict):
                errors.append(f"{where}.{member_key}[{j}] must be an object")
                continue
            if not str(ag.get("prompt") or "").strip():
                errors.append(f"{where}.{member_key}[{j}] is missing 'prompt'")
        total_agents += len(members)
        if title:
            seen_titles.append(title)

    if total_agents > MAX_DEFINITION_AGENTS:
        errors.append(
            f"declares {total_agents} agents — over the {MAX_DEFINITION_AGENTS} cap "
            "(split it into workflows you chain instead)"
        )
    return errors


def _agent_specs(raw: Any, default_type: str) -> tuple[AgentSpec, ...]:
    out: list[AgentSpec] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        out.append(AgentSpec(
            prompt=str(item.get("prompt") or "").strip(),
            label=str(item.get("label") or "").strip(),
            agent_type=str(item.get("agent_type") or item.get("subagent_type")
                           or default_type).strip() or default_type,
            model=str(item.get("model") or "").strip(),
        ))
    return tuple(out)


def _input_specs(raw: Any) -> tuple[InputSpec, ...]:
    items: list[Any]
    if isinstance(raw, dict):
        items = [{"name": k, **(v if isinstance(v, dict) else {"description": str(v)})}
                 for k, v in raw.items()]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        items = []
    out: list[InputSpec] = []
    for item in items:
        if isinstance(item, str):
            out.append(InputSpec(name=item.strip()))
            continue
        if not isinstance(item, dict):
            continue
        nm = str(item.get("name") or "").strip()
        if not nm:
            continue
        out.append(InputSpec(
            name=nm,
            description=str(item.get("description") or "").strip(),
            required=_as_bool(item.get("required"), False),
            default=str(item.get("default") or ""),
        ))
    return tuple(out)


def parse_workflow_md(text: str, fallback_name: str = "") -> WorkflowDefinition:
    """Parse one workflow Markdown file into a validated
    :class:`WorkflowDefinition`.

    Raises :class:`WorkflowDefinitionError` (with the full ``errors`` list) when
    the graph is missing or invalid — callers that scan a directory catch it and
    skip the file rather than failing the session."""

    from .skills import _parse_skill_md  # noqa: PLC0415 — one frontmatter parser

    meta, body = _parse_skill_md(text)
    data, briefing = _extract_json_block(body)

    name = (meta.get("name") or data.get("name") or fallback_name or "").strip()
    description = (meta.get("description") or data.get("description") or "").strip()
    errors = validate_definition_data(name, data)
    if errors:
        raise WorkflowDefinitionError(
            f"invalid workflow definition {name or fallback_name!r}: {errors[0]}", errors
        )

    default_type = (meta.get("default_agent_type")
                    or data.get("default_agent_type") or "general-purpose").strip()
    phases: list[PhaseSpec] = []
    for ph in data.get("phases", []):
        mode = str(ph.get("mode") or "parallel").strip().lower()
        phases.append(PhaseSpec(
            title=str(ph.get("title") or "").strip(),
            mode=mode,
            detail=str(ph.get("detail") or "").strip(),
            agents=_agent_specs(ph.get("agents"), default_type) if mode != "pipeline" else (),
            over=str(ph.get("over") or "").strip() if mode == "pipeline" else "",
            stages=_agent_specs(ph.get("stages"), default_type) if mode == "pipeline" else (),
        ))

    return WorkflowDefinition(
        name=name,
        description=description,
        phases=tuple(phases),
        inputs=_input_specs(data.get("inputs")),
        when_to_use=(meta.get("when_to_use") or data.get("when_to_use") or "").strip(),
        briefing=briefing.strip(),
        model=(meta.get("model") or "").strip(),
        default_agent_type=default_type,
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def workflow_dirs(cwd: Any = None) -> list[tuple[str, Path]]:
    """``(source_label, dir)`` pairs in precedence order: user then project.

    Missing directories are dropped. Project wins on name, mirroring
    :func:`mantis_agent.subagent.discover_agent_types`."""

    from .paths import get_mantis_agent_dir  # noqa: PLC0415

    base = Path(cwd) if cwd is not None else Path.cwd()
    pairs = [
        ("user", get_mantis_agent_dir() / WORKFLOWS_SUBDIR),
        ("project", base / ".mantis" / WORKFLOWS_SUBDIR),
    ]
    return [(label, d) for label, d in pairs if d.is_dir()]


def builtin_definitions() -> list[WorkflowDefinition]:
    """The shipped templates: understand · design · review · research · implement.

    They are parsed through the SAME path user files take, so a built-in that
    would not load as a file cannot ship."""

    out: list[WorkflowDefinition] = []
    for src in BUILTIN_WORKFLOW_SOURCES:
        out.append(replace(parse_workflow_md(src), source="builtin"))
    return out


def discover_workflow_definitions(
    cwd: Any = None, *, errors: list[str] | None = None
) -> list[WorkflowDefinition]:
    """Built-ins plus every ``workflows/*.md`` under the user and project dirs.

    Later sources win by name (project > user > builtin). Best-effort: a file
    that fails to parse is skipped and, when ``errors`` is passed, described
    there — a broken definition must never take down the session."""

    found: dict[str, WorkflowDefinition] = {d.name: d for d in builtin_definitions()}
    for source, d in workflow_dirs(cwd):
        for md in sorted(d.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                if errors is not None:
                    errors.append(f"{md}: {e}")
                continue
            try:
                defn = parse_workflow_md(text, md.stem)
            except WorkflowDefinitionError as e:
                if errors is not None:
                    errors.append(f"{md}: {e}")
                continue
            found[defn.name] = replace(defn, source=source, path=str(md))
    return sorted(found.values(), key=lambda d: d.name)


def load_workflow_definition(name: str, cwd: Any = None) -> WorkflowDefinition | None:
    """Look one definition up by name, honoring precedence. ``None`` if unknown."""

    want = (name or "").strip()
    if not want:
        return None
    for defn in discover_workflow_definitions(cwd):
        if defn.name == want:
            return defn
    return None


# ---------------------------------------------------------------------------
# Templating + input resolution
# ---------------------------------------------------------------------------


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_. :-]*)\}")


def render_template(text: str, ctx: dict[str, str]) -> str:
    """Substitute ``{key}`` placeholders from ``ctx``.

    Unknown keys are left EXACTLY as written — prompts routinely contain JSON
    or code with braces, and silently blanking them would corrupt the brief."""

    if not text:
        return ""

    def _sub(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        if key in ctx:
            return str(ctx[key])
        return m.group(0)

    return _PLACEHOLDER.sub(_sub, text)


def resolve_inputs(defn: WorkflowDefinition, raw: Any) -> dict[str, str]:
    """Validate + normalize invocation inputs against the definition.

    Missing required inputs raise :class:`WorkflowDefinitionError` listing every
    one of them (and what it is for), so the caller can fix it in one pass.
    Undeclared extras are kept — a definition may reference ``{anything}`` in a
    prompt without formally declaring it."""

    given: dict[str, Any] = {}
    if isinstance(raw, dict):
        given = dict(raw)
    elif isinstance(raw, str) and raw.strip():
        given = {"objective": raw.strip()}

    out: dict[str, str] = {}
    missing: list[str] = []
    for spec in defn.inputs:
        value = given.pop(spec.name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            if spec.required:
                missing.append(
                    f"{spec.name}" + (f" ({spec.description})" if spec.description else "")
                )
                continue
            value = spec.default
        out[spec.name] = _stringify(value)
    for k, v in given.items():
        out[str(k)] = _stringify(v)

    if missing:
        raise WorkflowDefinitionError(
            f"workflow {defn.name!r} needs input(s): " + ", ".join(missing), missing
        )
    return out


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_stringify(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _split_items(text: str) -> list[str]:
    """Turn an ``over: input:x`` value into a work list.

    Newline-separated wins (a pasted list); a single line falls back to commas;
    a lone value is one item."""

    if not text.strip():
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        return lines
    single = lines[0]
    if "," in single:
        return [p.strip() for p in single.split(",") if p.strip()]
    return [single]


def cache_key(phase: str, label: str, prompt: str) -> str:
    """Stable identity for one agent call across runs of the same definition.

    Keyed on the prompt digest as well as the position, so an edited prompt
    MISSES the cache — a resume replays only work that is still the same work."""

    digest = hashlib.sha1(prompt.encode("utf-8", "replace")).hexdigest()[:12]  # noqa: S324
    return f"{phase}\x00{label}\x00{digest}"


# ---------------------------------------------------------------------------
# Execution — walk the definition on a live Workflow
# ---------------------------------------------------------------------------


@dataclass
class _PhaseOutcome:
    title: str
    mode: str
    results: list[str] = field(default_factory=list)


def _brief(defn: WorkflowDefinition, prompt: str) -> str:
    """Fold the shared briefing into a child's prompt. Children start with no
    memory of anything, so the briefing has to travel with every one of them."""

    if not defn.briefing:
        return prompt
    return f"{defn.briefing.strip()}\n\n---\n\n{prompt}"


async def execute_definition(
    defn: WorkflowDefinition,
    *,
    workflow: Any,
    inputs: dict[str, str] | None = None,
    cache: dict[str, str] | None = None,
    valid_agent_types: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Walk ``defn`` phase by phase on a live :class:`~mantis_agent.workflow.Workflow`.

    * ``parallel`` — every agent at once behind the engine's barrier.
    * ``sequential`` — in order, each seeing ``{prev}``.
    * ``pipeline`` — one independent chain per item from ``over``, no barrier
      between items.

    ``cache`` (from a persisted run) short-circuits agents whose phase, label
    and prompt are unchanged — that is the resume path. ``valid_agent_types``
    coerces an unknown persona to the definition default and logs it, rather
    than failing a long run over a typo.

    Returns a structured result dict; the workflow is left ``finish``-ed."""

    ctx: dict[str, str] = dict(inputs or {})
    cache = cache or {}
    known = set(valid_agent_types) if valid_agent_types is not None else None
    outcomes: list[_PhaseOutcome] = []
    replayed = 0

    def _type_for(spec: AgentSpec) -> str:
        at = spec.agent_type or defn.default_agent_type
        if known is not None and at not in known:
            workflow.log(f"unknown agent type {at!r} → {defn.default_agent_type}")
            return defn.default_agent_type
        return at

    async def _run(spec: AgentSpec, *, title: str, label: str, extra: dict[str, str]) -> str:
        nonlocal replayed
        local = dict(ctx)
        local.update(extra)
        prompt = _brief(defn, render_template(spec.prompt, local))
        hit = cache.get(cache_key(title, label, prompt))
        if hit is not None:
            replayed += 1
        return await workflow.agent(
            prompt,
            label=label,
            phase=title,
            agent_type=_type_for(spec),
            model=spec.model or None,
            cached=hit,
        )

    for ph in defn.phases:
        title = ph.title
        with workflow.phase(title, detail=render_template(ph.detail, ctx)):
            results: list[str] = []
            if ph.mode == "pipeline":
                items = _pipeline_items(ph, ctx, outcomes)
                if not items:
                    workflow.log(f"phase {title!r}: nothing to pipeline over — skipped")
                else:
                    _guard_fanout(defn, ph, len(items))
                    stages = []
                    for si, spec in enumerate(ph.stages):
                        stages.append(_make_stage(spec, si, title, _run))
                    packed = [(item, i, "") for i, item in enumerate(items)]
                    done = await workflow.pipeline(packed, *stages)
                    results = [(t[2] if isinstance(t, tuple) else str(t)) for t in done]
            elif ph.mode == "sequential":
                prev = ""
                for i, spec in enumerate(ph.agents):
                    label = spec.label or f"{_slug(title)}-{i + 1}"
                    prev = await _run(spec, title=title, label=label,
                                      extra={"prev": prev, "index": str(i + 1)})
                    results.append(prev)
            else:  # parallel
                thunks = []
                for i, spec in enumerate(ph.agents):
                    label = spec.label or f"{_slug(title)}-{i + 1}"
                    thunks.append(
                        (lambda s=spec, t=title, la=label, ix=i: _run(
                            s, title=t, label=la, extra={"index": str(ix + 1)}))
                    )
                results = list(await workflow.parallel(thunks))
        outcomes.append(_PhaseOutcome(title=title, mode=ph.mode,
                                      results=[r or "" for r in results]))
        ctx[f"phase:{title}"] = _join_results(outcomes[-1])

    workflow.finish()
    run = workflow.run
    agents = run.all_agents()
    return {
        "workflow_id": run.id,
        "definition": defn.name,
        "name": run.name,
        "status": run.status,
        "job_id": run.job_id,
        "inputs": dict(inputs or {}),
        "phases": [
            {"title": o.title, "mode": o.mode, "results": list(o.results)}
            for o in outcomes
        ],
        "agents": [
            {
                "id": a.id, "label": a.label, "phase": a.phase,
                "agent_type": a.agent_type, "status": a.status,
                "turns": a.turns, "replayed": a.replayed,
            }
            for a in agents
        ],
        "replayed": replayed,
        "cost_usd": workflow.budget_tracker.total_usd,
        "log_lines": list(run.log_lines),
    }


def _make_stage(
    spec: AgentSpec,
    si: int,
    title: str,
    run: Callable[..., Awaitable[str]],
) -> Callable[[Any], Awaitable[Any]]:
    """One pipeline stage: ``(item, index, prev) → (item, index, output)``.

    The tuple is what carries ``{item}`` down the chain — the engine only passes
    the previous stage's return value, so the item has to ride along."""

    async def stage(value: Any) -> Any:
        item, idx, prev = value
        label = spec.label or f"{_slug(title)}-{si + 1}"
        label = f"{label}·{idx + 1}"
        out = await run(spec, title=title, label=label,
                        extra={"item": item, "index": str(idx + 1), "prev": prev})
        return (item, idx, out)

    return stage


def _pipeline_items(ph: PhaseSpec, ctx: dict[str, str],
                    outcomes: list[_PhaseOutcome]) -> list[str]:
    """Resolve a pipeline's ``over`` source into a list of work items."""

    over = ph.over
    if over.startswith("phase:"):
        want = over[len("phase:"):].strip()
        for o in outcomes:
            if o.title == want:
                return [r for r in o.results if (r or "").strip()]
        return []
    if over.startswith("input:"):
        return _split_items(ctx.get(over[len("input:"):].strip(), ""))
    return []


def _guard_fanout(defn: WorkflowDefinition, ph: PhaseSpec, item_count: int) -> None:
    """Refuse a fan-out that would blow past the agent cap at RUN time.

    Validation caps the static shape; a pipeline's real width only shows up
    once ``over`` resolves, so it is checked again here."""

    total = defn.min_agents + item_count * len(ph.stages) - len(ph.stages)
    if total > MAX_DEFINITION_AGENTS:
        raise WorkflowDefinitionError(
            f"phase {ph.title!r} would fan out to {item_count} items "
            f"({total} agents total) — over the {MAX_DEFINITION_AGENTS} cap"
        )


def _join_results(outcome: _PhaseOutcome) -> str:
    parts = [r.strip() for r in outcome.results if (r or "").strip()]
    return "\n\n".join(parts)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "agent"


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------
#
# Five shapes worth knowing, one per pattern the engine supports. They are
# deliberately small: a template earns its place by being a good default and a
# readable example of the format, not by covering every case.

_UNDERSTAND = '''---
name: understand
description: Map an unfamiliar area of the codebase with parallel readers, then synthesize one brief
when_to_use: before changing code you do not know yet
---

You are one of several agents mapping a codebase area. Report concretely, with
`file:line` citations. Never speculate about code you did not read.

```json
{
  "inputs": [
    {"name": "target", "required": true,
     "description": "the subsystem, path, or question to map"}
  ],
  "phases": [
    {
      "title": "Survey",
      "mode": "parallel",
      "detail": "three readers, three angles",
      "agents": [
        {"label": "structure", "agent_type": "explore",
         "prompt": "Map the STRUCTURE of: {target}. Which files/modules exist, what each is responsible for, and how they call each other. Cite file:line."},
        {"label": "entrypoints", "agent_type": "explore",
         "prompt": "Find the ENTRY POINTS and data flow for: {target}. Where does control enter, what state does it touch, where does it exit? Cite file:line."},
        {"label": "tests", "agent_type": "explore",
         "prompt": "Find the TESTS and invariants covering: {target}. What behavior is pinned down, and what is conspicuously untested? Cite file:line."}
      ]
    },
    {
      "title": "Synthesize",
      "mode": "sequential",
      "agents": [
        {"label": "brief", "agent_type": "general-purpose",
         "prompt": "Write one tight brief on {target} from the three surveys below. Lead with the mental model, then the map, then what is NOT covered by tests. Contradictions between surveys must be called out, not averaged.\\n\\n{phase:Survey}"}
      ]
    }
  ]
}
```
'''

_DESIGN = '''---
name: design
description: Generate independent design approaches in parallel, judge them, and synthesize a recommendation
when_to_use: when the solution space is wide and one-attempt-iterated would anchor you
---

You are producing an engineering design. Be decisive and concrete: name files,
name trade-offs, and state what you would NOT do.

```json
{
  "inputs": [
    {"name": "objective", "required": true,
     "description": "what has to be designed"},
    {"name": "constraints", "required": false,
     "description": "hard constraints the design must respect"}
  ],
  "phases": [
    {
      "title": "Approaches",
      "mode": "parallel",
      "detail": "three independent angles",
      "agents": [
        {"label": "minimal", "agent_type": "plan",
         "prompt": "Design the SMALLEST change that fully solves: {objective}\\nConstraints: {constraints}\\nOptimize for reviewability and reversibility. State the trade-off you are accepting."},
        {"label": "structural", "agent_type": "plan",
         "prompt": "Design the RIGHT long-term structure for: {objective}\\nConstraints: {constraints}\\nAssume you will live with this for two years. State the migration cost honestly."},
        {"label": "risk", "agent_type": "plan",
         "prompt": "Design for: {objective}\\nConstraints: {constraints}\\nStart from the failure modes: what breaks, what is hard to roll back, what silently corrupts state. Then design around them."}
      ]
    },
    {
      "title": "Judge",
      "mode": "parallel",
      "agents": [
        {"label": "judge", "agent_type": "verify",
         "prompt": "Score the three designs below against: {objective}\\nFor each: correctness risk, blast radius, cost to implement, cost to live with. Rank them and justify the ranking. Do not be diplomatic — pick one.\\n\\n{phase:Approaches}"}
      ]
    },
    {
      "title": "Recommend",
      "mode": "sequential",
      "agents": [
        {"label": "recommendation", "agent_type": "plan",
         "prompt": "Write the final design for {objective}. Start from the winning approach in the ranking, but graft in any genuinely better idea from the runners-up. Output: the design, the file-level plan, and the rejected alternatives with one line each on why.\\n\\nDESIGNS:\\n{phase:Approaches}\\n\\nRANKING:\\n{phase:Judge}"}
      ]
    }
  ]
}
```
'''

_REVIEW = '''---
name: review
description: Review a change across dimensions in parallel, adversarially verify each dimension, then report
when_to_use: on a diff or a module that is about to ship
---

You are reviewing code. A finding must name a file, a line, and a concrete
failure — inputs plus the wrong behavior they produce. Style opinions without a
failure are noise; leave them out.

```json
{
  "inputs": [
    {"name": "target", "required": true,
     "description": "the diff, file set, or module under review"}
  ],
  "phases": [
    {
      "title": "Review",
      "mode": "parallel",
      "detail": "one reviewer per dimension",
      "agents": [
        {"label": "correctness", "agent_type": "explore",
         "prompt": "Review for CORRECTNESS bugs: {target}\\nLogic errors, wrong conditions, off-by-one, unhandled None/empty, broken invariants. Each finding: file:line, the failing input, the wrong result."},
        {"label": "edge-cases", "agent_type": "explore",
         "prompt": "Review for EDGE CASES and error handling: {target}\\nEmpty/huge inputs, concurrent access, partial failure, cancellation, resource cleanup. Each finding: file:line, the scenario, the consequence."},
        {"label": "interfaces", "agent_type": "explore",
         "prompt": "Review for INTERFACE and compatibility problems: {target}\\nPublic API changes, silent behavior changes for existing callers, serialization/format drift, missing test coverage of the contract. Each finding: file:line and who breaks."}
      ]
    },
    {
      "title": "Verify",
      "mode": "pipeline",
      "over": "phase:Review",
      "detail": "refute each reviewer independently",
      "stages": [
        {"label": "refute", "agent_type": "verify",
         "prompt": "Try to REFUTE the findings below. For each: read the actual code and decide whether the failure really occurs. Default to refuted when you cannot reproduce the reasoning. Return only the findings that survive, with the evidence that convinced you.\\n\\n{item}"}
      ]
    },
    {
      "title": "Report",
      "mode": "sequential",
      "agents": [
        {"label": "report", "agent_type": "general-purpose",
         "prompt": "Write the review of {target} from the SURVIVING findings below. Order by severity. Each entry: file:line, one-sentence defect, concrete failure scenario, suggested fix. If nothing survived, say so plainly instead of padding.\\n\\n{phase:Verify}"}
      ]
    }
  ]
}
```
'''

_RESEARCH = '''---
name: research
description: Multi-modal sweep over a question, deep-read each lead, then synthesize a sourced answer
when_to_use: for open questions where one search angle will miss things
---

You are researching a question. Every claim in your output must be traceable to
something you actually read — a file, a command's output, or a page you fetched.

```json
{
  "inputs": [
    {"name": "question", "required": true,
     "description": "the question to answer"}
  ],
  "phases": [
    {
      "title": "Sweep",
      "mode": "parallel",
      "detail": "different search modalities",
      "agents": [
        {"label": "local", "agent_type": "explore",
         "prompt": "Search THIS repository for everything bearing on: {question}\\nCode, tests, docs, comments, config. Report leads with file:line."},
        {"label": "history", "agent_type": "explore",
         "prompt": "Search the project's history and written record for: {question}\\nCHANGELOG, docs, commit messages, TODOs. What was decided before, and why? Cite where you read it."},
        {"label": "external", "agent_type": "general-purpose",
         "prompt": "Research externally: {question}\\nUse web search/fetch for authoritative sources (specs, upstream docs, issue threads). Report each source with its URL and what it establishes."}
      ]
    },
    {
      "title": "Deep read",
      "mode": "pipeline",
      "over": "phase:Sweep",
      "detail": "one deep read per lead set",
      "stages": [
        {"label": "read", "agent_type": "explore",
         "prompt": "Follow up the leads below for the question: {question}\\nRead the actual sources rather than trusting the summary. Report what they establish, what they contradict, and which leads turned out to be dead ends.\\n\\n{item}"}
      ]
    },
    {
      "title": "Answer",
      "mode": "sequential",
      "agents": [
        {"label": "answer", "agent_type": "general-purpose",
         "prompt": "Answer: {question}\\nUse only what the deep reads below establish. Lead with the answer, then the evidence, then explicitly what remains unknown. Flag any place the sources disagree.\\n\\n{phase:Deep read}"}
      ]
    }
  ]
}
```
'''

_IMPLEMENT = '''---
name: implement
description: Plan a change, implement it, then adversarially verify the result
when_to_use: for a self-contained change you want executed end to end
---

You are part of an implementation chain. Work only within the stated scope, and
never report success for something you did not actually run.

```json
{
  "inputs": [
    {"name": "objective", "required": true,
     "description": "the change to make"},
    {"name": "scope", "required": false,
     "description": "files or directories the change is allowed to touch"}
  ],
  "phases": [
    {
      "title": "Plan",
      "mode": "sequential",
      "agents": [
        {"label": "plan", "agent_type": "plan",
         "prompt": "Plan this change: {objective}\\nAllowed scope: {scope}\\nRead the code first. Output an ordered, file-level plan with the exact edits, plus how the result will be verified."}
      ]
    },
    {
      "title": "Implement",
      "mode": "sequential",
      "agents": [
        {"label": "implement", "agent_type": "general-purpose",
         "prompt": "Implement the plan below for: {objective}\\nAllowed scope: {scope}\\nFollow the plan; if it turns out to be wrong, say so and do the right thing instead. Report exactly which files you changed and how.\\n\\nPLAN:\\n{prev}"}
      ]
    },
    {
      "title": "Verify",
      "mode": "sequential",
      "agents": [
        {"label": "verify", "agent_type": "verify",
         "prompt": "Adversarially verify this change: {objective}\\nRun the relevant tests and exercise the edge cases the implementer probably missed. Do NOT just confirm it looks right. End with a single final line that is literally 'VERDICT: PASS', 'VERDICT: FAIL', or 'VERDICT: PARTIAL'.\\n\\nWHAT WAS DONE:\\n{prev}"}
      ]
    }
  ]
}
```
'''

BUILTIN_WORKFLOW_SOURCES: tuple[str, ...] = (
    _UNDERSTAND, _DESIGN, _REVIEW, _RESEARCH, _IMPLEMENT,
)
