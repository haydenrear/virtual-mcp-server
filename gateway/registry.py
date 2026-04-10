from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Iterable, List, Optional

from .clients import DownstreamClient
from .models import DownstreamTool, SessionState
from .semantic import SemanticMatcher

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, clients: Dict[str, DownstreamClient], matcher: SemanticMatcher):
        self.clients = clients
        self.matcher = matcher
        self.tools_by_path: Dict[str, DownstreamTool] = {}
        self.tools_by_server: Dict[str, Dict[str, DownstreamTool]] = {}
        self.sessions: Dict[str, SessionState] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def get_or_create_session(self, session_id: str) -> SessionState:
        state = self.sessions.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id)
            self.sessions[session_id] = state
        return state

    def iter_tools(self) -> Iterable[DownstreamTool]:
        return self.tools_by_path.values()

    async def start(self) -> None:
        await self.refresh_all()
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        for server_id, client in self.clients.items():
            try:
                await client.close()
            except Exception:
                logger.exception("Failed closing downstream client %s", server_id)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                await self.refresh_all()
            except Exception:
                logger.exception("Background tool refresh failed")

    async def refresh_all(self) -> None:
        async with self._lock:
            new_tools_by_path: Dict[str, DownstreamTool] = {}
            new_tools_by_server: Dict[str, Dict[str, DownstreamTool]] = {}
            for server_id, client in self.clients.items():
                try:
                    tools = await client.refresh_tools()
                except Exception:
                    logger.exception("Failed refreshing downstream client %s", server_id)
                    continue
                per_server: Dict[str, DownstreamTool] = {}
                for tool in tools:
                    tool.last_seen_at = time.time()
                    new_tools_by_path[tool.path] = tool
                    per_server[tool.path] = tool
                new_tools_by_server[server_id] = per_server
            self.tools_by_path = new_tools_by_path
            self.tools_by_server = new_tools_by_server

    def filtered_tools(self, headers: Dict[str, str], path_prefix: str | None = None) -> List[DownstreamTool]:
        excluded_raw = headers.get("x-exclude-tools", "")
        excluded = {item.strip() for item in excluded_raw.split(",") if item.strip()}
        allow_prefix = headers.get("x-allow-tool-prefix", "").strip()

        tools = list(self.iter_tools())
        if path_prefix:
            normalized = path_prefix.strip("/")
            tools = [tool for tool in tools if tool.path.startswith(normalized)]
        if excluded:
            tools = [tool for tool in tools if tool.path not in excluded and tool.tool_name not in excluded]
        if allow_prefix:
            tools = [tool for tool in tools if tool.path.startswith(allow_prefix)]
        return sorted(tools, key=lambda tool: tool.path)

    def find_tool(self, tool_path: str) -> Optional[DownstreamTool]:
        return self.tools_by_path.get(tool_path)

    def suggest_tools(self, query: str, headers: Dict[str, str], limit: int = 5) -> List[Dict[str, object]]:
        tools = self.filtered_tools(headers)
        hits = self.matcher.rank(query=query, tools=tools, limit=limit)
        return [
            {
                "path": hit.tool.path,
                "tool_name": hit.tool.tool_name,
                "description": hit.tool.description,
                "score": hit.score,
                "reason": hit.reason,
            }
            for hit in hits
        ]
