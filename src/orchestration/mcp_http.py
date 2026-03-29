"""MCP HTTP equivalence — JSON-RPC 2.0 router.

Implements:
  - Single POST /mcp endpoint
  - Route by JSON-RPC method
  - JSON-RPC 2.0 request/success/error envelopes
  - Methods: initialize, tools/list, tools/call,
             resources/list, resources/read, prompts/list, prompts/get
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


# ---------------------------------------------------------------------------
# JSON-RPC error codes
# ---------------------------------------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# JsonRpcError
# ---------------------------------------------------------------------------

class JsonRpcError(Exception):
    """JSON-RPC error with code, message, and optional data."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------

def build_success(id: str | int | None, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response envelope."""
    return {"jsonrpc": "2.0", "id": id, "result": result}


def build_error(
    id: str | int | None,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response envelope."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": error}


# ---------------------------------------------------------------------------
# Request parser
# ---------------------------------------------------------------------------

def parse_jsonrpc_request(body: bytes) -> dict[str, Any]:
    """Parse and validate a JSON-RPC 2.0 request envelope.

    Raises JsonRpcError with appropriate code on failure.
    """
    if not body:
        raise JsonRpcError(PARSE_ERROR, "Parse error: empty body")

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise JsonRpcError(PARSE_ERROR, "Parse error: invalid JSON")

    if not isinstance(data, dict):
        raise JsonRpcError(INVALID_REQUEST, "Invalid request: expected JSON object")

    if data.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, "Invalid request: jsonrpc must be '2.0'")

    if "id" not in data:
        raise JsonRpcError(INVALID_REQUEST, "Invalid request: missing id")

    method = data.get("method")
    if not isinstance(method, str):
        raise JsonRpcError(INVALID_REQUEST, "Invalid request: method must be a string")

    return data


# ---------------------------------------------------------------------------
# Registry data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    """Definition of an MCP tool."""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], list[dict[str, Any]]]


@dataclass
class ResourceDef:
    """Definition of an MCP resource."""
    uri: str
    name: str
    description: str
    mime_type: str
    handler: Callable[[], list[dict[str, Any]]]


@dataclass
class PromptDef:
    """Definition of an MCP prompt template."""
    name: str
    description: str
    arguments: list[dict[str, Any]]
    handler: Callable[[dict[str, Any]], list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# McpRouter
# ---------------------------------------------------------------------------

class McpRouter:
    """JSON-RPC 2.0 method router implementing MCP HTTP equivalence.

    Usage:
        router = McpRouter(server_name="my-server", server_version="0.1.0")
        router.register_tool(ToolDef(...))
        http_status, response_dict = router.dispatch(request_body)
    """

    def __init__(
        self,
        *,
        server_name: str = "mcp-server",
        server_version: str = "0.1.0",
        auth_token: str | None = None,
    ):
        self._server_name = server_name
        self._server_version = server_version
        self._auth_token = auth_token

        self._tools: list[ToolDef] = []
        self._resources: list[ResourceDef] = []
        self._prompts: list[PromptDef] = []

        # Method dispatch table
        self._methods: dict[str, Callable[[dict[str, Any], str | int | None], dict[str, Any]]] = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "resources/list": self._handle_resources_list,
            "resources/read": self._handle_resources_read,
            "prompts/list": self._handle_prompts_list,
            "prompts/get": self._handle_prompts_get,
        }

    # -- Registry -----------------------------------------------------------

    def register_tool(self, tool: ToolDef) -> None:
        self._tools.append(tool)

    def register_resource(self, resource: ResourceDef) -> None:
        self._resources.append(resource)

    def register_prompt(self, prompt: PromptDef) -> None:
        self._prompts.append(prompt)

    # -- Dispatch -----------------------------------------------------------

    def dispatch(
        self,
        body: bytes,
        *,
        auth_token: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Parse, authenticate, route, and return (http_status, response_dict)."""

        # Auth check — return HTTP 401 before JSON-RPC handling
        if self._auth_token is not None:
            if auth_token != self._auth_token:
                return 401, {"error": "Unauthorized", "message": "Invalid or missing auth token"}

        # Try to extract id from body for error correlation (Fix #6)
        req_id: str | int | None = None
        try:
            raw = json.loads(body)
            if isinstance(raw, dict):
                req_id = raw.get("id")
        except Exception:
            pass

        # Parse the JSON-RPC request
        try:
            parsed = parse_jsonrpc_request(body)
        except JsonRpcError as exc:
            return 200, build_error(req_id, exc.code, exc.message, exc.data)

        req_id = parsed.get("id")
        method = parsed["method"]

        # Validate params type — must be dict if present (Fix #5)
        params = parsed.get("params")
        if params is not None and not isinstance(params, dict):
            return 200, build_error(
                req_id, INVALID_PARAMS,
                "Invalid params: params must be a JSON object",
            )

        # Route to handler
        handler = self._methods.get(method)
        if handler is None:
            return 200, build_error(req_id, METHOD_NOT_FOUND, "Method not found", {"method": method})

        try:
            result = handler(parsed, req_id)
            return 200, result
        except JsonRpcError as exc:
            return 200, build_error(req_id, exc.code, exc.message, exc.data)
        except Exception:
            return 200, build_error(req_id, INTERNAL_ERROR, "Internal error")

    # -- Method handlers ----------------------------------------------------

    def _handle_initialize(self, req: dict[str, Any], req_id: str | int | None) -> dict[str, Any]:
        params = req.get("params") or {}
        protocol_version = params.get("protocolVersion", "2024-11-05")
        return build_success(req_id, {
            "protocolVersion": protocol_version,
            "serverInfo": {
                "name": self._server_name,
                "version": self._server_version,
            },
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
            },
        })

    def _handle_tools_list(self, req: dict[str, Any], req_id: str | int | None) -> dict[str, Any]:
        tools = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self._tools
        ]
        return build_success(req_id, {"tools": tools})

    def _handle_tools_call(self, req: dict[str, Any], req_id: str | int | None) -> dict[str, Any]:
        params = req.get("params") or {}
        if not isinstance(params, dict):
            raise JsonRpcError(INVALID_PARAMS, "Invalid params: params must be an object")
        name = params.get("name")
        if not name:
            raise JsonRpcError(INVALID_PARAMS, "Invalid params: missing tool name")

        tool = next((t for t in self._tools if t.name == name), None)
        if tool is None:
            raise JsonRpcError(INVALID_PARAMS, f"Invalid params: unknown tool '{name}'")

        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "Invalid params: arguments must be an object")
        content = tool.handler(arguments)
        return build_success(req_id, {"content": content})

    def _handle_resources_list(self, req: dict[str, Any], req_id: str | int | None) -> dict[str, Any]:
        resources = [
            {
                "uri": r.uri,
                "name": r.name,
                "description": r.description,
                "mimeType": r.mime_type,
            }
            for r in self._resources
        ]
        return build_success(req_id, {"resources": resources})

    def _handle_resources_read(self, req: dict[str, Any], req_id: str | int | None) -> dict[str, Any]:
        params = req.get("params") or {}
        uri = params.get("uri")
        if not uri:
            raise JsonRpcError(INVALID_PARAMS, "Invalid params: missing resource URI")

        resource = next((r for r in self._resources if r.uri == uri), None)
        if resource is None:
            raise JsonRpcError(INVALID_PARAMS, f"Invalid params: unknown resource '{uri}'")

        contents = resource.handler()
        return build_success(req_id, {"contents": contents})

    def _handle_prompts_list(self, req: dict[str, Any], req_id: str | int | None) -> dict[str, Any]:
        prompts = [
            {
                "name": p.name,
                "description": p.description,
                "arguments": p.arguments,
            }
            for p in self._prompts
        ]
        return build_success(req_id, {"prompts": prompts})

    def _handle_prompts_get(self, req: dict[str, Any], req_id: str | int | None) -> dict[str, Any]:
        params = req.get("params") or {}
        name = params.get("name")
        if not name:
            raise JsonRpcError(INVALID_PARAMS, "Invalid params: missing prompt name")

        prompt = next((p for p in self._prompts if p.name == name), None)
        if prompt is None:
            raise JsonRpcError(INVALID_PARAMS, f"Invalid params: unknown prompt '{name}'")

        arguments = params.get("arguments") or {}
        messages = prompt.handler(arguments)
        return build_success(req_id, {"messages": messages})
