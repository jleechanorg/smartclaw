#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT_PATH="${1:-$ROOT_DIR/openclaw-config/symphony/swe_bench_verified_5.json}"

"$ROOT_DIR/scripts/sym-dispatch.sh" --plugin swe_bench_verified "$INPUT_PATH"
