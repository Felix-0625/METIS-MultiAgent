from types import SimpleNamespace

from core.expert_pool import ExpertPool


def _expert(
    expert_id: str,
    role: str,
    *,
    quality: float,
    status: str,
):
    return SimpleNamespace(
        expert_id=expert_id,
        name=expert_id,
        role=role,
        agent_type="pg",
        domains=[],
        skills=[],
        avg_quality_score=quality,
        status=status,
    )


def test_required_expert_type_filters_before_scoring() -> None:
    pool = ExpertPool.__new__(ExpertPool)
    pool.list_experts = lambda agent_type=None: [
        _expert(
            "frontend-high-score",
            "Frontend Engineer",
            quality=100,
            status="available",
        ),
        _expert(
            "backend-exact",
            "Backend Developer",
            quality=0,
            status="busy",
        ),
    ]

    matches = pool.match_experts(
        "Backend Developer",
        required_expert_type="backend",
        top_k=1,
    )

    assert [match["expert_id"] for match in matches] == ["backend-exact"]
    assert matches[0]["expert_type"] == "backend"


def test_required_expert_type_does_not_relabel_incompatible_expert() -> None:
    pool = ExpertPool.__new__(ExpertPool)
    pool.list_experts = lambda agent_type=None: [
        _expert(
            "frontend-only",
            "Frontend Engineer",
            quality=100,
            status="available",
        ),
    ]

    assert pool.match_experts(
        "Backend Developer",
        required_expert_type="backend",
    ) == []


def test_canonical_data_type_is_accepted_without_alias_reparsing() -> None:
    pool = ExpertPool.__new__(ExpertPool)
    pool.list_experts = lambda agent_type=None: [
        _expert(
            "data-expert",
            "data",
            quality=0,
            status="available",
        ),
    ]

    matches = pool.match_experts(
        "data",
        required_expert_type="data",
    )

    assert [match["expert_type"] for match in matches] == ["data"]
