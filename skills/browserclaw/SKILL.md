---
name: browserclaw
description: Capture browser traffic with Playwright and generate Python client + MCP tool artifacts from HAR sessions.
---

# browserclaw

Use this skill when the user wants to inspect a site's browser APIs without a browser extension.

## Workflow

1. Capture traffic:

```bash
browserclaw capture --url <url> --output generated/capture.har --manual
```

2. Infer a catalog:

```bash
browserclaw infer --har generated/capture.har --output generated/catalog.json
```

3. Generate artifacts:

```bash
browserclaw generate --catalog generated/catalog.json --output-dir generated/site
```

4. Optional one-shot:

```bash
browserclaw reverse --url <url> --output-dir generated/site --manual
```

## Critical Gotcha: Chrome Profile Isolation

**`--user-data-dir` creates a fully isolated browser profile.** This means:
- The debug Chrome has ZERO cookies/auth from your regular Chrome session
- Copying `~/.chatgpt_codex_auth_state.json` or `OAuth Token` between profile directories does NOT log you in
- The debug Chrome stays logged out even if your real Chrome is logged into the same site

**If you need auth from your real Chrome session**, launch the debug Chrome WITHOUT `--user-data-dir`:
```bash
/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9223 "https://chatgpt.com/codex"
```
This uses your actual Chrome profile at `~/Library/Application Support/Google/Chrome/Default`, preserving all cookies and session state.

**If you must use an isolated profile**, you must authenticate inside that debug window (manual login or use browserclaw's login flow with credentials).

## Non-Interactive Mode (No TTY)

If running in a non-interactive context (cron, script, agent session), bypass the `--manual` TTY prompt by passing an empty steps file:
```bash
browserclaw capture --url <url> --output capture.har --manual=false --steps /tmp/empty_steps.json
```
Where `/tmp/empty_steps.json` contains `[]` (empty JSON array). This tells browserclaw "no manual steps" without prompting stdin.

## Connecting Playwright to an Already-Running Debug Chrome

Instead of launching a new browser, connect Playwright to a Chrome already running with `--remote-debugging-port`:
```python
from playwright.async_api import async_playwright

playwright = await async_playwright().start()
browser = await playwright.chromium.connect_over_cdp("http://localhost:9223")
# Find your page among browser.contexts[].pages
```

This is useful for monitoring a human's browser session or capturing traffic from an existing authenticated state.

## Raw WebSocket CDP Client (Python stdlib only)

If `websocket`/`websockets` packages aren't available, Chrome DevTools Protocol (CDP) can be driven with pure Python stdlib:
- `socket` for TCP connection
- `base64` for WebSocket key exchange
- `hashlib` + `hmac` for WebSocket digest
- `struct` for frame parsing/unpacking
- `threading` + `queue` for async event dispatch

Chrome masks server→client frames with a 4-byte mask key (frame byte index 2). Unmask by XORing each byte with the mask key.

WS URL format: `ws://localhost:9223/devtools/page/<page-id>`

## Guardrails

- Manual auth only
- No stealth or bypass features
- Keep generated artifacts reviewable before use

