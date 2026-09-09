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

A **left rail** and a slim bar over the page.

The rail is the only surface on screen everywhere, so it carries the things
you navigate by. At the top, the mantis mark, the wordmark and the directory
this dashboard was started in. Then the eight pages, grouped and each with
its own drawn mark — **Workspace**: Overview · **Models**: My models, Deploy ·
**Work**: Sessions, Activity · **Extend**: MCP, Skills · **System**: Config.
A page carries a **count** when there is something in it (sessions on the
machine, families ready, MCP servers, skills) and a green live count when
something is running right now; zero is never printed. The active page is a
green-tinted row, and under it the rail opens the rows you were about to
click anyway — the five families under **My models** (with their vendor
marks and a ready dot), your recent projects under **Sessions**, what is live
under **Deploy**. Only the page you are on expands, and its caret folds the
rows away, and **Activity** opens the states its own filter has — Running,
Done, Failed — listing only the ones with something in them, because an empty
row promises something to look at that isn't there. A child is joined to its
parent by a **branch**: this sheet draws no lines, so it is a run of 1px
blocks on a 3px pitch (the card motif's material at its finest grain) with a
stub across to each row, and the accent on the row you are on.

Each **group heading is a control**: it folds its own section away and the
choice is remembered, because which sections you care about is a property of
how you work rather than of this visit. The chevron stays invisible until you
hover or focus the heading — five headings that all look like buttons read as
a filing cabinet — and shows itself while a group is folded. Navigating to a
page always opens the group it lives in, so a page can never be hidden by a
fold you made last week.

Counts are quiet mono readings. The **one** count that gets a surface is a
filled badge on **Activity** when runs have failed in the last seven days:
that is the reading you are meant to go and act on, and it only replaces the
live count when nothing is running, because two numbers on one row is a row
nobody reads. It is a real number off the activity ledger (`failed_7d`) or it
is nothing.

At the foot: the **current model** with a live dot and the
provider it is reached through (clicking goes to the page that would change
it), the **theme** toggle (system → dark → light, remembered), the version,
and a **local** / **lan · token** pill that says how the server is bound.

The rail folds to marks only with the chevron or **⌘\** — remembered per
browser — and folds itself on a phone-width window without spending that
preference.

The bar above the page says where you are: the page's own mark and name,
then what it is over ("184 sessions · 42 projects", the project you picked),
with **search or jump… ⌘K** on the right. Pages are full-width with a centred
column; the sessions page is three columns that each scroll on their own.

Every page mark is drawn on one 24-unit grid at one stroke weight in
`currentColor`, so a row colours its icon and its label together, and a
folded rail is still navigable. They are literal about what this program
does: a model is a chip with pins, a deployment is a card in a rack with its
power light on, MCP is two tools handed across a dashed link.

Surfaces are neutral — near-black in dark mode, a soft grey in light — and
there are **no lines**: no borders, no dividers, no shadows. Elevation is a
background step (page → panel → panel-2 → fill), so a card is a filled
rounded surface, hover is one step lighter, and the selected or active
thing is a green-tinted fill with green text. The rail sits on `panel` and
the content on the page ground, which is the same step every card uses — the
split between them needs no rule either. Inputs are filled, tables are
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

### Narrow, and the rail becomes a drawer

Below **1000px** the rail leaves the flow entirely and slides over the page
behind a scrim, opened by the one control the bar grows for it — a drawn
hamburger on the same 24-unit grid as every other mark, with an
`aria-expanded` state. Picking a page closes it; so does the scrim. It is
never the 60px strip of wordless marks there: a folded rail sliding over the
content is the worst of both, so the folded preference simply does not apply
below the breakpoint and is handed back the moment there is a column to fold
again. The render gate measures the rail at 900, 1000, 1200 and 1440 — off
the page when shut, flush and full width when open, no sideways scroll in
either state, every row still carrying its words and reachable by Tab.

Tab reaches every row (they are buttons, and the sheet has exactly one focus
ring); **up and down** then walk the rail, skipping anything folded away.

### The page header

Every page-shaped view opens the same way: the bar carries a **breadcrumb**,
and the page carries a title, a line saying what it is for, and a **stat
row**.

The breadcrumb is a trail you can walk back up. A page on its own is one
word; a page you have narrowed reads `My models › Claude`, and the chevron to
its left drops the step you took to get there. Only a narrowing that can
actually be undone becomes a level — the family tab, the activity filter, the
project and session you picked — because a back button that does nothing is
furniture. Intermediate steps are clickable; the step you are on is text.

The stat row is the **same reading tile the Overview uses**, so a figure means
the same thing wherever you meet it: a label with its mark, the figure, and a
line of context. Two rules keep it honest. A tile draws its inline chart only
when there is a real series behind it — none of these endpoints has one, so
they render flat rather than reserving a chart's worth of empty height. And a
tile carries a delta only when there is a previous period to compare with;
none of these has one, so none of them claims a trend. Per page: **My
models** — connected providers, models available, current model · **Activity**
— running, done, failed · **Deploy** — live deployments, hourly burn,
providers ready. The burn is summed from the rates providers actually quote
for endpoints that are actually up, and an endpoint with no rate is reported
as unpriced rather than counted as zero. The connected-providers figure is
filled by the Providers grid below it, from that grid's own predicate, so the
two can never disagree.

Three tiles stay three across until there is only room for one; four fold to
two. **Sessions** is the one view with no stat row: it is a three-pane
browser filling the viewport rather than a scrolling page, and a header band
would come out of the panes' height.

## What it shows

### Overview

The landing page: four readings across the top, then instruments down the
left and state you can act on down the right.

**The four readings.** Messages, tool calls, spend and your streak, each as a
level, what it moved since the window before it, and the shape of the run-up
under it — a line for a level, columns for a count per day, blocks for the
days you worked. The **window is chosen by the data and named in the label**:
`· 7d` if you worked this week, `· 30d` if you did not, `· all time` if the
machine has been quiet for a month. A dashboard that reports four zeroes
because you took a holiday has told you nothing, and one that says "7d" over
a month of numbers is lying; naming the window does both jobs. A move under
one percent is drawn in neutral ink rather than tinted green or red.

**Down the left, series over time:** the **trace** (26 weeks of daily volume
as a faint envelope with a 7-day mean over it, the peak annotated, and the
sessions / messages-per-session / active-days / you-vs-agent read-out under
it), **spend & usage**, and the **projects** ledger.

**Down the right, state right now:** the five provider families, **what ran**
(the last few jobs and workflow runs, with anything still running counted in
the header — the card is absent until something has run), **when you work**
and **what it reaches for**.

- **Providers · N/5 ready** — one row per family the SDK speaks:
  **OpenAI**, **Claude (Anthropic)**, **Gemini (Google)**, **Grok (xAI)** and
  **open source** (Ollama, vLLM, Together, Fireworks, Groq, OpenRouter, …).
  Each row is the vendor's mark, the family, the model you last ran from it
  (or, before you ever have, where it points), and on the right how it is
  authenticated — *key saved* on this machine, *key from env*, *OAuth token*
  for a Claude subscription, *no key* — with a dot for ready. Hover and that
  right edge becomes the one action that changes it: **test**, which performs
  one `GET /models` against the endpoint with the credential mantis would use
  and reports latency and model count in place. The open-source row says
  whether a local Ollama is answering and how many models it has loaded.
  Clicking a row lands in that family's setup on the models page; the card's
  last line goes to the models page whole.
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
- **When you work** — the weekday×hour punchcard, one dot per hour sized by
  volume, because by-hour and by-weekday separately cannot tell you about
  Sunday nights.
- **What it reaches for** — the tool spectrum: one bar split by tool, then
  the ranked list with counts and shares.

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
colour wash, no edge rail and **no outline of any kind** — an earlier ring of
pixel blocks around the card read as a dashed border at real size, and with
every connected card wearing one the grid became a field of dotted rectangles.
The current provider is marked instead by a **sparse pixel dither spread
across the card's whole inner width**: 3px accent blocks on an 8px column
pitch and a 4px row pitch, two rows deep in the empty strip below the head,
running from the left padding to the right one. Spread thin it reads as a
property *of* the card — the surface is textured, not trimmed and not
decorated in one corner.

Which columns carry a block is decided by an integer irrational-rotation test
(`c × 6183 mod 10000`, 0.6183 being a hair off the golden ratio). That
sequence is equidistributed, so the kept columns come out evenly spaced in a
non-repeating 3/5 rhythm with no clumps and no visible period — hashing or a
random draw at this density gives clusters and holes, which reads as noise;
this reads as texture somebody laid down on purpose. The second row is the
same sequence turned half a revolution, so the rows never stack into vertical
pairs. Being integer arithmetic it is exactly reproducible: the same card
draws the same field on every repaint. There is no `viewBox`, so one user unit
is one CSS pixel however wide the card ends up — a wider card gets bigger
gaps, never bigger blocks — and `shape-rendering="crispEdges"` keeps every
block a hard square at 1x and 2x. It does not animate, because a marching
dither reads as noise rather than as life.

The band is as wide as the card, and a card's width is only known after
layout, so the SVG is created empty and filled from its measured box by a
**ResizeObserver** — which fires the moment the element first has a box and
again whenever the grid reflows. (A frame callback loses the race against a
grid still being filled in from a fetch.) On the provider card — whose 63px
height is fixed — the band is pinned inside the card's own bottom padding with
a stated `width: calc(100% - 28px)`, because an `<svg>` is a replaced element
and setting `left` and `right` together would be ignored in favour of its
intrinsic 300px. On the two cards whose height is not fixed (the GPU provider
card and the model card) it is a laid-out item beside the action rather than
an overlay above it, so no card height, wrap or width can bring the two
together.

A connected-but-idle card wears the same field at **one row instead of two and
a seventh fill instead of a quarter** — about 7 blocks where Current has 24 on
the same card, a third of the density in half the depth — in neutral ink.
Since both now span the same width, the whole difference is carried by density
and row count, so it survives a greyscale or colour-blind reading. The render
gate measures every band at 900, 1200 and 1440: it must span its card, keep a
≥4px gap from everything else, sit on the pitch, and stop within two of its own
typical gaps of the right inset. The deploy page's GPU provider cards and the
current model's card in *Choose a model* wear the same motif, so the three
surfaces read as one app. An **opened** provider card wears none: it is a form
in a scrolling panel, its own head already carries the badge, and a band pinned
to the bottom of a scroll box lands on the body.

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
Ollama has pulled. Every tab but **All** leads with its family's mark, at the
same 16px box and 13px of ink the Deploy page's org filter pills use, keeping
the vendor's own colour on a bare pill — *All* is every family at once and
carries none, and **Open models** is not a vendor at all, so rather than borrow
Ollama's llama (which is what the catalogue lists as the family's logo, right
for a runtime and wrong for a family of thirty vendors) it gets a neutral
four-block glyph drawn on the same pixel grid as the card motif. *Local* is
Ollama, so there the llama is honest. The same rule draws the mark on each
group heading, so a tab and the heading it scrolls to can never disagree. The
choice lives in the URL (`#models/claude`), so a refresh or a pasted link lands
on the same tab, and it combines with the filter chips and the search box.

Models are **cards**, in the same responsive `minmax(300px, 1fr)` grid and the
same card component the Deploy page's model picker uses — one design for
"pick a model", not two. Each card carries the vendor's mark in its optically
normalised square, the **model id** as its title with the **serving provider**
as the caption under it, and the facts as quiet pills: the **context window**
(an asterisk marks a ceiling mantis learned from the endpoint's own error,
which overrides the declared number), **price per 1M tokens in · out** from
the SDK's price table (a dash where the table has no row, *free* for a local
runtime, which is not a price of zero but your own hardware), and capability
tags — *tools · effort · thinks*, plus *loaded* for an Ollama model that is in
memory right now. Readiness is printed **only when it is not ready**: a card
that needs a key says so in amber, and every other card says it is usable by
offering *use →*. The card's foot carries that one action; the current model's
card carries the slanted **Current** tag and the pixel motif instead, and no
action at all, because there is nothing left to do to it. *unlock →*
deep-links into that family's setup panel with the recommended method
preselected. Filter chips: all, ready to use, needs a key, free / local.

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
  provider section's label carries "N/M configured"; the rail counts live
  deployments and lists them under **Deploy**.
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
| `⌘\` / `ctrl+\` | fold the rail to marks only |
| `1` … `8` | jump to a page, in rail order (the ⌘K palette lists each page's keys) |
| `g` then `o` / `m` / `s` / `a` / `d` | overview / my models / sessions / activity / deploy |
| `g` then `p` / `k` / `c` | mcp / skills / config |
| `/` | focus the current page's search (models filter, sessions filter, …) |
| `↑` `↓` `Enter` in the models filter | walk the visible rows and switch to one |
| `Esc` | close a sheet |

## Command palette

`⌘K` (or `ctrl+K`, or the search button in the bar) opens a palette
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

The page follows `prefers-color-scheme` and the toggle at the foot of the
rail overrides it (`data-theme` on the root, remembered in the browser;
`?theme=dark|light` in the URL forces one for screenshots). Both palettes
are neutral — dark: `#0a0b0d` background, `#111316` panels, white-at-8%
borders, `#ededed` / `#9a9ea6` text; light: `#fafafa`, `#fff`, black-at-8%,
`#111` — with the mantis green as the single accent. Below about 900px the
rail folds to marks only and the sessions view shows one column at a time.

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
