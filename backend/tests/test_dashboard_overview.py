import asyncio
from types import SimpleNamespace

from api import routes_dashboard


def test_agent_duration_stops_at_first_execution_completion():
    agent = {
        "status": "completed",
        "started_at": 100,
        "finished_at": 20_000,
        "lifecycle_events": [
            {"status": "queued", "at": 100},
            {"status": "working", "message": "Execution started", "at": 110},
            {"status": "completed", "message": "Execution finished", "at": 140},
            {"status": "completed", "message": "All locked task attempts completed", "at": 20_000},
        ],
    }

    assert routes_dashboard._agent_execution_duration(agent) == 30


def test_non_terminal_agent_is_not_counted_as_completed_duration():
    assert routes_dashboard._agent_execution_duration({
        "status": "working", "started_at": 100, "finished_at": 200,
    }) is None


def test_corrupted_terminal_history_is_not_reported_as_long_execution():
    assert routes_dashboard._agent_execution_duration({
        "status": "completed",
        "started_at": 100,
        "finished_at": 20_000,
        "lifecycle_events": [{
            "status": "completed",
            "message": "All locked task attempts completed",
            "at": 20_000,
        }],
    }) is None


def test_project_display_status_closes_when_all_agents_are_terminal():
    ctx = SimpleNamespace(status="running")
    agents = [{"status": "completed"}, {"status": "completed"}]

    assert routes_dashboard._project_display_status(ctx, agents) == "completed"


def test_deleted_project_only_remains_in_token_history(monkeypatch):
    live = SimpleNamespace(
        name="Live",
        status="running",
        owner_user_id="user-1",
        agents={},
    )
    monkeypatch.setattr(routes_dashboard, "projects", {"live": live})
    monkeypatch.setattr(routes_dashboard, "get_user_llm_usage", lambda _user_id: {
        "projects": {
            "live": {"total_tokens": 10, "requests": 2},
            "deleted": {
                "project_name": "Deleted",
                "deleted": True,
                "total_tokens": 20,
            },
        },
        "updated_at": 123,
    })

    result = asyncio.run(routes_dashboard.dashboard_overview(
        SimpleNamespace(user_id="user-1")
    ))

    assert [row["project_id"] for row in result["projects"]] == ["live"]
    assert {row["project_id"] for row in result["token_projects"]} == {
        "live", "deleted",
    }
    assert result["summary"]["total_tokens"] == 30
    assert "cache_hit_rate" not in result["projects"][0]


def test_new_live_project_appears_as_zero_token_node_before_first_model_call(
    monkeypatch,
):
    live = SimpleNamespace(
        name="Brand New",
        status="pending",
        owner_user_id="user-1",
        agents={},
    )
    monkeypatch.setattr(routes_dashboard, "projects", {"new-project": live})
    monkeypatch.setattr(routes_dashboard, "get_user_llm_usage", lambda _user_id: {
        "projects": {}, "updated_at": None,
    })

    result = asyncio.run(routes_dashboard.dashboard_overview(
        SimpleNamespace(user_id="user-1")
    ))

    assert result["token_projects"] == [{
        "project_id": "new-project",
        "project_name": "Brand New",
        "status": "active",
        "total_tokens": 0,
    }]
