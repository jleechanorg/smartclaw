#!/usr/bin/env bash
# DEPRECATED: ao-resolve-threads.sh
#
# This script is DEPRECATED as of 2026-03-20.
#
# The thread resolution functionality is now handled by the AO core via:
#   python -m orchestration.auto_resolve_threads <owner> <repo> <pr-number> [--dry-run]
#
# Usage:
#   python -m orchestration.auto_resolve_threads jleechanorg jleechanclaw 123
#   python -m orchestration.auto_resolve_threads jleechanorg jleechanclaw 123 --dry-run
#
# Or use the AO orchestrator directly which integrates this functionality.

set -euo pipefail

echo "ERROR: ao-resolve-threads.sh is DEPRECATED." >&2
echo "" >&2
echo "The thread resolution functionality is now handled by the AO core." >&2
echo "Use the Python module directly:" >&2
echo "  python -m orchestration.auto_resolve_threads <owner> <repo> <pr-number> [--dry-run]" >&2
echo "" >&2
echo "This script will be removed in a future release." >&2

exit 1
