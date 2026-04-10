# Virtual MCP Gateway

A Python gateway that exposes a small, stable MCP tool surface over a dynamic set of downstream MCP servers.

## Features

- Frontend transport: Streamable HTTP
- Downstream transports: stdio, Streamable HTTP, SSE
- Virtual tool registry with path-based progressive disclosure
- Session-gated invocation via `describe_tool` + session header path disclosure
- Header-aware filtering and header forwarding on every downstream tool call
- Background downstream refresh with cached tool lists
- Semantic fallback suggestions using spaCy when a requested tool path does not exist

## Virtual tools exposed by the gateway

- `browse_tools`
- `search_tools`
- `describe_tool`
- `invoke_tool`
- `refresh_registry`

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m gateway.server --config sample-config.json --host 127.0.0.1 --port 8080
```

## Notes

- The gateway MCP endpoint is `POST /mcp`.
- The gateway forwards request headers to downstream tool calls, excluding a small set of hop-by-hop HTTP headers.
- Use `X-Session-Id` to control disclosure state explicitly. If omitted, the transport `Mcp-Session-Id` is used.
- When a tool is missing, the gateway returns semantic suggestions based on path/name/description similarity.

## Example JSON-RPC calls

### Initialize

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-05","capabilities":{"tools":{"listChanged":true}},"clientInfo":{"name":"demo-client","version":"0.1.0"}}}
```

### List tools

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

### Describe a virtual tool

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"describe_tool","arguments":{"tool_path":"http/remote/github/search_repos"}}}
```

### Invoke a virtual tool

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"invoke_tool","arguments":{"tool_path":"http/remote/github/search_repos","arguments":{"query":"model context protocol"}}}}
```
