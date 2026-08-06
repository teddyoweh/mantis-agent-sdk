# MCP OAuth and Dynamic Lifecycle — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/mcp/` (2,840 lines across `client.py`, `manager.py`, `server.py`, `types.py`)
**Objective:** Make hosted MCP servers usable through OAuth 2.1 with PKCE and secure token storage, then finish the connection lifecycle — bounded parallel startup, dynamic tool-list changes, reconnection, long-call handling, and output spill.

## 1. Executive summary

Mantis's MCP implementation is substantial and, in several places, notably careful. `manager.py` implements project-config trust with content hashing (`project_mcp_is_trusted`, `trust_project_mcp`, `_file_hash`, `filter_untrusted_project_servers`, gated by `MANTIS_MCP_TRUST_PROJECT`) — a cloned repository cannot silently add an MCP server. Tool names are namespaced `mcp__<server>__<tool>` with `_ns_segment` collapsing `__` in each segment so *"a server-supplied tool name can't inject an extra delimiter and impersonate another server's namespace."* `redact_mcp_entry`, `mask_secret`, `_redact_url`, and `_redact_args` keep credentials out of displays. `connect_all` catches `BaseException` deliberately, with a long comment explaining that a server dying around the handshake surfaces as `CancelledError` which *"an `except Exception` would let sail past, aborting the connect loop for every server after it, silently"* — and then calls `_drain_cancellation()` unconditionally so a queued cancellation is not blamed on the next server. That is the kind of detail that only gets written after someone debugged it.

Four gaps remain, and the first one gates access to most of the hosted MCP ecosystem.

**Authentication is static headers.** `SseServerConfig` and `HttpServerConfig` each carry `headers: dict[str, str]`, and that is the entire auth surface. Hosted MCP servers overwhelmingly use OAuth 2.1 with PKCE and dynamic client registration. With headers only, a user must obtain a bearer token out of band, paste it into a settings file, and re-paste it whenever it expires. Tokens land in a config file on disk. There is no refresh, no revocation, no per-server login state, and no way to use a server that requires an authorization-code flow at all.

**Startup is serial.** `connect_all` iterates `self.configs.items()` one at a time, documented as *"serially — startup order is deterministic and stdio spawns are cheap."* That is true for stdio. It is false for remote servers: eight HTTP servers each taking two seconds to handshake cost sixteen seconds of startup, serially, every session. The `timeout_s: float = 10.0` per server compounds — eight unreachable servers means eighty seconds before the first prompt.

**Tool lists are static after connect.** `connect_all` calls `list_tools()` once and adapts the result. MCP defines `notifications/tools/list_changed`, and a `notification_handler` exists on the client, but there is no path from that notification to a refreshed, re-namespaced, re-registered tool set. A server that adds a tool mid-session is invisible until restart.

**There is no reconnection.** A dropped remote connection or a crashed stdio subprocess leaves the server's tools registered but non-functional; every call fails until the session restarts. `MCPManager.aclose` and `stop` handle shutdown, but nothing handles recovery.

Two smaller items: tool results are returned whole with no spill for large outputs, and long-running tool calls hold a slot against `_MAX_INFLIGHT_SERVER_REQUESTS = 16` with a flat `request_timeout_s = 120.0` and no progress reporting.

## 2. Goals

### User outcomes

- `mantis mcp login <server>` opens a browser, completes OAuth, and the server works — with no token pasted into any file.
- Tokens refresh silently; an expired token never surfaces as a confusing tool error.
- `mantis mcp logout <server>` revokes access and removes stored credentials.
- Eight remote servers connect in about the time one takes.
- A server that adds tools mid-session exposes them without a restart.
- A dropped connection recovers automatically, with the failure visible while it is down.
- A tool returning 40 MB does not blow up the session's context or memory.

### Engineering goals

- Preserve `MCPClient`, `MCPManager`, `ServerConfig` variants, `load_mcp_server_configs`, `parse_server_entry`, `mcp_state`, and the whole redaction and trust surface.
- Keep the deliberate `BaseException` handling and `_drain_cancellation()` discipline in `connect_all`; parallelism must not lose it.
- Keep tool namespacing and `_ns_segment` sanitization exactly as they are.
- No new required dependency. OAuth uses `mantis_agent/http.py` and the stdlib; the loopback redirect server follows `serve.py`'s stdlib precedent.
- Credentials never touch the MCP config files.
- Python 3.9–3.14.

### Success metrics

- OAuth login succeeds against at least two real hosted MCP servers, including one requiring dynamic client registration.
- No token, refresh token, or client secret appears in any config file, log, trace, transcript, or status display.
- Eight remote servers connect within 1.5× the slowest single server's handshake.
- `list_changed` updates the registry within one second, with correct namespacing and no duplicate tools.
- A killed stdio server reconnects within the backoff window and its tools work again.
- Refresh is single-flight: 10 concurrent calls against an expired token produce exactly one refresh request.

## 3. Non-goals

- Implementing an MCP *server* beyond what `server.py` already provides.
- Supporting MCP spec versions beyond the negotiated `_PROTOCOL_VERSION = "2025-03-26"` plus whatever the OAuth work requires; version negotiation stays as-is.
- Replacing the trust model. Project-config trust already works and is reused.
- Hosting an OAuth authorization server.
- Sampling and elicitation redesign — `MCPElicitationRequest` and the sampling path stay as they are, except that `Elicitation` hook dispatch comes from `g_typed_hooks_and_full_lifecycle.md`.
- Cross-machine credential sync.

## 4. Current integration points

- `mantis_agent/mcp/types.py` (385 lines) — `StdioServerConfig`, `SseServerConfig`, `HttpServerConfig`, `SdkServerConfig` (tagged union on `type`), `MCPTool`, `MCPResource`, sampling structs with `max_tokens`.
- `mantis_agent/mcp/client.py` (890 lines) — `MCPClient`, `MCPError`, `MCPProtocolError`, `MCPElicitationRequest`, `_PROTOCOL_VERSION`, `_MAX_LIST_PAGES = 1000`, `_MAX_INFLIGHT_SERVER_REQUESTS = 16`, `_ELICITATION_ERROR_CODE = -32042`, `list_tools`, `list_resources`, `read_resource`, `close`, `notification_handler`, transport selection over `StdioTransport` / `SseTransport` / `HttpTransport`.
- `mantis_agent/mcp/manager.py` (781 lines) — `MCPManager` (`connect_all`, `status_rows`, `summary`, `aclose`, `start`, `stop`), config layering (`mcp_config_layers`, `load_mcp_server_configs`, `mcp_raw_entries`, `mcp_server_origin`), user-config mutation (`save_user_mcp_server`, `remove_user_mcp_server`), paste parsing (`parse_mcp_paste`, `parse_quick_mcp_entry`, `_strip_json_comments`, `_loads_lenient`), redaction (`_SECRETISH`, `mask_secret`, `_redact_url`, `_redact_args`, `redact_mcp_entry`), trust (`_mcp_trust_path`, `project_mcp_is_trusted`, `trust_project_mcp`, `filter_untrusted_project_servers`, `_TRUST_ENV`), namespacing (`_ns_segment`), `_drain_cancellation`, `_transport_label`.
- `mantis_agent/mcp/transports/` — `stdio.py`, `sse.py`, `http.py`, in-process.
- `mantis_agent/http.py` (206 lines) — HTTP client for token endpoints.
- `mantis_agent/anthropic_oauth.py` (194 lines) — **an existing OAuth implementation in this codebase.** Its flow, loopback handling, and storage patterns should be reused rather than reinvented.
- `mantis_agent/tools.py` — `ToolRegistry.add`, `defer`, `surface`, `deferred_tools`; dynamic tool updates land here.
- `mantis_agent/serve.py` — `mcp_state`, `add_mcp`, `test_mcp`, `delete_mcp`, `trust_project_mcp_file` in the dashboard.
- `mantis_agent/setup_wizard.py` — MCP setup flow.
- `mantis_agent/hooks.py` — `Elicitation` / `ElicitationResult`.
- `mantis_agent/paths.py` — credential storage location.

## 5. OAuth

### Flow

Implement OAuth 2.1 authorization code with PKCE, following the MCP authorization specification.

1. **Discovery.** On a `401` carrying `WWW-Authenticate` with `resource_metadata`, or on explicit login, fetch the protected-resource metadata, then the authorization-server metadata from `/.well-known/oauth-authorization-server` (falling back to `/.well-known/openid-configuration`).
2. **Client registration.** If the server supports dynamic client registration (RFC 7591), register and persist the resulting `client_id`. Otherwise use a configured `client_id`.
3. **Authorization.** Generate `code_verifier` (43–128 chars, `secrets.token_urlsafe`) and `code_challenge` = base64url(SHA-256(verifier)). Start a loopback HTTP listener on an ephemeral port. Open the browser to the authorization URL with `response_type=code`, `code_challenge_method=S256`, a cryptographically random `state`, and the `resource` parameter identifying the MCP server.
4. **Callback.** The loopback server accepts exactly one request, validates `state` in constant time, extracts `code`, renders a plain success page, and shuts down. It binds `127.0.0.1` only, never `0.0.0.0`, and times out after 5 minutes.
5. **Token exchange.** POST to the token endpoint with `code`, `code_verifier`, `redirect_uri`, and `resource`. Store the access token, refresh token, expiry, scope, and the issuer.
6. **Use.** Attach `Authorization: Bearer <token>` per request. This composes with existing static `headers`, which continue to work for servers that need them.

### Security requirements

- **PKCE is mandatory.** No plain `code_challenge_method`.
- **`state` is mandatory**, random, single-use, and compared with `secrets.compare_digest`.
- **Redirect URI is loopback-only** with an exact-match check on the callback.
- **Token audience binding.** Pass and validate the `resource` parameter so a token minted for one MCP server cannot be replayed against another. Refuse a token whose audience does not match the server.
- **Issuer validation.** The authorization server must be discovered from the resource's own metadata, not from user-supplied configuration that could point elsewhere.
- **URL validation at every step.** HTTPS required except for loopback; redirects revalidated; private-network and metadata addresses blocked — sharing the validator from `h_sandbox_egress_credentials_and_escape_controls.md`.
- **No tokens in config.** Credentials live in a separate store; `settings.json` and `.mcp.json` never contain them. This is the property that makes the existing `redact_mcp_entry` machinery unnecessary for OAuth servers rather than merely careful.

### Storage

`mantis_agent/mcp/credentials.py`:

- OS keychain where available (macOS Keychain, libsecret on Linux), falling back to a `0o600` JSON file in a `0o700` directory.
- Keyed by `(server_name, issuer, resource)` so re-pointing a server at a different host does not silently reuse a credential.
- Stored fields: access token, refresh token, expiry, scope, `client_id`, issuer, resource, obtained-at.
- Registered with the session redactor on load so a token cannot leak through tool output, traces, or transcripts.
- `mantis mcp logout` calls the revocation endpoint when advertised, then deletes locally regardless of revocation success — a failed revocation must not leave a credential on disk.

### Refresh

- Refresh proactively at 80% of lifetime, not on failure, so a long tool call does not fail mid-flight.
- **Single-flight per server.** Concurrent callers await one in-progress refresh via an `asyncio.Lock` plus a shared future. Ten tool calls hitting an expired token must produce one token request, not ten — several providers rate-limit or invalidate on concurrent refresh.
- On refresh failure with an invalid-grant response, mark the server `needs_login`, surface it in status, and fail subsequent calls with an actionable error naming the login command. Do not retry a dead refresh token in a loop.
- Rotate stored refresh tokens when the server issues a new one.
- A `401` mid-session triggers one refresh-and-retry, then `needs_login`.

## 6. Bounded parallel startup

### The change

`connect_all` becomes concurrent while preserving its error discipline exactly:

- Bounded by `maxConcurrentConnects` (default 8) via a semaphore.
- Each server's connect keeps its own `try/except BaseException`, its own `client.close()`, and its own `_drain_cancellation()`. **The per-server isolation is the reason parallelism is safe here**, and it must be preserved verbatim rather than hoisted.
- Results are collected and then **applied in configuration order**, so tool registration order stays deterministic even though connection order does not. The existing docstring's determinism claim remains true where it matters.
- Overall startup deadline (default 20 s). Servers not connected by then continue connecting in the background and register their tools when ready, rather than blocking the first prompt.
- stdio servers may still connect eagerly and cheaply; the concurrency limit primarily benefits remote transports.

### Lazy and deferred connection

Startup cost is not only latency; it is also context. Every connected server's tools go into the model's tool list.

- `"lazy": true` per server: register a placeholder and connect on first use, or surface through `tool_search` using the existing `ToolRegistry.defer` / `surface` machinery.
- Tool schemas from lazily-connected servers are fetched at connect time; until then the server contributes a single deferred entry.
- This mirrors what the browser plan specifies for optional tooling and uses infrastructure that already exists.

## 7. Dynamic tool lifecycle

### `list_changed`

- Register a handler for `notifications/tools/list_changed` on the existing `notification_handler` seam.
- On notification: debounce (default 500 ms), re-run `list_tools()`, re-apply the existing dedup and unnamed-tool warnings, re-namespace with `_ns_segment`, and diff against the current set.
- Apply the diff to the `ToolRegistry`: add new tools, remove withdrawn ones, update changed schemas.
- **A tool removed mid-turn must not vanish from under an in-flight call.** Mark it withdrawn and remove it after the current turn completes; a call to a withdrawn tool returns a structured error.
- Emit an activity event so the change is visible; log it.
- Rate-limit refreshes per server so a misbehaving server cannot cause continuous re-listing.

### Resources and prompts

The same treatment for `notifications/resources/list_changed` and prompts, where `list_resources` and `read_resource` already exist.

### Reconnection

- Detect disconnect: transport close, stdio process exit, HTTP stream end, or request failure with a connection-class error.
- Mark the server `disconnected`; its tools return a structured `MCPServerDisconnectedError` rather than a generic failure, so the model can adapt.
- Reconnect with exponential backoff and jitter (default 1 s → 60 s, unlimited attempts while the session lives, or a configured cap).
- On reconnect, re-run initialize and `list_tools`, re-apply namespacing, and diff into the registry as with `list_changed`.
- Re-authenticate if the server now requires it.
- Surface state transitions in `status_rows()` and the activity registry.
- **Never silently drop a server.** A permanently failed server stays visible in status with its last error; today's `self.errors[name]` behavior is the right precedent and should persist across the session.

## 8. Long calls and large outputs

### Progress

- Support the MCP progress notification so a long call reports activity instead of appearing hung.
- Route progress into `NodeActivity` on the activity registry, exactly like a subagent's progress.
- Keep `request_timeout_s = 120.0` as the default but make it per-server configurable, and reset the timeout on progress so a genuinely-working long call is not killed at an arbitrary boundary.
- Cancellation propagates a `notifications/cancelled` to the server.

### Concurrency

`_MAX_INFLIGHT_SERVER_REQUESTS = 16` is a per-client cap. Add:

- A per-server queue with a bounded wait and a clear error when saturated, rather than unbounded queueing.
- Long calls tracked in the activity registry so a saturated server is diagnosable.

### Output spill

A tool returning tens of megabytes currently flows straight into the result path.

- Cap model-visible output (default 64 KB) with head-and-tail preservation and an explicit truncation notice.
- Spill the full result to an artifact file under session state, with the path returned in the result.
- Reuse the spill machinery from `b_durable_jobs_and_reattachment.md`; do not write a second implementation.
- Redact on write.
- Apply per-call and per-session artifact quotas.

## 9. Security

Beyond the OAuth requirements in §5:

- **Server output is untrusted.** MCP tool results enter the model's context and are authored by a third party. They must pass through the same neutralization the child-report path uses in `e_subagent_trust_limits_and_isolation.md`: strip control and bidi characters, escape framing markers, and label provenance as `mcp:<server>`. A server that returns `<system-reminder>` text is exactly the confused-deputy case, and it is more likely than a malicious subagent because MCP servers are third-party code by definition.
- **Tool descriptions are untrusted too.** They go into the system prompt. Sanitize and length-cap them at adaptation time, and re-sanitize on `list_changed` refresh — an initially-benign server that later swaps in an instruction-shaped description is a live threat, and `list_changed` is the mechanism.
- **Namespacing stays.** `_ns_segment` collapsing is a real defense and must be applied on every refresh path, not only initial connect.
- **Trust gating extends to OAuth.** A project-supplied server config that triggers a browser OAuth flow is a phishing vector. Untrusted project servers must not be able to initiate login; trust first, then authenticate.
- **Credential scope.** A credential is bound to `(server, issuer, resource)`; changing any of them requires re-login. Editing a server's URL invalidates its stored credential.
- **Elicitation.** Server-initiated user prompts (`_ELICITATION_ERROR_CODE = -32042`) must be clearly attributed to the server and must never be able to request credentials or approve permissions.
- **Redaction.** Extend `_SECRETISH` coverage to token responses and OAuth metadata; consolidate with the shared redactor.

## 10. Configuration

```json
{
  "mcpServers": {
    "example": {
      "type": "http",
      "url": "https://mcp.example.com",
      "lazy": false,
      "auth": {
        "type": "oauth",
        "scopes": ["read", "write"],
        "clientId": null,
        "authorizationServer": null
      },
      "timeoutMs": 120000,
      "maxOutputBytes": 65536
    }
  },
  "mcp": {
    "maxConcurrentConnects": 8,
    "startupDeadlineMs": 20000,
    "connectTimeoutMs": 10000,
    "reconnect": {"enabled": true, "initialMs": 1000, "maxMs": 60000, "maxAttempts": 0},
    "listChanged": {"enabled": true, "debounceMs": 500, "maxRefreshesPerMinute": 6},
    "output": {"maxVisibleBytes": 65536, "spill": true, "sessionQuotaBytes": 536870912},
    "oauth": {
      "callbackHost": "127.0.0.1",
      "callbackTimeoutMs": 300000,
      "refreshAtFraction": 0.8,
      "storage": "keychain"
    },
    "sanitizeServerOutput": true
  }
}
```

`sanitizeServerOutput` may only be `true`, for the same reason `report.neutralize` may only be `true` in the subagent plan: it is a security baseline, and the key exists so its state is inspectable rather than so it can be disabled.

Environment: `MANTIS_MCP_TRUST_PROJECT` (existing), plus `MANTIS_MCP_NO_OAUTH=1` and `MANTIS_MCP_LAZY=1`.

## 11. Surface

```text
mantis mcp list                     servers, transport, auth state, tools, status
mantis mcp login <server>           OAuth flow
mantis mcp logout <server>          revoke and delete
mantis mcp status <server>          detailed: token expiry, scopes, last error
mantis mcp reconnect <server>
mantis mcp tools <server>           current tool list with schemas
mantis mcp test <server>            existing; extended with auth diagnostics
mantis mcp trust                    existing project trust
```

In the TUI, `/mcp` gains auth and connection state:

```text
/mcp
  github      http    ● connected     42 tools   oauth ✓ (expires 47m)
  linear      http    ● connected     18 tools   oauth ✓ (expires 3h)
  local-fs    stdio   ● connected      6 tools   —
  jira        http    ○ needs login    0 tools   oauth ✗ refresh failed
  legacy      sse     ◐ reconnecting   9 tools   retry in 8s (attempt 3)
```

`mcp_state()` in `serve.py` gains the same fields so the dashboard reflects them. Login is not initiated from the dashboard — a browser OAuth flow triggered from a web page is a confused-deputy risk; the dashboard shows state and directs the user to the CLI.

## 12. Errors

Extend the existing `MCPError` / `MCPProtocolError` hierarchy:

```text
MCPError                              (existing, JSON-RPC code carrier)
MCPProtocolError                      (existing)
├── MCPAuthRequiredError              # 401, no credential
├── MCPAuthFailedError                # refresh/exchange failed
├── MCPOAuthDiscoveryError
├── MCPOAuthStateMismatchError
├── MCPOAuthAudienceError             # token audience ≠ resource
├── MCPCallbackTimeoutError
├── MCPServerDisconnectedError
├── MCPReconnectExhaustedError
├── MCPToolWithdrawnError
├── MCPOutputTooLargeError            # handled by spill; recorded
├── MCPServerSaturatedError
└── MCPUntrustedServerError           # project server not yet trusted
```

Every auth error names the exact command that fixes it.

## 13. Delivery phases

### Phase 0 — Spike

1. Study `anthropic_oauth.py` and extract the reusable flow, loopback, and storage patterns.
2. Prototype discovery, PKCE, and token exchange against a real hosted MCP server.
3. Measure serial versus parallel connect for eight remote servers.
4. Verify `notification_handler` can carry `list_changed` without protocol changes.
5. Test keychain availability across macOS and Linux; confirm the fallback path.

**Exit:** OAuth works end to end against one real server; parallel connect measured; storage strategy chosen.

### Phase 1 — Parallel startup and lazy connect

1. Make `connect_all` concurrent with a semaphore, preserving per-server `BaseException` isolation and `_drain_cancellation()`.
2. Apply results in configuration order for deterministic registration.
3. Add the startup deadline with background completion.
4. Add `lazy` servers over `ToolRegistry.defer` / `surface`.
5. Add status reporting for in-progress connects.

**Exit:** eight servers connect in ~one server's time; error isolation is unchanged, proven by a test that kills a server mid-handshake.

### Phase 2 — OAuth core

1. Add `mcp/oauth.py` (discovery, PKCE, loopback callback, exchange) and `mcp/credentials.py` (keychain, fallback, redaction).
2. Add the `auth` config block and `AuthConfig` to the tagged union.
3. Implement `mantis mcp login` / `logout` / `status`.
4. Implement audience and issuer validation and shared URL validation.
5. Gate login behind project trust.

**Exit:** hosted servers work with no token in any config file.

### Phase 3 — Refresh and recovery

1. Implement proactive refresh at 80% lifetime.
2. Implement single-flight refresh with a lock and shared future.
3. Implement `401` retry-once and `needs_login` state.
4. Implement refresh-token rotation.
5. Add actionable errors naming the login command.

**Exit:** ten concurrent calls on an expired token produce one refresh; expiry never surfaces as a confusing failure.

### Phase 4 — Dynamic lifecycle

1. Implement `list_changed` with debounce, rate limit, and registry diffing.
2. Implement withdrawal semantics safe against in-flight calls.
3. Implement disconnect detection and backoff reconnection.
4. Re-authenticate and re-list on reconnect.
5. Surface all transitions in status and the activity registry.

**Exit:** tools appear and disappear correctly; a killed server recovers.

### Phase 5 — Long calls and output

1. Implement progress notifications and timeout reset on progress.
2. Route progress into the activity registry.
3. Implement cancellation propagation.
4. Implement output caps and artifact spill with quotas.
5. Add per-server queueing with a saturation error.

**Exit:** a 40 MB result is usable and bounded; long calls report progress.

### Phase 6 — Server output trust and hardening

1. Apply neutralization to tool results and tool descriptions, on connect and every refresh.
2. Adversarial review: OAuth state/audience, phishing via project configs, description injection on refresh.
3. Fuzz discovery documents, token responses, and notification payloads.
4. Leak tests for clients, transports, subprocesses, and callback servers.
5. Remove experimental gating.

## 14. Testing strategy

### Unit

- PKCE verifier/challenge correctness against known vectors.
- `state` generation, constant-time comparison, single use, mismatch rejection.
- Discovery document parsing: valid, missing fields, wrong issuer, malformed.
- Audience validation: matching, mismatched, absent.
- Token storage: keychain and fallback, permissions, key derivation from `(server, issuer, resource)`.
- Refresh: proactive timing, single-flight under 10 concurrent callers, rotation, invalid-grant handling.
- Callback server: single request, timeout, loopback-only bind, exact redirect match.
- Parallel connect: semaphore bound, per-server isolation, deterministic registration order.
- `list_changed` diffing: add, remove, schema change, duplicate names, unnamed tools, namespace collapse.
- Backoff schedule and jitter.
- Output truncation, spill, quota.
- Server output and description sanitization against the injection corpus.

### Integration

- Real OAuth against a test authorization server, including dynamic registration.
- Eight mock remote servers: parallel connect timing and correctness.
- A server killed mid-handshake does not abort the connect loop (the existing `_drain_cancellation` guarantee, under concurrency).
- `list_changed` mid-session updates the registry; a withdrawn tool called mid-turn errors cleanly.
- stdio server killed and reconnected; tools work again.
- Expired token mid-tool-call refreshes transparently.
- Untrusted project server cannot initiate login.

### End-to-end

- `mantis mcp login`, use tools, `logout`, verify revocation and deletion.
- Full session with a lazy server surfaced through `tool_search`.
- 40 MB tool result: bounded context, artifact written, quota respected.
- `/mcp` and the dashboard reflect auth and connection state accurately.

### Security

- No token in config files, logs, traces, transcripts, status output, or the dashboard — asserted by scanning all sinks.
- `state` mismatch and replay rejected.
- Token minted for server A rejected for server B.
- Callback bound to loopback only; external connection refused.
- Discovery pointed at a private or metadata address is refused.
- MCP tool result containing framing markers is neutralized.
- Tool description swapped to instruction-shaped text on `list_changed` is sanitized.
- Project server config cannot trigger a browser flow before trust.
- Elicitation cannot request credentials or approve a permission.

### Performance and reliability

- Startup latency: 1, 4, 8, 16 servers, serial versus parallel.
- Refresh under concurrency.
- `list_changed` storm rate limiting.
- Reconnect storm across many servers.
- Leak test: 200 connect/disconnect cycles with no leaked processes, sockets, or tasks.

## 15. Documentation

- `docs/guides/mcp.md` — update with auth, lazy servers, dynamic tools, reconnection.
- `docs/guides/mcp-oauth.md` — login, what is stored where, refresh, revocation, troubleshooting.
- `docs/guides/mcp-security.md` — trust model, untrusted server output, phishing risks, namespacing.
- `docs/api/mcp.md` — config schema including `auth`, public API.
- Troubleshooting: browser did not open, callback timed out, refresh failed, server disconnected.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 16. File-level implementation map

New:

- `mantis_agent/mcp/oauth.py`
- `mantis_agent/mcp/credentials.py`
- `mantis_agent/mcp/discovery.py`
- `mantis_agent/mcp/lifecycle.py` — reconnect, `list_changed`, registry diffing
- `mantis_agent/mcp/sanitize.py` — server output and description neutralization
- `mantis_agent/mcp/spill.py` — thin adapter over the shared spill implementation
- `tests/test_mcp_oauth_flow.py`
- `tests/test_mcp_oauth_security.py`
- `tests/test_mcp_credentials.py`
- `tests/test_mcp_refresh_singleflight.py`
- `tests/test_mcp_parallel_connect.py`
- `tests/test_mcp_list_changed.py`
- `tests/test_mcp_reconnect.py`
- `tests/test_mcp_output_spill.py`
- `tests/test_mcp_sanitize.py`
- `docs/guides/mcp-oauth.md`
- `docs/guides/mcp-security.md`

Modified:

- `mantis_agent/mcp/types.py` — `AuthConfig`, `lazy`, per-server timeouts and caps
- `mantis_agent/mcp/client.py` — auth headers, refresh hook, progress, cancellation, `list_changed` handler
- `mantis_agent/mcp/manager.py` — parallel `connect_all`, lifecycle, status fields
- `mantis_agent/mcp/transports/*.py` — disconnect detection
- `mantis_agent/tools.py` — dynamic add/remove with withdrawal safety
- `mantis_agent/http.py` — shared URL validation
- `mantis_agent/serve.py` — `mcp_state` auth fields; no login from the dashboard
- `mantis_agent/cli.py` — `mcp login/logout/status/reconnect/tools`
- `mantis_agent/tui_fullscreen.py` — `/mcp` state display
- `mantis_agent/setup_wizard.py` — OAuth in setup
- `tests/public_api_surface.txt` — intentional update

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Parallel connect loses the careful error isolation | Per-server `try/except BaseException` and `_drain_cancellation()` preserved verbatim; test kills a server mid-handshake under concurrency |
| Tool registration order becomes nondeterministic | Connect concurrently, apply results in configuration order |
| OAuth tokens leak into config or logs | Separate credential store; registered with the session redactor; sink-scanning test |
| Token replayed across servers | Audience binding via the `resource` parameter, validated on receipt |
| Concurrent refresh invalidates the token | Single-flight lock with a shared future |
| Project config phishes the user into an OAuth flow | Trust gating precedes any login; dashboard cannot initiate login |
| Malicious server injects instructions via tool results | Shared neutralization; provenance labeling |
| Malicious server swaps in a hostile description on refresh | Sanitization on every refresh path, not just connect |
| `list_changed` storm | Debounce plus per-minute rate limit |
| Tool withdrawn mid-call | Withdrawal deferred to turn end; structured error otherwise |
| Reconnect loops forever on a dead server | Backoff cap and optional attempt limit; server stays visible with its last error |
| Keychain unavailable | `0o600` fallback with an explicit status note |
| Large results exhaust memory | Caps, spill, quotas, shared with the jobs implementation |

## 18. Acceptance checklist

- [ ] OAuth 2.1 with PKCE works, including dynamic client registration.
- [ ] `state` is random, single-use, and constant-time compared.
- [ ] Tokens are audience-bound and validated against the server.
- [ ] Callback binds loopback only, accepts one request, and times out.
- [ ] No credential appears in any config, log, trace, transcript, or display.
- [ ] Refresh is proactive, single-flight, and rotates refresh tokens.
- [ ] Failed refresh yields `needs_login` with an actionable message.
- [ ] `logout` revokes where supported and always deletes locally.
- [ ] `connect_all` is concurrent, bounded, and preserves per-server error isolation.
- [ ] Registration order remains deterministic.
- [ ] Lazy servers register through the deferred-tool mechanism.
- [ ] `list_changed` updates the registry with correct namespacing and no duplicates.
- [ ] Tools withdrawn mid-turn do not break in-flight calls.
- [ ] Disconnected servers reconnect with backoff and stay visible while down.
- [ ] Long calls report progress and reset their timeout.
- [ ] Large outputs are capped and spilled with quotas.
- [ ] Server tool results and descriptions are sanitized on connect and refresh.
- [ ] Untrusted project servers cannot initiate login.
- [ ] `ruff check` and the full pytest suite pass.

## 19. Recommended implementation order

1. **Parallel connect first.** It is independent of OAuth, delivers immediate user-visible benefit, and its main risk — losing the error isolation — is testable in isolation before any auth work exists.
2. **Server output and description sanitization second.** Small, security-critical, and independent of everything else. MCP servers are third-party code whose output already reaches the model today.
3. **Read `anthropic_oauth.py` before writing `mcp/oauth.py`.** The codebase already has a working OAuth flow; a second one that behaves differently is a liability.
4. **Ship credential storage before the flow.** Where tokens live is the decision that is expensive to change later.
5. **Ship OAuth login/logout with trust gating in the same release.** A login flow reachable from an untrusted project config is a phishing vector, and adding the gate afterward means shipping the vector.
6. **Add refresh immediately after login.** Login without refresh produces a worse experience than static headers, because it works and then mysteriously stops.
7. **Add reconnection and `list_changed` together** — they share the registry-diffing code, and building one without the other duplicates it.
8. **Add long-call progress and output spill last**, reusing the jobs spill implementation rather than writing a second one.
