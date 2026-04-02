#!/bin/bash
# ai-reviewer-stress-test.sh — wrapper for the Python AI reviewer stress test
exec python3 "$(dirname "$0")/review_stress_test/run.py" "$@"
