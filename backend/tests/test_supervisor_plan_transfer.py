import pytest

from agents.supervisor_team import SupervisorLeaderAgent


class _NoopHermes:
    pass


def _leader() -> SupervisorLeaderAgent:
    return SupervisorLeaderAgent(
        agent_id="supervisor-plan-transfer",
        hermes_client=_NoopHermes(),
        project_id="project-plan-transfer",
    )


def test_load_project_plan_accepts_list_tech_stack_and_preserves_signal() -> None:
    leader = _leader()
    plan = {
        "project_overview": "Small API service",
        "core_features": ["health endpoint", "structured errors"],
        "tech_stack": ["FastAPI", "SQLite", "pytest"],
        "phases": [
            {
                "name": "API",
                "description": "Implement and verify the service contract",
            }
        ],
    }

    leader.load_project_plan(plan)

    assert leader.final_plan == plan
    for expected in (
        "Small API service",
        "health endpoint",
        "structured errors",
        "FastAPI",
        "SQLite",
        "pytest",
        "API",
        "Implement and verify the service contract",
    ):
        assert expected in leader.project_background


def test_load_project_plan_keeps_structured_tech_stack_compatible() -> None:
    leader = _leader()
    plan = {
        "project_overview": "Structured stack",
        "core_features": [],
        "tech_stack": {
            "frontend": "React",
            "backend": "FastAPI",
            "database": "PostgreSQL",
        },
        "phases": [],
    }

    leader.load_project_plan(plan)

    for expected in ("React", "FastAPI", "PostgreSQL"):
        assert expected in leader.project_background


@pytest.mark.parametrize("invalid_plan", [None, [], "plan"])
def test_load_project_plan_rejects_non_mapping_payload(invalid_plan) -> None:
    with pytest.raises(TypeError, match="mapping"):
        _leader().load_project_plan(invalid_plan)
