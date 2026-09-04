# The mantis terminal

`mantis` is a Claude-Code-style coding agent that ships in the same pip
package as the SDK. Point it at a project and it reads, writes, edits,
greps, and runs shell commands — driving whatever model you set up, from
any of the five provider families: OpenAI, Claude, Gemini, Grok, or an
open-source model on your own hardware or a hosted API.

```bash
pip install mantis-agent-sdk
mantis setup     # detects your machine, pulls the best local coding model
mantis           # start coding
```

Resume where you left off:

```bash
mantis --continue    # or -c: reopens your most recent conversation
```

## Staying up to date

```bash
mantis update          # upgrade to the latest release
mantis update --check  # just report what's available, install nothing
```

It works out how mantis was installed — `uv tool`, `pipx`, or plain pip —
and runs the matching upgrade, so you don't have to remember. A uv-created
venv has no `pip` inside it; mantis notices and uses `uv` instead rather
than failing with "No module named pip".

One case it won't touch: an **editable install from a source checkout**.
That's your working tree, possibly with uncommitted changes, so `mantis
update` prints the `git pull` you'd want and stops. `/update` inside the
terminal does the same thing.

`--check` exits `0` when you're current and `1` when an upgrade is waiting,
so it drops straight into a shell condition or a cron job.

## The status line

The footer under the prompt is one line, always, and it says four things:

```text
⏵⏵ accept edits on   ✦ Claude claude-sonnet-5   12k/200k 6% · $0.03 · 1 monitor
```

| Segment | Meaning |
|---|---|
| `⏵⏵ accept edits on` | The permission mode (shift+tab cycles it). `default` prints nothing so a plain session grows no chrome. |
| `✦ Claude` | The **provider family** the model actually routes to — decided by the backend URL, not the model's name, so a Claude id sent through your own proxy reads `⚙ Self-host`. |
| `claude-sonnet-5` | The model id. |
| `12k/200k 6%` | Context fill: tokens used / the model's effective window. Green below 60%, yellow below 85%, red after — the same ramp `/context` and `/dash` use. |
| `$0.03` | Session cost so far (omitted for local/free models). |
| `1 monitor` | Whatever is running in the background — press ↓ to manage it. |

The family glyphs are fixed so you can read them at a glance: `⌂` Local
(Ollama), `◯` OpenAI, `✦` Claude, `◆` Gemini, `✕` Grok, `◈` Hosted OSS
(DeepSeek, Kimi, GLM, Qwen, Groq, OpenRouter, Together, Fireworks, Cerebras),
`⚙` Self-host (any other OpenAI-compatible URL). The line never wraps: a
narrow terminal sheds the hints, then the effort knobs, then the family name
(the glyph stays); the mode, the model and the context fill are never dropped.

## `/dash` — the mini dashboard

`/dash` renders one panel that fits an 80×24 terminal:

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
`/mcp`, `/skills` or `/diff`: the model with its family and auth source
(`$VAR (env)`, `saved key`, `OAuth token`, `--api-key`, or `local`), a
context bar against the model's *effective* window (the one the endpoint has
actually enforced) with the estimated system/memory/conversation split,
session cost and tokens, permission mode, effort and sandbox state,
background jobs and workflow runs, MCP servers and tool counts, skills, and
the last five files this session's write tools touched. A `deploy` row
appears whenever a [deployment](deploy.md) exists, with the combined $/h.

`/dash live` repaints the panel in place every two seconds until you press
Esc or Ctrl+C. While a turn is streaming it pauses rather than paint over the
reply. `/status` opens with the same facts in one line, then the usual table.

## Switching between the five families

Every family is switchable from inside the session, and the confirmation
says where the request will actually go:

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
word (`/model claude`, `/model gemini`) for that provider's flagship,
`newest` for the freshest model, or a number from the picker list. A prefix
that only differs by version (`/model gpt-5`) takes the flagship instead of
asking; anything genuinely ambiguous opens the picker filtered by what you
typed.

When the provider has no credential yet, nothing is switched and the exact
fix is printed:

```text
› /model grok-4
○ grok-4 needs Grok (xAI) — run /enable xai · xai-… · get one at console.x.ai
```

`/enable <provider>` prompts for the key inline (masked), validates it
against the provider's `/models` endpoint, and switches to that provider's
flagship. `/disable <provider>` forgets a saved key. Both, run bare, list
every provider grouped by family with its live auth state.

`/models` (or a bare `/model`) opens the arrow-key picker, grouped by family
— active backend, local Ollama, open-weight, then OpenAI · Claude · Gemini ·
Grok · the hosted open-source APIs — with each header carrying its family
glyph and either its auth state or the `/enable` line that unlocks it. Enter
on a locked row pastes the key right there; Enter on an open-weight model
that isn't installed pulls it with Ollama and switches when the download
finishes. `/models list` prints the same catalog as text.

## Teach it your project

- **`/init`** — analyzes the repo and writes `MANTIS.md`, the project
  brief every session loads (it reads `AGENTS.md` too, if you have one).
- **`.mantis/rules/*.md`** — path-scoped rules with globs: drop a rule
  file that only applies when the agent touches matching paths.
- **`/memory`** — edit `MANTIS.md` / `AGENTS.md` in your `$EDITOR`.
- **`/learn`** — consolidate durable facts from the session into memory,
  so the next session already knows them.

## Working with files

- **`@path/to/file`** — mention a file and its contents are injected
  inline; mention a directory and you get its listing. Binary/image files
  are noted, not dumped.
- **`Ctrl+V`** — paste a copied screenshot or file path straight into the
  prompt. Terminals like iTerm2/WezTerm render images inline.
- **`/diff`** — review every file change the session has made.
- Edits render as real line-numbered diffs with word-level highlights.

## Autonomy

Three commands turn the terminal into an operator:

- **`/goal <task>`** — autopilot: plan → execute → verify until done.
- **`/watch <command>`** — run a command under watch; when it breaks, the
  agent wakes up and fixes it.
- **`/loop 5m <prompt>`** — re-run a prompt on an interval.

## Sessions

Conversations persist automatically. `/resume` reopens a past session,
`/branch` forks the current one to try a different approach, `/rewind`
steps back. `/compact` manually compacts a long conversation (it happens
automatically near the context limit, pinning your original task
verbatim). `/context` shows what's filling the window and the session's
running cost — also visible live in the footer.

## Delegation

The **task tool** spawns read-only subagents for research fan-outs, and
the SDK's sub-agent types (including twins) are available to the terminal
the same way they are in the library.

## Staying in control

`shift+tab` cycles the permission mode; dangerous commands always ask.
Plan mode presents an approach for approval before executing. If you
interrupt mid-turn (`Esc`), completed work is kept — open tool calls are
closed, not discarded.

For automation there's `mantis --dangerously-skip-permissions`
(`--godmode`), which bypasses every prompt — reserve it for sandboxes and
trusted CI, and see [Headless & CI](headless.md) for the safer scripted
path.

## Images

Copy a screenshot and mantis offers it: `Image in clipboard · ctrl+v to
paste` appears above the prompt, and `ctrl+v` stages it for your next
message. (On macOS `⌘V` can't carry an image into a terminal at all — that's
why the hint exists.) Once staged you'll see `◫ 1 image attached`, plus a
warning if the current model can't see images.

Terminals that bind `ctrl+v` to their own paste can use `/paste`, and
`/paste <path>` attaches a file directly. Dragging an image into the prompt
works too, even mid-sentence — "what's wrong with ~/shot.png here?" sends the
picture along with the question.

## Reading the transcript

A few rendering rules keep the transcript scannable at 80 columns:

- **Reasoning is collapsed.** A thinking block renders as one dim line —
  `✻ thinking (273 tokens)  Let me reason about this carefully.… · /thinking show`
  — instead of the reasoning itself. `/thinking show` expands it (capped at
  12 lines with a `… +N more lines` note), `/thinking hide` drops it, and
  `/thinking collapse` is the default. Ctrl+O's expanded transcript always
  has every line.
- **Tool calls are one line each.** `⚒ Run pytest -q`, `⚒ Read a.py`,
  `⚒ Search "def x"` — verb plus the salient argument, cut to the width. The
  result hugs the call underneath, previewed to 12 lines with
  `… +K more lines (ctrl+o to expand)`, and nothing wraps.
- **Shell output streams live.** A foreground `bash` call shows a live tail
  of its output under the call line while it runs, with its own elapsed
  clock — `⚒ Run pytest -q… (38s · esc to interrupt)` — so a long build reads
  as a build, not as the model thinking. When the command exits the tail is
  replaced by the usual result preview.
- **Provider errors are one box.** A 401, 404, 429, context overflow or an
  unreachable backend renders as a single red panel titled with the failure
  class and carrying the fix, never a traceback:

    ```text
    ╭─ ✗ auth failed (401) ────────────────────────────────────────────────╮
    │ HTTP 401 Unauthorized: invalid api key                               │
    │ → the API key looks invalid — re-run mantis setup, or /models to switch │
    ╰──────────────────────────────────────────────────────────────────────╯
    ```

## Deploy — bring your own GPU

`/deploy` turns an account on a GPU cloud into an OpenAI-compatible endpoint
serving any open-weight model, without leaving the session. It is the
terminal face of the [deploy feature](deploy.md) — the same operations as
`mantis-agent deploy` on the command line and the Deploy page of
`mantis serve`.

```text
› /deploy
╭─ mantis · deploy ────────────────────────────────────────────────────────────╮
│ providers ● RunPod Serverless                                                │
│           ○ Modal  run /deploy creds modal                                   │
│ deploys   1 deployment · 1 running · $2.49/h                                 │
│           ep-123  runpod · meta-llama/Llama-3.1-8B-Instruct · H100 80GB · r… │
╰───────────────────────────────────────── /deploy up · ls · connect · down ─╯
```

Three steps the first time — the empty panel prints them:

```text
› /deploy creds runpod            paste the cloud's API key (masked), validated on the spot
› /deploy gpus runpod             the GPU catalogue, cheapest first (--min-vram 40 to filter)
› /deploy up runpod Qwen/Qwen3-8B --gpu ADA_80_PRO
⚒ Deploy runpod · Qwen/Qwen3-8B · H100 80 GB (42s)
    pre-flight: 8.2B · BF16 · ~20 GB
    creating endpoint
    cold start… 503
(job #3 · /jobs to watch · the input stays live)
```

`up` runs as a background job, so you keep typing while the endpoint comes
up; its progress lines stream under the `⚒ Deploy …` call line in the same
in-place window a long `bash` call gets. When it finishes the block
collapses to the outcome and the exact connect line — and the full-screen
UI asks **Use it now?** so the session can switch immediately.
`/deploy connect <id>` verifies the endpoint and makes it the session's
model/backend exactly as `/model` would; `/deploy down <id>` asks first and
names the money it stops.

| Command | What it does |
|---|---|
| `/deploy` | Status panel: providers, live deployments, $/h |
| `/deploy providers` | Every adapter, its engines, and the env vars it needs |
| `/deploy creds <provider>` | Prompt for each credential (masked), save, validate, print the account |
| `/deploy gpus <provider> [--min-vram N]` | GPU catalogue, cheapest first |
| `/deploy models [query]` | Search open models on the HF Hub |
| `/deploy inspect <model>` | Pre-flight: params, dtype, VRAM estimate, vLLM servability |
| `/deploy up <provider> <model> --gpu <id> [--engine vllm] [--max-model-len N] [--tp N] [--min 0] [--max 1] [--idle 300] [--name X]` | Deploy as a background job |
| `/deploy ls` | Stored deployments (`--refresh` to poll the provider) |
| `/deploy status <id>` · `/deploy logs <id> [--tail N]` | Refresh one deployment · provider logs |
| `/deploy connect <id>` | Switch this session to the endpoint |
| `/deploy down <id>` | Tear it down (confirms) |

## Odds and ends

- `/export` writes the transcript; `/copy` copies the last reply.
- Vim editing mode, or `$EDITOR` for long prompts.
- Thinking blocks collapse to one line (`/thinking show|hide|collapse`);
  `/help` lists every command.
- Prefer a plain scrolling REPL? `MANTIS_CLASSIC=1 mantis` — everything on
  this page works there too except the live views (`/dash live`, the live
  shell tail).

## Input superpowers

| input | what happens |
|---|---|
| `! git status` | runs the shell command NOW, output lands in context (no model turn) |
| `# always use uv` | quick-saves a persistent memory note |
| `@file.py` | attaches the file's content inline |
| **Ctrl+V** | pastes an image/file from the clipboard (`[Image #1]` placeholder; warns if the model can't see images) |
| type while it works | message **queues** and fires when the turn ends (esc drops the queue) |
| **Esc Esc** | rewind picker — jump to an earlier message, files restored, edit & resend |
| **Tab / →** | accept the ghost **next-prompt suggestion** after a turn |
| ↑ / ↓ | prompt history, persistent across sessions |

## Custom slash commands & skills

- `./.mantis/commands/<name>.md` (or `~/.mantis-agent/commands/`) → `/name`,
  with `$ARGUMENTS` substitution. Frontmatter `description:` feeds the menu.
- Skills (`~/.mantis-agent/skills/<name>/SKILL.md`) are invocable directly:
  `/deploy-checklist staging`. `/skills` lists them. The agent can **create
  its own** — ask it to "save this as a skill".

## Autonomy

| command | what it does |
|---|---|
| `/goal <what you want>` | autopilot: plans via todos → executes → **adversarially verifies** (must earn `GOAL COMPLETE`) → reflects & saves lessons. 30-cycle cap, esc stops |
| `/swarm 3 <task>` | 3 parallel attempts in isolated git worktrees; a judge ranks the diffs and applies the winner |
| `/watch 30s pytest -q` | sentinel: the moment the command starts failing, the agent wakes and fixes it (edge-triggered) |
| `/loop 5m <prompt>` | re-run a prompt on an interval, never overlapping a running turn |
| `/cron every 30m <prompt>` | schedule a run that **outlives the session** — see [Scheduled runs](#scheduled-runs) |
| `/jobs` | background jobs — the model detaches long work with `task(run_in_background=true)`; you get a notification and the result auto-injects into context. `/jobs kill <id>` |

The model has two ways to keep an eye on something itself:

- **`monitor`** waits for *one* condition and returns — a port opening, a file
  appearing, a log line matching, a background shell exiting. Blocking, one
  answer.
- **`watch`** streams: it starts a long-running script and **every stdout line
  becomes a notification** in the conversation, so the agent reacts to a failing
  test or a new log error without being asked to go look. Lines printed within
  200ms coalesce into one message (a traceback stays one event), stderr goes to
  a log file without notifying, and a watch that fires too fast is stopped
  rather than allowed to flood the context. `persistent=true` runs it for the
  whole session. It shows up in `/jobs` with a `◈` glyph and an event count;
  stop it with `/jobs kill <id>`.

```
watch(command="tail -f dev.log | grep --line-buffered -E 'ERROR|Traceback'",
      description="errors in dev.log", persistent=true)
```

## Scheduled runs

`/loop` and `/watch` die with the session. `/cron` doesn't:

```
/cron every 30m triage new failures in the test suite
/cron                       # list what's scheduled
/cron rm a1b2c3d4
```

From the shell there's more: `mantis cron add "daily 09:00" "summarize
yesterday's commits"`, `mantis cron logs <id>` for a run's output,
`mantis cron run <id>` to fire one now, and **`mantis cron install`** once
— that registers a one-minute tick with launchd (macOS) or a systemd user
timer (Linux) so jobs fire with no terminal open.

Schedules: `every 30m` · `daily 09:00` · `mon 09:00` · `*/15 * * * *`.
Each job runs through the same headless path as `mantis -p`, in its own
directory, and **sandboxed by default** (`--no-sandbox` to opt out) —
unattended is exactly where "the user will approve it" stops being true.

## Sandboxing the shell

`/sandbox on` confines every shell command with the OS's own sandbox —
Seatbelt on macOS, bubblewrap on Linux. Writes are limited to the project
and temp; everything else on disk stays readable but read-only. It's a
kernel-level refusal, not a prompt, so it holds for `--godmode`, `/goal`
and CI runs where nobody is watching.

```bash
mantis -p "clean up the build" --sandbox            # confine this run
mantis -p "…" --sandbox --sandbox-no-network        # …and cut the network
```

```json
{"sandbox": {"enabled": true, "writableRoots": ["/extra/path"],
             "network": true, "failIfUnavailable": false}}
```

`/sandbox` shows what's in force. `failIfUnavailable` makes a missing
backend an error instead of a silent fallback to unconfined.

## A second opinion on the hard calls

Most turns of a long task are routine; a handful decide whether it works.
Pair a stronger model as an **advisor** and the agent consults it at exactly
those moments — before committing to an approach, when the same failure
keeps recurring, before declaring a hard task done:

```bash
mantis --advisor opus              # for this session
mantis -p "fix the flaky test" --advisor claude-opus-5 --godmode
```

```
/advisor opus     pair it (saved for next time)
/advisor          show the pairing
/advisor off      stop escalating
```

The advisor reads the whole conversation and returns **judgement, not
actions** — it gets no tools, so it can't race the main agent over the same
files. Each consult prints `⤴ consulting <model>` so a call to a second
model is never invisible, and a failed consult comes back as "proceed on
your own judgement" rather than taking the session down.

The part worth the flag: **the advisor doesn't have to live on the same
provider as your model.** It resolves its own base URL and key from the
catalog, independently of the session — so you can run Qwen on your own box
and escalate three decisions an hour to Opus, or drive a local DeepSeek with
a hosted Sonnet checking its plans. Set it permanently with
`{"advisorModel": "opus"}` in settings, or `MANTIS_ADVISOR` for CI.
Small local models don't get an advisor (a 7B that can't manage 22 tools
won't manage knowing when to escalate either).

## Big tool sets stay cheap

Every tool costs tokens on every request. Past a dozen tools mantis
**defers** the MCP ones: they're listed by name in the prompt, and the
model loads a schema with `tool_search` when it actually needs one. With
a 26-tool MCP server that's ~5,200 tokens per request down to ~790 — the
difference between "MCP works" and "MCP works on a 7B model". `/status`
shows how many are deferred; turn it off with
`{"toolSearch": {"mode": "off"}}` or force it with `"always"`.

## Sessions

- `/resume` opens an arrow-key picker (titles auto-generated after the first
  turn, `· N msgs · 2h ago`), and **replays the conversation** on resume
- `mantis -c` continues the last session; `mantis --resume <id>` from the shell
- `/rewind <n>` restores **code state too** — write tools checkpoint every
  file before touching it
- Crashes are detected: the next launch offers the unclean session's resume line
- The terminal tab is titled after the session (`✳ Retry Logic Refactor`)

## Everything else

`/dash` · `/status` · `/cost` · `/doctor` (live backend probe) ·
`/permissions` · `/mcp` · `/skills` · `/agents` · `/twin` · `/deploy` ·
`/update` · `/release-notes` — and `mantis serve` for
[the browser dashboard](dashboard.md) over the same state.

### Small local models

7B-class models automatically get a **slim 10-tool belt** and a compact
system prompt — a stable prompt prefix means Ollama's KV cache is reused, so
follow-up turns drop from ~19s to ~1s.
