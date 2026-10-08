"""Steering — add a message to a RUNNING agent's next turn.

A steer is not an interrupt. Nothing is cancelled: the model call in flight
finishes, the tool calls it asked for finish, and the NEXT request carries the
steering text as a user message. That is the whole contract, and it is what
:meth:`mantis_agent.Agent.steer` (and the run/client-level ``steer()`` built on
it) promise.

This module holds the two pieces the engine and the ``task`` tool share:

* :class:`SteerInbox` — a thread-safe FIFO owned by one :class:`Agent`. It is
  *open* only while that agent's ``run_iter`` is live; ``put`` on a closed
  inbox returns ``False`` and keeps nothing, so a steer can never silently
  leak into some later run. The loop drains it at the turn boundary.
* :data:`ACTIVE_STEER` — a ContextVar the run loop sets to its inbox for the
  duration of the run, so a tool body (the ``task`` tool) can notice that the
  user steered while it was blocking the turn and step out of the way by
  moving its subagent to the background.

Stdlib only: imported by ``agent.py`` at module load.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import Optional

__all__ = ["ACTIVE_STEER", "SteerInbox", "current_steer_inbox", "steer_framing"]


class SteerInbox:
    """Steering messages waiting for one agent's next turn.

    ``put`` may be called from any thread or task; ``take`` / ``take_or_close``
    / ``wait`` are called by the run loop and its tools on the loop thread.
    An ``asyncio.Event`` mirrors "something is pending" so a blocked tool can
    wake on it; the event is only ever touched on the loop thread (a ``put``
    from another thread hops over with ``call_soon_threadsafe``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[str] = []
        self._open = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event: Optional[asyncio.Event] = None

    # -- lifecycle (the run loop) -------------------------------------------

    def open(self) -> None:
        """Start accepting steers for a run. Called inside the run's loop."""
        try:
            loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:  # not on asyncio (trio): steering still works,
            loop = None       # only the tool-side wake-up is unavailable
        with self._lock:
            self._open = True
            self._items = []
            self._loop = loop
            self._event = asyncio.Event() if loop is not None else None

    def close(self) -> list[str]:
        """Stop accepting steers; return the ones never delivered, in order."""
        with self._lock:
            self._open = False
            left, self._items = self._items, []
            self._loop = None
            self._event = None
        return left

    @property
    def is_open(self) -> bool:
        return self._open

    # -- producers ------------------------------------------------------------

    def put(self, text: str) -> bool:
        """Queue ``text`` for the next turn. ``False`` (and nothing kept) when
        no run is live."""
        with self._lock:
            if not self._open:
                return False
            self._items.append(text)
            loop = self._loop
        self._sync_from_any_thread(loop)
        return True

    # -- consumers (the run loop / tools, on the loop thread) ------------------

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def take(self) -> list[str]:
        """Remove and return everything pending, oldest first."""
        with self._lock:
            items, self._items = self._items, []
        self._sync_event()
        return items

    def take_or_close(self) -> list[str]:
        """Atomically: everything pending, or — when nothing is — close the
        inbox. Used at a final stop, so a steer either lands in another turn
        or is refused by ``put``; there is no window where it is accepted and
        then dropped."""
        with self._lock:
            if self._items:
                items, self._items = self._items, []
            else:
                items = []
                self._open = False
        self._sync_event()
        return items

    async def wait(self) -> None:
        """Return once a steer is pending. Never returns on a closed inbox or
        off asyncio (the caller races it against its own work)."""
        while True:
            ev = self._event
            if ev is None:
                await asyncio.Event().wait()   # nothing will ever arrive
            if self.pending():
                return
            await ev.wait()
            if not self.pending():
                # A stale set (a take() raced the hop from another thread).
                ev.clear()

    # -- internals ---------------------------------------------------------

    def _sync_event(self) -> None:
        """Make the event say whether anything is pending. Loop thread only."""
        ev = self._event
        if ev is None:
            return
        if self.pending():
            ev.set()
        else:
            ev.clear()

    def _sync_from_any_thread(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._sync_event()
            return
        try:
            loop.call_soon_threadsafe(self._sync_event)
        except RuntimeError:  # loop closed under us: the run is over anyway
            pass


#: The inbox of the agent run the current task belongs to. Set by
#: ``Agent.run_iter`` for the length of the run (like ``TOOL_SCOPE``), so tool
#: bodies dispatched from that run inherit it; ``None`` outside any run.
ACTIVE_STEER: contextvars.ContextVar[Optional[SteerInbox]] = contextvars.ContextVar(
    "mantis_active_steer", default=None
)


def current_steer_inbox() -> Optional[SteerInbox]:
    """The live run's inbox, or ``None`` outside a steerable run."""
    inbox = ACTIVE_STEER.get()
    return inbox if inbox is not None and inbox.is_open else None


def steer_framing() -> str:
    """The one-line reminder that precedes steering text riding in the same
    user message as tool results — so a model reading
    ``<tool_result>…</tool_result>`` followed by prose (the text-channel path
    folds them into one string) knows the prose is the user talking, not more
    tool output."""
    from .system_reminder import wrap_system_reminder  # noqa: PLC0415

    return wrap_system_reminder(
        "The user sent the message below while you were working. It comes from "
        "the user, not from a tool. Take it into account before your next step "
        "and change course if it asks you to."
    )
