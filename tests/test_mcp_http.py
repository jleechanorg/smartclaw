"""Tests for orchestration.mcp_http — MCP HTTP equivalence (JSON-RPC 2.0 router).
"""

from __future__ import annotations

import json

import pytest

from orchestration.mcp_http import (
    JsonRpcError,
    McpRouter,
    PromptDef,
    ResourceDef,
    ToolDef,
    build_error,
    build_success,
    parse_jsonrpc_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(method: str, params: dict | None = None, id: str = "req-1") -> bytes:
    """Build a JSON-RPC 2.0 request body."""
    body: dict = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body).encode()


def _make_router(**kwargs) -> McpRouter:
    """Create an McpRouter with sensible defaults for testing."""
    return McpRouter(
        server_name=kwargs.get("server_name", "test-server"),
        server_version=kwargs.get("server_version", "0.1.0"),
        auth_token=kwargs.get("auth_token", None),
    )


# ---------------------------------------------------------------------------
# TestJsonRpcParsing
# ---------------------------------------------------------------------------

class TestJsonRpcParsing:
    """parse_jsonrpc_request validates JSON-RPC 2.0 envelope."""

    def test_valid_request(self):
        parsed = parse_jsonrpc_request(_req("tools/list"))
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "tools/list"
        assert parsed["id"] == "req-1"

    def test_non_json_body_raises_parse_error(self):
        with pytest.raises(JsonRpcError) as exc_info:
            parse_jsonrpc_request(b"not json at all")
        assert exc_info.value.code == -32700

    def test_empty_body_raises_parse_error(self):
        with pytest.raises(JsonRpcError) as exc_info:
            parse_jsonrpc_request(b"")
        assert exc_info.value.code == -32700

    def test_missing_jsonrpc_field_raises_invalid_request(self):
        body = json.dumps({"id": "1", "method": "initialize"}).encode()
        with pytest.raises(JsonRpcError) as exc_info:
            parse_jsonrpc_request(body)
        assert exc_info.value.code == -32600

    def test_wrong_jsonrpc_version_raises_invalid_request(self):
        body = json.dumps({"jsonrpc": "1.0", "id": "1", "method": "initialize"}).encode()
        with pytest.raises(JsonRpcError) as exc_info:
            parse_jsonrpc_request(body)
        assert exc_info.value.code == -32600

    def test_missing_method_raises_invalid_request(self):
        body = json.dumps({"jsonrpc": "2.0", "id": "1"}).encode()
        with pytest.raises(JsonRpcError) as exc_info:
            parse_jsonrpc_request(body)
        assert exc_info.value.code == -32600

    def test_missing_id_raises_invalid_request(self):
        body = json.dumps({"jsonrpc": "2.0", "method": "initialize"}).encode()
        with pytest.raises(JsonRpcError) as exc_info:
            parse_jsonrpc_request(body)
        assert exc_info.value.code == -32600

    def test_non_string_method_raises_invalid_request(self):
        body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": 42}).encode()
        with pytest.raises(JsonRpcError) as exc_info:
            parse_jsonrpc_request(body)
        assert exc_info.value.code == -32600

    def test_params_default_to_empty_dict(self):
        parsed = parse_jsonrpc_request(_req("initialize"))
        assert parsed.get("params") is None


# ---------------------------------------------------------------------------
# TestEnvelopeBuilders
# ---------------------------------------------------------------------------

class TestEnvelopeBuilders:
    """build_success and build_error produce correct JSON-RPC 2.0 envelopes."""

    def test_success_envelope(self):
        env = build_success("req-123", {"tools": []})
        assert env == {
            "jsonrpc": "2.0",
            "id": "req-123",
            "result": {"tools": []},
        }

    def test_error_envelope(self):
        env = build_error("req-123", -32601, "Method not found", {"method": "foo/bar"})
        assert env == {
            "jsonrpc": "2.0",
            "id": "req-123",
            "error": {
                "code": -32601,
                "message": "Method not found",
                "data": {"method": "foo/bar"},
            },
        }

    def test_error_envelope_without_data(self):
        env = build_error("req-123", -32603, "Internal error")
        assert env["error"]["code"] == -32603
        assert "data" not in env["error"]

    def test_success_envelope_preserves_id(self):
        env = build_success("abc-999", {})
        assert env["id"] == "abc-999"


# ---------------------------------------------------------------------------
# TestInitialize
# ---------------------------------------------------------------------------

class TestInitialize:
    """initialize method returns protocol version, server info, capabilities."""

    def test_returns_protocol_version(self):
        router = _make_router()
        status, resp = router.dispatch(_req("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
            "capabilities": {},
        }))
        assert status == 200
        result = resp["result"]
        assert result["protocolVersion"] == "2024-11-05"

    def test_returns_server_info(self):
        router = _make_router(server_name="my-server", server_version="2.0.0")
        _, resp = router.dispatch(_req("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "c", "version": "1.0.0"},
            "capabilities": {},
        }))
        assert resp["result"]["serverInfo"] == {"name": "my-server", "version": "2.0.0"}

    def test_returns_capabilities(self):
        router = _make_router()
        _, resp = router.dispatch(_req("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "c", "version": "1.0.0"},
            "capabilities": {},
        }))
        caps = resp["result"]["capabilities"]
        assert "tools" in caps
        assert "resources" in caps
        assert "prompts" in caps


# ---------------------------------------------------------------------------
# TestToolsList
# ---------------------------------------------------------------------------

class TestToolsList:
    """tools/list returns registered tools with name, description, inputSchema."""

    def test_empty_tools_list(self):
        router = _make_router()
        _, resp = router.dispatch(_req("tools/list"))
        assert resp["result"] == {"tools": []}

    def test_returns_registered_tool(self):
        router = _make_router()
        router.register_tool(ToolDef(
            name="weather.get_forecast",
            description="Get forecast by city",
            input_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            handler=lambda args: [{"type": "text", "text": "sunny"}],
        ))
        _, resp = router.dispatch(_req("tools/list"))
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "weather.get_forecast"
        assert tools[0]["description"] == "Get forecast by city"
        assert tools[0]["inputSchema"]["required"] == ["city"]

    def test_multiple_tools(self):
        router = _make_router()
        for name in ["tool_a", "tool_b", "tool_c"]:
            router.register_tool(ToolDef(
                name=name, description=f"Desc {name}",
                input_schema={"type": "object"}, handler=lambda args: [],
            ))
        _, resp = router.dispatch(_req("tools/list"))
        assert len(resp["result"]["tools"]) == 3


# ---------------------------------------------------------------------------
# TestToolsCall
# ---------------------------------------------------------------------------

class TestToolsCall:
    """tools/call invokes a registered tool and returns content."""

    def test_call_returns_content(self):
        router = _make_router()
        router.register_tool(ToolDef(
            name="weather.get_forecast",
            description="Get forecast",
            input_schema={"type": "object"},
            handler=lambda args: [{"type": "text", "text": f"72°F in {args.get('city', '?')}"}],
        ))
        _, resp = router.dispatch(_req("tools/call", {
            "name": "weather.get_forecast",
            "arguments": {"city": "San Francisco"},
        }))
        content = resp["result"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert "72°F" in content[0]["text"]

    def test_unknown_tool_returns_invalid_params(self):
        router = _make_router()
        status, resp = router.dispatch(_req("tools/call", {
            "name": "nonexistent_tool",
            "arguments": {},
        }))
        assert status == 200
        assert resp["error"]["code"] == -32602
        assert "nonexistent_tool" in resp["error"]["message"]

    def test_missing_name_param_returns_invalid_params(self):
        router = _make_router()
        status, resp = router.dispatch(_req("tools/call", {}))
        assert status == 200
        assert resp["error"]["code"] == -32602

    def test_non_object_arguments_return_invalid_params(self):
        router = _make_router()
        router.register_tool(ToolDef(
            name="weather.get_forecast",
            description="Get forecast",
            input_schema={"type": "object"},
            handler=lambda args: [{"type": "text", "text": "ok"}],
        ))
        status, resp = router.dispatch(_req("tools/call", {
            "name": "weather.get_forecast",
            "arguments": ["bad"],
        }))
        assert status == 200
        assert resp["error"]["code"] == -32602

    def test_tool_handler_exception_returns_internal_error(self):
        def explode(args):
            raise RuntimeError("boom")

        router = _make_router()
        router.register_tool(ToolDef(
            name="bad_tool", description="Broken",
            input_schema={"type": "object"}, handler=explode,
        ))
        status, resp = router.dispatch(_req("tools/call", {
            "name": "bad_tool", "arguments": {},
        }))
        assert status == 200
        assert resp["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# TestResourcesList
# ---------------------------------------------------------------------------

class TestResourcesList:
    """resources/list returns registered resource descriptors."""

    def test_empty_resources_list(self):
        router = _make_router()
        _, resp = router.dispatch(_req("resources/list"))
        assert resp["result"] == {"resources": []}

    def test_returns_registered_resource(self):
        router = _make_router()
        router.register_resource(ResourceDef(
            uri="file:///config.json",
            name="Config",
            description="App configuration",
            mime_type="application/json",
            handler=lambda: [{"type": "text", "text": "{}"}],
        ))
        _, resp = router.dispatch(_req("resources/list"))
        resources = resp["result"]["resources"]
        assert len(resources) == 1
        assert resources[0]["uri"] == "file:///config.json"
        assert resources[0]["name"] == "Config"


# ---------------------------------------------------------------------------
# TestResourcesRead
# ---------------------------------------------------------------------------

class TestResourcesRead:
    """resources/read returns typed content for a valid URI."""

    def test_read_returns_content(self):
        router = _make_router()
        router.register_resource(ResourceDef(
            uri="file:///config.json",
            name="Config",
            description="App configuration",
            mime_type="application/json",
            handler=lambda: [{"type": "text", "text": '{"key": "value"}'}],
        ))
        _, resp = router.dispatch(_req("resources/read", {"uri": "file:///config.json"}))
        content = resp["result"]["contents"]
        assert len(content) == 1
        assert content[0]["type"] == "text"

    def test_unknown_uri_returns_invalid_params(self):
        router = _make_router()
        status, resp = router.dispatch(_req("resources/read", {"uri": "file:///nope"}))
        assert status == 200
        assert resp["error"]["code"] == -32602

    def test_missing_uri_param_returns_invalid_params(self):
        router = _make_router()
        status, resp = router.dispatch(_req("resources/read", {}))
        assert status == 200
        assert resp["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# TestPromptsList
# ---------------------------------------------------------------------------

class TestPromptsList:
    """prompts/list returns prompt template metadata."""

    def test_empty_prompts_list(self):
        router = _make_router()
        _, resp = router.dispatch(_req("prompts/list"))
        assert resp["result"] == {"prompts": []}

    def test_returns_registered_prompt(self):
        router = _make_router()
        router.register_prompt(PromptDef(
            name="greeting",
            description="A friendly greeting",
            arguments=[{"name": "name", "description": "Who to greet", "required": True}],
            handler=lambda args: [{"role": "user", "content": {"type": "text", "text": f"Hi {args['name']}"}}],
        ))
        _, resp = router.dispatch(_req("prompts/list"))
        prompts = resp["result"]["prompts"]
        assert len(prompts) == 1
        assert prompts[0]["name"] == "greeting"
        assert prompts[0]["arguments"][0]["name"] == "name"


# ---------------------------------------------------------------------------
# TestPromptsGet
# ---------------------------------------------------------------------------

class TestPromptsGet:
    """prompts/get returns resolved prompt messages."""

    def test_get_returns_messages(self):
        router = _make_router()
        router.register_prompt(PromptDef(
            name="greeting",
            description="Greet",
            arguments=[{"name": "name", "required": True}],
            handler=lambda args: [{"role": "user", "content": {"type": "text", "text": f"Hi {args['name']}"}}],
        ))
        _, resp = router.dispatch(_req("prompts/get", {
            "name": "greeting", "arguments": {"name": "World"},
        }))
        messages = resp["result"]["messages"]
        assert len(messages) == 1
        assert "World" in messages[0]["content"]["text"]

    def test_unknown_prompt_returns_invalid_params(self):
        router = _make_router()
        status, resp = router.dispatch(_req("prompts/get", {"name": "nope"}))
        assert status == 200
        assert resp["error"]["code"] == -32602

    def test_missing_name_param_returns_invalid_params(self):
        router = _make_router()
        status, resp = router.dispatch(_req("prompts/get", {}))
        assert status == 200
        assert resp["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# TestMethodNotFound
# ---------------------------------------------------------------------------

class TestMethodNotFound:
    """Unknown method returns -32601 with method name in data."""

    def test_unknown_method(self):
        router = _make_router()
        status, resp = router.dispatch(_req("foo/bar"))
        assert status == 200
        assert resp["error"]["code"] == -32601
        assert resp["error"]["data"]["method"] == "foo/bar"

    def test_empty_method(self):
        router = _make_router()
        body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": ""}).encode()
        # Empty method is still technically valid JSON-RPC but we don't have a handler
        status, resp = router.dispatch(body)
        assert resp["error"]["code"] in (-32600, -32601)


# ---------------------------------------------------------------------------
# TestErrorSemantics
# ---------------------------------------------------------------------------

class TestErrorSemantics:
    """All 5 standard JSON-RPC error codes produce correct code and message."""

    def test_parse_error_code(self):
        router = _make_router()
        status, resp = router.dispatch(b"not json")
        assert resp["error"]["code"] == -32700
        assert "parse" in resp["error"]["message"].lower()

    def test_invalid_request_code(self):
        router = _make_router()
        body = json.dumps({"jsonrpc": "1.0", "id": "1", "method": "x"}).encode()
        status, resp = router.dispatch(body)
        assert resp["error"]["code"] == -32600

    def test_method_not_found_code(self):
        router = _make_router()
        status, resp = router.dispatch(_req("nonexistent/method"))
        assert resp["error"]["code"] == -32601

    def test_invalid_params_code(self):
        router = _make_router()
        status, resp = router.dispatch(_req("tools/call", {}))
        assert resp["error"]["code"] == -32602

    def test_internal_error_code(self):
        def explode(args):
            raise RuntimeError("boom")

        router = _make_router()
        router.register_tool(ToolDef(
            name="exploder", description="Breaks",
            input_schema={"type": "object"}, handler=explode,
        ))
        status, resp = router.dispatch(_req("tools/call", {
            "name": "exploder", "arguments": {},
        }))
        assert resp["error"]["code"] == -32603
        # Fix #4: must NOT leak raw exception text
        assert "boom" not in resp["error"]["message"]
        assert resp["error"]["message"] == "Internal error"


# ---------------------------------------------------------------------------
# TestHttpStatusGuidance
# ---------------------------------------------------------------------------

class TestHttpStatusGuidance:
    """JSON-RPC errors return HTTP 200; transport failures return 4xx."""

    def test_jsonrpc_error_returns_http_200(self):
        router = _make_router()
        status, resp = router.dispatch(_req("nonexistent/method"))
        assert status == 200
        assert "error" in resp

    def test_parse_error_returns_http_200(self):
        router = _make_router()
        status, resp = router.dispatch(b"not json")
        assert status == 200

    def test_success_returns_http_200(self):
        router = _make_router()
        status, resp = router.dispatch(_req("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "c", "version": "1.0.0"},
            "capabilities": {},
        }))
        assert status == 200
        assert "result" in resp


# ---------------------------------------------------------------------------
# TestParamsValidation
# ---------------------------------------------------------------------------

class TestParamsValidation:
    """Non-dict params returns -32602 INVALID_PARAMS."""

    def test_list_params_returns_invalid_params(self):
        """params as a JSON array should be rejected with -32602."""
        router = _make_router()
        body = json.dumps({
            "jsonrpc": "2.0", "id": "req-1",
            "method": "tools/list", "params": [1, 2, 3],
        }).encode()
        status, resp = router.dispatch(body)
        assert status == 200
        assert resp["error"]["code"] == -32602

    def test_string_params_returns_invalid_params(self):
        """params as a string should be rejected with -32602."""
        router = _make_router()
        body = json.dumps({
            "jsonrpc": "2.0", "id": "req-1",
            "method": "tools/list", "params": "bad",
        }).encode()
        status, resp = router.dispatch(body)
        assert status == 200
        assert resp["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# TestReqIdPreservation
# ---------------------------------------------------------------------------

class TestReqIdPreservation:
    """Error responses echo back the detectable request id."""

    def test_invalid_request_preserves_detectable_id(self):
        """Even when jsonrpc version is wrong, the id should be echoed."""
        router = _make_router()
        body = json.dumps({"jsonrpc": "1.0", "id": "abc-123", "method": "x"}).encode()
        status, resp = router.dispatch(body)
        assert status == 200
        assert resp["error"]["code"] == -32600
        assert resp["id"] == "abc-123"

    def test_missing_method_preserves_id(self):
        """Missing method should still echo the id."""
        router = _make_router()
        body = json.dumps({"jsonrpc": "2.0", "id": "xyz-789"}).encode()
        status, resp = router.dispatch(body)
        assert status == 200
        assert resp["error"]["code"] == -32600
        assert resp["id"] == "xyz-789"


# ---------------------------------------------------------------------------
# TestAuthMiddleware
# ---------------------------------------------------------------------------

class TestAuthMiddleware:
    """Auth failures return HTTP 401 before JSON-RPC handling."""

    def test_no_auth_configured_allows_all(self):
        router = _make_router(auth_token=None)
        status, resp = router.dispatch(_req("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "c", "version": "1.0.0"},
            "capabilities": {},
        }))
        assert status == 200
        assert "result" in resp

    def test_missing_token_returns_401(self):
        router = _make_router(auth_token="secret-token")
        status, resp = router.dispatch(_req("initialize"), auth_token=None)
        assert status == 401
        assert "error" in resp

    def test_wrong_token_returns_401(self):
        router = _make_router(auth_token="secret-token")
        status, resp = router.dispatch(_req("initialize"), auth_token="wrong-token")
        assert status == 401

    def test_correct_token_returns_200(self):
        router = _make_router(auth_token="secret-token")
        status, resp = router.dispatch(_req("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "c", "version": "1.0.0"},
            "capabilities": {},
        }), auth_token="secret-token")
        assert status == 200
        assert "result" in resp

    def test_auth_failure_body_is_valid_json(self):
        router = _make_router(auth_token="secret-token")
        status, resp = router.dispatch(_req("tools/list"), auth_token=None)
        assert status == 401
        # Response should still be serializable JSON
        json.dumps(resp)
