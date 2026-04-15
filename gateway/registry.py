from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Iterable, List, Optional

from .clients import DownstreamClient, build_client
from .models import (
    ClientConfig,
    DownstreamTool,
    MCPServerDefinition,
    MCPServerDeployment,
    SessionState,
)
from .semantic import SemanticMatcher

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, servers: Dict[str, MCPServerDefinition], matcher: SemanticMatcher):
        self.servers = servers
        self.matcher = matcher

        self.active_clients: Dict[str, DownstreamClient] = {}
        self.deployments: Dict[str, MCPServerDeployment] = {}
        self.last_init_by_server: Dict[str, dict] = {}

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
        for server in self.servers.values():
            if server.auto_deploy:
                try:
                    await self.deploy_server(server.server_id, init_values={})
                except Exception:
                    logger.exception("Auto-deploy failed for %s", server.server_id)

        await self.refresh_all()
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

        for server_id in list(self.active_clients.keys()):
            try:
                await self.undeploy_server(server_id)
            except Exception:
                logger.exception("Failed undeploying %s", server_id)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                await self.expire_idle_servers()
                await self.refresh_all()
            except Exception:
                logger.exception("Background registry refresh failed")

    async def expire_idle_servers(self) -> None:
        expired: List[str] = []
        for server_id, deployment in self.deployments.items():
            server_def = self.servers[server_id]
            if deployment.is_expired(server_def.idle_timeout_seconds):
                expired.append(server_id)

        for server_id in expired:
            logger.info("Undeploying idle server %s", server_id)
            await self.undeploy_server(server_id)

    async def deploy_server(
            self,
            server_id: str,
            init_values: Optional[dict] = None,
            reuse_last: bool = True,
    ) -> MCPServerDeployment:
        async with self._lock:
            server_def = self._require_server(server_id)

            supplied = dict(init_values or {})
            if not supplied and reuse_last and server_def.save_last_init:
                supplied = dict(self.last_init_by_server.get(server_id, {}))

            validated_init = server_def.validate_init(supplied)

            old_client = self.active_clients.get(server_id)
            if old_client is not None:
                await old_client.close()

            client_config = self._materialize_client_config(server_def, validated_init)
            client = build_client(client_config)
            await client.ensure_initialized()

            self.active_clients[server_id] = client
            deployment = MCPServerDeployment(
                server_id=server_id,
                initialized_at=time.time(),
                last_used_at=time.time(),
                init_values=validated_init,
                deployed=True,
            )
            self.deployments[server_id] = deployment

            if server_def.save_last_init:
                self.last_init_by_server[server_id] = dict(validated_init)

            try:
                tools = await client.refresh_tools()
            except Exception:
                await client.close()
                self.active_clients.pop(server_id, None)
                self.deployments.pop(server_id, None)
                raise

            self._replace_server_tools(server_id, tools)
            return deployment

    async def undeploy_server(self, server_id: str) -> None:
        async with self._lock:
            client = self.active_clients.pop(server_id, None)
            self.deployments.pop(server_id, None)
            self.tools_by_server.pop(server_id, None)
            self.tools_by_path = {
                path: tool
                for path, tool in self.tools_by_path.items()
                if tool.server_id != server_id
            }

        if client is not None:
            await client.close()

    async def refresh_all(self) -> None:
        async with self._lock:
            new_tools_by_path: Dict[str, DownstreamTool] = {}
            new_tools_by_server: Dict[str, Dict[str, DownstreamTool]] = {}

            for server_id, client in self.active_clients.items():
                try:
                    tools = await client.refresh_tools()
                except Exception:
                    logger.exception("Failed refreshing deployed server %s", server_id)
                    continue

                deployment = self.deployments.get(server_id)
                if deployment is not None:
                    deployment.touch()

                per_server: Dict[str, DownstreamTool] = {}
                for tool in tools:
                    tool.last_seen_at = time.time()
                    per_server[tool.path] = tool
                    new_tools_by_path[tool.path] = tool

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

    def browse_servers(self, deployed: Optional[bool] = None) -> List[dict]:
        items = []
        for server_id, server_def in sorted(self.servers.items()):
            is_deployed = server_id in self.deployments
            if deployed is not None and is_deployed != deployed:
                continue
            items.append(
                {
                    "server_id": server_id,
                    "display_name": server_def.display_name,
                    "description": server_def.description,
                    "deployed": is_deployed,
                    "tool_count": len(self.tools_by_server.get(server_id, {})),
                }
            )
        return items

    def describe_server(self, server_id: str) -> dict:
        server_def = self._require_server(server_id)
        deployment = self.deployments.get(server_id)

        payload = {
            "server_id": server_def.server_id,
            "display_name": server_def.display_name,
            "description": server_def.description,
            "transport": server_def.client.transport,
            "init_schema": server_def.schema_dict(),
            "save_last_init": server_def.save_last_init,
            "idle_timeout_seconds": server_def.idle_timeout_seconds,
            "deployed": deployment is not None,
        }

        if deployment is not None:
            payload["deployment"] = {
                "initialized_at": deployment.initialized_at,
                "last_used_at": deployment.last_used_at,
                "expires_at": deployment.expires_at(server_def.idle_timeout_seconds),
                "init_values": server_def.redact_init(deployment.init_values),
            }
        else:
            remembered = self.last_init_by_server.get(server_id)
            if remembered:
                payload["last_init_values"] = server_def.redact_init(remembered)

        return payload

    def find_tool(self, tool_path: str) -> Optional[DownstreamTool]:
        return self.tools_by_path.get(tool_path)

    def get_active_client(self, server_id: str) -> DownstreamClient:
        client = self.active_clients.get(server_id)
        if client is None:
            raise ValueError(f"MCP server is not deployed: {server_id}")
        deployment = self.deployments.get(server_id)
        if deployment is not None:
            deployment.touch()
        return client

    def suggest_tools(self, query: str, headers: Dict[str, str], limit: int = 5) -> List[Dict[str, object]]:
        tools = self.filtered_tools(headers)
        hits = self.matcher.rank(query=query, tools=tools, limit=limit)
        return [
            {
                "path": hit.tool.path,
                "tool_name": hit.tool.tool_name,
                "server_id": hit.tool.server_id,
                "description": hit.tool.description,
                "score": hit.score,
                "reason": hit.reason,
            }
            for hit in hits
        ]

    def _replace_server_tools(self, server_id: str, tools: List[DownstreamTool]) -> None:
        self.tools_by_path = {
            path: tool
            for path, tool in self.tools_by_path.items()
            if tool.server_id != server_id
        }
        per_server: Dict[str, DownstreamTool] = {}
        for tool in tools:
            per_server[tool.path] = tool
            self.tools_by_path[tool.path] = tool
        self.tools_by_server[server_id] = per_server

    def _materialize_client_config(
            self,
            server_def: MCPServerDefinition,
            init_values: dict,
    ) -> ClientConfig:
        base = server_def.client
        config = ClientConfig(**vars(base))

        # quick path-based/header/env mutation
        if "url" in init_values:
            config.url = init_values["url"]
        if "headers" in init_values and isinstance(init_values["headers"], dict):
            merged = dict(config.headers)
            merged.update(init_values["headers"])
            config.headers = merged
        if "command" in init_values and isinstance(init_values["command"], list):
            config.command = list(init_values["command"])
        if "tool_path_prefix" in init_values:
            config.tool_path_prefix = str(init_values["tool_path_prefix"])
        if "namespace" in init_values:
            config.namespace = str(init_values["namespace"])

        return config

    def _require_server(self, server_id: str) -> MCPServerDefinition:
        server = self.servers.get(server_id)
        if server is None:
            raise ValueError(f"Unknown MCP server: {server_id}")
        return server