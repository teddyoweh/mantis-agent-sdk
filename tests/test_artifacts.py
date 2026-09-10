import hashlib
import json
import os
import stat

import anyio
import pytest

from mantis_agent.artifacts import ArtifactStore, MAX_READ_CHARS, MAX_SEARCH_CHARS, make_artifact_tool
from mantis_agent.tools import ToolRegistry, dispatch_tool_calls
from mantis_agent.types import ToolUseBlock


def test_durable_deduplicated_private_store(tmp_path):
    store = ArtifactStore(tmp_path / "private")
    id = store.put_text("exact\r\nα")
    assert id == hashlib.sha256("exact\r\nα".encode()).hexdigest()
    assert store.put_text("exact\r\nα") == id
    assert len(list(store.root.iterdir())) == 1
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / id).stat().st_mode) == 0o600
    assert ArtifactStore(store.root).read(id)["content"] == "exact\r\nα"
    assert store.put_json({"b": 2, "a": 1}) == store.put_json({"a": 1, "b": 2})


@pytest.mark.parametrize("id", ["../secret", "/etc/passwd", "a" * 63, "A" * 64,
                                "f" * 64 + "\n", "f" * 64 + "/x", "", None])
def test_invalid_id(tmp_path, id):
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "store").read(id)


def test_symlink_and_hardlink_rejected(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    outside = tmp_path / "secret"
    outside.write_text("secret")
    id = "a" * 64
    (store.root / id).symlink_to(outside)
    with pytest.raises(OSError):
        store.read(id)
    (store.root / id).unlink()
    os.link(outside, store.root / id)
    with pytest.raises(ValueError):
        store.read(id)
    link = tmp_path / "linked-root"
    link.symlink_to(store.root)
    with pytest.raises(OSError):
        ArtifactStore(link)


def test_bounds_and_search_paging(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    text = "x" * (MAX_SEARCH_CHARS - 2) + "needle" + "z" * MAX_READ_CHARS
    id = store.put_text(text)
    assert len(store.read(id, limit=10**9)["content"]) == MAX_READ_CHARS
    first = store.read(id, query="needle")
    assert not first["matched"] and not first["eof"]
    second = store.read(id, first["next_offset"], 6, "needle")
    assert second["matched"] and second["content"] == "needle"
    assert second["offset"] == MAX_SEARCH_CHARS - 2
    assert store.read(id, len(text) + 100)["eof"]
    for kwargs in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 1.5},
                   {"query": ""}, {"query": "x" * 513}):
        with pytest.raises(ValueError):
            store.read(id, **kwargs)


def test_atomic_failure_keeps_prior_artifact(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "store")
    id = store.put_text("safe")
    def fail(*args, **kwargs):
        raise OSError("disk failed")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.put_text("new")
    assert store.read(id)["content"] == "safe"
    assert [p.name for p in store.root.iterdir()] == [id]


def test_registry_tool(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    id = store.put_text("abcdef")
    tool = make_artifact_tool(store)
    assert tool.name == "read_artifact" and tool.is_read_only
    registry = ToolRegistry()
    registry.add(tool)
    result = anyio.run(dispatch_tool_calls, registry, [ToolUseBlock(
        id="call", name="read_artifact", input={"id": id, "offset": 2, "limit": 3})])[0]
    assert not result.is_error
    assert json.loads(result.content)["content"] == "cde"
