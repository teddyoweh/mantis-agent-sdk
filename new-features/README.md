# Claude Code 2.1.220 Feature Plan Index

Implementation plans derived from the Claude Code 2.1.220 bundle and verified against the current Mantis repository. These documents cover genuinely missing or materially partial features; already-shipped foundations should be extended rather than rebuilt.

## Plans

- [Unified Activity Graph and Inline Rail](a_activity_graph_and_inline_rail.md)
- [Durable Jobs and Reattachment](b_durable_jobs_and_reattachment.md)
- [Peer Agent Teams](c_agent_teams.md)
- [Workflow Safety, Strict Resume, and Scale](d_workflow_safety_resume_and_scale.md)
- [Subagent Trust Boundaries, Limits, and Isolation](e_subagent_trust_limits_and_isolation.md)
- [Permission Policy Engine and Classifier Auto Mode](f_permission_policy_engine_and_auto_mode.md)
- [Typed Hooks and Full Lifecycle](g_typed_hooks_and_full_lifecycle.md)
- [Sandbox Egress, Credentials, and Escape Controls](h_sandbox_egress_credentials_and_escape_controls.md)
- [MCP OAuth and Dynamic Lifecycle](i_mcp_oauth_and_dynamic_lifecycle.md)
- [Plugin Packages and Marketplaces](j_plugin_packages_and_marketplaces.md)
- [Skills and Commands Standard, Policy, and Shell Blocks](k_skills_commands_policy_and_shell_blocks.md)
- [Automatic Memory Lifecycle](l_auto_memory_lifecycle.md)
- [Session Event API, Remote Control, Web, Mobile, and Teleport](m_session_event_api_and_remote_surfaces.md)
- [TUI and CLI UX Accessibility](o_tui_cli_ux_accessibility.md)
- [Statusline, Themes, and Output Styles](p_statusline_themes_output_styles.md)
- [Configurable Keybindings and Modal Editing](q_keybindings_and_modal_editing.md)
- [IDE Integrations](r_ide_integrations.md)
- [Channels and Reactive Operation](s_channels_and_reactive_operation.md)
- [Voice and Rich Input](t_voice_and_rich_input.md)
- [GitHub and GitLab CI Review, Triage, and Autofix](u_github_gitlab_ci_review_and_autofix.md)
- [Advisor Capability Ordering and Inheritance](v_advisor_capability_and_inheritance.md)

- [Browser and Computer Use](n_browser_computer_use.md)

## Dependency order

1. Activity graph, permission policy, typed hooks, sandbox, and session event contracts.
2. Durable jobs, subagent isolation, workflow hardening, MCP OAuth, and automatic memory.
3. Teams, plugins, skills/commands, IDEs, channels, and CI integrations.
4. Remote/web/mobile, richer presentation, voice, and advanced browser transports.

Each plan must be re-verified immediately before implementation because the bundle is minified and this branch already contains substantial in-progress work.
