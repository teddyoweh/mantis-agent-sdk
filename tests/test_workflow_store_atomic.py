"""Crash-safety of the durable workflow run store.

A run record is the only surviving artifact of an expensive workflow, so the
write that produces it must be all-or-nothing: a crash, a full disk or a reader
racing the writer must never turn a good record into a truncated one. These
tests drive ``save_run`` through a simulated mid-write failure and assert the
previously persisted record is still there afterwards, plus the reader-side
guards (``load_record`` / ``list_runs``) that keep one bad file from taking down
the whole history view.

MANTIS_AGENT_HOME is redirected at tmp_path, so every artifact lands in the
sandbox.
"""

from __future__ import annotations

import builtins
import io
import json
from pathlib import Path
from typing import Any

import pytest

from mantis_agent.workflow_store import (
    RECORD_VERSION,
    list_runs,
    load_record,
    run_path,
    runs_dir,
    save_run,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANTIS_AGENT_HOME", str(tmp_path / "home"))
    yield


def _run(run_id: str = "r1", status: str = "done") -> dict[str, Any]:
    """A minimal run snapshot shaped like ``WorkflowRun.to_dict()``."""

    return {
        "id": run_id,
        "name": "demo run",
        "status": status,
        "phases": [{"title": "Scan", "agents": [
            {"label": "a", "status": "done", "phase": "Scan",
             "prompt": "scan it", "result": "found things"},
        ]}],
    }


# -- the crash simulator -------------------------------------------------
#
# Both the naive (``Path.write_text``) and the atomic (temp file + replace)
# implementation ultimately open a file for writing and call ``.write`` on it,
# so intercepting the open is the one seam that is fair to both: whichever file
# the writer picks gets truncated halfway through, exactly as a full disk or a
# SIGKILL would leave it.

class _HalfWriteFile:
    """A file object that writes half the payload, then fails like ENOSPC."""

    def __init__(self, fh: Any) -> None:
        self._fh = fh

    def write(self, data: Any) -> int:
        half = data[: max(1, len(data) // 2)]
        self._fh.write(half)
        self._fh.flush()
        raise OSError(28, "No space left on device")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fh, name)

    def __enter__(self) -> "_HalfWriteFile":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._fh.close()
        return False


def _install_half_writer(monkeypatch, directory: Path) -> None:
    """Make every write-mode open under ``directory`` fail halfway through."""

    real_io_open = io.open
    real_builtin_open = builtins.open

    def _targets(file: Any) -> bool:
        try:
            return Path(file).parent == directory
        except TypeError:  # a raw fd — never ours
            return False

    def _wrap(real):
        def _opener(file, *args, **kwargs):
            fh = real(file, *args, **kwargs)
            mode = kwargs.get("mode", args[0] if args else "r")
            if "w" in str(mode) and _targets(file):
                return _HalfWriteFile(fh)
            return fh
        return _opener

    monkeypatch.setattr(io, "open", _wrap(real_io_open))
    monkeypatch.setattr(builtins, "open", _wrap(real_builtin_open))


# -- tests ---------------------------------------------------------------

def test_save_run_leaves_no_temp_file_behind():
    path = Path(save_run(_run(), definition="demo"))
    assert path.is_file()
    leftovers = [p.name for p in runs_dir().iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_crash_mid_write_preserves_previous_record(monkeypatch):
    # A good record lands first — this is the one an interrupted save must not
    # destroy.
    save_run(_run(status="done"), definition="demo")
    before = load_record("r1")
    assert before is not None and before["status"] == "done"

    with monkeypatch.context() as m:
        _install_half_writer(m, runs_dir())
        with pytest.raises(OSError):
            save_run(_run(status="running"), definition="demo")

    after = load_record("r1")
    assert after is not None, "a failed save destroyed the previous record"
    assert after["status"] == "done"
    assert json.loads(run_path("r1").read_text(encoding="utf-8"))["version"] == RECORD_VERSION
    assert [p.name for p in runs_dir().iterdir() if ".tmp-" in p.name] == []


def test_load_record_returns_none_for_truncated_json():
    save_run(_run(), definition="demo")
    p = run_path("r1")
    p.write_text(p.read_text(encoding="utf-8")[:40], encoding="utf-8")
    assert load_record("r1") is None

    p.write_text("\x00 not json at all", encoding="utf-8")
    assert load_record("r1") is None

    p.write_text('["a list, not a record"]', encoding="utf-8")
    assert load_record("r1") is None


def test_list_runs_skips_temp_and_corrupt_files():
    save_run(_run("good"), definition="demo")
    d = runs_dir()
    (d / "corrupt.json").write_text('{"run_id": "corrupt", "run"', encoding="utf-8")
    # An in-flight temp file, fully serialized but not yet renamed: valid JSON,
    # so only a name check keeps it out of the history listing.
    payload = (d / "good.json").read_text(encoding="utf-8")
    (d / "good.json.tmp-99999").write_text(payload, encoding="utf-8")
    (d / "other.tmp-99999.json").write_text(payload, encoding="utf-8")

    rows = list_runs()
    assert [r["run_id"] for r in rows] == ["good"]


def test_round_trip_still_redacts_and_loads():
    save_run(
        _run("r2"),
        definition="demo",
        inputs={"target": "acme", "api_key": "sk-live-123", "nested": {"a": 1}},
        job_id=7,
        result={"replayed": 2, "cost_usd": 0.5, "phases": [{"title": "Scan"}]},
    )
    rec = load_record("r2")
    assert rec is not None
    assert rec["version"] == RECORD_VERSION
    assert rec["job_id"] == 7
    assert rec["inputs"] == {
        "target": "acme", "api_key": "[redacted]", "nested": '{"a": 1}',
    }
    assert "sk-live-123" not in run_path("r2").read_text(encoding="utf-8")
    assert rec["summary"] == {"replayed": 2, "cost_usd": 0.5, "phases": ["Scan"]}
    assert rec["run"]["phases"][0]["agents"][0]["result"] == "found things"
