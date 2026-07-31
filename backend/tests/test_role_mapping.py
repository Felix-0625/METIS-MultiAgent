import pytest

from core.phase_manager import PhaseManager, UnsupportedExecutionRoleError
from core.role_mapping import canonical_expert_type, expert_type_candidates


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("API Engineer", "backend"),
        ("API设计专家", "backend"),
        ("Web Engineer", "fullstack_engineer"),
        ("Platform Engineer", "devops"),
        ("Node.js Engineer", "backend"),
        ("QA\u5de5\u7a0b\u5e08", "qa"),
        ("data", "data"),
        ("软件工程师", "fullstack_engineer"),
    ],
)
def test_common_executor_aliases_resolve(label: str, expected: str) -> None:
    assert canonical_expert_type(label) == expected


@pytest.mark.parametrize(
    "label",
    [
        "Frontend QA Engineer",
        "Backend Security Engineer",
        "DevOps Security Engineer",
        "Full-stack QA Engineer",
        "前端测试工程师",
        "后端安全工程师",
        "Product Designer",
        "Reactive Systems Engineer",
        "Implementation Engineer",
    ],
)
def test_composite_or_unknown_roles_fail_closed(label: str) -> None:
    assert canonical_expert_type(label) is None


def test_fullstack_skill_qualifiers_remain_one_executor() -> None:
    assert expert_type_candidates("Full-stack Node.js Developer") == {
        "fullstack_engineer",
        "backend",
    }
    assert canonical_expert_type("Full-stack Node.js Developer") == (
        "fullstack_engineer"
    )


def test_phase_initialization_rejects_roles_before_mutating_state(tmp_path) -> None:
    manager = PhaseManager("role-rejection", tmp_path)
    manager.phases = [{"phase_id": "existing"}]
    manager.project_contract = {"existing": True}

    with pytest.raises(UnsupportedExecutionRoleError) as exc:
        manager.init_phases_from_plan({
            "project_contract": {"replacement": True},
            "phases": [{
                "phase_id": "phase-1",
                "roles_needed": ["Frontend QA Engineer", "Product Designer"],
            }],
        })

    assert exc.value.roles == ("Frontend QA Engineer", "Product Designer")
    assert manager.phases == [{"phase_id": "existing"}]
    assert manager.project_contract == {"existing": True}


def test_single_string_role_is_not_split_into_characters(tmp_path) -> None:
    manager = PhaseManager("single-role", tmp_path)

    phase = manager.init_phases_from_plan({
        "phases": [{
            "phase_id": "phase-1",
            "roles_needed": "Node.js Engineer",
        }],
    })[0]

    assert phase["execution_roles"] == ["backend"]
