"""Fail-closed scheduling and completion gates for locked phase tasks.

The project contract decides *what* must run.  This module turns that immutable
task graph into deterministic execution waves and verifies that every task and
acceptance criterion has runner-controlled evidence before a phase can pass.
It deliberately has no FastAPI or Agent implementation dependencies so every
execution entry point can reuse the same policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Sequence

from core.evidence import (
    EvidenceKind,
    EvidenceRecord,
    create_evidence,
    validate_evidence_record,
)
from core.project_contract import phase_task_contract
from core.role_mapping import canonical_expert_type


PHASE_EXECUTION_SCHEMA_VERSION = 1
SUCCESS_STATUSES = frozenset({"completed", "succeeded", "passed"})
_SHA256 = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_RUNTIME_CRITERION = re.compile(
    r"\b(?:api|endpoint|http|runtime|health|request|response|status code|docker)\b"
    r"|接口|运行时|健康检查|请求|响应",
    re.IGNORECASE,
)
_COMMAND_CRITERION = re.compile(
    r"\b(?:command|install|test|tests|suite|regression|pytest|unittest|npm test|build|compile|lint|exit code)\b"
    r"|命令|测试|构建|编译|退出码",
    re.IGNORECASE,
)
_COMMAND_EXECUTION_ASSERTION = re.compile(
    r"\b(?:execute|executes|executed|pass|passes|passed|"
    r"succeed|succeeds|succeeded|successful|successfully|builds|built|"
    r"exit|exits|exited|error[- ]free|without errors?|no errors?)\b"
    r"|运行|执行|通过|成功|退出|无报错|不报错",
    re.IGNORECASE,
)
_CONTENT_ASSERTION = re.compile(
    r"\b(?:contain|contains|containing|define|defines|declares?|includes?|"
    r"exports?|fields?|scripts?|dependencies|configured?)\b"
    r"|包含|定义|声明|导出|字段|脚本|依赖|配置|源码|代码|路由",
    re.IGNORECASE,
)
_API_EXECUTION_ASSERTION = re.compile(
    r"\b(?:http|curl|request|response|runtime|health|probe|api tests?|"
    r"integration tests?|call|invoke)\b"
    r"|请求|响应|调用|运行|执行|实测|接口测试|集成测试",
    re.IGNORECASE,
)
_HTTP_STATUS_ASSERTION = re.compile(
    r"\b(?:returns?|status(?:\s+code)?(?:\s+is)?)\s*[:=]?\s*\d{3}\b"
    r"|(?:响应码|状态码)\s*[:=]?\s*\d{3}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PhaseExecutionIssue:
    layer: str
    code: str
    path: str
    message: str
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


@dataclass(frozen=True)
class PhaseExecutionValidation:
    valid: bool
    issues: tuple[PhaseExecutionIssue, ...]
    schema_version: int = PHASE_EXECUTION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "schema_version": self.schema_version,
            "issues": [issue.to_dict() for issue in self.issues],
        }


class PhaseExecutionContractError(RuntimeError):
    """Raised before or during dispatch when the locked execution contract fails."""

    def __init__(self, message: str, issues: Sequence[PhaseExecutionIssue] = ()):
        super().__init__(message)
        self.issues = tuple(issues)


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    return tuple(dict.fromkeys(
        text
        for item in values
        if (text := str(item or "").strip())
    ))


def _role_key(value: Any) -> str:
    role = str(value or "").strip()
    return canonical_expert_type(role) or role.casefold()


def _criteria(value: Any) -> list[Any]:
    values = value if isinstance(value, (list, tuple)) else [value]
    result: list[Any] = []
    for item in values:
        if isinstance(item, Mapping):
            criterion = str(
                item.get("criterion") or item.get("text") or ""
            ).strip()
            if criterion:
                result.append({**dict(item), "criterion": criterion})
        elif (criterion := str(item or "").strip()):
            result.append(criterion)
    return result


def _phase_id(phase: Mapping[str, Any], index: int) -> str:
    return str(phase.get("phase_id") or f"phase-{index + 1}").strip()


def _issue(
    code: str,
    path: str,
    message: str,
    expected: Any = None,
    actual: Any = None,
) -> PhaseExecutionIssue:
    return PhaseExecutionIssue(
        layer="phase_execution",
        code=code,
        path=path,
        message=message,
        expected=expected,
        actual=actual,
    )


def _merged_phase_tasks(
    phase: Mapping[str, Any],
) -> tuple[list[Dict[str, Any]], list[PhaseExecutionIssue]]:
    """Merge executable role choices into locked tasks without weakening them."""
    issues: list[PhaseExecutionIssue] = []
    locked = phase_task_contract(phase)
    requirements = [
        item for item in (phase.get("expert_requirements") or [])
        if isinstance(item, Mapping)
    ]
    requirement_by_id = {
        str(item.get("task_id") or "").strip(): item for item in requirements
    }
    phase_plan = (
        phase.get("phase_plan")
        if isinstance(phase.get("phase_plan"), Mapping)
        else {}
    )
    planned_task_by_id = {
        str(item.get("task_id") or "").strip(): item
        for item in (phase_plan.get("tasks") or [])
        if isinstance(item, Mapping)
    }
    planned_assignments_by_task: dict[str, list[Dict[str, Any]]] = {}
    for assignment in phase_plan.get("assignments") or []:
        if not isinstance(assignment, Mapping):
            continue
        for assigned_task_id in _strings(assignment.get("task_ids")):
            planned_assignments_by_task.setdefault(
                assigned_task_id, [],
            ).append(dict(assignment))
    if requirements:
        locked_ids = [str(item.get("task_id") or "").strip() for item in locked]
        requirement_ids = [
            str(item.get("task_id") or "").strip() for item in requirements
        ]
        if requirement_ids != locked_ids:
            issues.append(_issue(
                "executable_task_contract_drift",
                "$.expert_requirements",
                "executable task IDs/order differ from the locked task contract",
                locked_ids,
                requirement_ids,
            ))

    merged: list[Dict[str, Any]] = []
    phase_roles = _strings(phase.get("roles_needed") or phase.get("roles"))
    for index, raw in enumerate(locked):
        task = dict(raw)
        task_id = str(task.get("task_id") or "").strip()
        requirement = requirement_by_id.get(task_id, {})
        planned_task = planned_task_by_id.get(task_id, {})
        task_roles = _strings(task.get("roles") or task.get("required_role"))
        required_role = str(
            requirement.get("required_role")
            or task.get("required_role")
            or (task_roles[0] if task_roles else "")
        ).strip()
        task["task_id"] = task_id
        task["required_role"] = required_role
        task["dependencies"] = list(_strings(
            task.get("dependencies")
            if "dependencies" in task
            else requirement.get("dependencies")
        ))
        task["acceptance_criteria"] = _criteria(
            requirement.get("acceptance_criteria")
            if requirement.get("acceptance_criteria")
            else task.get("acceptance_criteria")
        )
        task["source_requirement_ids"] = list(_strings(
            task.get("source_requirement_ids")
            if task.get("source_requirement_ids")
            else requirement.get("source_requirement_ids")
        ))
        # The confirmed phase plan is the authoritative execution description.
        # Keep its task-level fields intact through scheduling; the locked
        # contract remains authoritative for IDs, dependencies, roles and
        # acceptance criteria above.
        if planned_task:
            task["name"] = str(planned_task.get("name") or task.get("name") or "")
            task["objective"] = str(
                planned_task.get("objective")
                or task.get("objective")
                or task.get("description")
                or ""
            )
            task["functional_details"] = list(
                planned_task.get("functional_details") or []
            )
            task["implementation"] = str(
                planned_task.get("implementation")
                or task.get("implementation")
                or ""
            )
            task["assignments"] = list(
                planned_assignments_by_task.get(task_id) or []
            )
            if task["assignments"]:
                task["assignment"] = dict(task["assignments"][0])
            task["effective_technical_requirements"] = [
                dict(item) if isinstance(item, Mapping) else item
                for item in (
                    phase_plan.get("effective_technical_requirements") or []
                )
            ]
        task["required_files"] = list(_strings(
            task.get("required_files")
            if task.get("required_files") is not None
            else requirement.get("required_files")
        ))
        task["_phase_roles"] = list(phase_roles)
        task["_task_index"] = index
        merged.append(task)

    # A later task that writes a file already assigned to an earlier task is
    # necessarily a dependent modification, even when the model omitted that
    # edge. Persisted task order is authoritative, so derive the minimum
    # serialization edge instead of allowing parallel writers to deadlock.
    last_owner_by_path: dict[str, str] = {}
    serialize_unknown_workspace = any(
        not _strings(task.get("required_files"))
        for task in merged
    )
    last_task_by_role: dict[str, str] = {}
    for task in merged:
        task_id = str(task.get("task_id") or "")
        dependencies = list(_strings(task.get("dependencies")))
        role = _role_key(task.get("required_role"))
        previous_task_id = last_task_by_role.get(role, "")
        if (
            serialize_unknown_workspace
            and role
            and previous_task_id
            and previous_task_id not in dependencies
        ):
            dependencies.append(previous_task_id)
        for raw_path in _strings(task.get("required_files")):
            path = raw_path.replace("\\", "/").casefold()
            previous_owner = last_owner_by_path.get(path)
            if (
                previous_owner
                and previous_owner != task_id
                and previous_owner not in dependencies
            ):
                dependencies.append(previous_owner)
            last_owner_by_path[path] = task_id
        task["dependencies"] = dependencies
        if role:
            last_task_by_role[role] = task_id
    return merged, issues


def validate_task_graph(
    phases: Sequence[Mapping[str, Any]],
    *,
    target_phase_id: str | None = None,
) -> PhaseExecutionValidation:
    """Validate a whole project or one target phase and its dependency closure."""
    issues: list[PhaseExecutionIssue] = []
    phase_rows: list[
        tuple[int, str, Mapping[str, Any], list[Dict[str, Any]]]
    ] = []
    task_locations: dict[str, tuple[int, int]] = {}
    graph: dict[str, tuple[str, ...]] = {}

    included_indices = set(range(len(phases)))
    if target_phase_id is not None:
        indices_by_id: dict[str, list[int]] = {}
        for index, phase in enumerate(phases):
            indices_by_id.setdefault(_phase_id(phase, index), []).append(index)
        target_id = str(target_phase_id)
        if target_id not in indices_by_id:
            return PhaseExecutionValidation(False, (_issue(
                "phase_not_found",
                "$.phase_id",
                f"phase not found: {target_id}",
            ),))
        included_indices = set()
        pending = [target_id]
        visited_phase_ids: set[str] = set()
        while pending:
            phase_id = pending.pop()
            if phase_id in visited_phase_ids:
                continue
            visited_phase_ids.add(phase_id)
            for phase_index in indices_by_id.get(phase_id, []):
                included_indices.add(phase_index)
                pending.extend(_strings(phases[phase_index].get("dependencies")))

    seen_phase_ids: set[str] = set()
    for phase_index, phase in enumerate(phases):
        if phase_index not in included_indices:
            continue
        phase_id = _phase_id(phase, phase_index)
        if phase_id in seen_phase_ids:
            issues.append(_issue(
                "phase_id_duplicate",
                f"$.phases[{phase_index}].phase_id",
                f"phase_id must be unique: {phase_id}",
            ))
        seen_phase_ids.add(phase_id)
        roles = _strings(phase.get("roles_needed") or phase.get("roles"))
        role_keys = {_role_key(role) for role in roles}
        if not roles:
            issues.append(_issue(
                "phase_roles_missing",
                f"$.phases[{phase_index}].roles_needed",
                f"phase {phase_id} must declare roles_needed",
            ))
        tasks, merge_issues = _merged_phase_tasks(phase)
        issues.extend(PhaseExecutionIssue(
            issue.layer,
            issue.code,
            f"$.phases[{phase_index}]{issue.path[1:]}",
            issue.message,
            issue.expected,
            issue.actual,
        ) for issue in merge_issues)
        if not tasks:
            issues.append(_issue(
                "phase_tasks_missing",
                f"$.phases[{phase_index}].task_contract",
                f"phase {phase_id} must contain locked tasks",
            ))
        phase_rows.append((phase_index, phase_id, phase, tasks))
        for task_index, task in enumerate(tasks):
            task_id = str(task.get("task_id") or "").strip()
            path = f"$.phases[{phase_index}].task_contract[{task_index}]"
            if not task_id:
                issues.append(_issue(
                    "task_id_missing", f"{path}.task_id",
                    "locked task_id must be non-empty",
                ))
                continue
            if task_id in task_locations:
                issues.append(_issue(
                    "task_id_duplicate", f"{path}.task_id",
                    f"task_id must be globally unique: {task_id}",
                ))
            else:
                task_locations[task_id] = (phase_index, task_index)
            role = str(task.get("required_role") or "").strip()
            if not role:
                issues.append(_issue(
                    "task_required_role_missing", f"{path}.required_role",
                    f"locked task {task_id} must choose required_role",
                ))
            elif _role_key(role) not in role_keys:
                issues.append(_issue(
                    "task_required_role_outside_phase",
                    f"{path}.required_role",
                    f"locked task {task_id} requires a role outside phase roles_needed",
                    list(roles),
                    role,
                ))
            graph[task_id] = _strings(task.get("dependencies"))

    phase_index_by_id = {
        phase_id: phase_index
        for phase_index, phase_id, _phase, _tasks in phase_rows
    }
    for phase_index, phase_id, phase, tasks in phase_rows:
        phase_dependencies = _strings(phase.get("dependencies"))
        for dependency in phase_dependencies:
            path = f"$.phases[{phase_index}].dependencies"
            dependency_index = phase_index_by_id.get(dependency)
            if dependency_index is None:
                issues.append(_issue(
                    "phase_dependency_unknown", path,
                    f"phase {phase_id} depends on unknown phase {dependency}",
                ))
            elif dependency_index >= phase_index:
                issues.append(_issue(
                    "phase_dependency_not_prior", path,
                    f"phase {phase_id} depends on non-prior phase {dependency}",
                ))

        for task_index, task in enumerate(tasks):
            task_id = str(task.get("task_id") or "").strip()
            seen_dependencies: set[str] = set()
            for dependency in _strings(task.get("dependencies")):
                path = (
                    f"$.phases[{phase_index}].task_contract"
                    f"[{task_index}].dependencies"
                )
                if dependency in seen_dependencies:
                    issues.append(_issue(
                        "task_dependency_duplicate", path,
                        f"task {task_id} repeats dependency {dependency}",
                    ))
                seen_dependencies.add(dependency)
                location = task_locations.get(dependency)
                if location is None:
                    issues.append(_issue(
                        "task_dependency_unknown", path,
                        f"task {task_id} depends on unknown task {dependency}",
                    ))
                elif location[0] > phase_index:
                    issues.append(_issue(
                        "task_dependency_future_phase", path,
                        f"task {task_id} depends on future-phase task {dependency}",
                    ))

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        cyclic = any(
            dependency in graph and visit(dependency)
            for dependency in graph.get(task_id, ())
        )
        visiting.remove(task_id)
        visited.add(task_id)
        return cyclic

    if any(visit(task_id) for task_id in graph):
        issues.append(_issue(
            "task_dependency_cycle",
            "$.phases",
            "locked task dependency graph contains a cycle",
        ))
    return PhaseExecutionValidation(not issues, tuple(issues))


def validate_task_owners(
    phase: Mapping[str, Any],
    assignments: Sequence[Mapping[str, Any]],
) -> PhaseExecutionValidation:
    """Require every locked task to be claimed by exactly one eligible Agent."""
    issues: list[PhaseExecutionIssue] = []
    tasks, merge_issues = _merged_phase_tasks(phase)
    issues.extend(merge_issues)
    tasks_by_id = {
        str(task.get("task_id") or "").strip(): task for task in tasks
    }
    owners_by_task: dict[str, list[tuple[int, str, str]]] = {}
    for index, assignment in enumerate(assignments):
        agent_id = str(
            assignment.get("agent_id") or assignment.get("id") or ""
        ).strip()
        role = str(
            assignment.get("required_role")
            or assignment.get("agent_role")
            or ""
        ).strip()
        if not agent_id:
            issues.append(_issue(
                "task_owner_agent_missing",
                f"$.assignments[{index}].agent_id",
                "task assignment must identify an Agent",
            ))
        task_ids = _strings(
            assignment.get("task_ids")
            or assignment.get("locked_task_ids")
            or assignment.get("assigned_task_ids")
        )
        if not task_ids:
            issues.append(_issue(
                "task_owner_claims_missing",
                f"$.assignments[{index}].task_ids",
                f"Agent {agent_id or index} does not claim any locked task",
            ))
        for task_id in task_ids:
            if task_id not in tasks_by_id:
                issues.append(_issue(
                    "task_owner_unknown_task",
                    f"$.assignments[{index}].task_ids",
                    f"Agent {agent_id or index} claims unknown task {task_id}",
                ))
                continue
            owners_by_task.setdefault(task_id, []).append((index, agent_id, role))

    for task_index, (task_id, task) in enumerate(tasks_by_id.items()):
        owners = owners_by_task.get(task_id, [])
        path = f"$.task_contract[{task_index}]"
        if not owners:
            issues.append(_issue(
                "task_owner_missing",
                f"{path}.task_id",
                f"locked task {task_id} has no Agent owner",
            ))
            continue
        if len(owners) != 1:
            issues.append(_issue(
                "task_owner_duplicate",
                f"{path}.task_id",
                f"locked task {task_id} has multiple Agent owners",
                1,
                len(owners),
            ))
            continue
        _index, agent_id, role = owners[0]
        required_role = str(task.get("required_role") or "").strip()
        if (
            role
            and required_role
            and _role_key(role) != _role_key(required_role)
        ):
            issues.append(_issue(
                "task_owner_role_mismatch",
                f"{path}.required_role",
                f"Agent {agent_id} role does not match task {task_id}",
                required_role,
                role,
            ))
    return PhaseExecutionValidation(not issues, tuple(issues))


def build_phase_dispatch_plan(
    phases: Sequence[Mapping[str, Any]],
    phase_id: str,
    assignments: Sequence[Mapping[str, Any]],
    *,
    completed_task_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Return dependency-safe waves, serializing tasks owned by one Agent."""
    graph_validation = validate_task_graph(
        phases,
        target_phase_id=phase_id,
    )
    target_index = next(
        (
            index for index, phase in enumerate(phases)
            if _phase_id(phase, index) == str(phase_id)
        ),
        None,
    )
    if target_index is None:
        issue = _issue(
            "phase_not_found", "$.phase_id",
            f"phase not found: {phase_id}",
        )
        raise PhaseExecutionContractError(issue.message, (issue,))
    phase = phases[target_index]
    owner_validation = validate_task_owners(phase, assignments)
    issues = [*graph_validation.issues, *owner_validation.issues]
    if issues:
        raise PhaseExecutionContractError(
            "phase execution contract is invalid",
            issues,
        )

    tasks, _ = _merged_phase_tasks(phase)
    task_by_id = {
        str(task.get("task_id") or "").strip(): task for task in tasks
    }
    owner_by_task: dict[str, str] = {}
    for assignment in assignments:
        agent_id = str(
            assignment.get("agent_id") or assignment.get("id") or ""
        ).strip()
        for task_id in _strings(
            assignment.get("task_ids")
            or assignment.get("locked_task_ids")
            or assignment.get("assigned_task_ids")
        ):
            owner_by_task[task_id] = agent_id

    complete = set(_strings(completed_task_ids))
    target_ids = set(task_by_id)
    incomplete_external: list[PhaseExecutionIssue] = []
    for task_index, task in enumerate(tasks):
        task_id = str(task.get("task_id") or "").strip()
        for dependency in _strings(task.get("dependencies")):
            if dependency not in target_ids and dependency not in complete:
                incomplete_external.append(_issue(
                    "task_dependency_incomplete",
                    f"$.task_contract[{task_index}].dependencies",
                    f"task {task_id} dependency is not completed: {dependency}",
                ))
    for dependency_phase in _strings(phase.get("dependencies")):
        dependency_index = next(
            (
                index for index, candidate in enumerate(phases)
                if _phase_id(candidate, index) == dependency_phase
            ),
            None,
        )
        if dependency_index is None:
            continue
        dependency_tasks, _ = _merged_phase_tasks(phases[dependency_index])
        missing = [
            str(task.get("task_id") or "")
            for task in dependency_tasks
            if str(task.get("task_id") or "") not in complete
        ]
        if missing:
            incomplete_external.append(_issue(
                "phase_dependency_incomplete",
                "$.dependencies",
                f"phase {phase_id} dependency {dependency_phase} is incomplete",
                [],
                missing,
            ))
    inherited_phase_task_ids = [
        str(task.get("task_id") or "")
        for dependency_phase in _strings(phase.get("dependencies"))
        for dependency_index, candidate in enumerate(phases)
        if _phase_id(candidate, dependency_index) == dependency_phase
        for task in _merged_phase_tasks(candidate)[0]
        if str(task.get("task_id") or "")
    ]
    if incomplete_external:
        raise PhaseExecutionContractError(
            "phase dependencies are incomplete",
            incomplete_external,
        )

    remaining = set(target_ids)
    scheduled: set[str] = set()
    waves: list[list[Dict[str, Any]]] = []
    workspace_serialized = (
        str(
            (
                phase.get("phase_plan")
                if isinstance(phase.get("phase_plan"), Mapping)
                else {}
            ).get("schema_version") or ""
        ) == "phase-plan/v1"
        and any(
            not _strings(task.get("required_files"))
            for task in tasks
        )
    )
    task_order = {
        str(task.get("task_id") or ""): index
        for index, task in enumerate(tasks)
    }
    while remaining:
        ready = [
            task_id for task_id in remaining
            if all(
                dependency in scheduled or dependency in complete
                for dependency in _strings(task_by_id[task_id].get("dependencies"))
            )
        ]
        ready.sort(key=task_order.__getitem__)
        selected: list[str] = []
        busy_agents: set[str] = set()
        for task_id in ready:
            if workspace_serialized and selected:
                break
            agent_id = owner_by_task[task_id]
            if agent_id in busy_agents:
                continue
            busy_agents.add(agent_id)
            selected.append(task_id)
        if not selected:
            issue = _issue(
                "task_dependency_cycle",
                "$.task_contract",
                "no dependency-safe task can be scheduled",
            )
            raise PhaseExecutionContractError(issue.message, (issue,))
        wave: list[Dict[str, Any]] = []
        for task_id in selected:
            task = task_by_id[task_id]
            wave.append({
                "task_id": task_id,
                "agent_id": owner_by_task[task_id],
                "name": task.get("name"),
                "objective": task.get("objective"),
                "functional_details": list(
                    task.get("functional_details") or []
                ),
                "implementation": task.get("implementation"),
                "effective_technical_requirements": list(
                    task.get("effective_technical_requirements") or []
                ),
                "assignments": [
                    dict(item)
                    for item in (task.get("assignments") or [])
                    if isinstance(item, Mapping)
                ],
                "assignment": (
                    dict(task["assignment"])
                    if isinstance(task.get("assignment"), Mapping)
                    else None
                ),
                "required_files": list(_strings(
                    task.get("required_files")
                )),
                "required_role": task.get("required_role"),
                "dependencies": list(dict.fromkeys([
                    *_strings(task.get("dependencies")),
                    *inherited_phase_task_ids,
                ])),
                "acceptance_criteria": acceptance_criterion_contracts(
                    task_id,
                    task.get("acceptance_criteria") or [],
                ),
                "source_requirement_ids": list(_strings(
                    task.get("source_requirement_ids")
                )),
            })
        waves.append(wave)
        scheduled.update(selected)
        remaining.difference_update(selected)

    return {
        "schema_version": PHASE_EXECUTION_SCHEMA_VERSION,
        "phase_id": str(phase_id),
        "task_ids": [
            str(task.get("task_id") or "").strip() for task in tasks
        ],
        "waves": waves,
    }


async def execute_phase_dispatch_plan(
    plan: Mapping[str, Any],
    executor: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
) -> Dict[str, Any]:
    """Execute each DAG wave; a failed task prevents every downstream wave."""
    results: list[Dict[str, Any]] = []
    completed: list[str] = []
    for wave_index, wave in enumerate(plan.get("waves") or []):
        raw_results = await asyncio.gather(
            *(executor(task) for task in wave),
            return_exceptions=True,
        )
        failures: list[PhaseExecutionIssue] = []
        for task, raw in zip(wave, raw_results):
            task_id = str(task.get("task_id") or "")
            if isinstance(raw, BaseException):
                failures.append(_issue(
                    "task_execution_raised",
                    f"$.waves[{wave_index}]",
                    f"task {task_id} raised {type(raw).__name__}",
                ))
                continue
            result = dict(raw or {})
            success = result.get("success") is True and str(
                result.get("status") or "completed"
            ).lower() in SUCCESS_STATUSES
            results.append({"task_id": task_id, **result})
            if success:
                completed.append(task_id)
            else:
                failures.append(_issue(
                    "task_execution_failed",
                    f"$.waves[{wave_index}]",
                    f"task {task_id} did not complete successfully",
                ))
        if failures:
            raise PhaseExecutionContractError(
                "task DAG execution stopped after a failed wave",
                failures,
            )
    return {
        "success": True,
        "schema_version": PHASE_EXECUTION_SCHEMA_VERSION,
        "phase_id": str(plan.get("phase_id") or ""),
        "completed_task_ids": completed,
        "results": results,
    }


def _normalize_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _criterion_source_class(
    criterion: str,
    artifact_paths: Sequence[str],
) -> tuple[str, tuple[str, ...]]:
    normalized = criterion.replace("\\", "/").casefold()
    executable_command_tokens = (
        "pytest", "npm install", "npm ci", "npm test", "npm run build",
        "install", "test", "build",
        "compile", "lint", "regression", "suite",
    )
    has_executable_command = any(
        token in normalized for token in executable_command_tokens
    )
    canonical_paths = tuple(dict.fromkeys(
        path
        for raw_path in artifact_paths
        if (path := _normalize_path(raw_path))
    ))
    mentioned = tuple(
        path for path in canonical_paths
        if path and re.search(
            rf"(?<![A-Za-z0-9_.@/-]){re.escape(path.casefold())}"
            r"(?![A-Za-z0-9_.@/-])",
            normalized,
        )
    )
    runtime_match = _RUNTIME_CRITERION.search(criterion)
    api_execution_requested = bool(
        _API_EXECUTION_ASSERTION.search(criterion)
        or _HTTP_STATUS_ASSERTION.search(criterion)
    )
    content_assertion = bool(_CONTENT_ASSERTION.search(criterion))
    endpoint_match = re.search(
        r"(?:\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)"
        r"(/[A-Za-z0-9_./{}:@?=&%-]+)",
        criterion,
        re.IGNORECASE,
    )
    if mentioned:
        if runtime_match and endpoint_match and api_execution_requested:
            return "api_runtime", ()
        if (
            _COMMAND_CRITERION.search(criterion)
            and _COMMAND_EXECUTION_ASSERTION.search(criterion)
            and has_executable_command
        ):
            return "command_test", ()
        # A file declaring a script/command is artifact content. Vague
        # runtime claims remain semantic; neither can borrow file bytes as
        # proof that execution occurred.
        if runtime_match or content_assertion:
            return "semantic", ()
        return "required_file", mentioned
    # Runtime evidence is only machine-verifiable when the criterion names an
    # endpoint. Vague UI/runtime wording must remain semantic so the reviewer
    # can produce an exact observation instead of an impossible empty API spec.
    if runtime_match and endpoint_match and api_execution_requested:
        return "api_runtime", ()
    # A sentence merely saying that documentation "contains commands" is a
    # content assertion, not an executable command test.
    if (
        _COMMAND_CRITERION.search(criterion)
        and _COMMAND_EXECUTION_ASSERTION.search(criterion)
        and has_executable_command
    ):
        return "command_test", ()
    return "semantic", ()


def _criterion_evidence_spec(
    criterion: str,
    artifact_paths: Sequence[str],
) -> Dict[str, Any]:
    source_class, mentioned_paths = _criterion_source_class(
        criterion, artifact_paths,
    )
    spec: Dict[str, Any] = {"source_class": source_class}
    if source_class == "required_file":
        spec["paths"] = list(mentioned_paths)
    elif source_class == "api_runtime":
        endpoint_match = re.search(
            r"(?:\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)"
            r"(/[A-Za-z0-9_./{}:@?=&%-]+)",
            criterion,
            re.IGNORECASE,
        )
        status_match = re.search(
            r"(?:returns?|status(?:\s+code)?(?:\s+is)?|响应(?:码)?)"
            r"\s*[:=]?\s*(\d{3})",
            criterion,
            re.IGNORECASE,
        )
        spec["endpoint"] = endpoint_match.group(1) if endpoint_match else ""
        spec["status_code"] = (
            int(status_match.group(1)) if status_match else None
        )
        spec["requires_assertions"] = True
    elif source_class == "command_test":
        normalized = criterion.casefold()
        command_tokens = [
            token for token in (
                "pytest", "npm test", "npm run build", "test", "build",
                "install", "compile", "lint", "regression", "suite",
            )
            if token in normalized
        ]
        if any(
            token in command_tokens
            for token in ("pytest", "npm test", "test", "regression", "suite")
        ):
            command_tokens.append("test")
        if any(
            token in command_tokens
            for token in ("npm run build", "build", "compile")
        ):
            command_tokens.append("build")
        if "install" in command_tokens:
            command_tokens.extend(("npm install", "npm ci"))
        command_tokens = list(dict.fromkeys(command_tokens))
        spec["command_tokens"] = command_tokens
        package_roots = {
            Path(path).parent.as_posix()
            for path in (
                _normalize_path(raw_path) for raw_path in artifact_paths
            )
            if Path(path).name.casefold() == "package.json"
        }
        if len(package_roots) == 1 and any(
            token in normalized
            for token in ("npm ", "npm.", "install")
        ):
            spec["cwd"] = next(iter(package_roots))
        spec["exit_code"] = 0
        spec["requires_log_digest"] = True
    else:
        spec["observation_required"] = True
    return spec


def acceptance_criterion_contracts(
    scope_id: str,
    criteria: Sequence[Any],
    *,
    artifact_paths: Sequence[str] = (),
) -> list[Dict[str, Any]]:
    """Return stable criterion identities and their exact evidence contracts."""
    rows: list[Dict[str, Any]] = []
    for index, raw in enumerate(criteria, 1):
        if isinstance(raw, Mapping):
            criterion = str(
                raw.get("criterion") or raw.get("text") or ""
            ).strip()
            explicit_id = str(raw.get("criterion_id") or "").strip()
            explicit_spec = raw.get("evidence_spec")
        else:
            criterion = str(raw or "").strip()
            explicit_id = ""
            explicit_spec = None
        if not criterion:
            continue
        criterion_id = explicit_id or f"{scope_id}:acceptance:{index}"
        evidence_spec = (
            dict(explicit_spec)
            if isinstance(explicit_spec, Mapping)
            else _criterion_evidence_spec(criterion, artifact_paths)
        )
        rows.append({
            "criterion_id": criterion_id,
            "criterion": criterion,
            "source_class": str(
                evidence_spec.get("source_class") or "semantic"
            ),
            "evidence_spec": evidence_spec,
        })
    return rows


def evidence_record_proves_criterion(
    raw: Mapping[str, Any],
    criterion_contract: Mapping[str, Any],
    *,
    task_id: str,
    task_run_id: str,
    execution_generation: str,
) -> bool:
    """Verify one raw server observation against one declared criterion."""
    criterion_id = str(criterion_contract.get("criterion_id") or "")
    covered = set(_strings(raw.get("covered_criterion_ids")))
    if criterion_id not in covered:
        return False
    if str(raw.get("task_id") or "") != task_id:
        return False
    if task_run_id and str(raw.get("task_run_id") or "") not in {"", task_run_id}:
        return False
    if str(raw.get("execution_generation") or "") != execution_generation:
        return False
    if raw.get("passed") is not True:
        return False
    spec = criterion_contract.get("evidence_spec") or {}
    source_class = str(spec.get("source_class") or "")
    raw_kind = str(raw.get("kind") or "").lower()
    if source_class == "command_test":
        if raw_kind == "api" or raw.get("exit_code") != 0:
            return False
        if not _SHA256.fullmatch(str(raw.get("log_digest") or "")):
            return False
        command = str(raw.get("command") or "").casefold()
        tokens = _strings(spec.get("command_tokens"))
        return bool(command and tokens and any(
            token.casefold() in command for token in tokens
        ))
    if source_class == "api_runtime":
        if raw_kind != "api":
            return False
        expected_endpoint = str(spec.get("endpoint") or "")
        if not expected_endpoint or str(raw.get("endpoint") or "") != expected_endpoint:
            return False
        expected_status = spec.get("status_code")
        if expected_status is not None and raw.get("status_code") != expected_status:
            return False
        assertions = raw.get("assertions")
        return bool(
            isinstance(assertions, list)
            and assertions
            and all(
                isinstance(item, Mapping)
                and item.get("passed") is True
                and str(item.get("name") or "").strip()
                for item in assertions
            )
        )
    return False


def _artifact_evidence_for_assignment(
    *,
    phase_id: str,
    agent_id: str,
    run_id: str,
    workspace: Path,
    file_registry: Mapping[str, Mapping[str, Any]],
    required_files: Sequence[str],
    delivery_evidence: Mapping[str, Any],
) -> tuple[EvidenceRecord, str, tuple[str, ...]]:
    root = Path(workspace).resolve()
    delivery_by_path = {
        _normalize_path(item.get("path")): item
        for item in delivery_evidence.get("files") or []
        if isinstance(item, Mapping) and _normalize_path(item.get("path"))
    }
    checks: list[Dict[str, Any]] = []
    digest_rows: list[Dict[str, Any]] = []
    normalized_paths = tuple(dict.fromkeys(
        path for item in required_files
        if (path := _normalize_path(item))
    ))
    for path in normalized_paths:
        registry = file_registry.get(path) or {}
        delivery = delivery_by_path.get(path) or {}
        target = (root / path).resolve()
        safe = True
        try:
            target.relative_to(root)
        except ValueError:
            safe = False
        payload = target.read_bytes() if safe and target.is_file() else None
        byte_digest = _sha256(payload) if payload is not None else ""
        delivery_digest = str(delivery.get("sha256") or "")
        if delivery_digest and not delivery_digest.startswith("sha256:"):
            delivery_digest = "sha256:" + delivery_digest
        passed = bool(
            payload is not None
            and str(registry.get("phase_id") or "") == str(phase_id)
            and str(registry.get("agent_id") or "") == agent_id
            and delivery_digest.lower() == byte_digest.lower()
            and isinstance(delivery.get("size"), int)
            and delivery.get("size") == len(payload)
        )
        checks.append({
            "name": f"registry owner and byte digest for {path}",
            "passed": passed,
            "path": path,
            "registry_agent_id": str(registry.get("agent_id") or ""),
            "byte_digest": byte_digest,
            "delivery_digest": delivery_digest,
            "size": len(payload) if payload is not None else None,
        })
        digest_rows.append({
            "path": path,
            "byte_digest": byte_digest,
            "size": len(payload) if payload is not None else None,
        })
    artifact_digest = (
        _sha256(json.dumps(
            digest_rows,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))
        if checks and all(check["passed"] for check in checks)
        else ""
    )
    evidence = create_evidence(
        EvidenceKind.ARTIFACT_VALIDATION,
        run_id or "missing-run",
        "metis.runner",
        {
            "validator": "phase.registry_byte_digest",
            "checks": checks or [{
                "name": "runner digested task artifact",
                "passed": False,
            }],
            "artifact_digest": artifact_digest,
            "phase_id": str(phase_id),
            "agent_id": agent_id,
        },
    )
    return evidence, artifact_digest, normalized_paths


def _bound_evidence_records(
    raw_bindings: Sequence[Mapping[str, Any]],
    *,
    task_id: str,
    criterion_id: str,
    task_run_id: str = "",
    execution_generation: str = "",
    authoritative_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    for binding in raw_bindings:
        if str(binding.get("task_id") or "") not in {"", task_id}:
            continue
        if str(binding.get("criterion_id") or "") != criterion_id:
            continue
        raw = binding.get("record") if isinstance(binding.get("record"), Mapping) else binding
        try:
            record = EvidenceRecord.from_dict(raw)
        except (TypeError, ValueError):
            continue
        covered = set(_strings(record.payload.get("covered_criterion_ids")))
        if criterion_id not in covered:
            continue
        if str(record.payload.get("task_id") or "") != task_id:
            continue
        if (
            task_run_id
            and str(record.payload.get("task_run_id") or "") != task_run_id
        ):
            continue
        if (
            execution_generation
            and str(record.payload.get("execution_generation") or "")
            != execution_generation
        ):
            continue
        if dict(raw) != dict(
            (authoritative_records or {}).get(record.evidence_id) or {}
        ):
            continue
        records.append(record)
    return records


def build_phase_evidence_bundle(
    phase: Mapping[str, Any],
    assignments: Sequence[Mapping[str, Any]],
    *,
    workspace: Path,
    file_registry: Mapping[str, Mapping[str, Any]],
    run_records: Mapping[str, Mapping[str, Any]],
    criterion_evidence: Sequence[Mapping[str, Any]] = (),
    phase_evidence: Sequence[Mapping[str, Any]] = (),
    authoritative_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build the common completion schema from registry, bytes and typed evidence."""
    tasks, _merge_issues = _merged_phase_tasks(phase)
    owner_by_task: dict[str, Mapping[str, Any]] = {}
    for assignment in assignments:
        for task_id in _strings(
            assignment.get("task_ids")
            or assignment.get("locked_task_ids")
            or assignment.get("assigned_task_ids")
        ):
            owner_by_task.setdefault(task_id, assignment)
    phase_artifact_paths = tuple(dict.fromkeys(
        path
        for assignment in assignments
        for item in (assignment.get("required_files") or [])
        if (path := _normalize_path(item))
    ))
    phase_artifact_checks: dict[str, Dict[str, Any]] = {}
    task_rows: list[Dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        assignment = owner_by_task.get(task_id) or {}
        agent_id = str(
            assignment.get("agent_id") or assignment.get("id") or ""
        )
        run = run_records.get(task_id) or {}
        payload = run.get("payload") or {}
        if (
            str(payload.get("task_id") or "") != task_id
            or str(payload.get("agent_id") or "") != agent_id
            or str(payload.get("phase_id") or "") != str(phase.get("phase_id") or "")
            or str(payload.get("execution_generation") or "") != str(
                phase.get("execution_generation") or ""
            )
            or str(payload.get("contract_digest") or "") != str(
                phase.get("execution_contract_digest") or ""
            )
            or int(payload.get("requirements_revision") or 0) != int(
                phase.get("execution_requirements_revision") or 0
            )
            or str(payload.get("artifact_baseline_digest") or "") != str(
                phase.get("execution_artifact_baseline_digest") or ""
            )
        ):
            run = {}
        run_id = str(run.get("run_id") or "")
        artifact, artifact_digest, artifact_paths = _artifact_evidence_for_assignment(
            phase_id=str(phase.get("phase_id") or ""),
            agent_id=agent_id,
            run_id=run_id,
            workspace=Path(workspace),
            file_registry=file_registry,
            required_files=list(assignment.get("required_files") or []),
            delivery_evidence=assignment.get("delivery_evidence") or {},
        )
        for check in artifact.payload.get("checks") or []:
            path = _normalize_path(check.get("path"))
            if path:
                phase_artifact_checks[path] = dict(check)
        evidence_by_id: dict[str, EvidenceRecord] = {}
        if assignment.get("required_files"):
            evidence_by_id[artifact.evidence_id] = artifact
        authoritative_records = {
            str(item.get("evidence_id") or ""): item
            for item in ((run.get("result") or {}).get("evidence") or [])
            if isinstance(item, Mapping) and item.get("evidence_id")
        }
        authoritative_records.update(
            dict(authoritative_evidence or {})
        )
        acceptance: list[Dict[str, Any]] = []
        criterion_contracts = acceptance_criterion_contracts(
            task_id,
            task.get("acceptance_criteria") or [],
            artifact_paths=artifact_paths,
        )
        for criterion_contract in criterion_contracts:
            acceptance_id = criterion_contract["criterion_id"]
            criterion = criterion_contract["criterion"]
            source_class = criterion_contract["source_class"]
            evidence_spec = criterion_contract["evidence_spec"]
            mentioned_paths = tuple(evidence_spec.get("paths") or ())
            bound: list[EvidenceRecord]
            if source_class == "required_file":
                artifact_checks = artifact.payload.get("checks") or []
                mentioned_checks = [
                    check for check in artifact_checks
                    if str(check.get("path") or "") in mentioned_paths
                ]
                artifact_passed = (
                    mentioned_checks
                    and all(check.get("passed") is True for check in mentioned_checks)
                )
                bound = [create_evidence(
                    EvidenceKind.ARTIFACT_VALIDATION,
                    run_id or "missing-run",
                    "metis.runner",
                    {
                        **artifact.payload,
                        "covered_criterion_ids": [acceptance_id],
                        "task_id": task_id,
                        "task_run_id": run_id,
                        "execution_generation": str(
                            phase.get("execution_generation") or ""
                        ),
                    },
                )] if artifact_passed else []
            else:
                bound = _bound_evidence_records(
                    criterion_evidence,
                    task_id=task_id,
                    criterion_id=acceptance_id,
                    task_run_id=run_id,
                    execution_generation=str(
                        phase.get("execution_generation") or ""
                    ),
                    authoritative_records=authoritative_records,
                )
            for record in bound:
                evidence_by_id[record.evidence_id] = record
            acceptance.append({
                "criterion_id": acceptance_id,
                "criterion": criterion,
                "source_class": source_class,
                "evidence_spec": evidence_spec,
                "passed": bool(bound),
                "evidence_ids": [record.evidence_id for record in bound],
            })
        started_at = float(run.get("started_at") or 0)
        finished_at = float(run.get("finished_at") or 0)
        task_rows.append({
            "task_id": task_id,
            "agent_id": agent_id,
            "status": "completed" if str(run.get("status") or "") == "succeeded" else "failed",
            "start_run_id": run_id if started_at > 0 else "",
            "completion_run_id": run_id if finished_at >= started_at > 0 else "",
            "artifact_digest": artifact_digest,
            "evidence": [record.to_dict() for record in evidence_by_id.values()],
            "acceptance": acceptance,
        })
    normalized_phase_evidence: list[Dict[str, Any]] = []
    for raw in phase_evidence:
        record_raw = raw.get("record") if isinstance(raw.get("record"), Mapping) else raw
        try:
            normalized_phase_evidence.append(
                EvidenceRecord.from_dict(record_raw).to_dict()
            )
        except (TypeError, ValueError):
            normalized_phase_evidence.append(dict(record_raw))
    phase_acceptance: list[Dict[str, Any]] = []
    for criterion_contract in acceptance_criterion_contracts(
        str(phase.get("phase_id") or "phase"),
        phase.get("acceptance_criteria") or [],
        artifact_paths=phase_artifact_paths,
    ):
        acceptance_id = criterion_contract["criterion_id"]
        criterion = criterion_contract["criterion"]
        source_class = criterion_contract["source_class"]
        if source_class == "required_file":
            mentioned_paths = tuple(
                criterion_contract["evidence_spec"].get("paths") or ()
            )
            mentioned_checks = [
                phase_artifact_checks[path]
                for path in mentioned_paths
                if path in phase_artifact_checks
            ]
            artifact_passed = bool(
                mentioned_paths
                and len(mentioned_checks) == len(mentioned_paths)
                and all(check.get("passed") is True for check in mentioned_checks)
            )
            digest_rows = [{
                "path": str(check.get("path") or ""),
                "byte_digest": str(check.get("byte_digest") or ""),
                "size": check.get("size"),
            } for check in mentioned_checks]
            record = create_evidence(
                EvidenceKind.ARTIFACT_VALIDATION,
                "phase-artifact-" + hashlib.sha256(
                    (
                        f"{phase.get('phase_id')}:"
                        f"{phase.get('execution_generation')}:"
                        f"{acceptance_id}"
                    ).encode("utf-8")
                ).hexdigest()[:24],
                "metis.runner",
                {
                    "validator": "phase.registry_byte_digest",
                    "checks": mentioned_checks or [{
                        "name": "phase registry byte digest unavailable",
                        "passed": False,
                    }],
                    "artifact_digest": _sha256(json.dumps(
                        digest_rows,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")) if artifact_passed else "",
                    "phase_id": str(phase.get("phase_id") or ""),
                    "task_id": str(phase.get("phase_id") or ""),
                    "task_run_id": "",
                    "execution_generation": str(
                        phase.get("execution_generation") or ""
                    ),
                    "covered_criterion_ids": [acceptance_id],
                },
            )
            bound = [record] if artifact_passed else []
            if bound:
                normalized_phase_evidence.append(record.to_dict())
        else:
            bound = _bound_evidence_records(
                phase_evidence,
                task_id=str(phase.get("phase_id") or ""),
                criterion_id=acceptance_id,
                execution_generation=str(
                    phase.get("execution_generation") or ""
                ),
                authoritative_records=authoritative_evidence,
            )
        phase_acceptance.append({
            "criterion_id": acceptance_id,
            "criterion": criterion,
            "source_class": source_class,
            "evidence_spec": criterion_contract["evidence_spec"],
            "passed": bool(bound),
            "evidence_ids": [record.evidence_id for record in bound],
        })
    return {
        "schema_version": PHASE_EXECUTION_SCHEMA_VERSION,
        "tasks": task_rows,
        "phase_evidence": normalized_phase_evidence,
        "phase_acceptance": phase_acceptance,
    }


def _acceptance_rows(
    task_id: str,
    criteria: Sequence[Any],
    *,
    artifact_paths: Sequence[str] = (),
) -> list[Dict[str, Any]]:
    return acceptance_criterion_contracts(
        task_id, criteria, artifact_paths=artifact_paths,
    )


def _validate_acceptance_evidence(
    *,
    path: str,
    task_id: str,
    criteria: Sequence[Any],
    rows: Any,
    valid_evidence: Mapping[str, EvidenceRecord],
    scope: str,
    artifact_paths: Sequence[str] = (),
    task_run_id: str = "",
    execution_generation: str = "",
) -> list[PhaseExecutionIssue]:
    issues: list[PhaseExecutionIssue] = []
    expected = _acceptance_rows(
        task_id, criteria, artifact_paths=artifact_paths,
    )
    actual_rows = [row for row in (rows or []) if isinstance(row, Mapping)]
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for row in actual_rows:
        criterion_id = str(row.get("criterion_id") or "").strip()
        by_id.setdefault(criterion_id, []).append(row)
    for contract in expected:
        criterion_id = contract["criterion_id"]
        criterion = contract["criterion"]
        evidence_spec = contract["evidence_spec"]
        matches = by_id.get(criterion_id, [])
        if len(matches) != 1:
            issues.append(_issue(
                f"{scope}_acceptance_evidence_missing"
                if not matches else f"{scope}_acceptance_evidence_duplicate",
                path,
                f"{criterion_id} must have exactly one evidence row",
                1,
                len(matches),
            ))
            continue
        row = matches[0]
        if str(row.get("criterion") or "") != criterion:
            issues.append(_issue(
                f"{scope}_acceptance_criterion_drift",
                path,
                f"{criterion_id} text differs from the locked criterion",
                criterion,
                row.get("criterion"),
            ))
        if row.get("passed") is not True:
            issues.append(_issue(
                f"{scope}_acceptance_not_passed",
                path,
                f"{criterion_id} is not marked passed",
            ))
        source_class = contract["source_class"]
        mentioned_paths = tuple(evidence_spec.get("paths") or ())
        if str(row.get("source_class") or "") != source_class:
            issues.append(_issue(
                f"{scope}_acceptance_source_class_drift",
                path,
                f"{criterion_id} source_class does not match the locked criterion",
                source_class,
                row.get("source_class"),
            ))
        if dict(row.get("evidence_spec") or {}) != dict(evidence_spec):
            issues.append(_issue(
                f"{scope}_acceptance_evidence_spec_drift",
                path,
                f"{criterion_id} evidence_spec differs from the locked criterion",
                evidence_spec,
                row.get("evidence_spec"),
            ))
        evidence_ids = _strings(row.get("evidence_ids"))
        if not evidence_ids or any(
            evidence_id not in valid_evidence
            for evidence_id in evidence_ids
        ):
            issues.append(_issue(
                f"{scope}_acceptance_evidence_invalid",
                path,
                f"{criterion_id} must reference valid passed machine evidence",
            ))
            continue
        matching = [
            valid_evidence[evidence_id] for evidence_id in evidence_ids
            if evidence_id in valid_evidence
        ]
        if source_class != "required_file":
            matching = [
                evidence for evidence in matching
                if (
                    criterion_id
                    in set(_strings(
                        evidence.payload.get("covered_criterion_ids")
                    ))
                    and str(evidence.payload.get("task_id") or "") == task_id
                    and (
                        not task_run_id
                        or not str(evidence.payload.get("task_run_id") or "")
                        or str(evidence.payload.get("task_run_id") or "")
                        == task_run_id
                    )
                    and (
                        not execution_generation
                        or str(
                            evidence.payload.get("execution_generation") or ""
                        ) == execution_generation
                    )
                )
            ]
            if not matching:
                issues.append(_issue(
                    f"{scope}_acceptance_evidence_scope_mismatch",
                    path,
                    f"{criterion_id} evidence does not explicitly cover this criterion",
                ))
                continue
        source_valid = False
        if source_class == "required_file":
            for evidence in matching:
                checks = evidence.payload.get("checks") or []
                checked_paths = {
                    str(check.get("path") or "")
                    for check in checks
                    if isinstance(check, Mapping) and check.get("passed") is True
                }
                if (
                    evidence.kind == EvidenceKind.ARTIFACT_VALIDATION.value
                    and evidence.producer == "metis.runner"
                    and criterion_id in set(_strings(
                        evidence.payload.get("covered_criterion_ids")
                    ))
                    and str(evidence.payload.get("task_id") or "") == task_id
                    and (
                        not task_run_id
                        or not str(evidence.payload.get("task_run_id") or "")
                        or str(evidence.payload.get("task_run_id") or "")
                        == task_run_id
                    )
                    and (
                        not execution_generation
                        or str(
                            evidence.payload.get("execution_generation") or ""
                        ) == execution_generation
                    )
                    and evidence.payload.get("validator") == "phase.registry_byte_digest"
                    and set(mentioned_paths).issubset(checked_paths)
                ):
                    source_valid = True
                    break
        elif source_class == "command_test":
            tokens = _strings(evidence_spec.get("command_tokens"))
            source_valid = any(
                evidence.kind in {
                    EvidenceKind.COMMAND.value,
                    EvidenceKind.TEST.value,
                    EvidenceKind.BUILD.value,
                }
                and evidence.producer in {"metis.runner", "metis.ci"}
                and _SHA256.fullmatch(
                    str(evidence.payload.get("log_digest") or "")
                )
                and tokens
                and any(
                    token.casefold()
                    in str(evidence.payload.get("command") or "").casefold()
                    for token in tokens
                )
                for evidence in matching
            )
        elif source_class == "api_runtime":
            expected_endpoint = str(evidence_spec.get("endpoint") or "")
            expected_status = evidence_spec.get("status_code")
            source_valid = any(
                evidence.kind in {
                    EvidenceKind.API.value,
                    EvidenceKind.SERVICE_HEALTH.value,
                    EvidenceKind.DOCKER.value,
                }
                and evidence.producer in {
                    "metis.runner", "metis.ci", "metis.runtime_acceptance",
                }
                and expected_endpoint
                and str(evidence.payload.get("endpoint") or "")
                == expected_endpoint
                and (
                    expected_status is None
                    or evidence.payload.get("status_code") == expected_status
                )
                for evidence in matching
            )
        else:
            source_valid = any(
                (
                    evidence.kind == EvidenceKind.SUPERVISOR_OBSERVATION.value
                    and evidence.producer == "metis.supervisor"
                )
                or (
                    evidence.kind == EvidenceKind.HUMAN_CONFIRMATION.value
                    and evidence.producer == "metis.user_confirmation"
                )
                for evidence in matching
            )
        if not source_valid:
            issues.append(_issue(
                f"{scope}_acceptance_evidence_source_mismatch",
                path,
                f"{criterion_id} evidence source cannot prove {source_class}",
            ))
    expected_ids = {
        str(contract.get("criterion_id") or "") for contract in expected
    }
    for criterion_id in by_id:
        if criterion_id not in expected_ids:
            issues.append(_issue(
                f"{scope}_acceptance_evidence_unknown",
                path,
                f"acceptance evidence references unknown criterion {criterion_id}",
            ))
    return issues


def validate_phase_completion(
    phase: Mapping[str, Any],
    assignments: Sequence[Mapping[str, Any]],
    evidence_bundle: Mapping[str, Any],
) -> PhaseExecutionValidation:
    """Gate phase completion on unique ownership and per-criterion evidence."""
    issues = list(validate_task_owners(phase, assignments).issues)
    if int(evidence_bundle.get("schema_version") or 0) != PHASE_EXECUTION_SCHEMA_VERSION:
        issues.append(_issue(
            "completion_schema_unsupported",
            "$.schema_version",
            "unsupported phase completion evidence schema",
            PHASE_EXECUTION_SCHEMA_VERSION,
            evidence_bundle.get("schema_version"),
        ))

    tasks, merge_issues = _merged_phase_tasks(phase)
    issues.extend(merge_issues)
    owner_by_task: dict[str, str] = {}
    required_files_by_task: dict[str, tuple[str, ...]] = {}
    for assignment in assignments:
        agent_id = str(
            assignment.get("agent_id") or assignment.get("id") or ""
        ).strip()
        for task_id in _strings(
            assignment.get("task_ids")
            or assignment.get("locked_task_ids")
            or assignment.get("assigned_task_ids")
        ):
            owner_by_task[task_id] = agent_id
            required_files_by_task[task_id] = _strings(
                assignment.get("required_files")
            )

    task_results = [
        row for row in (evidence_bundle.get("tasks") or [])
        if isinstance(row, Mapping)
    ]
    task_results_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for result in task_results:
        task_results_by_id.setdefault(
            str(result.get("task_id") or "").strip(), []
        ).append(result)

    for task_index, task in enumerate(tasks):
        task_id = str(task.get("task_id") or "").strip()
        path = f"$.tasks[{task_index}]"
        rows = task_results_by_id.get(task_id, [])
        if len(rows) != 1:
            issues.append(_issue(
                "task_completion_missing"
                if not rows else "task_completion_duplicate",
                path,
                f"locked task {task_id} must have exactly one completion row",
                1,
                len(rows),
            ))
            continue
        row = rows[0]
        expected_owner = owner_by_task.get(task_id, "")
        if str(row.get("agent_id") or "").strip() != expected_owner:
            issues.append(_issue(
                "task_completion_owner_mismatch",
                f"{path}.agent_id",
                f"task {task_id} completion owner differs from its claim",
                expected_owner,
                row.get("agent_id"),
            ))
        if str(row.get("status") or "").lower() not in SUCCESS_STATUSES:
            issues.append(_issue(
                "task_completion_not_successful",
                f"{path}.status",
                f"task {task_id} is not completed",
            ))

        valid_evidence: dict[str, EvidenceRecord] = {}
        for evidence_index, raw_evidence in enumerate(row.get("evidence") or []):
            errors = validate_evidence_record(raw_evidence)
            try:
                evidence = (
                    raw_evidence
                    if isinstance(raw_evidence, EvidenceRecord)
                    else EvidenceRecord.from_dict(raw_evidence)
                )
            except (TypeError, ValueError):
                evidence = None
            if errors or evidence is None or evidence.status != "passed":
                issues.append(_issue(
                    "task_evidence_invalid",
                    f"{path}.evidence[{evidence_index}]",
                    "; ".join(errors) or "evidence did not pass",
                ))
                continue
            valid_evidence[evidence.evidence_id] = evidence

        start_run_id = str(row.get("start_run_id") or "").strip()
        completion_run_id = str(row.get("completion_run_id") or "").strip()
        if not start_run_id or completion_run_id != start_run_id:
            issues.append(_issue(
                "task_run_lineage_invalid",
                f"{path}.completion_run_id",
                f"task {task_id} must bind one durable start/completion run_id",
                start_run_id,
                completion_run_id,
            ))
        for evidence_id, evidence in list(valid_evidence.items()):
            if (
                evidence.producer
                not in {"metis.supervisor", "metis.user_confirmation"}
                and evidence.run_id != completion_run_id
                and str(evidence.payload.get("task_run_id") or "")
                not in {"", completion_run_id}
            ):
                issues.append(_issue(
                    "task_evidence_run_mismatch",
                    f"{path}.evidence",
                    f"task {task_id} evidence belongs to another run",
                    completion_run_id,
                    evidence.run_id,
                ))
                valid_evidence.pop(evidence_id, None)
        artifact_digest = str(row.get("artifact_digest") or "")
        artifact_evidence = [
            evidence for evidence in valid_evidence.values()
            if (
                evidence.kind == EvidenceKind.ARTIFACT_VALIDATION.value
                and evidence.producer == "metis.runner"
                and evidence.payload.get("validator") == "phase.registry_byte_digest"
            )
        ]
        required_files = required_files_by_task.get(task_id, ())
        if required_files:
            if (
                not _SHA256.fullmatch(artifact_digest)
                or not any(
                    evidence.run_id == completion_run_id
                    and evidence.payload.get("artifact_digest") == artifact_digest
                    for evidence in artifact_evidence
                )
            ):
                issues.append(_issue(
                    "task_artifact_digest_invalid",
                    f"{path}.artifact_digest",
                    f"task {task_id} requires registry and byte-digest artifact evidence",
                ))
        elif artifact_digest or artifact_evidence:
            issues.append(_issue(
                "task_unowned_artifact_evidence",
                f"{path}.artifact_digest",
                f"fileless task {task_id} cannot borrow another task's artifact",
            ))

        criteria = list(task.get("acceptance_criteria") or [])
        if criteria:
            issues.extend(_validate_acceptance_evidence(
                path=f"{path}.acceptance",
                task_id=task_id,
                criteria=criteria,
                rows=row.get("acceptance"),
                valid_evidence=valid_evidence,
                scope="task",
                artifact_paths=[
                    str(check.get("path") or "")
                    for evidence in artifact_evidence
                    for check in (evidence.payload.get("checks") or [])
                    if isinstance(check, Mapping)
                ],
                task_run_id=completion_run_id,
                execution_generation=str(
                    phase.get("execution_generation") or ""
                ),
            ))

    expected_task_ids = {
        str(task.get("task_id") or "").strip() for task in tasks
    }
    for task_id in task_results_by_id:
        if task_id not in expected_task_ids:
            issues.append(_issue(
                "task_completion_unknown",
                "$.tasks",
                f"completion evidence references unknown task {task_id}",
            ))

    phase_criteria = list(phase.get("acceptance_criteria") or [])
    phase_evidence = [
        item for item in (evidence_bundle.get("phase_evidence") or [])
        if isinstance(item, (Mapping, EvidenceRecord))
    ]
    valid_phase_evidence: dict[str, EvidenceRecord] = {}
    for evidence_index, raw_evidence in enumerate(phase_evidence):
        errors = validate_evidence_record(raw_evidence)
        try:
            evidence = (
                raw_evidence
                if isinstance(raw_evidence, EvidenceRecord)
                else EvidenceRecord.from_dict(raw_evidence)
            )
        except (TypeError, ValueError):
            evidence = None
        if errors or evidence is None or evidence.status != "passed":
            issues.append(_issue(
                "phase_evidence_invalid",
                f"$.phase_evidence[{evidence_index}]",
                "; ".join(errors) or "evidence did not pass",
            ))
            continue
        valid_phase_evidence[evidence.evidence_id] = evidence
    if phase_criteria:
        issues.extend(_validate_acceptance_evidence(
            path="$.phase_acceptance",
            task_id=str(phase.get("phase_id") or "phase"),
            criteria=phase_criteria,
            rows=evidence_bundle.get("phase_acceptance"),
            valid_evidence=valid_phase_evidence,
            scope="phase",
            artifact_paths=tuple(dict.fromkeys(
                path
                for assignment in assignments
                for raw_path in (assignment.get("required_files") or [])
                if (path := _normalize_path(raw_path))
            )),
            execution_generation=str(
                phase.get("execution_generation") or ""
            ),
        ))
    return PhaseExecutionValidation(not issues, tuple(issues))


def validate_phase_execution_evidence(
    phase: Mapping[str, Any],
    assignments: Sequence[Mapping[str, Any]],
    evidence_bundle: Mapping[str, Any],
) -> PhaseExecutionValidation:
    """Gate review entry on ownership, durable runs and artifact digests.

    Generated criterion rows are optional review aids. Phase completion relies
    on durable execution/artifact evidence plus the independent Pre-QA and QC
    gates, so missing criterion projections do not invalidate execution.
    """
    completion = validate_phase_completion(phase, assignments, evidence_bundle)
    issues = tuple(
        issue for issue in completion.issues
        if "acceptance" not in issue.code
        and not issue.code.startswith("phase_evidence")
        and issue.code != "task_evidence_invalid"
    )
    return PhaseExecutionValidation(not issues, issues)


__all__ = [
    "PHASE_EXECUTION_SCHEMA_VERSION",
    "PhaseExecutionContractError",
    "PhaseExecutionIssue",
    "PhaseExecutionValidation",
    "build_phase_dispatch_plan",
    "build_phase_evidence_bundle",
    "execute_phase_dispatch_plan",
    "validate_phase_completion",
    "validate_phase_execution_evidence",
    "validate_task_graph",
    "validate_task_owners",
]
