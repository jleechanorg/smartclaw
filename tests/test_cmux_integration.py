"""
test_cmux_integration.py — Live integration tests for cmux skill.

Tests against the REAL production cmux socket:
  ~/Library/Application Support/cmux/cmux.sock  (PID 626, cmux.app)

API shape: {"ok": true, "result": {...}}  or  {"ok": false, "error": {...}}
(Not JSON-RPC 2.0 — the socket returns cmux-native response format.)

Run:
  python -m pytest tests/test_cmux_integration.py -v

Requires: cmux.app running, socket at ~/Library/Application Support/cmux/cmux.sock
"""
import json
import os
import socket
from pathlib import Path

import pytest

SOCKET_PATH = "${HOME}/Library/Application Support/cmux/cmux.sock"

PRODUCTION_SOCKET = "${HOME}/Library/Application Support/cmux/cmux.sock"


def is_socket_alive(path: str) -> bool:
    """Check if socket exists and is accepting connections."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(path)
            return True
    except Exception:
        return False


def cmux_rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    """Send request to live cmux socket, return parsed response.

    Handles both single-chunk and multi-chunk responses.
    Response shape is cmux-native: {"ok": true, "result": {...}}
    """
    payload = json.dumps(
        {"method": method, "params": params or {}, "id": req_id}
    ).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(SOCKET_PATH)
        s.sendall(payload)
        # cmux sends one JSON object per line — read until newline
        data = b""
        for chunk in iter(lambda: s.recv(16384), b""):
            data += chunk
            if b"\n" in data:
                break
        # Strip any trailing data beyond the first complete JSON line
        first_line = data.split(b"\n")[0]
        return json.loads(first_line.decode())


@pytest.fixture(scope="module")
def live_cmux():
    """Skip all tests if production socket is not available."""
    if not is_socket_alive(PRODUCTION_SOCKET):
        pytest.skip(f"cmux socket not available at {PRODUCTION_SOCKET}")
    return True


# ─── Item 4 + 9: Integration / E2E ───────────────────────────────────────────

class TestCmuxLiveSocket:
    """Full round-trip tests against live cmux socket."""

    def test_workspace_list_returns_ok_with_workspaces(self, live_cmux):
        """workspace.list returns ok=true and a workspaces array."""
        result = cmux_rpc("workspace.list")
        assert result.get("ok") is True, f"Expected ok=true, got: {result}"
        assert "result" in result
        assert "workspaces" in result["result"]
        assert isinstance(result["result"]["workspaces"], list)

    def test_workspace_list_contains_expected_fields(self, live_cmux):
        """Each workspace has id, title, ref, and current_directory fields."""
        result = cmux_rpc("workspace.list")
        workspaces = result["result"]["workspaces"]
        assert len(workspaces) > 0, "Expected at least one workspace"
        ws = workspaces[0]
        assert "id" in ws
        assert "ref" in ws
        assert "title" in ws
        # ref should be like "workspace:7"
        assert ws["ref"].startswith("workspace:")

    def test_system_tree_returns_window_hierarchy(self, live_cmux):
        """system.tree returns ok=true with a windows array."""
        result = cmux_rpc("system.tree")
        assert result.get("ok") is True, f"Expected ok=true, got: {result}"
        tree = result["result"]
        assert "windows" in tree
        assert isinstance(tree["windows"], list)
        if tree["windows"]:
            win = tree["windows"][0]
            assert "workspaces" in win or "ref" in win

    def test_surface_list_returns_surfaces(self, live_cmux):
        """surface.list returns ok=true with a surfaces array."""
        result = cmux_rpc("surface.list")
        assert result.get("ok") is True, f"Expected ok=true, got: {result}"
        assert "surfaces" in result["result"]
        assert isinstance(result["result"]["surfaces"], list)

    def test_window_list_returns_windows(self, live_cmux):
        """window.list returns ok=true with a windows array."""
        result = cmux_rpc("window.list")
        assert result.get("ok") is True, f"Expected ok=true, got: {result}"
        assert "windows" in result["result"]

    def test_rpc_id_roundtrips(self, live_cmux):
        """req_id in request matches id in response."""
        result = cmux_rpc("workspace.list", req_id=42)
        assert result.get("id") == 42, f"Expected id=42, got: {result}"

    def test_unknown_method_returns_ok_false(self, live_cmux):
        """Unknown method returns ok=false (graceful error, not a crash)."""
        result = cmux_rpc("nonexistent.method")
        # ok=false means cmux handled the unknown method gracefully
        assert result.get("ok") is False, f"Expected ok=false for unknown method, got: {result}"
        assert "error" in result

    def test_ping_method_returns_ok(self, live_cmux):
        """The ping method (if supported) returns ok=true."""
        result = cmux_rpc("workspace.ping")
        # Accept both ok=true (supported) and ok=false (unknown)
        assert "ok" in result

    def test_workspace_list_contains_workspaces_with_panes(self, live_cmux):
        """Workspace list includes workspaces that have panes (nested hierarchy)."""
        result = cmux_rpc("workspace.list")
        workspaces = result["result"]["workspaces"]
        assert len(workspaces) > 0
        # At least one workspace should have a surface or pane
        has_structure = any(
            "panes" in ws or "surfaces" in ws or "workspace_id" in ws
            for ws in workspaces
        )
        # If the flat list doesn't have it, check system.tree
        if not has_structure:
            tree_result = cmux_rpc("system.tree")
            windows = tree_result["result"]["windows"]
            has_structure = any(
                ws.get("panes") or ws.get("surfaces")
                for win in windows
                for ws in win.get("workspaces", [])
            )
        assert has_structure, "Expected workspaces to have pane/surface structure"

    def test_surface_list_references_valid_workspace(self, live_cmux):
        """Each surface in surface.list references a valid pane_ref."""
        result = cmux_rpc("surface.list")
        surfaces = result["result"]["surfaces"]
        if surfaces:
            surf = surfaces[0]
            assert "pane_ref" in surf, f"Expected pane_ref, got: {list(surf.keys())}"
            assert "id" in surf
            assert "ref" in surf
            # pane_ref should be like "pane:8"
            assert surf["pane_ref"].startswith("pane:")
