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

One slim top bar: the lowercase **mantis** wordmark, the eight page tabs —
**Overview · My models · Sessions · Activity · Deploy · MCP · Skills · Config**
— as one tight group of pills (the active one is green-tinted), then on the
right
the **current model** with a live dot and the provider it's reached through,
a **search or jump… ⌘K** button that opens the command palette, a **theme**
toggle (system → dark → light, remembered in the browser) and a **local** /
**lan · token** pill that says how the server is bound. Pages are
full-width with a centred column; the sessions page is three resizable
columns that each scroll on their own.

Surfaces are neutral — near-black in dark mode, a soft grey in light — and
there are **no lines**: no borders, no dividers, no shadows. Elevation is a
background step (page → panel → panel-2 → fill), so a card is a filled
rounded surface, hover is one step lighter, and the selected or active
thing is a green-tinted fill with green text. Inputs are filled, tables are
rows with hover fills, section headers are a normal-weight title-case label
with its count ("Providers · 3/5 ready", "GPU providers · 1/3 configured"),
and page captions are one short line. All-caps mono is reserved for tiny
metadata captions (column heads, tags). The only stroke on the page is the 2px focus
ring. The mantis green appears only where it means something: the active
tab, focus rings, primary buttons, status dots. Status is colour-coded everywhere: green running,
amber warming or tight, red failed, blue informational.

Everything is a **card**: projects, sessions, provider families, deploy
providers, models, GPUs, deployments, jobs and runs share one shape — a
filled surface with a 12px radius, hover one step lighter, selected
green-tinted.

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

- **Providers · N/5 ready** — five cards, one per family the SDK speaks:
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

### Activity

The full ledger of background jobs (sub-agents, workers, shells) and
persisted workflow runs, newest and still-running first, in filled rows:
kind, name, agents, elapsed, tokens, USD, a status chip (a breathing dot
means running) and age. Filter chips — *all · running · done · error ·
workflows · jobs* — and a search box (`/`) narrow it; the first 50 rows
show, **Load more** reveals the rest and **Load older** fetches beyond the
first 200. A row opens the run's detail sheet (phases, agents, per-agent
usage, the last log lines) or the session a job ran in. Running rows
update in place through the live version counter.

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

### My models

The page opens with **Providers** — the one and only way to connect one.
A progress line states how many of your providers are connected and reminds
you that keys live in `~/.mantis-agent` (chmod 600) and are only ever shown
masked. Below it, two labelled grids: **First-party** (Claude, OpenAI,
Gemini, Grok) and **Open-source & self-host** (the hosted catalogue, Ollama,
your own server).

Each is a card, and collapsed it says exactly four things: the vendor's mark,
the name, a state badge, and the one action that changes it (**Connect** /
**Manage**). Every collapsed card is 63px, so the grid reads as a matrix, and
every mark is optically normalised — measured ink, not a nominal box — so no
glyph looks bigger than its neighbours.

There are four states, and they are deliberately not four shades of the same
thing. **Current** is the one provider backing the model the SDK will
actually use; exactly one card can ever hold it, and it is read from the
current-model state rather than from "this family has a key". **Ready** means
connected and available — pick one of its models and it becomes Current.
**Not active** means credentials are saved but no method is switched on, and
**Not connected** means there is no credential yet. Each badge carries a
glyph whose *shape* differs — a filled dot, a hollow ring, a diamond, a faint
pip — so the states stay distinguishable without relying on colour. Current
is also the only **slanted** badge: an 8° tag with its label counter-skewed
back upright, so it differs from the rest in shape before you read a word.

The card surface stays the same neutral panel in every state; there is no
colour wash and no edge rail. The current provider is marked instead by a
**ring of pixel blocks** around the whole card — one stroked path whose 3px
width and 3/3 dash array lay down 3×3 squares, with antialiasing off
(`crispEdges`) and the centreline on a half pixel so every block is hard at
1x and 2x. The ring marches one block every three seconds, and stops entirely
under `prefers-reduced-motion`. A connected-but-idle card wears the same ring
in neutral ink at a third of the presence, so the two can never be confused.
The deploy page's GPU provider cards use the same ring, so the two surfaces
read as one app.

### Three groups, and one tally

Providers are grouped by what you can do with them, not by who makes them.
**Connected** gathers everything currently usable regardless of family, with
Current first and Ready after it; **First-party** and **Open-source &
self-host** hold what is left. A provider is in exactly one group, connecting
moves it between them with a 140ms fade, and an empty group shows no label at
all. The "*N* of *M* connected" line and the Connected group are produced by
the same predicate, so the number can never disagree with the cards — an
earlier version counted active auth methods and so missed a provider that was
current from the environment. There is no progress bar: the Connected group
is that same ratio at full size, with names on it.

### Opening a card

Opening a provider does not reflow the grid. The card expands as a panel
layered **above** it, anchored to the card's left edge and width, growing
downward — or upward if it would run off the bottom, and with its own scroll
if it fits neither way. The collapsed card keeps its 63px footprint
underneath, so every other card stays exactly where it was; the render gate
measures all fifteen rects open and closed and fails if any moves. Click
outside, press Escape, or open another card to close it — one at a time.
Focus moves to the first field on open and returns to the card on close. The
panel is the one raised surface in the stylesheet: everything flat still gets
its depth from a background step, never a shadow.

Opening a card (one at a time) reveals the rest in place — no sibling moves.
Inside is an **auth-type toggle**: Claude offers *API key · Claude subscription ·
Vertex AI · Bedrock · Azure AI Foundry*, OpenAI *API key · Azure OpenAI*,
Gemini *API key · Vertex AI*; a provider with only one way in shows that way
as a plain label rather than a lonely pill. Each pill carries a dot when that
method is configured (amber) or active (green). Picking a type swaps in only
that method's fields — generated from `auth_methods`, masked where secret,
each with its help text and the environment variable it persists under
(named once, in that help line) — plus **Save & test**, the only filled
button on the card, (saves, then probes and reports latency and live model
ids inline, or the explained error), **Check reachability** once configured,
and **Forget**. Several methods can be configured at once; exactly one is
active, and switching is one click.

**Claude subscription** replaces Save with **Sign in with Claude**: it opens
the authorize page in a new tab, then takes the pasted code or redirect URL
and finishes the exchange. Cloud methods (Vertex, Bedrock, Azure) show a
*detected* badge and name the CLI that already provides ambient credentials
(`gcloud auth application-default login`, `aws configure`) so you can leave
the fields blank. A connected card also states what it serves — the
endpoint, the model count with the current model named, the latency of the
last check and where the key came from — above a footer of its models, the
current one highlighted and each one click from becoming current. A card you
point at your own box says *the URL you set above* rather than a raw
template. `?openprov=<provider>` opens one directly. Nothing typed here ever comes back out:
responses carry env var *names* and masked hints only.

Below that, the model list is **tabbed by family** — *All · OpenAI · Claude · Gemini ·
Grok · Open models · Local*, each pill carrying its count; a model whose
family isn't connected shows **unlock**, which opens that family's setup
panel with the recommended method preselected. **All** keeps the
grouped view with the vendor's mark on each group header; a family tab
narrows to that family and drops the headers, and **Local** shows just what
Ollama has pulled. The choice lives in the URL (`#models/claude`), so a
refresh or a pasted link lands on the same tab, and it combines with the
filter chips and the search box. Every row shows the provider, the **context window** (an
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
where you paste a key, check reachability (per provider, in its own row), or
open the how-to-get-a-key guide.

### Deploy

Bring your own GPU cloud. The page is the hub for the `mantis_agent.deploy`
feature (the same thing `mantis-agent deploy` and the terminal's `/deploy`
drive): add a provider credential once, search any open model, see which
GPUs fit and what they cost, deploy with one click, watch it come up, then
**Use this model** so the SDK and the terminal point at it.

Reading top to bottom:

- **Header** — the page count is the number of live deployments; the
  provider section's label carries "N/M configured"; the top bar counts
  deployments.
- **GPU providers** — one card per adapter (RunPod, Hugging Face Inference
  Endpoints, Modal, DeepInfra, Baseten, Vast.ai, …): the vendor's mark in a
  square tinted with its own colour, the name over a one-line descriptor
  (*serverless · scale to zero*, *marketplace · always warm · public
  endpoint · plain http*), one status line that reads as a sentence — *No
  key*, *Key saved · not validated yet*, *Validated · teddy · $12.40
  balance* — quiet engine pills, and a single primary action: **Add key**
  (green) or, once saved, ghost **Replace key** and **Validate** with an
  *↗ api keys* link to the provider's key page. A configured provider's
  card is green-tinted. **Add key** opens an inline form: above the fields,
  a how-to-get-a-key guide from `provider_guides` — a one-line intro,
  numbered steps, an **Open <provider> API keys ↗** button, what the key
  looks like, and the free-credit note — then one input per
  `credential_field` with its help text. Saving validates straight away.
  Real vendor marks come from `mantis_agent/data/deploy_logos.json` when
  it is present (a letter tile otherwise).
- **A provider that can't run says so up front.** Some adapters need their
  own package (Modal deploys by driving its own SDK). That check is offline,
  and a provider missing it shows a warn state — *Needs the modal package* —
  is never counted as ready even with a key saved, and its primary action
  becomes **Install**: a sheet with the exact `pip install
  mantis-agent-sdk[modal]` line to copy, the `uv tool install --force` variant
  in one note, and a **Re-check** that re-reads the providers and updates the
  card in place. The dashboard never runs pip itself. Until it is satisfied,
  that provider is dimmed in the model picker's toggle, its GPU group is not
  offered, and any Deploy button for it reads **Needs package** — so the cost
  path is closed before a GPU is chosen, not after.
- **Gated models are stopped before the GPU, not after.** The Hub says how a
  repo gates: **auto** (click *Agree* while signed in and access is instant)
  or **manual** (the owner approves by hand, which can take days). With no
  Hugging Face token configured, a gated model's card shows
  *gated · auto* / *gated · manual*, its **Deploy** buttons read **Needs HF
  token** and are disabled, and selecting it raises a notice above *Fit &
  deploy* with the access line, a link to the repo page, and a masked
  **token field**. Saving it stores `HF_TOKEN` through the same credentials
  path as any provider key, re-inspects the model and re-enables Deploy with
  no reload; the same field sits in the confirm sheet's *Advanced*
  disclosure. Once set, the *Pick a model* header reads **Hugging Face
  token: set** and gated cards show a quiet *gated · token set*.
- **Pick a model** — the section header carries a **company filter**: one
  pill per organisation present in the results, each with its real mark and
  count, plus *All*. It combines with the search box, the sort control and
  the **New** pill (released or updated in the last 30 days), and it lives in
  the URL alongside the provider. Sorting offers *Trending · Downloads ·
  Likes · Recent* (the Hub's `lastModified`). Below it, the Hub search. Results are a grid of **model cards**: the model
  name over its org, pills for parameter count, dtype, license, a **gated**
  lock and a coloured **vllm ✓ / ? / ✗** verdict, and the estimated
  VRAM drawn as a bar measured against the largest GPU the selected provider
  actually rents (1 TB under *All*) and coloured by whether it fits — green
  fits, amber tight, grey larger than anything on offer. A quiet caption says
  how current the model is ("updated 3 days ago" inside a year, "updated Aug
  2026" beyond it). Where the Hub has nothing to derive from, the card says
  *size unknown* rather than showing an empty bar, and an unrecognised
  architecture explains itself on hover ("not in the vLLM support list —
  deploy may still work"). Hover shows *inspect →*; the
  selected card is green-tinted. With an empty query the grid is the curated
  set of good first deploys under a quiet label. New results replace the
  grid in place — no flash.
- **Fit & deploy** — for the selected model: its architectures, size, dtype,
  context length and VRAM estimate, then — for the provider chosen with the
  **provider toggle** in this section's header (*All* · RunPod · HF · Modal ·
  DeepInfra · Baseten · Vast.ai, unconfigured ones dimmed), or one group per
  configured provider under *All* — **GPU cards**: the GPU family with its
  VRAM, the price per hour set large, a coloured **fits / tight / no** pill
  (tight means under 15% headroom), the cold-start hint, and **Deploy** on
  the right. Pick the engine the provider supports (vLLM, SGLang, TGI,
  llama.cpp), open **advanced** for `max_model_len`, tensor parallel,
  quantisation, min/max replicas, idle timeout, `trust_remote_code` and an HF
  token for gated repos, and press **Deploy** on a row. The confirmation sheet
  pairs the model (its org mark) with the provider (its mark) as the
  headline, lays the spec out as tiles (GPU, engine, replicas, context),
  keeps the advanced knobs behind a disclosure, and puts the cost where you
  can't miss it — *$X/h while running*, then the idle cost and cold-start
  hint (amber when the provider is always warm). **Deploy to <provider>**
  or `Enter` deploys; `Esc` cancels. Results are cached for a minute.
- **Progress sheet** — the deploy runs as a background job; the sheet streams
  its progress lines with the elapsed time, then shows the endpoint URL, the
  served model name (what goes in `model=`) and the auth env var, with
  **Use this model** and **Copy** buttons for the one-line shell form
  (`MANTIS_AGENT_MODEL=… MANTIS_AGENT_BASE_URL=… mantis`) and the Python form
  (`MantisAgentOptions(model=…, backend=…)`).
- **Deployments** — every deployment the store knows about, as filled rows: provider mark,
  name, model, GPU, a status chip (a breathing dot while it's starting),
  endpoint (click to copy), price per hour plus accrued cost where the
  provider's billing API reports it, age, and **Use / Logs / Teardown**.
  Logs open in a side sheet with a tail size and a refresh button (providers
  without a logs API say so). Teardown asks first and names the hourly cost
  it stops. The table refreshes with the page's 15-second timer.

Empty states teach the path: with no provider configured the model picker
says *Add a GPU provider to deploy any model*; with nothing deployed the
table shows the three steps.

### Skills

A library, not a list. The header states what you have — how many skills, how
many always-loaded versus on-demand, how many are global versus from this
repo — as quiet pills, with **New skill**. Below it, a responsive grid of
**skill cards**: the name, the description clamped to two lines, a scope chip
(*Global* / *This project*, differently tinted), a loading chip (*Always
loaded* / *On demand*), the tools the skill declares as small mono pills, and
its file path as a caption. Each card carries a generated **identity glyph** —
a symmetric pattern derived deterministically from the skill's name, drawn on
the same 24-unit grid as the provider marks — so the grid is scannable at a
glance. Hover reveals *Edit* / *Delete*; a search box and pill filters (*All ·
Global · This project · Always loaded · On demand*) narrow it.

Clicking a card opens the **detail sheet**: the frontmatter as fields, the
body rendered with the built-in markdown renderer, the raw `SKILL.md` behind a
*Source* toggle, and Edit / Delete. **New skill** and **Edit** use the same
sheet: name, description, scope, category, an allowed-tools multi-select
(the built-ins plus every tool your other skills already name), and a
monospace body editor with a **live preview** beside it above ~1100px and
stacked below. The name is validated as you type — the exact path it will be
written to is shown before you save.

### MCP · Config

An inspector for every configured MCP server with a live connection test, and
the effective settings with the layer each value came from.

## Keyboard

| Keys | Action |
|---|---|
| `⌘K` / `ctrl+K` | the command palette (below) |
| `1` … `8` | jump to a page (the ⌘K palette lists each page's keys) |
| `g` then `o` / `m` / `s` / `a` / `d` | overview / my models / sessions / activity / deploy |
| `g` then `p` / `k` / `c` | mcp / skills / config |
| `/` | focus the current page's search (models filter, sessions filter, …) |
| `↑` `↓` `Enter` in the models filter | walk the visible rows and switch to one |
| `Esc` | close a sheet |

## Command palette

`⌘K` (or `ctrl+K`, or the search button in the top bar) opens a palette
over the page. Type to filter; `↑` `↓` move, `↵` runs, `esc` closes. It
lists, in groups: the eight **pages** (with their `g` chord), every
**project** known to the sessions page, the **sessions** of the project
you're in, live **deployments** (*connect …* makes one the current model),
and **actions** — *test <family> provider* for each provider family, *toggle
theme*, *refresh now*. It reuses the data the pages already loaded, so it
costs no extra requests.

## Empty states

Every empty place on the dashboard explains itself with a small hand-drawn
illustration (inline SVG on the same 24-unit grid as the provider marks,
`currentColor` for the structure and the accent token for the one live
detail — so they work in both themes and ship with the wheel), a one-line
headline, one line of what to do, and where it helps a button that does it:
an idle GPU card for *no deployments*, an empty socket for *no GPU provider*,
a two-turn transcript for *no sessions*, a server handing tools across a
dashed link for *no MCP servers*, an open playbook for *no skills*, a flat
trace for *nothing has run*, and a magnifier for *nothing matches*.

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
| `/api/activity?limit=N` | background job records and workflow runs with usage, plus `counts_7d` (running · done · error); `limit` up to 500 |
| `/api/workflow?id=RUN` | one run: phases, agents, per-agent usage, redacted inputs, last log lines |
| `/api/ollama` | the local daemon's models with size and loaded state |
| `/api/models` | providers (with family and auth), model info (window, price, capabilities), local models |
| `/api/auth/families` | every provider family: active method, one-line status, model count, all its methods |
| `/api/auth/methods?family=` | one family's methods with their fields (names, not values) and per-method status |
| `POST /api/auth/set` `{family, method, values}` | persist a method's fields and make it the active one — the only way the dashboard connects a provider |
| `POST /api/auth/clear` `{family, method}` | forget one method's saved values |
| `POST /api/auth/validate` `{family, method}` | probe it: latency, a few live model ids, or the explained error |
| `POST /api/auth/oauth/start` `{family}` · `POST /api/auth/oauth/finish` `{handle, code}` | the subscription sign-in, two steps |
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
