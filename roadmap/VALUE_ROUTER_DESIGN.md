# Value Router Design - Agent Orchestrator

**Date:** 2026-03-15
**Status:** Proposal

---

## Overview

Add a closed-loop Value Router (autonomous work allocator) that scores backlog items by expected merge value per agent-hour, then dynamically spawns/pauses/resumes sessions based on live outcomes.

## Motivation

- **Current state:** Backlog auto-claim and decomposition exist, but use mostly label/FIFO + fixed capacity (`MAX_CONCURRENT_AGENTS = 5`) in `services.ts`.
- **Current telemetry:** Rich telemetry (success/failure, reasons, durations) is collected in observability, but not yet feeding scheduling decisions.
- **Opportunity:** Becomes the optimizer layer above existing reaction/lifecycle machinery.
- **Impact:** Turns AO from "automation runner" into a self-optimizing execution system.

## Value Proposition

- Every run improves future prioritization (which issue types, repos, agents, and decomposition patterns actually ship fastest with least rework)
- Compounds throughput and merge quality without increasing operator overhead
- If useful, next step is a minimal V1 spec and initial scorer + scheduler loop behind a config flag

---

## Core Formula

```
MergeValue = (P_merge_24h * BusinessImpact * StrategicFit * Confidence) / ExpectedAgentHours
```

### Components

| Factor | Range | Description |
|--------|-------|-------------|
| **P_merge_24h** | 0..1 | Model from own history by repo/label/agent/type. Signals: similar issue merged rate, CI pass-on-first-try rate, review churn, reopen rate |
| **BusinessImpact** | 1..5 | Configurable weight from labels/metadata. Example: customer-facing=5, reliability=4, tech-debt=2 |
| **StrategicFit** | 0.8..1.3 | Multiplier for current priorities. Example: if this sprint targets onboarding, onboarding-labeled issues get +30% |
| **Confidence** | 0.6..1.2 | Evidence quality factor. Clear acceptance criteria/tests/decomposition quality increase it |
| **ExpectedAgentHours** | hours | Predicted completion time from past runs. Include expected retries from CI/review loops |

---

## Scheduling Algorithm

Schedule by descending `MergeValue` subject to constraints:

- Max parallel sessions
- Fairness per project
- Dependency/blocked checks

### Learning Loop

1. Start with hand-tuned weights + rule-based estimates
2. Log predicted vs actual
3. Replace each component with learned models once enough data is collected

---

## Implementation Notes

- V1 can be behind a config flag (`VALUE_ROUTER_ENABLED=false`)
- Reuse existing observability infrastructure
- Integrate with existing `services.ts` capacity management
- Initial scorer can use simple heuristics before ML models
