# Configuration

Three layers, highest priority first:

1. **What you pass in code** — `MantisAgentOptions(...)` or the `options=` dict.
   Always wins.
2. **Setting sources** — `settings.json` files on disk, opted into with
   `setting_sources=`. Later sources override earlier ones.
3. **Environment variables** — the floor.

An explicitly falsy value still counts as a choice: `system_prompt=""` clears
an inherited prompt rather than falling through to the layer below.

## Setting sources

`setting_sources` takes **source names**, not file paths. There are exactly
three, and an unknown name raises `ValueError` rather than silently doing
nothing:

| Name | File | Intended use |
|---|---|---|
| `"user"` | `$MANTIS_AGENT_HOME/settings.json` (default `~/.mantis-agent/settings.json`) | your machine-wide defaults |
| `"project"` | `<cwd>/.mantis-agent/settings.json` | the team's shared config — commit this |
| `"local"` | `<cwd>/.mantis-agent/settings.local.json` | your personal overrides — gitignore this |

```python
from mantis_agent import MantisAgentOptions

options = MantisAgentOptions(
    model="qwen2.5:7b",
    setting_sources=["user", "project", "local"],
)
```

The names match Claude Code one-for-one, so the directory layout carries over
between the two tools.

!!! warning "Settings only apply when you ask for them"

    `setting_sources` is opt-in. With the field unset, no `settings.json` is
    read and a file you carefully filled in has no effect — a common surprise.

## What a `settings.json` may contain

```json
{
  "model": "qwen2.5:7b",
  "backend": "http://localhost:11434",
  "system_prompt": "Reply tersely.",
  "max_turns": 20,
  "max_tokens": 2048,
  "temperature": 0.2,
  "max_budget_usd": 1.0,
  "include_memory": true,
  "permission_mode": "default",
  "permissions": {
    "allow": ["Bash(npm install)", "Read"],
    "deny": ["Bash(rm -rf*)"]
  },
  "allowed_tools": ["Bash", "Read"],
  "disallowed_tools": ["WebFetch"],
  "env": {"SOME_API_KEY": "…"},
  "mcp_servers": {"calc": {"command": "python", "args": ["calc.py"]}},
  "advisorModel": "claude-opus-5"
}
```

`permissions.allow` / `permissions.deny` are sugar for the flat
`allowed_tools` / `disallowed_tools` lists; both spellings are accepted and
merged. `env` is merged rather than replaced, so a project file can add a
variable without erasing a user-level one.

Watch the exact key names — the budget key is **`max_budget_usd`**, not
`max_usd` (that is the *option* name, and it does nothing in a settings file).

!!! danger "No credentials in `settings.json`"

    There is deliberately no `api_key` key: the project file is meant to be
    committed. Pass `api_key=` in code, or use the environment. The `env` block
    is filtered per-tier so a cloned repo can't inject protected variables —
    see the security notes in `settings.py`.

## Environment variables

The ones you'll reach for:

| Variable | Effect |
|---|---|
| `MANTIS_AGENT_HOME` | Root for memory, transcripts, settings. Default `~/.mantis-agent/`. |
| `MANTIS_AGENT_MODEL` | Default model when nothing else sets one. |
| `MANTIS_AGENT_BASE_URL` | Backend URL. Beats name-based inference, loses to an explicit `backend=`. |
| `MANTIS_AGENT_API_KEY` | Credential for HTTP backends, checked before the provider-specific chain. |
| `MANTIS_AGENT_MOCK` | `1` forces the mock provider — CI without keys. |
| `MANTIS_ADVISOR` | Advisor model, or `off` to disable. Beats the `advisorModel` setting. |
| `ANTHROPIC_API_KEY` | Anthropic passthrough (`x-api-key`) and built-in `WebFetch`. |
| `ANTHROPIC_AUTH_TOKEN` | Anthropic via `Authorization: Bearer` — OAuth logins and gateways. |
| `EXA_API_KEY` | Live web results for `WebSearch` / `WebFetch`. |

Provider keys are also read directly — `OPENAI_API_KEY`, `TOGETHER_API_KEY`,
`FIREWORKS_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`,
`DEEPINFRA_API_KEY`, `CEREBRAS_API_KEY`, `ANYSCALE_API_KEY`,
`MOONSHOT_API_KEY`. See
[Authentication](../guides/models-and-backends.md#authentication) for the
resolution order.

Behavioral knobs, mostly for CI and hardening:

| Variable | Effect |
|---|---|
| `MANTIS_HOOKS_FAIL_CLOSED` | A failing hook denies the tool call instead of allowing it. |
| `MANTIS_SANDBOX`, `MANTIS_SANDBOX_NETWORK`, `MANTIS_SANDBOX_SCRUB_ENV` | OS-level confinement for shell tools. |
| `MANTIS_MCP_TRUST_PROJECT`, `MANTIS_SKILLS_TRUST_PROJECT` | Pre-trust project-supplied MCP servers / skills. |
| `MANTIS_AGENT_MAX_TOOL_CONCURRENCY`, `MANTIS_AGENT_MAX_TOOL_RESULT` | Parallel tool cap; tool-result truncation. |
| `MANTIS_AGENT_RETRY_ATTEMPTS`, `MANTIS_AGENT_RETRY_BASE_S`, `MANTIS_AGENT_RETRY_MAX_S` | Transient-error retry policy. |
| `MANTIS_AGENT_NO_CONTEXT` | Skip the session-start repo/env context injection. |
| `MANTIS_WEB_ALLOW_LOCAL` | Let `WebFetch` reach private/loopback addresses. Off by default — it's an SSRF guard, so turn it on only for a trusted local target. |

### Paths and directories

| Variable | Effect |
|---|---|
| `MANTIS_AGENT_PROJECT_ROOT` | Override the detected project root (what `project` / `local` settings resolve against). |
| `MANTIS_AGENT_MODELS_DIR` | GGUF cache location for the llama.cpp setup path. |
| `MANTIS_JOBS_DIR` | Where background-job records are written. |
| `MANTIS_MCP_CREDENTIALS_DIR` | Where MCP OAuth credentials are stored. |

Repointing the first three is equivalent to forging a trust decision, so they
are on the protected list a project-supplied `env` block can never set.

### Terminal defaults

These supply defaults for the matching CLI flags, so you can set a preference
once instead of typing it every run:

| Variable | Effect |
|---|---|
| `MANTIS_AGENT_EFFORT` | Default reasoning effort (`--effort`). |
| `MANTIS_AGENT_VERBOSITY` | Default output verbosity (`--verbosity`). |
| `MANTIS_AGENT_REASONING_MODE` | Default reasoning mode, `standard` or `pro`. |
| `MANTIS_AGENT_FALLBACK_MODEL` | Model the terminal retries on when a turn fails before producing output. |
| `MANTIS_AGENT_NO_PREFLIGHT` | Skip backend preflight validation at launch. |
| `MANTIS_WATCH_FOLLOWUP` | Whether `/watch` sends an automatic follow-up turn when a watched command breaks. |
| `MANTIS_CHILD_REPORT_MAX` | Character cap on a sub-agent's report before head/tail truncation. |

### Browser tool

| Variable | Effect |
|---|---|
| `MANTIS_BROWSER` | Enable or disable the browser tool. |
| `MANTIS_BROWSER_ENGINE` | `chromium`, `firefox`, or `webkit`. |
| `MANTIS_BROWSER_HEADLESS` | Run headless (default) or windowed. |
| `MANTIS_BROWSER_ALLOWED_DOMAINS` / `MANTIS_BROWSER_BLOCKED_DOMAINS` | Comma-separated domain allow/deny lists. |

## Reading and writing settings in code

One source at a time:

```python
from mantis_agent import load_setting_source, save_setting_source

s = load_setting_source("user")      # {} if the file doesn't exist
s["model"] = "qwen2.5:7b"
path = save_setting_source("user", s)
print(path)                           # where it landed
```

Merged across sources, then layered underneath options — this is exactly what
`setting_sources=` does internally, and the way to do it by hand:

```python
from mantis_agent import apply_settings_to_options, load_settings

merged = load_settings(["user", "project"])
options = apply_settings_to_options({"model": "qwen2.5:7b"}, merged)
```

`apply_settings_to_options` returns an options **dict**, and only fills keys
the caller left unset. Don't splat a merged settings dict into
`MantisAgentOptions(**merged)`: settings keys like `permissions` aren't
dataclass fields, so it raises `TypeError`.

A malformed settings file raises `ValueError` from `load_setting_source` rather
than being ignored — a typo in JSON should not silently change how an agent
behaves.

## Where state lives

```
~/.mantis-agent/
├── settings.json         user-level settings
├── models.json           saved provider keys (chmod 600) + last/recent model
├── live_models.json      cached /v1/models responses
├── memory/               persistent memory entries + index
├── sessions/             JSONL transcripts, one per session
└── models/               GGUF cache (llama.cpp setup only)
```

Paths come from the `paths` module and all honor `MANTIS_AGENT_HOME`:

```python
from mantis_agent import (
    get_mantis_agent_dir,
    get_memory_dir,
    get_memory_index,
    get_session_path,
    get_sessions_dir,
)

print(get_mantis_agent_dir())
```
