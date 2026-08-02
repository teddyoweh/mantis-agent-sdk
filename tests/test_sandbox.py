"""OS-level shell confinement.

The interesting assertions here actually run commands under the sandbox — a
policy that generates a plausible-looking profile but doesn't stop a write is
worse than no sandbox at all, because it makes people trust unattended runs.
"""

from __future__ import annotations

import os
import pathlib
import platform
import subprocess
import sys

import pytest

from mantis_agent.sandbox import (
    SandboxPolicy,
    SandboxUnavailable,
    available_backend,
    load_policy,
    sandbox_status,
    seatbelt_profile,
    wrap_command,
)

HAS_BACKEND = available_backend() is not None
needs_sandbox = pytest.mark.skipif(
    not HAS_BACKEND, reason="no sandbox backend on this platform")


# -- policy ------------------------------------------------------------------


def test_off_by_default_and_a_no_op_when_off() -> None:
    """Turning a shell's capabilities down without being asked would be a
    nasty surprise, so the default is off and wrapping is identity."""
    argv = ["bash", "-lc", "echo hi"]
    assert wrap_command(argv, SandboxPolicy(enabled=False)) == argv


def test_writable_roots_always_include_cwd_and_temp(tmp_path) -> None:
    roots = SandboxPolicy(enabled=True).roots_for(str(tmp_path))
    assert str(tmp_path.resolve()) in roots
    import tempfile

    assert str(pathlib.Path(tempfile.gettempdir()).resolve()) in roots


def test_extra_roots_are_resolved_and_deduped(tmp_path) -> None:
    pol = SandboxPolicy(enabled=True,
                        writable_roots=[str(tmp_path), str(tmp_path), "~"])
    roots = pol.roots_for(str(tmp_path))
    assert roots.count(str(tmp_path.resolve())) == 1
    assert os.path.expanduser("~") in roots


def test_settings_drive_the_policy(monkeypatch, tmp_path) -> None:
    import json

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({
        "sandbox": {"enabled": True, "network": False,
                    "writableRoots": ["/opt/build"], "failIfUnavailable": True}
    }), encoding="utf-8")
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))
    monkeypatch.delenv("MANTIS_SANDBOX", raising=False)

    pol = load_policy()
    assert pol.enabled and pol.fail_if_unavailable
    assert pol.network is False
    assert "/opt/build" in pol.writable_roots


def test_env_overrides_settings(monkeypatch, tmp_path) -> None:
    """`--sandbox` has to reach subagents and background shells, which it does
    by riding on the environment."""
    import json

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"sandbox": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(home))

    monkeypatch.setenv("MANTIS_SANDBOX", "1")
    assert load_policy().enabled is True
    monkeypatch.setenv("MANTIS_SANDBOX", "0")
    assert load_policy().enabled is False
    monkeypatch.setenv("MANTIS_SANDBOX", "1")
    monkeypatch.setenv("MANTIS_SANDBOX_NETWORK", "0")
    assert load_policy().network is False


# -- profile generation ------------------------------------------------------


@pytest.mark.skipif(platform.system() != "Darwin", reason="seatbelt is macOS")
def test_seatbelt_profile_denies_writes_then_allows_the_roots(tmp_path) -> None:
    prof = seatbelt_profile(SandboxPolicy(enabled=True), str(tmp_path))
    assert "(deny file-write*)" in prof
    assert str(tmp_path.resolve()) in prof
    # order matters in seatbelt: the blanket deny must precede the allows
    assert prof.index("(deny file-write*)") < prof.index(str(tmp_path.resolve()))
    assert "(deny network*)" not in prof            # network on by default
    assert "(deny network*)" in seatbelt_profile(
        SandboxPolicy(enabled=True, network=False), str(tmp_path))


def test_unavailable_is_a_warning_by_default_and_fatal_on_request(monkeypatch) -> None:
    monkeypatch.setattr("mantis_agent.sandbox.available_backend", lambda: None)
    argv = ["bash", "-lc", "echo hi"]
    assert wrap_command(argv, SandboxPolicy(enabled=True)) == argv
    with pytest.raises(SandboxUnavailable):
        wrap_command(argv, SandboxPolicy(enabled=True, fail_if_unavailable=True))


def test_status_explains_itself(monkeypatch, tmp_path) -> None:
    st = sandbox_status(SandboxPolicy(enabled=True), cwd=str(tmp_path))
    assert st["enabled"] is True
    assert st["active"] is HAS_BACKEND
    if not HAS_BACKEND:
        assert st["reason"]                        # says why, not just "no"

    monkeypatch.setattr("mantis_agent.sandbox.available_backend", lambda: None)
    off = sandbox_status(SandboxPolicy(enabled=False), cwd=str(tmp_path))
    assert off["active"] is False and off["reason"]


# -- it actually confines ----------------------------------------------------


def _run(argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60,
                          check=False)


@needs_sandbox
def test_writes_inside_the_project_still_work(tmp_path) -> None:
    argv = wrap_command(["bash", "-lc", "echo ok > f.txt && cat f.txt"],
                        SandboxPolicy(enabled=True), cwd=str(tmp_path))
    r = _run(argv, str(tmp_path))
    assert r.returncode == 0 and "ok" in r.stdout
    assert (tmp_path / "f.txt").read_text().strip() == "ok"


@needs_sandbox
def test_writes_outside_the_project_are_refused_by_the_os(tmp_path) -> None:
    """The whole point. Not a permission prompt — a kernel-level refusal."""
    # Temp is a writable root on purpose (compilers, pip, shells all need it),
    # so the escape attempt has to aim somewhere that genuinely isn't.
    target = pathlib.Path.home() / ".mantis-sandbox-escape-check"
    if target.exists():
        target.unlink()
    argv = wrap_command(["bash", "-lc", f"echo nope > {target}"],
                        SandboxPolicy(enabled=True), cwd=str(tmp_path))
    r = _run(argv, str(tmp_path))
    assert r.returncode != 0
    assert not target.exists(), "the sandbox let a write escape"


@needs_sandbox
def test_reads_outside_the_project_still_work(tmp_path) -> None:
    """Confining reads too would break every interpreter on the box."""
    argv = wrap_command(["bash", "-lc", f"cat {sys.executable} > /dev/null && echo read-ok"],
                        SandboxPolicy(enabled=True), cwd=str(tmp_path))
    r = _run(argv, str(tmp_path))
    assert "read-ok" in r.stdout


@needs_sandbox
def test_extra_writable_root_is_honoured(tmp_path) -> None:
    """A root that is genuinely outside cwd and temp — so the write can only
    succeed because the policy named it."""
    extra = pathlib.Path.home() / ".mantis-sandbox-extra-root-check"
    extra.mkdir(exist_ok=True)
    try:
        target = extra / "f.txt"
        if target.exists():
            target.unlink()
        # not listed → refused
        denied = _run(wrap_command(["bash", "-lc", f"echo no > {target}"],
                                   SandboxPolicy(enabled=True), cwd=str(tmp_path)),
                      str(tmp_path))
        assert denied.returncode != 0 and not target.exists()
        # listed → allowed
        pol = SandboxPolicy(enabled=True, writable_roots=[str(extra)])
        allowed = _run(wrap_command(["bash", "-lc", f"echo ok > {target}"], pol,
                                    cwd=str(tmp_path)), str(tmp_path))
        assert allowed.returncode == 0 and target.read_text().strip() == "ok"
    finally:
        import shutil

        shutil.rmtree(extra, ignore_errors=True)


@needs_sandbox
@pytest.mark.skipif(os.environ.get("MANTIS_OFFLINE_TESTS") == "1",
                    reason="needs outbound network to prove it gets blocked")
def test_network_can_be_blocked(tmp_path) -> None:
    cmd = ("curl -s -m 5 -o /dev/null -w '%{http_code}' https://example.com "
           "|| echo BLOCKED")
    allowed = _run(wrap_command(["bash", "-lc", cmd],
                                SandboxPolicy(enabled=True, network=True),
                                cwd=str(tmp_path)), str(tmp_path))
    blocked = _run(wrap_command(["bash", "-lc", cmd],
                                SandboxPolicy(enabled=True, network=False),
                                cwd=str(tmp_path)), str(tmp_path))
    if "200" not in allowed.stdout:
        pytest.skip("no outbound network in this environment")
    assert "BLOCKED" in blocked.stdout or "000" in blocked.stdout


@needs_sandbox
def test_the_bash_tool_honours_the_policy(monkeypatch, tmp_path) -> None:
    """End to end through the actual tool the agent calls."""
    import anyio

    from mantis_agent.builtin_tools import bash
    from mantis_agent.builtin_tools import fs as fs_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANTIS_SANDBOX", "1")
    # bash remembers its cwd across calls (so `cd` sticks like a real shell) in
    # module state — clear it, or this test writes wherever an earlier test left
    # the shell parked.
    monkeypatch.setattr(fs_mod, "_BASH_CWD_BY_SCOPE", {}, raising=False)

    target = pathlib.Path.home() / ".mantis-sandbox-tool-escape-check"
    if target.exists():
        target.unlink()

    out = anyio.run(lambda: bash.fn(command=f"echo nope > {target}"))
    assert not target.exists(), f"bash escaped the sandbox: {out}"

    ok = anyio.run(lambda: bash.fn(command="echo inside > allowed.txt && cat allowed.txt"))
    assert "inside" in ok
    assert (tmp_path / "allowed.txt").exists(), "the write landed outside the test dir"
