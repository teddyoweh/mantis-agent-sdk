# Plugin Packages and Marketplaces — Extensive Implementation Plan

**Status:** Proposed
**Target:** A new `mantis_agent/plugins/` package over the existing extension points
**Objective:** Package skills, commands, agent personas, hooks, MCP servers, workflows, rules, and assets into versioned, verifiable install units with scopes, dependencies, lockfiles, capability disclosure, policy control, and atomic updates — then distribute them through simple, auditable marketplace indexes.

## 1. Executive summary

Mantis already has every extension point a plugin system would need. What it lacks is a unit.

| Extension point | Location | Discovery |
|---|---|---|
| Skills | `.mantis/skills/*.md`, `~/.mantis/skills/` | `discover_skills` |
| Agent personas | `.mantis/agents/*.md`, `get_agents_dir()` | `discover_agent_types` |
| Workflows | `.mantis/workflows/*.md` | `discover_workflow_definitions` |
| Rules | `.mantis/rules/*.md` | `discover_conditional_rules` |
| MCP servers | `.mcp.json`, user config | `load_mcp_server_configs` |
| Hooks | SDK only today; settings after `g_typed_hooks_and_full_lifecycle.md` | `Hooks` registry |
| Project instructions | `MANTIS.md`, `AGENTS.md` | `load_memory_files` |

Each has its own directory convention, its own parser, its own discovery function, and — where trust exists at all — its own trust mechanism. `mcp/manager.py` implements content-hash trust properly (`project_mcp_is_trusted`, `_file_hash`, `filter_untrusted_project_servers`). `project_memory.py` implements tiered import trust (`_TRUSTED_IMPORT_TIERS`). Skills and personas implement none, which `k_skills_commands_policy_and_shell_blocks.md` and `e_subagent_trust_limits_and_isolation.md` address.

Sharing an extension today means telling someone which files to copy where. There is no version, no dependency, no update path, no uninstall, no integrity check, and no way to see what an extension is capable of before installing it.

The opportunity is unusually favorable: the mechanisms exist and work. A plugin is a *bundle* of things Mantis already loads, plus a manifest describing them and a trust story for the bundle as a whole. This plan is mostly packaging, installation, and verification — not new runtime capability.

Two design commitments shape everything below.

**A plugin is data plus declarations, not arbitrary code.** Skills are markdown. Personas are markdown. Workflows are markdown or a validated script under the existing AST allowlist. MCP servers are configuration. Hooks are declarations. The only genuinely executable surfaces are hook commands, skill shell blocks, and MCP stdio servers — each of which is already gated by its own plan. Optional Python tool modules are supported but are a separate, higher-friction capability requiring explicit opt-in, because arbitrary Python is a category change from everything else.

**Capability disclosure is the security model.** Signatures prove *who* published, not *what is safe*. The manifest declares which extension points a plugin touches and which executable surfaces it uses; installation shows that list and requires approval. A user who approves a plugin declaring "3 skills, 1 agent, no hooks, no MCP, no shell" has meaningfully bounded what they accepted.

## 2. Goals

### User outcomes

- `mantis plugin install github:someone/mantis-python-pack` and get skills, personas, and workflows.
- See exactly what a plugin can do before it installs.
- Pin versions, reproduce an install from a lockfile, and update or roll back atomically.
- Uninstall completely, leaving nothing behind.
- Share a team's conventions as one installable unit committed to the repository.
- Trust that installing a plugin does not run its code.

### Engineering goals

- Change no discovery function's behavior for non-plugin content. Existing local files keep working identically.
- Add `plugin` as a source tier in the resolution order `k_skills_commands_policy_and_shell_blocks.md` defines.
- Immutable content-addressed store; installs never mutate in place.
- Atomic activation via symlink or manifest swap, so a failed update cannot leave a half-installed state.
- No new required dependency. Fetching uses `mantis_agent/http.py`; archives use stdlib `tarfile`/`zipfile`; hashing uses `hashlib`.
- Python 3.9–3.14.

### Success metrics

- A plugin bundling all seven extension types installs, activates, updates, and uninstalls cleanly.
- Uninstall leaves zero residue, verified by directory comparison.
- Installation never executes plugin-provided code — asserted by a test that installs a plugin containing a would-be-executing payload.
- Integrity failure aborts before activation, always.
- Lockfile installs are byte-reproducible.
- Plugin discovery adds under 20 ms to startup for 20 installed plugins.

## 3. Non-goals

- A hosted registry service. Marketplaces are static JSON indexes over HTTPS or git; anyone can host one.
- Arbitrary code execution as the primary model. Python tools are opt-in and clearly flagged.
- Sandboxing plugin-provided commands beyond what `h_sandbox_egress_credentials_and_escape_controls.md` already gives.
- Paid distribution or licensing enforcement.
- Automatic updates. Updates are explicit; a plugin that silently changes what is in the system prompt is unacceptable.
- Replacing `pip` for the SDK itself.

## 4. Current integration points

- `mantis_agent/skills.py` — `_skill_dirs`, `discover_skills`, `SKILLS_SUBDIR`, `SkillRegistry`.
- `mantis_agent/subagent.py` — `_agent_dirs`, `discover_agent_types`, `AgentType.source`.
- `mantis_agent/workflow_defs.py` — `workflow_dirs`, `discover_workflow_definitions`, `WORKFLOWS_SUBDIR`, `builtin_definitions`.
- `mantis_agent/rules.py` — `discover_conditional_rules`, `RULES_SUBDIR`.
- `mantis_agent/mcp/manager.py` — `mcp_config_layers`, `load_mcp_server_configs`, `parse_server_entry`, and the trust machinery (`_file_hash`, `_mcp_trust_path`, `project_mcp_is_trusted`).
- `mantis_agent/hooks.py` — the `Hooks` registry and, after typed hooks land, settings-declared handlers.
- `mantis_agent/settings.py` — `SETTING_SOURCES`, `load_settings`, `merge_settings`, `_deep_merge`, `_union_list`; plugins contribute a settings layer.
- `mantis_agent/paths.py` — `get_mantis_agent_dir`, `get_agents_dir`, `get_project_dir`.
- `mantis_agent/http.py` — fetching indexes and archives with URL validation.
- `mantis_agent/tools.py` — registering plugin-provided Python tools.
- `mantis_agent/serve.py` — dashboard listing.
- `mantis_agent/cli.py` — the `plugin` command family.

## 5. Package format

### Layout

```text
my-plugin/
  mantis-plugin.json          manifest (required)
  README.md
  LICENSE
  skills/*.md
  agents/*.md
  workflows/*.md
  rules/*.md
  mcp.json                    server declarations
  hooks.json                  hook declarations
  settings.json               contributed defaults (narrow scope)
  tools/*.py                  optional; requires allowPythonTools
  assets/                     static files referenced by content
```

Every directory is optional. A plugin with one skill is one markdown file plus a manifest.

### Manifest

```json
{
  "schemaVersion": 1,
  "name": "python-pack",
  "version": "1.4.2",
  "description": "Python conventions, review personas, and a release workflow",
  "author": {"name": "teddy", "url": "https://github.com/teddyoweh"},
  "license": "MIT",
  "homepage": "https://github.com/teddyoweh/mantis-python-pack",
  "requires": {"mantis": ">=2.62,<3"},
  "dependencies": {"base-conventions": "^1.2"},
  "provides": {
    "skills": ["py-style", "py-testing"],
    "agents": ["py-reviewer"],
    "workflows": ["release-python"],
    "rules": ["py-globs"],
    "mcpServers": ["pyright"],
    "hooks": ["PostToolUse:format"],
    "tools": [],
    "settings": ["formatter.python"]
  },
  "capabilities": {
    "shellBlocks": false,
    "hookCommands": true,
    "mcpStdio": true,
    "pythonTools": false,
    "network": ["pypi.org"],
    "filesystemWrites": ["<project>"]
  },
  "integrity": {"algorithm": "sha256", "files": {"skills/py-style.md": "sha256:…"}}
}
```

`provides` and `capabilities` are the two fields that matter operationally. `provides` drives namespacing, collision detection, and uninstall. `capabilities` drives the approval prompt.

**Declared capabilities are enforced, not advisory.** A plugin declaring `shellBlocks: false` whose skill contains a shell block fails validation at install. A plugin declaring `pythonTools: false` shipping `tools/*.py` fails. The manifest is a contract the installer verifies against the content, which is what makes the approval prompt meaningful.

### Namespacing

Everything a plugin provides is namespaced by plugin name:

- Skills and commands: `python-pack:py-style`, invocable as `/python-pack:release-python`.
- Agent personas: `python-pack:py-reviewer`.
- MCP servers: prefixed so tools become `mcp__python-pack__pyright__…`, reusing `_ns_segment` sanitization.
- Settings: only keys declared in `provides.settings` are accepted; anything else is dropped with a warning.

Unqualified names resolve to local definitions first, so a plugin can never shadow a user's own skill. This is the inverse of the usual package-manager convention and it is deliberate: local content must always win.

## 6. Store and activation

### Content-addressed store

```text
~/.mantis/plugins/
  store/<name>/<version>-<hash8>/     immutable, read-only after write
  active/<name> -> ../store/…         symlink (or manifest on Windows)
  installed.json                      what is installed, at which version, which scope
  lock.json                           resolved dependency graph with hashes
  cache/                              downloaded archives, verified
  trust.json                          approved capability sets by content hash
```

Requirements:

- Store directories are written to a temp location, verified, then renamed into place. Never partially populated.
- Content is read-only (`0o500` directories, `0o400` files) after write, so a plugin cannot rewrite itself at runtime.
- Multiple versions coexist; activation is a pointer swap.
- Rollback is re-pointing `active/<name>` at a prior version — no re-download.
- Uninstall removes the pointer, then garbage-collects unreferenced store entries.
- Directory `0o700`.

### Scopes

| Scope | Store | Committed | Use |
|---|---|---|---|
| `user` | `~/.mantis/plugins/` | No | Personal tooling |
| `project` | `<repo>/.mantis/plugins/` | Yes (lockfile; content optionally vendored) | Team conventions |
| `managed` | System policy directory | Via IT | Organization standards |

Project plugins install from a committed `plugins.json` + `lock.json`, so `mantis plugin sync` reproduces a teammate's environment exactly.

**A project plugin is untrusted on first encounter**, exactly like a project MCP server or skill. Cloning a repository must never activate a plugin. `mantis plugin sync` presents the capability set for approval before anything activates.

## 7. Sources and marketplaces

### Install sources

```text
mantis plugin install ./local-dir
mantis plugin install ./pack.tar.gz
mantis plugin install github:owner/repo[@ref][#subdir]
mantis plugin install https://example.com/pack-1.4.2.tar.gz
mantis plugin install python-pack@1.4.2 --from <marketplace>
```

### Marketplace index

A static JSON document — no service required:

```json
{
  "schemaVersion": 1,
  "name": "community",
  "updated": "2026-08-01T00:00:00Z",
  "plugins": [
    {
      "name": "python-pack",
      "description": "…",
      "versions": [
        {
          "version": "1.4.2",
          "url": "https://…/python-pack-1.4.2.tar.gz",
          "sha256": "…",
          "size": 48213,
          "requires": {"mantis": ">=2.62,<3"},
          "capabilities": {"hookCommands": true, "mcpStdio": true}
        }
      ]
    }
  ]
}
```

- Indexes are added explicitly: `mantis plugin marketplace add <name> <url>`.
- Index URLs must be HTTPS and are validated by the shared URL validator; a marketplace pointing at a private or metadata address is refused.
- Capabilities appear in the index so `mantis plugin search` can show them before download.
- Indexes are cached with an explicit refresh; a stale index is stated, never silently used as current.

## 8. Integrity and verification

Layered, in order:

1. **Transport** — HTTPS with certificate validation, redirect revalidation, size limits.
2. **Archive hash** — expected `sha256` from the index or the lockfile, verified before extraction. A mismatch aborts.
3. **Per-file hashes** — the manifest's `integrity.files` verified after extraction. Any mismatch discards the whole extraction.
4. **Signature (optional)** — detached signature over the manifest hash, verified against a configured public key. `require-signature` policy is available; unsigned plugins are refused where set.
5. **Capability conformance** — declared capabilities checked against actual content. Undeclared executable surfaces abort the install.

### Safe extraction

Archive extraction is a classic vulnerability surface and needs explicit rules:

- Reject absolute paths, `..` components, and symlinks or hardlinks pointing outside the extraction root — checked before writing, not after.
- Reject device files, FIFOs, and sockets.
- Enforce total uncompressed size and file-count limits (zip-bomb defense), and a compression-ratio ceiling.
- Normalize permissions; never honor setuid/setgid bits from the archive.
- Extract to a temp directory, verify fully, then rename into the store.

### Installation executes nothing

There are no install scripts, no `postinstall`, no `setup.py` execution. Installation is: fetch, verify, extract, verify, activate. Plugin content runs only when the user later invokes a skill, triggers a hook, or connects an MCP server — each already gated by its own permission layer. This is the property that makes the capability prompt trustworthy, and it must never be relaxed for convenience.

## 9. Approval

```text
Install python-pack 1.4.2 from community?

  provides
    2 skills          py-style, py-testing
    1 agent           py-reviewer
    1 workflow        release-python
    1 rule            py-globs
    1 MCP server      pyright  (stdio: node ./server.js)
    1 hook            PostToolUse → command: ruff format

  capabilities
    ⚠ runs a hook command on every file edit
    ⚠ starts a local process for the MCP server
    ✓ no shell blocks in skills
    ✓ no Python tool modules
    ✓ network: pypi.org only

  integrity  sha256 verified · unsigned
  author     teddy (github.com/teddyoweh)

  [i]nstall   [v]iew contents   [s]kip hooks and MCP   [c]ancel
```

Requirements:

- Executable capabilities are called out with a warning marker; passive content is not.
- `[v]iew contents` shows every file, since a skill body is a prompt injection vector regardless of signatures.
- `[s]kip hooks and MCP` installs the passive parts only — most plugins are mostly passive, and partial installation is better than an all-or-nothing choice that pushes users to accept more than they want.
- Approval is recorded against the content hash. An update with a *wider* capability set re-prompts, showing the diff; an update within the approved set may proceed under `updatePolicy: "prompt-on-capability-change"`.
- Non-interactive installs require `--yes` plus a `--expect-capabilities` assertion, so CI cannot silently accept widened capabilities.

## 10. Dependencies and lockfile

- Semver ranges in `dependencies`; resolution is a simple highest-compatible walk. Deep transitive graphs are a smell in this domain, so cap depth (default 3) and report anything deeper as a warning.
- Diamond conflicts are reported with both constraints and the requesting plugins; no silent resolution.
- `lock.json` records name, version, resolved URL, `sha256`, and the full dependency graph.
- `mantis plugin sync` installs exactly the lockfile, verifying every hash. Reproducibility is the point.
- `requires.mantis` is checked against the running version; an incompatible plugin refuses to activate with a clear message rather than failing mysteriously at use time.

## 11. Discovery integration

Each discovery function gains plugin directories, in tier order:

```python
def _skill_dirs(cwd=None) -> list[Path]:
    return [
        managed_skills_dir(),
        user_skills_dir(),
        *project_skill_dirs(cwd),          # nearest-first
        *active_plugin_dirs("skills"),     # NEW
        builtin_skills_dir(),
    ]
```

Rules:

- `plugin` sits below `project` in precedence, per `k_skills_commands_policy_and_shell_blocks.md`. Local content always wins.
- Plugin definitions carry `source="plugin:<name>"` for provenance, extending the existing `AgentType.source` convention (`builtin | user | project`).
- Plugin MCP servers merge into `mcp_config_layers` as their own layer, with the plugin's trust decision governing.
- Plugin hooks register through the typed-hook system with plugin attribution in `HookContext.arbitrary`.
- Plugin settings merge as a low-priority layer through `merge_settings`, restricted to declared keys, and **may never widen security policy** — permission allows, sandbox relaxations, and trust settings are rejected outright from a plugin layer.
- Disabling a plugin removes its contributions without uninstalling.

## 12. Python tools

The one genuinely executable extension, and it is opt-in at three levels: `allowPythonTools` in settings, `pythonTools: true` in the manifest, and explicit approval at install.

- Modules import at registration, so importing is execution. This is disclosed prominently; the approval prompt says so in plain language.
- Import happens lazily on first use where possible, not at startup.
- A module that fails to import disables that tool and reports it; it never breaks startup.
- Tools are namespaced `plugin__<name>__<tool>`.
- Tools declare `is_read_only` and `is_shell` honestly; a plugin tool that shells out without declaring `is_shell` bypasses the danger classifier, so validation checks for subprocess usage statically and refuses the plugin if undeclared.
- Plugin tools run under the same permission layer as built-ins; they receive no special authority.

Recommend against Python tools in documentation. An MCP server is the better distribution mechanism for executable capability: it is process-isolated, already permission-gated, and does not run inside the agent's address space.

## 13. Configuration

```json
{
  "plugins": {
    "enabled": true,
    "allowPythonTools": false,
    "requireSignature": false,
    "trustedKeys": [],
    "marketplaces": [{"name": "community", "url": "https://…/index.json"}],
    "updatePolicy": "prompt-on-capability-change",
    "install": {
      "maxArchiveBytes": 52428800,
      "maxUncompressedBytes": 209715200,
      "maxFiles": 5000,
      "maxCompressionRatio": 100,
      "maxDependencyDepth": 3
    },
    "disabled": [],
    "allowProjectPlugins": "prompt",
    "denyCapabilities": []
  }
}
```

`denyCapabilities` lets a managed policy forbid capability classes outright — `["pythonTools", "hookCommands"]` in a locked-down environment. Managed policy wins; a project or user setting cannot re-enable a denied capability.

Environment: `MANTIS_PLUGINS=0|1`, `MANTIS_PLUGINS_DIR`, `MANTIS_PLUGINS_NO_NETWORK=1`.

## 14. Surface

```text
mantis plugin list [--all]
mantis plugin install <source> [--scope user|project] [--version V]
mantis plugin update [<name>] [--dry-run]
mantis plugin remove <name>
mantis plugin enable|disable <name>
mantis plugin info <name>
mantis plugin search <query> [--marketplace M]
mantis plugin marketplace add|remove|list|refresh
mantis plugin sync                        install from lockfile
mantis plugin verify [<name>]             re-check integrity
mantis plugin gc                          drop unreferenced store entries
mantis plugin pack <dir>                  build an archive + manifest hashes
mantis plugin validate <dir>              lint a plugin before publishing
```

```text
$ mantis plugin list
NAME             VERSION  SCOPE    CAPABILITIES        PROVIDES        STATUS
python-pack      1.4.2    user     hook, mcp           2s 1a 1w 1r     active
team-conventions 0.9.0    project  —                   4s 2a           active
old-pack         2.1.0    user     shell               3s              disabled
```

`mantis plugin validate` is what makes authoring viable — it checks manifest conformance, capability declarations against content, hash generation, namespacing collisions, and `requires` ranges before publication.

In the TUI, `/plugins` lists installed plugins with provenance, and every plugin-provided skill, persona, workflow, and tool displays its plugin source wherever it appears.

## 15. Errors

```text
PluginError                        (base)
├── ManifestInvalidError
├── ManifestVersionError           # schemaVersion unsupported
├── CapabilityUndeclaredError      # content exceeds declared capabilities
├── CapabilityDeniedError          # blocked by policy
├── IntegrityError                 # archive or file hash mismatch
├── SignatureError
├── UnsafeArchiveError             # traversal, symlink escape, bomb
├── ArchiveTooLargeError
├── DependencyResolutionError      # with both constraints
├── DependencyDepthError
├── VersionIncompatibleError       # requires.mantis
├── PluginNotFoundError
├── PluginUntrustedError
├── NamespaceCollisionError        # two plugins, same provides
├── PluginDisabledError
├── StoreCorruptError
└── ActivationError                # rolled back
```

Every install failure leaves the previous state intact. Activation is the last step and is atomic; anything failing before it is a no-op.

## 16. Delivery phases

### Phase 0 — Design and prototype

1. Enumerate every discovery function and confirm the plugin-directory injection point in each.
2. Prototype the store with atomic activation on macOS, Linux, and Windows (symlink versus manifest).
3. Prototype safe extraction against a corpus of malicious archives.
4. Validate capability conformance checking against a real multi-type plugin.
5. Decide the Windows activation strategy definitively.

**Exit:** store and activation proven on all platforms; extraction corpus fully rejected.

### Phase 1 — Format and local install

1. Define the manifest schema and validator.
2. Implement the content-addressed store with atomic activation and rollback.
3. Implement local directory and archive installation with safe extraction.
4. Implement per-file integrity verification.
5. Implement `list`, `info`, `remove`, `enable`, `disable`, `verify`, `gc`.

**Exit:** a local plugin installs, activates, and uninstalls with zero residue.

### Phase 2 — Discovery integration

1. Add plugin directories to skills, agents, workflows, and rules discovery.
2. Implement namespacing and `source="plugin:<name>"` provenance.
3. Merge plugin MCP servers as a layer.
4. Merge plugin settings restricted to declared keys, with security-policy rejection.
5. Ensure disable removes contributions immediately.

**Exit:** plugin content is usable and clearly attributed everywhere it appears.

### Phase 3 — Capabilities and approval

1. Implement capability conformance checking.
2. Implement the approval prompt with partial install.
3. Implement trust records keyed by content hash and re-prompt on capability widening.
4. Implement `denyCapabilities` policy with managed precedence.
5. Implement project-plugin untrusted-on-clone behavior.

**Exit:** nothing activates without an informed decision; declared capabilities are enforced.

### Phase 4 — Remote sources and marketplaces

1. Implement HTTPS and git sources with URL validation and size limits.
2. Implement marketplace indexes, caching, refresh, and `search`.
3. Implement archive-hash verification against the index.
4. Implement optional signature verification and `requireSignature`.
5. Implement `pack` and `validate` for authors.

**Exit:** plugins install from a marketplace with verified integrity.

### Phase 5 — Dependencies and reproducibility

1. Implement semver resolution with depth caps and conflict reporting.
2. Implement `lock.json` and `sync`.
3. Implement `requires.mantis` checking.
4. Implement `update` with dry-run and capability diffing.
5. Implement rollback.

**Exit:** a teammate reproduces an environment exactly from a committed lockfile.

### Phase 6 — Python tools and hardening

1. Implement opt-in Python tool loading with lazy import and namespacing.
2. Implement static checks for undeclared subprocess usage.
3. Adversarial review: extraction, traversal, capability bypass, settings escalation, namespace impersonation.
4. Fuzz manifest and index parsing.
5. Remove experimental gating.

## 17. Testing strategy

### Unit

- Manifest validation: every field, unknown `schemaVersion`, malformed values.
- Capability conformance: undeclared shell block, undeclared Python tool, undeclared MCP stdio.
- Safe extraction: absolute paths, `..`, symlink escape, hardlink escape, device files, setuid bits, zip bomb, file-count and ratio limits.
- Integrity: archive mismatch, per-file mismatch, missing file, extra file.
- Store: atomic activation, rollback, coexisting versions, read-only enforcement, GC.
- Namespacing and collision detection between two plugins.
- Settings merge: declared keys only; security keys rejected.
- Semver resolution, diamond conflicts, depth cap, `requires.mantis`.
- Lockfile round-trip and reproducibility.
- Marketplace index parsing, cache staleness, URL validation.

### Integration

- Full multi-type plugin: install, verify contributions in every discovery function, disable, re-enable, remove.
- Plugin skill resolves below a same-named local skill.
- Plugin MCP server connects and its tools are correctly namespaced.
- Plugin hook fires with plugin attribution.
- Partial install (`skip hooks and MCP`) installs only passive content.
- Update with widened capabilities re-prompts and shows the diff.
- `sync` from a lockfile in a fresh environment reproduces exactly.
- Project plugin in a fresh clone is inactive until approved.

### End-to-end

- Install from a local marketplace index over HTTPS.
- Author flow: `pack`, `validate`, publish to a test index, install.
- Rollback after a bad update.
- `gc` reclaims space without breaking active plugins.
- Dashboard and `/plugins` show accurate state.

### Security

- **Install executes nothing:** a plugin containing a module with import-time side effects is installed; the side effect does not occur.
- Every malicious-archive case is rejected before any write outside the temp root.
- A plugin declaring `shellBlocks: false` with a shell block fails install.
- A plugin settings layer attempting to add a permission allow, relax the sandbox, or set a trust key is rejected.
- A plugin cannot shadow a local skill, persona, or workflow.
- A plugin cannot impersonate another plugin's namespace (`_ns_segment`-style sanitization on names).
- Marketplace URL pointing at a private or metadata address is refused.
- Signature mismatch under `requireSignature` refuses installation.
- Managed `denyCapabilities` cannot be overridden by user or project settings.
- Store files are read-only; a plugin cannot rewrite its own content at runtime.

### Performance

- Startup overhead with 20 installed plugins.
- Discovery cost across all extension points with plugins present.
- Install time for a 50 MB archive including verification.
- Marketplace search over a 1,000-entry index.

## 18. Documentation

- `docs/guides/plugins.md` — installing, scopes, marketplaces, updating, disabling.
- `docs/guides/plugins-authoring.md` — format, manifest reference, `pack`/`validate`, publishing.
- `docs/guides/plugins-security.md` — capability model, what signatures do and do not prove, why installation runs nothing, why MCP is preferred over Python tools.
- `docs/api/plugins.md` — manifest schema, store layout, public API.
- Update `mkdocs.yml` and `web/lib/docsNav.ts`.

## 19. File-level implementation map

New:

- `mantis_agent/plugins/__init__.py`
- `mantis_agent/plugins/manifest.py`
- `mantis_agent/plugins/store.py`
- `mantis_agent/plugins/install.py`
- `mantis_agent/plugins/archive.py` — safe extraction
- `mantis_agent/plugins/integrity.py`
- `mantis_agent/plugins/capabilities.py`
- `mantis_agent/plugins/marketplace.py`
- `mantis_agent/plugins/resolve.py` — dependencies and lockfile
- `mantis_agent/plugins/discovery.py` — directory injection
- `mantis_agent/plugins/trust.py`
- `mantis_agent/plugins/pack.py` — author tooling
- `tests/test_plugin_manifest.py`
- `tests/test_plugin_archive_security.py`
- `tests/test_plugin_store.py`
- `tests/test_plugin_install.py`
- `tests/test_plugin_capabilities.py`
- `tests/test_plugin_discovery.py`
- `tests/test_plugin_resolve.py`
- `tests/test_plugin_marketplace.py`
- `tests/test_plugin_security.py`
- `tests/fixtures/malicious_archives/**`
- `docs/guides/plugins.md`
- `docs/guides/plugins-authoring.md`
- `docs/guides/plugins-security.md`

Modified:

- `mantis_agent/skills.py` — plugin directories
- `mantis_agent/subagent.py` — plugin persona directories, `source`
- `mantis_agent/workflow_defs.py` — plugin workflow directories
- `mantis_agent/rules.py` — plugin rule directories
- `mantis_agent/mcp/manager.py` — plugin MCP layer
- `mantis_agent/hooks.py` — plugin hook registration and attribution
- `mantis_agent/settings.py` — plugin settings layer with restrictions
- `mantis_agent/tools.py` — plugin tool registration
- `mantis_agent/cli.py` — `plugin` command family
- `mantis_agent/tui_fullscreen.py` — `/plugins`
- `mantis_agent/serve.py` — dashboard listing
- `mantis_agent/paths.py` — plugin directories
- `tests/public_api_surface.txt` — intentional update

## 20. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Installing a plugin runs code | No install scripts; installation is fetch/verify/extract/activate; asserted by test |
| Malicious archive escapes extraction | Pre-write path validation, symlink/hardlink rejection, size/count/ratio caps, temp-then-rename |
| Capability declaration is cosmetic | Conformance checked against content; mismatch aborts install |
| Plugin settings escalate privilege | Declared keys only; security-policy keys rejected from plugin layers |
| Plugin shadows local content | `plugin` tier below `project`; unqualified names prefer local |
| Namespace impersonation between plugins | Name sanitization; collision detection at activation |
| Signature confused with safety | Documentation is explicit; capability disclosure is the actual control |
| Project plugin activates on clone | Untrusted until approved via `sync`, same model as MCP and skills |
| Half-installed state after failure | Immutable store, atomic activation last, rollback by pointer |
| Dependency graphs become unmanageable | Depth cap, conflicts reported not resolved, shallow graphs encouraged |
| Python tools become the norm | Triple opt-in, prominent warnings, MCP recommended instead |
| Startup cost grows with plugins | Lazy loading, measured budget, disable without uninstall |
| Windows lacks symlinks | Manifest-based activation decided in Phase 0, not improvised |

## 21. Acceptance checklist

- [ ] A plugin bundling all seven extension types installs, activates, updates, and uninstalls cleanly.
- [ ] Installation never executes plugin code, asserted by test.
- [ ] Every malicious-archive case is rejected before writing outside a temp root.
- [ ] Declared capabilities are verified against content; mismatches abort.
- [ ] The approval prompt shows provides, capabilities, integrity, and author, and supports partial install.
- [ ] Capability widening on update re-prompts with a diff.
- [ ] Project plugins are inactive in a fresh clone until approved.
- [ ] Plugin content never shadows local content and always shows its source.
- [ ] Plugin settings are restricted to declared keys and cannot touch security policy.
- [ ] Managed `denyCapabilities` cannot be overridden.
- [ ] The store is immutable and read-only; activation is atomic with rollback.
- [ ] Uninstall leaves zero residue.
- [ ] Lockfile installs are reproducible with full hash verification.
- [ ] `requires.mantis` incompatibility refuses activation with a clear message.
- [ ] `validate` and `pack` make authoring and publishing tractable.
- [ ] Startup overhead stays within budget at 20 plugins.
- [ ] `ruff check` and the full pytest suite pass.

## 22. Recommended implementation order

1. **Build safe extraction and the malicious-archive corpus first.** It is the highest-severity component, it is completely self-contained, and every later phase depends on it being correct.
2. **Build the immutable store with atomic activation second**, and settle the Windows strategy before writing any install logic on top of it.
3. **Ship local-directory installation only**, with no network, as the first release. It makes team plugins usable via a committed directory and exercises the whole pipeline with no remote-fetch risk.
4. **Add discovery integration fourth**, one extension point at a time, with provenance visible from the start so plugin content is never mistaken for local content.
5. **Add capability declaration and conformance before any remote source exists.** Fetching from the internet without an enforced capability contract is the ordering mistake to avoid.
6. **Add remote sources and marketplaces sixth**, with integrity verification in the same change — never a version where downloads happen unverified.
7. **Add dependencies and the lockfile seventh.** Most plugins will have no dependencies; this is important for teams and unimportant for individuals, so it should not gate earlier value.
8. **Add Python tools last, if at all.** Ship the rest first and see whether MCP servers cover the need; if they do, the highest-risk capability in this plan never has to exist.
9. Sequence this plan *after* `k_skills_commands_policy_and_shell_blocks.md` lands its trust gate. Plugins add a distribution channel for exactly the content that plan makes safe, and shipping distribution before the gate would multiply the exposure.
