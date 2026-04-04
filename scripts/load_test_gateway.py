#!/usr/bin/env python3
"""Simple load tester for OpenClaw gateway endpoints.

Supports:
- health mode: GET /health
- chat mode: POST /v1/chat/completions (non-streaming)
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_URL = "http://127.0.0.1:18789"
DEFAULT_CONFIG = Path.home() / ".openclaw" / "openclaw.json"


@dataclass
class RequestResult:
    ok: bool
    status: int
    latency_ms: float
    error: str = ""


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    index = min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1))))
    return sorted_vals[index]


def load_token(explicit_token: str | None) -> str:
    if explicit_token:
        return explicit_token
    env_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if env_token:
        return env_token
    if DEFAULT_CONFIG.exists():
        data = json.loads(DEFAULT_CONFIG.read_text())
        token = str(data.get("gateway", {}).get("auth", {}).get("token", "")).strip()
        if token and not token.startswith("${"):
            return token
    raise RuntimeError("Missing gateway token. Set OPENCLAW_GATEWAY_TOKEN or pass --token.")


def build_health_request(base_url: str, token: str, timeout_s: float) -> urllib.request.Request:
    req = urllib.request.Request(f"{base_url.rstrip('/')}/health", method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.timeout = timeout_s
    return req


def build_chat_request(base_url: str, token: str, timeout_s: float) -> urllib.request.Request:
    payload = {
        "model": "openai-codex/gpt-5.3-codex",
        "messages": [{"role": "user", "content": "Reply with exactly OK"}],
        "stream": False,
        "max_tokens": 8,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=body,
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.timeout = timeout_s
    return req


def execute_request(req: urllib.request.Request) -> RequestResult:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=req.timeout) as resp:
            _ = resp.read()
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return RequestResult(ok=200 <= resp.status < 300, status=resp.status, latency_ms=elapsed_ms)
    except urllib.error.HTTPError as err:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return RequestResult(ok=False, status=err.code, latency_ms=elapsed_ms, error=f"HTTPError: {err.reason}")
    except Exception as err:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return RequestResult(ok=False, status=0, latency_ms=elapsed_ms, error=f"{type(err).__name__}: {err}")


def run_load(
    base_url: str,
    token: str,
    mode: str,
    total_requests: int,
    concurrency: int,
    timeout_s: float,
) -> dict[str, Any]:
    tasks: queue.Queue[int] = queue.Queue()
    results: list[RequestResult] = []
    lock = threading.Lock()

    for i in range(total_requests):
        tasks.put(i)

    def worker() -> None:
        while True:
            try:
                _ = tasks.get_nowait()
            except queue.Empty:
                return
            req = (
                build_health_request(base_url, token, timeout_s)
                if mode == "health"
                else build_chat_request(base_url, token, timeout_s)
            )
            result = execute_request(req)
            with lock:
                results.append(result)
            tasks.task_done()

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    duration_s = max(0.0001, time.perf_counter() - started)

    latencies = [r.latency_ms for r in results]
    status_counts = Counter(r.status for r in results)
    errors = [r.error for r in results if r.error]

    return {
        "mode": mode,
        "base_url": base_url,
        "total_requests": total_requests,
        "concurrency": concurrency,
        "duration_s": round(duration_s, 4),
        "throughput_rps": round(len(results) / duration_s, 2),
        "success_count": sum(1 for r in results if r.ok),
        "failure_count": sum(1 for r in results if not r.ok),
        "status_counts": dict(sorted(status_counts.items())),
        "latency_ms": {
            "min": round(min(latencies), 2) if latencies else 0.0,
            "mean": round(statistics.mean(latencies), 2) if latencies else 0.0,
            "p50": round(percentile(latencies, 50), 2),
            "p90": round(percentile(latencies, 90), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "max": round(max(latencies), 2) if latencies else 0.0,
        },
        "error_samples": errors[:10],
        "started_at_epoch": int(time.time() - duration_s),
        "finished_at_epoch": int(time.time()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load test OpenClaw gateway.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Gateway base URL.")
    parser.add_argument("--mode", choices=["health", "chat"], default="health")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout (seconds).")
    parser.add_argument("--token", default=None, help="Gateway bearer token (optional).")
    parser.add_argument("--output", default="", help="Optional JSON output file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = load_token(args.token)
    summary = run_load(
        base_url=args.url,
        token=token,
        mode=args.mode,
        total_requests=args.requests,
        concurrency=args.concurrency,
        timeout_s=args.timeout,
    )

    print(json.dumps(summary, indent=2))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"Saved report: {out}")
    return 0 if summary["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
