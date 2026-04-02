#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT_PATH="${1:-$ROOT_DIR/openclaw-config/symphony/leetcode_hard_5.json}"

"$ROOT_DIR/scripts/sym-dispatch.sh" --plugin leetcode_hard "$INPUT_PATH"
