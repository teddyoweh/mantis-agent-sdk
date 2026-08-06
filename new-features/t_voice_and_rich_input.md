# Voice and Rich Input — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/clipboard.py`, the TUI input path, and a new `mantis_agent/input/` package
**Objective:** Unify clipboard, drag-and-drop, file references, images, pasted code, and `@` mentions behind one validated attachment model, then add explicit push-to-talk voice transcription on top of it.

## 1. Executive summary

Rich input in Mantis half exists, spread across three modules that do not know about each other.

`clipboard.py` (303 lines) is the most developed. It detects image media types from magic bytes (`_detect_media_type`), builds `ImageBlock`s, caps images at `_MAX_IMAGE_BYTES = 10 MB` and inline text at `_MAX_TEXT_BYTES = 256 KB`, recognizes `_IMAGE_EXTS`, grabs clipboard images and file paths per platform (`has_clipboard_image`, `grab_clipboard_image`, `grab_clipboard_file_path`), converts a file to blocks (`file_to_blocks`), finds image paths inside text with `_PATH_TOKEN_RE` and `find_image_paths`, and offers `looks_like_path` and `copy_to_clipboard`. The recent history shows clipboard hints, `Ctrl+V`, `/paste`, and drag-and-drop all landing.

`rules.py` has its own reference syntax: `_MENTION_RE = re.compile(r"(?<![\w@])@([\w./~-]+\.[\w]+)")` for `@src/db/schema.sql`, plus `active_files_from_messages` and `_PATH_KEYS` for extracting paths from tool inputs.

The TUI handles paste, drop, and typed input separately from both.

Four gaps follow.

**There is no attachment model.** Each path produces content blocks in its own way. There is no object representing "the user attached this thing," so there is no single place to validate, cap, deduplicate, preview, or account for attachments. Adding a fifth input path — IDE, web, mobile, voice — means a fifth implementation.

**Validation is per-path.** `clipboard.py` validates well: magic-byte detection rather than trusting extensions, explicit size caps. But a file arriving by drag-and-drop, by `@` mention, or by an IDE attachment does not necessarily pass through the same checks. Validation should be a property of attaching, not of one route.

**There is no aggregate budget.** Individual caps exist (10 MB image, 256 KB text). Nothing bounds the *total*: five images and three files can be attached to a single turn with no combined limit, and images are expensive in context terms.

**There is no voice input.** No audio capture, no transcription, nothing. For a terminal tool this is a genuine convenience gap — describing a bug is much faster spoken than typed — and it is also the input path with the most privacy weight, so it needs the most careful design.

## 2. Goals

### User outcomes

- Attach a file, an image, or a directory listing the same way regardless of how it arrived.
- See what is attached before sending, with sizes and estimated context cost, and remove any of it.
- Get a clear message when something is too large or unsupported, not a silent truncation.
- Reference a file with `@path` and have it attached, with completion while typing.
- Hold a key, speak, release, and have the transcript appear in the prompt for review before sending.
- Be certain the microphone is only active while the key is held.

### Engineering goals

- One `Attachment` model and one validation pipeline; every source produces attachments.
- Preserve `clipboard.py`'s public helpers and its magic-byte-over-extension discipline.
- Voice is optional and lazily loaded; core imports must not pay for it.
- Local transcription is the default where available; remote transcription is explicit and disclosed.
- Attachments carry provenance and are treated as untrusted content.
- Python 3.9–3.14; no new required dependency.

### Success metrics

- Every input path produces attachments through one validator, asserted by test.
- Total attachment budget is enforced per turn with a clear message.
- No file outside the workspace or an allowed root is attached without confirmation.
- Voice capture is provably bounded by the key press, verified by an audio-device lifecycle test.
- No audio is written to disk or transmitted unless the user configured it.
- Zero cold-start cost when voice is unused.

## 3. Non-goals

- Text-to-speech. Output speech is a separate concern and belongs with accessibility work if it is ever wanted.
- Always-listening or wake-word activation. Push-to-talk only — this is a deliberate, permanent constraint, not a first-phase limitation.
- Real-time streaming transcription with partial results in the first phase.
- Speaker identification, diarization, or voice authentication.
- Video or screen capture.
- Shipping a bundled speech model. Voice requires an explicit optional install.
- Replacing `@` mention semantics in `rules.py`; the syntax is shared and the behavior unified.

## 4. Current integration points

- `mantis_agent/clipboard.py` — `_IMAGE_EXTS`, `_MAX_IMAGE_BYTES`, `_MAX_TEXT_BYTES`, `_detect_media_type`, `_image_block`, `_run`, `has_clipboard_image`, `grab_clipboard_image`, `describe_clipboard_attachment`, `grab_clipboard_file_path`, `is_image_path`, `file_to_blocks`, `_PATH_TOKEN_RE`, `find_image_paths`, `looks_like_path`, `copy_to_clipboard`.
- `mantis_agent/rules.py` — `_MENTION_RE`, `_PATH_KEYS`, `active_files_from_messages`.
- `mantis_agent/types.py` — `ImageBlock`, `TextBlock`, `UserMessage`; the block types attachments produce.
- `mantis_agent/tui_fullscreen.py` — paste handling, `/paste`, drag-and-drop, the input buffer.
- `mantis_agent/tui.py` — classic input path.
- `mantis_agent/term/capabilities.py` — bracketed paste detection, from `o_tui_cli_ux_accessibility.md`.
- `mantis_agent/permissions.py` — reading a file outside the workspace is a permission decision.
- `mantis_agent/compact.py` — image-aware compaction already exists and must account for attachments.
- `mantis_agent/budget.py` — attachment context cost.
- `mantis_agent/headless.py` — attachments from CLI arguments and stdin.
- `mantis_agent/ide/` — IDE attachments from `r_ide_integrations.md`.
- `mantis_agent/catalog.py` — model capability, since not every model accepts images.

## 5. Attachment model

```python
@dataclass(frozen=True)
class Attachment:
    id: str
    kind: Literal["image", "text", "file", "directory", "audio", "transcript"]
    source: Literal["clipboard", "drop", "mention", "command", "ide", "voice", "stdin", "arg"]
    path: str | None            # resolved absolute, if from a file
    display: str                # what the user sees: "screenshot.png (412 KB)"
    media_type: str             # detected, never inferred from extension
    size_bytes: int
    est_tokens: int
    content: bytes | str
    truncated: bool = False
    omitted_bytes: int = 0
    trusted: bool = False       # user-authored vs. read from disk
    warnings: tuple[str, ...] = ()
```

### Pipeline

```text
source → acquire → detect → validate → budget → stage → preview → blocks
```

Every source enters at `acquire`; nothing bypasses `validate` or `budget`.

- **detect** — media type from magic bytes, extending `_detect_media_type`. Extensions are a hint for display only. A `.png` that is actually a 200 MB video is caught here.
- **validate** — size caps, path resolution, root checks, file-type checks (reject devices, FIFOs, sockets, symlink escapes).
- **budget** — aggregate accounting against the per-turn limit.
- **stage** — held in a pending set, editable before send.
- **preview** — rendered in the input area.
- **blocks** — converted to `ImageBlock` / `TextBlock` at send time, reusing `_image_block` and `file_to_blocks`.

### Staging

Attachments accumulate and are visible before sending:

```text
> explain this crash

  attached
    1  screenshot.png        412 KB   ~1.2k tokens   [x]
    2  logs/crash.log         48 KB   ~12k tokens    [x]  truncated, 180 KB omitted
    total ~13.2k tokens of 30k budget
```

- `Ctrl+X` or `/attach remove <n>` removes one; `/attach clear` removes all.
- Attachments persist across input edits and clear on send or explicit cancel.
- Truncation states the omitted byte count, consistent with the rule applied throughout this plan set.

## 6. Sources

### Clipboard

Preserve current behavior, routed through the pipeline. `has_clipboard_image`, `grab_clipboard_image`, `grab_clipboard_file_path`, and `describe_clipboard_attachment` become acquirers.

### Paste

- Bracketed paste, per `o_tui_cli_ux_accessibility.md`, so a multi-line paste is one event rather than a keystroke storm.
- Pasted text over a threshold (default 4 KB) offers attachment as a file-like block instead of inline text, keeping the prompt readable.
- Pasted content that `looks_like_path` resolves is offered as a file attachment, which the current code already does for images and should do generally.
- Pasted content is **never executed or interpreted** — it is text until the user sends it.

### Drag and drop

Terminal drop delivers a path (sometimes several, sometimes quoted or escaped). Parse with the existing `_PATH_TOKEN_RE` discipline, handle multiple paths and shell escaping, and attach each through the pipeline.

### `@` mentions

Unify with `rules.py`'s `_MENTION_RE`:

- Typing `@` opens path completion relative to the workspace.
- `@src/app.py` attaches the file; `@src/` attaches a bounded directory listing, not its contents.
- A mention resolving outside the workspace requires confirmation.
- The mention remains visible in the prompt text so the message reads naturally, with the attachment carried alongside.
- `rules.py` continues to use the same regex for rule activation; one syntax, two consumers.

### Commands and CLI

- `/attach <path>`, `/paste`, `/attach clear`, `/attach list`.
- `mantis run --attach file.png --attach log.txt`.
- stdin attachment for piped input, respecting `_STDIN_LIMIT`.

### IDE

`r_ide_integrations.md`'s selection and file context arrive as attachments with `source="ide"`, so editor content flows through the same validation and budget as everything else.

## 7. Validation and safety

Attachments are files chosen by a human but *read* by an agent, and their contents enter the model's context. Both facts matter.

- **Path resolution before checks.** `os.path.realpath` first, then root validation, so `docs/../../.ssh/id_rsa` and symlinks are caught. This mirrors the rule in `f_permission_policy_engine_and_auto_mode.md`, and the two must share the implementation.
- **Root policy.** Inside the workspace: attach freely. Outside: confirm, showing the resolved path. Protected paths from the permissions plan (`.env`, `~/.ssh`, `~/.aws`, `~/.mantis`) require explicit confirmation with a warning even when the user named them directly — attaching a credential file into model context is a real exfiltration path, and the user may not have realized what the file was.
- **File type.** Reject devices, FIFOs, sockets, and symlinks pointing outside allowed roots.
- **Media type from magic bytes**, never from the extension.
- **Binary handling.** A non-text, non-image binary is attached by reference (name, size, type) rather than content. Base64-encoding an arbitrary binary into context is expensive and almost never useful.
- **Secret scanning.** Attached text is scanned with the shared secret heuristics; a likely credential prompts before attaching. This is a warning, not a block — a user may legitimately need to share a config file — but it must be surfaced.
- **Content is untrusted.** An attached file's contents are data. They are labeled with provenance and cannot be instructions. A file containing prompt-injection text is exactly as untrusted as a file the agent read itself, and it is labeled as such.
- **Directory listings are bounded** — entry count, depth, and total bytes — and never recursive by default.

## 8. Budget

Per-turn aggregate limits, which currently do not exist:

```json
"budget": {
  "maxAttachments": 10,
  "maxTotalBytes": 26214400,
  "maxTotalTokens": 30000,
  "maxImages": 5,
  "maxImageBytes": 10485760,
  "maxTextBytes": 262144
}
```

- `maxImageBytes` and `maxTextBytes` preserve today's `_MAX_IMAGE_BYTES` and `_MAX_TEXT_BYTES`.
- Token estimates: images from dimensions per the model's tokenization; text by character heuristic. Estimates are shown as approximate.
- Exceeding a limit offers choices — remove something, truncate, or attach by reference — rather than failing outright.
- **Model capability is checked**: attaching an image when the active model does not accept images fails immediately with a clear message naming the model, rather than at request time with a provider error.
- Attachment cost participates in the context budget alongside memory (`l_auto_memory_lifecycle.md`) and skills (`k_skills_commands_policy_and_shell_blocks.md`), so one turn cannot be starved by another subsystem's allocation.

## 9. Voice

### Constraints

These are design constraints, not defaults:

- **Push-to-talk only.** The microphone opens on key press and closes on release. There is no always-listening mode, no wake word, and no configuration that enables one.
- **Explicit opt-in.** Disabled until configured; the optional dependency is installed deliberately.
- **Visible state.** A recording indicator is always shown while capturing, in the status line and as a distinct region — never only a color.
- **Review before send.** The transcript lands in the input buffer for editing. It is never sent automatically. This is what makes the feature safe to use in a room with other people.

### Capture

- Hold a configured key (default `Ctrl+Space`) to record; release to stop.
- Audio held in memory only. Nothing is written to disk unless `voice.keepRecordings` is explicitly enabled, and that setting carries a warning.
- Max duration (default 120 s) with a countdown as the limit approaches.
- Silence detection ends a recording early, configurable.
- Device selection and level indication; a clear error when no input device exists.
- Capture is a lazily-imported optional dependency; the module is not loaded until first use.

### Transcription

| Backend | Privacy | Notes |
|---|---|---|
| `local` | Audio never leaves the machine | Local speech model via an optional extra. **Default when available.** |
| `provider` | Audio sent to the configured provider | Explicit opt-in with a one-time disclosure |
| `command` | Depends on the command | User-supplied argv, sandboxed |
| `none` | — | Voice disabled |

Requirements:

- **Backend and destination are always visible** in `/voice status` and at the moment of first use. A user must never discover after the fact that their speech was uploaded.
- Remote transcription requires a one-time explicit confirmation naming the destination, remembered per backend.
- Audio bytes are registered with the redactor so they cannot appear in logs or traces.
- Transcription failures return a clear error; audio is discarded either way.
- `command` backend follows the same rules as status-line commands in `p_statusline_themes_output_styles.md`: argv only, sandboxed, timeout, output cap, user-tier configuration only.
- Language configurable; auto-detect where the backend supports it.

### Transcript handling

- Appended to the input buffer at the cursor, never replacing existing text.
- Punctuation and capitalization from the backend, with light normalization.
- A configurable phrase map for terms speech models get wrong — project names, tool names, `mantis` itself.
- Transcripts are **user-authored content** (`trusted=True`) because they originate from the user, unlike file contents. This distinction matters and should be explicit in the model.

## 10. Configuration

```json
{
  "input": {
    "attachments": {
      "enabled": true,
      "maxAttachments": 10,
      "maxTotalBytes": 26214400,
      "maxTotalTokens": 30000,
      "maxImages": 5,
      "maxImageBytes": 10485760,
      "maxTextBytes": 262144,
      "pasteThresholdBytes": 4096,
      "allowOutsideWorkspace": "prompt",
      "scanSecrets": true,
      "directoryListing": {"maxEntries": 200, "maxDepth": 1}
    },
    "mentions": {"enabled": true, "completion": true},
    "voice": {
      "enabled": false,
      "key": "c-space",
      "backend": "local",
      "model": null,
      "language": "auto",
      "maxSeconds": 120,
      "silenceStopMs": 1500,
      "device": null,
      "keepRecordings": false,
      "phraseMap": {}
    }
  }
}
```

Environment: `MANTIS_VOICE=0|1`, `MANTIS_VOICE_BACKEND`, `MANTIS_NO_ATTACHMENTS=1`.

Voice configuration is **user-tier only**. A project settings file must not be able to enable the microphone or choose a transcription destination.

## 11. Surface

```text
/attach <path>              attach a file
/attach list                staged attachments with sizes and estimates
/attach remove <n>
/attach clear
/paste                      attach from clipboard
/voice                      status: enabled, backend, destination, device
/voice on|off
/voice device <name>
/voice test                 record 3s, transcribe, show result and timing
```

`/voice test` is the diagnostic that makes setup tractable: it exercises capture, the backend, and normalization in one step and reports where a failure occurred.

The recording indicator:

```text
  ● recording  0:07 / 2:00   release ctrl+space to stop
```

Distinct region, glyph plus text, and announced in screen-reader mode.

## 12. Errors

```text
InputError                        (base)
├── AttachmentTooLargeError
├── AttachmentBudgetExceededError
├── AttachmentUnsupportedError     # model cannot accept this kind
├── AttachmentPathError            # outside roots, unresolvable
├── AttachmentTypeError            # device, socket, FIFO
├── AttachmentSecretWarning        # warning, prompts
├── AttachmentDeniedError          # user declined confirmation
├── VoiceUnavailableError          # dependency or device missing
├── VoiceDeviceError
├── VoiceTooLongError
├── VoiceBackendError
├── VoiceTranscriptionEmptyError
└── VoiceConsentRequiredError      # remote backend not yet confirmed
```

Every error states what to do next: install the extra, choose a device, remove an attachment, switch models.

## 13. Delivery phases

### Phase 0 — Audit and prototype

1. Inventory every current input path and the blocks it produces.
2. Design `Attachment` and confirm it covers clipboard, paste, drop, mention, CLI, and IDE.
3. Measure token cost of images at common sizes for accurate estimates.
4. Prototype push-to-talk capture on macOS and Linux; verify device release on key-up.
5. Evaluate local transcription options and their install footprint.

**Exit:** model covers all paths; capture provably releases the device; a viable local backend identified.

### Phase 1 — Attachment model

1. Add `input/` with `Attachment`, the pipeline, and the validator.
2. Route clipboard, paste, and drop through it, preserving `clipboard.py`'s public API.
3. Implement path resolution, root policy, file-type checks, and magic-byte detection.
4. Implement staging with preview, removal, and clearing.
5. Add `/attach` commands.

**Exit:** one validated path for existing sources; behavior preserved.

### Phase 2 — Budget and mentions

1. Implement aggregate per-turn budgets with token estimation.
2. Implement model-capability checking against `catalog.py`.
3. Implement over-budget choices rather than hard failure.
4. Unify `@` mentions with `rules.py`'s regex; add completion.
5. Integrate with the shared context accounting.

**Exit:** attachments are bounded, visible, and priced before sending.

### Phase 3 — Safety

1. Implement protected-path confirmation and secret scanning.
2. Implement provenance labeling and untrusted-content handling for file contents.
3. Bound directory listings.
4. Handle binaries by reference.
5. Wire IDE attachments through the same pipeline.

**Exit:** no credential is attached unknowingly; attached content carries no authority.

### Phase 4 — Voice capture

1. Add `input/voice/` with lazy imports and the optional extra.
2. Implement push-to-talk with strict key-bound device lifecycle.
3. Implement duration limits, silence detection, device selection, and level indication.
4. Implement the recording indicator with screen-reader announcement.
5. Add `/voice` commands and `/voice test`.

**Exit:** capture works, is visibly bounded, and never leaves the device open.

### Phase 5 — Transcription

1. Implement the local backend.
2. Implement the provider backend with one-time consent naming the destination.
3. Implement the sandboxed `command` backend.
4. Implement transcript insertion, normalization, and the phrase map.
5. Register audio with the redactor; ensure nothing is persisted by default.

**Exit:** speech reaches the prompt for review; the destination is always disclosed.

### Phase 6 — Hardening

1. Adversarial review: path escape, symlink, secret leakage, injection via attached content, project-tier voice enablement.
2. Fuzz path parsing, drop payloads, and media detection.
3. Device lifecycle leak tests across many press/release cycles.
4. Verify no audio on disk under default configuration.
5. Remove experimental gating.

## 14. Testing strategy

### Unit

- Media detection from magic bytes for every supported type, including mismatched extensions.
- Size caps: image, text, aggregate bytes, aggregate tokens, attachment count, image count.
- Path resolution: traversal, symlink escape, absolute, outside workspace, protected paths.
- File type rejection: device, FIFO, socket, directory-as-file.
- Token estimation accuracy against measured values.
- Model-capability rejection for images on a text-only model.
- Directory listing bounds.
- Secret scanning true and false positives.
- Paste threshold behavior and path-looking paste.
- Drop parsing: single, multiple, quoted, escaped, spaces.
- Mention parsing shared with `rules.py`'s regex.
- Staging: add, remove, clear, persistence across edits, clearing on send.
- Voice: duration cap, silence stop, device error, consent gating, phrase map.

### Integration

- Every source produces attachments through the same validator, asserted by instrumenting the pipeline.
- `clipboard.py`'s public helpers behave unchanged.
- Over-budget attachment offers choices and applies them correctly.
- Attaching `.env` prompts with a warning.
- IDE attachment flows through validation and budget.
- Voice: press, speak, release, transcript in buffer, editable, not auto-sent.
- Remote backend without consent refuses and prompts.

### End-to-end

- Multi-attachment turn with images and text; correct blocks produced.
- `/attach` command set.
- Drag-and-drop of several files.
- `@` mention with completion.
- `/voice test` reports each stage.
- Image-aware compaction accounts for attachments.

### Security

- `@../../.ssh/id_rsa` resolves and prompts with a warning.
- Symlink inside the workspace pointing to `~/.aws/credentials` is caught after resolution.
- Attached file containing injection text is labeled untrusted and confers no authority.
- Project settings cannot enable voice or set a transcription backend.
- No audio written to disk with default configuration, asserted by filesystem check.
- Audio bytes never appear in logs, traces, or transcripts.
- `command` transcription backend runs argv-only under the sandbox.
- Device is released on key-up even when transcription fails.

### Performance and reliability

- Cold import cost with voice unused (must be zero).
- Attachment validation cost for a 10 MB file.
- Token estimation cost.
- 500 press/release cycles with no device or memory leak.
- Transcription latency per backend.

## 15. Documentation

- `docs/guides/attachments.md` — sources, limits, mentions, budgets, troubleshooting.
- `docs/guides/voice.md` — setup, backends, push-to-talk, privacy, phrase map.
- `docs/guides/input-security.md` — what happens to attached content, protected paths, why attachments carry no authority.
- `docs/api/input.md` — `Attachment`, the pipeline, backend interface.
- A privacy statement for voice: what is captured, where it goes per backend, what is retained.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 16. File-level implementation map

New:

- `mantis_agent/input/__init__.py`
- `mantis_agent/input/attachment.py` — model and pipeline
- `mantis_agent/input/detect.py` — media type from magic bytes
- `mantis_agent/input/validate.py` — paths, types, roots, secrets
- `mantis_agent/input/budget.py` — aggregate accounting and estimation
- `mantis_agent/input/staging.py`
- `mantis_agent/input/sources/clipboard.py` — adapter over the existing module
- `mantis_agent/input/sources/drop.py`
- `mantis_agent/input/sources/mention.py`
- `mantis_agent/input/sources/cli.py`
- `mantis_agent/input/voice/__init__.py`
- `mantis_agent/input/voice/capture.py`
- `mantis_agent/input/voice/backends/local.py`
- `mantis_agent/input/voice/backends/provider.py`
- `mantis_agent/input/voice/backends/command.py`
- `tests/test_attachment_pipeline.py`
- `tests/test_attachment_validate.py`
- `tests/test_attachment_budget.py`
- `tests/test_attachment_sources.py`
- `tests/test_input_security.py`
- `tests/test_voice_capture.py`
- `tests/test_voice_backends.py`
- `docs/guides/attachments.md`
- `docs/guides/voice.md`

Modified:

- `mantis_agent/clipboard.py` — becomes a source adapter; public helpers preserved
- `mantis_agent/rules.py` — share `_MENTION_RE` with the mention source
- `mantis_agent/tui_fullscreen.py` — staging UI, `/attach`, `/voice`, recording indicator
- `mantis_agent/tui.py` — attachment path
- `mantis_agent/headless.py` — `--attach`, stdin attachments
- `mantis_agent/compact.py` — attachment-aware compaction
- `mantis_agent/budget.py` — attachment cost
- `mantis_agent/catalog.py` — model input capabilities
- `mantis_agent/permissions.py` — shared path resolution
- `mantis_agent/ide/context.py` — IDE attachments
- `pyproject.toml` — `voice` optional extra
- `tests/public_api_surface.txt` — intentional update

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Attaching a credential file into model context | Protected-path confirmation, secret scanning, resolved-path display |
| Path escape via mention, drop, or paste | `realpath` before checks, shared with the permissions plan; dedicated tests |
| Attached content treated as instructions | Provenance labeling, untrusted handling, injection tests |
| Aggregate context blowout | Per-turn budget with estimates and over-budget choices |
| Image sent to a text-only model | Capability check against the catalog, failing early with a clear message |
| Microphone stays open | Device lifecycle strictly bound to key press; 500-cycle leak test |
| Speech uploaded without the user realizing | Local default, one-time consent naming the destination, always-visible backend |
| Audio persisted unexpectedly | Memory only by default; `keepRecordings` carries a warning; filesystem assertion in tests |
| Project settings enable the microphone | Voice configuration is user-tier only |
| Voice dependency inflates install | Optional extra, lazy import, zero cold-start cost asserted |
| Refactoring breaks working clipboard behavior | Public helpers preserved; existing behavior tested before and after |
| Transcripts misread as untrusted input | Explicitly `trusted=True`; they originate from the user |

## 18. Acceptance checklist

- [ ] Every input source produces attachments through one validator.
- [ ] `clipboard.py`'s public helpers behave unchanged.
- [ ] Media type is detected from magic bytes, never the extension.
- [ ] Paths resolve before root and protected-path checks; symlink escapes are caught.
- [ ] Protected paths and likely secrets prompt with a warning.
- [ ] Attached file contents are labeled untrusted and confer no authority.
- [ ] Per-turn aggregate budgets are enforced with token estimates shown.
- [ ] Over-budget offers choices rather than failing outright.
- [ ] Images on a text-only model fail early with a clear message.
- [ ] Directory listings and binaries are bounded or attached by reference.
- [ ] Staged attachments are visible, removable, and clear on send.
- [ ] `@` mentions share one regex with `rules.py` and support completion.
- [ ] Voice is push-to-talk only; no always-listening mode exists in any configuration.
- [ ] The microphone is provably released on key-up, leak-tested.
- [ ] Recording state is always visible and announced.
- [ ] Transcripts land in the buffer for review and are never auto-sent.
- [ ] The transcription backend and destination are always disclosed; remote requires consent.
- [ ] No audio touches disk by default, asserted by test.
- [ ] Project settings cannot enable voice.
- [ ] Zero cold-start cost when voice is unused.
- [ ] `ruff check` and the full pytest suite pass.

## 19. Recommended implementation order

1. **Build the attachment model first and migrate existing sources onto it, changing nothing user-visible.** `clipboard.py` already does the hard parts well; the work is centralizing them so the fifth input path is free.
2. **Add the aggregate budget second.** It is the gap most likely to bite today — nothing currently stops five 10 MB images going into one turn — and it needs no new sources to be valuable.
3. **Add safety checks third**, sharing path resolution with the permissions plan rather than writing a second resolver. Two path validators that disagree is a bug generator.
4. **Unify `@` mentions fourth**, with completion. It is the highest-value input affordance that does not exist yet and costs little once the pipeline exists.
5. **Ship everything above before touching voice.** The attachment work is useful on its own and carries no privacy weight; bundling it with voice would delay it behind a much more sensitive feature.
6. **Build voice capture fifth, and get the device lifecycle right before writing any transcription code.** A microphone that stays open is the failure this feature cannot have, and it is testable independently of whether transcription works at all.
7. **Ship the local backend first.** A voice feature whose default uploads audio is a different product from one whose default does not, and shipping remote-first would set the wrong expectation permanently.
8. **Add the provider backend last**, with consent and disclosure in the same commit — never a version where audio can be uploaded without the user having been told.
