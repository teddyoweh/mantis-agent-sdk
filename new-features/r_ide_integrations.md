# IDE Integrations — Extensive Implementation Plan

**Status:** Proposed
**Target:** A new `mantis_agent/ide/` bridge plus a separately-versioned VS Code extension
**Objective:** Give Mantis the editor's context — active file, selection, diagnostics, open tabs — and give the editor Mantis's output as native diffs, decorations, permission prompts, and an activity view, over one transport-neutral protocol rather than an editor-specific integration.

## 1. Executive summary

Mantis has no IDE integration. It also has `mantis_agent/lsp.py` (referenced by the `lsp` tool in `tool_preview.TOOL_VERBS` as `("Look up", ("symbol",))`), which means the agent can already query language servers directly — a useful capability that is *not* the same thing as knowing what the developer is looking at.

The gap is bidirectional and both directions matter.

**Editor → agent.** The agent does not know which file is open, what is selected, which line the cursor is on, what the language server is currently complaining about, or which files the developer has been editing. A developer types "fix this" and Mantis has to ask what "this" is. That single missing signal — the active selection — accounts for a large share of the friction in a terminal-only workflow.

**Agent → editor.** Mantis writes files directly. The developer sees the change only after it lands, in the editor's file-changed notification. There is no native diff to review before applying, no inline decoration showing what the agent is about to touch, no permission prompt in the editor, no way to see agent activity without switching to the terminal.

The critical architectural decision is what the extension talks to. Building a VS Code extension that shells out to `mantis` and parses its stdout would work and would be a dead end: JetBrains, Neovim, Zed, and Emacs would each need their own bespoke path, and each would break on output changes.

Instead, the extension is a **client of the session protocol** defined in `m_session_event_api_and_remote_surfaces.md`. That protocol already specifies a versioned envelope, capability negotiation, subscription with replay, controller leases, and authentication over a Unix socket with peer-credential verification. An IDE is exactly the client it was designed for. This plan adds the *editor-specific operations* to that protocol and builds one reference extension against it.

This ordering has a consequence worth stating plainly: this plan is blocked on the session protocol. Building an IDE integration first would mean building a second protocol and then throwing one away.

## 2. Goals

### User outcomes

- Select code, type "explain this," and have the agent know what "this" is.
- See proposed edits as native diffs before they apply, and accept or reject per hunk.
- Approve permission prompts in the editor instead of switching to a terminal.
- See agent activity — running jobs, subagents, workflows — in an editor panel.
- Have the agent see live diagnostics and fix what the language server is reporting.
- Open a Mantis session scoped to the current workspace with one command.
- Click a file:line reference in agent output and jump there.

### Engineering goals

- Editor-agnostic bridge; the VS Code extension is one client, not the interface.
- Extend the session protocol with IDE operations rather than defining a second protocol.
- No editor SDK dependency in `mantis_agent`. The Python side speaks the protocol; the TypeScript side speaks VS Code.
- Editor-supplied data is untrusted input, handled like every other untrusted source in this plan set.
- The extension ships and versions separately; a version mismatch degrades gracefully with a clear message.
- Zero cost when no IDE is attached.
- Python 3.9–3.14.

### Success metrics

- Active selection reaches the agent within 100 ms of a request.
- Diff application is atomic per file; a rejected hunk leaves the file untouched.
- Permission prompts appear in the editor within 300 ms and the terminal reflects the decision.
- No editor-supplied path can escape the workspace root.
- Extension crash or disconnect never breaks the underlying session.
- Protocol version mismatch produces an actionable message, never a partial connection.

## 3. Non-goals

- Reimplementing the TUI inside the editor. The editor is a context provider and a review surface; conversation may live in a panel but the terminal remains first-class.
- Shipping JetBrains, Neovim, Zed, or Emacs clients in the first release. The protocol makes them possible; this plan builds one reference client.
- Replacing `lsp.py`. The agent keeps querying language servers directly; the IDE additionally supplies *live* editor state.
- An editor-hosted model runtime.
- Remote development beyond what the session protocol already handles.
- Inline completion. This is an agent integration, not a completion provider.

## 4. Current integration points

- `mantis_agent/protocol/` and `mantis_agent/daemon/` — from `m_session_event_api_and_remote_surfaces.md`. The transport, auth, negotiation, subscription, replay, and lease model. **This plan depends on that one.**
- `mantis_agent/activity/` — the event stream the editor panel renders.
- `mantis_agent/permissions.py` — `AskerFn`; the editor becomes an asker.
- `mantis_agent/lsp.py` — existing language-server querying; complementary.
- `mantis_agent/session_tree.py` — sessions scoped to a workspace; `list_sessions`, `load_for_resume`.
- `mantis_agent/tools.py` — edit tools whose output becomes a proposed diff.
- `mantis_agent/tool_preview.py` — `TOOL_VERBS` shapes what the editor renders for each call.
- `mantis_agent/paths.py` — socket location and workspace resolution.
- `mantis_agent/settings.py` — IDE configuration layer.

## 5. Architecture

```text
┌── VS Code ─────────────────────────────────┐
│  extension (TypeScript)                    │
│   ├─ context provider  (editor → agent)    │
│   ├─ diff/decoration   (agent → editor)    │
│   ├─ permission UI                         │
│   ├─ activity view                         │
│   └─ terminal launcher                     │
└──────────────┬─────────────────────────────┘
               │ session protocol over Unix socket (NDJSON)
┌──────────────▼─────────────────────────────┐
│  mantisd  (daemon)                          │
│   └─ ide/ bridge: editor ops, context store │
└──────────────┬─────────────────────────────┘
               │
        live session(s)
```

The extension holds no agent logic. It reports editor state, renders protocol events, and forwards user decisions. Every judgment stays in the agent.

### Attachment

- The extension connects to the existing daemon socket. If no daemon is running it offers to start one; it never starts one silently, since a background process is a decision the user should make.
- Peer-credential verification on the Unix socket is the authentication, exactly as the protocol specifies. No token in a VS Code setting.
- The extension declares `caps: ["ide", "stream", "control"]` at handshake and receives the negotiated intersection. An editor talking to an older daemon gets a clear "this daemon does not support IDE operations" rather than partial behavior.

## 6. Protocol additions

New operations, added to the existing envelope with no new framing:

**Editor → daemon (context)**

| Op | Payload |
|---|---|
| `ide.hello` | editor name, version, extension version, workspace roots |
| `ide.context.update` | active file, selection, cursor, visible range, dirty state |
| `ide.tabs.update` | open tabs with paths and pinned state |
| `ide.diagnostics.update` | diagnostics by file, severity-filtered |
| `ide.workspace.update` | roots added or removed |
| `ide.selection.explicit` | user deliberately sent a selection ("explain this") |

**Daemon → editor (rendering)**

| Op | Payload |
|---|---|
| `ide.diff.propose` | file, hunks, edit id |
| `ide.diff.status` | applied, rejected, superseded |
| `ide.decorate` | file, ranges, kind (reading, editing, finding) |
| `ide.permission.request` | the decision record from the permission layer |
| `ide.notify` | level, message, optional action |
| `ide.reveal` | file, line, column |
| `ide.activity` | activity envelope events, filtered to this workspace |

**Editor → daemon (control)**

| Op | Payload |
|---|---|
| `ide.diff.respond` | edit id, accept-all / reject-all / per-hunk selection |
| `ide.permission.respond` | allow_once / allow_session / deny |
| `session.prompt` | existing protocol op, reused |
| `node.action` | existing protocol op, reused |

Context updates are **debounced and coalesced** (default 150 ms). A developer moving the cursor generates continuous events; only the latest matters.

## 7. Context model

### What is sent

| Signal | Default | Notes |
|---|---|---|
| Active file path | Yes | Workspace-relative |
| Selection text | On request only | Not streamed continuously |
| Selection range | Yes | Cheap, no content |
| Cursor position | Yes | |
| Visible range | Yes | |
| Open tabs | Yes | Paths only |
| Recently edited files | Yes | Paths and timestamps |
| Diagnostics | Errors and warnings | Configurable severity |
| Dirty buffer contents | **No** by default | See below |
| Git branch and status | Yes | Already available to the agent |

### Dirty buffers

The hardest correctness question: the editor's buffer may differ from disk.

Rules:

- The agent reads from disk by default. Ambiguity about which version is authoritative causes edits against stale content.
- When a file is dirty and the agent is about to read or edit it, the editor is asked to either save or supply the buffer. Silently proceeding is not an option — this is the case that produces lost work.
- `ide.context.update` carries a `dirty` flag per file so the agent can raise the question before doing work.
- With `sendDirtyBuffers: true`, buffer contents are supplied for files the agent explicitly reads, subject to a size cap. Never streamed proactively.
- Before applying a diff to a dirty file, the extension prompts to save or discard. An edit landing on disk under a dirty buffer is silent data loss.

### Context budget

Editor context enters the model's context and must be bounded like everything else:

- Diagnostics capped (default 50, highest severity first) with the omitted count stated.
- Open tabs capped (default 30).
- Selection capped (default 32 KB) with truncation reported.
- Paths workspace-relative to save tokens and avoid leaking home-directory structure.
- Total editor context budget (default 2,000 tokens), reported in `/status` alongside the skills and memory budgets from their respective plans.

### Untrusted input

Editor data is not user speech. File paths, diagnostic messages, and selection contents are derived from the repository and from language servers, both of which process attacker-controllable content — a diagnostic message can contain arbitrary text from a source file.

- All editor-supplied strings are neutralized with the shared neutralizer: control characters, ANSI, bidi, framing markers, length caps.
- Editor context is injected in a labeled envelope declaring it observed state, not instructions.
- A diagnostic message saying "ignore previous instructions" is data, and the injection corpus covers it.
- Paths are validated against workspace roots after resolution; a path outside them is refused, not clamped.

## 8. Diffs and edits

### Proposal flow

Today an edit tool writes the file. With an IDE attached and `reviewEdits` enabled:

1. The agent's edit produces a proposal instead of a write.
2. `ide.diff.propose` sends the hunks; the editor shows a native diff.
3. The developer accepts all, rejects all, or selects hunks.
4. `ide.diff.respond` returns the decision.
5. The agent applies exactly the accepted hunks and receives the outcome as its tool result.

Requirements:

- **The permission layer still runs.** A diff proposal is not an approval; the edit tool call passes `check_permission` as it always does. The IDE review is an additional gate, never a replacement. Two gates that each think the other is authoritative is how both get bypassed.
- Application is atomic per file: temp write, fsync, rename.
- If the file changed on disk between proposal and response, the proposal is stale — refuse, report, and let the agent re-read. Never apply a stale hunk.
- Timeout (default 5 minutes) on an unanswered proposal, resolving to reject with a clear tool result, so the agent is never blocked forever on a developer who walked away.
- Partial acceptance is reported to the agent explicitly, including which hunks were rejected, so it can adapt rather than assume success.
- Without an IDE attached, or with `reviewEdits: false`, behavior is exactly as today.

### Decorations

- Files the agent is currently reading: subtle gutter marker.
- Ranges the agent is editing: highlight.
- Findings from a review: diagnostic-style squiggles in a Mantis diagnostic collection, cleared when resolved.
- Decorations are advisory and rate-limited; they must never make the editor feel busy.

## 9. Permissions in the editor

- `ide.permission.request` carries the `PermissionDecisionRecord` from `f_permission_policy_engine_and_auto_mode.md`, so the editor shows the rule, its source, the tool, the target, and — for shell commands — the per-segment decomposition.
- The prompt renders natively with the same three choices the terminal offers.
- **The controller lease governs.** Only the lease holder is prompted. Two surfaces answering one prompt is the race the lease exists to prevent, and the IDE is exactly the second surface that makes it likely.
- The terminal shows that a prompt was answered in the editor, and by which client.
- Remote-client restrictions apply: an IDE may only narrow the permission mode and may never approve a sandbox escape.
- If the editor disconnects with a prompt outstanding, the prompt returns to the terminal rather than being lost.

## 10. Activity view

An editor panel rendering the activity tree from `a_activity_graph_and_inline_rail.md`, filtered to the workspace:

```text
MANTIS
  ▾ session  fix auth flow                        running  2m14s
    ▾ turn 7
        Read  mantis_agent/permissions.py
      ▾ task  Explore                              running   18s
          Search "check_permission"  → 12 matches
    ▸ job #3  pytest -q                            running  1m02s
  ▾ findings (2)
      permissions.py:412  session allows bypass dangerous check
```

- Same event stream, same node kinds, same actions (stop, retry, message) subject to the lease.
- Clicking a file:line reveals it.
- Findings integrate with the editor's problems panel so they participate in existing workflows.

## 11. The extension

Separately versioned, published to the marketplace, containing no agent logic.

### Commands

```text
Mantis: Start Session (workspace)
Mantis: Send Selection
Mantis: Explain Selection
Mantis: Fix Diagnostic
Mantis: Open Terminal Session
Mantis: Show Activity
Mantis: Review Pending Edits
Mantis: Attach to Daemon
Mantis: Disconnect
```

### Settings

```json
{
  "mantis.enabled": true,
  "mantis.socketPath": null,
  "mantis.autoStartDaemon": false,
  "mantis.sendDiagnostics": "errors-and-warnings",
  "mantis.sendDirtyBuffers": false,
  "mantis.reviewEdits": true,
  "mantis.maxDiagnostics": 50,
  "mantis.maxOpenTabs": 30,
  "mantis.decorations": true,
  "mantis.activityView": true
}
```

`mantis.autoStartDaemon` defaults to `false`. Starting a background process automatically on editor launch is a decision that belongs to the user.

### Deep links

A `mantis://` URI handler enables "open this session in the editor" from the terminal or a notification.

Security requirements, because URI handlers are a classic injection surface:

- Only `session` and `activity` actions; strictly enumerated.
- Parameters are validated as opaque IDs matching a strict pattern; no paths, no commands, no shell arguments.
- Opening a session **reveals** it; it never sends a prompt, changes a mode, or starts work.
- Links from outside the machine are refused — the daemon confirms the session id exists locally before the editor acts on it.
- A malformed or unknown link shows an error and does nothing.

## 12. Configuration (Python side)

```json
{
  "ide": {
    "enabled": true,
    "reviewEdits": true,
    "diffTimeoutSeconds": 300,
    "contextBudgetTokens": 2000,
    "maxDiagnostics": 50,
    "maxOpenTabs": 30,
    "maxSelectionBytes": 32768,
    "debounceMs": 150,
    "allowDirtyBuffers": false,
    "decorations": {"enabled": true, "maxPerFile": 50, "rateLimitMs": 200},
    "deepLinks": {"enabled": true, "actions": ["session", "activity"]}
  }
}
```

Environment: `MANTIS_IDE=0|1`, `MANTIS_IDE_NO_REVIEW=1`.

## 13. Errors

```text
IDEError                          (base)
├── IDEProtocolVersionError
├── IDECapabilityError            # op not negotiated
├── IDEWorkspaceMismatchError     # path outside declared roots
├── IDEPathEscapeError
├── IDEStaleProposalError         # file changed since proposal
├── IDEProposalTimeoutError
├── IDEDirtyBufferError           # unsaved changes block an edit
├── IDEContextTooLargeError       # truncated, reported
├── IDEDisconnectedError
├── IDELeaseRequiredError         # control op without the lease
└── IDEDeepLinkInvalidError
```

Every error is reportable to the editor as a notification with an action where one exists.

## 14. Delivery phases

### Phase 0 — Design spike

1. Confirm the session protocol's envelope carries IDE operations with no framing changes.
2. Prototype a minimal VS Code extension connecting over the Unix socket.
3. Measure context update frequency and validate the debounce window.
4. Prototype native diff rendering and per-hunk acceptance.
5. Decide the dirty-buffer policy definitively and test it against real workflows.

**Exit:** extension connects and streams context; diff rendering proven; dirty-buffer policy settled.

### Phase 1 — Context, read-only

1. Add `ide/` bridge with the context store and `ide.hello` / `ide.context.update` / `ide.tabs.update`.
2. Implement neutralization, path validation against workspace roots, and the context budget.
3. Inject editor context into the agent in a labeled envelope.
4. Extension: context provider and `Send Selection` / `Explain Selection`.
5. Extension: `Open Terminal Session` scoped to the workspace.

**Exit:** the agent knows what the developer is looking at; nothing writes to the editor yet.

### Phase 2 — Activity and diagnostics

1. Stream activity events filtered to the workspace.
2. Extension: activity tree view with drill-down and reveal.
3. Diagnostics ingestion with severity filtering and caps.
4. `Fix Diagnostic` command.
5. Findings into the editor's problems panel.

**Exit:** the developer sees agent activity without leaving the editor.

### Phase 3 — Diffs

1. Implement proposal generation from edit tools behind `reviewEdits`.
2. Implement `ide.diff.propose` / `respond` with per-hunk selection.
3. Implement staleness detection, atomic application, and partial-acceptance reporting.
4. Implement timeout-to-reject.
5. Implement dirty-buffer prompting before application.

**Exit:** edits are reviewed natively; no stale or partial write can corrupt a file.

### Phase 4 — Permissions and control

1. Implement `ide.permission.request` / `respond` with the decision record.
2. Enforce the controller lease; return orphaned prompts to the terminal.
3. Implement `session.prompt` and `node.action` from the editor.
4. Enforce remote-client restrictions on mode and sandbox escape.
5. Implement decorations with rate limiting.

**Exit:** the editor is a full control surface, bounded by the same rules as any remote client.

### Phase 5 — Deep links, packaging, hardening

1. Implement the `mantis://` handler with strict validation.
2. Package and publish the extension; implement version negotiation and graceful degradation.
3. Adversarial review: path escape, deep-link injection, diagnostic-message injection, lease bypass.
4. Fuzz IDE protocol payloads.
5. Document the bridge so other editors can be built against it.

## 15. Testing strategy

### Unit (Python)

- IDE operation encode/decode within the existing envelope.
- Capability negotiation; un-negotiated ops refused.
- Path validation: outside workspace, traversal, symlink, absolute, UNC.
- Context budget: diagnostics, tabs, selection caps with reported truncation.
- Neutralization of paths, diagnostic messages, and selection content.
- Debounce and coalescing correctness.
- Proposal lifecycle: propose, accept, reject, per-hunk, stale, timeout.
- Atomic application and partial-acceptance reporting.
- Lease enforcement on every control op.
- Deep-link validation for every malformed shape.

### Integration

- Extension connects, negotiates, streams context; agent receives the selection.
- Diff proposed, partially accepted, applied correctly, agent told what was rejected.
- File modified on disk between propose and respond → stale, refused, agent re-reads.
- Permission prompt answered in the editor; terminal reflects it.
- Editor disconnects with a prompt outstanding → returns to the terminal.
- Activity events filtered to the workspace.
- Daemon restart while the extension is attached → reconnect and replay.

### End-to-end

- Full workflow: select code, ask for a fix, review the diff, accept, tests pass.
- Fix-diagnostic flow from the problems panel.
- Two workspace windows attached to separate sessions.
- Version mismatch shows an actionable message.
- Extension crash does not affect the running session.

### Security

- Editor-supplied path outside workspace roots is refused, not clamped.
- Diagnostic message containing injection text is neutralized (corpus-driven).
- Deep link with a path, command, or shell argument is rejected.
- Deep link cannot start work or change a mode.
- IDE client cannot set `bypass` or approve a sandbox escape.
- Control op without the lease is refused.
- Dirty buffer cannot be silently overwritten.
- Socket connection from a different UID is refused.

### Performance

- Context update latency and coalescing under rapid cursor movement.
- Activity streaming at high event rates.
- Decoration rendering cost on a large file.
- Zero daemon cost with no IDE attached.

## 16. Documentation

- `docs/guides/ide.md` — installing, connecting, commands, settings.
- `docs/guides/ide-editing.md` — diff review, dirty buffers, partial acceptance, what happens without the extension.
- `docs/guides/ide-security.md` — untrusted editor data, path validation, deep-link rules, lease semantics.
- `docs/api/ide-protocol.md` — the IDE operations, so other editors can be built.
- Extension README and marketplace listing.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 17. File-level implementation map

New (Python):

- `mantis_agent/ide/__init__.py`
- `mantis_agent/ide/ops.py` — protocol operations
- `mantis_agent/ide/context.py` — editor context store, budget, neutralization
- `mantis_agent/ide/proposals.py` — diff proposals, staleness, application
- `mantis_agent/ide/decorations.py`
- `mantis_agent/ide/deeplinks.py`
- `mantis_agent/ide/workspace.py` — root validation
- `tests/test_ide_ops.py`
- `tests/test_ide_context.py`
- `tests/test_ide_paths.py`
- `tests/test_ide_proposals.py`
- `tests/test_ide_permissions.py`
- `tests/test_ide_deeplinks.py`
- `tests/test_ide_security.py`
- `docs/guides/ide.md`
- `docs/api/ide-protocol.md`

New (extension, separate repository or `editors/vscode/`):

- `editors/vscode/src/extension.ts`
- `editors/vscode/src/client.ts` — protocol client
- `editors/vscode/src/context.ts`
- `editors/vscode/src/diff.ts`
- `editors/vscode/src/permissions.ts`
- `editors/vscode/src/activity.ts`
- `editors/vscode/src/deeplinks.ts`
- `editors/vscode/package.json`

Modified:

- `mantis_agent/protocol/ops.py` — IDE operations
- `mantis_agent/daemon/server.py` — IDE bridge registration
- `mantis_agent/permissions.py` — editor as `AskerFn`
- `mantis_agent/tools.py` — edit tools produce proposals when enabled
- `mantis_agent/activity/projections.py` — workspace filtering
- `mantis_agent/settings.py` — IDE configuration
- `mantis_agent/paths.py` — workspace resolution
- `tests/public_api_surface.txt` — intentional update

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Building a second protocol | Extension is a session-protocol client; IDE ops extend the existing envelope |
| Dirty buffers cause lost work | Disk is authoritative; prompt to save before editing; never silently overwrite |
| Stale diff application corrupts files | Content-hash staleness check; refuse and re-read |
| IDE review is mistaken for permission approval | Permission layer always runs; documented and tested |
| Two surfaces answer one prompt | Controller lease; orphaned prompts return to the terminal |
| Editor data injects instructions | Shared neutralizer, labeled envelope, injection corpus including diagnostics |
| Path escape via editor-supplied paths | Validation against workspace roots after resolution; refuse, never clamp |
| Deep links become an attack surface | Enumerated actions, opaque-ID validation, reveal-only, local existence check |
| Extension version skew | Explicit negotiation, graceful degradation, actionable message |
| Editor context inflates every request | Budget with caps and reported truncation; relative paths |
| Extension crash breaks the session | Extension holds no agent state; session runs independently |
| Auto-starting a daemon surprises users | `autoStartDaemon` defaults false; explicit offer |
| Decorations make the editor feel slow | Rate limiting, per-file caps, disableable |

## 19. Acceptance checklist

- [ ] The extension is a session-protocol client; no second protocol exists.
- [ ] Peer-credential auth over the Unix socket; no token in editor settings.
- [ ] Capability negotiation; un-negotiated operations refused.
- [ ] Active file, selection, cursor, tabs, and diagnostics reach the agent within budget.
- [ ] All editor-supplied strings are neutralized and injected in a labeled envelope.
- [ ] Paths are validated against workspace roots and refused, not clamped.
- [ ] Diff proposals render natively with per-hunk acceptance.
- [ ] The permission layer runs regardless of IDE review.
- [ ] Stale proposals are refused; application is atomic per file.
- [ ] Partial acceptance is reported to the agent explicitly.
- [ ] Unanswered proposals time out to reject.
- [ ] Dirty buffers are never silently overwritten.
- [ ] Permission prompts respect the controller lease; orphaned prompts return to the terminal.
- [ ] IDE clients cannot set `bypass` or approve a sandbox escape.
- [ ] Deep links are enumerated, validated, reveal-only, and locally verified.
- [ ] Extension crash or disconnect never breaks the session.
- [ ] Zero cost with no IDE attached.
- [ ] `ruff check` and the full pytest suite pass.

## 20. Recommended implementation order

1. **Do not start until `m_session_event_api_and_remote_surfaces.md` has landed its daemon, subscription, and lease model.** Everything here is a client of that. Starting earlier means writing a protocol that gets deleted.
2. **Ship read-only context first** — active file, selection, tabs — with `Send Selection` and `Explain Selection`. This is the majority of the value, carries almost no risk, and validates the transport with a real client.
3. **Add the activity view second.** Also read-only, and it makes the daemon's event stream visible to its first real consumer, which surfaces protocol gaps early.
4. **Add diagnostics third**, with neutralization from the first commit — diagnostic text is derived from source files and is the least obvious untrusted surface here.
5. **Add diff proposals fourth.** This is the first feature that writes, and staleness detection and atomic application must both exist before it ships, not after.
6. **Add permissions fifth**, and only after the lease is proven under the session protocol — an unsequenced permission surface produces double-answered prompts.
7. **Add deep links last**, with the strict validation in the same commit. A URI handler is the one part of this plan reachable from outside the machine.
8. Publish `docs/api/ide-protocol.md` alongside the VS Code release so a Neovim or JetBrains client can be built by someone else without reading the extension's source.
