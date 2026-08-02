import asyncio
import hashlib

import pytest

from core.evidence import EvidenceKind, create_evidence
from core.phase_execution_contract import (
    PHASE_EXECUTION_SCHEMA_VERSION,
    PhaseExecutionContractError,
    _artifact_evidence_for_assignment,
    _merged_phase_tasks,
    acceptance_criterion_contracts,
    build_phase_dispatch_plan,
    build_phase_evidence_bundle,
    execute_phase_dispatch_plan,
    validate_phase_completion,
    validate_phase_execution_evidence,
    validate_task_graph,
    validate_task_owners,
)


def test_historical_task_artifact_survives_same_phase_successor(tmp_path):
    path = "frontend/src/App.vue"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"successor bytes")
    original = b"original bytes"
    evidence, digest, _ = _artifact_evidence_for_assignment(
        phase_id="phase-1",
        agent_id="agent-original",
        run_id="run-original",
        workspace=tmp_path,
        file_registry={path: {
            "phase_id": "phase-1",
            "agent_id": "agent-successor",
        }},
        required_files=[path],
        delivery_evidence={"files": [{
            "path": path,
            "sha256": hashlib.sha256(original).hexdigest(),
            "size": len(original),
        }]},
    )

    assert digest.startswith("sha256:")
    assert evidence.payload["checks"][0]["passed"] is True
    assert evidence.payload["checks"][0]["historical_delivery"] is True


def test_historical_task_artifact_rejects_cross_phase_successor(tmp_path):
    path = "frontend/src/App.vue"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"later phase bytes")
    original = b"original bytes"
    evidence, digest, _ = _artifact_evidence_for_assignment(
        phase_id="phase-1",
        agent_id="agent-original",
        run_id="run-original",
        workspace=tmp_path,
        file_registry={path: {
            "phase_id": "phase-2",
            "agent_id": "agent-successor",
        }},
        required_files=[path],
        delivery_evidence={"files": [{
            "path": path,
            "sha256": hashlib.sha256(original).hexdigest(),
            "size": len(original),
        }]},
    )

    assert digest == ""
    assert evidence.payload["checks"][0]["passed"] is False


def test_phase_pm_acceptance_criteria_are_the_execution_contract():
    phase = _phase()
    phase["task_contract"][0]["acceptance_criteria"] = [
        "Total PM summary criterion",
    ]
    phase["expert_requirements"][0]["acceptance_criteria"] = [
        "GET /health returns 200",
        "POST /api/items returns 201",
    ]

    tasks, issues = _merged_phase_tasks(phase)

    assert issues == []
    assert tasks[0]["acceptance_criteria"] == [
        "GET /health returns 200",
        "POST /api/items returns 201",
    ]


def test_shared_required_file_derives_dependency_on_previous_writer():
    first = {
        "task_id": "task-1",
        "name": "Create route",
        "roles": ["backend"],
        "dependencies": [],
        "required_files": ["src/index.js"],
    }
    second = {
        "task_id": "task-2",
        "name": "Extend route",
        "roles": ["backend"],
        "dependencies": [],
        "required_files": ["src/index.js"],
    }
    phase = _phase(roles=("backend",), tasks=[first, second])

    tasks, issues = _merged_phase_tasks(phase)

    assert issues == []
    assert tasks[0]["dependencies"] == []
    assert tasks[1]["dependencies"] == ["task-1"]


def test_missing_required_files_serializes_tasks_as_dependency_chain():
    tasks = [
        {
            "task_id": "task-1",
            "name": "Scaffold",
            "roles": ["backend"],
            "dependencies": [],
            "required_files": [],
        },
        {
            "task_id": "task-2",
            "name": "Create endpoint",
            "roles": ["backend"],
            "dependencies": ["task-1"],
            "required_files": [],
        },
        {
            "task_id": "task-3",
            "name": "Extend same entrypoint",
            "roles": ["backend"],
            "dependencies": ["task-1"],
            "required_files": [],
        },
    ]
    phase = _phase(roles=("backend",), tasks=tasks)

    merged, issues = _merged_phase_tasks(phase)

    assert issues == []
    assert merged[2]["dependencies"] == ["task-1", "task-2"]


def test_required_file_evidence_spec_canonicalizes_duplicate_artifact_paths():
    contracts = acceptance_criterion_contracts(
        "phase-1-task-1",
        ["package.json is delivered"],
        artifact_paths=[
            "package.json",
            "./package.json",
            "package.json",
            r".\package.json",
        ],
    )

    assert contracts[0]["evidence_spec"] == {
        "source_class": "required_file",
        "paths": ["package.json"],
    }


def test_required_file_evidence_spec_does_not_match_parent_path_as_suffix():
    contracts = acceptance_criterion_contracts(
        "phase-1",
        ["frontend/package.json is delivered"],
        artifact_paths=["package.json", "frontend/package.json"],
    )

    assert contracts[0]["evidence_spec"] == {
        "source_class": "required_file",
        "paths": ["frontend/package.json"],
    }


def test_manifest_content_assertion_requires_semantic_review():
    contracts = acceptance_criterion_contracts(
        "phase-1-task-1",
        ["package.json defines npm run build"],
        artifact_paths=["package.json"],
    )

    assert contracts[0]["evidence_spec"] == {
        "source_class": "semantic",
        "observation_required": True,
    }


def test_api_route_definition_is_semantic_until_runtime_execution_is_requested():
    contracts = acceptance_criterion_contracts(
        "phase-1-task-1",
        [
            "API路由完整：GET /api/todos返回数组，POST /api/todos返回新对象",
            "GET /health API returns 200",
        ],
        artifact_paths=["server.js"],
    )

    assert contracts[0]["source_class"] == "semantic"
    assert contracts[1]["source_class"] == "api_runtime"


def test_npm_install_success_requires_command_evidence():
    contracts = acceptance_criterion_contracts(
        "phase-1-task-1",
        ["npm install 无报错"],
        artifact_paths=["package.json"],
    )

    assert contracts[0]["evidence_spec"] == {
        "source_class": "command_test",
        "command_tokens": ["install", "npm install", "npm ci"],
        "cwd": ".",
        "exit_code": 0,
        "requires_log_digest": True,
    }


def test_built_required_file_evidence_validates_when_runner_records_repeat_checks(
    tmp_path,
):
    task = {
        "task_id": "package-task",
        "name": "Package",
        "roles": ["backend"],
        "dependencies": [],
        "acceptance_criteria": ["package.json is delivered"],
    }
    phase = _phase(roles=("backend",), tasks=[task])
    path = "package.json"
    payload = b'{"scripts":{"start":"python -m backend.main"}}'
    (tmp_path / path).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assignment = {
        "agent_id": "agent-backend",
        "required_role": "backend",
        "task_ids": ["package-task"],
        "required_files": [path],
        "delivery_evidence": {
            "files": [{
                "path": path,
                "sha256": digest,
                "size": len(payload),
            }],
        },
    }
    phase_observation = create_evidence(
        EvidenceKind.SUPERVISOR_OBSERVATION,
        "supervisor-run",
        "metis.supervisor",
        {
            "actor_id": "supervisor-1",
            "observation_id": "observation-1",
            "scope_digest": "sha256:" + "2" * 64,
            "passed": True,
            "task_id": "phase-1",
            "task_run_id": "supervisor-run",
            "execution_generation": "generation-1",
            "covered_criterion_ids": ["phase-1:acceptance:1"],
        },
        observed_at=2,
    )
    bundle = build_phase_evidence_bundle(
        phase,
        [assignment],
        workspace=tmp_path,
        file_registry={
            path: {"phase_id": "phase-1", "agent_id": "agent-backend"},
        },
        run_records={
            "package-task": {
                "run_id": "run-package",
                "status": "succeeded",
                "started_at": 1,
                "finished_at": 2,
                "payload": {
                    "agent_id": "agent-backend",
                    "task_id": "package-task",
                    "phase_id": "phase-1",
                    "execution_generation": "generation-1",
                },
            },
        },
        phase_evidence=[{
            "task_id": "phase-1",
            "criterion_id": "phase-1:acceptance:1",
            "record": phase_observation.to_dict(),
        }],
        authoritative_evidence={
            phase_observation.evidence_id: phase_observation.to_dict(),
        },
    )

    task_row = bundle["tasks"][0]
    assert task_row["acceptance"][0]["evidence_spec"] == {
        "source_class": "required_file",
        "paths": [path],
    }
    # Both the base artifact record and its criterion-bound record carry the
    # same check. Validation must treat those repeated paths as one locked spec.
    assert sum(
        check.get("path") == path
        for evidence in task_row["evidence"]
        for check in (evidence.get("payload", {}).get("checks") or [])
    ) == 2
    result = validate_phase_completion(phase, [assignment], bundle)
    assert result.valid is True, [issue.to_dict() for issue in result.issues]


def test_phase_required_file_criterion_uses_aggregated_task_artifact_evidence(
    tmp_path,
):
    task = {
        "task_id": "package-task",
        "name": "Package",
        "roles": ["backend"],
        "dependencies": [],
        "acceptance_criteria": [
            "package.json has a registry-backed byte digest",
        ],
    }
    phase = _phase(roles=("backend",), tasks=[task])
    phase["acceptance_criteria"] = [
        "package.json has a registry-backed byte digest",
    ]
    path = "package.json"
    payload = b'{"scripts":{"start":"node app.js"}}'
    (tmp_path / path).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assignment = {
        "agent_id": "agent-backend",
        "required_role": "backend",
        "task_ids": ["package-task"],
        "required_files": [path],
        "delivery_evidence": {
            "files": [{
                "path": path,
                "sha256": digest,
                "size": len(payload),
            }],
        },
    }
    bundle = build_phase_evidence_bundle(
        phase,
        [assignment],
        workspace=tmp_path,
        file_registry={
            path: {"phase_id": "phase-1", "agent_id": "agent-backend"},
        },
        run_records={
            "package-task": {
                "run_id": "run-package",
                "status": "succeeded",
                "started_at": 1,
                "finished_at": 2,
                "payload": {
                    "agent_id": "agent-backend",
                    "task_id": "package-task",
                    "phase_id": "phase-1",
                    "execution_generation": "generation-1",
                },
            },
        },
    )

    assert bundle["phase_acceptance"][0]["passed"] is True
    result = validate_phase_completion(phase, [assignment], bundle)
    assert result.valid is True, [issue.to_dict() for issue in result.issues]


def test_phase_required_file_criterion_aggregates_owned_task_artifacts(
    tmp_path,
):
    tasks = [
        {
            "task_id": "root-manifest",
            "name": "Root manifest",
            "roles": ["backend"],
            "dependencies": [],
            "acceptance_criteria": [
                "package.json has a registry-backed byte digest"
            ],
        },
        {
            "task_id": "frontend-manifest",
            "name": "Frontend manifest",
            "roles": ["frontend"],
            "dependencies": [],
            "acceptance_criteria": [
                "frontend/package.json has a registry-backed byte digest"
            ],
        },
    ]
    phase = _phase(
        roles=("backend", "frontend"),
        tasks=tasks,
    )
    phase["acceptance_criteria"] = [
        (
            "package.json and frontend/package.json "
            "have registry-backed byte digests"
        )
    ]
    payloads = {
        "package.json": b'{"name":"root"}',
        "frontend/package.json": b'{"name":"frontend"}',
    }
    assignments = []
    run_records = {}
    file_registry = {}
    for task, (path, payload) in zip(tasks, payloads.items()):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        agent_id = f"agent-{task['task_id']}"
        run_id = f"run-{task['task_id']}"
        assignments.append({
            "agent_id": agent_id,
            "required_role": task["roles"][0],
            "task_ids": [task["task_id"]],
            "required_files": [path],
            "delivery_evidence": {
                "files": [{
                    "path": path,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }],
            },
        })
        file_registry[path] = {
            "phase_id": "phase-1",
            "agent_id": agent_id,
        }
        run_records[task["task_id"]] = {
            "run_id": run_id,
            "status": "succeeded",
            "started_at": 1,
            "finished_at": 2,
            "payload": {
                "agent_id": agent_id,
                "task_id": task["task_id"],
                "phase_id": "phase-1",
                "execution_generation": "generation-1",
            },
        }

    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry=file_registry,
        run_records=run_records,
    )

    phase_acceptance = bundle["phase_acceptance"][0]
    assert phase_acceptance["passed"] is True
    assert phase_acceptance["source_class"] == "required_file"
    assert phase_acceptance["evidence_spec"]["paths"] == [
        "package.json",
        "frontend/package.json",
    ]
    phase_records = [
        record for record in bundle["phase_evidence"]
        if record["evidence_id"] in phase_acceptance["evidence_ids"]
    ]
    assert len(phase_records) == 1
    assert phase_records[0]["payload"]["task_id"] == "phase-1"
    assert {
        check["path"] for check in phase_records[0]["payload"]["checks"]
    } == set(payloads)

    result = validate_phase_completion(phase, assignments, bundle)
    assert result.valid is True, [issue.to_dict() for issue in result.issues]


def _phase(
    phase_id="phase-1",
    *,
    roles=("backend", "qa"),
    dependencies=(),
    tasks=None,
):
    return {
        "phase_id": phase_id,
        "execution_generation": "generation-1",
        "roles_needed": list(roles),
        "dependencies": list(dependencies),
        "acceptance_criteria": ["all locked tasks pass"],
        "task_contract": tasks or [
            {
                "task_id": "api",
                "name": "API",
                "roles": ["backend"],
                "dependencies": [],
                "acceptance_criteria": ["GET /api/items API returns 200"],
            },
            {
                "task_id": "qa",
                "name": "QA",
                "roles": ["qa"],
                "dependencies": ["api"],
                "acceptance_criteria": ["regression suite passes"],
            },
        ],
        "expert_requirements": [
            {
                "task_id": task["task_id"],
                "task_name": task["name"],
                "task_description": task["name"],
                "required_role": task["roles"][0],
                "dependencies": list(task.get("dependencies") or []),
                "acceptance_criteria": list(task.get("acceptance_criteria") or []),
            }
            for task in (tasks or [
                {
                    "task_id": "api", "name": "API", "roles": ["backend"],
                    "dependencies": [], "acceptance_criteria": ["GET /api/items API returns 200"],
                },
                {
                    "task_id": "qa", "name": "QA", "roles": ["qa"],
                    "dependencies": ["api"],
                    "acceptance_criteria": ["regression suite passes"],
                },
            ])
        ],
    }


def _assignments():
    return [
        {"agent_id": "agent-backend", "required_role": "backend", "task_ids": ["api"]},
        {"agent_id": "agent-qa", "required_role": "qa", "task_ids": ["qa"]},
    ]


def _test_evidence(
    run_id,
    command,
    *,
    task_id="",
    criterion_id="",
    generation="generation-1",
):
    scope = {}
    if task_id and criterion_id:
        scope = {
            "task_id": task_id,
            "task_run_id": run_id,
            "execution_generation": generation,
            "covered_criterion_ids": [criterion_id],
        }
    return create_evidence(
        EvidenceKind.TEST,
        run_id,
        "metis.runner",
        {
            "command": command,
            "exit_code": 0,
            "tests_total": 1,
            "tests_failed": 0,
            "output_summary": "1 passed",
            "log_digest": "sha256:" + "1" * 64,
            **scope,
        },
        observed_at=1,
    )


def test_completion_does_not_require_generated_acceptance_criteria(tmp_path):
    task = {
        "task_id": "implementation",
        "name": "Implement the phase objective",
        "roles": ["backend"],
        "dependencies": [],
        "acceptance_criteria": [],
    }
    phase = _phase(roles=("backend",), tasks=[task])
    phase["acceptance_criteria"] = []
    assignments = [{
        "agent_id": "agent-backend",
        "required_role": "backend",
        "task_ids": ["implementation"],
        "required_files": [],
        "delivery_evidence": {},
    }]
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry={},
        run_records={"implementation": {
            "run_id": "run-implementation",
            "status": "succeeded",
            "started_at": 1,
            "finished_at": 2,
            "payload": {
                "agent_id": "agent-backend",
                "task_id": "implementation",
                "phase_id": "phase-1",
                "execution_generation": "generation-1",
            },
            "result": {},
        }},
    )

    result = validate_phase_completion(phase, assignments, bundle)

    assert result.valid is True, [issue.to_dict() for issue in result.issues]


def test_execution_gate_ignores_unproven_generated_acceptance_hints(tmp_path):
    task = {
        "task_id": "implementation",
        "name": "Implement the phase objective",
        "roles": ["backend"],
        "dependencies": [],
        "acceptance_criteria": ["Generated hint that has no dedicated evidence"],
    }
    phase = _phase(roles=("backend",), tasks=[task])
    phase["acceptance_criteria"] = ["Generated phase-level hint"]
    assignments = [{
        "agent_id": "agent-backend",
        "required_role": "backend",
        "task_ids": ["implementation"],
        "required_files": [],
        "delivery_evidence": {},
    }]
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry={},
        run_records={"implementation": {
            "run_id": "run-implementation",
            "status": "succeeded",
            "started_at": 1,
            "finished_at": 2,
            "payload": {
                "agent_id": "agent-backend",
                "task_id": "implementation",
                "phase_id": "phase-1",
                "execution_generation": "generation-1",
            },
            "result": {},
        }},
    )

    assert validate_phase_completion(phase, assignments, bundle).valid is False
    assert validate_phase_execution_evidence(
        phase, assignments, bundle,
    ).valid is True


def test_execution_gate_ignores_superseded_failed_qa_evidence(tmp_path):
    task = {
        "task_id": "implementation",
        "name": "Implement",
        "roles": ["backend"],
        "dependencies": [],
        "acceptance_criteria": [],
    }
    phase = _phase(roles=("backend",), tasks=[task])
    assignments = [{
        "agent_id": "agent-backend",
        "required_role": "backend",
        "task_ids": ["implementation"],
        "required_files": [],
        "delivery_evidence": {},
    }]
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry={},
        run_records={"implementation": {
            "run_id": "run-implementation",
            "status": "succeeded",
            "started_at": 1,
            "finished_at": 2,
            "payload": {
                "agent_id": "agent-backend",
                "task_id": "implementation",
                "phase_id": "phase-1",
                "execution_generation": "generation-1",
            },
            "result": {},
        }},
    )
    failed_history = _test_evidence(
        "run-implementation", "historical pre-QA"
    ).to_dict()
    failed_history["status"] = "failed"
    bundle["tasks"][0]["evidence"].append(failed_history)

    assert validate_phase_completion(
        phase, assignments, bundle,
    ).valid is False
    assert validate_phase_execution_evidence(
        phase, assignments, bundle,
    ).valid is True


def test_required_role_must_belong_to_phase_roles_needed():
    phase = _phase()
    phase["expert_requirements"][0]["required_role"] = "frontend"

    result = validate_task_graph([phase])

    assert result.valid is False
    assert "task_required_role_outside_phase" in {
        issue.code for issue in result.issues
    }


def test_display_role_matches_its_canonical_phase_role():
    task = {
        "task_id": "api",
        "name": "API",
        "roles": ["Backend Developer"],
        "dependencies": [],
        "acceptance_criteria": ["API responds"],
    }

    result = validate_task_graph([_phase(roles=("backend",), tasks=[task])])

    assert result.valid is True


def test_chinese_display_role_matches_its_canonical_phase_role():
    task = {
        "task_id": "ui",
        "name": "UI",
        "roles": ["前端工程师"],
        "dependencies": [],
        "acceptance_criteria": ["UI builds"],
    }

    result = validate_task_graph([_phase(roles=("frontend",), tasks=[task])])

    assert result.valid is True


def test_different_canonical_role_remains_outside_phase():
    task = {
        "task_id": "ui",
        "name": "UI",
        "roles": ["Frontend Developer"],
        "dependencies": [],
        "acceptance_criteria": ["UI builds"],
    }

    result = validate_task_graph([_phase(roles=("backend",), tasks=[task])])

    assert "task_required_role_outside_phase" in {
        issue.code for issue in result.issues
    }


def test_unknown_role_labels_are_not_collapsed_together():
    task = {
        "task_id": "compliance",
        "name": "Compliance",
        "roles": ["Compliance Analyst"],
        "dependencies": [],
        "acceptance_criteria": ["review exists"],
    }

    result = validate_task_graph([
        _phase(roles=("Risk Analyst",), tasks=[task])
    ])

    assert "task_required_role_outside_phase" in {
        issue.code for issue in result.issues
    }


def test_every_locked_task_requires_exactly_one_owner():
    phase = _phase()

    missing = validate_task_owners(phase, _assignments()[:1])
    duplicate = validate_task_owners(
        phase,
        [*_assignments(), {"agent_id": "agent-2", "task_ids": ["api"]}],
    )

    assert "task_owner_missing" in {issue.code for issue in missing.issues}
    assert "task_owner_duplicate" in {issue.code for issue in duplicate.issues}


def test_task_owner_role_accepts_canonical_alias():
    task = {
        "task_id": "ui",
        "name": "UI",
        "roles": ["前端工程师"],
        "dependencies": [],
        "acceptance_criteria": ["UI builds"],
    }
    phase = _phase(roles=("frontend",), tasks=[task])

    result = validate_task_owners(phase, [{
        "agent_id": "agent-frontend",
        "required_role": "Frontend Developer",
        "task_ids": ["ui"],
    }])

    assert result.valid is True


def test_same_executor_type_preserves_distinct_exact_role_task_claims():
    tasks = [
        {
            "task_id": "api", "name": "API",
            "roles": ["Backend Developer"], "dependencies": [],
            "acceptance_criteria": ["API endpoint responds"],
        },
        {
            "task_id": "worker", "name": "Worker",
            "roles": ["API Developer"], "dependencies": ["api"],
            "acceptance_criteria": ["worker command passes"],
        },
    ]
    phase = _phase(
        roles=("Backend Developer", "API Developer"),
        tasks=tasks,
    )
    assignments = [
        {
            "agent_id": "agent-backend",
            "required_role": "Backend Developer",
            "task_ids": ["api"],
        },
        {
            "agent_id": "agent-backend",
            "required_role": "API Developer",
            "task_ids": ["worker"],
        },
    ]

    validation = validate_task_owners(phase, assignments)
    plan = build_phase_dispatch_plan([phase], "phase-1", assignments)

    assert validation.valid is True
    assert [wave[0]["task_id"] for wave in plan["waves"]] == ["api", "worker"]
    assert [wave[0]["required_role"] for wave in plan["waves"]] == [
        "Backend Developer", "API Developer",
    ]
    api_criterion = plan["waves"][0][0]["acceptance_criteria"][0]
    assert api_criterion["criterion_id"] == "api:acceptance:1"
    assert api_criterion["source_class"] == "semantic"
    assert api_criterion["evidence_spec"] == {
        "source_class": "semantic",
        "observation_required": True,
    }


def test_vague_runtime_and_documented_command_criteria_remain_semantic():
    phase = _phase(roles=("frontend",), tasks=[{
        "task_id": "ui",
        "name": "UI",
        "roles": ["frontend"],
        "dependencies": [],
        "acceptance_criteria": [
            "前端页面显示空白待办界面，具备响应式CSS",
            "运行说明（README.md）已创建，包含安装和启动命令",
        ],
    }])
    plan = build_phase_dispatch_plan(
        [phase],
        "phase-1",
        [{"agent_id": "agent-ui", "required_role": "frontend", "task_ids": ["ui"]}],
    )

    criteria = plan["waves"][0][0]["acceptance_criteria"]
    assert [item["source_class"] for item in criteria] == ["semantic", "semantic"]
    assert all(
        item["evidence_spec"] == {
            "source_class": "semantic",
            "observation_required": True,
        }
        for item in criteria
    )


def test_explicit_endpoint_and_test_command_keep_typed_evidence_contracts():
    phase = _phase(tasks=[{
        "task_id": "api",
        "name": "API",
        "roles": ["backend"],
        "dependencies": [],
        "acceptance_criteria": [
            "GET /api/items returns 200",
            "npm test passes",
        ],
    }])
    plan = build_phase_dispatch_plan(
        [phase],
        "phase-1",
        [{"agent_id": "agent-api", "required_role": "backend", "task_ids": ["api"]}],
    )

    criteria = plan["waves"][0][0]["acceptance_criteria"]
    assert [item["source_class"] for item in criteria] == [
        "api_runtime",
        "command_test",
    ]
    assert criteria[0]["evidence_spec"]["endpoint"] == "/api/items"
    assert "test" in criteria[1]["evidence_spec"]["command_tokens"]


def test_three_display_roles_build_one_canonical_dispatch_plan():
    tasks = [
        {
            "task_id": "ui",
            "name": "UI",
            "roles": ["前端工程师"],
            "dependencies": [],
            "acceptance_criteria": ["frontend build passes"],
        },
        {
            "task_id": "api",
            "name": "API",
            "roles": ["Backend Developer"],
            "dependencies": [],
            "acceptance_criteria": ["API returns 200"],
        },
        {
            "task_id": "deploy",
            "name": "Deploy",
            "roles": ["运维工程师"],
            "dependencies": ["ui", "api"],
            "acceptance_criteria": ["Docker health check passes"],
        },
    ]
    phase = _phase(
        roles=("frontend", "backend", "devops"),
        tasks=tasks,
    )
    assignments = [
        {
            "agent_id": "agent-frontend",
            "required_role": "frontend",
            "task_ids": ["ui"],
        },
        {
            "agent_id": "agent-backend",
            "required_role": "后端工程师",
            "task_ids": ["api"],
        },
        {
            "agent_id": "agent-devops",
            "required_role": "devops",
            "task_ids": ["deploy"],
        },
    ]

    plan = build_phase_dispatch_plan(
        [phase],
        "phase-1",
        assignments,
    )

    assert {item["task_id"] for item in plan["waves"][0]} == {"ui", "api"}
    assert [item["task_id"] for item in plan["waves"][1]] == ["deploy"]
    assert {
        item["required_role"]
        for wave in plan["waves"]
        for item in wave
    } == {"前端工程师", "Backend Developer", "运维工程师"}


def test_dispatch_plan_uses_task_dag_not_role_order():
    tasks = [
        {
            "task_id": "qa-first",
            "name": "QA fixture",
            "roles": ["qa"],
            "dependencies": [],
            "acceptance_criteria": ["fixture exists"],
        },
        {
            "task_id": "backend-after-qa",
            "name": "Backend",
            "roles": ["backend"],
            "dependencies": ["qa-first"],
            "acceptance_criteria": ["backend consumes fixture"],
        },
    ]
    phase = _phase(tasks=tasks)
    assignments = [
        {"agent_id": "agent-qa", "required_role": "qa", "task_ids": ["qa-first"]},
        {
            "agent_id": "agent-backend",
            "required_role": "backend",
            "task_ids": ["backend-after-qa"],
        },
    ]

    plan = build_phase_dispatch_plan([phase], "phase-1", assignments)

    assert [[task["task_id"] for task in wave] for wave in plan["waves"]] == [
        ["qa-first"],
        ["backend-after-qa"],
    ]


def test_dispatch_serializes_independent_tasks_owned_by_same_agent():
    tasks = [
        {
            "task_id": "one", "name": "One", "roles": ["backend"],
            "dependencies": [], "acceptance_criteria": ["one passes"],
        },
        {
            "task_id": "two", "name": "Two", "roles": ["backend"],
            "dependencies": [], "acceptance_criteria": ["two passes"],
        },
    ]
    phase = _phase(roles=("backend",), tasks=tasks)
    assignments = [{
        "agent_id": "agent-backend",
        "required_role": "backend",
        "task_ids": ["one", "two"],
    }]

    plan = build_phase_dispatch_plan([phase], "phase-1", assignments)

    assert [[task["task_id"] for task in wave] for wave in plan["waves"]] == [
        ["one"],
        ["two"],
    ]


def test_dispatch_serializes_different_agents_that_touch_same_file():
    tasks = [
        {
            "task_id": "create-ui", "name": "Create UI", "roles": ["frontend"],
            "dependencies": [], "required_files": ["frontend/src/App.vue"],
            "acceptance_criteria": ["UI exists"],
        },
        {
            "task_id": "wire-ui", "name": "Wire UI", "roles": ["integration"],
            "dependencies": [], "required_files": ["frontend\\src\\App.vue"],
            "acceptance_criteria": ["UI is wired"],
        },
    ]
    phase = _phase(roles=("frontend", "integration"), tasks=tasks)
    assignments = [
        {"agent_id": "agent-ui", "required_role": "frontend", "task_ids": ["create-ui"]},
        {"agent_id": "agent-integration", "required_role": "integration", "task_ids": ["wire-ui"]},
    ]

    plan = build_phase_dispatch_plan([phase], "phase-1", assignments)

    assert [[task["task_id"] for task in wave] for wave in plan["waves"]] == [
        ["create-ui"],
        ["wire-ui"],
    ]


def test_dispatch_keeps_different_agents_with_disjoint_files_parallel():
    tasks = [
        {
            "task_id": "ui", "name": "UI", "roles": ["frontend"],
            "dependencies": [], "required_files": ["frontend/src/App.vue"],
            "acceptance_criteria": ["UI exists"],
        },
        {
            "task_id": "api", "name": "API", "roles": ["backend"],
            "dependencies": [], "required_files": ["backend/app/main.py"],
            "acceptance_criteria": ["API exists"],
        },
    ]
    phase = _phase(roles=("frontend", "backend"), tasks=tasks)
    assignments = [
        {"agent_id": "agent-ui", "required_role": "frontend", "task_ids": ["ui"]},
        {"agent_id": "agent-api", "required_role": "backend", "task_ids": ["api"]},
    ]

    plan = build_phase_dispatch_plan([phase], "phase-1", assignments)

    assert [[task["task_id"] for task in wave] for wave in plan["waves"]] == [["ui", "api"]]


def test_cycle_unknown_dependency_and_future_phase_fail_closed():
    cyclic_tasks = [
        {
            "task_id": "one", "name": "One", "roles": ["backend"],
            "dependencies": ["two"], "acceptance_criteria": ["one"],
        },
        {
            "task_id": "two", "name": "Two", "roles": ["backend"],
            "dependencies": ["one"], "acceptance_criteria": ["two"],
        },
    ]
    cyclic = validate_task_graph([_phase(roles=("backend",), tasks=cyclic_tasks)])
    unknown = validate_task_graph([_phase()])
    unknown_phase = _phase()
    unknown_phase["task_contract"][0]["dependencies"] = ["missing"]
    unknown_phase["expert_requirements"][0]["dependencies"] = ["missing"]
    unknown = validate_task_graph([unknown_phase])
    later = _phase("phase-2")
    earlier = _phase(
        "phase-1",
        tasks=[{
            "task_id": "early", "name": "Early", "roles": ["backend"],
            "dependencies": ["api"], "acceptance_criteria": ["early"],
        }],
    )
    future = validate_task_graph([earlier, later])

    assert "task_dependency_cycle" in {issue.code for issue in cyclic.issues}
    assert "task_dependency_unknown" in {issue.code for issue in unknown.issues}
    assert "task_dependency_future_phase" in {
        issue.code for issue in future.issues
    }


def test_target_phase_graph_ignores_unplanned_future_phases():
    current = _phase("phase-1")
    future = {
        "phase_id": "phase-2",
        "roles_needed": [],
        "dependencies": ["phase-1"],
        "task_contract": [],
        "expert_requirements": [],
    }

    full_project = validate_task_graph([current, future])
    target = validate_task_graph(
        [current, future],
        target_phase_id="phase-1",
    )
    dispatch = build_phase_dispatch_plan(
        [current, future],
        "phase-1",
        _assignments(),
    )

    assert {issue.code for issue in full_project.issues} >= {
        "phase_roles_missing",
        "phase_tasks_missing",
    }
    assert target.valid is True
    assert dispatch["phase_id"] == "phase-1"


def test_target_phase_graph_includes_planned_dependency_closure_only():
    dependency = _phase("phase-1")
    current_tasks = [{
        "task_id": "deploy",
        "name": "Deploy",
        "roles": ["backend"],
        "dependencies": ["qa"],
        "acceptance_criteria": ["deploy passes"],
    }]
    current = _phase(
        "phase-2",
        roles=("backend",),
        dependencies=("phase-1",),
        tasks=current_tasks,
    )
    future = {
        "phase_id": "phase-3",
        "roles_needed": [],
        "dependencies": ["phase-2"],
        "task_contract": [],
        "expert_requirements": [],
    }

    result = validate_task_graph(
        [dependency, current, future],
        target_phase_id="phase-2",
    )

    assert result.valid is True


def test_cross_phase_dependency_must_be_completed_before_dispatch():
    earlier = _phase("phase-1")
    later_tasks = [{
        "task_id": "deploy",
        "name": "Deploy",
        "roles": ["backend"],
        "dependencies": ["qa"],
        "acceptance_criteria": ["deploy passes"],
    }]
    later = _phase(
        "phase-2",
        roles=("backend",),
        dependencies=("phase-1",),
        tasks=later_tasks,
    )
    assignments = [{
        "agent_id": "agent-deploy",
        "required_role": "backend",
        "task_ids": ["deploy"],
    }]

    with pytest.raises(PhaseExecutionContractError) as exc:
        build_phase_dispatch_plan([earlier, later], "phase-2", assignments)

    assert {
        issue.code for issue in exc.value.issues
    } == {"task_dependency_incomplete", "phase_dependency_incomplete"}
    plan = build_phase_dispatch_plan(
        [earlier, later],
        "phase-2",
        assignments,
        completed_task_ids=["api", "qa"],
    )
    assert plan["waves"][0][0]["task_id"] == "deploy"
    assert plan["waves"][0][0]["dependencies"] == ["qa", "api"]


def test_execution_stops_before_downstream_wave_on_failure():
    plan = build_phase_dispatch_plan([_phase()], "phase-1", _assignments())
    called = []

    async def execute(task):
        called.append(task["task_id"])
        return {
            "success": task["task_id"] != "api",
            "status": "failed" if task["task_id"] == "api" else "completed",
        }

    with pytest.raises(PhaseExecutionContractError):
        asyncio.run(execute_phase_dispatch_plan(plan, execute))

    assert called == ["api"]


def test_completion_requires_owner_run_artifact_and_typed_criterion_evidence(tmp_path):
    phase = _phase()
    api_evidence = create_evidence(
        EvidenceKind.API,
        "run-api",
        "metis.runtime_acceptance",
        {
            "endpoint": "/api/items",
            "status_code": 200,
            "assertions": [{"name": "payload", "passed": True}],
            "task_id": "api",
            "task_run_id": "run-api",
            "execution_generation": "generation-1",
            "covered_criterion_ids": ["api:acceptance:1"],
        },
        observed_at=1,
    )
    qa_evidence = _test_evidence(
        "run-qa",
        "pytest regression",
        task_id="qa",
        criterion_id="qa:acceptance:1",
    )
    phase_evidence = create_evidence(
        EvidenceKind.SUPERVISOR_OBSERVATION,
        "supervisor-run",
        "metis.supervisor",
        {
            "actor_id": "supervisor-1",
            "observation_id": "observation-1",
            "scope_digest": "sha256:" + "2" * 64,
            "passed": True,
            "task_id": "phase-1",
            "task_run_id": "supervisor-run",
            "execution_generation": "generation-1",
            "covered_criterion_ids": ["phase-1:acceptance:1"],
        },
        observed_at=2,
    )
    assignments = _assignments()
    registry = {}
    for assignment, path, content in (
        (assignments[0], "backend/app.py", b"app"),
        (assignments[1], "tests/test_app.py", b"test"),
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        assignment["required_files"] = [path]
        assignment["delivery_evidence"] = {
            "files": [{"path": path, "sha256": digest, "size": len(content)}],
        }
        registry[path] = {
            "phase_id": "phase-1",
            "agent_id": assignment["agent_id"],
        }
    runs = {
        "api": {
            "run_id": "run-api", "status": "succeeded",
            "started_at": 1, "finished_at": 2,
                "payload": {"agent_id": "agent-backend", "task_id": "api", "phase_id": "phase-1", "execution_generation": "generation-1"},
            "result": {"evidence": [api_evidence.to_dict()]},
        },
        "qa": {
            "run_id": "run-qa", "status": "succeeded",
            "started_at": 1, "finished_at": 2,
                "payload": {"agent_id": "agent-qa", "task_id": "qa", "phase_id": "phase-1", "execution_generation": "generation-1"},
            "result": {"evidence": [qa_evidence.to_dict()]},
        },
    }
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry=registry,
        run_records=runs,
        criterion_evidence=[
            {
                "task_id": "api",
                "criterion_id": "api:acceptance:1",
                "record": api_evidence.to_dict(),
            },
            {
                "task_id": "qa",
                "criterion_id": "qa:acceptance:1",
                "record": qa_evidence.to_dict(),
            },
        ],
        phase_evidence=[{
            "task_id": "phase-1",
            "criterion_id": "phase-1:acceptance:1",
            "record": phase_evidence.to_dict(),
        }],
        authoritative_evidence={
            phase_evidence.evidence_id: phase_evidence.to_dict(),
        },
    )

    assert validate_phase_execution_evidence(phase, assignments, bundle).valid is True
    assert validate_phase_completion(phase, assignments, bundle).valid is True
    assert all(row["start_run_id"] == row["completion_run_id"] for row in bundle["tasks"])
    assert all(row["artifact_digest"].startswith("sha256:") for row in bundle["tasks"])

    bundle["tasks"][1]["acceptance"] = []
    invalid = validate_phase_completion(phase, assignments, bundle)
    assert invalid.valid is False
    assert "task_acceptance_evidence_missing" in {
        issue.code for issue in invalid.issues
    }


def test_agent_narrative_or_untrusted_evidence_cannot_complete_acceptance():
    phase = _phase()
    fake = _test_evidence("run-api", "pytest api").to_dict()
    fake["producer"] = "agent.narrative"
    bundle = {
        "schema_version": PHASE_EXECUTION_SCHEMA_VERSION,
        "tasks": [{
            "task_id": "api",
            "agent_id": "agent-backend",
            "status": "completed",
            "start_run_id": "run-api",
            "completion_run_id": "run-api",
            "artifact_digest": "sha256:" + "3" * 64,
            "evidence": [fake],
            "acceptance": [{
                "criterion_id": "api:acceptance:1",
                "criterion": "GET /api/items API returns 200",
                "source_class": "api_runtime",
                "evidence_spec": {
                    "source_class": "api_runtime",
                    "endpoint": "/api/items",
                    "status_code": 200,
                    "requires_assertions": True,
                },
                "passed": True,
                "evidence_ids": [fake["evidence_id"]],
            }],
        }],
        "phase_evidence": [],
        "phase_acceptance": [],
    }

    result = validate_phase_completion(phase, _assignments(), bundle)

    codes = {issue.code for issue in result.issues}
    assert "task_evidence_invalid" in codes
    assert "task_acceptance_evidence_invalid" in codes
    assert "task_completion_missing" in codes


def test_user_confirmation_must_exactly_match_authoritative_ledger(tmp_path):
    phase = _phase(
        roles=("backend",),
        tasks=[{
            "task_id": "one", "name": "One", "roles": ["backend"],
            "dependencies": [],
            "acceptance_criteria": ["manual behavior review"],
        }],
    )
    assignments = [{
        "agent_id": "agent-1",
        "required_role": "backend",
        "task_ids": ["one"],
        "required_files": [],
        "delivery_evidence": {},
    }]
    record = create_evidence(
        EvidenceKind.HUMAN_CONFIRMATION,
        "run-one",
        "metis.user_confirmation",
        {
            "passed": True,
            "actor_id": "user-1",
            "observation_id": "ledger-1",
            "scope_digest": "sha256:" + ("8" * 64),
            "task_id": "one",
            "task_run_id": "run-one",
            "execution_generation": "generation-1",
            "covered_criterion_ids": ["one:acceptance:1"],
        },
        observed_at=1,
    ).to_dict()
    tampered_authoritative = {
        **record,
        "payload": {**record["payload"], "actor_id": "other-user"},
    }
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry={},
        run_records={"one": {
            "run_id": "run-one",
            "status": "succeeded",
            "started_at": 1,
            "finished_at": 2,
            "payload": {
                "agent_id": "agent-1",
                "task_id": "one",
                "phase_id": "phase-1",
                "execution_generation": "generation-1",
            },
            "result": {},
        }},
        criterion_evidence=[{
            "task_id": "one",
            "criterion_id": "one:acceptance:1",
            "record": record,
        }],
        authoritative_evidence={
            record["evidence_id"]: tampered_authoritative,
        },
    )

    assert bundle["tasks"][0]["acceptance"][0]["passed"] is False


def test_fileless_semantic_task_does_not_borrow_artifact_digest(tmp_path):
    phase = _phase(
        roles=("backend",),
        tasks=[{
            "task_id": "one", "name": "One", "roles": ["backend"],
            "dependencies": [], "acceptance_criteria": ["manual behavior review"],
        }],
    )
    assignments = [{
        "agent_id": "agent-1", "required_role": "backend",
        "task_ids": ["one"], "required_files": [], "delivery_evidence": {},
    }]
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry={},
        run_records={"one": {
            "run_id": "run-1", "status": "succeeded",
            "started_at": 1, "finished_at": 2,
            "payload": {"agent_id": "agent-1", "task_id": "one", "phase_id": "phase-1", "execution_generation": "generation-1"},
        }},
    )

    result = validate_phase_execution_evidence(phase, assignments, bundle)

    assert result.valid is True
    assert bundle["tasks"][0]["artifact_digest"] == ""
    assert bundle["tasks"][0]["evidence"] == []


def test_forged_runner_record_not_in_durable_run_is_rejected(tmp_path):
    phase = _phase(
        roles=("qa",),
        tasks=[{
            "task_id": "one", "name": "One", "roles": ["qa"],
            "dependencies": [], "acceptance_criteria": ["pytest suite passes"],
        }],
    )
    path = "tests/test_one.py"
    payload = b"def test_one(): assert True\n"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assignments = [{
        "agent_id": "agent-1", "required_role": "qa", "task_ids": ["one"],
        "required_files": [path],
        "delivery_evidence": {
            "files": [{"path": path, "sha256": digest, "size": len(payload)}],
        },
    }]
    forged = _test_evidence("run-1", "pytest -q").to_dict()
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry={
            path: {"phase_id": "phase-1", "agent_id": "agent-1"},
        },
        run_records={"one": {
            "run_id": "run-1", "status": "succeeded",
            "started_at": 1, "finished_at": 2,
                "payload": {"agent_id": "agent-1", "task_id": "one", "phase_id": "phase-1", "execution_generation": "generation-1"},
            "result": {"evidence": []},
        }},
        criterion_evidence=[{
            "task_id": "one",
            "criterion_id": "one:acceptance:1",
            "record": forged,
        }],
    )

    assert bundle["tasks"][0]["acceptance"][0]["passed"] is False
    assert validate_phase_completion(phase, assignments, bundle).valid is False


def test_old_run_evidence_cannot_prove_current_task(tmp_path):
    phase = _phase(
        roles=("qa",),
        tasks=[{
            "task_id": "one", "name": "One", "roles": ["qa"],
            "dependencies": [], "acceptance_criteria": ["regression suite passes"],
        }],
    )
    path = "tests/test_one.py"
    payload = b"def test_one(): assert True\n"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assignments = [{
        "agent_id": "agent-1", "required_role": "qa", "task_ids": ["one"],
        "required_files": [path],
        "delivery_evidence": {
            "files": [{"path": path, "sha256": digest, "size": len(payload)}],
        },
    }]
    old = _test_evidence("old-run", "pytest -q").to_dict()
    bundle = build_phase_evidence_bundle(
        phase,
        assignments,
        workspace=tmp_path,
        file_registry={
            path: {"phase_id": "phase-1", "agent_id": "agent-1"},
        },
        run_records={"one": {
            "run_id": "current-run", "status": "succeeded",
            "started_at": 1, "finished_at": 2,
            "payload": {"agent_id": "agent-1", "task_id": "one", "phase_id": "phase-1", "execution_generation": "generation-1"},
            "result": {"evidence": [old]},
        }},
        criterion_evidence=[{
            "task_id": "one",
            "criterion_id": "one:acceptance:1",
            "record": old,
        }],
    )

    result = validate_phase_completion(phase, assignments, bundle)

    assert result.valid is False
    assert {
        "task_acceptance_evidence_invalid",
        "task_acceptance_not_passed",
    } & {
        issue.code for issue in result.issues
    }
