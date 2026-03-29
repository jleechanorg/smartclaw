#!/usr/bin/env python3
"""cmux socket client helper."""
import json
import os
import socket
import sys
import tempfile

_default_sock = os.environ.get("XDG_RUNTIME_DIR")
if _default_sock:
    _default_sock = os.path.join(_default_sock, "cmux.sock")
else:
    _default_sock = os.path.join(tempfile.gettempdir(), "cmux.sock")
SOCKET_PATH = os.environ.get("CMUX_SOCKET_PATH", _default_sock)


def rpc(method, params=None, req_id=1):
    """Send a JSON-RPC request to cmux socket."""
    payload = {"id": req_id, "method": method, "params": params or {}}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            # Accumulate bytes until newline delimiter is present in buffer, then
            # split at the first newline so only that JSON line is decoded.
            buffer = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                if b"\n" in buffer:
                    break
            line = buffer.split(b"\n", 1)[0].decode("utf-8")
            return json.loads(line)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        return {"error": f"Socket not available: {SOCKET_PATH}", "details": str(e)}


def main():
    if len(sys.argv) < 2:
        print("Usage: cmux_client.py <method> [params_json]")
        print("Example: cmux_client.py workspace.list")
        print("         cmux_client.py surface.send_text '{\"text\": \"ls\\n\"}'")
        sys.exit(1)

    method = sys.argv[1]
    params = {}
    if len(sys.argv) > 2:
        try:
            params = json.loads(sys.argv[2])
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Error: invalid JSON in params argument: {e}", file=sys.stderr)
            sys.exit(1)

    result = rpc(method, params)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
