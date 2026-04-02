#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestration.symphony_plugins import load_plugin


def main() -> None:
    plugin_name = os.environ.get("SYMPHONY_TASK_PLUGIN", "generic_tasks")
    plugin_input = os.environ.get("SYMPHONY_TASK_PLUGIN_INPUT")
    output_path = os.environ.get("SYMPHONY_TASK_ISSUES_JSON")

    if not plugin_input or not output_path:
        raise SystemExit("SYMPHONY_TASK_PLUGIN_INPUT and SYMPHONY_TASK_ISSUES_JSON are required")

    plugin = load_plugin(plugin_name)
    issues = plugin.load_issues(plugin_input)

    payload = {
        "plugin": plugin_name,
        "issues": [
            {
                "id": i.issue_id,
                "identifier": i.identifier,
                "title": i.title,
                "description": i.description,
                "labels": i.labels,
                "state": "Todo",
                "assigned_to_worker": True,
            }
            for i in issues
        ],
    }

    Path(output_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"plugin={plugin_name}")
    print(f"issues={len(issues)}")
    print(f"output={output_path}")


if __name__ == "__main__":
    main()
