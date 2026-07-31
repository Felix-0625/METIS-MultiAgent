import pytest

from core.phase_manager import PhaseManager, _execution_roles_for_phase
from core.project_contract import finalize_project_contract, parse_project_contract


def test_numeric_model_phase_ids_are_normalized_for_route_lookup(tmp_path) -> None:
    manager = PhaseManager("proj-test", tmp_path)

    phases = manager.init_phases_from_plan({
        "phases": [{"phase_id": 1, "name": "Setup"}],
    })

    assert phases[0]["phase_id"] == "1"
    assert manager.get_phase("1") is phases[0]


def test_legacy_numeric_phase_ids_remain_lookup_compatible(tmp_path) -> None:
    manager = PhaseManager("proj-test", tmp_path)
    manager.phases = [{"phase_id": 1, "name": "Legacy"}]

    assert manager.get_phase("1") is manager.phases[0]


def test_locked_plan_initializes_validated_executable_phase_contract(tmp_path) -> None:
    raw_phase = {
        "phase_id": 1,
        "name": "Foundation",
        "roles": ["backend"],
        "technology_stack": ["Node.js", "Express"],
        "tasks": [{
            "task_id": 1,
            "name": "Create backend",
            "description": "Create the Express backend",
            "dependencies": [],
        }],
    }
    plan = {
        "technology_stack": ["Node.js", "Express"],
        "phases": [raw_phase],
    }
    contract = finalize_project_contract(
        parse_project_contract("Use Node.js and Express."), plan
    )
    manager = PhaseManager("proj-locked", tmp_path)

    phases = manager.init_phases_from_plan({
        **plan, "project_contract": contract.as_mapping()
    })

    phase = phases[0]
    assert phase["roles_needed"] == ["backend"]
    assert phase["execution_roles"] == ["backend"]
    assert phase["tech_stack"] == ["Node.js", "Express"]
    assert phase["plan_contract_validated"] is True
    assert phase["plan_status"] == "saved"
    assert phase["expert_requirements"][0]["task_id"] == "1"


def test_phase_manager_preserves_phase_and_task_trace_fields(tmp_path) -> None:
    requirements = "The API must pass pytest."
    base_contract = parse_project_contract(requirements)
    source_id = base_contract.requirement_units[0].unit_id
    plan = {"phases": [{
        "phase_id": "phase-1", "name": "Delivery",
        "roles_needed": ["backend"],
        "acceptance_criteria": ["API passes"],
        "dependencies": ["phase-0"],
        "source_constraints": ["user outline"],
        "task_contract": [{
            "task_id": "task-1", "name": "API", "description": "Implement API",
            "roles": ["backend"],
            "source_requirement_ids": [source_id],
            "acceptance_criteria": ["pytest passes"],
        }],
    }]}
    contract = finalize_project_contract(base_contract, plan)
    manager = PhaseManager("trace-fields", tmp_path)
    phase = manager.init_phases_from_plan({
        **plan,
        "project_contract": contract.as_mapping(),
    })[0]
    assert phase["acceptance_criteria"] == ["API passes"]
    assert phase["dependencies"] == ["phase-0"]
    assert phase["source_constraints"] == ["user outline"]
    assert phase["task_contract"][0]["source_requirement_ids"] == [source_id]
    assert phase["task_contract"][0]["acceptance_criteria"] == ["pytest passes"]
    assert phase["plan_contract_validated"] is True
    assert phase["expert_requirements"][0]["source_requirement_ids"] == [source_id]
    assert phase["expert_requirements"][0]["acceptance_criteria"] == ["pytest passes"]


def test_product_roles_cannot_be_used_as_phase_execution_roles(tmp_path) -> None:
    raw_phase = {
        "phase_id": "1",
        "name": "Foundation",
        "roles": ["admin", "employee"],
        "tasks": [
            {"task_id": "1", "name": "Build Express API and JWT auth"},
            {"task_id": "2", "name": "Build React frontend"},
            {"task_id": "3", "name": "Create Dockerfile and .env.example"},
        ],
    }
    plan = {
        "technology_stack": ["Node.js", "Express", "React"],
        "roles": ["admin", "employee"],
        "phases": [raw_phase],
    }
    contract = finalize_project_contract(
        parse_project_contract("Use Node.js, Express and React."), plan,
        [
            {"path": "backend/package.json", "owner_type": "backend", "phase_id": "1"},
            {"path": "frontend/package.json", "owner_type": "frontend", "phase_id": "1"},
            {"path": "Dockerfile", "owner_type": "devops", "phase_id": "1"},
        ],
    )
    manager = PhaseManager("proj-product-roles", tmp_path)

    with pytest.raises(ValueError, match="admin, employee"):
        manager.init_phases_from_plan({
            **plan, "project_contract": contract.as_mapping()
        })


def test_mixed_fullstack_phase_keeps_other_execution_roles(tmp_path) -> None:
    manager = PhaseManager("mixed-fullstack", tmp_path)

    phase = manager.init_phases_from_plan({
        "phases": [{
            "phase_id": "phase-1",
            "name": "Integrated delivery",
            "roles_needed": [
                "Full-stack Developer",
                "QA Engineer",
                "DevOps Engineer",
            ],
            "tasks": [
                {"task_id": "app", "required_role": "Full-stack Developer"},
                {"task_id": "qa", "required_role": "QA Engineer"},
                {"task_id": "deploy", "required_role": "DevOps Engineer"},
            ],
        }],
    })[0]

    assert phase["execution_roles"] == [
        "fullstack_engineer",
        "qa",
        "devops",
    ]


def test_file_owners_do_not_expand_explicit_phase_execution_roles() -> None:
    phase = {
        "phase_id": "phase-1",
        "roles_needed": ["Developer"],
        "tasks": [{
            "task_id": "phase-1-task-1",
            "required_role": "Developer",
        }],
    }
    contract = {
        "required_files": [
            {
                "path": "package.json",
                "phase_id": "phase-1",
                "owner_type": "devops",
            },
            {
                "path": "src/server.js",
                "phase_id": "phase-1",
                "owner_type": "backend",
            },
        ],
    }

    assert _execution_roles_for_phase(phase, contract) == [
        "fullstack_engineer",
    ]
