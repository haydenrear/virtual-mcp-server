from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from .models import ClientConfig, DownstreamTool

logger = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"


class MCPError(RuntimeError):
    pass


class DownstreamClient(abc.ABC):
    def __init__(self, config: ClientConfig):
        self.config = config
        self._request_id = 0
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self.cached_tools: Dict[str, DownstreamTool] = {}

    @property
    def server_id(self) -> str:
        return self.config.server_id

    def next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def ensure_initialized(self) -> None:
        async with self._initialize_lock:
            if self._initialized:
                return
            await self.initialize()
            self._initialized = True

    @abc.abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def notify_initialized(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_tools(self, forwarded_headers: Optional[Dict[str, str]] = None) -> List[DownstreamTool]:
        raise NotImplementedError

    @abc.abstractmethod
    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        forwarded_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    async def refresh_tools(self, forwarded_headers: Optional[Dict[str, str]] = None) -> List[DownstreamTool]:
        await self.ensure_initialized()
        tools = await self.list_tools(forwarded_headers=forwarded_headers)
        self.cached_tools = {tool.path: tool for tool in tools}
        return tools

    def _normalize_tools(self, payload: List[Dict[str, Any]]) -> List[DownstreamTool]:
        results: List[DownstreamTool] = []
        prefix = self.config.tool_path_prefix.strip("/")
        namespace = prefix or self.server_id
        for item in payload:
            tool = DownstreamTool(
                server_id=self.server_id,
                tool_name=item["name"],
                description=item.get("description", ""),
                input_schema=item.get("inputSchema") or item.get("input_schema") or {},
                annotations=item.get("annotations"),
                title=item.get("title"),
                output_schema=item.get("outputSchema") or item.get("output_schema"),
                namespace=namespace,
                tags=item.get("tags", []),
            )
            tool.finalize()
            results.append(tool)
        return results


class JSONRPCOverHTTPClient(DownstreamClient):
    def __init__(self, config: ClientConfig):
        super().__init__(config)
        self._client = httpx.AsyncClient(timeout=config.request_timeout_seconds, follow_redirects=True)
        assert config.url is not None
        self.endpoint = config.url
        self._session_id: str | None = None

    async def initialize(self) -> None:
        payload = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self.next_request_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": self.config.protocol_version or "2025-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "clientInfo": {"name": "virtual-mcp-gateway", "version": "0.1.0"},
            },
        }
        headers = dict(self.config.headers)
        response = await self._client.post(self.endpoint, json=payload, headers=headers)
        response.raise_for_status()
        self._session_id = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
        _ = response.json()
        await self.notify_initialized()

    async def notify_initialized(self) -> None:
        payload = {"jsonrpc": JSONRPC_VERSION, "method": "notifications/initialized", "params": {}}
        headers = dict(self.config.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        response = await self._client.post(self.endpoint, json=payload, headers=headers)
        response.raise_for_status()

    async def _post(self, method: str, params: Dict[str, Any], forwarded_headers: Optional[Dict[str, str]] = None) -> Any:
        headers = dict(self.config.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if forwarded_headers:
            headers.update(forwarded_headers)
        payload = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self.next_request_id(),
            "method": method,
            "params": params,
        }
        response = await self._client.post(self.endpoint, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise MCPError(f"HTTP downstream error for {method}: {body['error']}")
        return body.get("result", {})

    async def list_tools(self, forwarded_headers: Optional[Dict[str, str]] = None) -> List[DownstreamTool]:
        result = await self._post("tools/list", {}, forwarded_headers=forwarded_headers)
        return self._normalize_tools(result.get("tools", []))

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        forwarded_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        result = await self._post(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            forwarded_headers=forwarded_headers,
        )
        return result


class SSEMCPClient(JSONRPCOverHTTPClient):
    """Compatibility client.

    Many legacy SSE servers still accept JSON-RPC POST requests on the same endpoint,
    while optionally using SSE for server-initiated events. This adapter keeps the same
    request behavior but is labeled separately in configuration so you can evolve it later.
    """


class StdioMCPClient(DownstreamClient):
    def __init__(self, config: ClientConfig):
        super().__init__(config)
        if not config.command:
            raise ValueError(f"stdio client {config.server_id} requires command")
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._pending: Dict[int, asyncio.Future[Any]] = {}

    async def _start(self) -> None:
        if self._proc is not None:
            return
        env = None
        self._proc = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._stdout_task = asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8").strip())
            except Exception:
                logger.warning("Failed to decode stdio JSON line from %s: %r", self.server_id, line)
                continue
            if "id" in message:
                future = self._pending.pop(int(message["id"]), None)
                if future and not future.done():
                    future.set_result(message)
            elif message.get("method") == "notifications/tools/list_changed":
                logger.info("Downstream %s emitted tools/list_changed", self.server_id)
            else:
                logger.debug("Received stdio notification from %s: %s", self.server_id, message)

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            logger.info("[%s stderr] %s", self.server_id, line.decode("utf-8").rstrip())

    async def _send(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        await self._start()
        assert self._proc is not None and self._proc.stdin is not None
        request_id = self.next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        payload = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }
        body = json.dumps(payload, separators=(",", ":")) + "\n"
        self._proc.stdin.write(body.encode("utf-8"))
        await self._proc.stdin.drain()
        message = await asyncio.wait_for(future, timeout=self.config.request_timeout_seconds)
        if "error" in message:
            raise MCPError(f"stdio downstream error for {method}: {message['error']}")
        return message.get("result", {})

    async def initialize(self) -> None:
        await self._send(
            "initialize",
            {
                "protocolVersion": self.config.protocol_version or "2025-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "clientInfo": {"name": "virtual-mcp-gateway", "version": "0.1.0"},
            },
        )
        await self.notify_initialized()

    async def notify_initialized(self) -> None:
        await self._start()
        assert self._proc is not None and self._proc.stdin is not None
        payload = {
            "jsonrpc": JSONRPC_VERSION,
            "method": "notifications/initialized",
            "params": {},
        }
        self._proc.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def list_tools(self, forwarded_headers: Optional[Dict[str, str]] = None) -> List[DownstreamTool]:
        result = await self._send("tools/list", {"_forwarded_headers": forwarded_headers or {}})
        return self._normalize_tools(result.get("tools", []))

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        forwarded_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return await self._send(
            "tools/call",
            {"name": tool_name, "arguments": arguments, "_forwarded_headers": forwarded_headers or {}},
        )

    async def close(self) -> None:
        if self._stdout_task is not None:
            self._stdout_task.cancel()
            with contextlib.suppress(Exception):
                await self._stdout_task
        if self._proc is not None:
            self._proc.terminate()
            with contextlib.suppress(ProcessLookupError):
                await self._proc.wait()
            self._proc = None


def build_client(config: ClientConfig) -> DownstreamClient:
    transport = config.transport.lower()
    if transport in {"http", "streamable-http", "streamable_http"}:
        return JSONRPCOverHTTPClient(config)
    if transport == "sse":
        return SSEMCPClient(config)
    if transport == "stdio":
        return StdioMCPClient(config)
    raise ValueError(f"Unsupported transport: {config.transport}")
