# Harness layers (user vs repository)

This directory holds **tracked copies** of **user-scope** harness text so PRs can review drift between `~/.claude/` and the smartclaw repo.

| File | Install target | Purpose |
|------|----------------|---------|
| [`user-command-harness.md`](user-command-harness.md) | `~/.claude/commands/harness.md` | General `/harness` command — any repo |
| [`harness-engineering-scope-snippet.md`](harness-engineering-scope-snippet.md) | Merge into `~/.claude/skills/harness-engineering/SKILL.md` | **Scope: user vs repository** section |

**Repository-local (OpenClaw-specific)** — not copied to global `~/.claude/`:

- [`.claude/skills/openclaw-harness/SKILL.md`](../../.claude/skills/openclaw-harness/SKILL.md)
- [`.claude/commands/harness.md`](../../.claude/commands/harness.md)

**Collision rule:** Workspace `.claude/commands/harness.md` overrides the global command for that project. Agents should read **user-scope first**, then **repo overlay**.

**Sync:** From repo root:

```bash
bash scripts/sync-harness-user-scope.sh
```

Use `--dry-run` to print actions without writing.

See also: [`CLAUDE.md`](../../CLAUDE.md) (Gateway, doctor/monitor parity).
