#!/bin/bash
# wiki-daily-worker.sh - Daily wiki improvement via AO minimax worker
# Location: ~/.smartclaw/scripts/wiki-daily-worker.sh

set -e

WIKI_DIR="$HOME/llm_wiki"
MEMORY_WIKI="$HOME/memory/wiki"
LOG_FILE="$HOME/Library/Logs/wiki-daily-worker.log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M')] $1" | tee -a "$LOG_FILE"
}

cd "$WIKI_DIR" || exit 1

log "=== Wiki Daily Worker Starting ==="

# Check for new sources to ingest
NEW_SOURCES=$(find "$WIKI_DIR/raw" -name "*.md" -newer "$WIKI_DIR/wiki/sources" 2>/dev/null | wc -l)
log "New source files found: $NEW_SOURCES"

if [ "$NEW_SOURCES" -gt 0 ]; then
    log "Ingesting new sources via minimax..."
    # Use claudem for cost-effective processing
    claudem() {
        claude --dangerously-skip-permissions \
            --model MiniMax-M2.5 \
            --max-turns 5 \
            "$@"
    }

    # Run ingest via minimax
    claudem -p "Ingest the $(ls $WIKI_DIR/raw/*.md | wc -l) new files in raw/ to wiki/sources/, create entities for key people/repos, concepts for key patterns, update index.md, then report what was added" 2>&1 | tee -a "$LOG_FILE"

    log "Ingest complete"
fi

# Run wiki-evolve to check and fix pattern compliance
log "Running wiki-evolve pattern check..."
if command -v python3 &> /dev/null; then
    python3 -c "
import os
wiki = '$WIKI_DIR/wiki'
sources = len([f for f in os.listdir(f'{wiki}/sources') if f.endswith('.md')])
entities = len([f for f in os.listdir(f'{wiki}/entities') if f.endswith('.md')])
concepts = len([f for f in os.listdir(f'{wiki}/concepts') if f.endswith('.md')])
print(f'Sources: {sources}, Entities: {entities}, Concepts: {concepts}')
print(f'Entity ratio: {entities*100//sources}%, Concept ratio: {concepts*100//sources}%')
"
fi

# Check memory wiki too
log "Checking memory wiki..."
if [ -d "$MEMORY_WIKI" ]; then
    sources=$(ls "$MEMORY_WIKI/sources"/*.md 2>/dev/null | wc -l)
    entities=$(ls "$MEMORY_WIKI/entities"/*.md 2>/dev/null | wc -l)
    concepts=$(ls "$MEMORY_WIKI/concepts"/*.md 2>/dev/null | wc -l)
    log "Memory wiki: Sources: $sources, Entities: $entities, Concepts: $concepts"
fi

log "=== Wiki Daily Worker Complete ==="