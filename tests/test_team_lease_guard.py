"""A granted claim must not already be expired.

``claim`` rejected a non-positive ``lease_seconds``; ``claim_next`` and
``heartbeat`` computed the same value and skipped the check. Either wrote
``lease_expires <= claimed_at`` — a claim expired the instant it was granted —
so the next peer reclaimed work that was still in flight, and a peer that
heartbeat with a bad lease evicted *itself* while still working.

It degraded to a loud ``TaskClaimConflictError`` rather than silent duplicate
work, which is why it was survivable; it was still a lease that lied.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mantis_agent.teams.tasks import TaskStore


def _store(tmp: str) -> TaskStore:
    return TaskStore(str(Path(tmp) / "tasks.db"))


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_no_entry_point_grants_an_already_expired_lease(bad) -> None:
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        st.create_task("team:1", "A", task_id="a")

        with pytest.raises(ValueError, match="lease_seconds must be positive"):
            st.claim("a", "peer:one", lease_seconds=bad)
        with pytest.raises(ValueError, match="lease_seconds must be positive"):
            st.claim_next("team:1", "peer:one", lease_seconds=bad)
        with pytest.raises(ValueError, match="lease_seconds must be positive"):
            st.heartbeat("a", "peer:one", lease_seconds=bad)


def test_a_healthy_lease_is_unaffected() -> None:
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        st.create_task("team:1", "A", task_id="a")
        st.claim("a", "peer:one", lease_seconds=30)
        assert st.get("a").assignee == "peer:one"
        # ...and the holder can still extend it.
        assert st.heartbeat("a", "peer:one", lease_seconds=30) > 0
