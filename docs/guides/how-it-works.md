# How configuration actually works

Everything on this page is behavior you would otherwise have to read the source
to learn. None of it is exotic — it's the set of rules that decide whether the
option you just set does anything at all.

## Unknown keys are silent, on purpose

A dict of options is not validated against a schema. Keys the runtime
recognizes are consumed; **everything else flows into `Agent.extra`**, where
adapters, hooks, and plugins can read provider-specific knobs without the SDK
growing a constructor parameter per release.

That escape hatch has a cost, and you should know the shape of it:

```python
from mantis_agent.query import _agent_from_options

agent = _agent_from_options({"model": "mock", "backend": "mock", "temperatur": 0.2})
print(agent.temperature)   # the capability default — your typo did nothing
print(agent.extra)         # {'temperatur': 0.2}
```

No exception, no warning. A misspelled key, a key from a different SDK, or a
key that used to exist all behave the same way: accepted, inert.

**So when an option seems to have no effect, suspect the key name first.**
`Agent.extra` is where the evidence is.

## Two option shapes, two code paths

`query()` branches on the *type* of `options`, and the two branches differ in
more than style. This is the single most common source of "why doesn't this
work".

| | `MantisAgentOptions` / `None` | plain `dict` |
|---|---|---|
| Message shape | flat, Claude-SDK-identical: `msg.content` | nested wire shape: `msg.message.content` |
| System prompt | `system_prompt` | `system` |
| Budget cap | `max_budget_usd` | `max_usd` |
| Backend when unset | inferred from the model name | **not** inferred — defaults to `http://localhost:8000/v1` |
| Entry point | `compat_query` | `query._agent_from_options` |

Cross the wires and you get an `AttributeError` a long way from the cause, or a
connection refused against a vLLM port you never started. Details and examples:
[Models and backends](models-and-backends.md#the-two-option-shapes).

## Precedence, in one place

For anything settable in more than one place, highest wins:

1. **Explicit argument** — `Agent(...)`, `MantisAgentOptions(...)`, `options={...}`.
   A deliberately falsy value counts: `system_prompt=""` clears an inherited
   prompt rather than falling through.
2. **Settings files** — only when you opt in with `setting_sources=`; later
   sources override earlier ones. `apply_settings_to_options` fills blanks and
   never overwrites an explicit value.
3. **Environment** — the floor.

Backend URL resolution has its own chain inside that first tier: explicit
`backend`/`base_url` → `$MANTIS_AGENT_BASE_URL` → inferred from the model name
(typed path only) → Ollama.

Credentials resolve last and separately: `api_key=` → `$MANTIS_AGENT_API_KEY` →
the provider-specific chain. See
[Authentication](models-and-backends.md#authentication).

## Failure is reported, not raised — unless you ask

`query()` finishes normally when a run fails and puts the detail on the final
message. A loop that only prints assistant text therefore prints nothing and
exits 0 when the backend is down. Two things make that visible:

```python
from mantis_agent import MantisAgentOptions

options = MantisAgentOptions(model="qwen2.5:7b", raise_on_error=True)
```

- `raise_on_error=True` yields the result message and *then* raises
  `AgentError`, so nothing is hidden from a consumer that reads it.
- Errors name their destination:
  `ProviderError: Not Found (404 from http://localhost:8000/v1/chat/completions)
  — port 8000 is the vLLM default …`. The query string is stripped, because
  some providers carry the API key there and an error message ends up in logs.

## Skills are off unless you ask

`skills=None` (the default) means **no** skills. An earlier default discovered
every `SKILL.md` under `~/.mantis-agent/skills/` and injected the matching ones
into any agent — including a library caller's, in an unrelated directory. That
made behavior depend on whose machine the code ran on.

| `skills=` | Effect |
|---|---|
| `None` (default) | off |
| `"auto"` | discover, inject the ones matching each turn — what the `mantis` terminal uses |
| `"all"` | every discovered skill |
| `["name", …]` | exactly these |

## Some knobs are `Agent`-only

Not everything on `Agent` is reachable through `query()`. Neither options path
forwards these, so passing them as options puts them in `extra` and nothing
happens:

| Knob | What it does | Reach it via |
|---|---|---|
| `compactor` | the compaction strategy and its threshold | `Agent(compactor=SimpleCompactor(...))` |
| `auto_compact` | turn automatic compaction off | `Agent(auto_compact=False)` |
| `model_capability` | override tool-use strategy, context window | `Agent(model_capability=replace(cap, ...))` — also a `query()` passthrough |
| `provider` | a fully constructed provider instance | `Agent(provider=...)` — also a `query()` passthrough |
| `Hooks(...)` dataclass | the 12 hook events the dict form doesn't map | `Agent(hooks=Hooks(...))` |

## Fields that accept a value and do nothing

`MantisAgentOptions` mirrors `claude_agent_sdk`'s option names so code can move
between the two SDKs without edits. Some of those names have no implementation
here — they are accepted so an import or a copied snippet doesn't `TypeError`,
and they are **inert**:

| Field | Status |
|---|---|
| `stderr`, `debug_stderr` | Claude SDK forwards CLI stderr lines; this SDK has no subprocess. Nothing invokes the callback. |
| `stdin_input` | no CLI to feed. |
| `extra_args` | no CLI to pass through to. |
| `permission_prompt_tool_name` | no external permission-prompt tool. |
| `continue_conversation`, `resume` | implemented for the terminal's headless mode, not wired from SDK options. Use `session_id=` plus a session store. |
| `add_dirs` | accepted, unread. |
| `include_partial_messages` | accepted, unread — stream with `agent.stream()` instead. |
| `reasoning_context` | accepted and deliberately stripped before the provider wire; no feature reads it. |
| `user` | accepted, unread. |

This table is generated from behavior, not memory:
`python scripts/check_doc_coverage.py` reports coverage, and
`tests/test_docs_snippets.py` fails if a documented option turns out to be
inert. If you need one of the above wired up, that's a feature request with a
clear shape — say which one and what it should do.

## Where the runtime disagrees with a docstring

One internal docstring describes a shape the runtime doesn't use, and the
runtime is what matters: `claude_compat.HookMatcher` says hooks are called
`(input, tool_use_id, context)`. The dispatcher calls them with **one**
argument, a `HookContext`. See [Hooks](hooks.md).

Related, and easy to trip over: MCP servers are configured at the **options**
level, not on `Agent` — there is no `Agent(mcp_servers=...)` field. Pass
`mcp_servers` in options (or drive an `MCPClient` yourself).

If you hit another such case, the fastest way to settle it is the same one used
to write this page: build the object and look at what the field became.
