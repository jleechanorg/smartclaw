"""
test_cmux_client.py — Unit tests for cmux_client.py

Run: python -m pytest tests/test_cmux_client.py -v
"""
import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add skills/cmux/scripts to path for import
sys.path.insert(0, str(Path(__file__).parent.parent / "skills/cmux/scripts"))
from cmux_client import rpc, SOCKET_PATH


class TestRpc:
    def test_rpc_sends_correct_jsonrpc_payload(self):
        """rpc() formats a valid JSON-RPC 2.0 request."""
        with patch('cmux_client.socket.socket') as mock_sock_class:
            mock_sock = MagicMock()
            mock_sock_class.return_value.__enter__.return_value = mock_sock
            mock_sock.recv.return_value = json.dumps({"id": 1, "result": {}}).encode()

            result = rpc("workspace.list")

            mock_sock.connect.assert_called_once_with(SOCKET_PATH)
            sent = json.loads(mock_sock.sendall.call_args[0][0].decode())
            assert sent["method"] == "workspace.list"
            assert sent["id"] == 1
            assert "params" in sent

    def test_rpc_returns_error_on_connection_failure(self):
        """Connection refused / file-not-found returns error dict."""
        with patch('cmux_client.socket.socket') as mock_sock_class:
            mock_sock = MagicMock()
            mock_sock_class.return_value.__enter__.return_value = mock_sock
            mock_sock.connect.side_effect = FileNotFoundError("no socket")

            result = rpc("workspace.list")

            assert "error" in result
            assert "Socket not available" in result["error"]

    def test_rpc_parses_json_response(self):
        """rpc() returns parsed Python dict from JSON response."""
        with patch('cmux_client.socket') as mock_module:
            mock_sock = MagicMock()
            mock_module.socket.return_value.__enter__.return_value = mock_sock
            mock_response = {"id": 1, "result": {"workspaces": ["w1", "w2"]}}
            mock_sock.recv.return_value = json.dumps(mock_response).encode()

            result = rpc("workspace.list")

            assert result == mock_response

    def test_rpc_with_custom_req_id(self):
        """rpc() accepts and forwards custom req_id."""
        with patch('cmux_client.socket.socket') as mock_sock_class:
            mock_sock = MagicMock()
            mock_sock_class.return_value.__enter__.return_value = mock_sock
            mock_sock.recv.return_value = json.dumps({"id": 99, "result": {}}).encode()

            rpc("workspace.list", req_id=99)

            sent = json.loads(mock_sock.sendall.call_args[0][0].decode())
            assert sent["id"] == 99

    def test_socket_path_default(self):
        """SOCKET_PATH uses env var CMUX_SOCKET_PATH with /tmp/cmux.sock fallback."""
        # Default in source is /tmp/cmux.sock
        assert SOCKET_PATH == "/tmp/cmux.sock" or SOCKET_PATH == os.environ.get("CMUX_SOCKET_PATH", "/tmp/cmux.sock")

    def test_rpc_sends_params(self):
        """rpc() forwards params dict in JSON-RPC params field."""
        with patch('cmux_client.socket.socket') as mock_sock_class:
            mock_sock = MagicMock()
            mock_sock_class.return_value.__enter__.return_value = mock_sock
            mock_sock.recv.return_value = json.dumps({"id": 1, "result": {}}).encode()

            rpc("surface.send_text", params={"surface_id": "surface:5", "text": "hello"})

            sent = json.loads(mock_sock.sendall.call_args[0][0].decode())
            assert sent["params"] == {"surface_id": "surface:5", "text": "hello"}
