"""E2E integration test: MCP JSON-RPC router over real HTTP.

Starts a real HTTP server with the McpRouter, sends curl-equivalent requests,
and verifies correct JSON-RPC responses end-to-end. No mocks.

Also tests gateway connectivity at localhost:18789 if available.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from collections.abc import Generator
from typing import Any
from urllib.request import Request, urlopen

import pytest

from orchestration.mcp_http import McpRouter, ToolDef, ResourceDef, PromptDef


# ---------------------------------------------------------------------------
# Fixtures: real HTTP server backed by McpRouter
# ---------------------------------------------------------------------------

class McpHttpHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that delegates POST /mcp to an McpRouter."""

    router: McpRouter  # Set by the fixture

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "Not found"}')
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Extract auth token
        auth_header = self.headers.get("Authorization", "")
        auth_token = None
        if auth_header.startswith("Bearer "):
            auth_token = auth_header[7:]

        status, resp = self.router.dispatch(body, auth_token=auth_token)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # Suppress server logs during tests


@pytest.fixture
def mcp_server() -> Generator[tuple[str, McpRouter], None, None]:
    """Start a real HTTP server with an McpRouter and yield its base URL."""
    router = McpRouter(
        server_name="e2e-test-server",
        server_version="0.1.0",
        auth_token="test-secret",
    )

    # Register a real tool
    def get_forecast(args: dict[str, Any]) -> list[dict[str, Any]]:
        city = args.get("city", "Unknown")
        return [{"type": "text", "text": f"72°F and sunny in {city}"}]

    router.register_tool(ToolDef(
        name="weather.get_forecast",
        description="Get forecast by city",
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        handler=get_forecast,
    ))

    # Register a real resource
    router.register_resource(ResourceDef(
        uri="config://app",
        name="App Config",
        description="Application configuration",
        mime_type="application/json",
        handler=lambda: [{"type": "text", "text": '{"env": "test"}'}],
    ))

    # Register a real prompt
    router.register_prompt(PromptDef(
        name="greet",
        description="Greeting prompt",
        arguments=[{"name": "name", "required": True}],
        handler=lambda args: [{"role": "user", "content": {"type": "text", "text": f"Hello {args['name']}"}}],
    ))

    class BoundMcpHttpHandler(McpHttpHandler):
        pass

    BoundMcpHttpHandler.router = router
    server = http.server.HTTPServer(("127.0.0.1", 0), BoundMcpHttpHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}", router

    server.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_mcp(base_url: str, body: dict, token: str | None = "test-secret") -> tuple[int, dict]:
    """POST a JSON-RPC request to the MCP endpoint. Returns (status, response)."""
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{base_url}/mcp", data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except Exception as exc:
        if hasattr(exc, "code"):
            return exc.code, json.loads(exc.read())
        raise


# ---------------------------------------------------------------------------
# E2E Tests: Real HTTP → McpRouter → Response
# ---------------------------------------------------------------------------

class TestE2EInitialize:
    """End-to-end: initialize via real HTTP POST."""

    def test_initialize_returns_server_info(self, mcp_server):
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-1", "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "e2e-client", "version": "1.0.0"},
                "capabilities": {},
            },
        })
        assert status == 200
        assert resp["result"]["serverInfo"]["name"] == "e2e-test-server"
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert resp["id"] == "e2e-1"


class TestE2EToolsRoundtrip:
    """End-to-end: tools/list and tools/call via real HTTP."""

    def test_list_tools(self, mcp_server):
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-2", "method": "tools/list", "params": {},
        })
        assert status == 200
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "weather.get_forecast"

    def test_call_tool(self, mcp_server):
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-3", "method": "tools/call",
            "params": {"name": "weather.get_forecast", "arguments": {"city": "San Francisco"}},
        })
        assert status == 200
        content = resp["result"]["content"]
        assert "72°F" in content[0]["text"]
        assert "San Francisco" in content[0]["text"]

    def test_call_unknown_tool(self, mcp_server):
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-4", "method": "tools/call",
            "params": {"name": "nonexistent", "arguments": {}},
        })
        assert status == 200
        assert resp["error"]["code"] == -32602


class TestE2EResourcesRoundtrip:
    """End-to-end: resources/list and resources/read via real HTTP."""

    def test_list_and_read_resource(self, mcp_server):
        base_url, _ = mcp_server
        # List
        _, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-5", "method": "resources/list", "params": {},
        })
        assert len(resp["result"]["resources"]) == 1
        uri = resp["result"]["resources"][0]["uri"]

        # Read
        _, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-6", "method": "resources/read",
            "params": {"uri": uri},
        })
        assert '"env"' in resp["result"]["contents"][0]["text"]


class TestE2EPromptsRoundtrip:
    """End-to-end: prompts/list and prompts/get via real HTTP."""

    def test_list_and_get_prompt(self, mcp_server):
        base_url, _ = mcp_server
        # List
        _, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-7", "method": "prompts/list", "params": {},
        })
        assert resp["result"]["prompts"][0]["name"] == "greet"

        # Get
        _, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-8", "method": "prompts/get",
            "params": {"name": "greet", "arguments": {"name": "World"}},
        })
        assert "World" in resp["result"]["messages"][0]["content"]["text"]


class TestE2EErrorHandling:
    """End-to-end: error paths via real HTTP."""

    def test_method_not_found(self, mcp_server):
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-9", "method": "unknown/method", "params": {},
        })
        assert status == 200
        assert resp["error"]["code"] == -32601
        assert resp["error"]["data"]["method"] == "unknown/method"

    def test_parse_error(self, mcp_server):
        base_url, _ = mcp_server
        data = b"not json at all"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer test-secret",
        }
        req = Request(f"{base_url}/mcp", data=data, headers=headers, method="POST")
        with urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
        assert resp["error"]["code"] == -32700

    def test_auth_failure_returns_401(self, mcp_server):
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "e2e-10", "method": "tools/list", "params": {},
        }, token="wrong-token")
        assert status == 401


class TestE2ECurlEquivalence:
    """End-to-end: curl examples from the spec actually work."""

    def test_spec_initialize_example(self, mcp_server):
        """Mirrors the curl example from the spec doc."""
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "1", "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "curl-client", "version": "1.0.0"},
                "capabilities": {},
            },
        })
        assert status == 200
        assert "result" in resp
        assert resp["result"]["protocolVersion"] == "2024-11-05"

    def test_spec_list_tools_example(self, mcp_server):
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "2", "method": "tools/list", "params": {},
        })
        assert status == 200
        assert "tools" in resp["result"]

    def test_spec_call_tool_example(self, mcp_server):
        base_url, _ = mcp_server
        status, resp = _post_mcp(base_url, {
            "jsonrpc": "2.0", "id": "3", "method": "tools/call",
            "params": {"name": "weather.get_forecast", "arguments": {"city": "San Francisco"}},
        })
        assert status == 200
        assert resp["result"]["content"][0]["type"] == "text"


# ---------------------------------------------------------------------------
# E2E: Gateway connectivity (optional — requires live gateway)
# ---------------------------------------------------------------------------

GATEWAY_URL = "http://localhost:18789"
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")


@pytest.mark.integration
@pytest.mark.skipif(not GATEWAY_TOKEN, reason="OPENCLAW_GATEWAY_TOKEN not set")
class TestGatewayConnectivity:
    """Smoke: verify the OpenClaw gateway is up and reachable."""

    def test_gateway_responds(self):
        """Gateway returns valid HTTP response (even if 405 for GET)."""
        req = Request(
            f"{GATEWAY_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
        )
        try:
            with urlopen(req, timeout=5) as resp:
                assert resp.status in (200, 405)
        except Exception as exc:
            if hasattr(exc, "code"):
                # 405 Method Not Allowed is expected for GET
                assert exc.code == 405
            else:
                pytest.skip(f"Gateway unreachable: {exc}")
