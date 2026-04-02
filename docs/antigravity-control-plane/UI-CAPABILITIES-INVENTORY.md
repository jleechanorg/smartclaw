# Antigravity Control Plane — UI Capabilities Inventory

**Bead:** `ORCH-ag1`
**Status:** Design in progress

---

## What the Antigravity Skill Supports Today

Source: `~/.claude/skills/antigravity-computer-use/SKILL.md` (canonical, 301 lines, loaded 2026-03-24)

### Core Loop

The skill implements a **screenshot-decide-act loop** driven by an LLM (the worker agent):

1. **Screenshot**: `peekaboo see [--analyze]` captures the current Antigravity Manager window
2. **Decide**: LLM analyzes screenshot + JSON annotation, decides next action
3. **Act**: LLM emits a Peekaboo command (`click`, `type`, `paste`, `press`)
4. **Repeat**: until goal is achieved or blocked

### What Can Be Done Today (v1 Skill)

| Capability | Status | Notes |
|---|---|---|
| **Manager window targeting** | ✅ Working | Targets window with internal title `'Manager'` — the Agent Manager sidebar |
| **Workspace enumeration** | ✅ Working | `peekaboo window list` + annotated screenshot shows all workspaces with `[ACTIVE]` markers |
| **Per-workspace navigation** | ✅ Working | Click workspace in sidebar → context switches to that workspace |
| **Element targeting** | ✅ Working | `--snapshot <ID>` with `--on elem_N` for precise element clicks |
| **Text input** | ✅ Working | `peekaboo paste` (not `type`) for text — bypasses keyboard interception |
| **Keyboard shortcuts** | ✅ Working | `peekaboo press` for special keys (Enter, Escape, etc.) |
| **Screenshot evidence** | ✅ Working | Every iteration captures a screenshot for audit/replay |
| **"Allow this conversation" dialog** | ✅ Working | Skill automatically clicks Allow without asking |
| **OAuth session management** | ✅ Working | Antigravity uses OAuth for agent auth inside the session |
| **Completion detection** | ✅ Working | Goal visible in screenshot evidence, or blocked with exact blocker |
| **Blocking on human input** | ✅ Working | Required human actions are surfaced as blockers with exact description |

### Peekaboo CLI Commands Used

```bash
# Capture screenshot of Manager window
peekaboo see --window Manager [--analyze]

# List all windows (for workspace enumeration)
peekaboo window list

# Click an element
peekaboo click --on elem_42 --snapshot <window_id>

# Type text (uses clipboard paste, not keyboard)
peekaboo paste "hello world" --on elem_42

# Press a key
peekaboo press Enter --on elem_42

# Get window snapshot for targeting
peekaboo screenshot --window Manager
```

### What Is NOT Yet Supported

| Missing Capability | Impact | Priority |
|---|---|---|
| **Native Antigravity API** | All automation goes through GUI (Peekaboo); no structured data exchange | High |
| **Window state detection** | Can't detect if Antigravity is loading, errored, or crashed without screenshots | High |
| **Clipboard monitoring** | Can't read Antigravity output via clipboard; must screenshot | Medium |
| **Multi-monitor support** | Assumes Antigravity is on primary monitor | Medium |
| **Menu bar automation** | No documented path for File/Edit/View menu operations | Low |
| **Notification handling** | Pop-up notifications not automatically dismissed | Low |
| **Session resume** | If Antigravity crashes mid-job, no session resume — full restart required | High |
| **Structured output parsing** | LLM must read screenshot to extract structured data (PR numbers, errors, etc.) | Medium |
| **Rate limiting awareness** | No backoff when Antigravity shows "too many requests" UI | Medium |
| **Parallel workspace control** | One Peekaboo session per workspace; no documented multi-workspace coordination | Medium |

---

## What Was Observed from cmux Workspace "antig"

**Finding: No active Antigravity tmux/cmux workspace exists at time of research (2026-03-24).**

The cmux workspace list shows 13 workspaces (`o: primary`, `ao: primary`, `ao2: monitor`, `ao3: misc`, `o2: misc`, `ao: cli`, `w: tails`, `w: trimcommands`, `w: export`, `cmux`, `openclaw: main`, `openclaw: memory`, `g: wc: generators`). No Antigravity-named workspace was found.

The file `~/.smartclaw/workspace/Antigravity_2026-03-24T05:13:57Z.png` (22KB, timestamped today) shows:
- Antigravity Manager window screenshot captured this morning
- Workspace sidebar visible with conversations listed
- UI elements suggesting a chat/agent interface

**This implies:** The Antigravity skill was run successfully in a Claude Code session (not a persistent tmux session), capturing the screenshot. No persistent antig tmux workspace was established.

**Gap:** For the control plane to operate Antigravity persistently, a **persistent tmux/cmux workspace** for Antigravity needs to be established — not just single-shot skill invocations.

---

## Capabilities Needed for v2+ Control Plane

### v2: Persistent Session Management

| Needed Capability | Currently Supported? | Implementation Approach |
|---|---|---|
| Launch Antigravity from command line | ❓ Not documented | Investigate `openclaw agents` or Antigravity CLI; if none, document as blocker |
| Attach to existing Antigravity session | ❓ Not documented | Investigate if Peekaboo can attach to running Antigravity process |
| Detect Antigravity crash/restart | ❓ Not documented | Periodic `peekaboo screenshot` + LLM analysis of screenshot for error state |
| Kill and restart Antigravity workspace | ❌ Not supported | Requires Antigravity CLI or AppleScript |
| Session state persistence across restarts | ❌ Not supported | Would need Antigravity session export/import |

### v2: Multi-Worker Coordination

| Needed Capability | Currently Supported? | Implementation Approach |
|---|---|---|
| Multiple simultaneous workspaces | ⚠️ Untested | Skill targets one workspace at a time; need to verify parallel workspace safety |
| Worktree conflict detection | ❌ Not in skill | Control plane must enforce per-repo exclusivity (see ARCHITECTURE.md) |
| Cross-repo job correlation | ❌ Not in skill | Control plane manages via `correlation_id` (see DATA-CONTRACTS.md) |
| Job cancellation mid-execution | ❌ Not in skill | Executor must check for cancellation signal between screenshot loops |

### v2: Observability

| Needed Capability | Currently Supported? | Implementation Approach |
|---|---|---|
| Structured log output | ❌ Not in skill | Executor must write to JSONL; control plane aggregates |
| Progress reporting | ❌ Not in skill | Worker sends heartbeat messages with progress string |
| Evidence screenshot archival | ⚠️ Partial | Skill saves screenshots; need centralized path (`antig_jobs/{job_id}/`) |
| Execution transcript | ❌ Not in skill | Capture LLM reasoning + Peekaboo commands to JSONL |

### v3: Hardening

| Needed Capability | Currently Supported? | Implementation Approach |
|---|---|---|
| Automatic retry on transient UI errors | ❌ Not in skill | Executor implements retry loop with screenshot-based error detection |
| Permission monitoring | ⚠️ Partial | `scripts/peekaboo-preflight.sh` checks; need continuous monitoring |
| Rate limiting / backoff | ❌ Not in skill | Executor detects rate-limit UI and backs off |
| Graceful degradation | ❌ Not in skill | If Peekaboo fails, executor should alert and deadletter |

---

## Gap Analysis Summary

### Critical Gaps (Block v2 AO Integration)

1. **No persistent Antigravity session management** — single-shot skill invocations won't work for AO workers that need to run asynchronously
2. **No Antigravity CLI** — if Antigravity doesn't have a CLI for launch/kill/restart, executor cannot recover from crashes
3. **No job cancellation mid-execution** — AO workers could run indefinitely if job isn't cancelled properly

### High-Priority Gaps (Should Fix in v2)

4. **Session resume** — Antigravity crash = full job failure; no recovery possible without session persistence
5. **Structured output** — LLM must OCR screenshots to extract PR numbers, errors; fragile and slow
6. **Parallel workspace safety** — untested; needs verification before multi-worker deployment

### Medium-Priority Gaps (Nice to Have for v2)

7. **Multi-monitor support**
8. **Rate limiting awareness**
9. **Menu bar automation**

---

## Research Actions for Sonnet Workers

Before implementing v2, Sonnet workers should investigate:

1. **Antigravity CLI**: Does Antigravity have a CLI for `--launch`, `--kill`, `--attach`? Check `Antigravity --help` and look for AppleScript/Safari extension APIs.
2. **Session persistence**: Can Antigravity sessions be exported to a file and restored? Check Antigravity preferences/data directories.
3. **Peekaboo multi-window**: Can `peekaboo` control two Manager windows simultaneously? Test with two Antigravity instances.
4. **Crash detection**: What does the Antigravity Manager window look like when crashed? Collect screenshots for LLM-based crash detection.

---

## Relationship to ORCH-ma4

The existing ORCH-ma4 design (`roadmap/PEEKABOO_ANTIGRAVITY_UI_AUTOMATION.md`) established:
- ✅ Peekaboo installation and preflight checks
- ✅ `PeekabooBridge.sock` location
- ✅ Single-session automation loop

The capabilities inventory above extends ORCH-ma4 by identifying what's needed for **multi-session, multi-repo orchestration**. The gaps listed should be addressed in the v2 phase.
