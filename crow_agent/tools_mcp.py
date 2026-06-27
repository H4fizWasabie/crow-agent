"""MCP (Model Context Protocol) client tools for Crow.

Connects to MCP servers (local subprocess or HTTP), discovers their tools,
and calls them. Uses JSON-RPC 2.0 over stdio or HTTP.

Server config: ~/.crow_agent/mcp_servers.json
Format: {"servers": {"name": {"command": "...", "args": [...], "env": {...}}}}
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("crow_agent.mcp")

DEFAULT_SERVERS_PATH = Path.home() / ".crow_agent" / "mcp_servers.json"
DEFAULT_SERVERS_CONFIG: dict[str, Any] = {"servers": {}}


def _load_servers() -> dict[str, Any]:
    """Load MCP server config from disk."""
    path = DEFAULT_SERVERS_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_SERVERS_CONFIG, indent=2))
        return DEFAULT_SERVERS_CONFIG
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return DEFAULT_SERVERS_CONFIG


def _save_servers(config: dict[str, Any]) -> None:
    DEFAULT_SERVERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_SERVERS_PATH.write_text(json.dumps(config, indent=2))


# ── MCP JSON-RPC client (stdio subprocess) ──

class _MCPStdioClient:
    """Talk to an MCP server over stdio (subprocess)."""

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None):
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        self._proc = subprocess.Popen(
            [command] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        self._lock = threading.Lock()
        self._next_id = 0
        # Initialize session
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            req = json.dumps({
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": method,
                "params": params or {},
            }) + "\n"
            try:
                self._proc.stdin.write(req.encode())  # type: ignore[union-attr]
                self._proc.stdin.flush()  # type: ignore[union-attr]
                line = self._proc.stdout.readline()  # type: ignore[union-attr]
                if not line:
                    return {"error": "No response from MCP server"}
                return json.loads(line)
            except Exception as e:
                return {"error": str(e)}

    def list_tools(self) -> list[dict[str, Any]]:
        resp = self._rpc("tools/list")
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        resp = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in resp:
            return json.dumps(resp["error"])
        result = resp.get("result", {})
        content = result.get("content", [])
        if isinstance(content, list):
            return "\n".join(
                c.get("text", json.dumps(c)) for c in content if isinstance(c, dict)
            )
        return json.dumps(result)


# ── In-memory client cache ──

_clients: dict[str, _MCPStdioClient] = {}


def _get_client(server_name: str) -> _MCPStdioClient | str:
    """Get or create an MCP client for a server. Returns client or error string."""
    if server_name in _clients:
        return _clients[server_name]

    config = _load_servers()
    servers = config.get("servers", {})
    entry = servers.get(server_name)
    if not entry:
        return f"Unknown MCP server: {server_name}. Available: {list(servers.keys())}"

    command = entry.get("command", "")
    args = entry.get("args", [])
    env = entry.get("env", None)
    if not command:
        return f"No command configured for server '{server_name}'"

    try:
        client = _MCPStdioClient(command, args, env)
        _clients[server_name] = client
        return client
    except Exception as e:
        return f"Failed to connect to MCP server '{server_name}': {e}"


# ── Registered tools ──

def register_tools(registry: Any, **kwargs: Any) -> None:
    """Register MCP tools."""

    @registry.register(
        description="List configured MCP servers and their available tools. Use first to discover what external tools are available."
    )
    def mcp_list_servers() -> str:
        config = _load_servers()
        servers = config.get("servers", {})
        if not servers:
            return (
                "No MCP servers configured.\n"
                f"Add servers to {DEFAULT_SERVERS_PATH}:\n"
                '{"servers": {"name": {"command": "...", "args": [...]}}}'
            )

        lines = []
        for name, entry in servers.items():
            cmd = entry.get("command", "?")
            lines.append(f"## {name}")
            lines.append(f"  command: {cmd} {' '.join(entry.get('args', []))}")
            # Try to list tools (best-effort, may fail if server not running)
            client = _get_client(name)
            if isinstance(client, str):
                lines.append(f"  status: disconnected ({client[:80]})")
            else:
                try:
                    tools = client.list_tools()
                    lines.append(f"  tools: {len(tools)} available")
                    for t in tools:
                        desc = t.get("description", "")[:80]
                        lines.append(f"    - {t.get('name', '?')}: {desc}")
                except Exception as e:
                    lines.append(f"  tools: error ({e})")
        return "\n".join(lines)

    @registry.register(
        description="List tools available from a specific MCP server. Call after mcp_list_servers."
    )
    def mcp_list_tools(server_name: str) -> str:
        client = _get_client(server_name)
        if isinstance(client, str):
            return client
        try:
            tools = client.list_tools()
            if not tools:
                return f"No tools available from '{server_name}'"
            lines = [f"## {server_name} tools:"]
            for t in tools:
                name = t.get("name", "?")
                desc = t.get("description", "")[:120]
                schema = t.get("inputSchema", {})
                props = schema.get("properties", {})
                args_str = ", ".join(props.keys()) if props else "none"
                lines.append(f"- {name}({args_str}): {desc}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing tools: {e}"

    @registry.register(
        description="Call a tool on an MCP server. Use after mcp_list_tools to see available tools and their arguments."
    )
    def mcp_call(server_name: str, tool_name: str, args_json: str = "{}") -> str:
        client = _get_client(server_name)
        if isinstance(client, str):
            return client
        try:
            arguments = json.loads(args_json)
        except json.JSONDecodeError as e:
            return f"Invalid JSON args: {e}"
        try:
            return client.call_tool(tool_name, arguments)
        except Exception as e:
            return f"Error calling {tool_name}: {e}"

    @registry.register(
        description="Add an MCP server configuration. command: executable path, args: list of CLI args."
    )
    def mcp_add_server(name: str, command: str, args_json: str = "[]") -> str:
        config = _load_servers()
        try:
            args = json.loads(args_json)
        except json.JSONDecodeError:
            return "args_json must be a valid JSON array, e.g. '[\"mcp\", \"serve\"]'"
        config.setdefault("servers", {})[name] = {"command": command, "args": args}
        _save_servers(config)
        # Clear cached client so next call reconnects
        _clients.pop(name, None)
        return f"Added MCP server '{name}': {command} {' '.join(args)}"
