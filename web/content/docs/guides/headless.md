# Headless & CI

Two ways to run without a terminal:

- **`mantis -p "…"`** — the terminal in print mode. Uses the model you
  already set up (no `--model`), the full coding toolset, and your
  sessions. Start here.
- **`mantis-agent run "…" --model …`** — the SDK's own one-shot runner,
  for when you want to pin every knob explicitly.

## `mantis -p` — print mode

```bash
mantis -p "fix the failing test"
mantis -p "summarize this repo" --output-format json | jq -r .result
cat spec.md | mantis -p --godmode
```

The prompt comes from the argument, from `-`, or from piped stdin.
`mantis -p` resolves the same model an interactive `mantis` would — the
one you last used, with its provider key and backend wired — so scripts
don't repeat `--model` on every call.

### Output formats

| `--output-format` | stdout |
|---|---|
| `text` (default) | the final reply, one trailing newline |
| `json` | one result object (`--verbose`: the whole message array) |
| `stream-json` | NDJSON, one message per line — **requires `--verbose`** |

`--json` is shorthand for `--output-format json`.

The exit code is `1` exactly when the result carries `is_error`, so
`mantis -p … && deploy` does the right thing. Errors and warnings go to
stderr; stdout only ever carries the result, so a pipeline can parse it.

### Streaming every message

```bash
mantis -p "refactor the parser" --output-format stream-json --verbose
```

Each line is a complete JSON object: a `system`/`init` event first
(model, tools, cwd, session id), then `assistant` and `user` messages as
the turn runs — including `tool_use` and `tool_result` blocks — and a
final `result`. That's the same shape the SDK's `query()` yields and the
same vocabulary as Claude Code's stream-json, so existing consumers port
over.

### Sessions are shared with the terminal

A print run is recorded in the same place `mantis --resume` reads, so CI
runs show up in your session picker and you can hand work back and forth:

```bash
sid=$(mantis -p "start the migration" --json | jq -r .session_id)
mantis -p "now update the tests" --resume "$sid"   # continues that conversation
mantis --resume "$sid"                             # …and open it locally
```

`--continue` picks the most recent session in the current directory.
Resuming reloads the prior turns, so the model doesn't redo work it
already did.

### Print-mode flags

| Flag | What it does |
|---|---|
| `-p`, `--print` | Run one prompt and exit |
| `--output-format` | `text` · `json` · `stream-json` |
| `--verbose` | Emit every message (required for stream-json) |
| `--godmode` | Run every tool without asking (alias: `--dangerously-skip-permissions`) |
| `--allowed-tools` | Comma-separated tools to auto-approve |
| `--disallowed-tools` | Comma-separated tools to refuse |
| `--append-system-prompt` | Add instructions without replacing the prompt |
| `--continue` / `--resume` | Run against a previous session |
| `--session-id` | Pin the session id (handy for correlating CI runs) |
| `--advisor` | Pair a stronger model to consult at decision points |
| `--max-turns` | Loop ceiling |

### An advisor for unattended runs

Nobody is watching a `-p` run, so "check this before you commit to it" is
the only review it gets. `--advisor` pairs a stronger model the agent
consults at decision points — before committing to an approach, on a
repeated failure, before calling the task done:

```bash
mantis -p "fix the flaky auth test" --advisor opus --godmode
MANTIS_ADVISOR=claude-opus-5 mantis -p "$PROMPT" --godmode   # or via the env
```

The advisor reads the run's conversation and answers with judgement — no
tools, so it can't act. It resolves its own provider and key, so the model
doing the work and the model checking it can live on different backends
(a cheap self-hosted model, escalating a few calls an hour to a hosted
one). `--advisor off` disables an advisor a runner inherited from settings.

## `mantis-agent run`

The SDK's one-shot form: send a prompt, get the final answer (or a JSON
result), exit. `--model` is required here.

## One-shot

```bash
mantis-agent run "Summarize what this repo does" --model qwen2.5:7b --tools
```

`--tools` hands the agent the coding toolset (read / write / edit / bash /
grep / glob / lsp / web) so it can actually do work. Without it you get a
plain chat completion.

Read the prompt from stdin with `-`:

```bash
cat spec.md | mantis-agent run - --model qwen2.5:7b --tools
```

## JSON output for scripts

```bash
mantis-agent run "Fix the failing test" --model qwen2.5:7b --tools --json
```

Prints one JSON object:

```json
{
  "result": "…final assistant text…",
  "is_error": false,
  "num_turns": 4,
  "total_cost_usd": 0.0031,
  "usage": { "...": "..." },
  "session_id": "…"
}
```

Pipe it to `jq`, gate CI on `is_error`, track spend with
`total_cost_usd`.

## Permissions in headless runs

There is no one to ask, so the default is safe: non-dangerous tools run
automatically and **dangerous shell commands are refused**. A prompt
nobody can answer must never become an implicit yes. To allow
everything:

```bash
mantis -p "…" --godmode
mantis-agent run "…" --model … --tools --dangerously-skip-permissions  # or --yes
```

Use that only where full autonomy is acceptable — a sandbox, a container,
trusted CI.

## Useful flags

| Flag | What it does |
|---|---|
| `--model` | Model slug (required) — routed like everywhere else |
| `--backend` | Override the backend URL |
| `--tools` | Enable the coding toolset |
| `--json` | Structured result on stdout |
| `--max-turns` | Loop ceiling (default 10) |
| `--max-tokens` | Per-turn output cap |
| `--yes` | Skip all permission checks (dangerous) |

`mantis-agent chat` is the same thing as a streaming stdin chat loop.

## Reliability, built in

Headless runs get the same hardening as everything else: transient errors
retry with exponential backoff (honoring `Retry-After`), context overflow
triggers an emergency compact + retry instead of dying, and
`fallback_model` in options retries a pre-output failure on a second
model.
