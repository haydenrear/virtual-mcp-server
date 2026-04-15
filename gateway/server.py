from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .clients import MCPError
from .config import GatewayConfigModel, load_config
from .registry import ToolRegistry
from .semantic import SemanticMatcher

logger = logging.getLogger(__name__)

DISCLOSURE_SESSION_HEADER = "x-session-id"
MCP_SESSION_HEADER = "mcp-session-id"


class GatewayServer:
    def __init__(self, config: GatewayConfigModel):
        self.config = config
        servers = {item.server_id: item.to_internal() for item in config.mcp_servers}
        self.registry = ToolRegistry(servers=servers, matcher=SemanticMatcher.create())
        self.mcp = FastMCP(
            name="virtual-mcp-gateway",
            instructions="Virtual MCP gateway over deployable downstream MCP servers.",
            lifespan=self._lifespan,
            streamable_http_path="/mcp",
        )
        self._register_tools()
        self._register_routes()
        self.app = self.mcp.streamable_http_app()

    @asynccontextmanager
    async def _lifespan(self, _: FastMCP) -> AsyncIterator[None]:
        await self.startup()
        try:
            yield
        finally:
            await self.shutdown()

    async def startup(self) -> None:
        await self.registry.start()
        logger.info(
            "Gateway started with %d catalog servers and %d active tools",
            len(self.registry.servers),
            len(self.registry.tools_by_path),
        )

    async def shutdown(self) -> None:
        await self.registry.stop()

    async def _health_endpoint(self, _: Request) -> JSONResponse:
        return JSONResponse(self.health_payload())

    def health_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "catalog_server_count": len(self.registry.servers),
            "deployed_server_count": len(self.registry.deployments),
            "tool_count": len(self.registry.tools_by_path),
            "spacy_model": self.registry.matcher.model_name,
        }

    def _register_tools(self) -> None:
        self.mcp.add_tool(self._browse_mcp_servers_tool, name="browse_mcp_servers")
        self.mcp.add_tool(self._describe_mcp_server_tool, name="describe_mcp_server")
        self.mcp.add_tool(self._deploy_mcp_server_tool, name="deploy_mcp_server")
        self.mcp.add_tool(self._undeploy_mcp_server_tool, name="undeploy_mcp_server")

        self.mcp.add_tool(self._browse_active_tools_tool, name="browse_active_tools")
        self.mcp.add_tool(self._search_tools_tool, name="search_tools")
        self.mcp.add_tool(self._describe_tool_tool, name="describe_tool")
        self.mcp.add_tool(self._invoke_tool_tool, name="invoke_tool")
        self.mcp.add_tool(self._refresh_registry_tool, name="refresh_registry")

    def _register_routes(self) -> None:
        self.mcp.custom_route("/health", methods=["GET"])(self._health_endpoint)

    async def _browse_mcp_servers_tool(
            self,
            deployed: bool | None = None,
    ) -> dict[str, Any]:
        items = self.registry.browse_servers(deployed=deployed)
        return {"items": items, "count": len(items)}

    async def _describe_mcp_server_tool(self, server_id: str) -> dict[str, Any]:
        return self.registry.describe_server(server_id)

    async def _deploy_mcp_server_tool(
            self,
            server_id: str,
            initialization: dict[str, Any] | None = None,
            reuse_last_initialization: bool = True,
    ) -> dict[str, Any]:
        deployment = await self.registry.deploy_server(
            server_id=server_id,
            init_values=initialization or {},
            reuse_last=reuse_last_initialization,
        )
        return {
            "server_id": server_id,
            "deployed": True,
            "initialized_at": deployment.initialized_at,
            "last_used_at": deployment.last_used_at,
            "tool_count": len(self.registry.tools_by_server.get(server_id, {})),
        }

    async def _undeploy_mcp_server_tool(self, server_id: str) -> dict[str, Any]:
        await self.registry.undeploy_server(server_id)
        return {"server_id": server_id, "deployed": False}

    async def _browse_active_tools_tool(
            self,
            ctx: Context,
            path_prefix: str = "",
            server_id: str | None = None,
            limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        headers = _headers_from_context(ctx)
        tools = self.registry.filtered_tools(headers=headers, path_prefix=path_prefix.strip())

        if server_id:
            tools = [tool for tool in tools if tool.server_id == server_id]

        tools = tools[:limit]
        return {
            "items": [
                {
                    "path": tool.path,
                    "tool_name": tool.tool_name,
                    "server_id": tool.server_id,
                    "description": tool.description,
                }
                for tool in tools
            ],
            "count": len(tools),
        }

    async def _search_tools_tool(
            self,
            query: str,
            ctx: Context,
            limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        headers = _headers_from_context(ctx)
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("search_tools requires a non-empty query")
        suggestions = self.registry.suggest_tools(query=normalized_query, headers=headers, limit=limit)
        return {
            "query": normalized_query,
            "matches": suggestions,
            "spacy_model": self.registry.matcher.model_name,
        }

    async def _describe_tool_tool(self, tool_path: str, ctx: Context) -> dict[str, Any]:
        headers = _headers_from_context(ctx)
        normalized_tool_path = tool_path.strip()
        if not normalized_tool_path:
            raise ValueError("describe_tool requires tool_path")

        tool = self.registry.find_tool(normalized_tool_path)
        if tool is None:
            suggestions = self.registry.suggest_tools(normalized_tool_path, headers=headers, limit=5)
            raise ValueError(f"Virtual tool not found: {normalized_tool_path}. Suggestions: {suggestions}")

        session = self.registry.get_or_create_session(_session_id(headers))
        session.last_headers = headers
        disclosure = session.disclose(tool.path, ttl_seconds=self.config.disclosure_ttl_seconds)

        server_description = self.registry.describe_server(tool.server_id)
        return {
            "tool_path": tool.path,
            "tool_name": tool.tool_name,
            "server_id": tool.server_id,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "server_initialization_schema": server_description["init_schema"],
            "disclosed_at": disclosure.disclosed_at,
            "expires_at": disclosure.expires_at,
            "session_header_name": DISCLOSURE_SESSION_HEADER,
        }

    async def _invoke_tool_tool(
            self,
            tool_path: str,
            arguments: dict[str, Any],
            ctx: Context,
    ) -> dict[str, Any]:
        headers = _headers_from_context(ctx)
        normalized_tool_path = tool_path.strip()
        if not normalized_tool_path:
            raise ValueError("invoke_tool requires tool_path")

        tool = self.registry.find_tool(normalized_tool_path)
        if tool is None:
            suggestions = self.registry.suggest_tools(normalized_tool_path, headers=headers, limit=5)
            raise ValueError(f"Virtual tool not found: {normalized_tool_path}. Suggestions: {suggestions}")

        session = self.registry.get_or_create_session(_session_id(headers))
        session.last_headers = headers
        if not session.validate(normalized_tool_path):
            raise ValueError(
                "Tool invocation denied. Call describe_tool first for the same path or an ancestor path in the "
                f"current {DISCLOSURE_SESSION_HEADER} session."
            )

        forwarded_headers = _forwardable_headers(headers)
        forwarded_headers["x-virtual-tool-path"] = tool.path
        forwarded_headers["x-virtual-tool-name"] = tool.tool_name
        forwarded_headers["x-virtual-tool-schema-digest"] = tool.schema_digest

        client = self.registry.get_active_client(tool.server_id)
        try:
            downstream_result = await client.call_tool(
                tool.tool_name,
                arguments,
                forwarded_headers=forwarded_headers,
            )
        except MCPError as exc:
            raise RuntimeError(f"Downstream tool call failed: {exc}") from exc

        if isinstance(downstream_result, dict):
            return downstream_result
        return {"value": downstream_result}

    async def _refresh_registry_tool(self) -> dict[str, Any]:
        await self.registry.refresh_all()
        return {
            "refreshed": True,
            "deployed_server_count": len(self.registry.deployments),
            "tool_count": len(self.registry.tools_by_path),
        }


def _session_id(headers: dict[str, str]) -> str:
    return headers.get(DISCLOSURE_SESSION_HEADER) or headers.get(MCP_SESSION_HEADER) or "default"


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _forwardable_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {
        "accept",
        "connection",
        "content-length",
        "content-type",
        "host",
        "last-event-id",
        "mcp-protocol-version",
        MCP_SESSION_HEADER,
        DISCLOSURE_SESSION_HEADER,
    }
    return {key: value for key, value in headers.items() if key not in blocked}


def _headers_from_context(ctx: Context) -> dict[str, str]:
    request = ctx.request_context.request
    if request is None:
        return {}
    raw_headers = getattr(request, "headers", None)
    if raw_headers is None:
        return {}
    return _normalize_headers(dict(raw_headers))


def build_app(config_path: str) -> Any:
    config = load_config(config_path)
    gateway = GatewayServer(config)
    return gateway.app


def main() -> None:
    parser = argparse.ArgumentParser(description="Virtual MCP Gateway")
    parser.add_argument("--config", required=True, help="Path to gateway JSON config")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    app = build_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()