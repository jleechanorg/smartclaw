# Orchestration System Design Justification

> Why each file in the orchestration layer exists and why we didn't use an existing tool.

Current architecture: `OpenClaw -> mctrl -> ai_orch`.

## Core Question: Why not just use `gh` CLI directly?

**We do use `gh` CLI.** The `gh_integration.py` module is a thin Python wrapper around `gh` — every GitHub API call shells out to `gh` (see `gh()` function at line 85). The wrapper exists because:

1. **Type-safe orchestration**: Python dataclasses (`PRInfo`, `MergeReadiness`, `CIStatus`) give orchestration code compile-time-like safety when composing multi-step workflows. Raw `gh` output is untyped JSON strings.

2. **Fail-closed error handling**: The wrapper enforces consistent error policy — CI check failures propagate (never silently return empty), unknown states map to "failed", GraphQL errors block merges. A shell script calling `gh` would need this logic duplicated at every call site.

3. **GraphQL queries not available via `gh pr`**: The unresolved-thread merge gate requires `pullRequest.reviewThreads` via GraphQL. `gh pr view --json` does not expose thread resolution state. The wrapper encapsulates this query with injection-safe `-f` variable passing.

4. **Aggregation logic**: `get_merge_readiness()` combines 4 independent checks (CI, reviews, unresolved threads, conflicts) into a single boolean with a blockers list. This composition doesn't exist in `gh` CLI.

5. **Testability**: Every function is independently testable via `unittest.mock.patch` on the `gh()` wrapper. Testing shell scripts that call `gh` requires brittle process-level mocking.

## Alternatives Considered

### Why not `gh` CLI scripts directly?

| Concern | `gh` CLI scripts | `gh_integration.py` |
|---|---|---|
| Error handling | Per-script, easy to forget | Centralized fail-closed policy |
| Type safety | None (string parsing) | Dataclasses + enums |
| GraphQL support | Manual `gh api graphql` calls | Encapsulated with variable injection |
| Testing | Process-level mocking | Function-level mocking |
| Reuse across orchestration | Copy-paste or source scripts | Import and call |

**Verdict**: `gh` CLI is the transport layer. `gh_integration.py` is the orchestration-safe interface to it.

### Why not openclaw/openclaw code?

OpenClaw's codebase (TypeScript) provides agent runtime, gateway, and session management. It does NOT provide:
- PR merge-readiness aggregation logic
- Unresolved review thread detection via GraphQL
- Fail-closed CI status composition
- Python-native integration for our `src/orchestration/` layer

This repo's orchestration layer (`src/orchestration/`) is Python. OpenClaw is TypeScript. Calling OpenClaw internals from Python would require an IPC bridge with no benefit over calling `gh` directly.

### Why not openclaw Mission Control code?

Mission Control was evaluated as a UI/control-plane application, not as the
authoritative execution path for this repo. The current direction is documented
in `roadmap/MCTRL_NO_OSS_MISSION_CONTROL.md`: `mctrl` owns orchestration state,
`ai_orch` owns execution, and GitHub/Slack integrations hang directly off that
stack.

Mission Control would have consumed `gh_integration.py` outputs, not replaced
them.

### Why not `ai_orch` code?

`ai_orch` (at `~/projects/worldarchitect.ai/orchestration/`) handles agent spawning, tmux lifecycle, and CLI invocation. It does NOT provide GitHub PR readiness checking. The design doc assigns GitHub state queries to the orchestration layer, not `ai_orch`.

### Why not PyGithub, GitHubKit, ghapi, gql, or other Python GitHub SDKs?

Researched 2026-03-04 across PyPI, GitHub, and web sources.

| Library | Stars | GraphQL? | `reviewThreads` typed? | Merge readiness? | Auth via `gh`? | Extra deps |
|---|---|---|---|---|---|---|
| [PyGithub](https://pygithub.readthedocs.io/) | ~7k | Partial (v2.8+, raw queries) | No typed wrapper | No | No (PAT only) | 5+ |
| [GitHubKit](https://github.com/yanyongyu/githubkit) | 319 | Yes (full) | No typed wrapper | No | No (PAT/App) | Pydantic + httpx |
| [octokit.py](https://github.com/khornberg/octokit.py) | ~350 | "In progress" | No | No | No | — |
| [gql](https://github.com/graphql-python/gql) | ~1.5k | Yes (generic client) | Yes (raw queries) | No | No | aiohttp, websockets |

**Key findings**:
- PyGithub (v2.8+) and GitHubKit can run the `reviewThreads` GraphQL query as a raw string, but neither provides a typed wrapper or merge-readiness aggregation.
- None uses `gh` CLI for auth — each requires managing PATs or GitHub App credentials separately.
- These SDKs replace the **transport layer** (`gh api graphql` subprocess call → `httpx.post`). They do NOT replace the **business logic** (~100 lines): thread filtering, bot exclusion, overflow guard, fail-closed aggregation.
- `gh` CLI already handles auth (via `gh auth`), pagination, rate limiting, and token management for free.
- This repo has zero non-test Python dependencies. Adding GitHubKit (Pydantic + httpx) or PyGithub (~30 transitive deps) to save ~25 lines of subprocess code is not justified.

**Verdict**: SDK swap saves ~25 lines of transport code but adds dependency management and auth complexity. The ~100 lines of domain-specific logic stays the same regardless.

### Why not Kodiak, Mergify, or Bulldozer (merge automation bots)?

| Tool | Language | Review threads? | Importable library? | Model |
|---|---|---|---|---|
| [Kodiak](https://github.com/chdsbd/kodiak) | Python 81% | Not documented | No (GitHub App service) | Self-hosted/SaaS |
| [Mergify](https://mergify.com) | Python | Yes (merge condition) | No (SaaS only) | SaaS |
| [Bulldozer](https://github.com/palantir/bulldozer) | Go | Required reviews only | No (GitHub App) | Self-hosted |

These are **operational bots** that auto-merge PRs when conditions are met. They solve a different problem: we don't want to auto-merge — we want to **query** merge readiness and present it to a human for judgment.

- **Kodiak** is Python and actively maintained (latest release Feb 2026), but its merge-evaluation code is embedded in a GitHub App backend, not an importable library. Deploying a full service to replace a 565-line file is not justified.
- **Mergify** handles review threads as merge conditions, but it's SaaS-only.
- **Bulldozer** (Palantir) checks required reviews but doesn't expose unresolved thread state.

**Verdict**: These are merge executors, not merge-readiness query libraries. Our orchestration layer needs to query and aggregate state, not auto-act on it.

### Why not GitHub Actions (Unresolved Review Threads Action)?

The [Unresolved Review Threads Action](https://github.com/marketplace/actions/unresolved-review-threads) (TypeScript) blocks merges via label management in CI. It:
- Runs only in GitHub Actions, not callable from Python
- Uses event triggers (`pull_request_review_comment`), not on-demand queries
- Author acknowledges it's "far from perfect" due to GitHub Actions event limitations

**Verdict**: CI-only enforcement. Our orchestration needs on-demand thread state queries from Python.

### Why not gh-pr-review (CLI extension)?

[gh-pr-review](https://github.com/agynio/gh-pr-review) is a Go-based `gh` extension that adds `--unresolved` filtering for viewing threads in terminal. It's designed for human review workflows, not programmatic merge gates. No Python API.

**Verdict**: CLI tool for humans, not a library for automation.

## File-by-File Justification (PR #30 Changes)

### `src/orchestration/gh_integration.py` (+12 lines changed)

**Purpose**: Python orchestration interface to GitHub via `gh` CLI.

**What changed in this PR**:
- `reviewThreads(first: 100)` query with `totalCount` overflow guard (fail-closed when >100 threads)
- `get_pending_comments` raises on GraphQL failure instead of silently returning `[]`
- `get_merge_readiness` now checks unresolved review threads as a merge blocker

**Why it exists**: See "Core Question" above. No existing tool provides fail-closed merge-readiness aggregation combining CI + reviews + thread resolution + conflict state.

### `src/tests/test_gh_integration.py` (+73 lines changed)

**Purpose**: pytest suite for `gh_integration.py`.

**What changed**: Tests for unresolved-thread blocking, GraphQL failure propagation, query shape conformance, pagination overflow guard, and workflow security assertions.

**Why it exists**: TDD methodology — every behavior change starts with a failing test. Tests verify fail-closed invariants that would be invisible in production until a merge gate silently fails open.

### `roadmap/MCTRL_NO_OSS_MISSION_CONTROL.md`

**Purpose**: Canonical decision doc for removing OSS Mission Control from the
supported architecture.

**Why it exists**: Consolidates the architectural direction in one place:
- `mctrl` is the lifecycle authority
- `ai_orch` is the execution substrate
- beads are canonical task state
- Mission Control is out of the target architecture

### `roadmap/TDD_EXECUTION_ROADMAP.md` (new, 90 lines)

**Purpose**: Phased execution plan for implementing the design doc with TDD discipline.

**Why it exists**: Separates "what to build" (design doc) from "how to ship it safely" (TDD roadmap). Defines Red/Green/Refactor phases, test layers per feature, and an immediate backlog tied to PR #30.

### `roadmap/ORCHESTRATION_DESIGN.md` (+1 line)

**Purpose**: Cross-reference to the new TDD roadmap.

**Why changed**: Adds a pointer in the Related Documents table so the TDD roadmap is discoverable from the existing orchestration design doc.

### `CLAUDE.md` (+5 lines) and `AGENTS.md` (+5 lines)

**Purpose**: CodeRabbit review protocol instructions.

**Why changed**: After pushing fixes for CodeRabbit review comments, agents need to post `@coderabbitai all good` to trigger re-review. Without this instruction, agents would push fixes but never signal CodeRabbit to verify them, leaving review threads unresolved indefinitely.

**Why both files**: `CLAUDE.md` is loaded by Claude Code sessions. `AGENTS.md` is loaded by other agent frameworks (Codex, Cursor). Both need the same protocol.
