"""``setting_sources`` actually loads + persists per source.

The Claude Agent SDK exposes ``MantisAgentOptions(setting_sources=[...])``
to declare which on-disk settings layers should contribute to the run.
Previously: mantis-agent-sdk accepted the field but did nothing with it.
Now: each named source resolves to a real path, the layers merge in
declared order (later overrides earlier), and the merged result
populates option defaults the user didn't pass explicitly.

These tests cover:
  * path resolution per source (``user`` / ``project`` / ``local``)
  * load / save / update on one source
  * merge precedence across multiple sources
  * apply_settings_to_options precedence (explicit user opts always win)
  * end-to-end wire-through via ``MantisAgentOptions.to_query_options``
    + ``compat_query._build_agent`` (so the agent actually reads the
    on-disk settings).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mantis_agent import (
    MantisAgentOptions,
    KNOWN_SETTING_KEYS,
    SETTING_SOURCES,
    apply_settings_to_options,
    load_setting_source,
    load_settings,
    merge_settings,
    resolve_setting_path,
    save_setting_source,
    update_setting_source,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_mantis_agent_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``$MANTIS_AGENT_HOME`` at a tmpdir so ``user`` source isolates."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    return home


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Tmpdir to use as the ``cwd`` for ``project`` + ``local`` sources."""

    project = tmp_path / "proj"
    project.mkdir()
    return project


# ---------------------------------------------------------------------------
# resolve_setting_path
# ---------------------------------------------------------------------------


def test_resolve_user_path_uses_mantis_agent_home(tmp_mantis_agent_home: Path) -> None:
    p = resolve_setting_path("user")
    assert p == tmp_mantis_agent_home / "settings.json"


def test_resolve_project_path_uses_cwd(tmp_project: Path) -> None:
    p = resolve_setting_path("project", cwd=tmp_project)
    assert p == tmp_project / ".mantis-agent" / "settings.json"


def test_resolve_local_path_uses_cwd(tmp_project: Path) -> None:
    p = resolve_setting_path("local", cwd=tmp_project)
    assert p == tmp_project / ".mantis-agent" / "settings.local.json"


def test_resolve_unknown_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown setting_source"):
        resolve_setting_path("not-a-real-source")


def test_setting_sources_canonical_tuple() -> None:
    # The canonical tuple is the source of truth for what's a valid name.
    assert SETTING_SOURCES == ("user", "project", "local")
    # Every name there must round-trip through resolve_setting_path
    # without raising (with a dummy cwd for the two project-scoped ones).
    for name in SETTING_SOURCES:
        resolve_setting_path(name, cwd="/tmp")


# ---------------------------------------------------------------------------
# load_setting_source / save_setting_source
# ---------------------------------------------------------------------------


def test_load_missing_returns_empty(
    tmp_mantis_agent_home: Path, tmp_project: Path
) -> None:
    assert load_setting_source("user") == {}
    assert load_setting_source("project", cwd=tmp_project) == {}
    assert load_setting_source("local", cwd=tmp_project) == {}


def test_save_then_load_roundtrip(tmp_mantis_agent_home: Path) -> None:
    data = {
        "model": "qwen2.5-7b-instruct",
        "max_turns": 5,
        "permissions": {"allow": ["Bash(npm install)"]},
    }
    path = save_setting_source("user", data)
    assert path.exists()
    # File content is pretty-printed JSON with a trailing newline so
    # the file is committable / diff-friendly.
    text = path.read_text()
    assert text.endswith("\n")
    assert "  " in text  # indented
    # Round-trips through load.
    loaded = load_setting_source("user")
    assert loaded == data


def test_save_creates_parent_dir(tmp_project: Path) -> None:
    # tmp_project/.mantis-agent does NOT exist yet; save_setting_source
    # must create it on demand.
    path = save_setting_source("project", {"model": "foo"}, cwd=tmp_project)
    assert path.parent.exists()
    assert path.parent.name == ".mantis-agent"


def test_load_rejects_broken_json(tmp_mantis_agent_home: Path) -> None:
    path = resolve_setting_path("user")
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_setting_source("user")


def test_load_rejects_non_object_json(tmp_mantis_agent_home: Path) -> None:
    path = resolve_setting_path("user")
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_setting_source("user")


def test_load_empty_file_returns_empty_dict(tmp_mantis_agent_home: Path) -> None:
    path = resolve_setting_path("user")
    path.write_text("", encoding="utf-8")
    assert load_setting_source("user") == {}


# ---------------------------------------------------------------------------
# update_setting_source — deep-merge a patch
# ---------------------------------------------------------------------------


def test_update_creates_when_missing(tmp_mantis_agent_home: Path) -> None:
    update_setting_source("user", {"model": "foo"})
    assert load_setting_source("user") == {"model": "foo"}


def test_update_deep_merges_nested(tmp_mantis_agent_home: Path) -> None:
    save_setting_source("user", {"permissions": {"allow": ["Bash"]}})
    update_setting_source("user", {"permissions": {"deny": ["WebFetch"]}})
    assert load_setting_source("user") == {
        "permissions": {"allow": ["Bash"], "deny": ["WebFetch"]}
    }


def test_update_unions_lists(tmp_mantis_agent_home: Path) -> None:
    save_setting_source("user", {"allowed_tools": ["Read"]})
    update_setting_source("user", {"allowed_tools": ["Bash", "Read"]})
    # Read was already there — should not appear twice.
    loaded = load_setting_source("user")
    assert loaded["allowed_tools"] == ["Read", "Bash"]


def test_update_replaces_scalars(tmp_mantis_agent_home: Path) -> None:
    save_setting_source("user", {"model": "foo", "max_turns": 5})
    update_setting_source("user", {"model": "bar"})
    # max_turns preserved; model replaced.
    loaded = load_setting_source("user")
    assert loaded == {"model": "bar", "max_turns": 5}


# ---------------------------------------------------------------------------
# merge_settings
# ---------------------------------------------------------------------------


def test_merge_returns_empty_for_no_layers() -> None:
    assert merge_settings() == {}


def test_merge_later_wins_for_scalars() -> None:
    out = merge_settings({"model": "a"}, {"model": "b"})
    assert out == {"model": "b"}


def test_merge_unions_lists_preserving_order() -> None:
    out = merge_settings({"x": [1, 2]}, {"x": [2, 3]})
    assert out == {"x": [1, 2, 3]}


def test_merge_concats_lists_of_dicts() -> None:
    # dicts are unhashable — fall back to plain concat
    out = merge_settings({"x": [{"a": 1}]}, {"x": [{"b": 2}]})
    assert out == {"x": [{"a": 1}, {"b": 2}]}


def test_merge_deeply_recurses_into_dicts() -> None:
    out = merge_settings(
        {"perm": {"allow": ["a"]}},
        {"perm": {"allow": ["b"], "deny": ["c"]}},
    )
    assert out == {"perm": {"allow": ["a", "b"], "deny": ["c"]}}


def test_merge_pure_function_does_not_mutate_inputs() -> None:
    a = {"x": [1, 2]}
    b = {"x": [3]}
    merge_settings(a, b)
    assert a == {"x": [1, 2]}
    assert b == {"x": [3]}


# ---------------------------------------------------------------------------
# load_settings — multi-source
# ---------------------------------------------------------------------------


def test_load_settings_in_order(
    tmp_mantis_agent_home: Path, tmp_project: Path
) -> None:
    save_setting_source("user", {"model": "user-model", "max_turns": 10})
    save_setting_source(
        "project",
        {"model": "project-model", "max_tokens": 1024},
        cwd=tmp_project,
    )
    save_setting_source("local", {"max_turns": 99}, cwd=tmp_project)

    out = load_settings(["user", "project", "local"], cwd=tmp_project)
    # local wins on max_turns; project wins on model; max_tokens from project.
    assert out == {"model": "project-model", "max_tokens": 1024, "max_turns": 99}


def test_load_settings_handles_missing_sources(
    tmp_mantis_agent_home: Path, tmp_project: Path
) -> None:
    # Only the user source exists; the other two are absent — load_settings
    # must not error.
    save_setting_source("user", {"model": "only-this"})
    out = load_settings(["user", "project", "local"], cwd=tmp_project)
    assert out == {"model": "only-this"}


def test_load_settings_empty_list_returns_empty() -> None:
    assert load_settings([]) == {}


def test_load_settings_subset_of_sources(
    tmp_mantis_agent_home: Path, tmp_project: Path
) -> None:
    # Caller picks ``["user", "local"]`` only — project is skipped even if
    # it exists.
    save_setting_source("user", {"model": "u"})
    save_setting_source("project", {"model": "p"}, cwd=tmp_project)
    save_setting_source("local", {"model": "l"}, cwd=tmp_project)
    assert load_settings(["user", "local"], cwd=tmp_project) == {"model": "l"}


# ---------------------------------------------------------------------------
# apply_settings_to_options — explicit options always win
# ---------------------------------------------------------------------------


def test_apply_fills_missing_keys() -> None:
    opts = {}
    loaded = {"model": "qwen", "max_turns": 7, "temperature": 0.3}
    out = apply_settings_to_options(opts, loaded)
    assert out["model"] == "qwen"
    assert out["max_turns"] == 7
    assert out["temperature"] == 0.3


def test_apply_keeps_explicit_user_opts() -> None:
    opts = {"model": "user-model", "max_turns": 100}
    loaded = {"model": "settings-model", "max_turns": 5, "max_tokens": 2048}
    out = apply_settings_to_options(opts, loaded)
    # User-set fields untouched.
    assert out["model"] == "user-model"
    assert out["max_turns"] == 100
    # Field not set by user — settings populate it.
    assert out["max_tokens"] == 2048


def test_apply_translates_system_prompt_to_system_key() -> None:
    out = apply_settings_to_options({}, {"system_prompt": "Be terse."})
    # Internal options dict uses "system" (not "system_prompt").
    assert out["system"] == "Be terse."


def test_apply_max_budget_usd_maps_to_max_usd() -> None:
    out = apply_settings_to_options({}, {"max_budget_usd": 1.5})
    assert out["max_usd"] == 1.5


def test_apply_permissions_flattens_into_extra() -> None:
    out = apply_settings_to_options(
        {},
        {"permissions": {"allow": ["Bash"], "deny": ["WebFetch"]}},
    )
    assert out["extra"]["allowed_tools"] == ["Bash"]
    assert out["extra"]["disallowed_tools"] == ["WebFetch"]


def test_apply_allowed_tools_unions_with_existing_extra() -> None:
    opts = {"extra": {"allowed_tools": ["Read"]}}
    loaded = {"allowed_tools": ["Bash", "Read"]}
    out = apply_settings_to_options(opts, loaded)
    # Settings come first, existing extra concatenates without dup.
    assert set(out["extra"]["allowed_tools"]) == {"Bash", "Read"}


def test_apply_env_merges_under_existing() -> None:
    opts = {"extra": {"env": {"FOO": "user_foo"}}}
    loaded = {"env": {"FOO": "settings_foo", "BAR": "settings_bar"}}
    out = apply_settings_to_options(opts, loaded)
    # User env wins on FOO; new key BAR comes from settings.
    assert out["extra"]["env"] == {"FOO": "user_foo", "BAR": "settings_bar"}


def test_apply_mcp_servers_only_when_user_unset() -> None:
    loaded = {"mcp_servers": {"calc": {"command": "python"}}}
    # User passed nothing → settings populate.
    out = apply_settings_to_options({}, loaded)
    assert out["mcp_servers"] == {"calc": {"command": "python"}}
    # User passed their own list → settings ignored.
    out2 = apply_settings_to_options({"mcp_servers": [("custom", {})]}, loaded)
    assert out2["mcp_servers"] == [("custom", {})]


def test_apply_unknown_keys_are_ignored() -> None:
    out = apply_settings_to_options({}, {"unknown_key": 42})
    assert "unknown_key" not in out
    assert out.get("extra", {}).get("unknown_key") is None


def test_known_setting_keys_lists_documented_keys() -> None:
    # Smoke test: the public set lists the keys the README documents.
    for k in (
        "model",
        "backend",
        "system_prompt",
        "max_turns",
        "max_tokens",
        "temperature",
        "permissions",
        "permission_mode",
        "allowed_tools",
        "disallowed_tools",
        "env",
        "mcp_servers",
    ):
        assert k in KNOWN_SETTING_KEYS


# ---------------------------------------------------------------------------
# End-to-end: MantisAgentOptions(setting_sources=[...]) wires through
# ---------------------------------------------------------------------------


def test_options_emit_setting_sources_in_query_opts(tmp_mantis_agent_home: Path) -> None:
    opts = MantisAgentOptions(setting_sources=["user", "project"]).to_query_options()
    assert opts["setting_sources"] == ["user", "project"]


def test_options_omit_setting_sources_when_unset() -> None:
    opts = MantisAgentOptions().to_query_options()
    assert "setting_sources" not in opts


def test_build_agent_applies_settings_to_model(
    tmp_mantis_agent_home: Path,
) -> None:
    """End-to-end: settings.json on disk affects what model the agent runs.

    No prompt is sent; we only build the Agent and inspect ``.model`` to
    avoid a network call. That's enough to prove the wire-through is real.
    """

    save_setting_source("user", {"model": "qwen2.5-7b-instruct"})

    from mantis_agent.compat_query import _build_agent, _normalize_options

    options = MantisAgentOptions(setting_sources=["user"])
    opts = _normalize_options(options)
    agent = _build_agent(opts)
    assert agent.model == "qwen2.5-7b-instruct"


def test_build_agent_explicit_model_overrides_settings(
    tmp_mantis_agent_home: Path,
) -> None:
    """If the user passes model= explicitly, settings.json is ignored."""

    save_setting_source("user", {"model": "from-settings"})

    from mantis_agent.compat_query import _build_agent, _normalize_options

    options = MantisAgentOptions(
        model="from-explicit-arg",
        setting_sources=["user"],
    )
    opts = _normalize_options(options)
    agent = _build_agent(opts)
    assert agent.model == "from-explicit-arg"


def test_build_agent_layers_three_sources(
    tmp_mantis_agent_home: Path, tmp_project: Path
) -> None:
    """Three sources merge: user < project < local, later wins."""

    save_setting_source("user", {"model": "from-user", "max_turns": 3})
    save_setting_source("project", {"model": "from-project"}, cwd=tmp_project)
    save_setting_source("local", {"max_tokens": 4096}, cwd=tmp_project)

    from mantis_agent.compat_query import _build_agent, _normalize_options

    options = MantisAgentOptions(
        cwd=str(tmp_project),
        setting_sources=["user", "project", "local"],
    )
    opts = _normalize_options(options)
    agent = _build_agent(opts)
    # model: project wins over user.
    assert agent.model == "from-project"
    # max_turns: from user (only source with it).
    assert agent.max_steps == 3
    # max_tokens: from local.
    assert agent.max_tokens == 4096


def test_build_agent_no_sources_unchanged(tmp_mantis_agent_home: Path) -> None:
    """``setting_sources=None`` is the same as not passing the field."""

    save_setting_source("user", {"model": "ignored"})

    from mantis_agent.compat_query import _build_agent, _normalize_options

    # No setting_sources — file on disk should be ignored.
    options = MantisAgentOptions()
    opts = _normalize_options(options)
    agent = _build_agent(opts)
    # Default fallback model from env (or hardcoded) — not from settings.
    assert agent.model != "ignored"


def test_persistence_records_back_to_source(
    tmp_mantis_agent_home: Path,
) -> None:
    """Saving a delta back to a source persists and survives a reload.

    This is the "persisting per source" side of the roadmap item — the
    agent / a user can update one source independently of the others.
    """

    save_setting_source("user", {"model": "old", "max_turns": 5})

    # Patch one key.
    update_setting_source("user", {"max_turns": 99})

    # Survives reload.
    loaded = load_setting_source("user")
    assert loaded == {"model": "old", "max_turns": 99}

    # And a separate source ("project") doesn't inherit it.
    with pytest.MonkeyPatch.context() as m:
        m.chdir(tmp_mantis_agent_home)  # any tmpdir cwd
        # No project file written → empty.
        assert load_setting_source("project", cwd=tmp_mantis_agent_home) == {}
