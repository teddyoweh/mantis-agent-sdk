# TUI and CLI UX Accessibility — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/tui.py` (6,647 lines), `mantis_agent/tui_fullscreen.py` (4,232 lines), `cli.py`, `headless.py`
**Objective:** Make the terminal interface work correctly and legibly across terminals, widths, color capabilities, and assistive technologies — by extracting a terminal-capability layer, a layout model, and a rendering contract out of two very large files.

## 1. Executive summary

Mantis has two terminal interfaces. `tui.py` is the classic line-oriented UI at 6,647 lines. `tui_fullscreen.py` is the live UI at 4,232 lines, built on `prompt_toolkit` with `full_screen=False`, `erase_when_done=True`, and ANSI-formatted content passed through `ANSI(...)`. Both work, and the fullscreen picker and prompt are what users actually interact with.

Their size is the root problem. At ten thousand combined lines with rendering, state, input handling, and business logic interleaved, every UX property that should be centrally enforced is instead re-decided at each call site. The evidence is concrete and greppable:

**Terminal width is queried at eight separate call sites.** `shutil.get_terminal_size((80, 24))` appears at `tui.py:2755`, `tui.py:6076`, and `tui_fullscreen.py:1367,1519,1628,1770,1914,1968,4138,4151`. Each caller re-derives its own layout arithmetic from it. There is no single source of layout truth, which means a resize is handled inconsistently, narrow terminals are handled per-widget, and any change to spacing conventions must be made in ten places.

**`NO_COLOR` is not supported.** A grep across the package finds no reference. `NO_COLOR` is a widely-honored convention, and its absence is a straightforward standards gap that also affects users who need high contrast or who pipe output through tools that mangle escapes. Colors are hardcoded as ANSI 256 escapes with a comment noting they *"work in Terminal.app — no truecolor needed"* — a reasonable pragmatic choice, but one that hardcodes a palette with no way to override it.

**Terminal capability detection is `isatty()` and nothing else.** `sys.stdin.isatty() and sys.stdout.isatty()` gates interactive behavior in several places. There is no detection of color depth, Unicode support, box-drawing capability, hyperlink support, bracketed paste, or whether the terminal is a screen reader's virtual terminal. Everything is assumed to work, and where it does not, output is mangled with no fallback.

**There is no screen-reader path.** A `prompt_toolkit` application that redraws a live region is close to unusable with a screen reader: the reader either re-announces the whole region on every repaint or announces nothing. There is no linear, announcement-oriented mode.

**CLI errors are human text.** `headless.py` has `_ERROR_TEXT`, and failures surface as prose. There are no stable exit codes or machine-readable error objects, so scripts cannot distinguish "the model refused" from "the API key is missing" from "the network failed."

There is also a known, documented test-reliability problem: the fullscreen PTY tests race on the boot summary and pass alone but fail in the full suite. The mitigation in use is to synchronize on footer hints instead. That is a symptom of the same root cause — no deterministic "UI is ready" signal — and this plan should fix the cause, not just keep working around it.

## 2. Goals

### User outcomes

- The UI is legible and correct at 60 columns, at 200 columns, and while being resized.
- `NO_COLOR=1` produces readable monochrome output; low-capability terminals get ASCII instead of mangled box drawing.
- Screen-reader users can operate Mantis through a linear, announced mode.
- Every overlay closes with `Escape`, every list navigates the same way, and focus is always visible.
- Command discovery works: typing `/` shows what exists, with descriptions and fuzzy matching.
- Long-running work is announced without stealing focus or scrolling away the prompt.
- Scripts can branch on a stable exit code and a machine-readable error object.

### Engineering goals

- Extract capability detection, layout, and rendering into focused modules. **Do not grow `tui.py` or `tui_fullscreen.py`.** They should shrink.
- One source of terminal width and height, subscribed to rather than polled.
- One rendering contract shared by classic, fullscreen, and headless output.
- Behavior-preserving refactors first, with the existing tests as the contract.
- Give the PTY tests a deterministic readiness signal so the boot-summary race is fixed rather than worked around.
- Python 3.9–3.14; no new required dependency.

### Success metrics

- `shutil.get_terminal_size` appears exactly once in the package.
- `NO_COLOR=1` output contains zero ANSI escapes, asserted by test.
- Rendering is correct at 40, 60, 80, 120, and 200 columns for every view, asserted by snapshot tests.
- Screen-reader mode passes a manual checklist with VoiceOver and NVDA.
- Fullscreen PTY tests pass reliably in the full suite with no footer-hint workaround.
- No measurable render-latency regression.

## 3. Non-goals

- Rewriting either TUI. This is extraction and hardening, not replacement.
- A GUI, a web UI, or a TUI framework migration.
- Changing the visual identity beyond what capability fallbacks require — `p_statusline_themes_output_styles.md` owns presentation.
- Keybinding configuration — `q_keybindings_and_modal_editing.md` owns that.
- Full WCAG conformance, which does not map cleanly onto terminals. The target is "usable with a screen reader and legible without color."
- Internationalization and RTL layout, beyond not corrupting such text.

## 4. Current integration points

- `mantis_agent/tui.py` — classic UI; `isatty` gates at 991, 2086, 2121, 2162; width at 2755, 6076; viewers for jobs, workflows, sessions, and rewind.
- `mantis_agent/tui_fullscreen.py` — live UI; `Application(layout=…, key_bindings=kb, full_screen=False, erase_when_done=True)` at 4064; `_MODE_ANSI` palette at 36; ANSI-formatted content throughout; width at eight sites; job-completion rendering at 4138 and 4151.
- `mantis_agent/cli.py` — argument parsing and command dispatch.
- `mantis_agent/headless.py` — `_ERROR_TEXT`, `_dump`, `_final_text`, `run_print`.
- `mantis_agent/tool_preview.py` — `TOOL_VERBS`, `tool_arg_preview`, `tool_call_preview`, `tool_result_preview`; the shared one-line rendering already extracted, and the model for further extraction.
- `mantis_agent/workflow_view.py` (542 lines) — a viewer that becomes a projection under `a_activity_graph_and_inline_rail.md`.
- `mantis_agent/clipboard.py` — paste and attachment paths that need terminal capability awareness (bracketed paste).
- `mantis_agent/setup_wizard.py` — `isatty` gate at 352.
- `mantis_agent/watch.py` — sets `TERM="dumb"` for subprocesses, which is the right instinct and a precedent for capability handling.
- `tests/test_fullscreen_pty.py`, `tests/test_help_lines.py` — the existing UI contract.

## 5. Terminal capability layer

Add `mantis_agent/term/capabilities.py` — one detection pass at startup, cached, overridable.

```python
@dataclass(frozen=True)
class TerminalCaps:
    is_tty: bool
    color: Literal["none", "16", "256", "truecolor"]
    unicode: bool                 # can render non-ASCII
    box_drawing: bool             # can render U+2500 block
    emoji_width: bool             # wcwidth agrees with the terminal
    hyperlinks: bool              # OSC 8
    bracketed_paste: bool
    alternate_screen: bool
    synchronized_output: bool     # DEC 2026, reduces flicker
    mouse: bool
    screen_reader: bool
    width: int
    height: int
    term: str
    program: str                  # TERM_PROGRAM
```

Detection order, most authoritative first:

1. **Explicit overrides** — `MANTIS_TERM_*` environment variables and settings. Always win, because detection is heuristic and users know their terminal.
2. **`NO_COLOR`** — set to anything non-empty forces `color="none"`. Non-negotiable and checked before anything else color-related.
3. **`FORCE_COLOR`** — forces color on when not a TTY, for CI logs that render ANSI.
4. **`isatty()`** — false means `color="none"`, no mouse, no alternate screen, no bracketed paste.
5. **`COLORTERM`** — `truecolor` or `24bit` implies truecolor.
6. **`TERM`** — `dumb` disables essentially everything; `*-256color` implies 256.
7. **`TERM_PROGRAM`** — a small table for known terminals where `TERM` under-reports.
8. **Locale** — `LANG`/`LC_ALL` containing UTF-8 implies Unicode.
9. **Screen-reader hints** — `MANTIS_SCREEN_READER=1`, or platform signals where reliably available. Never guessed from `TERM`.

Detection must be **cheap and non-interactive**. No terminal queries that write escape sequences and wait for a response: those hang on terminals that do not answer and corrupt output when the response is not consumed. Where a capability cannot be determined safely, assume the conservative value and let the user override.

`MANTIS_TERM_CAPS=json` prints the detected capabilities, which is the first thing to ask for in any rendering bug report.

## 6. Layout model

Add `mantis_agent/term/layout.py` with one owner of size.

```python
class TerminalSize:
    """Single source of terminal dimensions. Subscribed to, not polled."""
    def current(self) -> tuple[int, int]: ...
    def subscribe(self, fn) -> None: ...
```

Requirements:

- Reads `shutil.get_terminal_size` exactly once per change, not per render.
- Handles `SIGWINCH` on POSIX; falls back to polling on a timer where signals are unavailable.
- Debounced (default 50 ms) so a dragged resize does not cause a repaint storm.
- All ten current call sites become `layout.width` / `layout.height`.
- A lint rule or test asserts `get_terminal_size` appears only in this module.

### Breakpoints

Replace ad-hoc arithmetic with named breakpoints:

| Name | Columns | Behavior |
|---|---|---|
| `tiny` | < 60 | Single column, no side-by-side, truncated labels, no rail detail |
| `narrow` | 60–79 | Single column, abbreviated headers |
| `normal` | 80–119 | Current default layout |
| `wide` | ≥ 120 | Side-by-side panels where a view supports them |

Every view declares its minimum viable width. Below it, the view renders a compact summary plus a note that a wider terminal shows more — rather than corrupting.

### Text measurement

Terminal width is not string length. Add `term/measure.py`:

- `display_width(s)` using `wcwidth`-equivalent logic implemented in-package (no new dependency): zero for combining marks, two for East Asian wide and most emoji, zero for the ANSI escapes already embedded in the strings the fullscreen UI passes to `ANSI(...)`.
- `truncate(s, width, ellipsis="…")` that never splits a grapheme cluster or an escape sequence — truncating mid-escape leaks raw bytes to the terminal and corrupts everything after it.
- `pad(s, width)` accounting for display width.
- Emoji width disagreement between terminals is real; where `emoji_width` is uncertain, prefer ASCII markers, which is also what the box-drawing fallback does.

## 7. Rendering contract

Add `mantis_agent/term/render.py` as the single styling seam.

- **Semantic styles, not literal escapes.** `style("error", text)`, not `\x1b[31m`. `_MODE_ANSI` becomes the default mapping in a palette table, which `p_statusline_themes_output_styles.md` then makes themeable.
- **Degradation is automatic.** With `color="none"`, styles resolve to plain text plus, where meaning would be lost, a textual marker (`ERROR:` rather than red).
- **Glyph fallbacks.** One table mapping semantic glyphs to Unicode and ASCII forms: `●`/`*`, `▸`/`>`, `✓`/`ok`, `⚠`/`!`, box-drawing to `-`/`|`/`+`.
- **Meaning is never carried by color alone.** Status is a glyph plus a word; a red dot with no label fails for colorblind users and in monochrome.
- **Escapes never cross a truncation boundary**, enforced by `measure.truncate`.
- **Hyperlinks** use OSC 8 when supported and fall back to plain paths.

Existing spacing conventions must be preserved. The extraction is behavior-preserving by default; visual changes belong to the themes plan, not here.

## 8. Interaction

### Focus

- Exactly one focused region at a time, always visibly indicated with a marker, not only a color.
- `Tab` / `Shift+Tab` cycle focusable regions; `Escape` returns to the input.
- Focus never moves on its own. New output, a completed job, or an arriving notification must not steal focus — this is the single most disruptive UI behavior and the rule is absolute.
- Focus is announced in screen-reader mode.

### Overlays

- One overlay stack with uniform behavior: `Escape` closes the top, `Ctrl+C` closes all and returns to input.
- Consistent navigation across every list: arrows, `Home`/`End`, `PgUp`/`PgDn`, type-to-filter.
- Overlays never open on their own from background events; they show a rail indicator instead.
- Every overlay shows its available keys in a footer — which is what the PTY tests already synchronize on, making it a documented contract rather than a workaround.

### Command discovery

- `/` opens a palette with fuzzy matching over names, aliases, and descriptions.
- Grouped by category; recently used first.
- Argument hints inline; `/help <command>` for detail.
- Unknown commands suggest near matches rather than only erroring.
- Discovery covers commands from plugins and skills, namespaced per `k_skills_commands_policy_and_shell_blocks.md`.

### Notifications

- Transient messages appear in a dedicated region, never inline in the transcript where they scroll away and never as an overlay that steals focus.
- Severity via glyph and word.
- Bounded queue with coalescing; a chatty watch cannot flood.
- OS notifications for terminal states when the terminal is unfocused, subject to configuration.

### Input

- Bracketed paste enabled where supported, so a multi-line paste is one event rather than a hundred keystrokes — which currently risks triggering per-character handlers.
- Paste over the size cap prompts rather than silently truncating.
- `Ctrl+C` cancels the current operation; twice in quick succession exits, with the second press requiring confirmation while work is running.
- Interrupt during streaming stops the stream cleanly without leaving partial state.

## 9. Screen-reader mode

A live-redrawing region is fundamentally hostile to screen readers. The fix is a distinct mode, not an adjustment.

Enabled by `MANTIS_SCREEN_READER=1` or `accessibility.screenReader: true`.

Behavior:

- **Linear output only.** No live-updating regions, no spinners, no progress bars that repaint. Output appends; it never rewrites.
- Status changes announce once, as text: "Running pytest.", "Job 3 finished, 12 seconds.", "Permission needed for bash."
- No alternate screen, no cursor repositioning, no `erase_when_done`.
- Overlays render as sequential prompts with numbered choices rather than navigable lists.
- Tables render as labeled key-value lines; column alignment is meaningless to a reader.
- Every glyph has a text equivalent; no meaning is glyph-only.
- Announcement verbosity is configurable — `low` for terminal states only, `normal` for state changes, `high` for tool calls.
- The rail from `a_activity_graph_and_inline_rail.md` becomes an on-demand summary command rather than a persistent repainting line.

Testability: assert that in screen-reader mode the output stream contains no cursor-movement or erase sequences, and that every state transition emits exactly one announcement line.

## 10. Machine-readable CLI

### Exit codes

```text
0   success
1   agent completed with an error result
2   usage or configuration error
3   authentication or credential error
4   permission denied (fail-closed, non-interactive)
5   budget or limit exceeded
6   provider or network error
7   cancelled or interrupted
8   internal error
```

### Error objects

With `--output-format json`, errors emit a structured object rather than prose:

```json
{"type": "error", "code": "auth_missing_key", "message": "No API key for provider 'anthropic'.",
 "hint": "Set ANTHROPIC_API_KEY or run: mantis setup", "docs": "https://mantisagent.cc/docs/setup",
 "retryable": false}
```

Requirements:

- Stable `code` values, documented and covered by a snapshot test so they cannot drift silently.
- `_ERROR_TEXT` in `headless.py` becomes the human rendering of the same table.
- Errors go to stderr; results to stdout, so piping works.
- `--quiet` suppresses progress but never errors.
- Progress output is disabled automatically when stdout is not a TTY.

## 11. The PTY test race

The fullscreen PTY tests pass in isolation and fail in the full suite, racing on the boot summary. The working mitigation is to synchronize on footer hints. Fix the cause:

- Emit a deterministic readiness marker when the application has completed its first full render — an OSC sequence or a sentinel line consumed by the test harness, invisible in normal use.
- Provide `MANTIS_TUI_SYNC=1` enabling a synchronous, deterministic render mode for tests: no timers, no debounce, explicit frame stepping.
- Give the test harness a `wait_for_ready()` helper rather than each test choosing its own anchor.
- Keep footer hints as a documented UI contract — they are genuinely useful — but stop depending on them for synchronization.
- Assert the marker fires exactly once per application start.

This is a small change that removes a recurring source of flakiness and makes every future UI test easier to write correctly.

## 12. Configuration

```json
{
  "ui": {
    "color": "auto",
    "unicode": "auto",
    "glyphs": "auto",
    "hyperlinks": "auto",
    "mouse": false,
    "notifications": {"os": true, "inline": true, "maxQueue": 20},
    "resizeDebounceMs": 50,
    "minWidth": 40,
    "commandPalette": true
  },
  "accessibility": {
    "screenReader": false,
    "verbosity": "normal",
    "announceToolCalls": false,
    "reduceMotion": false,
    "highContrast": false
  },
  "cli": {"outputFormat": "text", "quiet": false, "progress": "auto"}
}
```

Environment:

- `NO_COLOR`, `FORCE_COLOR` — standard, honored
- `MANTIS_SCREEN_READER=1`
- `MANTIS_TERM_CAPS=json`
- `MANTIS_TUI_SYNC=1`
- `MANTIS_UI_ASCII=1`

## 13. Errors

```text
UIError                          (base)
├── TerminalTooSmallError        # below minWidth; degrade, not crash
├── UnsupportedCapabilityError
├── RenderOverflowError          # internal; caught and degraded
└── InputDecodeError             # malformed input sequence
```

None of these should reach the user as a traceback. A rendering failure degrades to plain text and logs; a UI bug must never take down a session with work in progress.

## 14. Delivery phases

### Phase 0 — Audit and harness

1. Inventory every `get_terminal_size`, `isatty`, and raw escape sequence.
2. Build a snapshot-testing harness rendering every view at five widths with three capability profiles.
3. Capture current output as the baseline so refactors are provably behavior-preserving.
4. Fix the PTY readiness marker and sync mode first — every later phase depends on reliable UI tests.
5. Manual screen-reader baseline with VoiceOver and NVDA; document what is unusable.

**Exit:** snapshot harness in place; PTY tests reliable in the full suite; accessibility baseline documented.

### Phase 1 — Capability layer

1. Add `term/capabilities.py` with the full detection order.
2. Honor `NO_COLOR` and `FORCE_COLOR`.
3. Add `MANTIS_TERM_CAPS=json`.
4. Add overrides via settings and environment.
5. Route existing `isatty` gates through the layer.

**Exit:** `NO_COLOR=1` produces zero escapes; capabilities are inspectable.

### Phase 2 — Layout and measurement

1. Add `term/layout.py` with `SIGWINCH` handling and debounce.
2. Replace all ten `get_terminal_size` call sites; add the single-use assertion.
3. Add `term/measure.py` with display width, escape-safe truncation, and padding.
4. Add breakpoints and per-view minimum widths.
5. Snapshot-test every view at every breakpoint.

**Exit:** one width source; correct rendering from 40 to 200 columns; resize handled.

### Phase 3 — Rendering contract

1. Add `term/render.py` with semantic styles and the glyph table.
2. Migrate `_MODE_ANSI` and raw escapes to semantic styles.
3. Implement automatic degradation for color, Unicode, and box drawing.
4. Ensure no meaning is color-only.
5. Add OSC 8 hyperlinks with fallback.

**Exit:** rendering is centralized and degrades correctly; snapshots unchanged at the default profile.

### Phase 4 — Interaction

1. Unify the overlay stack, focus model, and list navigation.
2. Enforce no-focus-stealing.
3. Add the command palette with fuzzy matching and plugin/skill commands.
4. Add the notification region with coalescing.
5. Enable bracketed paste; fix interrupt semantics.

**Exit:** consistent interaction across every view; no overlay opens itself.

### Phase 5 — Screen reader and CLI

1. Implement screen-reader mode with linear output and announcements.
2. Add verbosity levels and glyph text equivalents.
3. Add stable exit codes and structured error objects.
4. Split stdout and stderr correctly; auto-disable progress off-TTY.
5. Snapshot-test error codes.

**Exit:** usable with VoiceOver and NVDA; scripts can branch on codes.

### Phase 6 — Hardening

1. Fuzz input sequences and paste handling.
2. Test on Terminal.app, iTerm2, Alacritty, kitty, Windows Terminal, tmux, screen, and `TERM=dumb`.
3. Verify RTL and CJK text does not corrupt layout.
4. Re-run the accessibility checklist.
5. Measure render latency against the baseline.

## 15. Testing strategy

### Unit

- Capability detection for every combination of `NO_COLOR`, `FORCE_COLOR`, `TERM`, `COLORTERM`, `TERM_PROGRAM`, `LANG`, and `isatty`.
- Override precedence.
- `display_width` for ASCII, CJK, emoji, combining marks, and embedded escapes.
- `truncate` never splits a grapheme or an escape sequence.
- Breakpoint selection and per-view minimum widths.
- Style resolution and degradation at each color level.
- Glyph fallback table completeness — every semantic glyph has an ASCII form.
- Exit-code mapping for every failure class.
- Error object schema and code stability.

### Snapshot

- Every view at 40, 60, 80, 120, 200 columns.
- Each of three capability profiles: full, no-color, ASCII-only.
- Screen-reader mode output for each view.
- Baseline comparison proving Phases 1–3 are behavior-preserving at the default profile.

### PTY

- Readiness marker fires exactly once.
- `MANTIS_TUI_SYNC=1` produces deterministic frames.
- Full suite passes with no footer-hint synchronization.
- Resize mid-session re-renders correctly.
- Bracketed paste of 500 lines is one event.
- `Ctrl+C` semantics: cancel, then confirm-to-exit.

### Accessibility

- Screen-reader mode emits no cursor-movement or erase sequences.
- One announcement per state transition, no duplicates.
- Every glyph has a text equivalent in output.
- No status distinguished by color alone.
- Manual VoiceOver and NVDA checklist.

### Compatibility

- `TERM=dumb`, tmux, screen, and each named terminal emulator.
- CJK and RTL content in transcripts and file paths.
- Very long single-line output.
- Terminal narrower than `minWidth`.

## 16. Documentation

- `docs/guides/terminal.md` — supported terminals, capability detection, overrides, troubleshooting.
- `docs/guides/accessibility.md` — screen-reader mode, verbosity, `NO_COLOR`, high contrast, known limitations stated honestly.
- `docs/guides/cli.md` — exit codes, output formats, error objects, scripting.
- `docs/api/terminal.md` — `TerminalCaps`, layout, render API for embedders.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 17. File-level implementation map

New:

- `mantis_agent/term/__init__.py`
- `mantis_agent/term/capabilities.py`
- `mantis_agent/term/layout.py`
- `mantis_agent/term/measure.py`
- `mantis_agent/term/render.py`
- `mantis_agent/term/glyphs.py`
- `mantis_agent/term/overlay.py` — overlay stack and focus
- `mantis_agent/term/notify.py`
- `mantis_agent/term/palette_cmd.py` — command palette
- `mantis_agent/term/screenreader.py`
- `mantis_agent/cli_errors.py` — codes and error objects
- `tests/test_term_capabilities.py`
- `tests/test_term_measure.py`
- `tests/test_term_layout.py`
- `tests/test_term_render.py`
- `tests/test_ui_snapshots.py`
- `tests/test_screen_reader.py`
- `tests/test_cli_exit_codes.py`
- `tests/snapshots/**`
- `docs/guides/terminal.md`
- `docs/guides/accessibility.md`

Modified:

- `mantis_agent/tui.py` — width, styles, overlays through the new modules; should shrink
- `mantis_agent/tui_fullscreen.py` — same; readiness marker; sync mode
- `mantis_agent/workflow_view.py` — rendering contract
- `mantis_agent/tool_preview.py` — measurement and glyphs
- `mantis_agent/cli.py` — exit codes
- `mantis_agent/headless.py` — structured errors, stdout/stderr split
- `mantis_agent/setup_wizard.py` — capability layer
- `mantis_agent/clipboard.py` — bracketed paste awareness
- `tests/test_fullscreen_pty.py` — use `wait_for_ready()`
- `tests/public_api_surface.txt` — intentional update

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Refactoring 10k lines regresses the UI | Snapshot baseline captured first; every phase proven behavior-preserving at the default profile |
| Escape-aware truncation is subtly wrong | Dedicated tests including escapes at boundaries; never split a sequence |
| Capability detection is wrong on some terminal | Conservative defaults, explicit overrides, `MANTIS_TERM_CAPS=json` for diagnosis |
| Interactive terminal queries hang | No query-and-wait detection; environment and heuristics only |
| Screen-reader mode diverges and rots | Snapshot tests for it; announcement-count assertions |
| `SIGWINCH` handling causes repaint storms | Debounced; subscription rather than polling |
| Emoji width differs across terminals | ASCII preferred where width is uncertain; fallback table |
| PTY tests remain flaky | Readiness marker plus sync mode; footer hints retained as contract, not synchronization |
| Command palette conflicts with input | Only on a leading `/`; escapable; disableable |
| Notifications steal focus | Absolute no-focus-stealing rule, tested |
| Exit codes break existing scripts | Currently unspecified, so any code is new information; documented and snapshot-tested from the start |
| Extraction stalls halfway | Phases are independently shippable; each leaves the tree better than it found it |

## 19. Acceptance checklist

- [ ] `shutil.get_terminal_size` appears exactly once in the package.
- [ ] `NO_COLOR` and `FORCE_COLOR` are honored; `NO_COLOR=1` emits zero escapes.
- [ ] Capabilities are detected without interactive terminal queries and are inspectable.
- [ ] Overrides via settings and environment always win.
- [ ] Display width is correct for CJK, emoji, and combining marks; truncation never splits an escape or grapheme.
- [ ] Every view renders correctly at 40–200 columns, snapshot-tested.
- [ ] Resize is handled via `SIGWINCH` with debounce.
- [ ] Styles are semantic; degradation to monochrome and ASCII is automatic.
- [ ] No status is conveyed by color alone.
- [ ] Focus is always visible and never stolen by background events.
- [ ] Every overlay closes with `Escape`; navigation is uniform.
- [ ] The command palette covers built-in, plugin, and skill commands.
- [ ] Bracketed paste is one event; oversized paste prompts.
- [ ] Screen-reader mode is linear, announces once per transition, and emits no cursor control.
- [ ] Exit codes are stable and documented; JSON errors carry stable codes.
- [ ] Results go to stdout, errors to stderr; progress auto-disables off-TTY.
- [ ] PTY tests pass reliably in the full suite via a readiness marker.
- [ ] `ruff check` and the full pytest suite pass.

## 20. Recommended implementation order

1. **Fix the PTY readiness marker and add sync mode first.** Every subsequent phase is a refactor of UI code, and refactoring behind flaky tests is how regressions ship. This is a small change with outsized leverage.
2. **Build the snapshot harness second and capture the baseline.** Without it, "behavior-preserving" is a claim rather than a fact.
3. **Ship `NO_COLOR` support third.** It is a few lines through the capability layer, it is a recognized standard, and it immediately helps users who need monochrome.
4. **Consolidate terminal size fourth.** Ten call sites to one is mechanical, verifiable by snapshot, and unblocks breakpoints.
5. **Add measurement fifth** — escape-safe truncation is a correctness fix, not a feature; mid-escape truncation corrupts everything downstream of it.
6. **Centralize rendering sixth**, keeping output byte-identical at the default profile so the diff is provably safe. This also hands `p_statusline_themes_output_styles.md` the seam it needs.
7. **Unify interaction seventh**, with the no-focus-stealing rule enforced from the first commit.
8. **Add screen-reader mode eighth.** It depends on the rendering contract existing, and doing it earlier would mean building it twice.
9. **Add CLI exit codes and structured errors last** — independent of the TUI work, and easy to slot in whenever there is a gap.
