#!/usr/bin/env bash
# Render a concrete AO config for local runtime use.
#
# Source config stays tracked in the repo (`~/.smartclaw/agent-orchestrator.yaml`).
# Runtime config is written to `~/.agent-orchestrator.yaml` with shell placeholders
# resolved from the current environment first, then from an interactive bash login
# shell (`bash -lic`) so ~/.bashrc-backed secrets are available to launchd flows.

set -euo pipefail

QUIET=0
JSON_MODE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet)
      QUIET=1
      shift
      ;;
    --json)
      JSON_MODE=1
      shift
      ;;
    --help|-h)
      cat <<'EOF'
Usage: render-agent-orchestrator-config.sh [--quiet] [--json] [source-path] [output-path]
EOF
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

SOURCE_PATH="${1:-$HOME/.smartclaw/agent-orchestrator.yaml}"
OUTPUT_PATH="${2:-$HOME/.agent-orchestrator.yaml}"

if [[ ! -f "$SOURCE_PATH" ]]; then
  echo "ERROR: source AO config not found: $SOURCE_PATH" >&2
  exit 1
fi

python3 - "$SOURCE_PATH" "$OUTPUT_PATH" "$QUIET" "$JSON_MODE" <<'PY'
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

source_path = Path(sys.argv[1]).expanduser()
output_path = Path(sys.argv[2]).expanduser()
quiet = bool(int(sys.argv[3]))
json_mode = bool(int(sys.argv[4]))
text = source_path.read_text(encoding="utf-8")

pattern = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}")
matches = list(pattern.finditer(text))
vars_needed = sorted({m.group(1) for m in matches})

def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)

shell_env: dict[str, str] = {}
if vars_needed:
    names_json = json.dumps(vars_needed)
    shell_cmd = (
        "python3 - <<'PY'\n"
        "import json, os\n"
        f"names = json.loads({names_json!r})\n"
        "print(json.dumps({name: os.environ[name] for name in names if name in os.environ}))\n"
        "PY"
    )
    try:
        proc = subprocess.run(
            ["bash", "-lic", shell_cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        proc = None
        warn(f"bash -lic timed out after {exc.timeout}s while resolving AO config placeholders")
    if proc and proc.returncode == 0 and proc.stdout.strip():
        for line in reversed([line for line in proc.stdout.splitlines() if line.strip()]):
            try:
                shell_env = json.loads(line)
                break
            except json.JSONDecodeError as exc:
                parse_error = exc
        else:
            shell_env = {}
            stdout_preview = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            warn(
                "failed to parse login-shell env JSON; "
                f"last parse error={parse_error}; last stdout line={stdout_preview!r}"
            )
    elif proc and proc.returncode != 0:
        stderr = proc.stderr.strip()
        stdout = proc.stdout.strip()
        details = [f"rc={proc.returncode}"]
        if stderr:
            details.append(f"stderr={stderr!r}")
        if stdout:
            details.append(f"stdout={stdout!r}")
        warn("login shell subprocess failed: " + ", ".join(details))

resolved_from_shell = 0
used_defaults: set[str] = set()
unresolved: set[str] = set()

def replace(match: re.Match[str]) -> str:
    global resolved_from_shell
    name = match.group(1)
    default = match.group(2)
    if name in os.environ:
        return os.environ[name]
    if name in shell_env:
        resolved_from_shell += 1
        return shell_env[name]
    if default is not None:
        used_defaults.add(name)
        return default
    unresolved.add(name)
    return match.group(0)

rendered = pattern.sub(replace, text)
summary = {
    "rendered_path": str(output_path),
    "resolved_from_shell": resolved_from_shell,
    "used_defaults": sorted(used_defaults),
    "unresolved": sorted(unresolved),
}

if unresolved:
    warn("unresolved placeholders left in output: " + ", ".join(sorted(unresolved)))
    if json_mode:
        print(json.dumps(summary))
    sys.exit(1)

output_path.parent.mkdir(parents=True, exist_ok=True)
fd, temp_path_str = tempfile.mkstemp(
    prefix=output_path.name + ".",
    suffix=".tmp",
    dir=output_path.parent,
    text=True,
)
temp_path = Path(temp_path_str)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, output_path)
finally:
    if temp_path.exists():
        temp_path.unlink()

if json_mode:
    print(json.dumps(summary))
elif not quiet:
    print(f"Rendered {output_path}")
    if resolved_from_shell:
        print(f"Resolved from bash -lic: {resolved_from_shell}")
    if used_defaults:
        print("Used defaults for: " + ", ".join(sorted(used_defaults)))
PY
