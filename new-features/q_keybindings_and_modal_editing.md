# Configurable Keybindings and Modal Editing — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/tui_fullscreen.py` and a new `mantis_agent/keys/` package
**Objective:** Replace 77 hardcoded key bindings with a semantic action layer, contextual keymaps, user-configurable bindings with conflict detection, generated help, and complete default, Vim, Emacs, and accessibility presets.

## 1. Executive summary

`tui_fullscreen.py` contains 77 `@kb.add(...)` decorators. Each binds a literal key to an inline handler, scoped by a `prompt_toolkit` filter such as `_mcp_view_idle`, `_q_open`, `_effort_open`, or `_picker_open`. A representative cluster:

```python
@kb.add("up",   filter=_mcp_view_idle)
@kb.add("c-p",  filter=_mcp_view_idle)
@kb.add("down", filter=_mcp_view_idle)
@kb.add("c-n",  filter=_mcp_view_idle)
@kb.add("a",    filter=_mcp_view_idle)
@kb.add("e",    filter=_mcp_view_idle)
...
```

The pattern works and is readable in the small. At 77 instances across many views it produces four concrete problems.

**Bindings cannot be configured.** There is no way to change a key. A user whose terminal intercepts `Ctrl+P`, or who wants `j`/`k` navigation, or who has muscle memory from another tool, has no recourse short of editing source. `~/.mantis/keybindings.json` does not exist.

**There is no conflict detection.** Two bindings for the same key under overlapping filters are resolved by `prompt_toolkit`'s registration order, silently. Whether `escape` closes the effort picker or the model picker when both filters could match is an emergent property of decorator ordering, discoverable only by trying it.

**Help is hand-maintained.** Footer hints and help text are written separately from the bindings they describe. They drift, and the drift is invisible until a user follows a hint that no longer works. `tests/test_help_lines.py` exists precisely because this is a known problem — a test that checks help text is a test compensating for help not being generated.

**Navigation is inconsistent by construction.** Each view chooses its own keys. Some support `Ctrl+P`/`Ctrl+N`, some do not. Some accept `Escape` to close, some `q`. There is no enforced convention because there is no place where conventions could be enforced.

There is also no modal editing. `prompt_toolkit` supports Vi mode natively and the codebase does not enable or expose it, which is a notable gap for the terminal-native audience this product targets.

The fix is a layer of indirection that has paid for itself in every editor and terminal application that has adopted it: keys bind to **named semantic actions**, keymaps bind actions to keys per context, and everything else — help, conflict detection, configuration, presets — derives from that one table.

## 2. Goals

### User outcomes

- Rebind any key by editing `~/.mantis/keybindings.json`.
- Get a clear error naming both bindings when a configuration conflicts, instead of silent shadowing.
- Use Vim keys throughout, including modal editing in the prompt.
- Use `j`/`k` navigation in every list without per-view surprises.
- Press `?` in any context and see exactly the bindings that are active there, generated from the real table.
- Rely on consistent conventions: `Escape` always closes, `Enter` always confirms, `Tab` always cycles focus.
- Configure a chord (`Ctrl+X Ctrl+S`) for a rarely-used action.

### Engineering goals

- One action registry; every binding refers to an action, never to an inline handler.
- Contextual keymaps with a defined precedence order, resolved once and cached.
- Help generated from the table so it cannot drift.
- Migration is behavior-preserving: the default keymap reproduces today's bindings exactly.
- `prompt_toolkit` remains the input layer; this is a layer above it, not a replacement.
- Python 3.9–3.14, no new dependency.

### Success metrics

- Zero literal key strings outside keymap definitions, asserted by test.
- The default keymap reproduces all 77 current bindings, verified by comparison test.
- Conflict detection catches every same-key-same-context pair, tested exhaustively.
- Help output is generated; `test_help_lines.py` becomes a generation test rather than a drift test.
- Vim preset covers navigation, editing, and command modes.
- No measurable input-latency regression.

## 3. Non-goals

- Replacing `prompt_toolkit`.
- A full Vim implementation. The preset covers navigation, common motions, and modal editing — not macros, registers, or `:` ex commands beyond a small set.
- Mouse configuration, beyond enabling or disabling it.
- Per-plugin keybindings in the first release; the registry allows them later.
- Rebinding terminal-level keys the terminal intercepts before Mantis sees them (`Ctrl+S` flow control, `Ctrl+Z` suspend) — these are detected and reported, not fought.
- Classic `tui.py` bindings in the first phase; it migrates after the fullscreen UI proves the model.

## 4. Current integration points

- `mantis_agent/tui_fullscreen.py` — 77 `@kb.add` sites; filters `_mcp_view_idle`, `_q_open`, `_effort_open`, `_picker_open` and others; `Application(layout=layout, key_bindings=kb, full_screen=False, erase_when_done=True)` at 4064; `_Keys.Any` and `Keys.Any` catch-alls at 3238 and 3382.
- `mantis_agent/tui.py` — its own input handling, migrating in a later phase.
- `mantis_agent/term/overlay.py` — the overlay stack and focus model from `o_tui_cli_ux_accessibility.md`; keymap contexts align with overlay states.
- `mantis_agent/term/capabilities.py` — terminal capability detection informs which keys are actually deliverable.
- `tests/test_help_lines.py` — the existing help contract.
- `tests/test_fullscreen_pty.py` — PTY tests that press keys; they become the migration's regression suite.
- `mantis_agent/settings.py` — keybinding configuration loading.
- `mantis_agent/paths.py` — `~/.mantis/keybindings.json`.

## 5. Action registry

An action is a named, described, context-scoped operation.

```python
@dataclass(frozen=True)
class Action:
    id: str                      # "list.next", "overlay.close"
    title: str                   # "Next item" — shown in help
    contexts: tuple[str, ...]    # where it applies
    handler: Callable
    repeatable: bool = False     # accepts a numeric prefix
    hidden: bool = False         # excluded from help but bindable
    destructive: bool = False    # may require confirmation
```

Namespaced identifiers, grouped by area:

```text
input.submit          input.newline         input.cancel
input.history.prev    input.history.next    input.clear
input.paste           input.attach

nav.up                nav.down              nav.left           nav.right
nav.top               nav.bottom            nav.page.up        nav.page.down
nav.next.sibling      nav.prev.sibling

focus.next            focus.prev            focus.input        focus.rail

overlay.close         overlay.close.all     overlay.confirm
overlay.filter        overlay.help

list.select           list.toggle           list.expand        list.collapse

session.interrupt     session.exit          session.new        session.resume
session.rewind        session.compact

activity.open         activity.stop         activity.retry     activity.message
activity.transcript

view.jobs             view.agents           view.workflows     view.mcp
view.skills           view.plugins          view.team

edit.mode.normal      edit.mode.insert      edit.mode.visual
edit.word.next        edit.word.prev        edit.line.start    edit.line.end
edit.delete.word      edit.delete.line      edit.undo          edit.redo
```

Every one of the 77 current bindings maps to exactly one action. The mapping is the migration.

## 6. Contexts and keymaps

### Contexts

A context is a named UI state, mirroring the existing filters:

```text
global              always active, lowest precedence
input               the prompt is focused
input.insert        modal: insert mode
input.normal        modal: normal mode
input.visual        modal: visual mode
overlay             any overlay is open
overlay.picker      model picker
overlay.effort      effort picker
overlay.mcp         MCP view
overlay.jobs        jobs view
overlay.workflows   workflow view
overlay.help
rail                the activity rail is focused
streaming           a response is streaming
permission          a permission prompt is showing
```

Contexts form a stack. The active set is the stack from most to least specific, and resolution walks it in that order. `overlay.picker` beats `overlay` beats `global`. The current filter functions become context predicates, one to one, which keeps the migration mechanical.

### Keymap format

```json
{
  "version": 1,
  "extends": "default",
  "bindings": {
    "global": {
      "c-c": "session.interrupt",
      "c-d": "session.exit",
      "?": "overlay.help"
    },
    "overlay": {
      "escape": "overlay.close",
      "up": "nav.up",
      "down": "nav.down",
      "c-p": "nav.up",
      "c-n": "nav.down",
      "enter": "list.select",
      "/": "overlay.filter"
    },
    "overlay.mcp": {
      "a": "mcp.add",
      "e": "mcp.edit",
      "d": "mcp.delete",
      "t": "mcp.test"
    },
    "input": {
      "enter": "input.submit",
      "s-enter": "input.newline",
      "c-v": "input.paste"
    }
  },
  "unbind": ["input:c-r"]
}
```

- `extends` composes presets; a user file typically extends `default` and overrides a handful of keys.
- `unbind` removes an inherited binding, which is necessary and is otherwise impossible with pure merging.
- Key syntax: `c-` control, `s-` shift, `a-` alt/meta, `escape`, `enter`, `tab`, `space`, `f1`–`f12`, and literal characters. Chords are space-separated: `"c-x c-s"`.

### Resolution

1. Build the active context stack from the UI state.
2. Walk from most to least specific; the first binding for the pressed key wins.
3. Chords: after a prefix match, enter a pending state with a timeout (default 1 s) and a visible indicator; an unmatched continuation aborts and reports.
4. Numeric prefixes accumulate for `repeatable` actions (`3j`).
5. Unmatched keys in an `input` context insert text; elsewhere they are ignored, with `Keys.Any` handlers becoming explicit `text.insert` and `list.typeahead` actions rather than catch-alls.

Resolution is computed once per context-stack change and cached, so the input path is a dictionary lookup.

## 7. Conflict detection

Conflicts are found at load time, not at press time.

Detected:

- **Same key, same context, two actions** — hard error naming both, with file and line.
- **Shadowing across contexts** — a `global` binding unreachable because a more specific context always binds the same key: a warning, since it is often intentional.
- **Chord prefix collisions** — `"c-x"` bound directly and `"c-x c-s"` also bound: an error, since the direct binding makes the chord unreachable.
- **Unknown action ids** — error, with near-match suggestions.
- **Action bound in a context it does not declare** — error.
- **Terminal-intercepted keys** — `c-s`, `c-q`, `c-z`, `c-c` in some configurations: a warning explaining the terminal will consume it, with a suggested alternative. Detected via the capability layer.
- **Unreachable actions** — a non-hidden action with no binding in any context: a warning, so a feature is not shipped inaccessible.

`mantis keys check` runs all of this against the effective configuration and exits non-zero on errors, so a keymap can be validated in CI.

## 8. Presets

### `default`

Reproduces today's 77 bindings exactly. Verified by a test that enumerates current bindings and asserts equality with the preset. This is what makes the migration provably behavior-preserving.

Beyond that, it enforces conventions the current code approximates but does not guarantee:

- `Escape` closes the topmost overlay in every context.
- `Enter` confirms; `Tab` cycles focus.
- Arrow keys and `Ctrl+P`/`Ctrl+N` navigate in every list, not just some.
- `?` opens context help everywhere.
- `/` filters in every list view.

### `vim`

Extends `default` and adds modal editing.

- **Normal mode** by default in the prompt when `vim` is active; `i`/`a`/`o` enter insert; `Escape` returns to normal.
- Navigation: `h j k l`, `w b e`, `0 ^ $`, `gg G`, `Ctrl+D`/`Ctrl+U`.
- Editing: `x`, `dd`, `dw`, `cw`, `D`, `C`, `u`, `Ctrl+R`, `p`, `y`.
- Visual mode: `v`, `V`, with motions and `d`/`y`/`c`.
- Counts: `3j`, `2dw`.
- List views: `j`/`k` navigate, `gg`/`G` jump, `/` searches, `n`/`N` cycle matches.
- A minimal `:` command line mapping to Mantis commands, so `:q` and `:w` behave sensibly rather than being unbound surprises.

Mode is displayed in the status line via the `mode` segment from `p_statusline_themes_output_styles.md`, using the token system rather than a hardcoded color. This is the one place a modal UI genuinely requires visual feedback, and it must not be color-only — the indicator shows the mode name.

`prompt_toolkit`'s built-in Vi mode handles the prompt buffer; the action layer handles everything outside it. Delegating rather than reimplementing text editing is the right division and avoids reinventing a well-tested component.

### `emacs`

Extends `default` with `Ctrl+A`/`Ctrl+E`, `Alt+B`/`Alt+F`, `Ctrl+K`, `Ctrl+Y`, `Ctrl+W`, `Alt+D`, and `Ctrl+X` chord prefixes. `prompt_toolkit`'s Emacs mode covers the buffer; the action layer covers navigation.

### `accessible`

Extends `default` for screen-reader use, coordinated with `o_tui_cli_ux_accessibility.md`:

- No single-letter bindings outside text entry — a screen reader's own commands frequently use them.
- Every action reachable by an explicit, announced key or through the command palette.
- No chords, which are difficult to discover and to announce.
- `Tab`-based navigation preferred over arrow keys.
- Every binding has a spoken description in the action's `title`.

## 9. Help generation

Help is derived from the table; it is never written by hand.

`?` shows bindings for the active context stack:

```text
Keys — MCP view

  navigation
    ↑ / ctrl+p          Previous server
    ↓ / ctrl+n          Next server
    → / enter           Show details

  actions
    a                   Add server
    e                   Edit server
    t                   Test connection
    d / x               Delete server
    r                   Reconnect

  global
    ?                   This help
    escape              Close
    ctrl+c              Interrupt
```

Requirements:

- Grouped by action namespace; multiple keys for one action shown together.
- Only actions active in the current context stack; hidden actions excluded.
- Footer hints generated from the same table, so the strings the PTY tests synchronize on are guaranteed accurate.
- `mantis keys list [--context C] [--format json]` dumps the effective keymap for documentation and debugging.
- Documentation tables are generated at build time, so `docs/` cannot drift from the code.

`tests/test_help_lines.py` changes character: instead of asserting that hand-written help matches reality, it asserts that generation produces the expected structure. The drift class of bug disappears.

## 10. Configuration

```json
{
  "keys": {
    "preset": "default",
    "file": null,
    "chordTimeoutMs": 1000,
    "vimMode": false,
    "showModeIndicator": true,
    "warnOnTerminalIntercept": true
  }
}
```

`~/.mantis/keybindings.json` holds the user keymap; a project may supply `.mantis/keybindings.json`, which **may only rebind, never add new actions**, and never bind a `destructive` action to a key that a default preset uses for something benign. A cloned repository silently remapping `Enter` to a destructive action would be a genuine hazard, and the restriction is cheap.

Environment: `MANTIS_KEYS_PRESET`, `MANTIS_KEYS_FILE`, `MANTIS_VIM=1`.

## 11. Errors

```text
KeymapError                      (base)
├── KeymapSchemaError            # malformed file, with line
├── KeymapVersionError
├── UnknownActionError           # with near-match suggestions
├── ActionContextError           # action bound outside its declared contexts
├── BindingConflictError         # same key, same context
├── ChordPrefixConflictError
├── ExtendsCycleError
├── UnknownKeyError              # unparseable key syntax
└── ProjectKeymapDeniedError     # project file adding actions or destructive rebinds
```

A malformed user keymap falls back to `default` with a visible error naming the file and line. Being unable to type into the application because of a typo in a config file is not an acceptable failure mode.

## 12. Surface

```text
/keys                       active bindings for the current context
/keys all                   the full effective keymap
/keys check                 conflicts and warnings
/keys preset <name>
/keys reload
mantis keys list [--json]
mantis keys check [--file F]
mantis keys export          write the effective map as a starting user file
```

`mantis keys export` matters for adoption: a user customizing bindings should start from a complete, correct file rather than authoring one from documentation.

## 13. Delivery phases

### Phase 0 — Inventory and harness

1. Enumerate all 77 bindings with their filters and handlers; produce the action mapping.
2. Build a comparison test capturing today's effective bindings as the baseline.
3. Confirm the context stack maps one-to-one onto existing filters.
4. Prototype resolution and measure input latency.
5. Confirm `prompt_toolkit` Vi/Emacs modes can be delegated to for buffer editing.

**Exit:** complete mapping; baseline captured; latency acceptable.

### Phase 1 — Action registry

1. Add `keys/actions.py` with the registry and the `Action` dataclass.
2. Extract all 77 handlers into named actions, leaving bindings as-is.
3. Declare contexts per action.
4. Add the unreachable-action check.
5. Assert no behavior change against the baseline.

**Exit:** every handler is a named action; behavior identical.

### Phase 2 — Keymaps and resolution

1. Add `keys/keymap.py` with the schema, `extends`, and `unbind`.
2. Add `keys/context.py` with the stack and predicates from the existing filters.
3. Add `keys/resolve.py` with precedence, chords, counts, and caching.
4. Ship the `default` preset reproducing all 77 bindings; comparison test passes.
5. Replace the `@kb.add` decorators with a single dispatch bridging to `prompt_toolkit`.

**Exit:** bindings come from data; the literal-key assertion passes; no behavior change.

### Phase 3 — Configuration and conflicts

1. Load `~/.mantis/keybindings.json` and project overrides.
2. Implement every conflict and warning check.
3. Implement terminal-intercept detection via the capability layer.
4. Implement project restrictions and `ProjectKeymapDeniedError`.
5. Add `mantis keys check`, `list`, `export`, and `/keys` commands.

**Exit:** users can rebind; conflicts are caught at load with actionable errors.

### Phase 4 — Help generation

1. Generate context help from the registry.
2. Generate footer hints from the same source.
3. Convert `test_help_lines.py` to a generation test.
4. Generate documentation tables at build time.
5. Add spoken descriptions for the accessible preset.

**Exit:** help cannot drift; PTY footer hints are guaranteed accurate.

### Phase 5 — Presets

1. Ship `vim` with modal editing, delegating buffer editing to `prompt_toolkit`.
2. Add the mode indicator through the status-line token system.
3. Ship `emacs`.
4. Ship `accessible`.
5. Add `/keys preset` with instant switching.

**Exit:** four working presets; modal editing usable end to end.

### Phase 6 — Classic UI and hardening

1. Migrate `tui.py` input handling onto the action layer.
2. Adversarial review: project keymap abuse, chord state confusion, resolution edge cases.
3. Fuzz keymap files and key-sequence parsing.
4. PTY tests across all presets.
5. Remove experimental gating.

## 14. Testing strategy

### Unit

- Key syntax parsing: every modifier, special key, chord, and malformed input.
- Keymap schema validation, `extends` composition, `unbind`, cycle detection.
- Resolution precedence across the full context stack.
- Chord matching: prefix, completion, timeout, abort, indicator state.
- Numeric prefixes for repeatable and non-repeatable actions.
- Every conflict class: same-key-same-context, shadowing, chord prefix, unknown action, wrong context, terminal intercept, unreachable action.
- Project keymap restrictions: adding actions rejected, destructive rebinds rejected.
- Malformed user file falls back to `default` with a reported error.
- Help generation for each context; hidden actions excluded.

### Comparison

- The `default` preset reproduces all 77 baseline bindings exactly — the migration's central test.
- Each preset's full effective map snapshot-tested.

### PTY

- Every navigation and action key in every view, per preset.
- Vim modal transitions: normal → insert → visual → normal.
- Counts and motions in normal mode.
- Chord entry, completion, timeout, and abort with a visible indicator.
- `Escape` closes the topmost overlay from every nested state.
- Preset switch mid-session applies immediately.
- Generated footer hints match the active keymap.

### Accessibility

- `accessible` preset has no single-letter bindings outside text entry.
- Every non-hidden action is reachable.
- No chords in the accessible preset.
- Every action has a non-empty spoken title.

### Performance

- Resolution latency per keypress (cached lookup).
- Context-stack change cost.
- Keymap load and validation time.
- No regression against the pre-migration input path.

## 15. Documentation

- `docs/guides/keybindings.md` — presets, customizing, key syntax, chords, conflicts.
- `docs/guides/vim-mode.md` — supported motions, modes, differences from Vim.
- `docs/reference/keybindings.md` — generated tables per context and preset.
- `docs/api/keys.md` — action registry for embedders and future plugin bindings.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 16. File-level implementation map

New:

- `mantis_agent/keys/__init__.py`
- `mantis_agent/keys/actions.py` — registry, `Action`
- `mantis_agent/keys/keymap.py` — schema, loading, `extends`, `unbind`
- `mantis_agent/keys/context.py` — context stack and predicates
- `mantis_agent/keys/resolve.py` — precedence, chords, counts, cache
- `mantis_agent/keys/conflicts.py`
- `mantis_agent/keys/help.py` — generation
- `mantis_agent/keys/parse.py` — key syntax
- `mantis_agent/keys/presets/default.json`
- `mantis_agent/keys/presets/vim.json`
- `mantis_agent/keys/presets/emacs.json`
- `mantis_agent/keys/presets/accessible.json`
- `tests/test_keys_parse.py`
- `tests/test_keys_keymap.py`
- `tests/test_keys_resolution.py`
- `tests/test_keys_conflicts.py`
- `tests/test_keys_default_parity.py`
- `tests/test_keys_help_generation.py`
- `tests/test_keys_presets_pty.py`
- `docs/guides/keybindings.md`
- `docs/guides/vim-mode.md`

Modified:

- `mantis_agent/tui_fullscreen.py` — 77 decorators replaced by one dispatch; handlers become actions
- `mantis_agent/tui.py` — migrated in Phase 6
- `mantis_agent/term/overlay.py` — context stack integration
- `mantis_agent/term/capabilities.py` — terminal-intercept detection
- `mantis_agent/term/statusline.py` — mode indicator segment
- `mantis_agent/settings.py` — keys configuration
- `mantis_agent/cli.py` — `keys` command family
- `mantis_agent/paths.py` — keybindings file location
- `tests/test_help_lines.py` — becomes a generation test
- `tests/test_fullscreen_pty.py` — preset-aware
- `tests/public_api_surface.txt` — intentional update

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Migrating 77 bindings changes behavior | Baseline comparison test; `default` preset must match exactly |
| Resolution adds input latency | Cached per context stack; measured against baseline |
| Chord state confuses users | Visible pending indicator, timeout, abort message |
| A bad user keymap makes the app unusable | Fall back to `default` with a visible error; `mantis keys check` |
| Project keymap becomes an attack | Rebind-only, no new actions, no destructive rebinds of benign default keys |
| Terminal intercepts a bound key | Detected via capabilities, warned at load with an alternative suggested |
| Vim mode is a half-implementation | Buffer editing delegated to `prompt_toolkit`; scope documented explicitly |
| Help generation misses context nuance | Generated per context stack; PTY tests assert hints match the active map |
| Accessible preset conflicts with screen-reader keys | No single letters outside text entry; no chords; manual verification |
| `Keys.Any` catch-alls resist migration | Converted to explicit `text.insert` / `list.typeahead` actions |
| Classic UI diverges | Migrated in Phase 6 onto the same registry |
| Presets drift from actions | Unreachable-action check; snapshot tests per preset |

## 18. Acceptance checklist

- [ ] All 77 bindings are named actions; no inline handler remains bound directly.
- [ ] No literal key string exists outside keymap definitions, asserted by test.
- [ ] The `default` preset reproduces current bindings exactly, proven by comparison test.
- [ ] Contexts form a stack with defined precedence; resolution is cached.
- [ ] Chords work with a timeout, a visible indicator, and clean aborts.
- [ ] Numeric prefixes work for repeatable actions.
- [ ] Every conflict class is detected at load with an actionable error.
- [ ] Terminal-intercepted keys are warned about with alternatives.
- [ ] Unreachable actions are reported.
- [ ] Users can rebind via `~/.mantis/keybindings.json`; `extends` and `unbind` work.
- [ ] Project keymaps may only rebind and cannot introduce destructive surprises.
- [ ] A malformed keymap falls back to `default` with a clear error.
- [ ] Help and footer hints are generated from the registry and cannot drift.
- [ ] `vim`, `emacs`, and `accessible` presets ship and are PTY-tested.
- [ ] Modal editing works with a non-color-only mode indicator.
- [ ] `mantis keys check` exits non-zero on conflicts.
- [ ] `ruff check` and the full pytest suite pass.

## 19. Recommended implementation order

1. **Capture the baseline first.** A test that enumerates today's 77 effective bindings is the specification for the entire migration; without it, "behavior-preserving" cannot be demonstrated.
2. **Extract actions second, leaving bindings untouched.** This is a large but mechanical refactor that changes nothing observable and makes every later step small.
3. **Introduce keymaps and resolution third, shipping only the `default` preset.** At this point the system is data-driven and provably identical, which is the right moment to stop and release.
4. **Add help generation fourth, before user configuration.** Once users can rebind, hand-written help is immediately wrong; generating it first means customization never produces stale hints. It also converts `test_help_lines.py` from a drift detector into a generation test, which is a real reduction in maintenance.
5. **Add user configuration and conflict detection fifth**, with `mantis keys export` in the same release so users start from a correct file.
6. **Add presets sixth.** `vim` is the most requested and the most work; delegate buffer editing to `prompt_toolkit` rather than reimplementing motions.
7. **Add the `accessible` preset alongside `vim`**, coordinated with `o_tui_cli_ux_accessibility.md`'s screen-reader mode — the two are only useful together.
8. **Migrate `tui.py` last.** The fullscreen UI is where users spend their time and where the model should be proven first.
