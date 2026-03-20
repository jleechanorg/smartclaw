#!/bin/bash
set -euo pipefail

# SmartClaw Dependency Bootstrap Script
# Safe, idempotent installer for orchestration dependencies

echo "==> SmartClaw Dependency Bootstrap"
echo

# Detect Python
detect_python() {
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    elif command -v python >/dev/null 2>&1; then
        echo "python"
    else
        echo "ERROR: No Python interpreter found" >&2
        echo "Please install Python 3.11 or higher" >&2
        exit 1
    fi
}

PYTHON_BIN="${PYTHON_BIN:-$(detect_python)}"
if [ -z "$PYTHON_BIN" ]; then
    exit 1
fi
echo "Using Python: $PYTHON_BIN"

# Check Python version
PYTHON_VERSION=$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
REQUIRED_VERSION="3.11"

if ! "$PYTHON_BIN" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
    echo "ERROR: Python $REQUIRED_VERSION+ required, found $PYTHON_VERSION" >&2
    exit 1
fi

echo "Python version: $PYTHON_VERSION ✓"

# Check for pip
if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    echo "ERROR: pip not found. Install pip first." >&2
    exit 1
fi

echo "pip: available ✓"

# Check for tmux (optional but recommended)
if command -v tmux >/dev/null 2>&1; then
    echo "tmux: available ✓"
else
    echo "WARNING: tmux not found. Async mode requires tmux." >&2
fi

# Check for git
if command -v git >/dev/null 2>&1; then
    echo "git: available ✓"
else
    echo "ERROR: git not found. Required." >&2
    exit 1
fi

# Check for gh (optional)
if command -v gh >/dev/null 2>&1; then
    echo "gh: available ✓"
else
    echo "WARNING: gh CLI not found. GitHub integration will be limited." >&2
fi

echo
echo "==> Installing smartclaw"
# Use --user to avoid PEP 668 externally-managed-environment errors on modern distros.
# If inside an active virtualenv, --user is harmless (pip ignores it in venvs).
"$PYTHON_BIN" -m pip install --upgrade --no-cache-dir --user . || {
    echo "ERROR: Failed to install package" >&2
    echo "Tip: If this fails, activate a virtual environment first:" >&2
    echo "  python3 -m venv .venv && source .venv/bin/activate && bash install.sh" >&2
    exit 1
}

echo
echo "==> Verifying installation"
"$PYTHON_BIN" -c "import orchestration; print('smartclaw: installed ✓')" || {
    echo "WARNING: smartclaw package not found."
}

echo
echo "==> Next steps"
echo "  1. Verify agent CLIs are installed: claude, codex, gemini, etc."
echo "  2. Configure credentials for your agent CLIs"
echo "  3. Import and use: python -c 'from orchestration import ...'"
echo "  4. See README.md for usage examples"
echo
echo "==> Done!"
