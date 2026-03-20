# Makefile for orchestration development tasks

.PHONY: beads sync list

# List recent beads (open tasks/bugs)
beads:
	br list --limit 20

# Sync beads database with JSONL source of truth
sync:
	br sync

# Alias for list
list: beads
