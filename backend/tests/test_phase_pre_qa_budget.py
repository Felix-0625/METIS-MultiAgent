from types import SimpleNamespace

import pytest

from api import routes_phases
from core.phase_execution_contract import acceptance_criterion_contracts
from core.pre_qa_verifier import CommandGate, CommandObservation
from core.project_contract import parse_project_contract
from scripts.industrial_acceptance import PROJECT_REQUIREMENTS


class _PM:
    def __init__(self, phases, required_files):
        self.phases = phases
        self.project_contract = {
            "technology_stack": ["Node.js", "React"],
            "required_files": required_files,
        }
        self.file_registry = {
            item["path"]: {"agent_id": "agent-1"}
            for item in required_files
        }

    def get_phase(self, phase_id):
        return next(item for item in self.phases if item["phase_id"] == phase_id)


class _EvidenceMachine:
    def __init__(self):
        self.records = []

    def record_evidence(self, **kwargs):
        self.records.append(kwargs)


def test_non_applicable_pre_qa_evidence_is_not_added_to_quality_round():
    machine = _EvidenceMachine()
    routes_phases._record_pre_qa_machine_evidence(machine, {
        "evidence": [
            {
                "kind": "test",
                "gate_id": "test-backend",
                "command": "npm test",
                "exit_code": -1,
                "passed": False,
                "applicable": False,
                "executed": False,
                "log_digest": "sha256:optional",
            },
            {
                "kind": "test",
                "gate_id": "test-root",
                "command": "npm test",
                "exit_code": 0,
                "passed": True,
                "applicable": True,
                "executed": True,
                "log_digest": "sha256:passed",
            },
        ],
    })

    assert [record["command"] for record in machine.records] == ["npm test"]
    assert machine.records[0]["metadata"]["applicable"] is True


def test_passed_pre_qa_records_structured_scoped_summary():
    class _ScopedEvidenceMachine(_EvidenceMachine):
        def to_dict(self):
            return {
                "run_id": "run-1",
                "scope": {
                    "project_id": "project-1",
                    "phase_id": "phase-1",
                    "phase_generation_id": "generation-1",
                    "scope_digest": "scope-1",
                    "artifact_digest": "artifact-1",
                },
            }

    machine = _ScopedEvidenceMachine()
    routes_phases._record_pre_qa_machine_evidence(machine, {
        "passed": True,
        "status": "passed",
        "evidence": [{
            "kind": "file_exists",
            "gate_id": "required-file",
            "command": "check required file",
            "exit_code": 0,
            "passed": True,
            "applicable": True,
            "executed": True,
            "log_digest": "sha256:raw",
        }],
    })

    summary = next(record for record in machine.records if record["kind"] == "pre_qa")
    assert summary["passed"] is True
    assert summary["metadata"] == {
        "project_id": "project-1",
        "phase_id": "phase-1",
        "phase_generation_id": "generation-1",
        "scope_digest": "scope-1",
        "artifact_digest": "artifact-1",
        "result": "passed",
        "evidence_count": 1,
    }


def test_failed_pre_qa_does_not_record_passing_summary():
    machine = _EvidenceMachine()
    routes_phases._record_pre_qa_machine_evidence(machine, {
        "passed": False,
        "status": "pre_qa_failed",
        "evidence": [],
    })

    assert not any(record["kind"] == "pre_qa" for record in machine.records)


def _ctx(tmp_path, project_id):
    return SimpleNamespace(
        project_id=project_id,
        workspace=str(tmp_path),
        agents={
            "agent-1": {
                "id": "agent-1",
                "owner_type": "engineer",
                "allowed_path_prefixes": ["phase-one.txt", "final.txt"],
            }
        },
    )


def test_pre_qa_check_sources_bind_task_dependency_and_universal_gate(tmp_path):
    project_id = "pre-qa-source-binding"
    phase = {
        "phase_id": "phase-2",
        "phase_plan": {
            "schema_version": "phase-plan/v1",
            "tasks": [{
                "task_id": "task-current",
                "dependencies": ["task-dependency"],
                "acceptance_criteria": [],
            }],
        },
    }
    pm = _PM(
        [
            {
                "phase_id": "phase-1",
                "phase_plan": {
                    "tasks": [{"task_id": "task-dependency"}],
                },
            },
            phase,
        ],
        [],
    )
    pm.file_registry = {
        "src/current.py": {"task_id": "task-current"},
        "src/dependency.py": {"task_id": "task-dependency"},
    }
    routes_phases._phase_managers[project_id] = pm
    try:
        result = routes_phases._normalize_pre_qa_check_sources(
            _ctx(tmp_path, project_id),
            "phase-2",
            {
                "passed": False,
                "status": "pre_qa_failed",
                "failure_category": "pre_qa_failed",
                "failed_gate": "contract",
                "issues": [
                    {
                        "code": "missing_required_file",
                        "path": "src/current.py",
                        "gate": "contract",
                    },
                    {
                        "code": "owner_mismatch",
                        "path": "src/dependency.py",
                        "gate": "contract",
                    },
                    {
                        "code": "command_gate_failed",
                        "path": "",
                        "gate": "test-root",
                    },
                ],
                "evidence": [],
            },
        )
    finally:
        routes_phases._phase_managers.pop(project_id, None)

    assert result["passed"] is False
    assert all(issue["blocking"] is True for issue in result["issues"])
    source_types = [
        {source["source_type"] for source in issue["check_sources"]}
        for issue in result["issues"]
    ]
    assert "task_contract" in source_types[0]
    assert "dependency_contract" in source_types[1]
    assert "universal_quality_gate" in source_types[2]


def test_untraceable_pre_qa_heuristic_is_warning_only(tmp_path):
    project_id = "pre-qa-untraceable-warning"
    phase = {
        "phase_id": "phase-1",
        "phase_plan": {
            "tasks": [{
                "task_id": "task-current",
                "dependencies": [],
                "acceptance_criteria": [],
            }],
        },
    }
    pm = _PM([phase], [])
    pm.file_registry = {
        "src/current.py": {"task_id": "task-current"},
    }
    routes_phases._phase_managers[project_id] = pm
    try:
        result = routes_phases._normalize_pre_qa_check_sources(
            _ctx(tmp_path, project_id),
            "phase-1",
            {
                "passed": False,
                "status": "pre_qa_failed",
                "failure_category": "pre_qa_failed",
                "failed_gate": "style_guess",
                "issues": [{
                    "code": "subjective_style_guess",
                    "path": "src/current.py",
                    "gate": "style_guess",
                }],
                "evidence": [],
            },
        )
    finally:
        routes_phases._phase_managers.pop(project_id, None)

    assert result["passed"] is True
    assert result["status"] == "passed_with_warnings"
    assert result["issues"] == []
    assert result["warnings"][0]["blocking"] is False
    assert result["warnings"][0]["source_trace_status"] == (
        "untraceable_heuristic"
    )


def test_early_phase_defers_full_project_commands(monkeypatch, tmp_path):
    project_id = "early-phase-budget"
    (tmp_path / "phase-one.txt").write_text("delivered", encoding="utf-8")
    pm = _PM(
        [
            {"phase_id": "phase-1", "user_confirmed": False},
            {"phase_id": "phase-2", "user_confirmed": False},
        ],
        [
            {"path": "phase-one.txt", "phase_id": "phase-1", "owner_type": "engineer"},
            {"path": "future.txt", "phase_id": "phase-2", "owner_type": "engineer"},
        ],
    )
    routes_phases._phase_managers[project_id] = pm
    calls = []
    monkeypatch.setattr(
        routes_phases.LocalCommandRunner,
        "__call__",
        lambda self, gate, workspace: calls.append(gate.gate_id)
        or CommandObservation(0),
    )
    try:
        result = routes_phases._execute_phase_pre_qa(
            _ctx(tmp_path, project_id), "phase-1"
        )
    finally:
        routes_phases._phase_managers.pop(project_id, None)

    assert result["passed"] is True
    assert calls == []
    assert result["deferred_evidence"]
    assert {item["status"] for item in result["deferred_evidence"]} == {"deferred"}
    assert {item["reason"] for item in result["deferred_evidence"]} == {
        "full_project_delivery_incomplete"
    }


def test_early_phase_defers_test_gate_that_references_future_test_file(
    monkeypatch,
    tmp_path,
):
    project_id = "future-test-target"
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"node --test tests/todos.test.js"}}',
        encoding="utf-8",
    )
    phase = {
        "phase_id": "phase-1",
        "user_confirmed": False,
        "acceptance_criteria": ["npm test at repository root exits 0"],
        "task_contract": [{
            "task_id": "phase-1-task-1",
            "acceptance_criteria": ["npm test at repository root exits 0"],
        }],
    }
    pm = _PM(
        [phase, {"phase_id": "phase-2", "user_confirmed": False}],
        [
            {
                "path": "package.json",
                "phase_id": "phase-1",
                "owner_type": "backend",
            },
            {
                "path": "tests/todos.test.js",
                "phase_id": "phase-2",
                "owner_type": "qa",
            },
        ],
    )
    routes_phases._phase_managers[project_id] = pm
    ctx = _ctx(tmp_path, project_id)
    ctx.agents["agent-1"]["owner_type"] = "backend"
    ctx.agents["agent-1"]["allowed_path_prefixes"] = ["package.json"]
    calls = []
    monkeypatch.setattr(
        routes_phases.LocalCommandRunner,
        "__call__",
        lambda self, gate, workspace: calls.append(gate.gate_id)
        or CommandObservation(0),
    )
    try:
        result = routes_phases._execute_phase_pre_qa(
            ctx, "phase-1"
        )
    finally:
        routes_phases._phase_managers.pop(project_id, None)

    assert result["passed"] is True, result
    assert calls == []
    assert "test-root" in {
        item["gate_id"] for item in result["deferred_evidence"]
    }


def test_phase_api_runtime_criterion_runs_and_binds_machine_evidence(
    monkeypatch,
    tmp_path,
):
    project_id = "phase-api-runtime"
    source = tmp_path / "src" / "server.js"
    source.parent.mkdir()
    source.write_text("module.exports = {};\n", encoding="utf-8")
    generation = "generation-1"
    criterion = "GET /health returns 200 with JSON"
    task = {
        "task_id": "phase-1-task-1",
        "acceptance_criteria": [criterion],
    }
    phase = {
        "phase_id": "phase-1",
        "user_confirmed": False,
        "execution_generation": generation,
        "task_contract": [task],
        "acceptance_criteria": [],
    }
    pm = _PM(
        [phase],
        [{
            "path": "src/server.js",
            "phase_id": "phase-1",
            "task_id": "phase-1-task-1",
            "owner_type": "backend",
        }],
    )
    pm.file_registry["src/server.js"] = {
        "agent_id": "agent-1",
        "phase_id": "phase-1",
    }
    receipt = {
        "status": "succeeded",
        "execution_generation": generation,
        "completion_run_id": "run-1",
        "required_files": ["src/server.js"],
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=str(tmp_path),
        agents={
            "agent-1": {
                "id": "agent-1",
                "phase_id": "phase-1",
                "owner_type": "backend",
                "allowed_path_prefixes": ["src/server.js"],
                "locked_tasks": [task],
                "task_execution_receipts": {
                    "phase-1-task-1": receipt,
                },
            }
        },
        supervisor_quality_runs={},
    )
    monkeypatch.setenv("METIS_TEST_MODE", "1")
    def runtime_result(*_args, **kwargs):
        check_id = kwargs["project_contract"]["acceptance_criteria"][0][
            "evidence_spec"
        ]["check_id"]
        return {
            "passed": True,
            "status": "passed",
            "http_observations": [{
                "check_id": check_id,
                "method": "GET",
                "path": "/health",
                "status_code": 200,
                "is_json": True,
                "body": {"status": "ok"},
            }],
        }

    monkeypatch.setattr(
        routes_phases.runtime_acceptance,
        "run_runtime_acceptance",
        runtime_result,
    )
    routes_phases._phase_managers[project_id] = pm
    try:
        result = routes_phases._execute_phase_pre_qa(ctx, "phase-1")
        phase["pre_qa_result"] = result
        routes_phases._record_server_acceptance_evidence(ctx, phase)
    finally:
        routes_phases._phase_managers.pop(project_id, None)

    api_evidence = [
        row for row in result["evidence"] if row["kind"] == "api"
    ]
    assert result["passed"] is True
    assert len(api_evidence) == 1
    assert api_evidence[0]["endpoint"] == "/health"
    assert api_evidence[0]["criterion_bindings"] == [{
        "task_id": "phase-1-task-1",
        "task_run_id": "run-1",
        "execution_generation": generation,
        "criterion_id": "phase-1-task-1:acceptance:1",
    }]
    assert receipt["criterion_evidence"], {
        "result": result,
        "authoritative": phase.get("authoritative_criterion_evidence"),
    }
    assert receipt["criterion_evidence"][0]["criterion_id"] == (
        "phase-1-task-1:acceptance:1"
    )


def test_retryable_non_actionable_runtime_failure_is_infrastructure(
    monkeypatch, tmp_path,
):
    project_id = "phase-api-provider-failure"
    source = tmp_path / "src" / "server.js"
    source.parent.mkdir()
    source.write_text("module.exports = {};\n", encoding="utf-8")
    task = {
        "task_id": "phase-1-task-1",
        "acceptance_criteria": ["GET /health returns 200 with JSON"],
    }
    phase = {
        "phase_id": "phase-1",
        "user_confirmed": False,
        "execution_generation": "generation-1",
        "task_contract": [task],
        "acceptance_criteria": [],
    }
    pm = _PM([phase], [{
        "path": "src/server.js",
        "phase_id": "phase-1",
        "task_id": "phase-1-task-1",
        "owner_type": "backend",
    }])
    pm.file_registry["src/server.js"] = {
        "agent_id": "agent-1", "phase_id": "phase-1",
    }
    ctx = SimpleNamespace(
        project_id=project_id,
        workspace=str(tmp_path),
        agents={"agent-1": {
            "id": "agent-1",
            "phase_id": "phase-1",
            "owner_type": "backend",
            "allowed_path_prefixes": ["src/server.js"],
            "locked_tasks": [task],
        }},
    )
    monkeypatch.setattr(
        routes_phases.runtime_acceptance,
        "run_runtime_acceptance",
        lambda *_args, **_kwargs: {
            "passed": False,
            "status": "provider_error",
            "error_category": "infrastructure_provider_error",
            "retryable": True,
            "actionable": False,
            "summary": "runtime provider temporarily unavailable",
        },
    )
    routes_phases._phase_managers[project_id] = pm
    try:
        result = routes_phases._execute_phase_pre_qa(ctx, "phase-1")
    finally:
        routes_phases._phase_managers.pop(project_id, None)

    assert result["status"] == "infrastructure_failed"
    assert result["failure_category"] == "infrastructure_failed"
    assert result["retryable"] is True
    assert result["actionable"] is False
    assert result["issues"][0]["code"] == "runtime_infrastructure_failed"


def test_phase_command_criterion_uses_owned_package_root_for_gate_binding():
    phase = {
        "phase_id": "phase-1",
        "execution_generation": "generation-1",
        "acceptance_criteria": ["npm install exits 0"],
    }
    ctx = SimpleNamespace(
        agents={
            "agent-1": {
                "id": "agent-1",
                "phase_id": "phase-1",
                "task_execution_receipts": {
                    "task-1": {
                        "status": "succeeded",
                        "execution_generation": "generation-1",
                        "completion_run_id": "run-1",
                        "required_files": ["package.json"],
                    }
                },
            }
        },
    )

    rows = routes_phases._phase_pre_qa_command_contracts(ctx, phase)

    assert rows[0]["contract"]["evidence_spec"]["cwd"] == "."
    assert routes_phases._pre_qa_gate_matches_contract(
        "install-root",
        rows[0],
    ) is True


def test_last_phase_runs_full_project_commands(monkeypatch, tmp_path):
    project_id = "last-phase-budget"
    (tmp_path / "final.txt").write_text("delivered", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"echo build"}}',
        encoding="utf-8",
    )
    pm = _PM(
        [
            {"phase_id": "phase-1", "user_confirmed": True},
            {"phase_id": "phase-2", "user_confirmed": False},
        ],
        [{"path": "final.txt", "phase_id": "phase-2", "owner_type": "engineer"}],
    )
    routes_phases._phase_managers[project_id] = pm
    calls = []
    monkeypatch.setattr(routes_phases, "node_fullstack_command_gates", lambda **_: (
        CommandGate("build-root", "build", ("npm", "run", "build")),
    ))
    monkeypatch.setattr(
        routes_phases.LocalCommandRunner,
        "__call__",
        lambda self, gate, workspace: calls.append(gate.gate_id)
        or CommandObservation(0),
    )
    try:
        result = routes_phases._execute_phase_pre_qa(
            _ctx(tmp_path, project_id), "phase-2"
        )
    finally:
        routes_phases._phase_managers.pop(project_id, None)

    assert result["passed"] is True
    assert calls == ["build-root"]
    assert "deferred_evidence" not in result


@pytest.mark.parametrize(
    ("phase_id", "criterion", "expected_gates"),
    [
        (
            "phase-2",
            "npm test -- --runInBand in backend exits 0",
            ["install-backend", "test-backend"],
        ),
        (
            "phase-3",
            "npm run build in frontend exits 0",
            ["install-frontend", "build-frontend"],
        ),
    ],
)
def test_early_phase_runs_only_its_declared_machine_gate(
    monkeypatch,
    tmp_path,
    phase_id,
    criterion,
    expected_gates,
):
    """Future files defer release gates, not this phase's own acceptance gate."""
    project_id = f"phase-local-{phase_id}"
    current_path = "phase-one.txt"
    (tmp_path / current_path).write_text("delivered", encoding="utf-8")
    package_root = tmp_path / (
        "backend" if phase_id == "phase-2" else "frontend"
    )
    package_root.mkdir()
    package_root.joinpath("package.json").write_text(
        (
            '{"scripts":{"test":"echo test"}}'
            if phase_id == "phase-2"
            else '{"scripts":{"build":"echo build"}}'
        ),
        encoding="utf-8",
    )
    phase_number = int(phase_id.rsplit("-", 1)[1])
    phases = [
        {
            "phase_id": f"phase-{number}",
            "user_confirmed": number < phase_number,
        }
        for number in range(1, 5)
    ]
    current_phase = phases[phase_number - 1]
    current_phase["acceptance_criteria"] = [criterion]
    current_phase["task_contract"] = [{
        "task_id": f"{phase_id}-task-1",
        "acceptance_criteria": [criterion],
    }]
    pm = _PM(
        phases,
        [
            {
                "path": current_path,
                "phase_id": phase_id,
                "owner_type": "engineer",
            },
            {
                "path": "future.txt",
                "phase_id": "phase-4",
                "owner_type": "engineer",
            },
        ],
    )
    routes_phases._phase_managers[project_id] = pm
    calls = []
    monkeypatch.setattr(
        routes_phases,
        "node_fullstack_command_gates",
        lambda **_: (
            CommandGate("install-root", "install", ("npm", "install")),
            CommandGate(
                "install-backend",
                "install",
                ("npm", "install"),
                "backend",
            ),
            CommandGate(
                "install-frontend",
                "install",
                ("npm", "install"),
                "frontend",
            ),
            CommandGate(
                "test-backend",
                "test",
                ("npm", "test", "--", "--runInBand"),
                "backend",
            ),
            CommandGate(
                "build-frontend",
                "build",
                ("npm", "run", "build"),
                "frontend",
            ),
            CommandGate(
                "docker-build",
                "docker_build",
                ("docker", "build", "."),
            ),
        ),
    )
    monkeypatch.setattr(
        routes_phases.LocalCommandRunner,
        "__call__",
        lambda self, gate, workspace: calls.append(gate.gate_id)
        or CommandObservation(0),
    )
    try:
        result = routes_phases._execute_phase_pre_qa(
            _ctx(tmp_path, project_id), phase_id
        )
    finally:
        routes_phases._phase_managers.pop(project_id, None)

    assert result["passed"] is True
    assert calls == expected_gates
    assert not set(expected_gates) & {
        item["gate_id"] for item in result.get("deferred_evidence", [])
    }


@pytest.mark.parametrize(
    ("criterion", "root_gate", "wrong_scoped_gate"),
    [
        (
            "npm test at repository root exits 0",
            "test-root",
            "test-backend",
        ),
        (
            "npm run build at repository root exits 0",
            "build-root",
            "build-frontend",
        ),
    ],
)
def test_explicit_root_criterion_cannot_bind_to_nested_workspace_gate(
    criterion,
    root_gate,
    wrong_scoped_gate,
):
    contract = acceptance_criterion_contracts(
        "phase-4", [criterion]
    )[0]
    # Phase scope naturally mentions all specialist workspaces. Those mentions
    # must not override the criterion's explicit repository-root scope.
    row = {
        "contract": contract,
        "scope_text": (
            "backend developer frontend developer "
            "backend/src/runtime-acceptance.js "
            "frontend/src/release-acceptance.tsx"
        ),
    }

    assert routes_phases._pre_qa_gate_matches_contract(root_gate, row) is True
    assert (
        routes_phases._pre_qa_gate_matches_contract(wrong_scoped_gate, row)
        is False
    )


def test_industrial_command_criteria_have_exact_phase_gate_mapping():
    contract = parse_project_contract(PROJECT_REQUIREMENTS)
    gate_ids = (
        "install-root",
        "install-backend",
        "install-frontend",
        "test-backend",
        "build-frontend",
        "test-root",
        "build-root",
    )
    actual = []
    for phase in contract.phases:
        for task in phase.tasks:
            for criterion in acceptance_criterion_contracts(
                task.task_id, task.acceptance_criteria
            ):
                if criterion["source_class"] != "command_test":
                    continue
                row = {
                    "contract": criterion,
                    "scope_text": str(task.to_dict()).casefold(),
                }
                actual.append((
                    task.task_id,
                    criterion["criterion"],
                    tuple(
                        gate_id for gate_id in gate_ids
                        if routes_phases._pre_qa_gate_matches_contract(
                            gate_id, row
                        )
                    ),
                ))
        for criterion in acceptance_criterion_contracts(
            phase.phase_id, phase.acceptance_criteria
        ):
            if criterion["source_class"] != "command_test":
                continue
            row = {
                "contract": criterion,
                "scope_text": str(phase.to_dict()).casefold(),
            }
            actual.append((
                phase.phase_id,
                criterion["criterion"],
                tuple(
                    gate_id for gate_id in gate_ids
                    if routes_phases._pre_qa_gate_matches_contract(
                        gate_id, row
                    )
                ),
            ))

    assert actual == [
        (
            "phase-2-task-1",
            "npm test in backend exits 0",
            ("test-backend",),
        ),
        (
            "phase-2",
            "npm test in backend exits 0",
            ("test-backend",),
        ),
        (
            "phase-3-task-1",
            "npm run build in frontend exits 0",
            ("build-frontend",),
        ),
        (
            "phase-3",
            "npm run build in frontend exits 0",
            ("build-frontend",),
        ),
        (
            "phase-4-task-2",
            "npm test in backend exits 0",
            ("test-backend",),
        ),
        (
            "phase-4-task-3",
            "npm run build in frontend exits 0",
            ("build-frontend",),
        ),
        (
            "phase-4",
            "npm test at repository root exits 0",
            ("test-root",),
        ),
        (
            "phase-4",
            "npm run build at repository root exits 0",
            ("build-root",),
        ),
    ]
