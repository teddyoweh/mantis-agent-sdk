# Headless & CI

`mantis-agent run` is the one-shot, scriptable form of the coding agent:
send a prompt, get the final answer (or a JSON result), exit. No TUI, no
interaction.

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
automatically and **dangerous shell commands are refused**. To allow
everything:

```bash
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
