# Statusline, Themes, and Output Styles — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/term/` (from `o_tui_cli_ux_accessibility.md`), `tui_fullscreen.py`, `serve_ui.py`, `headless.py`
**Objective:** Replace hardcoded ANSI with a semantic token system, ship built-in accessible themes, make the status line configurable including safe external commands, and define render policies so terminal, Markdown, JSON, IDE, and web presentations share one contract.

## 1. Executive summary

Presentation in Mantis is currently a literal. `tui_fullscreen.py` defines `_MODE_ANSI` at line 36 under a comment noting the palette uses *"ANSI 256/standard colors (work in Terminal.app — no truecolor needed)"* — a pragmatic choice that maximizes compatibility. Content is assembled as ANSI-escaped strings and handed to `prompt_toolkit` through `ANSI(...)` at roughly a dozen sites. `tui.py` does the same independently.

Three consequences follow.

**There is no theme.** Colors cannot be changed. A user on a light background, a user who needs high contrast, and a user with a colorblind-safe palette requirement all get the same escapes. `o_tui_cli_ux_accessibility.md` establishes that `NO_COLOR` is not honored either; that plan adds the capability layer and the semantic-style seam, and this plan builds the theme system on top of it. **The dependency runs one way and matters: without semantic styles, theming means rewriting a dozen call sites per theme.**

**The status line is fixed.** What it shows is decided in code. A user cannot add their git branch, their Kubernetes context, their cloud profile, or a cost counter. Every comparable tool solves this by letting the user supply a command whose stdout becomes the status line — which is useful and is also, done carelessly, arbitrary command execution on a timer.

**Output style is implicit.** The agent's prose is rendered one way in the terminal, another way in `serve_ui.py`'s HTML, and as raw text in `headless.py`. Markdown handling, code-block rendering, table handling, and truncation rules are re-decided per surface. Adding the IDE panel from `r_ide_integrations.md` would make a fourth. There is no shared notion of "render this agent output for this surface."

These three are one problem: presentation decisions live at the point of use rather than in a policy the surfaces share.

## 2. Goals

### User outcomes

- Pick a theme, including high-contrast and colorblind-safe options, and have it apply everywhere.
- Get a readable UI on a light terminal background without manual fiddling.
- Configure the status line: which segments, in what order, with what separators.
- Add a custom segment from a command — safely, with a timeout and a cache.
- Choose how the agent's prose is presented: full Markdown, plain, or compact.
- Have the web dashboard and IDE panel look like the same product as the terminal.

### Engineering goals

- Build on the semantic-style seam from `o_tui_cli_ux_accessibility.md`; do not introduce a second styling path.
- Themes are data — JSON files — not code.
- Every theme is validated for contrast before shipping, not by eye.
- Status commands are sandboxed, bounded, cached, and never block a render.
- One render policy consumed by terminal, Markdown, JSON, IDE, and web.
- No new required dependency.
- Python 3.9–3.14.

### Success metrics

- Zero raw ANSI escapes outside the theme resolver, asserted by test.
- Every built-in theme passes an automated contrast check at its stated level.
- A status command that hangs, crashes, or floods never degrades render latency, proven by test.
- The same agent output renders coherently across all five surfaces from one policy.
- Theme switching is instant and requires no restart.

## 3. Non-goals

- A general terminal-graphics or layout framework. Layout belongs to the accessibility plan.
- Truecolor-only themes. 256-color remains the baseline for compatibility, with truecolor as an enhancement.
- Restyling the marketing site. `serve_ui.py`'s dashboard participates; the public website does not.
- User-authored render policies as code. Policies are declarative.
- Replacing the web UI's markup, which `m_session_event_api_and_remote_surfaces.md` keeps intact.
- Theme distribution — `j_plugin_packages_and_marketplaces.md` can carry themes once the format exists.

## 4. Current integration points

- `mantis_agent/tui_fullscreen.py` — `_MODE_ANSI` at line 36; `ANSI(...)` rendering at 764, 772, 836, 949, 963, 972, 1391, 1408 and elsewhere; the status/footer regions.
- `mantis_agent/tui.py` — independent color and formatting decisions.
- `mantis_agent/term/render.py`, `capabilities.py`, `glyphs.py` — the seam this plan fills, from `o_tui_cli_ux_accessibility.md`.
- `mantis_agent/tool_preview.py` — `TOOL_VERBS` and the preview functions; already the shared one-line renderer and a model for policy-driven rendering.
- `mantis_agent/serve_ui.py` (2,283 lines) — `INDEX_HTML`, `MANTIS_SVG`; the web surface.
- `mantis_agent/headless.py` — `_final_text`, `_dump`; the machine surface.
- `mantis_agent/workflow_view.py` — a viewer needing consistent presentation.
- `mantis_agent/settings.py` — theme and status configuration.
- `mantis_agent/sandbox.py` — status commands run confined.
- `mantis_agent/permissions.py` — status commands are commands and are policy-checked at configuration time.
- `mantis_agent/activity/` — the rail, whose segments the status system renders.
- `mantis_agent/budget.py`, `catalog.py` — cost and model data for status segments.

## 5. Token system

### Semantic tokens

Presentation refers to meaning, never to color.

```text
text.primary        text.secondary      text.muted        text.inverse
accent.primary      accent.secondary
status.success      status.warning      status.error      status.info
status.running      status.pending      status.blocked    status.cancelled
diff.added          diff.removed        diff.context      diff.header
syntax.keyword      syntax.string       syntax.comment    syntax.number
                    syntax.function     syntax.type
ui.border           ui.selection        ui.focus          ui.cursor
ui.scrollbar        ui.overlay.bg       ui.overlay.border
tool.read           tool.write          tool.exec         tool.search
agent.self          agent.child         agent.peer        agent.untrusted
```

`agent.untrusted` earns its place: content from a child report, an MCP server, or a channel event is visually distinguishable from first-party output. Several plans in this set label such content structurally; giving it a visual token makes the labeling legible at a glance.

### Theme format

```json
{
  "name": "mantis-dark",
  "version": 1,
  "appearance": "dark",
  "contrast": "AA",
  "colorblindSafe": true,
  "tokens": {
    "text.primary":   {"256": 252, "truecolor": "#e6e6e6"},
    "status.error":   {"256": 203, "truecolor": "#ff5f5f", "attrs": ["bold"]},
    "status.success": {"256": 114, "truecolor": "#87d787"},
    "diff.added":     {"256": 65,  "truecolor": "#5f875f", "bg": {"256": 22}}
  },
  "glyphs": {"status.running": "●", "status.blocked": "⊘"},
  "fallback": "mantis-dark-16"
}
```

- Each token supplies values per color depth. The resolver picks by detected capability and falls back down the chain: truecolor → 256 → 16 → none.
- `attrs` covers bold, dim, italic, underline, applied only where the terminal supports them.
- `fallback` names a theme to inherit unspecified tokens from, so a variant is a small file.
- Themes are JSON, validated against a schema, loadable from `~/.mantis/themes/`, a project directory, or a plugin.

### Built-in themes

| Theme | Appearance | Contrast | Colorblind-safe |
|---|---|---|---|
| `mantis-dark` | dark | AA | yes |
| `mantis-light` | light | AA | yes |
| `high-contrast-dark` | dark | AAA | yes |
| `high-contrast-light` | light | AAA | yes |
| `mono` | any | AAA | n/a — no color, glyphs and weight only |
| `ansi-16` | any | — | inherits the terminal's own palette |

`mono` is not just an accessibility option; it is what `NO_COLOR` resolves to, which means the monochrome path is a first-class theme that gets tested rather than a degraded afterthought.

### Contrast validation

Themes are validated automatically, not judged by eye:

- Compute WCAG contrast ratios for every foreground/background pair a theme actually produces.
- `AA` requires ≥ 4.5:1 for normal text; `AAA` requires ≥ 7:1.
- Terminal backgrounds are not knowable, so validate against the theme's declared `appearance` reference background, and document that assumption.
- `colorblindSafe: true` is verified by simulating deuteranopia, protanopia, and tritanopia and asserting that tokens whose distinction carries meaning — `status.success` versus `status.error` most importantly — remain distinguishable.
- A built-in theme failing validation fails the test suite. A user theme failing it produces a warning, not a refusal; it is their terminal.

### Background detection

- `appearance: "auto"` uses `COLORFGBG` where present, then `TERM_PROGRAM` heuristics, then defaults to dark.
- No interactive terminal queries — the accessibility plan forbids query-and-wait detection, and background queries are the classic case that hangs.
- Users override explicitly; the auto path is a convenience, never a guess the user cannot correct.

## 6. Status line

### Segments

```json
{
  "statusline": {
    "enabled": true,
    "position": "bottom",
    "separator": " · ",
    "segments": [
      {"type": "model"},
      {"type": "mode"},
      {"type": "cwd", "style": "basename"},
      {"type": "git", "showDirty": true},
      {"type": "context", "format": "percent"},
      {"type": "cost", "format": "session"},
      {"type": "activity", "maxItems": 2},
      {"type": "command", "id": "k8s", "command": "kubectl config current-context",
       "intervalMs": 30000, "timeoutMs": 1000, "maxBytes": 64, "icon": "⎈"}
    ]
  }
}
```

Built-in segment types: `model`, `mode`, `cwd`, `git`, `context`, `cost`, `tokens`, `activity`, `session`, `sandbox`, `advisor`, `team`, `channel`, `text`, `command`.

Rendering rules:

- Segments render right-to-left by priority when space is short: low-priority segments drop entirely rather than truncating into ambiguity.
- Each segment declares a minimum width and whether it is droppable.
- At the `tiny` breakpoint only `mode` and `activity` survive.
- The status line never wraps.
- It is rendered from cached values; a render never waits on anything.

### Command segments

This is the feature with real risk, so its rules are strict.

- **Argv only, never a shell.** A `command` is a list or a string parsed with `shlex.split`; no shell interpretation, no pipes, no globs. A user wanting a pipeline writes a script and invokes that script.
- **Executed under the session sandbox** with the scrubbed environment from `h_sandbox_egress_credentials_and_escape_controls.md`. A status command must not be a path to the API key.
- **Never on the render path.** Commands run on a timer in a background task; the status line renders whatever was last cached. A hanging command shows a stale value with a staleness marker, never a frozen UI.
- **Bounded:** timeout (default 1 s), output cap (default 256 bytes, first line only), minimum interval (default 5 s, enforced regardless of configuration).
- **Output sanitized** — control characters, ANSI, and bidi stripped, and length capped. A command's output is untrusted content rendered into the UI chrome, which is exactly the injection surface that would let it forge status.
- **Configuration is user- or managed-tier only.** A project settings file cannot define a status command. A cloned repository must not be able to execute anything on a timer, and this is the most obvious way it would try.
- **Approval on first use**, showing the exact argv, with the approval keyed to a content hash. Editing the command re-prompts.
- Failures show a distinct marker and back off exponentially; a persistently failing command is disabled with a notice rather than retried forever.

The combination — argv-only, sandboxed, off the render path, rate-floored, sanitized, user-tier-only, hash-approved — is what makes an arbitrary-command feature acceptable. Removing any one of them reopens the hole.

## 7. Output styles

A render policy describes how agent output is presented, and every surface consumes the same policy.

```json
{
  "output": {
    "style": "markdown",
    "codeBlocks": {"syntax": true, "lineNumbers": false, "maxLines": 200, "wrap": false},
    "tables": {"render": true, "maxWidth": "terminal"},
    "lists": {"compact": false},
    "links": {"style": "osc8"},
    "thinking": {"show": "collapsed"},
    "toolCalls": {"style": "preview"},
    "diffs": {"style": "unified", "context": 3, "wordLevel": true},
    "maxBlockLines": 500,
    "truncation": {"strategy": "head-tail", "notice": true}
  }
}
```

Styles: `markdown` (default), `plain` (no formatting, for logs and pipes), `compact` (dense, minimal blank lines), `verbose` (full thinking and tool detail).

### Per-surface capability

Surfaces differ in what they can render, and the policy adapts rather than each surface reinterpreting:

| Surface | Markdown | Syntax | Tables | Links | Images |
|---|---|---|---|---|---|
| Terminal | Rendered to ANSI | Yes | Box-drawn or ASCII | OSC 8 or plain | Path only |
| Headless text | Stripped | No | ASCII | Plain | Path |
| Headless JSON | Raw source preserved | n/a | n/a | n/a | Ref |
| IDE panel | Native | Native | Native | Native | Yes |
| Web dashboard | HTML | Yes | HTML | Yes | Yes |

Rules:

- `headless` JSON preserves the **raw** Markdown source. A machine consumer must receive what the model produced, not a rendering of it.
- Truncation is applied per policy at every surface, and always states what was omitted — consistent with the rule applied throughout this plan set.
- Tool-call rendering reuses `tool_preview.TOOL_VERBS` so a call is described identically everywhere.
- Untrusted content — child reports, MCP results, channel events — renders with the `agent.untrusted` token and a visible source label, on every surface.

### Markdown rendering

- A small in-package renderer; no new dependency.
- Supported: headings, emphasis, inline code, fenced code with language hints, lists, block quotes, tables, links, horizontal rules.
- **Raw HTML in Markdown is never interpreted** — in the terminal it is meaningless, and in the web surface it would be an injection vector. It renders as literal text.
- Syntax highlighting via a small tokenizer for common languages, degrading to plain when the language is unknown. Highlighting is a nicety and must never be a correctness risk; a tokenizer bug should produce dull output, not wrong output.
- Code blocks over `maxLines` collapse with an expansion hint.

## 8. Configuration

```json
{
  "theme": {
    "name": "auto",
    "appearance": "auto",
    "path": null,
    "overrides": {"status.error": {"256": 196}}
  },
  "statusline": {"enabled": true, "segments": [], "separator": " · ", "position": "bottom"},
  "output": {"style": "markdown"},
  "web": {"theme": "inherit"}
}
```

`theme.name: "auto"` selects `mantis-dark` or `mantis-light` by detected appearance, and `mono` when `NO_COLOR` is set. `theme.overrides` lets a user adjust one token without copying a whole theme.

Environment:

- `MANTIS_THEME`
- `MANTIS_STATUSLINE=0|1`
- `MANTIS_OUTPUT_STYLE`
- `NO_COLOR` — always wins, resolving to `mono`

## 9. Surface

```text
/theme                     current theme, appearance, contrast, capability
/theme list                available themes with contrast and colorblind flags
/theme set <name>
/theme preview <name>      render a sample without switching
/theme validate <path>     contrast and colorblind checks on a user theme
/statusline                current segments and their values
/statusline add|remove <type>
/statusline test <id>      run a command segment once and show output + timing
/output style <style>
```

`/theme preview` renders a fixed sample containing every token — diff, code, statuses, tool calls, untrusted content — so a theme can be judged before adopting it, and so `mono` and high-contrast variants get exercised.

## 10. Errors

```text
ThemeError                      (base)
├── ThemeNotFoundError
├── ThemeSchemaError
├── ThemeContrastError           # built-in failing validation → test failure
├── ThemeTokenMissingError       # resolved via fallback chain; reported
├── StatusSegmentError
├── StatusCommandDeniedError     # project-tier or unapproved
├── StatusCommandTimeoutError
├── StatusCommandOutputError     # oversize or unparseable
└── OutputPolicyError
```

A theme problem must never break the UI. An unresolvable token falls back to `text.primary` and is reported once.

## 11. Delivery phases

### Phase 0 — Inventory and dependency

1. Confirm `o_tui_cli_ux_accessibility.md`'s semantic-style seam is in place; this plan does not start before it.
2. Catalogue every color decision in `tui.py` and `tui_fullscreen.py` and map to tokens.
3. Extract `_MODE_ANSI` as the initial `mantis-dark` token values so nothing changes visually on day one.
4. Build the contrast and colorblind validators.
5. Prototype a command segment and measure cost off the render path.

**Exit:** token map complete; validators working; default theme reproduces current output exactly.

### Phase 1 — Theme system

1. Add `term/theme.py` with schema, loader, resolver, and fallback chain.
2. Ship `mantis-dark` byte-identical to today's output.
3. Add `mantis-light`, high-contrast variants, `mono`, and `ansi-16`.
4. Wire `NO_COLOR` to `mono` and `appearance: auto` detection.
5. Add `/theme` commands and `theme.overrides`.

**Exit:** themes switch instantly; default output unchanged; every built-in passes validation.

### Phase 2 — Token migration

1. Replace every raw escape with a token lookup.
2. Add the assertion that no ANSI escape appears outside the resolver.
3. Migrate `tui.py` and `tui_fullscreen.py`, then `workflow_view.py`.
4. Add the `agent.untrusted` token and apply it to child, MCP, and channel content.
5. Snapshot-test all themes at all capability levels.

**Exit:** presentation is centralized; snapshots prove behavior preservation.

### Phase 3 — Status line

1. Add the segment model and built-in segments.
2. Implement priority-based dropping and breakpoint behavior.
3. Implement the cached render path with no blocking.
4. Add `/statusline` commands.
5. Integrate activity, cost, sandbox, advisor, team, and channel segments.

**Exit:** the status line is configurable and never blocks a render.

### Phase 4 — Command segments

1. Implement argv-only execution under the sandbox with a scrubbed environment.
2. Implement background scheduling, timeout, output cap, and sanitization.
3. Implement user/managed-tier restriction and hash-based approval.
4. Implement failure backoff and auto-disable.
5. Add `/statusline test`.

**Exit:** custom segments work with every guard in place.

### Phase 5 — Output styles

1. Add the render policy model.
2. Add the Markdown renderer with syntax highlighting and safe HTML handling.
3. Implement per-surface capability adaptation.
4. Apply to terminal, headless text, headless JSON, and web.
5. Add `/output style` and truncation notices.

**Exit:** one policy drives every surface; JSON preserves raw source.

### Phase 6 — Web, IDE, and hardening

1. Apply themes to `serve_ui.py` via CSS custom properties generated from tokens.
2. Supply tokens to the IDE panel over the protocol.
3. Adversarial review: status command injection, theme file abuse, Markdown HTML, ANSI in rendered content.
4. Fuzz theme files and Markdown input.
5. Re-validate contrast across all themes.

## 12. Testing strategy

### Unit

- Theme schema validation: valid, missing tokens, malformed colors, bad fallback chains, cycles.
- Resolver fallback: truecolor → 256 → 16 → none for every token.
- `theme.overrides` precedence.
- Contrast computation against known WCAG values.
- Colorblind simulation distinguishing success from error in every theme.
- `NO_COLOR` resolves to `mono` regardless of other configuration.
- Appearance detection from `COLORFGBG` and `TERM_PROGRAM`.
- Segment priority dropping at each breakpoint; status line never wraps.
- Command segment: argv parsing, shell metacharacters not interpreted, timeout, output cap, sanitization, minimum interval enforcement, backoff, auto-disable.
- Project-tier status command rejected.
- Markdown rendering for every supported construct; raw HTML rendered literally.
- Truncation strategies and notices.
- Per-surface capability adaptation.

### Snapshot

- Every theme at every color depth for a fixed sample containing all tokens.
- Default theme output byte-identical to the pre-migration baseline.
- Status line at each breakpoint.
- Agent output in each style across each surface.

### Integration

- Theme switch mid-session applies immediately with no restart.
- Command segment updating on its interval while renders continue.
- Hanging command shows a stale value with a marker; render latency unaffected.
- Web dashboard reflects the terminal theme.
- Headless JSON preserves raw Markdown.

### Security

- Status command with shell metacharacters does not execute a shell.
- Status command cannot read the provider API key from its environment.
- Status command output containing ANSI or bidi cannot forge or corrupt status.
- Project settings defining a status command are rejected.
- An edited approved command re-prompts.
- Theme file with a path traversal in `fallback` is rejected.
- Markdown containing raw HTML or a `javascript:` link is inert on the web surface.
- Untrusted agent content renders with its source label on every surface.

### Performance

- Render latency with the status line enabled versus disabled.
- Theme resolution cost per frame (must be cached, effectively zero).
- Markdown rendering cost on a 2,000-line response.
- Command segment scheduling overhead with five segments.

## 13. Documentation

- `docs/guides/themes.md` — choosing, authoring, validating, overrides.
- `docs/guides/statusline.md` — segments, ordering, custom commands and their constraints.
- `docs/guides/output-styles.md` — styles, per-surface behavior, truncation.
- `docs/api/theme.md` — schema, token reference, resolver API.
- A token reference table with a rendered example per token.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 14. File-level implementation map

New:

- `mantis_agent/term/theme.py` — schema, loader, resolver
- `mantis_agent/term/tokens.py` — token catalogue
- `mantis_agent/term/contrast.py` — WCAG and colorblind validation
- `mantis_agent/term/statusline.py` — segments and rendering
- `mantis_agent/term/status_command.py` — sandboxed command segments
- `mantis_agent/term/markdown.py` — renderer
- `mantis_agent/term/syntax.py` — tokenizer
- `mantis_agent/term/output_policy.py`
- `mantis_agent/themes/*.json` — built-in themes
- `tests/test_theme_schema.py`
- `tests/test_theme_resolver.py`
- `tests/test_theme_contrast.py`
- `tests/test_statusline_segments.py`
- `tests/test_status_command_security.py`
- `tests/test_markdown_render.py`
- `tests/test_output_policy.py`
- `tests/snapshots/themes/**`
- `docs/guides/themes.md`
- `docs/guides/statusline.md`
- `docs/guides/output-styles.md`

Modified:

- `mantis_agent/term/render.py` — token resolution
- `mantis_agent/tui_fullscreen.py` — `_MODE_ANSI` removed; tokens throughout
- `mantis_agent/tui.py` — tokens throughout
- `mantis_agent/workflow_view.py` — tokens
- `mantis_agent/tool_preview.py` — policy-driven previews
- `mantis_agent/headless.py` — output policy, raw Markdown in JSON
- `mantis_agent/serve_ui.py` — CSS custom properties from tokens
- `mantis_agent/settings.py` — theme, statusline, output configuration
- `mantis_agent/sandbox.py` — status command confinement
- `tests/public_api_surface.txt` — intentional update

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Status commands become arbitrary execution | Argv-only, sandboxed, scrubbed env, user-tier only, hash-approved, rate-floored, off the render path |
| Status command output forges UI chrome | Sanitized on ingest; length-capped; rendered in a fixed slot |
| Token migration changes appearance | Default theme extracted from `_MODE_ANSI`; byte-identical snapshot baseline |
| Themes ship with unreadable combinations | Automated contrast and colorblind validation; built-in failure fails CI |
| Background detection is wrong | Explicit override always available; no interactive queries; documented default |
| Markdown renderer introduces an injection path | Raw HTML never interpreted; `javascript:` links inert; fuzzed |
| Syntax highlighting bugs corrupt output | Tokenizer failure degrades to plain; never affects content |
| Status line blocks rendering | Cached values only; commands strictly background |
| Web and terminal drift | One token source generating CSS custom properties |
| Theming without the style seam | Sequenced strictly after the accessibility plan's `render.py` |
| Overrides create unreadable custom themes | Warning with a contrast report, not a refusal — it is the user's terminal |
| Escapes leak into rendered content | No raw escapes outside the resolver, asserted by test |

## 16. Acceptance checklist

- [ ] No raw ANSI escape exists outside the theme resolver, asserted by test.
- [ ] `mantis-dark` reproduces the pre-migration output byte-identically.
- [ ] Light, high-contrast, `mono`, and `ansi-16` themes ship and validate.
- [ ] Every built-in theme passes WCAG contrast at its declared level.
- [ ] Colorblind-safe themes keep success and error distinguishable under simulation.
- [ ] `NO_COLOR` resolves to `mono` and overrides all other configuration.
- [ ] Themes switch instantly without restart; `theme.overrides` works.
- [ ] The status line is configurable, priority-drops, and never wraps or blocks.
- [ ] Command segments are argv-only, sandboxed, scrubbed, capped, sanitized, and hash-approved.
- [ ] Project settings cannot define a status command.
- [ ] A hanging status command never affects render latency.
- [ ] One output policy drives terminal, headless text, headless JSON, IDE, and web.
- [ ] Headless JSON preserves raw Markdown source.
- [ ] Raw HTML in Markdown is never interpreted.
- [ ] Untrusted content is visually and textually labeled on every surface.
- [ ] `/theme preview` exercises every token.
- [ ] `ruff check` and the full pytest suite pass.

## 17. Recommended implementation order

1. **Do not start before `o_tui_cli_ux_accessibility.md` lands `term/render.py`.** Theming without a semantic-style seam means editing a dozen call sites per theme, and the work would be redone.
2. **Extract `_MODE_ANSI` into `mantis-dark` first and prove byte-identical output.** This is the change that makes every later step safe to review: if the default theme reproduces today exactly, the migration is verifiable rather than aspirational.
3. **Build the contrast and colorblind validators before authoring any new theme.** Authoring first and validating later produces themes that must be redone.
4. **Ship `mono` early** — it is what `NO_COLOR` resolves to, and having it as a real, tested theme rather than a degraded path is what keeps the monochrome experience good.
5. **Migrate tokens fourth**, with the no-raw-escape assertion added in the same change so regressions cannot creep back.
6. **Ship the status line with built-in segments only, fifth.** Most of the value is in `model`, `mode`, `git`, `context`, and `cost`; none of it carries execution risk.
7. **Add command segments sixth, with every guard in the first commit.** This is the one feature here that executes user-supplied commands, and a version without the sandbox, the tier restriction, or the approval gate should never exist.
8. **Add output styles last.** They touch the most surfaces and benefit from the token system being settled; doing them earlier would mean rendering decisions made before the vocabulary existed.
