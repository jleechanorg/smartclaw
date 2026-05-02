---
name: x-to-skill
description: "Take an X/Twitter post and turn its workflow into a reusable Hermes SKILL.md. Use when the user shares a tweet URL and asks to turn it into a skill, capture this workflow, or skillify this post."
when_to_use: "Use when the user shares an X/Twitter post (URL or ID) and asks to turn it into a skill, capture this as a workflow, or skillify this post. Examples: 'turn this tweet into a skill', 'capture this X post as a workflow', 'x-to-skill', 'skillify this post', 'build a skill from this tweet'"
arguments:
  - tweet_url
  - skill_name
  - description
argument-hint: "[tweet_url] [skill_name] [description of the workflow]"
context: inline
---

# X-to-Skill — Turn an X Post into a Reusable Hermes Skill

Take any X/Twitter post that describes a workflow, tool, or process, and generate a properly-structured SKILL.md from it.

## Inputs

- `$tweet_url`: Full tweet URL or just the numeric ID (e.g. `2046876981711769720` or `https://x.com/garrytan/status/2046876981711769720`)
- `$skill_name`: (optional) Name for the skill. If omitted, infer from the tweet content.
- `$description`: (optional) Short description. If omitted, infer from the tweet content.

## Steps

### 1. Fetch the Tweet

Try these in order:

**If `x-cli` is installed and X_API_KEY is set:**
```bash
x-cli -j tweet get <tweet_id>
```

**If x-cli is not available**, use `mcp__slack__*` if the tweet was shared in Slack, or try `terminal` with `curl` against the syndication endpoint:
```bash
curl -sL "https://syndication.twitter.com/srv/timeline-profile/screen-name/<username>" -H "User-Agent: Mozilla/5.0"
```

Extract:
- Author handle and display name
- Full tweet text
- Any quote-tweet or attached thread context
- Date posted

### 2. Analyze the Tweet Content

Identify:
- Is this a workflow/process (numbered steps, sequential actions)?
- Is this a tool or project announcement?
- Is this advice or a principle?
- What are the implied or explicit steps?
- What arguments/inputs would this skill need?
- What tools would the skill require?

If the tweet is part of a thread, try to fetch the full thread context. Thread context often contains the actual workflow steps.

### 3. Build the SKILL.md

Use the `skillify` skill pattern:

**Frontmatter:**
```yaml
---
name: {{infer or use provided}}
description: {{one-line description of what this does}}
when_to_use: "{{trigger phrases a user would actually type to invoke this}}"
argument-hint: "{{arg1} {arg2}}"
arguments:
  {{list of argument names}}
context: inline
---

# {{Title}}
```

**Body structure:**
- **Goal**: What the skill accomplishes and what success looks like
- **Inputs**: Arguments the user must provide
- **Steps**: Numbered steps with **success criteria** for each

### 4. Infer Missing Context

Fill gaps intelligently:
- A tool announcement → add steps for install, setup, and basic usage
- A workflow → add proper success criteria to each step
- Advice/principle → frame it as a decision skill with trigger conditions
- If the tweet references a project (e.g. gbrain) → include the install + setup steps

If the referenced tool/project has known setup steps (check memory or common knowledge), include them.

### 5. Save the Skill

- Save to `~/.hermes/skills/<skill_name>/SKILL.md`
- For repo-specific skills: ask the user whether to save to `.claude/skills/` instead

### 6. Report

Tell the user:
- Where the skill was saved
- How to invoke it: `/<skill_name> [arguments]`
- What the skill does in one line

## Example: Garry Tan's gbrain Skill (from his X post)

From a tweet about building a personal agent brain with these properties:
- Ingest meetings, emails, tweets, voice calls, ideas
- Enrich every person and company encountered
- Self-wiring knowledge graph
- Overnight consolidation
- Sleep smarter than you wake up

Would produce a skill like `agent-brain-builder`:

```yaml
---
name: agent-brain-builder
description: "Build a self-wiring agent brain from your daily data sources"
when_to_use: "Use when the user wants to set up a personal agent brain. Examples: 'set up my brain', 'build an agent brain', 'install gbrain', 'make my agent smarter overnight'"
argument-hint: "[brain_name] [data_sources]"
arguments:
  - brain_name
  - data_sources
context: inline
---

# Agent Brain Builder

Build a self-wiring knowledge brain for your AI agent.

## Goal

Agent accumulates knowledge from daily work, enriches entities, and compounds intelligence overnight.

## Inputs

- `$brain_name`: Name for the brain (e.g. "my-brain")
- `$data_sources`: Comma-separated sources to connect (meetings, email, tweets, voice, ideas)

## Steps

### 1. Clone and Install Brain
Clone the brain repo, install dependencies, link the CLI.

**Success criteria**: `gbrain --version` returns a version number.

### 2. Initialize the Brain
Run `gbrain init` to set up the database and schema.

**Success criteria**: Brain database created, schema initialized.

### 3. Connect Data Sources
Configure integrations for each source (meetings, email, tweets, etc.).

**Success criteria**: Each source has a working integration confirmed.

### 4. Run First Ingestion
Ingest existing data from all sources.

**Success criteria**: Pages created in the brain, entities identified.

### 5. Schedule Overnight Consolidation
Set up cron jobs for nightly enrichment and link-wiring.

**Success criteria**: Cron jobs active, next run scheduled.
```
