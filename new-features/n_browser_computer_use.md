# Browser and Computer Use — Extensive Implementation Plan

**Status:** Proposed  
**Target:** `mantis-agent-sdk` library and `mantis` terminal  
**Objective:** Let Mantis securely control a rendered browser, diagnose web applications, and verify workflows end to end.

## 1. Executive summary

Mantis currently offers `web_search` and `web_fetch` in `mantis_agent/builtin_tools/web.py`. They retrieve search results and readable HTTP content, but cannot execute a web application, retain browser state, inspect the post-JavaScript page, interact with controls, capture screenshots, or observe console and network behavior.

Build a Playwright-backed subsystem in stages:

1. Local isolated browser MVP.
2. Console/network diagnostics, uploads, downloads, dialogs, waits, and traces.
3. Security hardening and persistent isolated profiles.
4. TUI, CLI, permission-preview, artifact, and lifecycle integration.
5. CDP attachment to an explicitly approved browser.
6. Extension/native-host and remote browser transports.
7. Browser-scoped low-level computer actions and visual grounding.

Use in-process Python orchestration and an optional, lazily imported Playwright dependency. Hide execution behind a transport-neutral interface so tools retain the same contracts when CDP, extension, or remote transports are added.

## 2. Goals

### User outcomes

Mantis should be able to:

- Start a local app, open it, complete a form, and verify the result.
- Reproduce a frontend failure, inspect console and network errors, fix it, and retest.
- Inspect the rendered accessibility structure rather than only initial HTML.
- Verify desktop and mobile viewport layouts with screenshots.
- Upload test fixtures and verify downloads within controlled directories.
- Navigate multiple tabs and popups.
- Attach to an existing browser only after explicit approval.

### Engineering goals

- Reuse `Tool` and `ToolRegistry` from `mantis_agent/tools.py`.
- Pass every browser action through `mantis_agent/agent.py` permissions and hooks.
- Extend `mantis_agent/permissions.py` rather than introduce a parallel policy stack.
- Keep Playwright and browser startup lazy.
- Preserve Python 3.9–3.14 support.
- Prefer semantic accessibility references over brittle CSS/XPath selectors.
- Bound model-visible output and save complete safe artifacts separately.
- Guarantee cleanup after completion, cancellation, timeout, browser crash, or tool failure.
- Keep `web_search` and `web_fetch` as the cheaper preferred path for ordinary research.

### Success metrics

- At least 90% success on deterministic navigation/form workflows.
- No leaked browser processes in lifecycle tests.
- No password, cookie, token, or authorization-header leakage in results, logs, hooks, traces, or transcripts.
- Negligible cold-start impact when browser support is unused.
- Bounded semantic snapshots with explicit narrowing guidance.
- Domain policy resistant to redirects, popup navigation, DNS-label confusion, blocked IPs, and cloud-metadata access.

## 3. Non-goals for the MVP

- CAPTCHA solving, stealth automation, or anti-bot bypasses.
- Silent use of a personal browser profile.
- Silent access to stored credentials, cookies, payments, or authenticated tabs.
- Desktop-wide computer control.
- Pixel-only interaction as the default.
- Extension/native-host or cloud-browser infrastructure in Phase 1.
- Automatic browser-binary downloads during tool execution.
- Complete cross-browser parity in the first release.

## 4. Current integration points

The feature should extend these existing components:

- `mantis_agent/tools.py`: schemas, timeouts, read-only hints, concurrency, dispatch.
- `mantis_agent/agent.py`: streaming execution, cancellation, permissions, hooks, result ordering, cleanup.
- `mantis_agent/permissions.py`: allow/deny/ask rules, session approvals, input mutation and recheck.
- `mantis_agent/hooks.py`: existing tool lifecycle and `HookContext.arbitrary` metadata.
- `mantis_agent/builtin_tools/web.py`: search/fetch remains separate.
- `mantis_agent/builtin_tools/__init__.py`: built-in exports.
- `mantis_agent/tool_preview.py`: browser action and permission previews.
- `mantis_agent/cli.py`, `headless.py`, `tui.py`, and `tui_fullscreen.py`: registration and UI.
- `mantis_agent/sandbox.py`: shell confinement remains distinct from browser policy.
- `pyproject.toml`: optional dependency and supported Python versions.

Browser operations must be ordinary `Tool` objects so current hooks, permissions, tracing, streaming, and cancellation apply.

## 5. Product model

### Browser session

A session owns:

- Transport and capability set.
- Browser process or remote connection.
- Browser contexts and pages.
- Short page IDs such as `p1`.
- Semantic element refs such as `e1`.
- URL and origin history.
- Domain and private-network policy.
- Viewport/device settings.
- Bounded console, page-error, and network buffers.
- Upload/download policy.
- Artifact directory and trace/video state.
- Cancellation and activity state.

Create the session lazily on the first browser call. Close it from the agent lifecycle.

### Isolation modes

Implement progressively:

1. **Ephemeral isolated:** default temporary context; state deleted on close.
2. **Persistent isolated:** Mantis-owned profile; explicit permission and clear status.
3. **Attached local:** explicit CDP endpoint.
4. **Extension bridge:** paired extension/native host.
5. **Remote browser:** authenticated WebSocket worker.

Every transport advertises capabilities. Unsupported operations return explicit errors.

### Page and element identity

- Assign stable short IDs to pages.
- Generate element refs from semantic snapshots.
- Bind refs to session, page, frame, snapshot generation, role/name, and locator recipe.
- Invalidate or safely re-resolve refs after navigation or meaningful DOM changes.
- Return a stale-reference error telling the model to request a new snapshot.
- Keep raw selectors as a discouraged, permission-sensitive expert escape hatch.

## 6. Tool surface

### Phase 1 core tools

#### `browser_open`

Inputs:

- `url`
- `new_tab=false`
- `wait_until=domcontentloaded`
- optional timeout and viewport

Returns session/page ID, final URL, title, navigation result, compact semantic summary, redirects, and security notices. Validate initial URL and every redirect/origin transition.

#### `browser_snapshot`

Inputs include page ID, optional scope ref, interactive-only mode, text/hidden switches, and character limit. Return page metadata, generation ID, compact semantic tree, refs, focus, dialogs/overlays, and truncation/artifact notices. Mark read-only.

#### `browser_click`

Click a semantic ref with button, click count, modifiers, and timeout. Return URL/title changes, newly opened pages, dialogs/downloads, and concise changed-state information.

#### `browser_type`

Type into a semantic ref with clear, submit, delay, and sensitivity controls. Infer password sensitivity automatically and redact values everywhere outside execution.

#### `browser_select`

Select values in native selects and reliable semantic combobox/listbox patterns. Return a structured unsupported-control error where safe interaction is unavailable.

#### `browser_scroll`

Scroll page or referenced container by direction and bounded amount.

#### `browser_screenshot`

Capture page, element, or full-page screenshots. Return an image attachment where supported and always a controlled artifact path and dimensions.

#### `browser_tabs`

Use an action enum for list, new, select, and close. Include page IDs, titles, URLs, and selected state.

#### `browser_close`

Close one page or the full session, finalize requested artifacts, and terminate owned resources.

### Phase 2 diagnostics

- `browser_console`: bounded console and page-error queries by level, text, page, and cursor.
- `browser_network`: bounded requests/responses by URL, method, type, status, failure, and cursor; bodies excluded by default.
- `browser_wait`: wait for element state, text, URL, load state, request/response, download, or console condition.
- `browser_upload`: controlled file-input uploads with root, symlink, file-type, count, and size checks.
- `browser_downloads`: list and manage downloads within an artifact root.
- `browser_dialog`: inspect and accept/dismiss prompts, alerts, and confirms.
- `browser_evaluate`: explicitly enabled, high-risk JavaScript escape hatch with timeout and result bounds.

### Phase 3 computer actions

Add a browser-scoped `browser_computer` tool supporting mouse move, coordinate click, hover, drag, wheel, key chords, text insertion, and optional touch. Semantic tools remain preferred. Coordinate actions require a recent screenshot generation and matching viewport.

## 7. Architecture

### Proposed package layout

```text
mantis_agent/browser/
  __init__.py
  types.py
  errors.py
  policy.py
  manager.py
  session.py
  refs.py
  snapshot.py
  artifacts.py
  redaction.py
  tools.py
  transports/
    __init__.py
    base.py
    playwright.py
    cdp.py             # later
    native_bridge.py   # later
    remote.py          # later
```

If `mantis_agent/builtin_tools/browser.py` is introduced, make it a thin re-export layer.

### `BrowserTransport`

Define a protocol with methods for connect, close, capabilities, page listing/creation/closure, navigation, snapshots, actions, screenshots, console/network queries, and trace start/stop. Tool implementations depend only on this interface.

### `BrowserManager`

Responsibilities:

- Lazy creation and synchronized ownership.
- Mapping agent/session identity to browser sessions.
- Capability and status reporting.
- Session/page lookup.
- Closing one or all sessions.
- Process-wide cleanup.
- Leak prevention after cancellation.

Prefer explicit construction:

```python
def create_browser_tools(manager: BrowserManager) -> tuple[Tool, ...]:
    ...
```

Avoid an unbounded ownership-free global singleton.

### `BrowserSession`

Own mutable state and enforce page/ref generations. Serialize mutation operations per page; initially serialize the full session for correctness and optimize independent pages later.

### `ElementRefStore`

Store serializable locator recipes and semantic metadata, not externally visible Playwright element handles:

- session/page/generation
- frame identity/path
- role/name
- locator fallback
- stale-detection metadata

### Playwright transport

Use `playwright.async_api`, imported only during initialization. Defaults:

- Chromium.
- Headless in SDK/headless CLI; configurable headed mode in TUI.
- Temporary isolated context/profile.
- No personal profile or extension.
- Deterministic viewport, locale, and timezone where practical.
- Bounded navigation/action timeouts.
- Downloads routed through artifact handling.
- HTTPS errors rejected by default with explicit localhost-development override.

Installation remains explicit:

```bash
pip install 'mantis-agent-sdk[browser]'
python -m playwright install chromium
```

Never download the browser silently during a tool call.

### Concurrency and cancellation

- Mutating operations are not concurrency-safe on the same page.
- Start mutation tools with `is_concurrency_safe=False`.
- Introduce page-aware read concurrency only after locks exist.
- Cancellation must abort Playwright waits/actions and pending dialog/download handlers.
- Cleanup must be idempotent and cancellation-shielded.

## 8. Semantic snapshots

### Requirements

- Reflect rendered post-JavaScript state.
- Prefer accessibility role, name, value, state, and hierarchy.
- Include useful structural text and actionable controls.
- Assign concise refs in document order.
- Preserve frame boundaries.
- Include focus, disabled, checked, selected, expanded, required, invalid, and hidden states.
- Exclude scripts, styles, and full DOM dumps.
- Bound nodes, depth, and characters.
- Save complete safe snapshots as artifacts when truncated.
- Explain how to narrow with `scope_ref`.

Suggested format:

```text
Page p1 — “Checkout” — https://localhost:3000/checkout
Snapshot generation: 7

main
  heading “Checkout” [level=1]
  textbox “Email” [ref=e1 required value=""]
  textbox “Card number” [ref=e2 required sensitive]
  checkbox “Save card” [ref=e3 checked=false]
  button “Pay $24.00” [ref=e4]
  status “No payment submitted”
```

Use compact predictable text for models and structured internal records for SDK clients.

### Frames and shadow DOM

- Include iframe boundaries.
- Store frame traversal internally.
- Support open shadow roots where Playwright permits.
- Return explicit unsupported/security errors when browser restrictions prevent access.

## 9. Security and permission model

Security is part of the MVP.

### `BrowserPolicy`

Proposed settings:

```json
{
  "browser": {
    "enabled": false,
    "engine": "chromium",
    "headless": true,
    "mode": "ephemeral",
    "allowedDomains": [],
    "blockedDomains": [],
    "allowPrivateNetwork": true,
    "allowFileUrls": false,
    "allowJavascript": false,
    "allowUploads": false,
    "uploadRoots": [],
    "downloadRoot": ".mantis/artifacts/browser-downloads",
    "persistentProfile": null,
    "recordTrace": "on-failure",
    "maxPages": 8,
    "navigationTimeoutMs": 30000,
    "actionTimeoutMs": 10000
  }
}
```

Defaults should support localhost development without exposing cloud metadata or unrelated private-network targets.

### URL validation

Before initial navigation and every redirect/origin transition:

- Allow HTTP/HTTPS only by default.
- Reject or immediately redact embedded credentials.
- Normalize IDNs with IDNA.
- Resolve and classify addresses.
- Distinguish loopback, private, link-local, multicast, reserved, and public targets.
- Block metadata endpoints such as `169.254.169.254` and equivalents.
- Revalidate redirects, popups, and client-side navigation.
- Mitigate DNS rebinding where practical.
- Define explicit localhost/development-certificate behavior.
- Block file, data, JavaScript, Chrome, and extension schemes unless narrowly enabled.

### Domain transitions

- Track each page's effective origin.
- Detect transitions caused by clicks, forms, popups, redirects, and JavaScript.
- Same-origin actions may inherit authorization.
- New origins receive allow/block checks and, when required, a human prompt.
- Display source, destination, title, and initiating action.
- Support allow once, allow domain for session, and deny.
- Match domains on DNS-label boundaries.
- Never let approval for one redirect authorize an unrelated final target.

### Permission categories

Read-only:

- Snapshot, screenshot, tab list, console read, sanitized network metadata.

Mutating:

- Navigation, click, type, select, scroll, tab create/close, dialog action.

High-risk:

- JavaScript, uploads, copying downloads outside artifacts, persistent profiles, attached personal browser, clipboard, and credential-bearing actions.

Add browser previews to `mantis_agent/tool_preview.py` showing page/title, destination, semantic target, navigation/download possibility, redacted text, upload filenames/sizes, and JavaScript summary.

### Redaction

Never expose unredacted passwords, cookies, authorization headers, storage tokens, private keys, client certificates, sensitive request bodies, typed sensitive values, or uploaded contents. Implement centralized recursive redaction. Hook contexts receive sanitized browser data by default.

### Upload controls

- Require explicit policy and permission.
- Resolve symlinks before root validation.
- Reject directories, devices, sockets, and special files.
- Cap count, individual size, and total size.
- Preview filename/type/size.

### Download controls

- Save into a Mantis-controlled artifact directory.
- Sanitize filenames and avoid overwrite.
- Cap duration and size.
- Optionally inspect MIME/signature.
- Require separate permission to copy elsewhere.

### Existing-browser attachment

- Disabled by default.
- Explicitly configured and interactively confirmed.
- Display browser/device identity and selected tab.
- Do not auto-select among multiple devices without trusted pairing.
- Persist only opaque device IDs.
- Provide disconnect and forget controls.
- Use a separate allowlist from ephemeral browsing.

## 10. Artifacts, observability, and errors

### Artifact layout

```text
.mantis/artifacts/browser/<session-id>/
  screenshots/
  snapshots/
  downloads/
  traces/
  videos/
  network/
  metadata.json
```

Fall back to a user or temporary directory when project-local storage is unsuitable.

### Output budgets

Configure separate limits for snapshots, console/network entries, response bodies, screenshots, and total artifacts. When truncating, state what was omitted, provide filters/narrowing, and save complete safe data when appropriate.

### Tracing

Add spans for launch/connect, navigation, snapshot, action, permission wait, upload/download, recording finalization, and cleanup. Sanitize URLs and exclude cookies, secrets, typed values, and bodies.

### Error taxonomy

- `BrowserUnavailableError`
- `BrowserInstallRequiredError`
- `BrowserLaunchError`
- `BrowserConnectionError`
- `BrowserPolicyError`
- `NavigationBlockedError`
- `DomainTransitionDeniedError`
- `PageNotFoundError`
- `ElementNotFoundError`
- `StaleElementRefError`
- `ActionTimeoutError`
- `PageClosedError`
- `DialogBlockedError`
- `DownloadBlockedError`
- `UploadBlockedError`
- `JavaScriptDisabledError`
- `UnsupportedBrowserCapabilityError`

Return concise recoverable tool errors and retain detailed diagnostics in logs/artifacts.

## 11. TUI and CLI integration

### Tool registration

- Detect package and executable availability.
- Register as deferred tools or advertise through `tool_search`.
- Initialize only when called.
- Do not send unavailable browser schemas on every request.

### `/browser` commands

Planned commands:

```text
/browser status
/browser install
/browser open [url]
/browser show
/browser headless on|off
/browser tabs
/browser select <page-id>
/browser trace start|stop
/browser disconnect
/browser close
/browser clear-profile
/browser devices
/browser pair
/browser forget <device-id>
```

Phase 1 advertises only status, open, headless, tabs, select, and close.

### Status

Show availability, transport, engine/version, headless state, isolation mode, session/pages, selected page/title/domain, domain-policy summary, JavaScript/upload/download status, trace state, and artifact location.

### Rendering

Permission preview example:

```text
Browser click
  page: p1 — Checkout
  target: button “Pay $24.00” [e4]
  may navigate: yes
```

Result example:

```text
Clicked button “Pay $24.00”
  URL: https://localhost:3000/receipt
  title: Receipt
  console: 1 new error
  network: POST /api/payments → 500
```

Render images inline where supported; otherwise show path and dimensions. Preserve current TUI spacing conventions.

### Headless CLI

- Default to headless.
- Fail closed when permission is required and no asker or preapproval exists.
- Add an explicit browser-enable flag/config.
- Include machine-readable browser events in JSON/streaming output.
- Never launch a visible browser in CI without explicit configuration.

## 12. Hooks and lifecycle

- Use existing tool hooks for all browser tools.
- Put browser metadata in `HookContext.arbitrary` initially.
- Consider new public browser lifecycle events only after demonstrated need.
- Revalidate permission and browser policy after any hook mutation.
- Cleanup stops recordings, rejects pending actions, closes pages/contexts/processes, disconnects transports, removes ephemeral profiles, preserves requested artifacts, and finalizes downloads safely.

## 13. Settings and packaging

Potential environment overrides:

- `MANTIS_BROWSER=0|1`
- `MANTIS_BROWSER_ENGINE=chromium|firefox|webkit`
- `MANTIS_BROWSER_HEADLESS=0|1`
- `MANTIS_BROWSER_CDP_URL`
- `MANTIS_BROWSER_ARTIFACT_DIR`
- `MANTIS_BROWSER_ALLOWED_DOMAINS`
- `MANTIS_BROWSER_BLOCKED_DOMAINS`

Follow current settings precedence. Store remote credentials outside project settings.

Add a `browser` optional dependency with a Playwright minimum selected by the compatibility spike. Keep transports internal until stable. Candidate public exports are `BrowserManager`, `BrowserPolicy`, and `create_browser_tools`; update `tests/public_api_surface.txt` intentionally.

Diagnostics must distinguish missing Python package, missing browser executable, unsupported architecture, and missing Linux system dependencies.

## 14. Detailed delivery phases

### Phase 0 — Design spike

1. Build a private Playwright spike against deterministic local pages.
2. Launch lazily and verify normal/cancelled cleanup.
3. Generate semantic snapshots and refs.
4. Click/type through refs.
5. Capture screenshots.
6. Exercise redirects, popups, frames, shadow DOM, and stale refs.
7. Measure snapshot size and latency.
8. Select a Playwright version compatible with the Python matrix.
9. Validate macOS ARM64 and Linux.
10. Decide whether Playwright accessibility APIs suffice or a controlled DOM extractor is required.

**Exit:** deterministic form flow, no process leaks, stable snapshot format, viable dependency matrix.

### Phase 1 — Secure local MVP

1. Add browser package, types, and errors.
2. Implement policy and URL validation.
3. Implement transport protocol and Playwright transport.
4. Implement manager/session ownership.
5. Implement page IDs and ref store.
6. Implement snapshots and truncation artifacts.
7. Implement all Phase 1 tools.
8. Add read-only/concurrency metadata.
9. Add permission previews and redaction.
10. Integrate cancellation and agent cleanup.
11. Add optional dependency and install diagnostics.
12. Register tools only when enabled/available, preferably deferred.
13. Add Phase 1 `/browser` commands.
14. Add docs/examples.

**Exit:** local app form workflow and screenshot pass; redirect policy and secret-leak tests pass; no cleanup leaks; core works without Playwright.

### Phase 2 — Developer diagnostics

1. Add bounded console/page-error buffers.
2. Add bounded request/response/failure buffers.
3. Add cursor-based filtering.
4. Add console, network, and wait tools.
5. Add dialogs.
6. Add controlled uploads/downloads.
7. Add trace-on-failure.
8. Add optional bounded response-body capture.
9. Add concise diagnostic summaries to action results.
10. Add TUI artifact links/rendering.

**Exit:** agent diagnoses console/API failures; file boundaries hold; traces survive failures; buffers remain bounded.

### Phase 3 — Hardening and cross-browser experiments

1. Fuzz redirects, popups, frames, downloads, and URL parsing.
2. Harden DNS/private-network/metadata protections.
3. Audit recursive redaction.
4. Add persistent isolated profiles with consent.
5. Experiment with Firefox/WebKit capabilities.
6. Add crash/restart recovery.
7. Enforce page/session limits and idle expiry.
8. Profile snapshot performance and caching.
9. Add accessibility/keyboard verification helpers.
10. Conduct adversarial security review.

### Phase 4 — CDP attachment

1. Implement CDP transport.
2. Validate endpoint and require consent.
3. Enumerate contexts/pages safely.
4. Require explicit page selection.
5. Respect transport capabilities.
6. Add disconnect/forget/status controls.
7. Never accidentally close the user's full browser.
8. Separate attached-browser policy.

### Phase 5 — Extension/native-host bridge

1. Define a versioned framed protocol.
2. Build extension/native host separately.
3. Validate socket ownership and modes.
4. Add reconnect/backoff and bounded timeouts.
5. Add device discovery, pairing, and opaque persisted IDs.
6. Support permission prompts/domain notices.
7. Add keepalive and mid-call disconnect handling.
8. Negotiate protocol/capabilities.
9. Sign/package components.
10. Threat-model impersonation, replay, stale pairing, and socket attacks.

### Phase 6 — Remote browser transport

1. Define authenticated versioned WebSocket protocol.
2. Add capability negotiation.
3. Add call IDs, cancellation, timeout, and idempotency rules.
4. Add device selection and session scoping.
5. Transfer artifacts with size/hash validation.
6. Add heartbeat, reconnect, and replay semantics.
7. Add TLS and credential rotation.
8. Review multi-tenant isolation.
9. Provide a reference worker and deployment guide.

### Phase 7 — Advanced computer use

1. Add browser-scoped coordinate input.
2. Bind actions to screenshot generations.
3. Add drag, hover, wheel, keys, and touch.
4. Add optional visual-grounding adapters.
5. Add tab groups/browser switching.
6. Add GIF/video recording.
7. Add reusable parameterized browser workflows.
8. Add mobile emulation profiles.
9. Add rich remote/thin-client browser events.
10. Treat desktop-wide control as a separate security project.

## 15. Testing strategy

### Unit tests

- Settings defaults and parsing.
- URL schemes, normalization, IDNs, and domain boundaries.
- Redirect/origin decisions and IP classifications.
- Metadata endpoint blocking.
- Ref generation, staleness, and re-resolution.
- Snapshot formatting and truncation.
- Recursive redaction.
- Upload/download roots, symlinks, traversal, and overwrite.
- Tool schemas, read-only hints, and concurrency.
- Error conversion and optional-dependency diagnostics.

### Deterministic integration site

Cover navigation, forms, client routing, popup/tabs, same/cross-origin frames, shadow DOM, dialogs, upload/download, console errors, API success/failure/delay, redirects, dynamic stale refs, large snapshots, password fields, and sensitive headers. Prefer a tiny stdlib or existing-dependency server.

### End-to-end tests

- Open → snapshot → type → click → verify.
- Start a dev server via Bash and verify in browser.
- Popup/tab flow.
- Upload/download flow.
- Console/network diagnosis.
- Screenshot artifacts.
- Permission deny, allow once, session allow.
- Hook mutation followed by policy recheck.
- Cancellation during navigation/action/download.
- Browser crash and cleanup.
- Headless JSON/streaming behavior.

### Security tests

- Blocked scheme variants.
- Unicode/encoded host confusion.
- `corp.com.evil.net` boundary bypass.
- Redirect/popup to blocked, private, or metadata targets.
- DNS rebinding simulation where practical.
- URL credential and query-secret redaction.
- Password, cookie, and authorization leakage.
- Upload symlink escape.
- Download traversal/overwrite.
- JavaScript disabled enforcement.
- Attached-browser consent.
- Bridge socket ownership/mode and remote replay/impersonation tests in later phases.

### Performance/reliability tests

- Cold import without Playwright.
- Launch latency.
- Repeated creation/cleanup.
- Large-page snapshot latency.
- Bounded buffers and memory growth.
- Max-page enforcement.
- Repeated cancellation.
- Artifact disk quota.
- Process leak detection.

### CI

- Unit/security jobs without browser extra.
- Chromium integration job.
- Linux and macOS ARM64 validation where available.
- Python 3.9 and newest Python as initial browser gates; expand after stabilization.
- Firefox/WebKit jobs only after declaring support.

## 16. Documentation

Add:

- `docs/guides/browser.md`: install, first workflow, tool usage, screenshots, debugging.
- `docs/guides/browser-security.md`: isolation, domain policy, profiles, uploads/downloads, JavaScript, attached browsers.
- `docs/api/browser.md`: public API and tool schemas.
- Example local-app verification script.
- Troubleshooting for missing binaries, Linux dependencies, certificates, timeouts, and headless/headed behavior.
- Comparison explaining when to use `web_fetch` versus browser tools.

Update README, docs navigation, changelog, and release notes only when implementation ships—not for this proposal alone.

## 17. File-level implementation map

Likely new files:

- `mantis_agent/browser/**`
- `tests/test_browser_policy.py`
- `tests/test_browser_refs.py`
- `tests/test_browser_snapshot.py`
- `tests/test_browser_tools.py`
- `tests/test_browser_security.py`
- `tests/test_browser_lifecycle.py`
- `tests/browser_site/**`
- `docs/guides/browser.md`
- `docs/guides/browser-security.md`
- `docs/api/browser.md`

Likely modified files:

- `pyproject.toml`
- `mantis_agent/agent.py`
- `mantis_agent/tools.py` only if context injection needs a minimal extension
- `mantis_agent/permissions.py`
- `mantis_agent/tool_preview.py`
- `mantis_agent/builtin_tools/__init__.py`
- `mantis_agent/__init__.py`
- `mantis_agent/cli.py`
- `mantis_agent/headless.py`
- `mantis_agent/tui.py`
- `mantis_agent/tui_fullscreen.py`
- `mantis_agent/settings.py`
- `tests/public_api_surface.txt`
- docs navigation files

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Browser dependency/binary is large | Optional extra, lazy import, explicit install |
| Semantic refs become stale | Generations, safe re-resolution, clear recovery errors |
| Models misuse selectors/coordinates | Semantic-first tools and prompts |
| Authenticated data leaks | Isolated defaults and centralized redaction |
| SSRF/private-network access | URL/IP/domain checks on every transition |
| Redirect bypass | Validate every hop and final origin |
| Browser processes leak | Owned manager, idempotent shielded cleanup, leak tests |
| Snapshots consume context | Interactive-only defaults, limits, scopes, artifacts |
| Concurrent actions race | Serialize mutations; add page locks before concurrency |
| User browser is damaged | Separate attached mode, explicit selection, no implicit close |
| Remote bridge is impersonated | Authenticated protocol, pairing, socket checks, TLS |
| Cross-browser behavior diverges | Capability negotiation and Chromium-first support |
| Hidden dependencies break Python support | Compatibility spike and CI matrix |

## 19. Release and rollout

1. Keep behind `browser.enabled` and optional dependency during preview.
2. Mark API experimental until Phase 2 stabilizes.
3. Ship Chromium ephemeral mode first.
4. Collect structured, sanitized failure classes—not page data.
5. Make changes to public exports intentional and snapshot-tested.
6. Do not enable attached/persistent/remote modes by default.
7. Complete security review before calling browser support stable.
8. Use the repository release checklist only when publishing an actual version: tests, version bump, changelog, build/wheel verification, and release operations.

## 20. MVP acceptance checklist

- [ ] Core package imports without Playwright.
- [ ] Missing-dependency guidance is actionable.
- [ ] Chromium launches lazily in ephemeral mode.
- [ ] Open, snapshot, click, type, select, scroll, screenshot, tabs, and close work.
- [ ] Semantic refs cover forms, frames, and common controls.
- [ ] Stale refs fail safely.
- [ ] Initial URLs and redirects are policy checked.
- [ ] Metadata/private-network policy is tested.
- [ ] Passwords and auth data are redacted everywhere.
- [ ] Permission previews describe semantic targets.
- [ ] Hook-mutated inputs are rechecked.
- [ ] Cancellation leaves no process or temporary profile.
- [ ] Model-visible outputs are bounded.
- [ ] Full safe artifacts are discoverable.
- [ ] TUI and headless modes work.
- [ ] Documentation and troubleshooting are complete.
- [ ] Unit, integration, security, lifecycle, and CI gates pass.

## 21. Recommended implementation order

The highest-agency path is:

1. Complete the compatibility/lifecycle spike.
2. Implement policy and URL validation before public navigation tools.
3. Build manager, transport, session, page IDs, and refs.
4. Ship snapshot/open/click/type first and test one full local workflow.
5. Add tabs/select/scroll/screenshots and complete MVP lifecycle handling.
6. Wire permissions, previews, deferred registration, TUI, headless, and docs.
7. Add console/network/waits, then controlled file transfer and traces.
8. Perform hardening before persistent or attached browser state.
9. Add CDP only after isolated mode is stable.
10. Treat extension/remote bridges as separate reviewed projects.
11. Add low-level computer actions last; semantic interaction should remain the default.
