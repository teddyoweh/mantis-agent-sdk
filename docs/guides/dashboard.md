# The dashboard (`mantis serve`)

`mantis serve` starts a small local web page over everything mantis keeps in
`~/.mantis-agent`: which providers you can reach, what the agent has been
doing, what it cost, every session on the machine, and the skills and MCP
servers it's wired to. It is an instrument panel for a local agent runtime —
not a hosted service. Nothing leaves your machine.

```bash
mantis serve                 # http://127.0.0.1:8787, opens a browser tab
mantis serve --port 9000     # a different port
mantis serve --no-open       # print the URL, don't open a browser
mantis serve --lan           # bind 0.0.0.0 so a phone/laptop on your wifi can open it
```

The server is Python's standard-library `http.server` — no extra
dependencies, no build step, one self-contained HTML page with every asset
(including the provider logos) inlined, so it works with the wifi off.

## What it shows

### Overview

The landing page. Reading top to bottom, in words:

- **Signal path** — a one-line wiring diagram: *families ready → current
  model → running jobs → sessions → last-7-day spend*. Each node is a real
  count and turns amber when something is missing (no model set, no provider
  ready).
- **Providers · five families** — five cards, one per family the SDK speaks:
  **OpenAI**, **Claude (Anthropic)**, **Gemini (Google)**, **Grok (xAI)** and
  **open source** (Ollama, vLLM, Together, Fireworks, Groq, OpenRouter, …).
  Each card shows the vendor's mark, how that family is authenticated
  (*key saved* on this machine, *key from env*, *OAuth token* for a Claude
  subscription, or *no key*), the last model you used from it, and a
  **test** button that performs one `GET /models` against the endpoint with
  the credential mantis would use and reports latency and model count. The
  open-source card also says whether a local Ollama is answering and how
  many models it has loaded. Clicking a card lands in that provider's setup
  on the models page.
- **Live · jobs & workflow runs** — background jobs (sub-agents, workers,
  shells) and persisted workflow runs, newest and still-running first. Each
  row carries a status dot (a breathing one means running), kind, elapsed
  time, recorded tokens and dollars for runs, and opens either the run's
  detail (phases, agents, per-agent usage, the last log lines) or the
  session it belonged to. This section and the provider cards refresh every
  15 seconds while the tab is visible, and pause when it isn't.
- **Spend & usage** — a bar per day for the last 7 or 30 days. **Hatched**
  bars are *estimated*: transcripts store messages, not the provider's usage
  record, so session tokens are estimated from transcript size (≈4 characters
  per token, with the whole context re-billed on every assistant turn) and
  priced at the current model's rate from the SDK's price table. **Solid**
  bars are *recorded*: workflow runs persist real per-agent usage and cost.
  Below the chart, a per-provider breakdown for the last 30 days labels each
  row *estimated* or *recorded*. When the current model has no row in the
  price table the page shows tokens and leaves dollars blank rather than
  guessing.
- **Activity · last 26 weeks** — the message trace, the weekday×hour
  punchcard, the tool spectrum and the projects ledger (now with estimated
  tokens per project).

### Sessions

Three panes: projects, the sessions in the selected project (with a filter
box — press `/`), and the conversation. The conversation opens with a strip
of readings — turns, estimated tokens in/out, estimated cost, and the peak
context fill as a percentage of the model's window — then a **context-fill
chart**: one bar per assistant turn showing how full the window was, a dashed
line at the window's ceiling, and a blue line for cumulative estimated cost.
A compaction shows up as a visible cliff. Clicking a bar scrolls to that turn.

Messages carry their timestamp and, on assistant turns, the context size and
cost of that turn. Tool calls show their input; **tool results start
collapsed** with a first-line preview and size, and open on click (errors
open by default). Secrets are masked before anything is sent to the page:
vendor key prefixes (`sk-…`, `xai-…`, `ghp_…`, …), bearer tokens,
`NAME=value` pairs whose name looks like a credential, and tokens in URL
query strings.

### Models

The model list is grouped into the five families, with the vendor's mark on
each group header. Every row shows the provider, the **context window** (an
asterisk marks a ceiling mantis learned from the endpoint's own error, which
overrides the declared number), **price per 1M tokens in · out** from the
SDK's price table (a dash where the table has no row, *free* for local
runtimes), capability badges (tools / effort / thinks), and a one-click
*use →*. Filter chips: all, ready to use, needs a key, free / local.

A **local models · ollama** section lists everything the local daemon has
pulled — size on disk, parameter count and quantisation, and whether the
model is **loaded** in memory right now (with its VRAM). If Ollama isn't
answering the section says so and how to start it.

Below that, the provider setup list — grouped by family, connected first —
where you paste a key, check reachability, or open the how-to-get-a-key
guide.

### MCP · Skills · Config

Unchanged from before: an inspector for every configured MCP server with a
live connection test, an editor for `SKILL.md` playbooks, and the effective
settings with the layer each value came from.

## Keyboard

| Keys | Action |
|---|---|
| `1` … `6` | jump to a page (the rail shows each key) |
| `g` then `o` / `s` / `m` | overview / sessions / models |
| `g` then `p` / `k` / `c` | mcp / skills / config |
| `/` | focus the current page's search (models filter, sessions filter, …) |
| `↑` `↓` `Enter` in the models filter | walk the visible rows and switch to one |
| `Esc` | close a sheet |

## Theme and layout

The page follows `prefers-color-scheme`: the same olive paper/ink palette,
inverted (paper becomes ink) in dark mode. It reflows to a single column with
a horizontal nav below about 900px wide; the sessions view then shows one
pane at a time.

## API

Every page reads JSON from `/api/…`. They are handy from `curl` on a
loopback bind:

| Endpoint | Returns |
|---|---|
| `/api/overview` | rail readout: version, current model, counts, families ready, running jobs, 7-day spend |
| `/api/providers` | the five families with auth state, last model, local Ollama status |
| `/api/spend` | per-day estimated + recorded tokens/USD for 30 days, 7/30-day totals, per-provider and per-family breakdown |
| `/api/activity?limit=N` | background job records and workflow runs with usage |
| `/api/workflow?id=RUN` | one run: phases, agents, per-agent usage, redacted inputs, last log lines |
| `/api/ollama` | the local daemon's models with size and loaded state |
| `/api/models` | providers (with family and auth), model info (window, price, capabilities), local models |
| `/api/session?cwd=…&id=…` | the transcript with per-turn ledger and session stats |
| `/api/analytics`, `/api/projects`, `/api/sessions`, `/api/skills`, `/api/mcp`, `/api/config` | as before |

## Security notes

- **Loopback by default.** Without `--lan` the server binds `127.0.0.1` and
  only this machine can reach it. Reads need no token there.
- **Writes always need the token.** Saving a key, switching model, editing a
  skill or MCP server is a `POST`, and every `POST` requires the per-launch
  token that is inlined into the served page. A random web page open in your
  browser can't forge it: same-origin policy stops it reading the token.
- **`--lan` adds the token to reads too.** The printed URL carries `?k=…`;
  every `/api/…` call without it (as a query parameter or an `X-Mantis-Token`
  header) is refused with 401. Anyone with that URL can read your sessions —
  treat it like a password and don't paste it into chat.
- **DNS-rebinding defence.** Requests whose `Host` header isn't an address the
  server actually bound are refused with 421.
- **Secrets are masked on the wire.** API keys appear as `sk-…1234`, settings
  values under credential-looking names are masked, MCP env/headers are
  masked until you click *Reveal secrets*, and transcript text is scanned for
  key-shaped strings. The masking is a safety net for screenshots and shared
  screens, not a guarantee — a transcript can still contain something the
  patterns don't recognise.
- **Nothing phones home.** No CDN, no analytics, no fonts fetched; the
  provider **test** buttons and the MCP connection test are the only outbound
  requests, and only when you click them.
