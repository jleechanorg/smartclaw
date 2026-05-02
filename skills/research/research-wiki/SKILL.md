---
name: research-wiki
description: Search LLM-wiki and roadmap knowledge repos for project context
triggers:
  - "llm-wiki"
  - "roadmap"
  - "search wiki"
  - "find in docs"
---

# research-wiki skill

Searches Jeffrey's personal knowledge repos in parallel when context is needed.

## Repos

| Repo | Path | Notes |
|---|---|---|
| **LLM-wiki (main)** | `~/llm_wiki` | Primary knowledge wiki |
| LLM-wiki (autor) | `~/llm-wiki-autor-phase3` | Autoresearch worktree |
| Roadmap | `~/roadmap` | `roadmap` (ws:10) |

## When to use

Load this skill at **every session start** as part of the comprehensive-memory-search COMMIT. It supplements the standard memory search (mem0, session_search, MEMORY.md, daily notes) — it does NOT replace them.

Also load whenever you need to search project documentation, past decisions, learning docs, or any knowledge that lives in these repos.

## Search commands (use these exact forms)

### LLM-wiki (llm_wiki — PRIMARY)

```bash
grep -ri "KEYWORD" ~/llm_wiki/ --include="*.md" | head -20
```

### LLM-wiki (autor phase3 — secondary worktree)

```bash
grep -ri "KEYWORD" ~/llm-wiki-autor-phase3/ --include="*.md" | head -20
```

### Roadmap (~/roadmap)

```bash
grep -ri "KEYWORD" ~/roadmap/ --include="*.md" | head -20
```

Replace `KEYWORD` with the user's query terms. Run all three in parallel when doing the comprehensive memory search at session start.

## Output format

When returning results, cite:
- file path
- matching line content
- relevance to query

## Memory attribution

If you used research-wiki results, add to your Memory: line:
`research-wiki: found <N> matches in llm-wiki-autor-phase3 and/or roadmap`
