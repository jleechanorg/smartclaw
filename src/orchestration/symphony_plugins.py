from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkflowSpec:
    title: str
    intro: str
    requirements: list[str]


@dataclass(frozen=True)
class IssueSpec:
    issue_id: str
    identifier: str
    title: str
    description: str
    labels: list[str]


class TaskPlugin:
    name: str

    def build_workflow_spec(self) -> WorkflowSpec:
        raise NotImplementedError

    def load_issues(self, plugin_input_path: str) -> list[IssueSpec]:
        raise NotImplementedError


class PluginName(StrEnum):
    GENERIC_TASKS = "generic_tasks"
    LEETCODE_HARD = "leetcode_hard"
    SWE_BENCH_VERIFIED = "swe_bench_verified"


def _read_plugin_json(plugin_name: str, plugin_input_path: str) -> Any:
    try:
        with Path(plugin_input_path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{plugin_name}: invalid JSON in {plugin_input_path}: {exc.msg}") from exc


def _require_object(value: Any, *, plugin_name: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{plugin_name}: expected object at '{field}'")
    return value


def _require_list(value: Any, *, plugin_name: str, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{plugin_name}: expected list at '{field}'")
    return value


def _require_string(
    value: Any, *, plugin_name: str, record_idx: int, field: str, allow_empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{plugin_name}: record[{record_idx}] field '{field}' must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{plugin_name}: record[{record_idx}] field '{field}' must not be empty")
    return value


def _require_labels(value: Any, *, plugin_name: str, record_idx: int, field: str) -> list[str]:
    labels = _require_list(value, plugin_name=plugin_name, field=f"record[{record_idx}].{field}")
    if not all(isinstance(label, str) for label in labels):
        raise ValueError(f"{plugin_name}: record[{record_idx}] field '{field}' must be list[str]")
    return labels


class GenericTasksPlugin(TaskPlugin):
    name = PluginName.GENERIC_TASKS.value

    def build_workflow_spec(self) -> WorkflowSpec:
        return WorkflowSpec(
            title="General coding tasks",
            intro="Complete assigned engineering tasks with production-ready changes.",
            requirements=[
                "Implement code changes directly in the target repository workspace.",
                "Run targeted tests/lint for changed areas.",
                "Summarize what changed and any residual risks.",
                "Do not ask for user input unless truly blocked.",
            ],
        )

    def load_issues(self, plugin_input_path: str) -> list[IssueSpec]:
        plugin_name = self.name
        data = _require_object(
            _read_plugin_json(plugin_name, plugin_input_path),
            plugin_name=plugin_name,
            field="root",
        )
        tasks = _require_list(data.get("tasks"), plugin_name=plugin_name, field="tasks")

        issues: list[IssueSpec] = []
        for record_idx, raw_task in enumerate(tasks):
            task = _require_object(raw_task, plugin_name=plugin_name, field=f"tasks[{record_idx}]")
            if "id" not in task:
                raise ValueError(f"{plugin_name}: record[{record_idx}] missing required field 'id'")
            if "title" not in task:
                raise ValueError(f"{plugin_name}: record[{record_idx}] missing required field 'title'")
            idx = str(task["id"])
            title = _require_string(task["title"], plugin_name=plugin_name, record_idx=record_idx, field="title")
            description = task.get("description", "")
            if not isinstance(description, str):
                raise ValueError(f"{plugin_name}: record[{record_idx}] field 'description' must be a string")
            labels = _require_labels(task.get("labels", []), plugin_name=plugin_name, record_idx=record_idx, field="labels")
            issues.append(
                IssueSpec(
                    issue_id=f"issue-gen-{idx}",
                    identifier=f"GEN-{idx}",
                    title=title,
                    description=description,
                    labels=["general", *labels],
                )
            )
        return issues


class LeetCodeHardPlugin(TaskPlugin):
    name = PluginName.LEETCODE_HARD.value

    def build_workflow_spec(self) -> WorkflowSpec:
        return WorkflowSpec(
            title="LeetCode hard benchmark task",
            intro="Solve the assigned LeetCode Hard problems in Python.",
            requirements=[
                "Create a solutions directory with one Python file per problem slug.",
                "Create tests/test_solutions.py with correctness tests for all assigned problems.",
                "Run python3 -m pytest -q.",
                "Write the command output to bench_report.txt.",
                "If pytest fails, fix and rerun until passing.",
            ],
        )

    def load_issues(self, plugin_input_path: str) -> list[IssueSpec]:
        plugin_name = self.name
        data = _require_object(
            _read_plugin_json(plugin_name, plugin_input_path),
            plugin_name=plugin_name,
            field="root",
        )
        problems = _require_list(data.get("problems"), plugin_name=plugin_name, field="problems")

        issues: list[IssueSpec] = []
        for record_idx, raw_problem in enumerate(problems):
            problem = _require_object(
                raw_problem, plugin_name=plugin_name, field=f"problems[{record_idx}]"
            )
            for required_field in ("id", "slug", "title"):
                if required_field not in problem:
                    raise ValueError(
                        f"{plugin_name}: record[{record_idx}] missing required field '{required_field}'"
                    )
            pid = str(problem["id"])
            slug = _require_string(
                problem["slug"], plugin_name=plugin_name, record_idx=record_idx, field="slug"
            )
            title = _require_string(
                problem["title"], plugin_name=plugin_name, record_idx=record_idx, field="title"
            )
            issues.append(
                IssueSpec(
                    issue_id=f"issue-lc-{pid}",
                    identifier=f"LC-{pid}",
                    title=title,
                    description=(
                        f"Solve LeetCode {pid} {title} ({slug}) in Python. "
                        "Create tests and record pytest output in bench_report.txt."
                    ),
                    labels=["benchmark", "leetcode", "hard", slug],
                )
            )
        return issues


class SweBenchVerifiedPlugin(TaskPlugin):
    name = PluginName.SWE_BENCH_VERIFIED.value

    def build_workflow_spec(self) -> WorkflowSpec:
        return WorkflowSpec(
            title="SWE-bench Verified benchmark task",
            intro="Resolve assigned SWE-bench Verified instances by patching code and validating tests.",
            requirements=[
                "Read each instance context and implement a minimal fix.",
                "Create a patch summary in bench_report.txt with files touched and rationale.",
                "Run relevant tests for each instance and include pass/fail output.",
                "If tests fail, iterate until passing or document a concrete blocker.",
                "Do not ask for user input.",
            ],
        )

    def load_issues(self, plugin_input_path: str) -> list[IssueSpec]:
        plugin_name = self.name
        data = _require_object(
            _read_plugin_json(plugin_name, plugin_input_path),
            plugin_name=plugin_name,
            field="root",
        )
        instances = _require_list(data.get("instances"), plugin_name=plugin_name, field="instances")

        issues: list[IssueSpec] = []
        for record_idx, raw_instance in enumerate(instances):
            instance = _require_object(
                raw_instance, plugin_name=plugin_name, field=f"instances[{record_idx}]"
            )
            for required_field in ("instance_id", "repo", "base_commit", "problem_statement"):
                if required_field not in instance:
                    raise ValueError(
                        f"{plugin_name}: record[{record_idx}] missing required field '{required_field}'"
                    )
            instance_id = _require_string(
                instance["instance_id"],
                plugin_name=plugin_name,
                record_idx=record_idx,
                field="instance_id",
            )
            repo = _require_string(instance["repo"], plugin_name=plugin_name, record_idx=record_idx, field="repo")
            base_commit = _require_string(
                instance["base_commit"], plugin_name=plugin_name, record_idx=record_idx, field="base_commit"
            )
            problem_statement = _require_string(
                instance["problem_statement"],
                plugin_name=plugin_name,
                record_idx=record_idx,
                field="problem_statement",
                allow_empty=True,
            )
            issues.append(
                IssueSpec(
                    issue_id=f"issue-swe-{instance_id}",
                    identifier=f"SWE-{instance_id}",
                    title=f"{repo} {instance_id}",
                    description=(
                        f"SWE-bench Verified instance {instance_id} in {repo}. "
                        f"Base commit: {base_commit}.\n"
                        f"Problem statement:\n{problem_statement}"
                    ),
                    labels=["benchmark", "swe-bench-verified", repo],
                )
            )
        return issues


_PLUGINS: dict[str, type[TaskPlugin]] = {
    PluginName.GENERIC_TASKS.value: GenericTasksPlugin,
    PluginName.LEETCODE_HARD.value: LeetCodeHardPlugin,
    PluginName.SWE_BENCH_VERIFIED.value: SweBenchVerifiedPlugin,
}


def list_plugins() -> list[str]:
    return sorted(_PLUGINS)


def load_plugin(name: str) -> TaskPlugin:
    if name not in _PLUGINS:
        available = ", ".join(list_plugins())
        raise ValueError(f"Unknown plugin: {name}. Available: {available}")
    return _PLUGINS[name]()
