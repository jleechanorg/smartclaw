# AO Exhaustive Audit Findings (File-Level Sweep)

Date: 2026-03-05

## Scope

This audit compared orchestration capabilities across:

- `mctrl` (`/Users/jleechan/project_jleechanclaw/mctrl`)
- `jleechanclaw` (`/Users/jleechan/project_jleechanclaw/jleechanclaw`)
- `worldarchitect.ai` (`/Users/jleechan/projects/worldarchitect.ai`)
- AO reference (`/Users/jleechan/projects_reference/agent-orchestrator`)

## Method

Literal file-level sweep completed before scoring:

- `mctrl`: 25 tracked files
- `jleechanclaw`: 188 tracked files
- `worldarchitect.ai`: 4,136 tracked files

All tracked paths were read for coverage (`rg --files` + full read pass), then key capability claims were verified against implementation files.

## Findings

### Where AO is clearly better

1. Plugin kernel and registry model:
   - AO: `packages/core/src/plugin-registry.ts`, `packages/core/src/types.ts`
   - Current stack: no equivalent typed runtime registry in Python orchestration.
2. Durable session metadata + archive/restore flow:
   - AO: `packages/core/src/session-manager.ts`, `packages/core/src/metadata.ts`
   - Current stack: partial session persistence and heartbeat tracking, but no AO-grade archive/restore path.
3. Lifecycle reaction completeness:
   - AO: `packages/core/src/lifecycle-manager.ts`
   - Current stack has lifecycle logic (`jleechanclaw/src/orchestration/lifecycle_reactions.py`) but still narrower on parity behaviors.
4. Integrated preflight + operator UX around orchestration commands:
   - AO has consolidated checks and CLI surface; Python stack has scattered checks and command-specific validation.

### Where your stack is better

1. Review remediation depth and accountability:
   - `~/.claude/commands/copilot.md`
   - `~/.claude/commands/_copilot_modules/commentfetch.py`
2. Practical tmux execution hardening and battle-tested operational scripts:
   - `worldarchitect.ai/orchestration/runner.py`
   - `worldarchitect.ai/orchestration/agent_monitor.py`
   - `worldarchitect.ai/orchestration/cleanup_completed_agents.py`
3. Existing advanced GitHub integration in mctrl:
   - `mctrl/src/orchestration/gh_integration.py`
   - `mctrl/src/tests/test_gh_integration.py`

### Parity or near-parity areas

1. CI/review/mergeability read models in `gh_integration.py` are close to AO's scm-github semantics.
2. Lifecycle reactions exist in Python and can be hardened to close remaining gaps without adopting AO wholesale.

## New Beads Created From This Audit

Epic:

- `ORCH-a68` AO gap-closure program from exhaustive file audit (with TDD roadmap)

Implementation beads:

- `ORCH-ozi` AO-lite plugin kernel: typed contracts + runtime plugin loader
- `ORCH-4yy` Session metadata + archive/restore parity in Python orchestration
- `ORCH-zzd` Unified preflight gate before convergence execution
- `ORCH-twf` Lifecycle reaction parity hardening (retry/escalation/all-complete)
- `ORCH-y7b` Integration test matrix for minimal-stack plugin combinations
- `ORCH-a68.1` Reconcile architecture docs: AO-authority vs minimal-stack outer-ralph

TDD beads:

- `ORCH-xoi` TDD: plugin kernel contract tests + loader failure modes
- `ORCH-bvk` TDD: session metadata/archive/restore + liveness enrichment
- `ORCH-4vi` TDD: preflight matrix (auth/env/runtime) with fail-closed behavior
- `ORCH-czz` TDD: lifecycle transition + reaction escalation semantics

Dependency wiring:

- Each TDD bead blocks its corresponding implementation bead.
- `ORCH-y7b` depends on all main implementation tracks.
- `ORCH-a68.1` currently blocks `ORCH-orl` to force architecture-doc consistency before outer-ralph rollout.

## Agreement Check Against Latest Design + Beads

After reading the latest design docs and bead table:

1. I agree with `jleechanclaw/roadmap/CONVERGENCE_ENGINE_DESIGN.md` on the minimal-stack direction:
   - Outer-ralph LLM orchestrator + deterministic tools/plugins is the right baseline.
2. I do not fully agree with `mctrl/roadmap/ORCHESTRATION_DESIGN.md` where it still states AO control-plane authority as locked direction.
   - That statement conflicts with the newer convergence design.
3. I agree with keeping AO as a pattern/source library to copy from (not as a framework dependency).
   - This is exactly what the new gap-closure beads operationalize.

## Recommended Source of Truth

Use the minimal-stack convergence design as the authoritative architecture and treat AO-authority language in older docs as historical context pending reconciliation (`ORCH-a68.1`).
