"""``mantis update`` — install detection, version ordering, and the refusals.

Three things here are worth pinning down:

* **2.61 > 2.9.** A string compare gets that backwards, which would report "up
  to date" forever once the minor hit double digits. The fallback parser (used
  when ``packaging`` isn't installed — it is not a declared dependency) has to
  agree with ``packaging`` on this.
* **Source checkouts are never touched.** An editable install is somebody's
  working tree, and this project's routinely carries uncommitted work, so the
  command must print instructions and exit 1 rather than run anything.
* **The updater that actually runs** matches how mantis was installed. Handing a
  uv-managed tool to pip is how you get an "upgrade" that changes nothing.
"""

from __future__ import annotations

import sys

import pytest

from mantis_agent import update as u


# ---------------------------------------------------------------------------
# Version ordering
# ---------------------------------------------------------------------------


ORDER_CASES = [
    ("2.61.0", "2.9.0", True),      # numeric, not lexicographic
    ("2.9.0", "2.61.0", False),
    ("2.62.0", "2.61.0", True),
    ("2.61.0", "2.61.0", False),    # equal is not newer
    ("2.61.1", "2.61.0", True),
    ("2.61", "2.61.0", False),      # 2.61 == 2.61.0
    ("3.0.0", "2.99.99", True),
    ("2.61.0rc1", "2.61.0", False),  # prerelease sorts below its release
    ("2.61.0", "2.61.0rc1", True),
    ("2.61.0.post1", "2.61.0", True),
]


@pytest.mark.parametrize(("a", "b", "expected"), ORDER_CASES)
def test_is_newer(a: str, b: str, expected: bool) -> None:
    assert u.is_newer(a, b) is expected


@pytest.mark.parametrize(("a", "b", "expected"), ORDER_CASES)
def test_fallback_parser_agrees_without_packaging(monkeypatch, a, b, expected) -> None:
    """The no-``packaging`` path must reach the same verdicts — mantis does not
    depend on packaging, so on a clean install this is the code that runs."""
    real_import = __import__

    def _no_packaging(name, *rest):
        if name.startswith("packaging"):
            raise ImportError("packaging is not installed")
        return real_import(name, *rest)

    monkeypatch.setattr("builtins.__import__", _no_packaging)
    assert u.parse_version(a)[0] == 0  # confirms the fallback actually ran
    assert u.is_newer(a, b) is expected


def test_unparseable_version_does_not_crash() -> None:
    assert u.is_newer("not-a-version", "2.61.0") is False


# ---------------------------------------------------------------------------
# Install detection
# ---------------------------------------------------------------------------


def test_editable_checkout_is_detected_as_source() -> None:
    # The test suite runs from the checkout, so this is the live case.
    assert u.detect_install().kind == "source"


def _force_non_source(monkeypatch, prefix: str) -> None:
    monkeypatch.setattr(u, "_editable_root", lambda: None)
    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "executable", prefix + "/bin/python")


def test_uv_tool_install_uses_uv(monkeypatch) -> None:
    _force_non_source(monkeypatch, "/home/me/.local/share/uv/tools/mantis-agent-sdk")
    got = u.detect_install()
    assert got.kind == "uv"
    assert got.command == ["uv", "tool", "upgrade", u.DIST_NAME]


def test_pipx_install_uses_pipx(monkeypatch) -> None:
    _force_non_source(monkeypatch, "/home/me/.local/pipx/venvs/mantis-agent-sdk")
    got = u.detect_install()
    assert got.kind == "pipx"
    assert got.command == ["pipx", "upgrade", u.DIST_NAME]


def test_plain_install_falls_back_to_pip(monkeypatch) -> None:
    _force_non_source(monkeypatch, "/usr/local")
    monkeypatch.setattr(u, "_has_pip", lambda: True)
    got = u.detect_install()
    assert got.kind == "pip"
    assert got.command[1:] == ["-m", "pip", "install", "-U", u.DIST_SPEC]


def test_venv_without_pip_uses_uv(monkeypatch) -> None:
    """uv-created venvs ship no pip, so `python -m pip` dies with "No module
    named pip". Observed for real on a uv venv — uv installs into the same
    interpreter instead."""
    _force_non_source(monkeypatch, "/home/me/scratch/venv")
    monkeypatch.setattr(u, "_has_pip", lambda: False)
    monkeypatch.setattr(u.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    got = u.detect_install()
    assert got.kind == "uv-pip"
    # --python pins the target interpreter; without it uv would guess from
    # $VIRTUAL_ENV and could upgrade a different environment entirely.
    assert got.command[:4] == ["uv", "pip", "install", "--python"]
    assert got.command[4] == sys.executable
    assert got.command[-1] == u.DIST_SPEC


def test_no_pip_and_no_uv_still_reports_the_pip_command(monkeypatch) -> None:
    _force_non_source(monkeypatch, "/home/me/scratch/venv")
    monkeypatch.setattr(u, "_has_pip", lambda: False)
    monkeypatch.setattr(u.shutil, "which", lambda _name: None)
    assert u.detect_install().kind == "pip"


def test_pip_target_keeps_the_cli_extra() -> None:
    # Upgrading to bare mantis-agent-sdk would drop the terminal deps.
    assert u.DIST_SPEC == "mantis-agent-sdk[cli]"


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@pytest.fixture
def _pypi(monkeypatch):
    """Pin the 'latest on PyPI' answer so no test touches the network."""

    def _set(version: str | None, detail: str = "") -> None:
        monkeypatch.setattr(u, "latest_version", lambda **_: (version, detail))

    return _set


def test_already_current_exits_zero_and_runs_nothing(monkeypatch, _pypi, capsys) -> None:
    _pypi("2.61.0")
    monkeypatch.setattr(u, "current_version", lambda: "2.61.0")
    monkeypatch.setattr(u.subprocess, "run", _explode)
    assert u.run_update([]) == 0
    assert "already on the latest release" in capsys.readouterr().out


def test_local_build_ahead_of_pypi_says_so(monkeypatch, _pypi, capsys) -> None:
    # A dev build is newer than anything published; "already on the latest
    # release (2.61.0)" while running 2.62.0 reads like the check misfired.
    _pypi("2.61.0")
    monkeypatch.setattr(u, "current_version", lambda: "2.62.0")
    monkeypatch.setattr(u.subprocess, "run", _explode)
    assert u.run_update([]) == 0
    assert "ahead of the published release (2.61.0)" in capsys.readouterr().out


def test_source_checkout_refuses_and_exits_one(monkeypatch, _pypi, capsys) -> None:
    _pypi("2.99.0")
    monkeypatch.setattr(u, "current_version", lambda: "2.61.0")
    monkeypatch.setattr(u, "detect_install",
                        lambda: u.Install("source", "editable", None, "/repo"))
    monkeypatch.setattr(u.subprocess, "run", _explode)  # must not run anything
    assert u.run_update([]) == 1
    out = capsys.readouterr().out
    assert "won't touch it" in out
    assert "git -C /repo pull" in out


def test_check_never_installs_even_when_outdated(monkeypatch, _pypi, capsys) -> None:
    _pypi("2.99.0")
    monkeypatch.setattr(u, "current_version", lambda: "2.61.0")
    monkeypatch.setattr(u, "detect_install",
                        lambda: u.Install("pip", "pip", ["pip", "install", "-U", "x"]))
    monkeypatch.setattr(u.subprocess, "run", _explode)
    # Exit 1 = "an update is available and was not applied", so `mantis update
    # --check` is usable as a shell condition.
    assert u.run_update(["--check"]) == 1
    assert "update available: 2.61.0 → 2.99.0" in capsys.readouterr().out


def test_successful_update_runs_the_detected_command(monkeypatch, _pypi, capsys) -> None:
    _pypi("2.99.0")
    monkeypatch.setattr(u, "current_version", lambda: "2.61.0")
    monkeypatch.setattr(u, "detect_install",
                        lambda: u.Install("uv", "uv", ["uv", "tool", "upgrade", "x"]))
    ran: list[list[str]] = []

    def _fake_run(cmd, **kw):
        ran.append(list(cmd))
        return _Completed(0)

    monkeypatch.setattr(u.subprocess, "run", _fake_run)
    monkeypatch.setattr(u, "_installed_version_fresh", lambda: "2.99.0")
    assert u.run_update([]) == 0
    assert ran == [["uv", "tool", "upgrade", "x"]]
    assert "updated 2.61.0 → 2.99.0" in capsys.readouterr().out


def test_failed_updater_reports_and_exits_one(monkeypatch, _pypi, capsys) -> None:
    _pypi("2.99.0")
    monkeypatch.setattr(u, "current_version", lambda: "2.61.0")
    monkeypatch.setattr(u, "detect_install",
                        lambda: u.Install("pip", "pip", ["pip", "install", "-U", "x"]))
    monkeypatch.setattr(u.subprocess, "run", lambda *a, **k: _Completed(2))
    assert u.run_update([]) == 1
    assert "nothing was changed" in capsys.readouterr().out


def test_missing_updater_binary_suggests_pip(monkeypatch, _pypi, capsys) -> None:
    _pypi("2.99.0")
    monkeypatch.setattr(u, "current_version", lambda: "2.61.0")
    monkeypatch.setattr(u, "detect_install",
                        lambda: u.Install("uv", "uv", ["uv", "tool", "upgrade", "x"]))

    def _missing(*a, **k):
        raise FileNotFoundError("uv")

    monkeypatch.setattr(u.subprocess, "run", _missing)
    assert u.run_update([]) == 1
    assert "not on PATH" in capsys.readouterr().out


def test_offline_is_reported_not_crashed(monkeypatch, _pypi, capsys) -> None:
    _pypi(None, "could not reach PyPI (ConnectError)")
    monkeypatch.setattr(u, "current_version", lambda: "2.61.0")
    monkeypatch.setattr(u.subprocess, "run", _explode)
    assert u.run_update([]) == 1
    assert "could not reach PyPI" in capsys.readouterr().out


def test_latest_version_handles_a_bad_response(monkeypatch) -> None:
    class _R:
        status_code = 500

        def json(self):  # pragma: no cover — never reached on a 500
            return {}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _R())
    version, detail = u.latest_version()
    assert version is None
    assert "500" in detail


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["update", "upgrade"])
def test_mantis_word_dispatches_to_run_update(monkeypatch, word) -> None:
    """`mantis update` must reach run_update before the terminal boots — and
    `upgrade` is accepted because people type it."""
    from mantis_agent import tui

    seen: list[list[str]] = []
    monkeypatch.setattr(u, "run_update", lambda argv: seen.append(argv) or 0)
    assert tui.main([word, "--check"]) == 0
    assert seen == [["--check"]]


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _explode(*a, **k):  # noqa: ANN002, ANN003
    raise AssertionError("no subprocess should run in this case")
