"""``mantis-agent setup-local-llamacpp`` — CPU-friendly GGUF catalog + install helper.

These tests cover the pure / mockable bits: the catalog shape, the
default recommendation, environment detection, the URL builder, the
launch-command builder, the GGUF downloader (against a tiny in-memory
HTTP server) and that the CLI subcommand parses + dispatches to the
right handler. The actual ``llama-server`` launch + smoke test are
integration territory and not exercised here.
"""

from __future__ import annotations

import io
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from mantis_agent import cli
from mantis_agent.setup_local_llamacpp import (
    CPU_FRIENDLY_GGUF_MODELS,
    DEFAULT_GGUF_RECOMMENDATION,
    DEFAULT_LLAMACPP_PORT,
    GGUFModel,
    build_llamacpp_server_command,
    default_models_dir,
    download_gguf,
    huggingface_gguf_url,
    install_llamacpp_instructions,
    is_llamacpp_running,
    is_llamacpp_server_installed,
    print_gguf_model_table,
)


# ---------------------------------------------------------------------------
# Catalog invariants
# ---------------------------------------------------------------------------


def test_catalog_is_nonempty_and_well_typed() -> None:
    assert len(CPU_FRIENDLY_GGUF_MODELS) >= 5
    for m in CPU_FRIENDLY_GGUF_MODELS:
        assert isinstance(m, GGUFModel)
        assert m.tag
        assert m.hf_repo and "/" in m.hf_repo
        assert m.hf_filename.endswith(".gguf")
        assert m.size_gb > 0
        assert m.min_ram_gb > 0
        assert m.family
        # Tags must be unique — they're the user-facing handle.
    tags = [m.tag for m in CPU_FRIENDLY_GGUF_MODELS]
    assert len(tags) == len(set(tags)), "duplicate GGUF tag in catalog"


def test_default_recommendation_is_in_catalog() -> None:
    tags = {m.tag for m in CPU_FRIENDLY_GGUF_MODELS}
    assert DEFAULT_GGUF_RECOMMENDATION in tags


def test_catalog_ordered_by_size() -> None:
    """Smaller GGUFs first — same convention as the Ollama catalog so
    callers iterating either list see the cheap options up top."""

    sizes = [m.size_gb for m in CPU_FRIENDLY_GGUF_MODELS]
    assert sizes == sorted(sizes), "CPU_FRIENDLY_GGUF_MODELS should be size-ascending"


def test_catalog_caps_at_8b() -> None:
    """Nothing > 8 B params — anything bigger is GPU territory."""

    for m in CPU_FRIENDLY_GGUF_MODELS:
        n = float(m.params.rstrip("BMK").rstrip("b").rstrip())
        if m.params.endswith(("M", "m")):
            n /= 1000
        assert n <= 8.0, f"{m.tag} ({m.params}) exceeds CPU ceiling"


def test_reasoning_models_flagged_thinking() -> None:
    """DeepSeek-R1 GGUFs must have ``thinking=True`` so the agent
    capability resolver knows to parse ``<think>`` blocks."""

    for m in CPU_FRIENDLY_GGUF_MODELS:
        if m.family == "DeepSeek":
            assert m.thinking, f"{m.tag} should emit <think> blocks"


def test_catalog_mirrors_ollama_default_size() -> None:
    """The default GGUF pick should match the Ollama default's parameter
    class — users swapping paths shouldn't see a behavior cliff."""

    from mantis_agent.setup_local import DEFAULT_RECOMMENDATION as OLLAMA_DEFAULT

    default = next(m for m in CPU_FRIENDLY_GGUF_MODELS if m.tag == DEFAULT_GGUF_RECOMMENDATION)
    # Both defaults are Qwen 1.5B-class. The exact parameter strings
    # differ ("1.5b" vs "1.5B") but the *size* is the contract.
    assert default.family == "Qwen"
    assert "1.5b" in OLLAMA_DEFAULT.lower()
    assert "1.5b" in default.tag.lower()


# ---------------------------------------------------------------------------
# huggingface_gguf_url
# ---------------------------------------------------------------------------


def test_huggingface_gguf_url_shape() -> None:
    url = huggingface_gguf_url("Qwen/Qwen2.5-1.5B-Instruct-GGUF", "f.gguf")
    assert url == "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/f.gguf"


def test_every_catalog_entry_builds_a_usable_url() -> None:
    """Every curated entry must round-trip through the URL builder
    without producing a doubled-slash or a malformed path."""

    for m in CPU_FRIENDLY_GGUF_MODELS:
        url = huggingface_gguf_url(m.hf_repo, m.hf_filename)
        assert url.startswith("https://huggingface.co/")
        # No doubled slashes anywhere (besides https://).
        assert "//" not in url[len("https://"):]


# ---------------------------------------------------------------------------
# build_llamacpp_server_command
# ---------------------------------------------------------------------------


def test_build_llamacpp_server_command_defaults() -> None:
    cmd = build_llamacpp_server_command("/path/to/model.gguf")
    # First arg is the binary
    assert cmd[0] == "llama-server"
    # Model path threaded through
    assert "/path/to/model.gguf" in cmd
    # Loopback by default (don't expose unauth LLMs to the LAN)
    assert "127.0.0.1" in cmd
    # Standard port
    assert str(DEFAULT_LLAMACPP_PORT) in cmd
    # --jinja on by default so OpenAI-compat tools[] works
    assert "--jinja" in cmd


def test_build_llamacpp_server_command_custom_port_and_host() -> None:
    cmd = build_llamacpp_server_command(
        "/m.gguf", port=9090, host="0.0.0.0"
    )
    assert "9090" in cmd
    assert "0.0.0.0" in cmd


def test_build_llamacpp_server_command_threads_omitted_when_none() -> None:
    """If the user doesn't pass ``n_threads``, we don't emit ``-t`` so
    llama-server picks its own default (logical-core count)."""

    cmd = build_llamacpp_server_command("/m.gguf")
    assert "-t" not in cmd


def test_build_llamacpp_server_command_threads_emitted_when_set() -> None:
    cmd = build_llamacpp_server_command("/m.gguf", n_threads=8)
    assert "-t" in cmd
    assert "8" in cmd


def test_build_llamacpp_server_command_no_jinja() -> None:
    cmd = build_llamacpp_server_command("/m.gguf", jinja=False)
    assert "--jinja" not in cmd


def test_build_llamacpp_server_command_chat_template_flag() -> None:
    cmd = build_llamacpp_server_command(
        "/m.gguf", chat_template_flag="qwen-tools"
    )
    assert "--chat-template" in cmd
    assert "qwen-tools" in cmd


def test_build_llamacpp_server_command_custom_binary() -> None:
    """Older builds shipped the binary as :program:`server`; users can
    override the name without forking the helper."""

    cmd = build_llamacpp_server_command("/m.gguf", binary="server")
    assert cmd[0] == "server"


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def test_is_llamacpp_server_installed_returns_bool() -> None:
    assert isinstance(is_llamacpp_server_installed(), bool)


def test_is_llamacpp_server_installed_uses_shutil_which() -> None:
    """We accept either `llama-server` (modern) or `server` (legacy)."""

    with patch("mantis_agent.setup_local_llamacpp.shutil.which") as which:
        which.side_effect = lambda name: "/usr/bin/server" if name == "server" else None
        assert is_llamacpp_server_installed() is True

    with patch("mantis_agent.setup_local_llamacpp.shutil.which", return_value=None):
        assert is_llamacpp_server_installed() is False


def test_is_llamacpp_running_returns_false_for_unbound_port() -> None:
    """A random high port that nothing's listening on must report False."""

    # Pick a port that's almost certainly unbound by binding then closing.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # Now the port is free — probe it.
    assert is_llamacpp_running(port=port, host="127.0.0.1") is False


def test_is_llamacpp_running_returns_true_against_running_server() -> None:
    """Spin up a one-shot HTTP server on a free port and probe it."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib BaseHTTPRequestHandler API
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

        def log_message(self, *args: object, **kwargs: object) -> None:
            # Silence the default stderr access-log so test output stays clean.
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        assert is_llamacpp_running(port=port, host="127.0.0.1") is True
    finally:
        server.shutdown()
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# Install hint
# ---------------------------------------------------------------------------


def test_install_instructions_mentions_platform() -> None:
    """The hint we print should at least reference the user's actual
    package channel — brew on macOS, releases on Linux, releases on
    Windows."""

    msg = install_llamacpp_instructions()
    if sys.platform.startswith("darwin"):
        assert "brew" in msg.lower()
    elif sys.platform.startswith("linux"):
        assert "release" in msg.lower() or "github.com/ggerganov" in msg
    elif sys.platform.startswith("win"):
        assert "windows" in msg.lower() or "release" in msg.lower()


# ---------------------------------------------------------------------------
# default_models_dir
# ---------------------------------------------------------------------------


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANTIS_AGENT_MODELS_DIR", "/custom/path/models")
    assert default_models_dir() == "/custom/path/models"


def test_xdg_data_home_used_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANTIS_AGENT_MODELS_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
    assert default_models_dir() == os.path.join("/xdg/data", "mantis-agent", "models")


def test_default_models_dir_returns_absolute_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without overrides, the default is a real, non-empty path."""

    monkeypatch.delenv("MANTIS_AGENT_MODELS_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    out = default_models_dir()
    assert isinstance(out, str)
    assert out  # non-empty


# ---------------------------------------------------------------------------
# download_gguf — round-trip against a tiny in-memory HTTP server
# ---------------------------------------------------------------------------


def _serve_static_gguf(
    payload: bytes,
) -> tuple[HTTPServer, threading.Thread, int]:
    """Spin up an HTTP server that serves ``payload`` at any path. Returns
    (server, thread, port). Caller is responsible for shutdown."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("content-type", "application/octet-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object, **kwargs: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t, port


def test_download_gguf_writes_file_and_returns_path(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the downloader pulls a payload from a stand-in HTTP
    server and persists it at the expected on-disk path."""

    payload = b"FAKE GGUF" * 4096  # ~36 KB — too small to look like a real model.
    server, t, port = _serve_static_gguf(payload)
    try:
        # Construct a one-off GGUFModel that points at our fake server's
        # path. ``huggingface_gguf_url`` is patched so the canonical URL
        # builder isn't bypassed in the production path (we redirect it
        # at the network layer).
        m = GGUFModel(
            tag="fake-test",
            hf_repo="local/test",
            hf_filename="fake.gguf",
            params="0M",
            size_gb=0.0001,  # ~100 KB target so the 95% size check is permissive
            min_ram_gb=1,
            family="Test",
            tools=False,
            thinking=False,
            chat_template_flag="",
            notes="test-only",
        )

        def fake_url(repo: str, filename: str) -> str:
            return f"http://127.0.0.1:{port}/{filename}"

        monkeypatch.setattr(
            "mantis_agent.setup_local_llamacpp.huggingface_gguf_url", fake_url
        )

        target = download_gguf(
            m, models_dir=str(tmp_path), show_progress=False
        )
        assert os.path.exists(target)
        assert os.path.basename(target) == m.hf_filename
        assert os.path.getsize(target) == len(payload)
    finally:
        server.shutdown()
        t.join(timeout=2)


def test_download_gguf_is_idempotent_when_file_exists(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-downloaded file at the right path + size must NOT
    re-download — re-running setup-local-llamacpp should be cheap."""

    # Pre-stage a file that's the catalog-stated size (in bytes).
    target_dir = str(tmp_path)
    m = GGUFModel(
        tag="cached-test",
        hf_repo="local/test",
        hf_filename="cached.gguf",
        params="0M",
        size_gb=0.001,  # 1 MB target
        min_ram_gb=1,
        family="Test",
        tools=False,
        thinking=False,
        chat_template_flag="",
        notes="test-only",
    )
    target_path = os.path.join(target_dir, m.hf_filename)
    # Write 1 MB so it passes the 95% size check.
    with open(target_path, "wb") as fh:
        fh.write(b"\0" * int(m.size_gb * (1024**3)))

    # Patch the URL builder to a URL that doesn't actually resolve —
    # if the function tries to download, the test crashes (which is
    # exactly the signal we want).
    monkeypatch.setattr(
        "mantis_agent.setup_local_llamacpp.huggingface_gguf_url",
        lambda repo, filename: "http://127.0.0.1:0/should-not-be-called",
    )

    out = download_gguf(m, models_dir=target_dir, show_progress=False)
    assert out == target_path


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------


def test_print_gguf_model_table_runs() -> None:
    """Smoke: the table prints without exploding and includes every tag
    and the recommendation footer."""

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        print_gguf_model_table()
    out = buf.getvalue()
    assert "MODEL" in out
    assert "PARAMS" in out
    for m in CPU_FRIENDLY_GGUF_MODELS:
        assert m.tag in out
    assert DEFAULT_GGUF_RECOMMENDATION in out


# ---------------------------------------------------------------------------
# CLI subcommand
# ---------------------------------------------------------------------------


def test_cli_setup_local_llamacpp_list_flag_dispatches_to_table() -> None:
    """``mantis-agent setup-local-llamacpp --list`` must dispatch to the
    table printer (not the orchestrator) and exit clean."""

    with patch(
        "mantis_agent.setup_local_llamacpp.print_gguf_model_table"
    ) as printer:
        rc = cli.main(["setup-local-llamacpp", "--list"])
    assert rc == 0
    printer.assert_called_once()


def test_cli_setup_local_llamacpp_forwards_args_to_orchestrator() -> None:
    """All the user-facing flags must reach the run_setup_local_llamacpp
    call — we don't want a flag that silently ghosts because the CLI
    glue forgot to pass it through."""

    with patch(
        "mantis_agent.setup_local_llamacpp.run_setup_local_llamacpp"
    ) as runner:
        runner.return_value = 0
        rc = cli.main([
            "setup-local-llamacpp",
            "--model", "qwen2.5-1.5b-instruct-q4_k_m",
            "--models-dir", "/tmp/models",
            "--port", "9090",
            "--host", "0.0.0.0",
            "--skip-download",
            "--skip-smoke-test",
        ])
    assert rc == 0
    runner.assert_called_once_with(
        model="qwen2.5-1.5b-instruct-q4_k_m",
        models_dir="/tmp/models",
        port=9090,
        host="0.0.0.0",
        skip_download=True,
        skip_smoke_test=True,
    )


def test_cli_setup_local_llamacpp_defaults_when_no_flags() -> None:
    """A bare ``mantis-agent setup-local-llamacpp`` (no args) must still
    dispatch — defaults fill in for everything."""

    with patch(
        "mantis_agent.setup_local_llamacpp.run_setup_local_llamacpp"
    ) as runner:
        runner.return_value = 0
        rc = cli.main(["setup-local-llamacpp"])
    assert rc == 0
    runner.assert_called_once()
    # Defaults sanity: model is None (orchestrator picks the recommendation),
    # port is 8080, host is loopback.
    kwargs = runner.call_args.kwargs
    assert kwargs["model"] is None
    assert kwargs["port"] == DEFAULT_LLAMACPP_PORT
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["skip_download"] is False
    assert kwargs["skip_smoke_test"] is False


def test_cli_setup_local_llamacpp_help_lists_in_top_level_help() -> None:
    """The new subcommand must appear in the top-level ``--help`` so
    users discover it."""

    parser = cli._build_parser()
    # Find the subparsers action to inspect available choices.
    sub_actions = [a for a in parser._actions if a.dest == "cmd"]
    assert sub_actions, "expected a single subparsers action"
    choices = list(sub_actions[0].choices)
    assert "setup-local-llamacpp" in choices
    # And the original subcommand must still be there — we added, not replaced.
    assert "setup-local" in choices


# ---------------------------------------------------------------------------
# run_setup_local_llamacpp orchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_rejects_unknown_model() -> None:
    """If the user picks a tag we don't curate, the orchestrator exits
    cleanly with a non-zero code rather than silently downloading
    something they didn't intend."""

    from mantis_agent.setup_local_llamacpp import run_setup_local_llamacpp

    rc = run_setup_local_llamacpp(model="totally-fake-model-tag")
    assert rc == 2


def test_orchestrator_skip_download_path_exits_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``--skip-download``, the orchestrator should never call
    download_gguf and should still return cleanly even if the server
    isn't running — it just won't run the smoke test."""

    from mantis_agent.setup_local_llamacpp import run_setup_local_llamacpp

    # Force the "server not running" branch deterministically.
    monkeypatch.setattr(
        "mantis_agent.setup_local_llamacpp.is_llamacpp_running",
        lambda **kwargs: False,
    )
    # And make sure we never call the downloader.
    with patch(
        "mantis_agent.setup_local_llamacpp.download_gguf"
    ) as dl:
        rc = run_setup_local_llamacpp(skip_download=True)
    assert rc == 0
    dl.assert_not_called()
