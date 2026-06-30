"""``mantis-agent setup-local`` — CPU-friendly model catalog + install helper.

These tests cover the pure / mockable bits: the catalog shape, the
default recommendation, environment detection, and that the CLI
subcommand parses + dispatches to the right handler. The actual
``ollama pull`` and ``ollama serve`` paths are integration territory.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from mantis_agent import cli
from mantis_agent import setup_local as setup_local_mod
from mantis_agent.setup_local import (
    CPU_FRIENDLY_MODELS,
    DEFAULT_RECOMMENDATION,
    LocalModel,
    WINDOWS_INSTALLER_FLAGS,
    WINDOWS_INSTALLER_URL,
    install_ollama,
    install_ollama_windows,
    is_ollama_installed,
    is_ollama_running,
    print_model_table,
    run_setup_local,
    start_ollama_server,
)


# ---------------------------------------------------------------------------
# Catalog invariants
# ---------------------------------------------------------------------------


def test_catalog_is_nonempty_and_well_typed() -> None:
    assert len(CPU_FRIENDLY_MODELS) >= 5
    for m in CPU_FRIENDLY_MODELS:
        assert isinstance(m, LocalModel)
        assert m.tag and ":" in m.tag  # all entries are Ollama tag-form
        assert m.size_gb > 0
        assert m.min_ram_gb > 0
        assert m.family


def test_default_recommendation_is_in_catalog() -> None:
    tags = {m.tag for m in CPU_FRIENDLY_MODELS}
    assert DEFAULT_RECOMMENDATION in tags


def test_catalog_ordered_by_size() -> None:
    """Smaller models first — we want users to see the cheap options
    at the top of the table."""

    sizes = [m.size_gb for m in CPU_FRIENDLY_MODELS]
    assert sizes == sorted(sizes), "CPU_FRIENDLY_MODELS should be size-ascending"


def test_catalog_caps_at_8b() -> None:
    """Nothing > 8 B params — anything bigger is GPU territory."""

    for m in CPU_FRIENDLY_MODELS:
        # `params` is a short string like "1.5B" / "8B" — strip and parse.
        n = float(m.params.rstrip("BMK").rstrip("b").rstrip())
        if m.params.endswith(("M", "m")):
            n /= 1000  # 135M = 0.135B
        assert n <= 8.0, f"{m.tag} ({m.params}) exceeds CPU ceiling"


def test_reasoning_models_flagged_thinking() -> None:
    """If a tag is in the DeepSeek-R1 family, it must be marked
    `thinking=True` so the agent loop knows to parse `<think>` blocks."""

    for m in CPU_FRIENDLY_MODELS:
        if m.family == "DeepSeek":
            assert m.thinking, f"{m.tag} should emit <think> blocks"


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def test_is_ollama_installed_returns_bool() -> None:
    """Type-check only — actual installation state varies per machine."""

    assert isinstance(is_ollama_installed(), bool)


def test_is_ollama_running_returns_false_for_unreachable_host() -> None:
    """A host that can't possibly answer should give False, not raise."""

    assert is_ollama_running("http://127.0.0.1:1") is False


# ---------------------------------------------------------------------------
# CLI plumbing — `mantis-agent setup-local --list`
# ---------------------------------------------------------------------------


def test_setup_local_command_registered() -> None:
    """The CLI parser must accept `setup-local` as a subcommand."""

    parser = cli._build_parser()
    args = parser.parse_args(["setup-local", "--list"])
    assert args.cmd == "setup-local"
    assert args.list_models is True


def test_setup_local_list_prints_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    """`mantis-agent setup-local --list` should print every model tag."""

    print_model_table()
    out = capsys.readouterr().out
    for m in CPU_FRIENDLY_MODELS:
        assert m.tag in out
    assert "Default recommendation" in out


def test_setup_local_list_dispatches_through_main(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: invoking the CLI with `setup-local --list` runs
    cleanly and returns 0."""

    rc = cli.main(["setup-local", "--list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert DEFAULT_RECOMMENDATION in out


# ---------------------------------------------------------------------------
# Auto-start `ollama serve` when the daemon isn't running
# ---------------------------------------------------------------------------


def test_start_ollama_server_short_circuits_when_already_running() -> None:
    """If the daemon already answers, we must not spawn a second one."""

    with patch(
        "mantis_agent.setup_local.is_ollama_running", return_value=True
    ) as running, patch(
        "mantis_agent.setup_local.subprocess.Popen"
    ) as popen:
        ok, log_path = start_ollama_server()
    assert ok is True
    assert log_path is None
    assert running.called
    popen.assert_not_called()


def test_start_ollama_server_returns_false_when_not_installed() -> None:
    """No ollama on PATH → can't start it. Don't try."""

    with patch(
        "mantis_agent.setup_local.is_ollama_running", return_value=False
    ), patch(
        "mantis_agent.setup_local.is_ollama_installed", return_value=False
    ), patch(
        "mantis_agent.setup_local.subprocess.Popen"
    ) as popen:
        ok, log_path = start_ollama_server()
    assert ok is False
    assert log_path is None
    popen.assert_not_called()


def test_start_ollama_server_spawns_and_polls_until_ready() -> None:
    """When ollama is installed but down, spawn `ollama serve` and wait."""

    # First two probes return False (still booting), third returns True.
    probes = iter([False, False, True])

    def fake_running(*_a: object, **_k: object) -> bool:
        return next(probes)

    with patch(
        "mantis_agent.setup_local.is_ollama_installed", return_value=True
    ), patch(
        "mantis_agent.setup_local.is_ollama_running", side_effect=fake_running
    ), patch(
        "mantis_agent.setup_local.subprocess.Popen"
    ) as popen, patch(
        "mantis_agent.setup_local.time.sleep"  # don't actually sleep in tests
    ):
        ok, log_path = start_ollama_server(timeout_s=5.0)

    assert ok is True
    assert log_path is not None
    assert popen.call_count == 1
    # We MUST detach the daemon — otherwise it dies when the user's
    # `mantis-agent setup-local` process exits.
    _, kwargs = popen.call_args
    if "start_new_session" in kwargs:
        assert kwargs["start_new_session"] is True
    elif "creationflags" in kwargs:
        # Windows: must request a new process group + detached
        assert kwargs["creationflags"] != 0


def test_start_ollama_server_times_out_cleanly() -> None:
    """If the daemon never answers, return False — don't hang forever."""

    with patch(
        "mantis_agent.setup_local.is_ollama_installed", return_value=True
    ), patch(
        "mantis_agent.setup_local.is_ollama_running", return_value=False
    ), patch(
        "mantis_agent.setup_local.subprocess.Popen"
    ), patch(
        "mantis_agent.setup_local.time.sleep"
    ):
        ok, log_path = start_ollama_server(timeout_s=0.1)

    assert ok is False
    assert log_path is not None  # caller can read the captured server log


def test_run_setup_local_auto_starts_server_by_default() -> None:
    """The whole point of `setup-local`: don't punt to the user — start
    `ollama serve` ourselves when it's installed but not running."""

    with patch(
        "mantis_agent.setup_local.is_ollama_installed", return_value=True
    ), patch(
        "mantis_agent.setup_local.is_ollama_running", return_value=False
    ), patch(
        "mantis_agent.setup_local.start_ollama_server",
        return_value=(True, "/tmp/ollama.log"),
    ) as starter, patch(
        "mantis_agent.setup_local.pull_model", return_value=0
    ), patch(
        "mantis_agent.setup_local.smoke_test", return_value=True
    ):
        rc = run_setup_local(skip_smoke_test=True)

    assert rc == 0
    starter.assert_called_once()


def test_run_setup_local_can_opt_out_of_auto_start(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`auto_start_server=False` preserves the old behavior: just say
    'go run ollama serve' and exit 1."""

    with patch(
        "mantis_agent.setup_local.is_ollama_installed", return_value=True
    ), patch(
        "mantis_agent.setup_local.is_ollama_running", return_value=False
    ), patch(
        "mantis_agent.setup_local.start_ollama_server"
    ) as starter:
        rc = run_setup_local(auto_start_server=False)

    assert rc == 1
    starter.assert_not_called()
    out = capsys.readouterr().out
    assert "ollama serve" in out


def test_run_setup_local_reports_when_auto_start_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If auto-start fails, surface the captured log path so the user can
    actually debug — don't just say 'failed'."""

    with patch(
        "mantis_agent.setup_local.is_ollama_installed", return_value=True
    ), patch(
        "mantis_agent.setup_local.is_ollama_running", return_value=False
    ), patch(
        "mantis_agent.setup_local.start_ollama_server",
        return_value=(False, "/tmp/ollama-fail.log"),
    ):
        rc = run_setup_local()

    assert rc == 1
    err = capsys.readouterr().err
    assert "/tmp/ollama-fail.log" in err


def test_cli_no_auto_start_server_flag_parses() -> None:
    """The CLI must expose the opt-out flag and forward it correctly."""

    parser = cli._build_parser()
    args = parser.parse_args(["setup-local", "--no-auto-start-server"])
    assert args.auto_start_server is False

    args_default = parser.parse_args(["setup-local"])
    assert args_default.auto_start_server is True


def test_cli_start_timeout_flag_parses() -> None:
    """`--start-timeout 30` should land in args as start_timeout_s=30.0."""

    parser = cli._build_parser()
    args = parser.parse_args(["setup-local", "--start-timeout", "30"])
    assert args.start_timeout_s == 30.0


# ---------------------------------------------------------------------------
# Windows installer wrapper
# ---------------------------------------------------------------------------
#
# These tests run on every platform — we don't gate on ``sys.platform``
# because the function under test is pure-Python and the OS-specific bits
# (subprocess launch, urllib fetch) are mocked. The function being
# callable on Linux/macOS during the test suite is intentional: it keeps
# us from accidentally writing code that imports ``win32api`` and silently
# fails on the CI machines we actually run.


import os
import urllib.error


def _make_installer_constants_known() -> None:
    """Sanity check the module-level constants other tests rely on."""

    assert WINDOWS_INSTALLER_URL.startswith("https://ollama.com/")
    assert WINDOWS_INSTALLER_URL.endswith(".exe")
    # The four canonical Inno Setup silent-install flags.
    assert "/VERYSILENT" in WINDOWS_INSTALLER_FLAGS
    assert "/SUPPRESSMSGBOXES" in WINDOWS_INSTALLER_FLAGS
    assert "/NORESTART" in WINDOWS_INSTALLER_FLAGS
    assert "/SP-" in WINDOWS_INSTALLER_FLAGS


def test_windows_installer_constants_well_formed() -> None:
    _make_installer_constants_known()


def test_install_ollama_dispatches_to_windows_on_win32() -> None:
    """The umbrella ``install_ollama()`` must route to the Windows path
    when ``sys.platform`` looks like Windows. Without this, Windows users
    get the old "go download it yourself" message back."""

    with patch.object(setup_local_mod.sys, "platform", "win32"), patch.object(
        setup_local_mod, "install_ollama_windows", return_value=0
    ) as winstall:
        rc = install_ollama(auto_confirm=True)

    assert rc == 0
    winstall.assert_called_once_with(auto_confirm=True)


def test_install_ollama_does_not_dispatch_to_windows_on_posix() -> None:
    """On Linux/macOS we must keep the curl-pipe-sh code path. Regressing
    that would 404 on any non-Windows machine."""

    with patch.object(setup_local_mod.sys, "platform", "linux"), patch.object(
        setup_local_mod, "install_ollama_windows"
    ) as winstall, patch.object(
        setup_local_mod.urllib.request, "urlopen"
    ) as urlopen, patch.object(
        setup_local_mod.subprocess, "call", return_value=0
    ):
        # Fake a tiny install.sh body so the call() codepath runs.
        urlopen.return_value.__enter__.return_value.read.return_value = b"#!/bin/sh\necho ok\n"
        rc = install_ollama(auto_confirm=True)

    assert rc == 0
    winstall.assert_not_called()


def test_install_ollama_windows_uses_preexisting_installer_when_provided(
    tmp_path: object,
) -> None:
    """When ``installer_path`` is set, we MUST NOT fetch anything — the
    user already has the .exe on disk. Mock urllib so a hit would fail
    the test."""

    fake_exe = tmp_path / "OllamaSetup.exe"  # type: ignore[operator]
    fake_exe.write_bytes(b"MZ" + b"\x00" * 64)  # plausible PE header

    with patch.object(
        setup_local_mod.urllib.request, "urlopen"
    ) as urlopen, patch.object(
        setup_local_mod.subprocess, "call", return_value=0
    ) as scall, patch.object(
        setup_local_mod, "_prepend_to_process_path"
    ):
        rc = install_ollama_windows(
            auto_confirm=True, installer_path=str(fake_exe)
        )

    assert rc == 0
    urlopen.assert_not_called()
    args, _ = scall.call_args
    cmd = args[0]
    assert cmd[0] == str(fake_exe)
    # Every Inno Setup silent flag must be present, in order.
    for flag in WINDOWS_INSTALLER_FLAGS:
        assert flag in cmd, f"missing flag: {flag}"


def test_install_ollama_windows_downloads_and_runs_installer(
    tmp_path: object,
) -> None:
    """Happy path: download the .exe to a tempfile, run it silently,
    return 0, then clean up the tempfile."""

    # Capture which tempfile gets created so we can assert it was deleted.
    captured_tmp: dict[str, str] = {}
    real_mkstemp = setup_local_mod.tempfile.mkstemp

    def spy_mkstemp(**kwargs: object) -> tuple[int, str]:
        fd, p = real_mkstemp(**kwargs)
        captured_tmp["path"] = p
        return fd, p

    fake_body = io.BytesIO(b"\x4d\x5a" + b"\x00" * 1024)  # tiny "exe"

    with patch.object(
        setup_local_mod.tempfile, "mkstemp", side_effect=spy_mkstemp
    ), patch.object(setup_local_mod.urllib.request, "urlopen") as urlopen, patch.object(
        setup_local_mod.subprocess, "call", return_value=0
    ) as scall, patch.object(
        setup_local_mod, "_prepend_to_process_path"
    ) as path_patch:
        urlopen.return_value.__enter__.return_value.read.side_effect = (
            lambda n=-1: fake_body.read(n)
        )
        rc = install_ollama_windows(auto_confirm=True)

    assert rc == 0
    # We actually launched the installer with the silent flags.
    args, _ = scall.call_args
    cmd = args[0]
    assert cmd[0] == captured_tmp["path"]
    for flag in WINDOWS_INSTALLER_FLAGS:
        assert flag in cmd
    # The temp .exe was cleaned up post-run.
    assert not os.path.exists(captured_tmp["path"])
    # PATH was augmented for the current process.
    path_patch.assert_called_once()


def test_install_ollama_windows_handles_download_failure() -> None:
    """When the .exe fetch fails (no network, 404, etc.), we must return
    non-zero and NOT try to subprocess-launch a half-downloaded file."""

    with patch.object(
        setup_local_mod.urllib.request,
        "urlopen",
        side_effect=urllib.error.URLError("network down"),
    ), patch.object(
        setup_local_mod.subprocess, "call"
    ) as scall:
        rc = install_ollama_windows(auto_confirm=True)

    assert rc == 1
    scall.assert_not_called()


def test_install_ollama_windows_propagates_installer_nonzero_exit(
    tmp_path: object,
) -> None:
    """If OllamaSetup.exe returns 1 (user cancelled UAC, disk full,
    whatever), the wrapper must surface that exit code — we don't
    silently swallow errors."""

    fake_exe = tmp_path / "OllamaSetup.exe"  # type: ignore[operator]
    fake_exe.write_bytes(b"MZ")

    with patch.object(
        setup_local_mod.subprocess, "call", return_value=1602  # Windows ERROR_CANCELLED
    ), patch.object(setup_local_mod, "_prepend_to_process_path") as path_patch:
        rc = install_ollama_windows(
            auto_confirm=True, installer_path=str(fake_exe)
        )

    assert rc == 1602
    # Failed install → don't lie to the rest of the process about PATH.
    path_patch.assert_not_called()


def test_install_ollama_windows_handles_installer_missing(
    tmp_path: object,
) -> None:
    """``installer_path`` pointing at a non-existent file must fail
    fast with a clear error, not 'try to run it and OSError'."""

    missing = tmp_path / "nope.exe"  # type: ignore[operator]
    with patch.object(setup_local_mod.subprocess, "call") as scall:
        rc = install_ollama_windows(
            auto_confirm=True, installer_path=str(missing)
        )
    assert rc != 0
    scall.assert_not_called()


def test_install_ollama_windows_handles_subprocess_oserror(
    tmp_path: object,
) -> None:
    """If Windows refuses to launch the installer (rare — corrupt download,
    EPERM), we report and return non-zero without crashing."""

    fake_exe = tmp_path / "OllamaSetup.exe"  # type: ignore[operator]
    fake_exe.write_bytes(b"MZ")

    with patch.object(
        setup_local_mod.subprocess, "call", side_effect=OSError("denied")
    ):
        rc = install_ollama_windows(
            auto_confirm=True, installer_path=str(fake_exe)
        )

    assert rc != 0


def test_install_ollama_windows_respects_timeout(tmp_path: object) -> None:
    """A stuck installer must time out cleanly, not block forever."""

    import subprocess as real_subprocess

    fake_exe = tmp_path / "OllamaSetup.exe"  # type: ignore[operator]
    fake_exe.write_bytes(b"MZ")

    with patch.object(
        setup_local_mod.subprocess,
        "call",
        side_effect=real_subprocess.TimeoutExpired(cmd="OllamaSetup", timeout=1),
    ):
        rc = install_ollama_windows(
            auto_confirm=True, installer_path=str(fake_exe), timeout_s=0.01
        )

    assert rc != 0


def test_install_ollama_windows_prompts_when_not_auto_confirm() -> None:
    """Without ``auto_confirm=True``, the wrapper must ask the user
    before fetching/running anything — matches the POSIX contract."""

    with patch("builtins.input", return_value="n"), patch.object(
        setup_local_mod.urllib.request, "urlopen"
    ) as urlopen, patch.object(
        setup_local_mod.subprocess, "call"
    ) as scall:
        rc = install_ollama_windows()

    assert rc != 0
    urlopen.assert_not_called()
    scall.assert_not_called()


def test_install_ollama_windows_proceeds_on_yes() -> None:
    """``y`` at the prompt should proceed all the way through."""

    fake_body = io.BytesIO(b"MZ")
    with patch("builtins.input", return_value="y"), patch.object(
        setup_local_mod.urllib.request, "urlopen"
    ) as urlopen, patch.object(
        setup_local_mod.subprocess, "call", return_value=0
    ) as scall, patch.object(setup_local_mod, "_prepend_to_process_path"):
        urlopen.return_value.__enter__.return_value.read.side_effect = (
            lambda n=-1: fake_body.read(n)
        )
        rc = install_ollama_windows()
    assert rc == 0
    urlopen.assert_called()
    scall.assert_called()


def test_install_ollama_windows_prompt_eof_returns_nonzero() -> None:
    """If stdin closes during the prompt (CI environment, no TTY), we
    must NOT silently proceed — that would surprise-install Ollama."""

    with patch("builtins.input", side_effect=EOFError()), patch.object(
        setup_local_mod.urllib.request, "urlopen"
    ) as urlopen:
        rc = install_ollama_windows()
    assert rc != 0
    urlopen.assert_not_called()


def test_install_ollama_windows_uses_no_shell_arg(tmp_path: object) -> None:
    """Belt-and-suspenders: the installer launch must NOT use ``shell=True``.
    Inno Setup .exes need argv-style invocation; shell=True on Windows
    runs cmd.exe which mangles flag quoting and is a security smell."""

    fake_exe = tmp_path / "OllamaSetup.exe"  # type: ignore[operator]
    fake_exe.write_bytes(b"MZ")

    with patch.object(
        setup_local_mod.subprocess, "call", return_value=0
    ) as scall, patch.object(setup_local_mod, "_prepend_to_process_path"):
        install_ollama_windows(auto_confirm=True, installer_path=str(fake_exe))

    _, kwargs = scall.call_args
    # The current implementation simply omits `shell=`. Either way: not True.
    assert kwargs.get("shell") in (None, False)


def test_install_ollama_windows_passes_custom_download_url(
    tmp_path: object,
) -> None:
    """A custom ``download_url`` (mirror / pinned version) must be the
    URL we fetch — not the default URL."""

    fake_body = io.BytesIO(b"MZ")
    seen: dict[str, str] = {}

    def fake_urlopen(req: object, *args: object, **kwargs: object) -> object:
        seen["url"] = getattr(req, "full_url", str(req))
        ctx = object()
        # mimic context manager + read()
        class _R:
            def __enter__(self_inner: object) -> object: return self_inner
            def __exit__(self_inner: object, *a: object) -> bool: return False
            def read(self_inner: object, n: int = -1) -> bytes: return fake_body.read(n)
        return _R()

    custom = "https://mirror.example/OllamaSetup-pinned.exe"
    with patch.object(
        setup_local_mod.urllib.request, "urlopen", side_effect=fake_urlopen
    ), patch.object(
        setup_local_mod.subprocess, "call", return_value=0
    ), patch.object(setup_local_mod, "_prepend_to_process_path"):
        rc = install_ollama_windows(auto_confirm=True, download_url=custom)

    assert rc == 0
    assert seen["url"] == custom


def test_prepend_to_process_path_handles_missing_dir(tmp_path: object) -> None:
    """Pointing at a non-existent install dir must be a no-op, not raise.
    This matters because OllamaSetup.exe accepts a ``/DIR=`` flag and we
    can't know the user's choice."""

    before = os.environ.get("PATH", "")
    setup_local_mod._prepend_to_process_path(
        str(tmp_path / "does-not-exist")  # type: ignore[operator]
    )
    after = os.environ.get("PATH", "")
    assert before == after


def test_prepend_to_process_path_prepends_when_dir_exists(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path for the PATH augmentation: the install dir lands at the
    *front* of PATH so ``shutil.which`` returns the freshly-installed
    binary, not an older one lingering on PATH from a prior session."""

    # Create a real directory so the function doesn't bail early.
    real_dir = tmp_path / "Ollama"  # type: ignore[operator]
    real_dir.mkdir()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    setup_local_mod._prepend_to_process_path(str(real_dir))
    new_path = os.environ["PATH"]
    assert new_path.split(os.pathsep)[0] == str(real_dir)
    # Old entries preserved.
    assert "/usr/bin" in new_path.split(os.pathsep)


def test_prepend_to_process_path_is_idempotent(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling twice doesn't pile the same dir onto PATH twice — keeps
    PATH from growing unboundedly across repeat setup runs."""

    real_dir = tmp_path / "Ollama"  # type: ignore[operator]
    real_dir.mkdir()
    monkeypatch.setenv("PATH", str(real_dir) + os.pathsep + "/usr/bin")
    before = os.environ["PATH"]
    setup_local_mod._prepend_to_process_path(str(real_dir))
    assert os.environ["PATH"] == before


def test_run_setup_local_uses_windows_installer_on_win32() -> None:
    """End-to-end wiring: ``run_setup_local(install_ollama_if_missing=True)``
    on Windows must call ``install_ollama_windows``, not the Linux script."""

    with patch.object(setup_local_mod.sys, "platform", "win32"), patch.object(
        setup_local_mod, "is_ollama_installed", side_effect=[False, True]
    ), patch.object(
        setup_local_mod, "install_ollama_windows", return_value=0
    ) as winstall, patch.object(
        setup_local_mod, "is_ollama_running", return_value=True
    ), patch.object(
        setup_local_mod, "pull_model", return_value=0
    ), patch.object(setup_local_mod, "smoke_test", return_value=True):
        rc = run_setup_local(install_ollama_if_missing=True)
    assert rc == 0
    winstall.assert_called_once()
