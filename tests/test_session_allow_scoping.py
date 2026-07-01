"""'Allow for session' scoping — approving an edit to a file covers further edits
to THAT file (keyed by path, not the exact old_string/new_string)."""

from __future__ import annotations

from types import SimpleNamespace

import anyio

from mantis_agent.permissions import (
    Allow,
    PermissionContext,
    _session_key,
    check_permission,
)
from mantis_agent.tools import tool


def test_edit_tools_keyed_by_path() -> None:
    edit = SimpleNamespace(name="edit_file")
    k_a1 = _session_key(edit, {"path": "foo.py", "old_string": "a", "new_string": "b"})
    k_a2 = _session_key(edit, {"path": "foo.py", "old_string": "x", "new_string": "y"})
    k_b = _session_key(edit, {"path": "bar.py", "old_string": "a", "new_string": "b"})
    assert k_a1 == k_a2          # same file, different edits → same key
    assert k_a1 != k_b           # different file → different key


def test_write_and_notebook_keyed_by_path() -> None:
    w = SimpleNamespace(name="write_file")
    assert _session_key(w, {"path": "x", "content": "1"}) == _session_key(w, {"path": "x", "content": "2"})
    nb = SimpleNamespace(name="notebook_edit")
    assert _session_key(nb, {"notebook_path": "n.ipynb", "cell_number": 1}) \
        == _session_key(nb, {"notebook_path": "n.ipynb", "cell_number": 9})


def test_bash_stays_exact() -> None:
    b = SimpleNamespace(name="bash")
    assert _session_key(b, {"command": "npm test"}) == _session_key(b, {"command": "npm test"})
    assert _session_key(b, {"command": "npm test"}) != _session_key(b, {"command": "npm run build"})


@tool(name="edit_file", is_read_only=False)
async def _edit(path: str, old_string: str = "", new_string: str = "") -> str:
    return "ok"


def test_end_to_end_session_allow_covers_same_file() -> None:
    async def main():
        asked: list = []

        async def asker(t, inp, _prompt):
            asked.append(inp.get("path"))
            return "allow_session"

        ctx = PermissionContext(mode="default", asker=asker)
        d1 = await check_permission(_edit, {"path": "foo.py", "old_string": "a", "new_string": "b"}, ctx)
        d2 = await check_permission(_edit, {"path": "foo.py", "old_string": "q", "new_string": "z"}, ctx)
        d3 = await check_permission(_edit, {"path": "bar.py", "old_string": "a", "new_string": "b"}, ctx)
        return asked, (d1, d2, d3)

    asked, decisions = anyio.run(main)
    # foo.py asked once; the SECOND foo.py edit was NOT re-prompted; bar.py asked.
    assert asked == ["foo.py", "bar.py"]
    assert all(isinstance(d, Allow) for d in decisions)
