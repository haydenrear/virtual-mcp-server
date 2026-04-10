from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from .clients import MCPError, build_client
from .config import GatewayConfigModel, load_config
from .models import DownstreamTool
from .registry import ToolRegistry
from .semantic import SemanticMatcher

logger = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"
VIRTUAL_TOOL_NAMES = {
    "browse_tools",
    "search_tools",
    "describe_tool",
    "invoke_tool",
    "refresh_registry",
}


class GatewayServer:
    def __init__(self, config: GatewayConfigModel):
        self.config = config
        clients = {item.server_id: build_client(item.to_internal()) for item in config.clients}
        self.registry = ToolRegistry(clients=clients, matcher=SemanticMatcher.create())
        self.app = FastAPI(title="virtual-mcp-gateway")
        self._configure_routes()

    async def startup(self) -> None:
        await self.registry.start()
        logger.info("Gateway started with %d downstream clients and %d tools", len(self.registry.clients), len(self.registry.tools_by_path))

    async def shutdown(self) -> None:
        await self.registry.stop()

    def _configure_routes(self) -> None:
        @self.app.on_event("startup")
        async def _startup() -> None:
            await self.startup()

        @self.app.on_event("shutdown")
        async def _shutdown() -> None:
            await self.shutdown()

        @self.app.get("/health")
        async def health() -> Dict[str, Any]:
            return {
                "ok": True,
                "clients": list(self.registry.clients.keys()),
                "tool_count": len(self.registry.tools_by_path),
                "spacy_model": self.registry.matcher.model_name,
            }

        @self.app.post("/mcp")
        async def mcp_endpoint(request: Request) -> JSONResponse:
            body = await request.json()
            headers = _normalize_headers(dict(request.headers))
            method = body.get("method")
            params = body.get("params") or {}
            request_id = body.get("id")

            try:
                if method == "initialize":
                    result = self._handle_initialize()
                elif method == "notifications/initialized":
                    return JSONResponse(status_code=202, content={})
                elif method == "tools/list":
                    result = await self._handle_tools_list(headers)
                elif method == "tools/call":
                    result = await self._handle_tools_call(params, headers)
                else:
                    return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")
                if request_id is None:
                    return JSONResponse(status_code=202, content={})
                return JSONResponse(content={"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result})
            except GatewayHTTPError as exc:
                return _jsonrpc_error(request_id, exc.code, exc.message, exc.data)
            except Exception as exc:
                logger.exception("Unhandled gateway error")
                return _jsonrpc_error(request_id, -32000, str(exc))

    def _handle_initialize(self) -> Dict[str, Any]:
        return {
            "protocolVersion": self.config.protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {"name": "virtual-mcp-gateway", "version": "0.1.0"},
        }

    async def _handle_tools_list(self, headers: Dict[str, str]) -> Dict[str, Any]:
        tools = [
            {
                "name": "browse_tools",
                "description": "Browse virtual tools by hierarchical path prefix.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path_prefix": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "search_tools",
                "description": "Search the virtual tool registry by text query. Uses spaCy similarity when available.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "describe_tool",
                "description": "Reveal the full schema for a virtual tool and register that disclosure in the current session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool_path": {"type": "string"},
                    },
                    "required": ["tool_path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "invoke_tool",
                "description": "Invoke a virtual tool that has already been disclosed in this session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool_path": {"type": "string"},
                        "arguments": {"type": "object"},
                        "schema_digest": {"type": "string"},
                    },
                    "required": ["tool_path", "arguments", "schema_digest"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "refresh_registry",
                "description": "Refresh downstream tool caches immediately.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        ]
        return {"tools": tools}

    async def _handle_tools_call(self, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in VIRTUAL_TOOL_NAMES:
            raise GatewayHTTPError(-32602, f"Unknown gateway tool: {name}")
        if name == "browse_tools":
            return self._wrap_content(await self._browse_tools(arguments, headers))
        if name == "search_tools":
            return self._wrap_content(await self._search_tools(arguments, headers))
        if name == "describe_tool":
            return self._wrap_content(await self._describe_tool(arguments, headers))
        if name == "invoke_tool":
            return await self._invoke_tool(arguments, headers)
        if name == "refresh_registry":
            await self.registry.refresh_all()
            return self._wrap_content({"refreshed": True, "tool_count": len(self.registry.tools_by_path)})
        raise GatewayHTTPError(-32602, f"Unsupported tool call: {name}")

    def _wrap_content(self, value: Any) -> Dict[str, Any]:
        return {
            "content": [
                {
                    "type": "text",
                    "text": _json_dumps(value),
                }
            ],
            "structuredContent": value,
        }

    async def _browse_tools(self, arguments: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        path_prefix = str(arguments.get("path_prefix") or "").strip()
        limit = int(arguments.get("limit") or 100)
        tools = self.registry.filtered_tools(headers=headers, path_prefix=path_prefix)[:limit]
        return {
            "items": [
                {
                    "path": tool.path,
                    "tool_name": tool.tool_name,
                    "description": tool.description,
                    "schema_digest": tool.schema_digest,
                }
                for tool in tools
            ],
            "count": len(tools),
        }

    async def _search_tools(self, arguments: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise GatewayHTTPError(-32602, "search_tools requires a non-empty query")
        limit = int(arguments.get("limit") or 10)
        suggestions = self.registry.suggest_tools(query=query, headers=headers, limit=limit)
        return {"query": query, "matches": suggestions, "spacy_model": self.registry.matcher.model_name}

    async def _describe_tool(self, arguments: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        tool_path = str(arguments.get("tool_path") or "").strip()
        if not tool_path:
            raise GatewayHTTPError(-32602, "describe_tool requires tool_path")
        tool = self.registry.find_tool(tool_path)
        if tool is None:
            suggestions = self.registry.suggest_tools(tool_path, headers=headers, limit=5)
            raise GatewayHTTPError(
                -32004,
                f"Virtual tool not found: {tool_path}",
                {"suggestions": suggestions},
            )
        session = self.registry.get_or_create_session(_session_id(headers))
        session.last_headers = headers
        disclosure = session.disclose(tool.path, tool.schema_digest, ttl_seconds=self.config.disclosure_ttl_seconds)
        return {
            "tool_path": tool.path,
            "tool_name": tool.tool_name,
            "server_id": tool.server_id,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "schema_digest": tool.schema_digest,
            "disclosed_at": disclosure.disclosed_at,
            "expires_at": disclosure.expires_at,
        }

    async def _invoke_tool(self, arguments: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        tool_path = str(arguments.get("tool_path") or "").strip()
        if not tool_path:
            raise GatewayHTTPError(-32602, "invoke_tool requires tool_path")
        schema_digest = str(arguments.get("schema_digest") or "").strip()
        call_args = arguments.get("arguments")
        if not isinstance(call_args, dict):
            raise GatewayHTTPError(-32602, "invoke_tool requires an object in arguments")

        tool = self.registry.find_tool(tool_path)
        if tool is None:
            suggestions = self.registry.suggest_tools(tool_path, headers=headers, limit=5)
            raise GatewayHTTPError(
                -32004,
                f"Virtual tool not found: {tool_path}",
                {"suggestions": suggestions},
            )

        session = self.registry.get_or_create_session(_session_id(headers))
        session.last_headers = headers
        if not session.validate(tool_path, schema_digest):
            raise GatewayHTTPError(
                -32010,
                "Tool invocation denied. Call describe_tool first with the same tool_path and use the current schema_digest.",
                {
                    "tool_path": tool_path,
                    "current_schema_digest": tool.schema_digest,
                },
            )

        forwarded_headers = _forwardable_headers(headers)
        forwarded_headers["x-virtual-tool-path"] = tool.path
        forwarded_headers["x-virtual-tool-name"] = tool.tool_name
        forwarded_headers["x-virtual-tool-schema-digest"] = tool.schema_digest

        client = self.registry.clients[tool.server_id]
        try:
            downstream_result = await client.call_tool(tool.tool_name, call_args, forwarded_headers=forwarded_headers)
        except MCPError as exc:
            raise GatewayHTTPError(-32020, f"Downstream tool call failed: {exc}") from exc

        if isinstance(downstream_result, dict):
            return downstream_result
        return self._wrap_content(downstream_result)


class GatewayHTTPError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _session_id(headers: Dict[str, str]) -> str:
    return headers.get("mcp-session-id") or headers.get("x-session-id") or "default"


def _normalize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _forwardable_headers(headers: Dict[str, str]) -> Dict[str, str]:
    blocked = {
        "content-length",
        "content-type",
        "host",
        "connection",
    }
    return {key: value for key, value in headers.items() if key not in blocked}


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, indent=2, default=str)


def _jsonrpc_error(request_id: Any, code: int, message: str, data: Any | None = None) -> JSONResponse:
    payload: Dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if data is not None:
        payload["error"]["data"] = data
    return JSONResponse(status_code=200, content=payload)


def build_app(config_path: str) -> FastAPI:
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
