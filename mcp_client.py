"""MCP client manager.

Spawns one or more MCP servers as subprocesses and exposes a sync interface
for thread-based callers. Each server runs in a dedicated asyncio task on a
shared background event-loop thread; sessions stay open for the lifetime of
the manager.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("travelmind.mcp")


@dataclass
class _ServerHandle:
    name: str
    session: Optional[ClientSession] = None
    tools: list = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    error: Optional[BaseException] = None


class MCPManager:
    """Owns a background asyncio loop and one keeper task per MCP server."""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._handles: dict[str, _ServerHandle] = {}
        self._ready = threading.Event()
        self._closed = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-loop")
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def close(self) -> None:
        if self._closed or not self._loop:
            return
        self._closed = True

        async def _signal_all():
            for h in self._handles.values():
                h.shutdown.set()

        try:
            asyncio.run_coroutine_threadsafe(_signal_all(), self._loop).result(timeout=5)
        except Exception as e:
            log.warning("mcp close signal failed: %s", e)

        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # ── server registration ────────────────────────────────────────────────

    def add_server(
        self,
        name: str,
        command: str,
        args: list[str],
        env: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Start an MCP server. Blocks until initialized. Returns its tool list.

        Each tool dict: {name, description, input_schema}.
        """
        if not self._loop:
            raise RuntimeError("MCPManager not started")
        if name in self._handles:
            raise ValueError(f"server '{name}' already registered")

        handle = _ServerHandle(name=name)
        self._handles[name] = handle

        params = StdioServerParameters(command=command, args=args, env=env or None)

        async def _schedule():
            asyncio.create_task(_keeper(name, params, handle), name=f"mcp-{name}")

        asyncio.run_coroutine_threadsafe(_schedule(), self._loop).result(timeout=5)

        ready = self._wait_event(handle.ready, timeout=timeout)
        if not ready:
            self._handles.pop(name, None)
            raise TimeoutError(f"MCP server '{name}' did not initialize in {timeout}s")
        if handle.error is not None:
            self._handles.pop(name, None)
            raise handle.error
        log.info("mcp server '%s' ready, %d tool(s)", name, len(handle.tools))
        return [
            {"name": t.name, "description": (t.description or ""), "input_schema": t.inputSchema}
            for t in handle.tools
        ]

    def _wait_event(self, ev: asyncio.Event, timeout: float) -> bool:
        fut = asyncio.run_coroutine_threadsafe(_wait(ev, timeout), self._loop)
        try:
            return fut.result(timeout=timeout + 2)
        except Exception:
            return False

    # ── tool calls ─────────────────────────────────────────────────────────

    def has_server(self, name: str) -> bool:
        return name in self._handles and self._handles[name].session is not None

    def list_tools(self, name: str) -> list[dict[str, Any]]:
        h = self._handles.get(name)
        if not h:
            return []
        return [
            {"name": t.name, "description": (t.description or ""), "input_schema": t.inputSchema}
            for t in h.tools
        ]

    def call_tool(self, server: str, tool: str, args: dict, timeout: float = 60.0) -> str:
        """Call a tool synchronously. Returns concatenated text content."""
        h = self._handles.get(server)
        if not h or h.session is None:
            raise RuntimeError(f"MCP server '{server}' not available")

        async def _call():
            result = await h.session.call_tool(tool, args)
            parts: list[str] = []
            for c in result.content or []:
                txt = getattr(c, "text", None)
                if txt:
                    parts.append(txt)
            return "\n\n".join(parts) if parts else ""

        fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        return fut.result(timeout=timeout)


# ── module-level coroutines ────────────────────────────────────────────────

async def _wait(ev: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _keeper(name: str, params: StdioServerParameters, handle: _ServerHandle) -> None:
    """Long-running task that owns one MCP session's lifecycle."""
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resp = await session.list_tools()
                handle.session = session
                handle.tools = list(resp.tools)
                handle.ready.set()
                await handle.shutdown.wait()
    except BaseException as e:
        handle.error = e
        handle.ready.set()
        log.warning("mcp keeper '%s' exited with error: %s", name, e)
