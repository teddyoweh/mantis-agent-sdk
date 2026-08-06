# Session Event API, Remote Control, Web, Mobile, and Teleport — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/serve.py`, a new `mantis_agent/protocol/` package, and a session daemon
**Objective:** Define one versioned session/control/event protocol, serve it from a local daemon with authenticated clients and exclusive controller leases, then build web, mobile, and IDE surfaces and transactional session handoff on top of it — instead of a second runtime per surface.

## 1. Executive summary

Mantis already ships a remote surface. `mantis_agent/serve.py` is 1,345 lines and `serve_ui.py` is 2,283 more: a stdlib `ThreadingHTTPServer` that renders a self-contained dashboard over sessions, models, config, skills, MCP entries, and analytics. It is better engineered than most first attempts — it uses `secrets.compare_digest` for token comparison, it mints a fresh `secrets.token_urlsafe(12)` per run, it requires the token for every write, and `_host_ok()` implements a genuine DNS-rebinding defense with an explicit comment about why a rebound hostname still sends the attacker's `Host` header.

It is also structurally unable to become the remote-control surface, for reasons that are architectural rather than incidental.

**It observes; it does not participate.** The module docstring says *"Everything is read straight from `~/.mantis-agent` — nothing is mutated."* That is no longer quite true — `POST /api/key`, `/api/connect`, `/api/mcp`, and the skill endpoints all mutate configuration — but it is true of *sessions*. There is no way to send a prompt, answer a permission prompt, stop a job, or steer a run. It reads the on-disk stores of sessions that other processes own. It cannot reach a live session at all.

**There is no event stream.** Every view polls. `_projects_signature()` exists precisely to make polling cheaper by detecting change cheaply. Polling is the right call for a dashboard over files; it cannot express "a permission prompt is waiting for you right now," which is the defining requirement of remote control.

**There is no protocol.** The endpoints are ad-hoc JSON shaped by what the one HTML page needed. There is no version, no capability negotiation, no schema, and no contract another client could implement against. An IDE extension, a mobile app, and an automation script would each have to reverse-engineer the dashboard's private API and would each break on the next UI change.

**Transport and auth do not survive leaving loopback.** `--lan` binds `0.0.0.0` and puts the token in the URL query string (`/?k=<token>`). Over a LAN that is plaintext HTTP: the token is visible to anyone on the network, and it lands in browser history, in referrer headers on any outbound link, and in any intermediary log. On loopback, reads require no token at all. That posture is defensible for a local dashboard and indefensible for a surface that can approve a `rm -rf`. There is no TLS, no per-client identity, no revocation, and no lease.

**`ThreadingHTTPServer` is wrong for streams.** One thread per connection is fine for short polling requests and poor for many long-lived event streams.

This plan keeps `serve.py` as a dashboard, extracts a real protocol beneath it, and builds every remote surface on that protocol. The protocol's event vocabulary is the activity envelope from `a_activity_graph_and_inline_rail.md` — that plan's journal is this plan's replay log, and building this without it means inventing a second event model.

## 2. Goals

### User outcomes

- Open a session from a phone or another machine, see live output, and answer a permission prompt.
- Send a prompt to a running session from outside the terminal that started it.
- Have exactly one controller at a time, with explicit handoff — never two surfaces racing to answer the same prompt.
- Reconnect after a network drop and receive the events missed, not a truncated view.
- Move a session from a laptop to a workstation and continue it, with a clear transactional handoff.
- Build a client — IDE extension, script, bot — against a documented, versioned protocol.

### Engineering goals

- One protocol serving TUI, headless, web, mobile, and IDE. No surface gets its own runtime.
- Reuse the activity envelope and journal rather than defining a parallel event model.
- Keep `mantis serve` working exactly as today; it becomes one client of the daemon.
- Stdlib-first, consistent with the existing server's no-extra-dependency stance; an optional extra may add a better transport.
- Bounded everything: connections, subscriptions, replay window, message size, backpressure.
- Fail closed on authentication, authorization, and version mismatch.
- Python 3.9–3.14.

### Success metrics

- A permission prompt raised in a terminal session is answerable from a second surface in under 500 ms end to end on loopback.
- Reconnect after a 60-second drop replays every missed event with no gaps and no duplicates.
- Exactly one controller lease is held at any moment, proven under a concurrent-claim stress test.
- No token ever appears in a URL, a log, or a referrer.
- Protocol version mismatch produces a clear, actionable error rather than a partial connection.
- Daemon idle cost under 10 MB RSS and negligible CPU.

## 3. Non-goals

- A hosted cloud service. Everything here is local-first: a daemon on the user's machine, optionally reachable over their own network or their own tunnel.
- Rewriting `serve_ui.py`. The dashboard keeps its markup and gains live data.
- Multi-user collaboration semantics. One user, multiple devices. Shared editing is out of scope.
- Replacing the SDK. In-process embedding stays the primary integration path.
- Mobile native apps in the first phases. A responsive web client reached over the protocol comes first.
- The IDE extension itself — `r_ide_integrations.md` builds that on this protocol.
- Inbound third-party events — `s_channels_and_reactive_operation.md` owns webhooks and chat.

## 4. Current integration points

- `mantis_agent/serve.py` — `_Handler`, `_auth_ok`, `_host_ok`, `_route`, `list_projects`, `sessions_for`, `session_detail`, `overview`, `analytics`, `_projects_signature`, `_redact_settings`, `_SECRET_KEY_RE`, and the `--lan` / token plumbing in `main`.
- `mantis_agent/serve_ui.py` — `INDEX_HTML`, `MANTIS_SVG`, `__TOKEN__` substitution.
- `mantis_agent/activity/` — the event envelope, registry, and journal from `a_activity_graph_and_inline_rail.md`. This is the protocol's payload.
- `mantis_agent/session.py` — `Session`, `SessionStore`, `SqliteSessionStore`, `SessionInfo`, `Checkpoint`.
- `mantis_agent/session_tree.py` — `SessionTranscript`, `TranscriptEntry`, `load_for_resume`, `branch_session`, `rewind_chain`, `list_sessions`, `iter_session_files`. Teleport builds on these.
- `mantis_agent/permissions.py` — `AskerFn`; a remote asker is how a phone answers a prompt.
- `mantis_agent/hooks.py` — `Notification` events feed push.
- `mantis_agent/jobs.py`, `workflow.py` — control targets.
- `mantis_agent/headless.py` — a protocol client rather than a separate output path.
- `mantis_agent/http.py` — URL validation for outbound tunnel checks.
- `mantis_agent/paths.py` — socket and state locations.

## 5. Architecture

### Components

```text
┌─ mantis TUI ────────┐   ┌─ mantis headless ──┐   ┌─ IDE ext ─┐   ┌─ phone ─┐
│  in-process session │   │  in-process session│   │  client   │   │  client │
└──────────┬──────────┘   └─────────┬──────────┘   └─────┬─────┘   └────┬────┘
           │ registers               │ registers          │ connects     │
           ▼                         ▼                    ▼              ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │                        mantis daemon (mantisd)                        │
   │  session registry · event fan-out · replay · leases · auth · pairing  │
   └───────────────────────────────────────────────────────────────────────┘
           ▲ unix socket (local)            ▲ TLS TCP (LAN / tunnel)
```

### Transport

Two transports, one protocol:

- **Local:** a Unix domain socket at `~/.mantis/run/mantisd.sock`, mode `0o600`, in a `0o700` directory. Peer credentials verified via `SO_PEERCRED` on Linux and `LOCAL_PEERCRED` on macOS — a same-UID check that is stronger than any token because it cannot be stolen or replayed. Windows falls back to a loopback TCP socket with a token file at `0o600`.
- **Remote:** TCP with TLS, off by default. Certificate is self-signed and generated on first use; clients pin its fingerprint at pairing time. Plain HTTP is never offered for remote control — this is the single most important departure from `--lan` today.

Framing is newline-delimited JSON over both, so the protocol is inspectable with `nc` and implementable in any language without a library.

### Why a daemon

Sessions currently live inside the process that started them. A remote client must reach a live session, and a session must outlive a client's connection. The daemon is the rendezvous point. It deliberately does **not** run agent loops: sessions register with it and stream through it. That keeps the daemon small, restartable, and unable to become a second execution engine — the same discipline `a_activity_graph_and_inline_rail.md` applies to the registry.

A session process that loses the daemon keeps running and re-registers when it returns. The daemon is not on the critical path of doing work; it is on the critical path of *watching* work.

### Concurrency

Replace `ThreadingHTTPServer` for the protocol path with a single-threaded `asyncio` server. One event loop, bounded per-client queues, explicit backpressure. `serve.py`'s dashboard may keep its threaded server and become a protocol client, which avoids destabilizing working code.

## 6. Protocol

### Envelope

```json
{"v": 1, "id": "c7", "t": "req", "op": "session.subscribe", "d": {...}}
{"v": 1, "id": "c7", "t": "res", "ok": true, "d": {...}}
{"v": 1, "t": "ev", "seq": 4821, "d": {"type": "node_status", ...}}
{"v": 1, "id": "c7", "t": "err", "code": "not_authorized", "msg": "..."}
```

- `v` is the protocol major version. A mismatch is refused at handshake with the supported range, never silently downgraded.
- `t` is `req` / `res` / `ev` / `err`.
- `id` correlates request and response; events are unsolicited and carry `seq`.
- `d` for events is the **activity envelope struct** from `a_activity_graph_and_inline_rail.md`, encoded exactly as `msgspec` produces it. There is no translation layer and no second schema.

### Handshake

```json
→ {"v":1,"t":"req","id":"1","op":"hello",
   "d":{"client":"mantis-mobile/0.3","protocol":[1],"caps":["control","stream","replay"]}}
← {"v":1,"t":"res","id":"1","ok":true,
   "d":{"server":"mantisd/2.62.0","protocol":1,
        "caps":["control","stream","replay","teleport"],
        "auth":"required","session_count":3}}
```

Capability negotiation is explicit. A client asks for what it can do; the server replies with the intersection. An operation outside the negotiated set is refused rather than attempted — the same rule the browser plan applies to transports.

### Operations

**Discovery**

| Op | Returns |
|---|---|
| `session.list` | Live and recent sessions with id, cwd, title, status, controller |
| `session.get` | Full detail for one session |
| `project.list` | Projects, reusing `serve.py`'s `list_projects` |

**Streaming**

| Op | Notes |
|---|---|
| `session.subscribe` | Args: `session_id`, `from_seq`, `filter`. Streams activity events |
| `session.unsubscribe` | |
| `transcript.tail` | Streams message-level events for a conversation view |

**Control** (requires a lease)

| Op | Notes |
|---|---|
| `control.acquire` | Claim the controller lease |
| `control.release` | |
| `control.heartbeat` | Keeps the lease alive |
| `session.prompt` | Send a user turn |
| `session.interrupt` | Cancel the current turn |
| `permission.respond` | Answer a pending ask: `allow_once`, `allow_session`, `deny` |
| `node.action` | `stop` / `pause` / `resume` / `retry` / `skip` on an activity node |
| `plan.approve` | Approve a presented plan |
| `session.mode` | Change permission mode (narrowing only from remote) |

**Lifecycle**

| Op | Notes |
|---|---|
| `session.create` | Start a new session in a given cwd |
| `session.resume` | Resume by id via `session_tree.load_for_resume` |
| `session.fork` | Via `branch_session` |
| `session.teleport.*` | See §9 |

**Meta**

| Op | Notes |
|---|---|
| `ping` | Liveness |
| `caps` | Re-negotiate after a server upgrade |
| `pair.*` | Device pairing |

### Replay and delivery

- Every event carries the monotonic per-session `seq` the activity registry already assigns.
- `session.subscribe` with `from_seq` replays from the journal, then switches to live with no gap and no duplicate. The switchover is the correctness-critical moment: the daemon buffers live events during journal read and de-duplicates by `seq`.
- Replay is bounded by the journal retention window. A client asking for a `seq` older than retention gets an explicit `replay_truncated` response carrying the earliest available `seq` and a state snapshot, rather than a silent partial stream.
- Delivery is at-least-once with `seq` de-duplication at the client. Exactly-once is not attempted.
- Backpressure: per-client bounded queue. A slow client is warned, then coalesced (activity events collapse to latest-per-node), then disconnected with `slow_consumer`. It is never allowed to grow the server's memory.

## 7. Authentication and authorization

### Local

Same-UID peer credential check on the Unix socket. No token. A process running as the user already has the user's authority; adding a token would be theater.

### Remote pairing

Tokens in URLs are the current weakness and are removed entirely.

1. User runs `mantis remote pair` on the host. It displays a short-lived (120 s) numeric code and a QR containing host, port, and the TLS certificate fingerprint.
2. The client connects over TLS, pins the fingerprint, and submits the code.
3. Host and client complete a challenge-response over the TLS channel; the client receives a long-lived device credential bound to that certificate.
4. The credential is stored in the OS keychain where available, otherwise in a `0o600` file.
5. Credentials are per device, named, listable, and revocable: `mantis remote devices`, `mantis remote revoke <name>`.

Rules:

- Credentials never appear in a URL, a query string, a log, or a referrer.
- Pairing requires physical access to the host at pairing time. There is no remote enrollment.
- Rate-limit and lock out code attempts; the code is single-use.
- Certificate rotation invalidates pinned credentials and requires re-pairing, which is correct.

### Authorization

Not every paired device gets every capability:

```json
{"device": "phone", "caps": ["stream", "respond_permission"], "sessions": "*"}
{"device": "workstation", "caps": ["stream", "control", "teleport"], "sessions": "*"}
{"device": "ci-bot", "caps": ["stream"], "sessions": "project:/srv/app"}
```

Remote-specific restrictions, enforced server-side:

- A remote client may only **narrow** the permission mode. It can never set `bypass`, and it can never widen from `default` to `acceptEdits`. A stolen phone must not be able to disable the permission system.
- `dangerouslyDisableSandbox` approval is refused from remote clients entirely.
- Escalating operations may require a host-side confirmation depending on policy (`remote.confirmOnHost`).

### Controller leases

Two surfaces answering one permission prompt is a correctness bug, not a UX wrinkle.

- Exactly one client holds the lease per session. `control.acquire` succeeds only if free or expired.
- The lease has a TTL (default 30 s) refreshed by `control.heartbeat`. A dead client's lease expires rather than deadlocking the session.
- The local TUI holds the lease implicitly while focused and yields it on explicit handoff.
- `control.acquire` with `force: true` requires host-side confirmation and notifies the displaced holder.
- Every lease transition is an activity event, so the record shows who controlled the session when.
- Read-only subscription never requires a lease; watching is always allowed to any authorized device.

## 8. Push notifications

The value of a remote surface is largely "tell me when you need me."

- Notification-class events: permission ask pending, plan awaiting approval, run complete, run failed, job terminal, budget exhausted, teammate idle.
- Delivery: a long-poll or stream endpoint for connected clients; OS-level notification on the host via existing mechanisms; optional user-configured webhook (subject to the URL validation rules in `h_sandbox_egress_credentials_and_escape_controls.md`).
- Notification content is **metadata only** by default — session title, event type, node kind. Prompt text, file contents, and command strings are fetched over the authenticated channel, never embedded in a push payload that may traverse third-party infrastructure.
- Coalescing and rate limiting per session so a chatty watch cannot become a notification storm.

## 9. Teleport

Moving a live session between machines. This is the highest-risk feature and needs transactional semantics.

### Model

A session's transferable state:

- Transcript JSONL from `session_tree.SessionTranscript`.
- Session metadata and checkpoints from `session.py`.
- Activity journal.
- Permission mode, session allows, and rule set.
- CWD and project identity.
- Pending state: in-flight tool calls, unanswered permission asks, running jobs.

Not transferable: OS process state, open file handles, running subprocesses, worktrees, sandbox state, MCP connections.

### Protocol

```text
session.teleport.offer    → source announces intent, returns a manifest
session.teleport.accept   → target validates compatibility, reserves an id
session.teleport.transfer → chunked, hashed content transfer
session.teleport.commit   → two-phase: target confirms, source seals
session.teleport.abort    → either side, any time before commit
```

Rules:

- **Quiesce first.** The source refuses to offer while a tool call is in flight; it drains or cancels, then offers. Transferring mid-tool-call would duplicate side effects.
- **Two-phase commit.** The source seals (marks the session read-only and records a teleport-out marker) only after the target confirms a complete, hash-verified receipt. Abort at any prior point leaves the source authoritative.
- **No split brain.** The sealed marker is written into the source transcript. A source that reopens a sealed session opens it read-only with a pointer to where it went.
- **Compatibility check.** Version, provider availability, and CWD existence are validated at `accept`. A target that cannot resolve the project path refuses rather than opening a broken session.
- **Pending state is surfaced, not silently dropped.** Unanswered permission asks and running jobs are listed in the manifest; the user is told what will not survive.
- **Content integrity.** Per-chunk and whole-transfer hashes; size limits; resumable by chunk index.

Teleport ships last and behind an explicit flag. It is the feature most likely to lose user data if rushed.

## 10. Configuration

```json
{
  "remote": {
    "daemon": {"enabled": true, "socket": null, "idleShutdownMinutes": 60},
    "tcp": {
      "enabled": false,
      "host": "127.0.0.1",
      "port": 0,
      "tls": {"cert": null, "key": null, "autoGenerate": true}
    },
    "pairing": {"enabled": true, "codeTtlSeconds": 120, "maxAttempts": 5},
    "control": {
      "leaseTtlSeconds": 30,
      "allowForce": true,
      "confirmOnHost": ["session.mode", "plan.approve", "control.acquire.force"],
      "denyFromRemote": ["dangerouslyDisableSandbox"],
      "maxModeFromRemote": "acceptEdits"
    },
    "stream": {
      "maxClients": 16,
      "maxSubscriptionsPerClient": 8,
      "queueSize": 1024,
      "replayWindowSeconds": 3600,
      "maxMessageBytes": 1048576
    },
    "notifications": {"enabled": true, "metadataOnly": true, "webhook": null},
    "teleport": {"enabled": false, "maxBytes": 268435456}
  }
}
```

Environment:

- `MANTIS_DAEMON=0|1`
- `MANTIS_DAEMON_SOCKET`
- `MANTIS_REMOTE_TCP=0|1`
- `MANTIS_REMOTE_NO_CONTROL=1`

`MANTIS_REMOTE_NO_CONTROL=1` must disable all control operations regardless of any other setting — a one-flag way to make every remote surface read-only.

## 11. CLI and TUI surface

```text
mantis daemon start|stop|status
mantis remote pair                    show pairing code + QR
mantis remote devices                 list paired devices
mantis remote revoke <name>
mantis remote url                     print the TLS URL for a paired client
mantis serve                          existing dashboard, now a protocol client
mantis attach <session-id>            attach a terminal to a live remote session
mantis teleport out <session-id> --to <device>
mantis teleport in
```

In the TUI:

```text
/remote            daemon status, connected clients, controller, capabilities
/remote pair
/remote devices
/remote yield      hand the controller lease to a waiting client
/remote lock       refuse remote control for this session
```

The status line shows a compact indicator when a session is remotely observed or controlled. A user must always be able to see that another surface is watching. Silent observation is not acceptable.

## 12. Errors

```text
ProtocolError                  (base)
├── VersionMismatchError
├── UnknownOperationError
├── CapabilityNotNegotiatedError
├── NotAuthenticatedError
├── NotAuthorizedError
├── PairingCodeInvalidError
├── PairingRateLimitedError
├── CertificatePinMismatchError
├── LeaseHeldError
├── LeaseExpiredError
├── SessionNotFoundError
├── SessionNotLiveError
├── ReplayTruncatedError        # carries earliest available seq
├── SlowConsumerError
├── MessageTooLargeError
├── TeleportIncompatibleError
├── TeleportInFlightError       # tool call active
├── TeleportIntegrityError
└── TeleportSealedError
```

Every error carries a stable `code`, a human `msg`, and, where recovery is possible, structured data (earliest `seq`, current lease holder, supported protocol range).

## 13. Delivery phases

### Phase 0 — Protocol design

1. Write the protocol specification as a versioned document before any implementation.
2. Confirm the activity envelope serves as the event payload without additions.
3. Prototype the Unix socket transport and peer-credential checks on macOS and Linux.
4. Measure fan-out cost for 16 clients on a chatty session.
5. Decide the replay window and its interaction with journal retention.

**Exit:** specification reviewed; envelope reuse confirmed; peer-credential check works on both platforms.

### Phase 1 — Daemon and local transport

1. Add `mantis_agent/protocol/` with envelope, ops, and errors.
2. Implement the asyncio daemon with the Unix socket transport.
3. Implement session registration from in-process sessions.
4. Implement `session.list`, `session.get`, `session.subscribe`, `ping`, `hello`.
5. Add `mantis daemon start|stop|status` and idle shutdown.

**Exit:** a second terminal can watch a live session's activity stream locally.

### Phase 2 — Replay and backpressure

1. Implement `from_seq` replay from the activity journal.
2. Implement the gapless live switchover with `seq` de-duplication.
3. Implement bounded queues, coalescing, and `slow_consumer` disconnection.
4. Implement `replay_truncated` with a state snapshot.
5. Stress test reconnection under load.

**Exit:** a 60-second disconnect replays with no gaps or duplicates.

### Phase 3 — Control and leases

1. Implement `control.acquire` / `release` / `heartbeat` with TTL.
2. Implement `session.prompt`, `session.interrupt`, `node.action`.
3. Implement `permission.respond` by wiring a remote `AskerFn`.
4. Implement `plan.approve` and `session.mode` with narrowing-only enforcement.
5. Add `/remote` commands and the observation indicator.

**Exit:** a permission prompt is answerable from a second local client; exactly one lease holder under stress.

### Phase 4 — Secure remote transport

1. Implement TLS with auto-generated certificates and fingerprint pinning.
2. Implement pairing with codes, QR, rate limiting, and challenge-response.
3. Implement device credentials, keychain storage, listing, and revocation.
4. Implement per-device capability grants and remote restrictions.
5. Remove URL tokens from every path; migrate `serve.py`'s `--lan` to the new mechanism with a deprecation warning.

**Exit:** a phone on the LAN can watch and approve over TLS with no token in any URL.

### Phase 5 — Web client and dashboard integration

1. Convert `serve.py` into a protocol client.
2. Add live streaming to `serve_ui.py` without rewriting its markup.
3. Add a responsive session view with prompt entry and permission response.
4. Add notifications with metadata-only payloads.
5. Add `mantis attach` for terminal-to-remote-session attachment.

**Exit:** the existing dashboard shows live data and can control a session.

### Phase 6 — Teleport

1. Implement the manifest and compatibility validation.
2. Implement quiesce, chunked hashed transfer, and two-phase commit.
3. Implement sealing and read-only reopening of a teleported-out session.
4. Implement abort and resume paths.
5. Test abrupt termination at every stage of the transfer.

**Exit:** a session moves between machines without duplication or loss; every failure leaves exactly one authoritative copy.

### Phase 7 — Hardening

1. Adversarial review: pairing, pinning, lease races, replay forgery, backpressure exhaustion.
2. Fuzz the framing and every operation payload.
3. Leak tests for sockets, threads, TLS contexts, and client queues.
4. Soak with 16 clients across multiple sessions for 24 hours.
5. Publish the protocol specification and a reference client.

## 14. Testing strategy

### Unit

- Envelope encode/decode for every message type across the Python matrix.
- Version negotiation: lower, higher, missing, malformed.
- Capability negotiation and refusal of un-negotiated operations.
- Lease state machine: acquire, expire, release, force, concurrent claims.
- Replay: gapless switchover, de-duplication, truncation, out-of-range `seq`.
- Backpressure: queue fill, coalescing correctness, disconnect threshold.
- Peer credential check, including a different-UID connection.
- Pairing: code expiry, single use, rate limit, wrong code, replay.
- Certificate pinning: match, mismatch, rotation.
- Authorization: every remote restriction, especially mode narrowing and sandbox-escape refusal.
- Teleport manifest validation and integrity hashing.

### Integration

- Two local clients on one live session; one controls, one observes.
- Permission ask answered remotely; the terminal reflects it.
- Reconnect mid-run and verify complete replay.
- Daemon restart while a session runs; session re-registers and continues.
- `serve.py` as a protocol client rendering live data.
- Headless run streamed to a subscriber.

### End-to-end

- Phone-equivalent client over TLS on a LAN: pair, watch, approve, prompt.
- `mantis attach` to a session started in another terminal.
- Full teleport between two daemons on one machine, then across machines.
- Notification delivery for a permission ask.

### Security

- Token never present in any URL, log, referrer, or process argument list — asserted by scanning all sinks.
- Unpaired client refused at TLS and at handshake.
- Pinned-fingerprint mismatch refuses connection.
- Remote client cannot set `bypass` or approve a sandbox escape.
- Cross-UID Unix socket connection refused.
- DNS rebinding against the TCP transport.
- Replay forgery: a client-supplied `seq` cannot inject events.
- Lease race: 20 concurrent `control.acquire` calls yield exactly one holder.
- Teleport interception: a modified chunk fails the integrity check and aborts.
- Malformed frames of every shape do not crash the daemon.

### Performance and reliability

- Fan-out latency at 16 clients.
- Idle daemon RSS and CPU.
- Replay of a 100 MB journal.
- Reconnect storm: 16 clients reconnect simultaneously.
- 24-hour soak with no socket, thread, or memory growth.

## 15. Documentation

- `docs/guides/remote.md` — daemon, pairing, devices, controlling a session remotely.
- `docs/guides/remote-security.md` — threat model, pairing, pinning, lease semantics, what remote clients can never do, migration from `--lan` URL tokens.
- `docs/guides/teleport.md` — model, what transfers, what does not, failure handling.
- `docs/api/protocol.md` — the full versioned specification: envelope, operations, errors, capabilities, replay semantics.
- A reference client in under 200 lines demonstrating subscribe and control.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 16. File-level implementation map

New:

- `mantis_agent/protocol/__init__.py`
- `mantis_agent/protocol/envelope.py`
- `mantis_agent/protocol/ops.py`
- `mantis_agent/protocol/errors.py`
- `mantis_agent/protocol/version.py`
- `mantis_agent/daemon/__init__.py`
- `mantis_agent/daemon/server.py`
- `mantis_agent/daemon/registry.py` — live session registry
- `mantis_agent/daemon/fanout.py` — subscriptions, queues, backpressure
- `mantis_agent/daemon/replay.py`
- `mantis_agent/daemon/leases.py`
- `mantis_agent/daemon/auth.py` — peer credentials, pairing, devices
- `mantis_agent/daemon/tls.py`
- `mantis_agent/daemon/notify.py`
- `mantis_agent/teleport/__init__.py`
- `mantis_agent/teleport/manifest.py`
- `mantis_agent/teleport/transfer.py`
- `mantis_agent/remote_client.py` — reference client used by `serve.py` and `mantis attach`
- `tests/test_protocol_envelope.py`
- `tests/test_protocol_negotiation.py`
- `tests/test_daemon_transport.py`
- `tests/test_daemon_replay.py`
- `tests/test_daemon_backpressure.py`
- `tests/test_daemon_leases.py`
- `tests/test_daemon_auth.py`
- `tests/test_remote_authorization.py`
- `tests/test_teleport.py`
- `tests/test_remote_security.py`
- `docs/guides/remote.md`
- `docs/guides/remote-security.md`
- `docs/guides/teleport.md`
- `docs/api/protocol.md`

Modified:

- `mantis_agent/serve.py` — becomes a protocol client; `--lan` token deprecated
- `mantis_agent/serve_ui.py` — live streaming, no `__TOKEN__` in URLs
- `mantis_agent/activity/` — journal exposes a replay reader for the daemon
- `mantis_agent/permissions.py` — remote `AskerFn` integration
- `mantis_agent/session.py`, `session_tree.py` — teleport hooks, sealed marker
- `mantis_agent/headless.py` — protocol client output path
- `mantis_agent/cli.py` — `daemon`, `remote`, `attach`, `teleport` commands
- `mantis_agent/tui_fullscreen.py` — `/remote` commands, observation indicator
- `mantis_agent/paths.py` — socket and run directory
- `tests/public_api_surface.txt` — intentional update

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Daemon becomes a second execution engine | Strict scope: registry, fan-out, replay, leases, auth. No agent loops |
| Daemon outage stops work | Sessions run independently and re-register; daemon is on the watching path, not the working path |
| Two surfaces answer one prompt | Exclusive lease with TTL, force requiring host confirmation, stress-tested |
| Token theft over LAN | URL tokens removed; TLS + pinning + paired device credentials |
| Stolen device controls the session | Per-device capabilities, narrowing-only mode, sandbox escape refused, revocation |
| Replay gaps or duplicates | `seq`-based de-duplication, buffered switchover, explicit truncation error |
| Slow client exhausts memory | Bounded queues, coalescing, disconnect |
| Protocol churn breaks clients | Explicit version and capability negotiation; no silent downgrade |
| Teleport duplicates or loses a session | Quiesce, two-phase commit, sealing, integrity hashes, abort paths |
| `serve.py` regression during conversion | Dashboard keeps its threaded server; conversion is incremental with parity tests |
| Windows lacks Unix sockets | Loopback TCP with a `0o600` token file, documented as the platform path |
| Notification payloads leak content | Metadata-only by default; content fetched over the authenticated channel |

## 18. Acceptance checklist

- [ ] Protocol is versioned, specified, and negotiated; mismatch fails clearly.
- [ ] Event payloads are the activity envelope, not a second schema.
- [ ] Local transport uses peer credentials; no local token.
- [ ] No credential appears in any URL, log, referrer, or argument list.
- [ ] Remote transport is TLS-only with pinned fingerprints and paired devices.
- [ ] Devices are listable and revocable; pairing is rate-limited and single-use.
- [ ] Exactly one controller lease per session, with TTL and audited transitions.
- [ ] Remote clients can only narrow the permission mode and can never approve a sandbox escape.
- [ ] `MANTIS_REMOTE_NO_CONTROL=1` makes every remote surface read-only.
- [ ] Replay is gapless and duplicate-free; truncation is explicit.
- [ ] Backpressure bounds memory and disconnects slow consumers.
- [ ] A permission prompt is answerable from a second surface.
- [ ] The user can always see that a session is remotely observed.
- [ ] `serve.py` works as before and now streams live.
- [ ] Teleport is transactional; every failure leaves exactly one authoritative copy.
- [ ] Docs include the protocol specification and a reference client.
- [ ] `ruff check` and the full pytest suite pass.

## 19. Recommended implementation order

1. **Write the specification first.** This is the one plan where the artifact that matters most is a document. Clients will be written against it by people who cannot read the server.
2. Build the daemon with the local Unix socket and read-only subscription. Prove the activity envelope streams unchanged.
3. Add replay and backpressure before any control operation. A stream that loses events is not a foundation for control.
4. Add leases before control operations, not alongside them. Control without exclusivity is a bug that will be discovered in production.
5. Add local control and wire the remote `AskerFn`. At this point two local terminals can share a session, which is already useful and fully testable without any network security work.
6. Add TLS, pairing, and device credentials as one release. Never ship remote reachability before the credential model.
7. Convert `serve.py` to a client and add live streaming — the visible payoff, built on a foundation that is already secure.
8. Add notifications.
9. Add teleport last, behind a flag, with the most testing per line of any component here.
10. Publish the specification and reference client, then let `r_ide_integrations.md` and `s_channels_and_reactive_operation.md` build on it rather than beside it.
