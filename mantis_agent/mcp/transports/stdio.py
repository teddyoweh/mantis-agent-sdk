"""Stdio transport — spawn a local subprocess, talk JSON-RPC over its stdin/stdout.

This is the dominant MCP transport for filesystem servers, git servers,
Slack servers, etc. — anything the user installs locally. Framing is
line-delimited JSON: one complete JSON-RPC message per ``\\n``-terminated
line on each direction.

Implementation notes
--------------------
* ``anyio.open_process`` returns an ``anyio.abc.Process`` whose ``stdin`` /
  ``stdout`` are ``ByteSendStream`` / ``ByteReceiveStream``.
* We use a ``BufferedByteReceiveStream`` so we can pull line-at-a-time
  without re-implementing newline scanning.
* stderr is read in the background to a list (kept small) so a chatty
  server doesn't deadlock by filling its stderr pipe.
"""

from __future__ import annotations

import os
from typing import Any

import anyio
import msgspec
from anyio.abc import Process
from anyio.streams.buffered import BufferedByteReceiveStream

from .base import TransportClosed


_DECODER = msgspec.json.Decoder()
_ENCODER = msgspec.json.Encoder()


class StdioTransport:
    """Spawn ``command args...`` and exchange line-delimited JSON-RPC."""

    __slots__ = (
        "command",
        "args",
        "env",
        "_proc",
        "_stdout",
        "_stderr_task_group",
        "_closed",
        "_send_lock",
    )

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self._proc: Process | None = None
        self._stdout: BufferedByteReceiveStream | None = None
        self._stderr_task_group: anyio.abc.TaskGroup | None = None
        self._closed = False
        self._send_lock = anyio.Lock()

    async def __aenter__(self) -> "StdioTransport":
        await self._spawn()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _spawn(self) -> None:
        # Inherit parent env, layer in caller-provided keys. Many MCP
        # servers need PATH; passing only ``env`` would strip it.
        merged_env = dict(os.environ)
        merged_env.update(self.env)

        self._proc = await anyio.open_process(
            [self.command, *self.args],
            stdin=anyio.subprocess.PIPE,
            stdout=anyio.subprocess.PIPE,
            stderr=anyio.subprocess.PIPE,
            env=merged_env,
        )
        assert self._proc.stdout is not None
        self._stdout = BufferedByteReceiveStream(self._proc.stdout)

        # Drain stderr to avoid the pipe filling up. A future iteration
        # could plumb this into a logger; v0 just discards.
        async def _drain_stderr() -> None:
            stderr = self._proc.stderr if self._proc else None
            if stderr is None:
                return
            try:
                async for _ in stderr:
                    pass
            except Exception:  # noqa: BLE001 — stderr is best-effort
                pass

        # Spawn a detached task. We don't enter a TaskGroup here because
        # the transport lifecycle is bounded by the user's MCPClient
        # context, not by an enclosing TG.
        self._stderr_task_group = anyio.create_task_group()
        await self._stderr_task_group.__aenter__()
        self._stderr_task_group.start_soon(_drain_stderr)

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise TransportClosed("stdio transport is closed")
        payload = _ENCODER.encode(message) + b"\n"
        async with self._send_lock:
            try:
                await self._proc.stdin.send(payload)
            except (BrokenPipeError, anyio.ClosedResourceError) as e:
                raise TransportClosed("stdio peer closed stdin") from e

    async def receive(self) -> dict[str, Any]:
        if self._closed or self._stdout is None:
            raise TransportClosed("stdio transport is closed")
        try:
            line = await self._stdout.receive_until(b"\n", max_bytes=16 * 1024 * 1024)
        except (anyio.EndOfStream, anyio.ClosedResourceError) as e:
            raise TransportClosed("stdio peer closed stdout") from e
        if not line:
            # Empty line — keep reading. Some servers emit blank framing.
            return await self.receive()
        return _DECODER.decode(line)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        # Close stdin to signal EOF, then give the process a moment to
        # exit cleanly before we terminate.
        if proc.stdin is not None:
            try:
                await proc.stdin.aclose()
            except Exception:  # noqa: BLE001
                pass
        try:
            with anyio.fail_after(2.0):
                await proc.wait()
        except TimeoutError:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        if self._stderr_task_group is not None:
            try:
                await self._stderr_task_group.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass
