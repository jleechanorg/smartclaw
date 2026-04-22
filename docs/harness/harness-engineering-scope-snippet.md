# Merge into `~/.claude/skills/harness-engineering/SKILL.md`

Insert **after** the line `**Command**: \`~/.claude/commands/harness.md\`` and **before** `## Harness Layers (ordered by durability)`:

```markdown
## Scope: user vs repository

- **User scope (general)**: `~/.claude/commands/harness.md` and this skill apply to **any** repo unless a project overrides them.
- **Repository overlay**: Some projects ship `.claude/commands/harness.md` and/or `.claude/skills/<name>/SKILL.md` that **extend** user-scope rules (for example gateway operations). When both exist, **read the repo-local file** for project-specific failure modes. **Collision:** workspace-local `.claude/commands/` overrides the same-named global command in that workspace.
- **smartclaw / `~/.smartclaw`**: Use the **`openclaw-harness`** skill in that repo for gateway, canary, deploy, and lane-backlog triage. Tracked user-scope copies for drift control live under **`docs/harness/`** in [smartclaw](https://github.com/jleechanorg/smartclaw).
```

After you manually merge this snippet, the canonical merged skill in this repo’s checkout should match **`~/.claude/skills/harness-engineering/SKILL.md`**. Running **`scripts/sync-harness-user-scope.sh`** updates the user-scope **`~/.claude/commands/harness.md`** only; it does **not** update this skill file.
