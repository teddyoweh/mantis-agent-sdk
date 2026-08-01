# The mantis terminal

`mantis` is a Claude-Code-style coding agent that ships in the same pip
package as the SDK. Point it at a project and it reads, writes, edits,
greps, and runs shell commands — driving whatever model you set up.

```bash
pip install mantis-agent-sdk
mantis setup     # detects your machine, pulls the best local coding model
mantis           # start coding
```

Resume where you left off:

```bash
mantis --continue    # or -c: reopens your most recent conversation
```

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

## Odds and ends

- `/export` writes the transcript; `/copy` copies the last reply.
- Vim editing mode, or `$EDITOR` for long prompts.
- Thinking blocks render dimmed; `/help` lists every command.
- Prefer a plain scrolling REPL? `MANTIS_CLASSIC=1 mantis`.

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

## Sessions

- `/resume` opens an arrow-key picker (titles auto-generated after the first
  turn, `· N msgs · 2h ago`), and **replays the conversation** on resume
- `mantis -c` continues the last session; `mantis --resume <id>` from the shell
- `/rewind <n>` restores **code state too** — write tools checkpoint every
  file before touching it
- Crashes are detected: the next launch offers the unclean session's resume line
- The terminal tab is titled after the session (`✳ Retry Logic Refactor`)

## Everything else

`/status` · `/cost` · `/doctor` (live backend probe) · `/permissions` ·
`/mcp` · `/skills` · `/agents` · `/twin` · `/update` · `/release-notes`

### Small local models

7B-class models automatically get a **slim 10-tool belt** and a compact
system prompt — a stable prompt prefix means Ollama's KV cache is reused, so
follow-up turns drop from ~19s to ~1s.
