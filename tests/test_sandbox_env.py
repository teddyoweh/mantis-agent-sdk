"""The sandbox has to confine the environment, not just the filesystem.

A ``writable_roots`` policy is a fine story about disks and a useless one about
secrets: a process that inherits ``ANTHROPIC_API_KEY`` can read it out of
``os.environ`` and POST it anywhere the network allows. These tests pin the
allowlist behaviour — the only shape of environment filtering that survives a
variable nobody thought to deny.
"""

from __future__ import annotations

import os

from mantis_agent.redaction import (
    SECRET_NAME_RE,
    build_child_env,
    is_secret_name,
    redact_value,
)
from mantis_agent.sandbox import SandboxPolicy, child_env, load_policy

# Names that must never reach a child, in the casings people actually use.
CREDENTIAL_SHAPED = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "DATABASE_PASSWORD",
    "MYSQL_PASSWD",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AUTHORIZATION",
    "SESSION_COOKIE",
    "MY_PRIVATE_THING",
    "npm_config_registry_apikey",
]


# -- the shared regex --------------------------------------------------------


def test_one_regex_covers_all_three_legacy_patterns() -> None:
    """serve.py, mcp/manager.py and workflow_store.py each grew their own list;
    the union is the point of this module."""
    for word in ("key", "token", "secret", "password", "passwd", "apikey",
                 "auth", "credential", "cookie", "session_id", "private"):
        assert SECRET_NAME_RE.search(word), word
        assert SECRET_NAME_RE.search(word.upper()), word
    assert is_secret_name("ANTHROPIC_API_KEY")
    assert not is_secret_name("PATH")
    assert not is_secret_name("")


def test_redact_value_hides_content_but_not_emptiness() -> None:
    assert "sk-live-1234" not in redact_value("sk-live-1234")
    assert redact_value("") == ""
    assert redact_value(None) == ""


# -- the allowlist -----------------------------------------------------------


def test_no_credential_shaped_variable_survives(monkeypatch) -> None:
    for name in CREDENTIAL_SHAPED:
        monkeypatch.setenv(name, "sk-super-secret")
    env = build_child_env()
    leaked = [k for k in env if is_secret_name(k)]
    assert leaked == [], f"secret-shaped vars reached the child: {leaked}"
    assert "sk-super-secret" not in "\x00".join(env.values())


def test_anthropic_api_key_specifically_is_absent(monkeypatch) -> None:
    """The one that pays the bill."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deadbeef")
    env = build_child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "sk-ant-deadbeef" not in "\x00".join(env.values())


def test_allowlisted_variables_survive(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/agent")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("TERM", "xterm-256color")
    env = build_child_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/agent"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "C"           # LC_* prefix, not an exact name
    assert env["TERM"] == "xterm-256color"


def test_unknown_variables_are_dropped_even_when_innocuous(monkeypatch) -> None:
    """Allowlist, not denylist: a name nobody anticipated loses by default."""
    monkeypatch.setenv("MY_HARMLESS_BUILD_FLAG", "1")
    assert "MY_HARMLESS_BUILD_FLAG" not in build_child_env()


def test_extra_pass_lets_callers_widen_the_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    assert build_child_env(extra_pass=["CI"])["CI"] == "true"


def test_extra_pass_cannot_smuggle_a_secret(monkeypatch) -> None:
    """The allowlist is not a way around the secret rule."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deadbeef")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_deadbeef")
    env = build_child_env(extra_pass=["ANTHROPIC_API_KEY", "GITHUB_TOKEN"])
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env


def test_base_mapping_is_used_instead_of_os_environ() -> None:
    env = build_child_env({"PATH": "/only/this", "SECRET_SAUCE": "x"})
    assert env["PATH"] == "/only/this"
    assert "SECRET_SAUCE" not in env


def test_tmpdir_is_rewritten_across_all_three_spellings() -> None:
    env = build_child_env({"PATH": "/bin", "TMPDIR": "/old", "TMP": "/old",
                           "TEMP": "/old"}, tmpdir="/box/tmp")
    assert env["TMPDIR"] == "/box/tmp"
    assert env["TMP"] == "/box/tmp"
    assert env["TEMP"] == "/box/tmp"


def test_path_is_never_empty() -> None:
    """A child with no PATH can't exec anything, which reads as a mysterious
    hang rather than as a security decision."""
    assert build_child_env({}).get("PATH")


# -- the sandbox seam --------------------------------------------------------


def test_child_env_delegates_and_scrubs(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deadbeef")
    env = child_env(SandboxPolicy(enabled=True))
    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("PATH")


def test_child_env_cwd_sets_pwd(tmp_path) -> None:
    env = child_env(SandboxPolicy(enabled=True), cwd=str(tmp_path))
    assert env["PWD"] == str(tmp_path)


def test_scrubbing_off_falls_back_to_the_denylist_not_to_nothing(monkeypatch) -> None:
    """Opting out of the allowlist must not be leakier than having no sandbox.

    This test previously asserted that ``scrub_env=False`` handed back the parent
    environment verbatim, on the reasoning that a half-scrubbed environment is the
    worst of both worlds. That reasoning is right about *allowlisting* and wrong
    about the floor: before this module existed, the bash tool applied a
    credential-name denylist to every child unconditionally. Returning
    ``dict(os.environ)`` therefore made turning the sandbox ON with scrubbing OFF
    leak strictly more than leaving it off entirely — a security control that
    makes things worse when half-enabled.

    So opting out drops to the old denylist: weaker than the allowlist, never
    absent. Ordinary variables still pass through untouched.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deadbeef")
    monkeypatch.setenv("MY_BUILD_FLAG", "verbose")
    env = child_env(SandboxPolicy(enabled=True, scrub_env=False))

    assert "ANTHROPIC_API_KEY" not in env
    # ...but this is genuinely a wider environment than the allowlist gives: an
    # unrecognized, non-credential-shaped variable survives, which is the whole
    # point of the opt-out.
    assert env["MY_BUILD_FLAG"] == "verbose"


def test_scrub_env_defaults_on_so_existing_constructions_keep_working() -> None:
    assert SandboxPolicy().scrub_env is True
    assert SandboxPolicy(enabled=True, network=False).scrub_env is True


def test_load_policy_honours_the_env_flag(monkeypatch, tmp_path) -> None:
    import json

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"sandbox": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.delenv("MANTIS_SANDBOX_SCRUB_ENV", raising=False)

    assert load_policy().scrub_env is True          # default when unset
    monkeypatch.setenv("MANTIS_SANDBOX_SCRUB_ENV", "0")
    assert load_policy().scrub_env is False
    monkeypatch.setenv("MANTIS_SANDBOX_SCRUB_ENV", "1")
    assert load_policy().scrub_env is True


def test_load_policy_honours_the_settings_key(monkeypatch, tmp_path) -> None:
    import json

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({
        "sandbox": {"enabled": True, "scrubEnv": False}}), encoding="utf-8")
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.delenv("MANTIS_SANDBOX_SCRUB_ENV", raising=False)
    assert load_policy().scrub_env is False


# -- the defect, stated as the attack ----------------------------------------


def test_a_real_subprocess_cannot_read_the_api_key(monkeypatch, tmp_path) -> None:
    """The end-to-end shape of the bug: spawn a child with the scrubbed env and
    prove the credential is not there to be exfiltrated."""
    import subprocess
    import sys

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deadbeef")
    r = subprocess.run(
        [sys.executable, "-c",
         "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))"],
        env=child_env(SandboxPolicy(enabled=True)),
        capture_output=True, text=True, timeout=60, check=False)
    assert r.stdout.strip() == "ABSENT", r.stderr

    # ...and the same child *without* scrubbing sees it, so the test is
    # measuring the scrub rather than an accident of how pytest is launched.
    r2 = subprocess.run(
        [sys.executable, "-c",
         "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'))"],
        env=dict(os.environ), capture_output=True, text=True, timeout=60,
        check=False)
    assert r2.stdout.strip() == "sk-ant-deadbeef"


# -- the wiring: the real `bash` tool, not a mock ----------------------------
#
# `build_child_env` existing is not the fix; the fix is the two places that
# actually spawn a shell calling it. Everything below drives the real tool
# object from `builtin_tools` and reads the child's own `env` output, because
# that is the exact thing an exfiltrating command would read.

# Four shapes of leak, and only the first two are caught by a denylist that
# greps for KEY/TOKEN/SECRET. The last two are the argument for an allowlist:
# a DSN's password lives in the *value*, and an agent socket is a credential
# with no credential-ish word in its name at all.
_SENTINELS = {
    "ANTHROPIC_API_KEY": "sk-ant-SENTINEL-e2e",
    "MY_SECRET_TOKEN": "tok-SENTINEL-e2e",
    "MY_DB_DSN": "postgres://user:pw-SENTINEL-e2e@db.internal/prod",
    "SSH_AUTH_SOCK": "/tmp/ssh-SENTINEL-e2e/agent.1",
}


def _plant_secrets(monkeypatch) -> None:
    for name, value in _SENTINELS.items():
        monkeypatch.setenv(name, value)


def _leaks(text: str) -> list[str]:
    return sorted({name for name, value in _SENTINELS.items()
                   if name in text or value in text})


def _run_bash(**kwargs) -> str:
    import anyio

    from mantis_agent.builtin_tools import bash  # the real tool object

    return anyio.run(lambda: bash.fn(**kwargs))


def test_foreground_bash_hands_the_child_a_scrubbed_environment(monkeypatch) -> None:
    """MANTIS_SANDBOX=1 + `env` in the child: nothing of ours may come back."""
    _plant_secrets(monkeypatch)
    monkeypatch.setenv("MANTIS_SANDBOX", "1")

    out = _run_bash(command="env | sort", timeout=60)

    assert "PATH=" in out, out          # the child really did run
    assert _leaks(out) == [], out


def test_background_bash_hands_the_child_a_scrubbed_environment(
        monkeypatch, tmp_path) -> None:
    """The background path builds its own `Popen`, so it can rot separately."""
    import time

    _plant_secrets(monkeypatch)
    monkeypatch.setenv("MANTIS_SANDBOX", "1")
    dump = tmp_path / "child-env.txt"

    started = _run_bash(command=f"env | sort > {dump}", timeout=60,
                        run_in_background=True)
    assert "Started in background" in started
    for _ in range(100):
        if dump.exists() and dump.read_text(encoding="utf-8", errors="replace"):
            break
        time.sleep(0.1)

    text = dump.read_text(encoding="utf-8", errors="replace")
    assert "PATH=" in text, text
    assert _leaks(text) == [], text

    from mantis_agent.builtin_tools.fs import terminate_background_shells

    terminate_background_shells()


def test_sandboxed_bash_keeps_the_toolchain_working(monkeypatch, tmp_path) -> None:
    """The allowlist has to leave a *usable* shell behind: an agent that can't
    find its own interpreter would just get the sandbox turned off."""
    monkeypatch.setenv("MANTIS_SANDBOX", "1")

    out = _run_bash(command="pwd && printenv HOME && python3 -c 'print(1+1)'",
                    timeout=60)

    assert "2" in out, out
    assert os.path.expanduser("~") in out, out


def test_sandbox_flags_are_inherited_by_nested_shells(monkeypatch) -> None:
    """A nested `mantis` that lost MANTIS_SANDBOX would run its own shells
    unconfined — the flag is a policy, not a secret."""
    monkeypatch.setenv("MANTIS_SANDBOX", "1")
    out = _run_bash(command="printenv MANTIS_SANDBOX || echo ABSENT", timeout=60)
    assert "1" in out and "ABSENT" not in out, out


def test_unsandboxed_bash_still_strips_credentials(monkeypatch) -> None:
    """Confinement off is not the same as protection off: the historical
    denylist still runs, so `env` never prints the key that pays the bill."""
    _plant_secrets(monkeypatch)
    monkeypatch.delenv("MANTIS_SANDBOX", raising=False)

    out = _run_bash(command="env | sort", timeout=60)

    assert "PATH=" in out, out
    assert "ANTHROPIC_API_KEY" not in out, out
    assert _SENTINELS["ANTHROPIC_API_KEY"] not in out, out
    assert _SENTINELS["MY_SECRET_TOKEN"] not in out, out


def test_the_spawn_sites_do_not_build_their_own_environment() -> None:
    """A regression guard with teeth: `bash` must not go back to filtering
    `os.environ` inline. Both spawn sites read one helper, and that helper is
    the only place allowed to decide what a child may know."""
    import inspect

    from mantis_agent.builtin_tools import fs

    src = inspect.getsource(fs.bash.fn)
    assert "_child_env(" in src
    assert "os.environ.items()" not in src
