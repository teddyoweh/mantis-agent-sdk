# Configuration

mantis reads settings from three places. When they disagree, the more specific
one wins:

1. **Code** — whatever you pass to `MantisAgentOptions(...)` or
   `query(options=...)`. Always wins.
2. **Settings files** — `settings.json` files loaded via `setting_sources=`.
   Later sources override earlier ones, key by key.
3. **Environment variables** — the floor.

A falsy value you set on purpose still wins: `system_prompt=""` clears an
inherited prompt instead of falling through.

## Environment variables

| Variable | Effect |
|---|---|
| `MANTIS_AGENT_HOME` | Override the `~/.mantis-agent/` root. |
| `MANTIS_AGENT_MODEL` | Default model when nothing else sets one. |
| `MANTIS_AGENT_BASE_URL` | Backend URL. Beats name-based inference; loses to an explicit `backend=`. |
| `MANTIS_AGENT_API_KEY` | Key for HTTP backends, checked before the provider-specific chain. |
| `MANTIS_AGENT_MOCK` | `1` forces the mock provider — CI with no keys. |
| `MANTIS_ADVISOR` | Advisor model (`opus`, an id, or `off`). Beats the `advisorModel` setting. |
| `OPENAI_API_KEY` | OpenAI, and part of the OpenAI-compat fallback chain. |
| `ANTHROPIC_API_KEY` | Anthropic via `x-api-key`; also built-in `WebFetch`. |
| `ANTHROPIC_AUTH_TOKEN` | Anthropic via `Authorization: Bearer` — OAuth and gateways. |
| `EXA_API_KEY` | Live web results for `WebSearch` / `WebFetch`. |

<!-- docs-check: skip-env MANTIS_AGENT_BACKEND -->

There is no `MANTIS_AGENT_BACKEND`: adapters are chosen from the backend URL,
not from a name. Earlier versions of this page listed one — it never existed.
See [Models and backends](../guides/models-and-backends.md) for how selection
actually works.

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

### Safety switches

| Variable | Effect |
|---|---|
| `MANTIS_HOOKS_FAIL_CLOSED` | A raising hook denies the tool call instead of allowing it. |
| `MANTIS_SANDBOX`, `MANTIS_SANDBOX_NETWORK`, `MANTIS_SANDBOX_SCRUB_ENV` | OS-level confinement for shell tools. |
| `MANTIS_MCP_TRUST_PROJECT`, `MANTIS_SKILLS_TRUST_PROJECT` | Pre-trust project-supplied MCP servers / skills. |
| `MANTIS_WEB_ALLOW_LOCAL` | Let `WebFetch` reach private/loopback addresses. Off by default — it's an SSRF guard. |

### Browser tool

| Variable | Effect |
|---|---|
| `MANTIS_BROWSER` | Enable or disable the browser tool. |
| `MANTIS_BROWSER_ENGINE` | `chromium`, `firefox`, or `webkit`. |
| `MANTIS_BROWSER_HEADLESS` | Run headless (default) or windowed. |
| `MANTIS_BROWSER_ALLOWED_DOMAINS` / `MANTIS_BROWSER_BLOCKED_DOMAINS` | Comma-separated domain allow/deny lists. |

## Setting sources

`setting_sources` takes **source names**, not paths. Three exist, and a name
outside the set raises rather than silently doing nothing:

| Name | File | For |
|---|---|---|
| `"user"` | `$MANTIS_AGENT_HOME/settings.json` (default `~/.mantis-agent/settings.json`) | your machine-wide defaults |
| `"project"` | `<cwd>/.mantis-agent/settings.json` | the team's config — commit it |
| `"local"` | `<cwd>/.mantis-agent/settings.local.json` | your overrides — gitignore it |

```python
from mantis_agent import MantisAgentOptions

options = MantisAgentOptions(
    model="qwen2.5:7b",
    setting_sources=["user", "project", "local"],
)
```

> **Settings are opt-in.** With `setting_sources` unset, the SDK reads no
> settings file at all — a carefully filled-in `settings.json` simply won't
> apply. (The `mantis` terminal loads them for you; the library does not.)

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
  "mcp_servers": {"calc": {"command": "python", "args": ["calc.py"]}},
  "advisorModel": "opus"
}
```

Exact names matter. The budget key is `max_budget_usd` (`max_usd` is the
*option* name and does nothing here), `permission_mode` takes `default` /
`auto` / `bypass`, and `permissions` holds only `allow` and `deny`.

`advisorModel` pairs a stronger model that the terminal consults at decision
points — it resolves its own provider, so the config above runs a local 7B and
escalates the hard calls to Opus. Override per run with `--advisor` or
`MANTIS_ADVISOR` (`off` disables it).

> **No credentials here.** There is deliberately no `api_key` setting: the
> project file is meant to be committed. Pass `api_key=` in code or use the
> environment.

## Programmatic loading

```python
from mantis_agent import load_setting_source, save_setting_source

s = load_setting_source("user")     # {} when the file doesn't exist
s["model"] = "qwen2.5:7b"
print(save_setting_source("user", s))   # returns where it wrote
```

Merge several sources and layer them *underneath* your options — what
`setting_sources=` does internally:

```python
from mantis_agent import apply_settings_to_options, load_settings

merged = load_settings(["user", "project"])
options = apply_settings_to_options({"model": "qwen2.5:7b"}, merged)
```

`apply_settings_to_options` returns an options **dict** and only fills keys you
left unset. Don't splat settings into `MantisAgentOptions(**merged)` — keys like
`permissions` aren't dataclass fields, so it raises `TypeError`.

Broken JSON raises `ValueError` instead of being ignored: a typo should never
quietly change how an agent behaves.

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
