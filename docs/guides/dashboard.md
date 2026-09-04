# The dashboard (`mantis serve`)

`mantis serve` starts a small local web page over everything mantis keeps in
`~/.mantis-agent`: which providers you can reach, what the agent has been
doing, what it cost, every session on the machine, and the skills and MCP
servers it's wired to — and, on the **Deploy** page, the GPU clouds you can
stand any open model up on. It is an instrument panel for a local agent
runtime — not a hosted service. Nothing leaves your machine unless you click
a button that says so.

```bash
mantis serve                 # http://127.0.0.1:8787, opens a browser tab
mantis serve --port 9000     # a different port
mantis serve --no-open       # print the URL, don't open a browser
mantis serve --lan           # bind 0.0.0.0 so a phone/laptop on your wifi can open it
```

The server is Python's standard-library `http.server` — no extra
dependencies, no build step, one self-contained HTML page with every asset
(including the provider logos) inlined, so it works with the wifi off.

## The shell

One slim top bar: the mantis mark, the seven page tabs (the active one
carries a green underline; hover shows its number key), then on the right
the **current model** with a live dot and the provider it's reached through,
a **search or jump… ⌘K** button that opens the command palette, a **theme**
toggle (system → dark → light, remembered in the browser) and a **local** /
**lan · token** pill that says how the server is bound. Pages are
full-width with a centred column; the sessions page is three resizable
columns that each scroll on their own.

Surfaces are neutral — near-black in dark mode, off-white in light — with
hairline borders for elevation and **no shadows**. The mantis green appears
only where it means something: the active tab, focus rings, primary
buttons, status dots and the signal path. Status is colour-coded
everywhere: green running, amber warming or tight, red failed, blue
informational.

Everything is a **card**: projects, sessions, provider families, deploy
providers, deployments, jobs and runs share one shape — 1px border, 10px
radius, hover brightens the border, selected turns it green.

The page is **reactive** rather than polled: it long-polls
`/api/events`, a version counter over everything it renders, and
re-renders only when that moves — through a keyed diff so a refresh never
resets your scroll or closes a drawer you opened. Lists show skeleton
placeholders on first load; actions (test a provider, use a deployment,
tear one down) update the page optimistically and roll back if the
request fails. A 15-second timer stays as the fallback when the long-poll
can't connect; both pause while the tab is hidden.

## What it shows

### Overview

The landing page. Reading top to bottom, in words:

- **Signal path** — a one-line wiring diagram: *families ready → current
  model → running jobs → sessions → deployed (when any GPU deployment is
  live) → last-7-day spend*. Each node is a real count, green-tinted when
  wired, amber when something is missing (no model set, no provider ready).
  It is the one decorative element on the page.
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

Three columns: **project cards**, the **session cards** in the selected
project (with a filter box — press `/`), and the conversation.

A project card's title is the basename of its working directory — or, when
that is itself an id-shaped name (a temp dir, a checkout named by hash), the
project's first real prompt. It is never a bare UUID; the id and path sit
underneath as a small mono caption. Pills give the session count, the last
activity and the estimated cost. A session card is titled by its first user
prompt (meta blocks stripped), with pills for messages, age, estimated cost
and the model the estimate is priced at (transcripts record no model), and
the session id as a caption.

The conversation opens with a strip of readings — turns, estimated tokens in/out, estimated cost, and the peak
context fill as a percentage of the model's window — then a **context-fill
chart**: one bar per assistant turn showing how full the window was, a dashed
line at the window's ceiling, and a blue line for cumulative estimated cost.
A compaction shows up as a visible cliff. Clicking a bar scrolls to that turn.

Messages are rows with a **role chip** (USER blue, ASSISTANT green,
compaction amber), the timestamp and, on assistant turns, the context size
and cost of that turn. Assistant text is rendered as light **markdown**
(bold, lists, headings, inline and fenced code, links) by a tiny built-in
renderer — no library. Each tool call is one collapsed row —
`⚒ Grep · auth.py` with the result's size — that opens to the exact
arguments and the result in a code block; errors open by default. Context
the runtime injected — `<system-reminder>` blocks, `<env>`, `[context]`
markers, meta messages — is **hidden by default** behind a small
*show context (N)* toggle on the message it belonged to.
Secrets are masked before anything is sent to the page:
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

### Deploy

Bring your own GPU cloud. The page is the hub for the `mantis_agent.deploy`
feature (the same thing `mantis-agent deploy` and the terminal's `/deploy`
drive): add a provider credential once, search any open model, see which
GPUs fit and what they cost, deploy with one click, watch it come up, then
**Use this model** so the SDK and the terminal point at it.

Reading top to bottom:

- **Signal path** — *providers configured → model picked → deployed →
  current model*. The overview's own signal path gains a **deployed** node
  whenever a deployment is live, and the rail counts them.
- **GPU providers** — one card per adapter (RunPod, Hugging Face Inference
  Endpoints, Modal, DeepInfra, Baseten, Vast.ai, …) with the vendor's mark,
  whether a key is saved and whether it **validated** (with the account's
  balance or credits when the provider reports one), and badges for what the
  provider does: **scale to zero** vs **always warm**, and **public endpoint**
  in warning colours where the URL is reachable without our auth (Vast.ai's
  plain-HTTP endpoints are flagged the same way). **Add key** opens an inline
  form generated from the adapter's own `credential_fields` — secret fields
  are password inputs, each with its help text and a link to the provider's
  console. Saving validates straight away.
- **Pick a model** — a Hugging Face Hub search with *trending / downloads /
  likes* sort. Each row shows the id, parameter count, dominant dtype,
  license, a **gated** tag, a **vllm ✓ / ✗ / ?** servability verdict and
  the estimated VRAM. With an empty query the list is the curated set of
  good first deploys. Clicking a row inspects it.
- **Fit & deploy** — for the selected model: its architectures, size, dtype,
  context length and VRAM estimate, then one table per *configured*
  provider: GPU, VRAM, price per hour, a **fits / tight / no** verdict (tight
  means under 15% headroom) and whether it cold-starts from zero or stays
  warm. Pick the engine the provider supports (vLLM, SGLang, TGI,
  llama.cpp), open **advanced** for `max_model_len`, tensor parallel,
  quantisation, min/max replicas, idle timeout, `trust_remote_code` and an HF
  token for gated repos, and press **Deploy** on a row. A confirmation names
  the cost first: *$X/h while running · $Y/h idle*. Results are cached for a
  minute.
- **Progress sheet** — the deploy runs as a background job; the sheet streams
  its progress lines with the elapsed time, then shows the endpoint URL, the
  served model name (what goes in `model=`) and the auth env var, with
  **Use this model** and **Copy** buttons for the one-line shell form
  (`MANTIS_AGENT_MODEL=… MANTIS_AGENT_BASE_URL=… mantis`) and the Python form
  (`MantisAgentOptions(model=…, backend=…)`).
- **Deployments** — every deployment the store knows about: provider mark,
  name, model, GPU, a status chip (a breathing dot while it's starting),
  endpoint (click to copy), price per hour plus accrued cost where the
  provider's billing API reports it, age, and **Use / Logs / Teardown**.
  Logs open in a side sheet with a tail size and a refresh button (providers
  without a logs API say so). Teardown asks first and names the hourly cost
  it stops. The table refreshes with the page's 15-second timer.

Empty states teach the path: with no provider configured the model picker
says *Add a GPU provider to deploy any model*; with nothing deployed the
table shows the three steps.

### MCP · Skills · Config

Unchanged from before: an inspector for every configured MCP server with a
live connection test, an editor for `SKILL.md` playbooks, and the effective
settings with the layer each value came from.

## Keyboard

| Keys | Action |
|---|---|
| `⌘K` / `ctrl+K` | the command palette (below) |
| `1` … `7` | jump to a page (the tabs show each key on hover) |
| `g` then `o` / `s` / `m` / `d` | overview / sessions / models / deploy |
| `g` then `p` / `k` / `c` | mcp / skills / config |
| `/` | focus the current page's search (models filter, sessions filter, …) |
| `↑` `↓` `Enter` in the models filter | walk the visible rows and switch to one |
| `Esc` | close a sheet |

## Command palette

`⌘K` (or `ctrl+K`, or the search button in the top bar) opens a palette
over the page. Type to filter; `↑` `↓` move, `↵` runs, `esc` closes. It
lists, in groups: the seven **pages** (with their `g` chord), every
**project** known to the sessions page, the **sessions** of the project
you're in, live **deployments** (*connect …* makes one the current model),
and **actions** — *test <family> provider* for each provider family, *toggle
theme*, *refresh now*. It reuses the data the pages already loaded, so it
costs no extra requests.

## Theme and layout

The page follows `prefers-color-scheme` and the toggle in the top bar
overrides it (`data-theme` on the root, remembered in the browser;
`?theme=dark|light` in the URL forces one for screenshots). Both palettes
are neutral — dark: `#0a0b0d` background, `#111316` panels, white-at-8%
borders, `#ededed` / `#9a9ea6` text; light: `#fafafa`, `#fff`, black-at-8%,
`#111` — with the mantis green as the single accent. Below about 900px the
tabs scroll horizontally and the sessions view shows one column at a time.

## API

Every page reads JSON from `/api/…`. They are handy from `curl` on a
loopback bind:

| Endpoint | Returns |
|---|---|
| `/api/overview` | top-bar readout: version, current model, counts, families ready, running jobs, live deployments, 7-day spend |
| `/api/events?since=V&timeout=S` | long-poll: blocks until the state version differs from `V` (or `S` seconds pass); returns `{version, changed}` |
| `/api/projects` | project cards: friendly `title`, first prompt, path, session count, last activity, estimated tokens and USD |
| `/api/sessions?cwd=…` | session cards: `display_title`, prompts, message count, estimated tokens and USD, the pricing model |
| `/api/providers` | the five families with auth state, last model, local Ollama status |
| `/api/spend` | per-day estimated + recorded tokens/USD for 30 days, 7/30-day totals, per-provider and per-family breakdown |
| `/api/activity?limit=N` | background job records and workflow runs with usage |
| `/api/workflow?id=RUN` | one run: phases, agents, per-agent usage, redacted inputs, last log lines |
| `/api/ollama` | the local daemon's models with size and loaded state |
| `/api/models` | providers (with family and auth), model info (window, price, capabilities), local models |
| `/api/session?cwd=…&id=…` | the transcript with per-turn ledger and session stats |
| `/api/analytics`, `/api/projects`, `/api/sessions`, `/api/skills`, `/api/mcp`, `/api/config` | as before |

Deploy endpoints (`POST` bodies are JSON; every `POST` needs the token):

| Endpoint | Does |
|---|---|
| `GET /api/deploy/providers` | every deploy adapter: configured, credential fields, engines, console URL, scale-to-zero / public flags, last validation result, logo id |
| `POST /api/deploy/creds` `{provider, values:{ENV: value}}` | save credentials into the user settings env, validate, return the account (only the env *names* come back) |
| `POST /api/deploy/validate` `{provider}` | network check of the saved credentials |
| `GET /api/deploy/gpus?provider=&min_vram=` | the provider's GPU catalogue, cheapest first |
| `GET /api/deploy/models?q=&sort=&limit=` | HF Hub search (curated list when `q` is empty) |
| `GET /api/deploy/inspect?model=` | pre-flight facts plus, per configured provider, which GPUs fit (cached 60 s) |
| `POST /api/deploy/up` `{provider, model, gpu, engine, opts}` | start a deploy job → `{job}` |
| `GET /api/deploy/job?id=` | job progress lines, status, and the final deployment or error |
| `GET /api/deploy/list?refresh=1` | stored deployments with cost |
| `GET /api/deploy/status?id=`, `GET /api/deploy/logs?id=&tail=` | one deployment refreshed from the provider; its last log lines |
| `POST /api/deploy/connect` `{id}` | verify the endpoint answers, make it the current model; returns `{model, backend, api_key_env, headers}` plus ready-to-paste shell and Python lines |
| `POST /api/deploy/down` `{id}` | start a teardown job → `{job}` |

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
- **Deploy credentials go to your user settings env.** Saving a provider key
  on the Deploy page writes it to the `env` block of the *user* settings file
  under `~/.mantis-agent` (the same place `catalog.set_key` puts provider
  keys) and exports it into the running process. The value is never sent back
  to the page — not even masked — only the names of the variables that were
  saved.
- **Deployed endpoints may be public.** On some providers the endpoint URL is
  reachable by anyone who has it (the card and the confirm dialog say so).
  Keep the auth env var set, don't paste endpoint URLs into chat, and tear a
  deployment down when you're done — it bills by the hour either way.
- **Deploy actions need the token, like every write.** Saving credentials,
  deploying, connecting and tearing down are `POST`s behind the per-launch
  token; under `--lan` the reads are too. Anyone holding the `--lan` URL can
  start and stop GPU spend on your accounts — treat it as the credential it
  is.
- **Nothing phones home.** No CDN, no analytics, no fonts fetched; the
  provider **test** buttons, the MCP connection test and the Deploy page's
  provider calls (search, validate, deploy, logs, teardown) are the only
  outbound requests, and only when you click them.
