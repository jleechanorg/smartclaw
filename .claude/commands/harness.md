# /harness — Fix the harness, not just the symptom

Canonical protocol: **`~/.claude/skills/harness-engineering/SKILL.md`**

## When to use

- After a user correction, outage, or repeated manual fix — ask whether the **instructions, skills, tests, or CI** should change so the class of error cannot recur.
- For **OpenClaw gateway** incidents: read repo **`CLAUDE.md` → Gateway (Local Machine)** — `/health` green is necessary but not sufficient; check `gateway.err.log` for `lane wait exceeded`, `session file locked`, and `SlackWebSocket` patterns before declaring “fixed.”
- If the correction was **“you forgot Slack MCP”** for a thread URL or “are we down?” — add or strengthen **`CLAUDE.md` → Slack — Reading threads** and ensure future sessions **read Slack via MCP before** replying (see that section for COMMIT rules).

## Execution

1. Read **`harness-engineering/SKILL.md`** and follow its **5 Whys (technical)** and **5 Whys (agent path)** — both mandatory.
2. Classify the failure (mislabeled artifact, silent degradation, LLM path error, etc.).
3. Propose changes to the **most durable layer** (usually `CLAUDE.md` / skills first; tests/CI when automatable).
4. If the user passes **`--fix`**, implement harness changes in the same session and report diffs.

## Related

- Repo root **`CLAUDE.md`** — gateway, deploy, and WS discipline are the live runbook.
- **`scripts/doctor.sh`** / **`monitor-agent.sh`** — functional probes; extend both when adding a new failure mode (parity rule in `CLAUDE.md`).

## Confusing “settings” changes (scripts vs config)

If the user says behavior “randomly” changed but **`openclaw.json`** / **Cursor** / **MCP** were not edited, check **tracked harness scripts** first (e.g. `scripts/dropped-thread-followup.sh`): defaults and heuristics ship in git; **`DROP_*` env vars** override. Document the distinction in **`CLAUDE.md` → Operational scripts vs openclaw.json** and the relevant skill so operators do not confuse script iteration with gateway or model config drift.
