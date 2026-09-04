# The `mantis` terminal

`mantis` is the full-screen coding-agent terminal that ships with the package:
a pinned input at the bottom, the conversation scrolling above it, and a status
line that always tells you which model you are on, where it runs, and how full
its context is. This page covers the parts of that surface that make it feel
like one product across the five provider families — OpenAI, Claude, Gemini,
Grok and open-source models — rather than a thin wrapper over whichever backend
happens to be active.

Start it with `mantis` (the classic scrolling REPL is `MANTIS_CLASSIC=1 mantis`;
everything below works there too except the live views).

## The status line

The footer under the prompt is one line, always, and it says four things:

```text
⏵⏵ accept edits on   ✦ Claude claude-sonnet-5   12k/200k 6% · $0.03 · 1 monitor
```

| Segment | Meaning |
|---|---|
| `⏵⏵ accept edits on` | The permission mode (shift+tab cycles it). Coloured by mode; `default` prints nothing so a plain session grows no chrome. |
| `✦ Claude` | The **provider family** the model actually routes to — decided by the backend URL, not the model's name, so a Claude id sent through your own proxy reads `⚙ Self-host`. |
| `claude-sonnet-5` | The model id. |
| `12k/200k 6%` | Context fill: tokens used / the model's effective window and the percentage. Green below 60 %, yellow below 85 %, red after — the same ramp `/context` and `/dash` use. |
| `$0.03` | Session cost so far (omitted for local/free models). |
| `1 monitor` | Whatever is running in the background (monitors, agents, jobs) — press ↓ to manage it. |

The family glyphs are fixed so you can read them at a glance:

| Glyph | Family | Providers |
|---|---|---|
| `⌂` | Local | Ollama on `localhost` |
| `◯` | OpenAI | `openai` |
| `✦` | Claude | `anthropic` (API key or OAuth token) |
| `◆` | Gemini | `gemini` |
| `✕` | Grok | `xai` |
| `◈` | Hosted OSS | DeepSeek, Kimi, GLM, Qwen, Groq, OpenRouter, Together, Fireworks, Cerebras |
| `⚙` | Self-host | any other OpenAI-compatible URL (`/connect`) |

The line never wraps. When the terminal is narrow it sheds detail in a fixed
order — the `(shift+tab to cycle)` hint, then the `↓ to manage` hint, then the
effort/verbosity knobs, then the family name (the glyph stays) — and whatever is
still too long is cut on a safe boundary. The mode, the model and the context
fill are never dropped.

## `/dash` — the mini dashboard

`/dash` renders one panel that fits an 80×24 terminal and widens its bar on
bigger ones:

```text
╭─ mantis · dashboard ─────────────────────────────────────────────────────────╮
│ model    ✦ claude-sonnet-5  Claude (Anthropic) · OAuth token                 │
│ context  ██████████░░░░░░░░░░░░░░░░  38%  76k / 200k  124k free              │
│          system 2.1k · context/memory 400 · conversation 74k                 │
│ session  $0.03 · 401k in · 31k out · 12 messages · 4m                        │
│ mode     accept edits on · effort=high · sandbox on                          │
│ work     jobs 3 (1 running · 1 failed)   workflows 1 (1 running)             │
│ tools    mcp 2/2 servers · 14 tools   skills 3   agent tools 31              │
│ edits    mantis_agent/tui.py · tests/test_dash.py                            │
╰────────────────────────────────────── /dash live · /context · /jobs · /diff ─╯
```

Every row is read from the same place the dedicated command reads it, so the
panel can never disagree with `/context`, `/cost`, `/jobs`, `/workflows`,
`/mcp`, `/skills` or `/diff`:

- **model** — the id, its family and provider, and the auth source (`$VAR
  (env)`, `saved key`, `OAuth token`, `--api-key`, or `local` for keyless
  backends).
- **context** — a proportional bar, tokens used / the model's *effective*
  window (the one the agent has actually seen the endpoint enforce, not the
  capability table's guess), the percentage in the shared colour ramp, and the
  estimated split between system prompt, memory/env context and conversation.
- **session** — accumulated cost, total input and output tokens across every
  call, the message count and the session age.
- **mode** — permission mode, effort, and whether the shell sandbox is on.
- **work** — background jobs and workflow runs, with how many are running or
  failed.
- **tools** — MCP servers connected out of configured, their tool count, the
  skills discovered, and the tools the agent currently carries.
- **edits** — the last five files this session's write tools touched (the
  checkpoints `/rewind` restores from), newest first.

`/dash live` repaints the panel in place every two seconds until you press Esc
or Ctrl+C (or type anything). While a turn is streaming it pauses rather than
paint over the reply.

`/status` opens with the same facts in one line, then the usual table:

```text
▎✦ claude-sonnet-5 · Claude (Anthropic) · OAuth token · ctx 38% · $0.03 · accept edits on · 3 jobs (1 running)
```

## Switching between the five families

Every family is switchable from inside the session with one command, and the
confirmation says where the request will actually go:

```text
› /model claude-opus-5
model → claude-opus-5 · ✦ Claude (Anthropic) · via api.anthropic.com · OAuth token

› /model gpt-5
model → gpt-5.6-sol · ◯ OpenAI · via api.openai.com · $OPENAI_API_KEY (env)

› /model gemini-2.5-pro
model → gemini-2.5-pro · ◆ Gemini · via generativelanguage.googleapis.com/v1beta/openai · saved key

› /model grok-4
model → grok-4 · ✕ Grok (xAI) · via api.x.ai · $XAI_API_KEY (env)

› /model qwen3:8b
model → qwen3:8b · ⌂ Local (Ollama) · via localhost:11434 · local · free
```

`/model` is forgiving: an exact id, a case-insensitive fragment, a provider
word (`/model claude`, `/model gemini`) for that provider's flagship, `newest`
for the freshest model, or a number from the picker list. A family prefix that
only differs by version (`/model gpt-5` against `gpt-5.6-sol · gpt-5.6 ·
gpt-5.4`) takes the flagship instead of asking you to pick; anything genuinely
ambiguous opens the picker filtered by what you typed.

When the provider has no credential yet, nothing is switched and the exact fix
is printed:

```text
› /model grok-4
○ grok-4 needs Grok (xAI) — run /enable xai · xai-… · get one at console.x.ai
```

`/enable <provider>` prompts for the key inline (masked), validates it against
the provider's `/models` endpoint, and switches to that provider's flagship.
`/disable <provider>` forgets a saved key. Both, run bare, list every provider
in the catalog grouped by family with its live auth state:

```text
usage: /enable <provider>
  ◯ OpenAI
      openai     OpenAI             ● $OPENAI_API_KEY (env)
  ✦ Claude
      anthropic  Claude (Anthropic) ○ /enable anthropic · sk-ant-… · console.anthropic.com/settings/keys
  ◆ Gemini
      gemini     Gemini             ○ /enable gemini · AIza… · aistudio.google.com/apikey
  ✕ Grok
      xai        Grok (xAI)         ● $XAI_API_KEY (env)
  ◈ Hosted OSS
      deepseek   DeepSeek           ○ /enable deepseek · platform.deepseek.com/api_keys
      …
```

### The picker

`/models` (or a bare `/model`) opens the arrow-key picker. Groups are ordered by
family — active backend, local Ollama, open-weight, then OpenAI · Claude ·
Gemini · Grok · the hosted open-source APIs — and each group's header carries
its family glyph and either its auth state or the `/enable` line that unlocks
it:

```text
✦ Claude (Anthropic)  · OAuth token
◆ Gemini  🔒 enter adds a key · /enable gemini
```

Press Enter on a locked row to paste the key right there; on an open-weight
model that is not installed, Enter pulls it with Ollama and switches when the
download finishes. `/models list` prints the same catalog as text, family by
family, for terminals that do not do overlays.

## Reading the transcript

A few rendering rules keep the transcript scannable at 80 columns:

- **Reasoning is collapsed.** A thinking block renders as one dim line —
  `✻ thinking (273 tokens)  Let me reason about this carefully.… · /thinking show`
  — instead of the reasoning itself. `/thinking show` expands it (capped at 12
  lines with a `… +N more lines` note), `/thinking hide` drops it, and
  `/thinking collapse` is the default. Ctrl+O's expanded transcript always has
  every line regardless.
- **Tool calls are one line each.** `⚒ Run pytest -q`, `⚒ Read a.py`,
  `⚒ Search "def x"`, `⚒ Find **/*.py` — verb plus the salient argument, cut to
  the width. The result hugs the call underneath, previewed to 12 lines with
  `… +K more lines (ctrl+o to expand)`, and every preview line is cut to the
  terminal width so nothing wraps.
- **A running tool has its own timer.** While a tool executes the spinner
  reads `⚒ Run pytest -q… (38s · esc to interrupt)` — the tool's own elapsed
  seconds, not the turn clock — so a long build reads as a build, not as the
  model thinking.
- **Provider errors are one box.** A 401, 404, 429, context overflow or an
  unreachable backend renders as a single red panel titled with the failure
  class and carrying the fix, never a traceback:

    ```text
    ╭─ ✗ auth failed (401) ────────────────────────────────────────────────╮
    │ HTTP 401 Unauthorized: invalid api key                               │
    │ → the API key looks invalid — re-run mantis setup, or /models to switch │
    ╰──────────────────────────────────────────────────────────────────────╯
    ```

## Command reference for this page

| Command | What it does |
|---|---|
| `/dash` · `/dash live` | The dashboard panel; live repaints every 2 s until Esc |
| `/status` | One-line dashboard, then version · model · auth · session table |
| `/context` · `/cost` | The context breakdown and the spend, on their own |
| `/model <id\|alias>` | Switch with a one-line routing confirmation |
| `/models` · `/models list` | The picker · the family-grouped text listing |
| `/enable [provider]` · `/disable [provider]` | Add / forget a key; bare, list every provider |
| `/connect <url> [model]` | Point at your own OpenAI-compatible server |
| `/pull <tag>` | Download an open model with Ollama and switch to it |
| `/thinking show\|hide\|collapse` | How reasoning blocks render |
